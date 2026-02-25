import time
import boto3
import threading
import mimetypes
import os
from botocore.exceptions import ClientError
import logging
from typing import Dict, List, Optional
from django.conf import settings
from .models import Deployment, DockerImage
from django.contrib.auth import get_user_model
import json


User = get_user_model()
dir_path = os.path.dirname(os.path.realpath(__file__))
logger = logging.getLogger(__name__)

class AWSupdateServices:
    def __init__(self, region_name: str = 'us-east-1'):
        self.s3_services = boto3.client('s3', region_name=region_name)

    def backup_data_base(self, container):
        pass

    def update_conatiner(self):
        pass

class AWSService:
    def __init__(self, region_name: str = 'us-east-1'):
        self.region_name = region_name
        self.ecs_client = boto3.client('ecs', region_name=region_name)
        self.ecr_client = boto3.client('ecr', region_name=region_name)
        self.stack_formations = boto3.client('cloudformation', region_name=region_name)
        self.logs_client = boto3.client('logs', region_name=region_name)
        self.autoscaling_client = boto3.client('application-autoscaling', region_name=region_name)
        self.secrets_manager_client = boto3.client('secretsmanager', region_name=region_name)
        self.sts_client = boto3.client('sts', region_name=region_name)
        self.ec2_client = boto3.client('ec2', region_name=region_name)
        self.route_53_dns = boto3.client("route53domains")
        self.route_53 = boto3.client("route53")
        self.update_services = AWSupdateServices()

    def create_stack(self, deployment: Deployment, parameters: List[Dict[str, str]], stack_name: str = None) -> str:
        print("Creating CloudFormation stack for deployment:", parameters)
        with open(os.path.join(dir_path, 'ecs-fargate.yml')) as f:
            template_body = f.read()
        stack_name = stack_name or f"{deployment.name}-stack"
        try:
            response = self.stack_formations.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=parameters,
                Capabilities=['CAPABILITY_NAMED_IAM'],
                OnFailure='ROLLBACK'
            )
            return response['StackId']
        except Exception as e:
            logger.error(f"Error creating stack: {str(e)}")
            try:
                self.stack_formations.delete_stack(StackName=stack_name)
                logger.info(f"Stack {stack_name} eliminado tras error en la creación.")
            except Exception as delete_exc:
                logger.error(f"Error eliminando stack tras fallo en la creación: {str(delete_exc)}")
                raise
            raise

    def get_first_task_public_ip(self, stack_name: str) -> str:
        try:
            # 1️⃣ Obtener nombre del ECS Cluster desde el Stack
            print(f"===============>: {stack_name}")
            resources = self.stack_formations.describe_stack_resources(StackName=stack_name)
            print(resources, "****")
            cluster_name = None
            service_arn = None
            for res in resources['StackResources']:
                if res['ResourceType'] == 'AWS::ECS::Cluster':
                    cluster_name = res['PhysicalResourceId']
                
                if res['ResourceType'] == 'AWS::ECS::Service':
                    # El PhysicalResourceId de un AWS::ECS::Service es su ARN completo
                    service_arn = res['PhysicalResourceId']

                # Solo salimos del bucle si ya encontramos ambos para ahorrar tiempo
                if cluster_name and service_arn:
                    break

            if not cluster_name:
                raise Exception(f"No ECS Cluster found in stack: {stack_name}")
            
            if not service_arn:
                logger.warning(f"No ECS Service found in stack: {stack_name}")
            
            # 2️⃣ Listar las tasks activas en el Cluster
            tasks = self.ecs_client.list_tasks(
                cluster=cluster_name,
                desiredStatus='RUNNING'
            )['taskArns']

            if not tasks:
                return {"error": "No running ECS tasks yet."}
                # raise Exception("No running ECS tasks found.")

            task_arn = tasks[0]  # Primera Task

            # 3️⃣ Obtener detalles de la Task para conseguir la ENI
            task_desc = self.ecs_client.describe_tasks(
                cluster=cluster_name,
                tasks=[task_arn]
            )

            attachments = task_desc['tasks'][0]['attachments']
            network_interface_id = None
            for detail in attachments[0]['details']:
                if detail['name'] == 'networkInterfaceId':
                    network_interface_id = detail['value']
                    break

            if not network_interface_id:
                raise Exception("No network interface found for task.")

            # 4️⃣ Consultar la ENI en EC2 para obtener la IP pública
            eni_desc = self.ec2_client.describe_network_interfaces(
                NetworkInterfaceIds=[network_interface_id]
            )

            public_ip = eni_desc['NetworkInterfaces'][0].get('Association', {}).get('PublicIp')

            if not public_ip:
                raise Exception("No network interface found for task.")
                # return {"error": "No available public IP yet for the network interface"}

            return public_ip

        except Exception as e:
            logger.error(f"Error getting public IP: {str(e)}")
            raise

    def get_secret_value(self, secret_name: str) -> Optional[str]:
        try:
            response = self.secrets_manager_client.get_secret_value(SecretId=secret_name)
            if 'SecretString' in response:
                return response['SecretString']
            else:
                return None
        except Exception as e:
            logger.error(f"Error getting secret value: {str(e)}")
            raise

    def get_account_id(self):
        response = self.sts_client.get_caller_identity()
        account_id = response['Account']
        return account_id

    def create_secret(self, secret_name: str, secret_value: dict):
        try:
            response = self.secrets_manager_client.create_secret(
                Name=secret_name,
                SecretString=json.dumps(secret_value)
            )
            return response['ARN']
        except self.secrets_manager_client.exceptions.ResourceExistsException:
            print(f"⚠️ El secreto '{secret_name}' ya existe.")
            existing_secret_arn = self.build_credentials_parameter(secret_name)
            return existing_secret_arn

    def build_credentials_parameter(self, secret_name: str):
        return f"arn:aws:secretsmanager:{self.region_name}:{self.get_account_id()}:secret:{secret_name}"

    def update_service(self, deployment: Deployment, task_definition_arn: str) -> None:
        try:
            self.ecs_client.update_service(
                cluster=deployment.aws_cluster_arn,
                service=deployment.aws_service_arn,
                taskDefinition=task_definition_arn,
                desiredCount=deployment.desired_count,
                deploymentConfiguration={
                    'maximumPercent': 200,
                    'minimumHealthyPercent': 100,
                    'deploymentCircuitBreaker': {
                        'enable': True,
                        'rollback': True
                    }
                }
            )
        except Exception as e:
            logger.error(f"Error updating service: {str(e)}")
            raise

    def get_service_arn_from_stack(self, stack_name: str) -> Optional[str]:
        """Obtiene el ARN del servicio buscando en los recursos del Stack de CloudFormation"""
        try:
            # Pedimos a CloudFormation los recursos del stack
            resources = self.stack_formations.describe_stack_resources(StackName=stack_name)
            for res in resources['StackResources']:
                # El PhysicalResourceId de un recurso AWS::ECS::Service es su ARN completo
                if res['ResourceType'] == 'AWS::ECS::Service':
                    return res['PhysicalResourceId']
            return None
        except Exception as e:
            logger.error(f"Error buscando service ARN en el stack {stack_name}: {str(e)}")
            return None

    # def delete_service(self, deployment: Deployment) -> None:
        """Busca el ARN, lo guarda y elimina el servicio ECS"""
        try:
            # 1. Cargar la configuración de regiones/stacks que guardaste como JSON
            cluster_data = json.loads(deployment.aws_cluster_arn)
            
            for item in cluster_data:
                region = item['region']
                stack_name = item['stack_name']

                # 2. Intentar obtener el ARN del servicio si no lo tenemos
   
                service_arn = item['service_arn']
                if not service_arn or service_arn == "" or service_arn.startswith('['):
                    # Si no lo tenemos, lo buscamos en CloudFormation
                    service_arn = self.get_service_arn_from_stack(stack_name)
                    

                    if service_arn:
                        # 3. Guardarlo en el modelo para que ya quede registrado
                        deployment.aws_service_arn = service_arn
                        deployment.save()
                        logger.info(f"ARN recuperado y guardado: {service_arn}")
                    else:
                        logger.warning(f"No se encontró un servicio activo en el stack {stack_name}")
                        continue

                # 4. Proceder con la eliminación usando el ARN recuperado
                try:
                    logger.info(f"Iniciando eliminación del servicio {service_arn} en {region}")
                    # self.get_service_arn_from_stack(stack_name)
                    # Reducir contador a 0
                    print("*****************", service_arn)
                    self.ecs_client.update_service(
                        cluster=item['cluster_arn'], # O el nombre físico del cluster
                        service=service_arn,
                        desiredCount=0
                    )
                    
                    # Opcional: El waiter puede tardar mucho, podrías omitirlo si vas a borrar el stack completo
                    # self.ecs_client.get_waiter('services_inactive').wait(...)

                    # Eliminar servicio
                    self.ecs_client.delete_service(
                        cluster=item['cluster_arn'], 
                        service=service_arn
                    )
                    logger.info(f"Servicio {service_arn} eliminado exitosamente.")

                except Exception as e:
                    logger.error(f"Error en pasos de borrado ECS: {str(e)}")

        except Exception as e:
            logger.error(f"Error crítico en delete_service: {str(e)}")
            raise
    def delete_service(self, deployment: Deployment) -> None:
        """
        Elimina el servicio ECS de forma robusta.
        Extrae los nombres reales del Cluster y Servicio desde el ARN para evitar 
        errores de validación de Boto3.
        """
        try:
            # 1. Cargar la configuración regional
            cluster_data = json.loads(deployment.aws_cluster_arn)
            
            for item in cluster_data:
                region = item.get('region', self.region_name)
                stack_name = item.get('stack_name')
                # Creamos el cliente regional para asegurar que la API responda en la zona correcta
                regional_ecs = boto3.client('ecs', region_name=region)
                regional_cf = boto3.client('cloudformation', region_name=region)

                # 2. Obtener el ARN del servicio (del JSON o buscándolo en el Stack)
                service_arn = item.get('service_arn')
                if not service_arn or service_arn == "" or service_arn.startswith('['):
                    service_arn = self.get_service_arn_from_stack(stack_name)

                if not service_arn:
                    logger.warning(f"No se encontró servicio activo en el stack {stack_name} de {region}")
                    # Si no hay servicio, procedemos a intentar borrar el stack de todos modos
                    self._cleanup_stack(regional_cf, stack_name)
                    continue

                # 3. LIMPIEZA MAESTRA: Extraer nombres cortos del ARN del Servicio
                # Formato ARN: arn:aws:ecs:region:account:service/cluster-name/service-name
                try:
                    parts = service_arn.split('/')
                    service_name = parts[-1]  # 'ecs-service'
                    cluster_name = parts[-2]  # 'estesiii_6e81cf79' (El nombre real del cluster)
                    
                    print(f"--- Datos Identificados en {region} ---")
                    print(f"Cluster detectado: {cluster_name}")
                    print(f"Service detectado: {service_name}")
                except IndexError:
                    # Fallback por si el service_arn no tiene el formato esperado
                    service_name = service_arn.split('/')[-1]
                    cluster_name = item.get('cluster_arn').split('/')[-1]

                # 4. Proceso de eliminación en AWS
                try:
                    logger.info(f"Deteniendo servicio {service_name} en {cluster_name}...")
                    
                    # Paso A: Escalar a 0 (Obligatorio para poder borrar)
                    regional_ecs.update_service(
                        cluster=cluster_name,
                        service=service_name,
                        desiredCount=0
                    )
                    
                    # Paso B: Borrar el servicio
                    regional_ecs.delete_service(
                        cluster=cluster_name, 
                        service=service_name
                    )
                    logger.info(f"Servicio {service_name} borrado de ECS satisfactoriamente.")

                    # Paso C: Borrar el Stack de CloudFormation (Limpieza total de recursos)
                    self._cleanup_stack(regional_cf, stack_name)

                except regional_ecs.exceptions.ServiceNotFoundException:
                    logger.warning(f"El servicio {service_name} ya no existe. Limpiando stack...")
                    self._cleanup_stack(regional_cf, stack_name)
                except Exception as e:
                    logger.error(f"Error operando en ECS ({region}): {str(e)}")

        except Exception as e:
            logger.error(f"Error crítico en delete_service: {str(e)}")
            raise

    def _cleanup_stack(self, cf_client, stack_name):
        """Método auxiliar para borrar el stack de CloudFormation"""
        try:
            logger.info(f"Iniciando eliminación del Stack: {stack_name}")
            cf_client.delete_stack(StackName=stack_name)
        except Exception as e:
            logger.error(f"No se pudo eliminar el stack {stack_name}: {str(e)}")

    def get_service_status(self, deployment: Deployment) -> Dict:
        try:
            response = self.ecs_client.describe_services(
                cluster=deployment.aws_cluster_arn,
                services=[deployment.aws_service_arn]
            )
            service = response['services'][0]
            return {
                'status': service['status'],
                'runningCount': service['runningCount'],
                'pendingCount': service['pendingCount'],
                'desiredCount': service['desiredCount'],
                'events': service['events'][:5]  # Últimos 5 eventos
            }
        except Exception as e:
            logger.error(f"Error getting service status: {str(e)}")
            raise

    def update_service_scaling(self, deployment: Deployment) -> None:
        """Actualiza la configuración de auto-scaling de un servicio existente"""
        if not deployment.auto_scaling_enabled:
            # Desregistrar el recurso escalable si existe
            try:
                resource_id = f"service/{deployment.aws_cluster_arn.split('/')[-1]}/{deployment.name}-service"
                self.autoscaling_client.deregister_scalable_target(
                    ServiceNamespace='ecs',
                    ResourceId=resource_id,
                    ScalableDimension='ecs:service:DesiredCount'
                )
            except Exception as e:
                logger.warning(f"Error deregistering scalable target: {str(e)}")
            return

        # Actualizar la configuración de auto-scaling
        self.configure_auto_scaling(deployment)

