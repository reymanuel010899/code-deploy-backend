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
        logger.debug("Creating CloudFormation stack for deployment: %s", parameters)
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
        except self.stack_formations.exceptions.AlreadyExistsException:
            existing = self.stack_formations.describe_stacks(StackName=stack_name)
            stack = existing['Stacks'][0]
            stack_status = stack['StackStatus']
            # Estados terminales donde el stack no se puede reutilizar → eliminar y recrear
            dead_states = {
                'ROLLBACK_COMPLETE', 'ROLLBACK_FAILED',
                'CREATE_FAILED', 'DELETE_FAILED',
                'UPDATE_ROLLBACK_FAILED',
            }
            if stack_status in dead_states:
                logger.warning("Stack %s en estado %s, eliminando para recrear...", stack_name, stack_status)
                self.stack_formations.delete_stack(StackName=stack_name)
                # Esperar a que termine de eliminarse (máx 5 min)
                waiter = self.stack_formations.get_waiter('stack_delete_complete')
                waiter.wait(StackName=stack_name, WaiterConfig={'Delay': 10, 'MaxAttempts': 30})
                logger.info("Stack %s eliminado. Recreando...", stack_name)
                response = self.stack_formations.create_stack(
                    StackName=stack_name,
                    TemplateBody=template_body,
                    Parameters=parameters,
                    Capabilities=['CAPABILITY_NAMED_IAM'],
                    OnFailure='ROLLBACK'
                )
                return response['StackId']
            else:
                logger.warning("Stack %s ya existe en estado %s, reutilizando.", stack_name, stack_status)
                return stack['StackId']
        except Exception as e:
            logger.error(f"Error creating stack: {str(e)}")
            raise

    def get_first_task_public_ip(self, stack_name: str) -> str:
        try:
            # 1️⃣ Obtener nombre del ECS Cluster desde el Stack
            resources = self.stack_formations.describe_stack_resources(StackName=stack_name)
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
            logger.warning("El secreto '%s' ya existe, reutilizando.", secret_name)
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
                    'maximumPercent': 100,
                    'minimumHealthyPercent': 50,
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
            resources = self.stack_formations.describe_stack_resources(StackName=stack_name)
            for res in resources['StackResources']:
                if res['ResourceType'] == 'AWS::ECS::Service':
                    return res['PhysicalResourceId']
            return None
        except Exception as e:
            logger.error(f"Error buscando service ARN en el stack {stack_name}: {str(e)}")
            return None

    def get_cluster_arn_from_stack(self, stack_name: str) -> Optional[str]:
        """Obtiene el ARN del cluster ECS buscando en los recursos del Stack de CloudFormation"""
        try:
            resources = self.stack_formations.describe_stack_resources(StackName=stack_name)
            for res in resources['StackResources']:
                if res['ResourceType'] == 'AWS::ECS::Cluster':
                    return res['PhysicalResourceId']
            return None
        except Exception as e:
            logger.error(f"Error buscando cluster ARN en el stack {stack_name}: {str(e)}")
            return None

    # def delete_service(self, deployment: Deployment) -> None:
        # """Busca el ARN, lo guarda y elimina el servicio ECS"""
        # try:
        #     # 1. Cargar la configuración de regiones/stacks que guardaste como JSON
        #     cluster_data = json.loads(deployment.aws_cluster_arn)
            
        #     for item in cluster_data:
        #         region = item['region']
        #         stack_name = item['stack_name']

        #         # 2. Intentar obtener el ARN del servicio si no lo tenemos
   
        #         service_arn = item['service_arn']
        #         if not service_arn or service_arn == "" or service_arn.startswith('['):
        #             # Si no lo tenemos, lo buscamos en CloudFormation
        #             service_arn = self.get_service_arn_from_stack(stack_name)
                    

        #             if service_arn:
        #                 # 3. Guardarlo en el modelo para que ya quede registrado
        #                 deployment.aws_service_arn = service_arn
        #                 deployment.save()
        #                 logger.info(f"ARN recuperado y guardado: {service_arn}")
        #             else:
        #                 logger.warning(f"No se encontró un servicio activo en el stack {stack_name}")
        #                 continue

        #         # 4. Proceder con la eliminación usando el ARN recuperado
        #         try:
        #             logger.info(f"Iniciando eliminación del servicio {service_arn} en {region}")
        #             # self.get_service_arn_from_stack(stack_name)
        #             # Reducir contador a 0
        #             print("*****************", service_arn)
        #             self.ecs_client.update_service(
        #                 cluster=item['cluster_arn'], # O el nombre físico del cluster
        #                 service=service_arn,
        #                 desiredCount=0
        #             )
                    
        #             # Opcional: El waiter puede tardar mucho, podrías omitirlo si vas a borrar el stack completo
        #             # self.ecs_client.get_waiter('services_inactive').wait(...)

        #             # Eliminar servicio
        #             self.ecs_client.delete_service(
        #                 cluster=item['cluster_arn'], 
        #                 service=service_arn
        #             )
        #             logger.info(f"Servicio {service_arn} eliminado exitosamente.")

        #         except Exception as e:
        #             logger.error(f"Error en pasos de borrado ECS: {str(e)}")

        # except Exception as e:
        #     logger.error(f"Error crítico en delete_service: {str(e)}")
        #     raise
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
                    
                    logger.debug("Región: %s | Cluster: %s | Service: %s", region, cluster_name, service_name)
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

    def delete_lambda(self, deployment: Deployment) -> None:
        """Elimina la función Lambda y su API Gateway asociado."""
        region = deployment.regions[0] if deployment.regions else self.region_name
        lambda_client = boto3.client('lambda', region_name=region)
        apigw_client = boto3.client('apigatewayv2', region_name=region)

        # 1. Eliminar API Gateway
        if deployment.aws_task_arn and deployment.aws_task_arn.startswith('apigw:'):
            api_id = deployment.aws_task_arn.replace('apigw:', '')
            try:
                apigw_client.delete_api(ApiId=api_id)
                logger.info(f"API Gateway {api_id} eliminado.")
            except apigw_client.exceptions.NotFoundException:
                logger.warning(f"API Gateway {api_id} no encontrado, ya fue eliminado.")
            except Exception as e:
                logger.error(f"Error eliminando API Gateway {api_id}: {str(e)}")

        # 2. Eliminar función Lambda
        if deployment.aws_service_arn:
            function_name = deployment.aws_service_arn.split(':')[-1] if ':' in deployment.aws_service_arn else deployment.aws_service_arn
            try:
                lambda_client.delete_function(FunctionName=deployment.aws_service_arn)
                logger.info(f"Función Lambda {function_name} eliminada.")
            except lambda_client.exceptions.ResourceNotFoundException:
                logger.warning(f"Función Lambda {function_name} no encontrada, ya fue eliminada.")
            except Exception as e:
                logger.error(f"Error eliminando función Lambda {function_name}: {str(e)}")

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

    def deploy_lambda(self, deployment: Deployment, lambda_config: dict) -> dict:
        """
        Despliega una funcion Lambda y la conecta con un API Gateway HTTP.
        Retorna { function_arn, api_id, api_url }.
        """
        import zipfile
        import io

        region = deployment.aws_region or self.region_name
        lambda_client = boto3.client('lambda', region_name=region)
        apigw_client = boto3.client('apigatewayv2', region_name=region)
        iam_client = boto3.client('iam', region_name=region)
        account_id = self.get_account_id()

        function_name = f"zaplet-{deployment.name}-{deployment.id}"
        # Recortar para cumplir limite de 64 chars de Lambda
        function_name = function_name[:64].rstrip('-')

        runtime = lambda_config.get('runtime', 'nodejs18.x')
        handler = lambda_config.get('handler', 'index.handler')
        timeout = min(int(lambda_config.get('timeout', 30)), 900)
        memory = int(lambda_config.get('memory', 128))
        http_method = (lambda_config.get('http_method') or lambda_config.get('httpMethod') or 'GET').upper()
        valid_methods = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'ANY'}
        if http_method not in valid_methods:
            http_method = 'GET'
        # codeFiles puede ser lista de dicts o de OrderedDicts — normalizamos a dicts planos
        raw_files = lambda_config.get('codeFiles') or lambda_config.get('code_files') or []
        code_files = [dict(f) for f in raw_files]
        env_vars_raw = lambda_config.get('environmentVars', '') or ''

        logger.info(f"deploy_lambda: runtime={runtime}, handler={handler}, code_files count={len(code_files)}")
        for cf in code_files:
            logger.info(f"  archivo: {cf.get('name')}, content length: {len(cf.get('content', ''))}")

        # Parsear variables de entorno (formato CLAVE=valor por linea)
        env_variables = {}
        for line in env_vars_raw.splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                key, _, val = line.partition('=')
                env_variables[key.strip()] = val.strip()

        # Si no hay archivos de codigo o estan todos vacios, usar handler por defecto
        default_handler_file = 'index.js'
        default_content = (
            'exports.handler = async (event, context) => {\n'
            '    return {\n'
            '        statusCode: 200,\n'
            '        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },\n'
            '        body: JSON.stringify({ message: "Hello from Zaplet!" })\n'
            '    };\n'
            '};\n'
        )
        if not code_files or all(not cf.get('content', '').strip() for cf in code_files):
            logger.warning("No hay codigo valido en codeFiles, usando handler por defecto")
            code_files = [{'name': default_handler_file, 'content': default_content}]

        # Construir ZIP del codigo en memoria
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for code_file in code_files:
                content = code_file.get('content', '').strip()
                if not content:
                    content = f"// {code_file.get('name', 'module')}\n"
                zf.writestr(code_file.get('name', 'index.js'), content)
        zip_buffer.seek(0)
        zip_bytes = zip_buffer.read()
        logger.info(f"ZIP generado: {len(zip_bytes)} bytes")

        # Obtener o crear el IAM role de ejecucion para Lambda
        role_name = f"zaplet-lambda-role-{account_id}"
        try:
            role_response = iam_client.get_role(RoleName=role_name)
            role_arn = role_response['Role']['Arn']
            logger.info(f"Usando IAM role existente: {role_arn}")
        except iam_client.exceptions.NoSuchEntityException:
            logger.info(f"Creando IAM role: {role_name}")
            trust_policy = json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }]
            })
            role_response = iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=trust_policy,
                Description="Zaplet Lambda execution role"
            )
            role_arn = role_response['Role']['Arn']
            # Adjuntar politica basica de ejecucion Lambda (logs en CloudWatch)
            iam_client.attach_role_policy(
                RoleName=role_name,
                PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
            )
            # Esperar a que el role este disponible
            time.sleep(10)

        # Crear o actualizar la funcion Lambda
        kwargs_common = {
            'Runtime': runtime,
            'Role': role_arn,
            'Handler': handler,
            'Timeout': timeout,
            'MemorySize': memory,
        }
        if env_variables:
            kwargs_common['Environment'] = {'Variables': env_variables}

        try:
            existing = lambda_client.get_function(FunctionName=function_name)
            logger.info(f"Actualizando funcion Lambda existente: {function_name}")
            lambda_client.update_function_code(
                FunctionName=function_name,
                ZipFile=zip_bytes
            )
            # Esperar a que la actualizacion termine antes de cambiar config
            waiter = lambda_client.get_waiter('function_updated')
            waiter.wait(FunctionName=function_name)
            lambda_client.update_function_configuration(
                FunctionName=function_name,
                **kwargs_common
            )
            function_arn = existing['Configuration']['FunctionArn']
        except lambda_client.exceptions.ResourceNotFoundException:
            logger.info(f"Creando funcion Lambda: {function_name}")
            response = lambda_client.create_function(
                FunctionName=function_name,
                Code={'ZipFile': zip_bytes},
                **kwargs_common
            )
            function_arn = response['FunctionArn']
            # Esperar a que la funcion este activa
            waiter = lambda_client.get_waiter('function_active')
            waiter.wait(FunctionName=function_name)

        # Crear o reutilizar el API Gateway HTTP
        api_name = f"zaplet-api-{deployment.name}-{deployment.id}"
        api_name = api_name[:128]

        existing_api_id = None
        # Buscar si ya existe un API para este deployment
        if deployment.aws_task_arn and deployment.aws_task_arn.startswith('apigw:'):
            existing_api_id = deployment.aws_task_arn.replace('apigw:', '')

        if existing_api_id:
            try:
                apigw_client.get_api(ApiId=existing_api_id)
                api_id = existing_api_id
                logger.info(f"Reutilizando API Gateway: {api_id}")
            except Exception:
                existing_api_id = None

        if not existing_api_id:
            api_response = apigw_client.create_api(
                Name=api_name,
                ProtocolType='HTTP',
                CorsConfiguration={
                    'AllowOrigins': ['*'],
                    'AllowMethods': ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH'],
                    'AllowHeaders': ['*'],
                }
            )
            api_id = api_response['ApiId']
            logger.info(f"API Gateway creado: {api_id}")

        api_url = f"https://{api_id}.execute-api.{region}.amazonaws.com"

        # RouteKey: "ANY /{proxy+}" atrapa todo, los demás solo ese método
        route_key = '$default' if http_method == 'ANY' else f'{http_method} /'
        logger.info(f"RouteKey a usar: {route_key}")

        # 1. Dar permiso a API Gateway para invocar la Lambda ANTES de crear la integracion
        source_arn = f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*/*"
        statement_id = f"apigw-invoke-{api_id}"
        try:
            lambda_client.add_permission(
                FunctionName=function_name,
                StatementId=statement_id,
                Action='lambda:InvokeFunction',
                Principal='apigateway.amazonaws.com',
                SourceArn=source_arn,
            )
            logger.info("Permiso Lambda → API Gateway añadido")
        except lambda_client.exceptions.ResourceConflictException:
            logger.info("Permiso Lambda → API Gateway ya existe")

        # 2. Crear integracion Lambda → API Gateway
        # Si el API ya existia, limpiar integraciones anteriores para evitar duplicados
        existing_integrations = apigw_client.get_integrations(ApiId=api_id).get('Items', [])
        for integ in existing_integrations:
            try:
                apigw_client.delete_integration(ApiId=api_id, IntegrationId=integ['IntegrationId'])
            except Exception:
                pass

        integration_response = apigw_client.create_integration(
            ApiId=api_id,
            IntegrationType='AWS_PROXY',
            IntegrationUri=function_arn,
            PayloadFormatVersion='2.0',
            TimeoutInMillis=min(int(timeout * 1000), 29000),
        )
        integration_id = integration_response['IntegrationId']
        logger.info(f"Integracion creada: {integration_id}")

        # 3. Crear/actualizar la ruta con el método HTTP elegido
        existing_routes = apigw_client.get_routes(ApiId=api_id).get('Items', [])
        # Eliminar rutas anteriores para evitar conflictos al cambiar método
        for old_route in existing_routes:
            try:
                apigw_client.delete_route(ApiId=api_id, RouteId=old_route['RouteId'])
            except Exception:
                pass
        apigw_client.create_route(
            ApiId=api_id,
            RouteKey=route_key,
            Target=f'integrations/{integration_id}',
        )
        logger.info(f"Ruta '{route_key}' creada")

        # 4. Crear stage $default con AutoDeploy — si ya existe, actualizar para forzar re-deploy
        existing_stages = apigw_client.get_stages(ApiId=api_id).get('Items', [])
        default_stage = next((s for s in existing_stages if s['StageName'] == '$default'), None)
        if default_stage:
            apigw_client.update_stage(
                ApiId=api_id,
                StageName='$default',
                AutoDeploy=True,
            )
            logger.info("Stage $default actualizado")
        else:
            apigw_client.create_stage(
                ApiId=api_id,
                StageName='$default',
                AutoDeploy=True,
            )
            logger.info("Stage $default creado")

        # 5. Forzar un deployment explícito para que la ruta quede activa inmediatamente
        try:
            apigw_client.create_deployment(
                ApiId=api_id,
                StageName='$default',
            )
            logger.info("Deployment explícito del API Gateway creado")
        except Exception as e:
            logger.warning(f"No se pudo crear deployment explícito (puede ser normal si AutoDeploy ya lo hizo): {e}")

        logger.info(f"Lambda desplegada y conectada a API Gateway. URL: {api_url}")

        return {
            'function_arn': function_arn,
            'api_id': api_id,
            'api_url': api_url,
        }

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
            # Verificar / crear IGW y ruta de internet en la VPC
            igws = ec2.describe_internet_gateways(
                Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}]
            )['InternetGateways']
            if igws:
                igw_id = igws[0]['InternetGatewayId']
            else:
                igw = ec2.create_internet_gateway()
                igw_id = igw['InternetGateway']['InternetGatewayId']
                ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

            # Asegurar que la route table principal tiene ruta 0.0.0.0/0 → IGW
            rts = ec2.describe_route_tables(
                Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]},
                         {'Name': 'association.main', 'Values': ['true']}]
            )['RouteTables']
            main_rt_id = None
            if rts:
                main_rt_id = rts[0]['RouteTableId']
                existing_routes = [r.get('DestinationCidrBlock') for r in rts[0].get('Routes', [])]
                if '0.0.0.0/0' not in existing_routes:
                    try:
                        ec2.create_route(RouteTableId=main_rt_id, DestinationCidrBlock='0.0.0.0/0', GatewayId=igw_id)
                        logger.info("Ruta 0.0.0.0/0 → IGW creada en route table %s (%s)", main_rt_id, region)
                    except Exception as rt_err:
                        logger.warning("No se pudo crear ruta en route table %s: %s", main_rt_id, rt_err)

            # 2. Buscar subnets públicas asociadas a esa VPC, en AZs distintas
            subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['Subnets']

            def pick_two_different_az(subnet_list):
                seen_azs = {}
                for s in subnet_list:
                    az = s['AvailabilityZone']
                    if az not in seen_azs:
                        seen_azs[az] = s
                    if len(seen_azs) >= 2:
                        break
                return list(seen_azs.values())

            public_subnets = pick_two_different_az([s for s in subnets if s.get('MapPublicIpOnLaunch')])
            if len(public_subnets) < 2:
                public_subnets = pick_two_different_az(subnets)

            if len(public_subnets) < 2:
                # La región no tiene 2 subnets disponibles, crear las que falten en distintas AZs
                import ipaddress
                azs = ec2.describe_availability_zones()['AvailabilityZones']
                existing_cidrs = {s['CidrBlock'] for s in subnets}

                # Obtener el CIDR de la VPC para generar subnets compatibles
                vpc_info = ec2.describe_vpcs(VpcIds=[vpc_id])['Vpcs'][0]
                vpc_cidr = vpc_info['CidrBlock']
                vpc_network = ipaddress.IPv4Network(vpc_cidr, strict=False)

                # Generar candidatos /24 dentro del CIDR de la VPC
                candidate_cidrs = []
                for subnet_net in vpc_network.subnets(new_prefix=24):
                    candidate_cidrs.append(str(subnet_net))
                    if len(candidate_cidrs) >= 20:
                        break

                new_subnets = list(public_subnets)
                used_azs = {s['AvailabilityZone'] for s in new_subnets}
                az_index = 0
                for cidr in candidate_cidrs:
                    if len(new_subnets) >= 2:
                        break
                    if cidr not in existing_cidrs:
                        # Elegir una AZ que no esté ya usada
                        available_azs = [a['ZoneName'] for a in azs if a['ZoneName'] not in used_azs]
                        if not available_azs:
                            available_azs = [azs[az_index % len(azs)]['ZoneName']]
                        az = available_azs[0]
                        try:
                            s = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az)
                            sid = s['Subnet']['SubnetId']
                            ec2.modify_subnet_attribute(SubnetId=sid, MapPublicIpOnLaunch={'Value': True})
                            # Asociar a la route table principal (que ya tiene ruta al IGW)
                            if rts:
                                try:
                                    ec2.associate_route_table(RouteTableId=main_rt_id, SubnetId=sid)
                                except Exception:
                                    pass
                            new_subnets.append(s['Subnet'])
                            used_azs.add(az)
                            az_index += 1
                        except Exception as subnet_err:
                            logger.warning("No se pudo crear subnet %s: %s", cidr, subnet_err)
                            continue
                public_subnets = new_subnets

            subnet1_id = public_subnets[0]['SubnetId']
            subnet2_id = public_subnets[1]['SubnetId']

            # Garantizar que ambas subnets tienen ruta a internet.
            # Una subnet puede tener MapPublicIpOnLaunch=True pero estar en una
            # route table sin ruta 0.0.0.0/0 → el task no puede llegar a CloudWatch.
            if main_rt_id:
                for sid in [subnet1_id, subnet2_id]:
                    # Ver qué route table usa esta subnet (explícita o main)
                    rt_resp = ec2.describe_route_tables(
                        Filters=[{'Name': 'association.subnet-id', 'Values': [sid]}]
                    )['RouteTables']
                    if rt_resp:
                        # Tiene una RT explícita — verificar si tiene ruta a internet
                        subnet_routes = [r.get('DestinationCidrBlock') for r in rt_resp[0].get('Routes', [])]
                        if '0.0.0.0/0' not in subnet_routes:
                            # No tiene ruta → reasociar a la main RT que sí la tiene
                            assoc_id = rt_resp[0]['Associations'][0].get('RouteTableAssociationId')
                            if assoc_id:
                                try:
                                    ec2.disassociate_route_table(AssociationId=assoc_id)
                                except Exception:
                                    pass
                            try:
                                ec2.associate_route_table(RouteTableId=main_rt_id, SubnetId=sid)
                                logger.info("Subnet %s reasociada a main RT %s para acceso a internet (%s)", sid, main_rt_id, region)
                            except Exception as assoc_err:
                                logger.warning("No se pudo reasociar subnet %s: %s", sid, assoc_err)
                    else:
                        # Usa la main RT implícitamente — ya tiene la ruta, no hacer nada
                        pass

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
            logger.debug("Bucket %s ya existe.", bucket_name)
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
        logger.debug("Bucket %s configurado para hosting web estático.", bucket_name)

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
                        ACL='public-read'
                    )
                logger.debug("Archivo subido: %s", key)

        logger.info("Proyecto desplegado en S3: http://%s.s3-website-%s.amazonaws.com", bucket_name, self.s3_services.meta.region_name)

        # # 4. Si se especifica dominio y datos de contacto, configurar dominio + SSL
        # if domain_name and contact_info:
        #     print("Configurando dominio y SSL...")
        #     self.gestionar_dominio(domain_name, contact_info)
        #     print(f"Dominio {domain_name} configurado con SSL (validación pendiente).")

    def create_lambda_deployment(self, deployment: Deployment, lambda_config) -> None:
        """Despliega la funcion Lambda + API Gateway y guarda la URL en el deployment."""
        try:
            deployment.status = 'creating'
            deployment.save()

            # Normalizar a dict plano para evitar problemas con OrderedDict del serializer
            if hasattr(lambda_config, 'items'):
                config = dict(lambda_config)
                # Normalizar codeFiles a lista de dicts planos
                raw_files = config.get('codeFiles') or config.get('code_files') or []
                config['codeFiles'] = [dict(f) for f in raw_files]
                lambda_config = config

            logger.info(f"create_lambda_deployment: config keys={list(lambda_config.keys()) if lambda_config else []}")
            logger.info(f"codeFiles count: {len(lambda_config.get('codeFiles', []) if lambda_config else [])}")

            region = deployment.aws_region or 'us-east-1'
            aws_service = AWSService(region_name=region)
            result = aws_service.deploy_lambda(deployment, lambda_config)

            # Guardar ARN de la funcion en aws_task_arn y el api_id en aws_service_arn
            deployment.aws_task_arn = f"apigw:{result['api_id']}"
            deployment.aws_service_arn = result['function_arn']
            deployment.deployment_url = result['api_url']
            deployment.status = 'running'
            # Persist the deployed lambda config (including codeFiles) so the editor
            # can reload the exact last-deployed version
            deployment.lambda_config = {
                'runtime':        lambda_config.get('runtime', 'nodejs18.x'),
                'handler':        lambda_config.get('handler', 'index.handler'),
                'timeout':        min(int(lambda_config.get('timeout', 30)), 900),
                'memory':         int(lambda_config.get('memory', 128)),
                'environmentVars': lambda_config.get('environmentVars', '') or '',
                'trigger':        lambda_config.get('trigger', 'api-gateway'),
                'httpMethod':     (lambda_config.get('http_method') or lambda_config.get('httpMethod') or 'POST').upper(),
                'deadLetterQueue': bool(lambda_config.get('deadLetterQueue', False)),
                'codeFiles':      [dict(f) for f in (lambda_config.get('codeFiles') or [])],
            }
            deployment.save()
            logger.info(f"Lambda deployment completado. URL: {result['api_url']}")

        except Exception as e:
            deployment.status = 'failed'
            deployment.save()
            logger.error(f"Error en create_lambda_deployment: {str(e)}")
            raise

    def check_status(self):
        logger.debug("Checking deployment status...")
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
            backend_env = next((e for e in environment_variables if e.get("name")), None)
            images_backend = backend_env.get("name") if backend_env else ""
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
            image_name = image.get("name", "").upper().split(':')[0].split('/')[-1]
            if any(engine in image_name for engine in database_engines):
                self.database_engine = image_name
                break

        # Extracción mejorada de variables de entorno para diferentes motores
        for env in environment_variables:
            env_name = env.get("name", "")
            env_value = env.get("value", "")

            # Postgres support
            if env_name == "POSTGRES_DB":
                database_name = env_value
            elif env_name == "POSTGRES_USER":
                database_user = env_value
            elif env_name == "POSTGRES_PASSWORD":
                database_password = env_value
            elif env_name == "POSTGRES_HOST":
                database_host = env_value
            elif env_name == "POSTGRES_PORT":
                database_port = env_value

            # MySQL support
            elif env_name == "MYSQL_DATABASE":
                database_name = env_value
            elif env_name == "MYSQL_USER":
                database_user = env_value
            elif env_name == "MYSQL_PASSWORD":
                database_password = env_value
            elif env_name == "MYSQL_ROOT_PASSWORD":
                database_root_password = env_value
            elif env_name == "MYSQL_HOST":
                database_host = env_value
            elif env_name == "MYSQL_PORT":
                database_port = env_value

        # Si no se detectó host o se puso "db" (común en Docker Compose), 
        # por defecto es localhost para Fargate (comunicación intra-task)
        if not database_host or database_host.lower() == "db":
            database_host = "localhost"
            
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
                {
                    'ParameterKey': 'HealthCheckPath',
                    'ParameterValue': str(ecs_config.get('healthCheckPath', '/'))
                },
                {
                    'ParameterKey': 'HealthCheckInterval',
                    'ParameterValue': str(ecs_config.get('healthCheckInterval', 30))
                },
                {
                    'ParameterKey': 'HealthCheckTimeout',
                    'ParameterValue': str(ecs_config.get('healthCheckTimeout', 5))
                },
                {
                    'ParameterKey': 'HealthCheckRetries',
                    'ParameterValue': str(ecs_config.get('healthCheckRetries', 3))
                },
            ]

            parameters_template.append({
                'ParameterKey': 'LoadBalancerEnabled',
                'ParameterValue': 'true' if ecs_config.get('loadBalancer', False) else 'false'
            })
            parameters_template.append({
                'ParameterKey': 'AutoScalingEnabled',
                'ParameterValue': 'true' if ecs_config.get('autoScaling', False) else 'false'
            })

            # Multi-region deployment
            regions = ecs_config.get('regions', ['us-east-1'])
            cluster_arns = []
            for idx, region in enumerate(regions):
                if idx == 0 and ecs_config.get('vpc_id') and ecs_config.get('subnet1') and ecs_config.get('subnet2'):
                    vpc_id = ecs_config['vpc_id']
                    subnet1 = ecs_config['subnet1']
                    subnet2 = ecs_config['subnet2']
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

                # Pre-crear el log group en esta región antes de lanzar el stack.
                # awslogs-create-group falla si no hay conectividad en runtime;
                # crearlo aquí desde el backend evita ese race condition.
                log_group_name = f"/ecs/{deployment.aws_cluster_arn}"
                try:
                    logs_client = boto3.client(
                        'logs',
                        region_name=region,
                        aws_access_key_id=getattr(settings, 'AWS_ACCESS_KEY_ID', None),
                        aws_secret_access_key=getattr(settings, 'AWS_SECRET_ACCESS_KEY', None),
                    )
                    logs_client.create_log_group(logGroupName=log_group_name)
                    logger.info("Log group '%s' creado en %s", log_group_name, region)
                except logs_client.exceptions.ResourceAlreadyExistsException:
                    logger.debug("Log group '%s' ya existe en %s", log_group_name, region)
                except Exception as e:
                    logger.warning("No se pudo pre-crear log group en %s: %s", region, e)

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
        """
        Actualiza el desiredCount en todos los ECS services del deployment.
        Para deployments multi-región itera cada stack en aws_cluster_arn.
        Para Lambda no hace nada (no hay concept de desired_count).
        """
        import json as _json

        if deployment.service == 'lambda':
            return

        desired = deployment.desired_count or 0

        # Intentar parsear las entradas multi-región
        try:
            cluster_arns = _json.loads(deployment.aws_cluster_arn or '[]')
        except Exception:
            cluster_arns = []

        if cluster_arns and isinstance(cluster_arns, list) and isinstance(cluster_arns[0], dict):
            # Multi-región: iterar cada stack
            errors = []
            for item in cluster_arns:
                region = item.get('region', deployment.aws_region or 'us-east-1')
                stack_name = item.get('stack_name')
                if not stack_name:
                    continue
                try:
                    aws_svc = AWSService(region)
                    service_arn = aws_svc.get_service_arn_from_stack(stack_name)
                    if not service_arn:
                        logger.warning("No se encontró ECS service en stack %s", stack_name)
                        continue
                    # Obtener cluster ARN del stack
                    cluster_arn = aws_svc.get_cluster_arn_from_stack(stack_name)
                    aws_svc.ecs_client.update_service(
                        cluster=cluster_arn or stack_name,
                        service=service_arn,
                        desiredCount=desired,
                    )
                    logger.info("desiredCount=%s aplicado en %s (%s)", desired, stack_name, region)
                except Exception as e:
                    logger.error("Error actualizando %s en %s: %s", stack_name, region, e)
                    errors.append(str(e))
            if errors:
                raise Exception("; ".join(errors))
        else:
            # Deployment single-región legacy
            try:
                self.aws_service.ecs_client.update_service(
                    cluster=deployment.aws_cluster_arn,
                    service=deployment.aws_service_arn,
                    desiredCount=desired,
                )
            except Exception as e:
                raise
    

    # Elimina un deployment existente
    def delete_deployment(self, deployment: Deployment) -> None:
        """Elimina un deployment"""
        try:
            deployment.status = 'deleting'
            deployment.save()

            # Eliminar recursos según el tipo de servicio
            if deployment.service == 'lambda':
                self.aws_service.delete_lambda(deployment)
            else:
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
            logger.info("Disponibilidad del dominio '%s': %s", domain_name, status)
        except ClientError as e:
            logger.error("Error al verificar disponibilidad de dominio: %s", e)
            return

        # 2. Registrar dominio si está disponible
        if status == 'AVAILABLE':
            try:
                logger.info("Dominio disponible. Registrando...")
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
                logger.info("Dominio registrado. Puede tardar unos minutos en estar activo.")
            except ClientError as e:
                logger.error("Error al registrar dominio: %s", e)
                return
        else:
            logger.info("Dominio ya registrado. Se creará zona hospedada.")

        # 3. Crear zona hospedada pública en Route 53
        try:
            response = self.route53.create_hosted_zone(
                Name=domain_name,
                CallerReference=str(time.time()),
                HostedZoneConfig={'Comment': 'Zona hospedada automática', 'PrivateZone': False}
            )
            hosted_zone_id = response['HostedZone']['Id']
            ns = response['DelegationSet']['NameServers']
            logger.info("Zona hospedada creada. NameServers: %s", ns)
        except ClientError as e:
            logger.error("Error al crear zona hospedada: %s", e)
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
            logger.info("Certificado solicitado con ARN: %s", cert_arn)

            # Esperar a que ACM genere las opciones de validación DNS
            time.sleep(5)
            cert_detail = acm.describe_certificate(CertificateArn=cert_arn)
            validation_options = cert_detail['Certificate']['DomainValidationOptions']

            # Crear registros DNS para validación en Route 53
            changes = []
            for option in validation_options:
                if 'ResourceRecord' in option:
                    rr = option['ResourceRecord']
                    logger.debug("Registro DNS SSL — Nombre: %s | Tipo: %s | Valor: %s", rr['Name'], rr['Type'], rr['Value'])
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
                logger.info("Registros DNS de validación creados en Route 53.")
            else:
                logger.warning("No se encontraron registros DNS para crear.")

            logger.info("El certificado SSL se activará una vez se valide el dominio.")

        except ClientError as e:
            logger.error("Error al solicitar certificado SSL: %s", e)
            return

        logger.info("Dominio '%s' configurado con SSL pendiente de validación.", domain_name)
    
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