class DeploymentService:
    def __init__(self):
        self.aws_service = AWSService()
        self.database_engine = 'MYSQL'

    def _normalize_schedule_expression(self, schedule: str):
        """
        Accepts 'cron(...)' / 'rate(...)' or a raw cron body like '0 8 * * ? *'
        and returns an Application Auto Scaling compatible expression.
        """
        if not schedule:
            return None
        schedule = str(schedule).strip()
        if not schedule:
            return None
        if schedule.startswith("cron(") or schedule.startswith("rate("):
            return schedule
        return f"cron({schedule})"

    def configure_ecs_scheduler(self, deployment: Deployment, ecs_config: dict, cluster_name: str, service_name: str, regions: list):
        """
        Configures scheduled start/stop for an ECS service using Application Auto Scaling scheduled actions.
        This is the closest AWS-native "scheduler" for ECS desired count.
        """
        scheduler = ecs_config.get("scheduler") or {}
        if not isinstance(scheduler, dict) or not scheduler.get("enabled"):
            return

        start_schedule = self._normalize_schedule_expression(scheduler.get("start_cron"))
        stop_schedule = self._normalize_schedule_expression(scheduler.get("stop_cron"))

        start_desired = scheduler.get("start_desired_count") or ecs_config.get("desiredCount") or 1
        try:
            start_desired = int(start_desired)
        except Exception:
            start_desired = 1
        start_desired = max(1, start_desired)

        max_capacity = ecs_config.get("maxCapacity") or start_desired
        try:
            max_capacity = int(max_capacity)
        except Exception:
            max_capacity = start_desired
        max_capacity = max(start_desired, max_capacity)

        resource_id = f"service/{cluster_name}/{service_name}"

        for region in regions or ["us-east-1"]:
            aws = AWSService(region)

            # Register scalable target (needed for scheduled actions)
            # Retry because the ECS service may still be creating via CloudFormation.
            last_error = None
            for _ in range(12):
                try:
                    aws.autoscaling_client.register_scalable_target(
                        ServiceNamespace="ecs",
                        ResourceId=resource_id,
                        ScalableDimension="ecs:service:DesiredCount",
                        MinCapacity=0,
                        MaxCapacity=max_capacity,
                    )
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    time.sleep(10)

            if last_error:
                logger.error(f"Scheduler: failed to register scalable target for {resource_id} in {region}: {last_error}")
                continue

            # Stop action (scale to 0)
            if stop_schedule:
                stop_action_name = f"{deployment.id}-{region}-stop"
                try:
                    aws.autoscaling_client.put_scheduled_action(
                        ServiceNamespace="ecs",
                        ScheduledActionName=stop_action_name,
                        ResourceId=resource_id,
                        ScalableDimension="ecs:service:DesiredCount",
                        Schedule=stop_schedule,
                        ScalableTargetAction={"MinCapacity": 0, "MaxCapacity": 0},
                    )
                except Exception as e:
                    logger.error(f"Scheduler: failed to create stop scheduled action for {resource_id} in {region}: {e}")

            # Start action (scale to N)
            if start_schedule:
                start_action_name = f"{deployment.id}-{region}-start"
                try:
                    aws.autoscaling_client.put_scheduled_action(
                        ServiceNamespace="ecs",
                        ScheduledActionName=start_action_name,
                        ResourceId=resource_id,
                        ScalableDimension="ecs:service:DesiredCount",
                        Schedule=start_schedule,
                        ScalableTargetAction={"MinCapacity": start_desired, "MaxCapacity": start_desired},
                    )
                except Exception as e:
                    logger.error(f"Scheduler: failed to create start scheduled action for {resource_id} in {region}: {e}")

    def get_or_create_vpc_and_subnets(self, region):
        ec2 = boto3.client('ec2', region_name=region)
        # 1. Buscar VPC existente (preferiblemente la default)
        vpcs = ec2.describe_vpcs()['Vpcs']
        vpc_id = None
        if vpcs:
            for vpc in vpcs:
                if vpc.get('IsDefault'):
                    vpc_id = vpc['VpcId']
                    break
            if not vpc_id:
                vpc_id = vpcs[0]['VpcId']
            # --- NUEVO: Verificar si la VPC tiene IGW adjunto ---
            igws = ec2.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])['InternetGateways']
            if not igws:
                # No hay IGW adjunto, crear y adjuntar uno
                igw = ec2.create_internet_gateway()
                igw_id = igw['InternetGateway']['InternetGatewayId']
                ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            # 2. Buscar subnets públicas asociadas a esa VPC
            subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['Subnets']
            public_subnets = [s for s in subnets if s.get('MapPublicIpOnLaunch')]
            if len(public_subnets) < 2:
                public_subnets = subnets[:2]
            subnet1_id = public_subnets[0]['SubnetId']
            subnet2_id = public_subnets[1]['SubnetId']
            return vpc_id, subnet1_id, subnet2_id
        else:
            # No hay VPCs, crea una nueva y subnets públicas
            vpc = ec2.create_vpc(CidrBlock='10.0.0.0/16')
            vpc_id = vpc['Vpc']['VpcId']
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={'Value': True})
            ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={'Value': True})
            azs = ec2.describe_availability_zones()['AvailabilityZones']
            subnet1 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.1.0/24', AvailabilityZone=azs[0]['ZoneName'])
            subnet2 = ec2.create_subnet(VpcId=vpc_id, CidrBlock='10.0.2.0/24', AvailabilityZone=azs[1]['ZoneName'])
            subnet1_id = subnet1['SubnetId']
            subnet2_id = subnet2['SubnetId']
            igw = ec2.create_internet_gateway()
            igw_id = igw['InternetGateway']['InternetGatewayId']
            ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            route_table = ec2.create_route_table(VpcId=vpc_id)
            rt_id = route_table['RouteTable']['RouteTableId']
            ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock='0.0.0.0/0', GatewayId=igw_id)
            ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet1_id)
            ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet2_id)
            ec2.modify_subnet_attribute(SubnetId=subnet1_id, MapPublicIpOnLaunch={'Value': True})
            ec2.modify_subnet_attribute(SubnetId=subnet2_id, MapPublicIpOnLaunch={'Value': True})
            return vpc_id, subnet1_id, subnet2_id

    def deploy_simple_page_s3(self, local_build_path: str, bucket_name: str, domain_name: str = None, contact_info: dict = None):
        try:
            self.s3_services.head_bucket(Bucket=bucket_name)
            print(f"Bucket {bucket_name} ya existe.")
        except self.s3_services.exceptions.NoSuchBucket:
            self.s3_services.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={'LocationConstraint': self.s3_services.meta.region_name}
            )

        website_config = {
            'ErrorDocument': {'Key': 'index.html'},
            'IndexDocument': {'Suffix': 'index.html'}
        }
        self.s3_services.put_bucket_website(Bucket=bucket_name, WebsiteConfiguration=website_config)
        print(f"Bucket {bucket_name} configurado para hosting web estático.")

        # 3. Sube todos los archivos del proyecto a S3
        for root, dirs, files in os.walk(local_build_path):
            for file in files:
                file_path = os.path.join(root, file)
                key = os.path.relpath(file_path, local_build_path).replace("\\", "/")  # para Windows también
                content_type, _ = mimetypes.guess_type(file_path)
                content_type = content_type or 'application/octet-stream'

                with open(file_path, 'rb') as data:
                    self.s3_services.put_object(
                        Bucket=bucket_name,
                        Key=key,
                        Body=data,
                        ContentType=content_type,
                        ACL='public-read'  # para que sea accesible públicamente
                    )
                print(f"Archivo subido: {key}")

        print(f"Proyecto desplegado en S3: http://{bucket_name}.s3-website-{self.s3_services.meta.region_name}.amazonaws.com")

        # # 4. Si se especifica dominio y datos de contacto, configurar dominio + SSL
        # if domain_name and contact_info:
        #     print("Configurando dominio y SSL...")
        #     self.gestionar_dominio(domain_name, contact_info)
        #     print(f"Dominio {domain_name} configurado con SSL (validación pendiente).")

    def check_status(self):
        print("Checking deployment status...")
        deployments = Deployment.objects.filter(status='pending')
        for deployment in deployments:
            try:
                status = self.get_service_status(deployment)
                if status['status'] == 'ACTIVE':
                    deployment.status = 'active'
                elif status['status'] == 'INACTIVE':
                    deployment.status = 'inactive'
                else:
                    deployment.status = 'pending'
                deployment.save()
            except Exception as e:
                logger.error(f"Error checking status for deployment {deployment.id}: {str(e)}")

    # Crea un deployment en AWS ECS
    def create_deployment(self, deployment: Deployment, docker_images: list, environment_variables: list, ecs_config: dict, user: User) -> None:
        cluster_name_for_scheduler = deployment.aws_cluster_arn
        if ecs_config.get("IsRepoPrivate"):
            images_backend = environment_variables[1].get("name")
            secret_name = f"{images_backend}_{user.username}_1"
            arn_secret = self.aws_service.create_secret(secret_name, ecs_config.get("privateRegistryCredentials", {}))

        database_name = ''
        database_user = ''
        database_password = ''
        database_host = ''
        database_port = ''
        database_root_password = ''
        database_engines = [
            "POSTGRES",
            "MYSQL",
            "MARIADB",
            "ORACLE-SE2",
            "ORACLE-EE",         
            "SQLSERVER-EE",     
            "SQLSERVER-SE",     
            "SQLSERVER-EX",      
            "SQLSERVER-WEB",    
            "AURORA",            
            "AURORA-MYSQL",     
            "AURORA-POSTGRESQL"  
        ]

        for image in docker_images:
            if image.get('name').upper() in database_engines:
                self.database_engine = image.get('name').split(':')[0].upper()
                break


        for env in environment_variables:
            if env.get("name") == "MYSQL_ROOT_PASSWORD":
                database_root_password = env.get("value")

            if env.get("name") == "MYSQL_DATABASE":
                database_name = env.get("value")
           
            if env.get("name") == "MYSQL_USER":
                database_user = env.get("value")
  
            if env.get("name") == "MYSQL_PASSWORD":
                database_password = env.get("value")
             
            if env.get("name") == "MYSQL_HOST":         
                database_host = env.get("value")
              
            if env.get("name") == "MYSQL_PORT":
                database_port = env.get("value")
            
        try:
            parameters_template = [
                {
                    'ParameterKey': 'ClusterName',
                    'ParameterValue': deployment.aws_cluster_arn
                },
                {
                    'ParameterKey': 'TaskName',
                    'ParameterValue': deployment.aws_task_arn or "default-task"
                },
                {
                    'ParameterKey': 'Image1',
                    'ParameterValue': docker_images[0].get('name') if len(docker_images) > 0 else None
                },
                {
                    'ParameterKey': 'Image2',
                   'ParameterValue': docker_images[1].get('name') if len(docker_images) > 1 else None
                },
                {
                    'ParameterKey': 'Image3',
                    'ParameterValue': docker_images[2].get('name').lower() if len(docker_images) > 2 else None
                },
                {
                    'ParameterKey': 'App1Port',
                    'ParameterValue': str(docker_images[0].get('port')) if len(docker_images) > 0 else None
                },
                {
                    'ParameterKey': 'App2Port',
                    'ParameterValue': str(docker_images[1].get('port')) if len(docker_images) > 1 else None
                },
                {
                    'ParameterKey': 'App3Port',
                    'ParameterValue': str(docker_images[2].get('port')) if len(docker_images) > 2 else None
                },
                {
                    'ParameterKey': 'Cpu',
                    'ParameterValue': str(deployment.cpu_units)
                },
                {
                    'ParameterKey': 'Memory',
                    'ParameterValue': str(deployment.memory_mb)
                },
                {
                    'ParameterKey': 'CpuContainer1',
                    'ParameterValue': str(docker_images[0].get('cpu')) if len(docker_images) > 0 else None
                },
                {    'ParameterKey': 'CpuContainer2',
                    'ParameterValue': str(docker_images[1].get('cpu')) if len(docker_images) > 1 else None
                },
                {        
                    'ParameterKey': 'CpuContainer3',
                    'ParameterValue': str(docker_images[2].get('cpu')) if len(docker_images) > 2 else None
                },
                {    
                    'ParameterKey': 'MemoryContainer1',
                    'ParameterValue': str(docker_images[0].get('memory')) if len(docker_images) > 0 else None
                },
                {    
                    'ParameterKey': 'MemoryContainer2',
                    'ParameterValue': str(docker_images[1].get('memory')) if len(docker_images) > 1 else None
                },
                {   'ParameterKey': 'MemoryContainer3',
                    'ParameterValue': str(docker_images[2].get('memory')) if len(docker_images) > 2 else None
                },
                {
                    'ParameterKey': 'DatabaseEngine',
                    'ParameterValue': self.database_engine,
                },
                {
                    'ParameterKey': 'DatabaseName',
                    'ParameterValue': database_name
                },
                { 
                    'ParameterKey': 'DatabaseUser',
                    'ParameterValue':  database_user
                },
                {       
                    'ParameterKey': 'DatabasePassword',        
                    'ParameterValue':  database_password
                },
                {
                    'ParameterKey': 'DatabaseHost',
                    'ParameterValue':  database_host
                },
                {
                    'ParameterKey': 'DatabasePort',
                    'ParameterValue':  database_port
                },
                {
                    'ParameterKey': 'DatabaseRootPassword',
                    'ParameterValue':  database_root_password
                },
                {
                    'ParameterKey': 'DesiredTaskCount',
                    'ParameterValue':  str(ecs_config.get("desiredCount", 1))
                },
                {
                    'ParameterKey': 'CredetialRepository',
                    'ParameterValue':  self.aws_service.build_credentials_parameter(secret_name) if ecs_config.get("IsRepoPrivate") else ""
                },
                {
                    'ParameterKey': 'IsPrivateRepo',
                    'ParameterValue':  'true' if ecs_config.get("IsRepoPrivate") else 'false'
                },
            ]

            if ecs_config.get('loadBalancer', False):
                parameters_template.append({
                    'ParameterKey': 'LoadBalancerEnabled',
                    'ParameterValue': 'true'
                })
            else:
                parameters_template.append({
                    'ParameterKey': 'LoadBalancerEnabled',
                    'ParameterValue': 'false'
                })

            # Multi-region deployment
            regions = ecs_config.get('regions', ['us-east-1'])
            cluster_arns = []
            for idx, region in enumerate(regions):
                if idx == 0:
                    vpc_id = ecs_config.get('vpc_id', 'vpc-0a0b08c33cea3a18a')
                    subnet1 = ecs_config.get('subnet1', 'subnet-08090e6b25ac43e90')
                    subnet2 = ecs_config.get('subnet2', 'subnet-07b334de303749093')
                else:
                    vpc_id, subnet1, subnet2 = self.get_or_create_vpc_and_subnets(region)
                parameters = []
                for param in parameters_template:
                    if param['ParameterKey'] == 'VPCId':
                        parameters.append({'ParameterKey': 'VPCId', 'ParameterValue': vpc_id})
                    elif param['ParameterKey'] == 'PublicSubnetId':
                        parameters.append({'ParameterKey': 'PublicSubnetId', 'ParameterValue': subnet1})
                    elif param['ParameterKey'] == 'PublicSubnetId2':
                        parameters.append({'ParameterKey': 'PublicSubnetId2', 'ParameterValue': subnet2})
                    else:
                        parameters.append(param)
                # Generar un nombre corto y único para el role IAM
                stack_name = f"{deployment.name}-{region}-stack"
                if len(stack_name) > 100:
                    stack_name = stack_name[:100]
                role_name = f"{stack_name}-ecsTaskRole"
                if len(role_name) > 120:
                    role_name = role_name[:120]

                parameters.append({'ParameterKey': 'ECSExecutionRoleName', 'ParameterValue': role_name})

                if not any(p['ParameterKey'] == 'VPCId' for p in parameters):
                    parameters.append({'ParameterKey': 'VPCId', 'ParameterValue': vpc_id})
                if not any(p['ParameterKey'] == 'PublicSubnetId' for p in parameters):
                    parameters.append({'ParameterKey': 'PublicSubnetId', 'ParameterValue': subnet1})
                if not any(p['ParameterKey'] == 'PublicSubnetId2' for p in parameters):
                    parameters.append({'ParameterKey': 'PublicSubnetId2', 'ParameterValue': subnet2})
                aws_service = AWSService(region)
                service_arn = None
                cluster_arn = aws_service.create_stack(deployment, parameters, stack_name=stack_name)
                cluster_arns.append({'region': region, 'cluster_arn': cluster_arn, 'vpc_id': vpc_id,'service_arn': service_arn, 'subnet1': subnet1, 'subnet2': subnet2, 'stack_name': stack_name, 'role_name': role_name})
           
            deployment.aws_cluster_arn = json.dumps(cluster_arns)
            deployment.regions = json.dumps([region['region'] for region in cluster_arns])

            # Handle domain purchasing if domain name is provided
            domain_name = ecs_config.get('domain_name')
            if domain_name:
                try:
                    # Wait for the load balancer to be created and get its DNS name
                    time.sleep(30)  # Give some time for the stack to create resources
                    
                    # Get the load balancer DNS name from the first region
                    if cluster_arns:
                        first_region = cluster_arns[0]
                        aws_service = AWSService(first_region['region'])
                        
                        # Get load balancer DNS name from CloudFormation outputs
                        try:
                            stack_outputs = aws_service.stack_formations.describe_stacks(
                                StackName=first_region['stack_name']
                            )['Stacks'][0]['Outputs']
                            
                            load_balancer_dns = None
                            for output in stack_outputs:
                                if output['OutputKey'] == 'LoadBalancerDNS':
                                    load_balancer_dns = output['OutputValue']
                                    break
                            
                            if load_balancer_dns:
                                # Purchase and configure the domain
                                self.purchase_and_configure_domain(domain_name, load_balancer_dns)
                                deployment.domain_name = domain_name
                                deployment.save()
                        except Exception as e:
                            logger.error(f"Error getting load balancer DNS: {str(e)}")
                            
                except Exception as e:
                    logger.error(f"Error purchasing domain {domain_name}: {str(e)}")

            # Configure scheduler (start/stop) if enabled
            try:
                service_name_for_scheduler = ecs_config.get("serviceName") or "ecs-service"
                self.configure_ecs_scheduler(
                    deployment=deployment,
                    ecs_config=ecs_config,
                    cluster_name=cluster_name_for_scheduler,
                    service_name=service_name_for_scheduler,
                    regions=ecs_config.get("regions", ["us-east-1"]),
                )
            except Exception as e:
                logger.error(f"Error configuring scheduler: {str(e)}")
            
            threading.Timer(120, self.check_status, ).start()
            deployment.save()

        except Exception:
            deployment.status = 'failed'
            deployment.save()
            raise

    # Actualiza un deployment existente
    def update_deployment(self, deployment: Deployment) -> None:
        """Actualiza un deployment existente"""
        try:
            task_definition_arn = self.aws_service.create_task_definition(deployment)
            self.aws_service.update_service(deployment, task_definition_arn)
        except Exception as e:
            raise
    

    # Elimina un deployment existente
    def delete_deployment(self, deployment: Deployment) -> None:
        """Elimina un deployment"""
        try:
            deployment.status = 'deleting'
            deployment.save()

            # Eliminar servicio
            
            self.aws_service.delete_service(deployment)

            # Eliminar deployment de la base de datos
            deployment.delete()

        except Exception as e:
            deployment.status = 'failed'
            deployment.save()
            raise

    # Obtiene los logs de un deployment
    def get_deployment_status(self, deployment: Deployment) -> Dict:
        """Obtiene el estado actual del deployment"""
        try:
            status = self.aws_service.get_service_status(deployment)
            
            # Actualizar estado en la base de datos
            if status['runningCount'] == deployment.desired_count:
                deployment.status = 'running'
            elif status['pendingCount'] > 0:
                deployment.status = 'creating'
            elif status['runningCount'] == 0:
                deployment.status = 'stopped'
            deployment.save()

            return status

        except Exception as e:
            logger.error(f"Error getting deployment status: {str(e)}")
            raise

    # Configura un dominio y SSL en Route 53
    def get_domain(self, domain_name, contact_info):
        try:
            # 1. Verificar disponibilidad del dominio
            response = self.route53domains.check_domain_availability(DomainName=domain_name)
            status = response['Availability']
            print(f"Disponibilidad del dominio '{domain_name}': {status}")
        except ClientError as e:
            print(f"Error al verificar disponibilidad: {e}")
            return

        # 2. Registrar dominio si está disponible
        if status == 'AVAILABLE':
            try:
                print("Dominio disponible. Registrando...")
                self.route53domains.register_domain(
                    DomainName=domain_name,
                    DurationInYears=1,
                    AutoRenew=True,
                    AdminContact=contact_info,
                    RegistrantContact=contact_info,
                    TechContact=contact_info,
                    PrivacyProtectAdminContact=True,
                    PrivacyProtectRegistrantContact=True,
                    PrivacyProtectTechContact=True
                )
                print("Dominio registrado. Puede tardar unos minutos en estar activo.")
            except ClientError as e:
                print(f"Error al registrar dominio: {e}")
                return
        else:
            print("Dominio ya registrado. Se creará zona hospedada.")

        # 3. Crear zona hospedada pública en Route 53
        try:
            response = self.route53.create_hosted_zone(
                Name=domain_name,
                CallerReference=str(time.time()),
                HostedZoneConfig={'Comment': 'Zona hospedada automática', 'PrivateZone': False}
            )
            hosted_zone_id = response['HostedZone']['Id']
            print("Zona hospedada creada con éxito.")
            ns = response['DelegationSet']['NameServers']
            print("NameServers que debes configurar en tu registrador:")
            for n in ns:
                print(f" - {n}")
        except ClientError as e:
            print(f"Error al crear zona hospedada: {e}")
            return

        # 4. Solicitar certificado SSL en ACM (en us-east-1)
        try:
            acm = boto3.client('acm', region_name='us-east-1')
            cert_response = acm.request_certificate(
                DomainName=domain_name,
                ValidationMethod='DNS',
                SubjectAlternativeNames=[f'www.{domain_name}'],
                IdempotencyToken=str(int(time.time())),
                Options={'CertificateTransparencyLoggingPreference': 'ENABLED'}
            )
            cert_arn = cert_response['CertificateArn']
            print(f"Certificado solicitado con ARN: {cert_arn}")

            # Esperar a que ACM genere las opciones de validación DNS
            time.sleep(5)
            cert_detail = acm.describe_certificate(CertificateArn=cert_arn)
            validation_options = cert_detail['Certificate']['DomainValidationOptions']

            # Crear registros DNS para validación en Route 53
            changes = []
            for option in validation_options:
                if 'ResourceRecord' in option:
                    rr = option['ResourceRecord']
                    print(f"Creando registro DNS para validación SSL:")
                    print(f"  Nombre: {rr['Name']}")
                    print(f"  Tipo: {rr['Type']}")
                    print(f"  Valor: {rr['Value']}")

                    changes.append({
                        'Action': 'UPSERT',
                        'ResourceRecordSet': {
                            'Name': rr['Name'],
                            'Type': rr['Type'],
                            'TTL': 300,
                            'ResourceRecords': [{'Value': rr['Value']}]
                        }
                    })

            if changes:
                self.route53.change_resource_record_sets(
                    HostedZoneId=hosted_zone_id,
                    ChangeBatch={'Changes': changes}
                )
                print("Registros DNS de validación creados en Route 53.")
            else:
                print("No se encontraron registros DNS para crear.")

            print("El certificado SSL se activará una vez se valide el dominio (puede tardar varios minutos).")

        except ClientError as e:
            print(f"Error al solicitar certificado SSL: {e}")
            return

        print(f"Dominio '{domain_name}' configurado con SSL pendiente de validación.")
    
    def purchase_and_configure_domain(self, domain_name: str, load_balancer_dns: str):
        """Purchase domain in Route53 and configure DNS to point to load balancer"""
        try:
            # Initialize Route53 Domains client
            route53_domains_client = boto3.client(
                'route53domains',
                region_name='us-east-1',
                aws_access_key_id=getattr(settings, 'AWS_ACCESS_KEY_ID', None),
                aws_secret_access_key=getattr(settings, 'AWS_SECRET_ACCESS_KEY', None)
            )
            
            # Initialize Route53 client for DNS management
            route53_client = boto3.client(
                'route53',
                aws_access_key_id=getattr(settings, 'AWS_ACCESS_KEY_ID', None),
                aws_secret_access_key=getattr(settings, 'AWS_SECRET_ACCESS_KEY', None)
            )
            
            # Register the domain
            registration_response = route53_domains_client.register_domain(
                DomainName=domain_name,
                DurationInYears=1,
                AutoRenew=True,
                AdminContact={
                    'FirstName': 'Admin',
                    'LastName': 'User',
                    'ContactType': 'PERSON',
                    'OrganizationName': 'Your Organization',
                    'AddressLine1': '123 Main St',
                    'City': 'City',
                    'State': 'State',
                    'CountryCode': 'US',
                    'ZipCode': '12345',
                    'PhoneNumber': '+1.1234567890',
                    'Email': 'admin@example.com'
                },
                RegistrantContact={
                    'FirstName': 'Admin',
                    'LastName': 'User',
                    'ContactType': 'PERSON',
                    'OrganizationName': 'Your Organization',
                    'AddressLine1': '123 Main St',
                    'City': 'City',
                    'State': 'State',
                    'CountryCode': 'US',
                    'ZipCode': '12345',
                    'PhoneNumber': '+1.1234567890',
                    'Email': 'admin@example.com'
                },
                TechContact={
                    'FirstName': 'Admin',
                    'LastName': 'User',
                    'ContactType': 'PERSON',
                    'OrganizationName': 'Your Organization',
                    'AddressLine1': '123 Main St',
                    'City': 'City',
                    'State': 'State',
                    'CountryCode': 'US',
                    'ZipCode': '12345',
                    'PhoneNumber': '+1.1234567890',
                    'Email': 'admin@example.com'
                }
            )
            
            # Get the hosted zone ID for the domain
            hosted_zones = route53_client.list_hosted_zones()
            domain_hosted_zone = None
            
            for zone in hosted_zones['HostedZones']:
                if zone['Name'] == f'{domain_name}.':
                    domain_hosted_zone = zone
                    break
            
            if not domain_hosted_zone:
                # Create hosted zone for the domain
                hosted_zone_response = route53_client.create_hosted_zone(
                    Name=domain_name,
                    CallerReference=f'{domain_name}-{int(time.time())}'
                )
                hosted_zone_id = hosted_zone_response['HostedZone']['Id']
            else:
                hosted_zone_id = domain_hosted_zone['Id']
            
            # Create A record pointing to the load balancer
            route53_client.change_resource_record_sets(
                HostedZoneId=hosted_zone_id,
                ChangeBatch={
                    'Changes': [
                        {
                            'Action': 'UPSERT',
                            'ResourceRecordSet': {
                                'Name': domain_name,
                                'Type': 'A',
                                'AliasTarget': {
                                    'HostedZoneId': 'Z35SXDOTRQ7X7K',  # ALB hosted zone ID for us-east-1
                                    'DNSName': load_balancer_dns,
                                    'EvaluateTargetHealth': True
                                }
                            }
                        }
                    ]
                }
            )
            
            logger.info(f"Domain {domain_name} purchased and configured successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error purchasing domain {domain_name}: {str(e)}")
            raise