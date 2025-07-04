import boto3
import os
import logging
from typing import Dict, List, Optional
from django.conf import settings
from .models import Deployment, DockerImage


dir_path = os.path.dirname(os.path.realpath(__file__))
logger = logging.getLogger(__name__)


class AWSService:
    def __init__(self, region_name: str = 'us-east-1'):
        self.region_name = region_name
        self.ecs_client = boto3.client('ecs', region_name=region_name)
        self.ecr_client = boto3.client('ecr', region_name=region_name)
        self.stack_formations = boto3.client('cloudformation', region_name=region_name)
        self.logs_client = boto3.client('logs', region_name=region_name)
        self.autoscaling_client = boto3.client('application-autoscaling', region_name=region_name)
        self.secrets_manayer_client = boto3.client('secretsmanager', region_name=region_name)

    def create_stack(self, deployment: Deployment,  parameters: List[Dict[str, str]]) -> str:
        print("Creating CloudFormation stack for deployment:", parameters)
        with open(os.path.join(dir_path, 'ecs-fargate.yml')) as f:
            template_body = f.read()

        try:
            response = self.stack_formations.create_stack(
                StackName=f"{deployment.name}-stack",
                TemplateBody=template_body,
                Parameters=parameters,
                Capabilities=['CAPABILITY_NAMED_IAM'],
                OnFailure='ROLLBACK'
            )
            return response['StackId']
        except Exception as e:
            logger.error(f"Error creating stack: {str(e)}")
            try:
                self.stack_formations.delete_stack(StackName=f"{deployment.name}-stack")
                logger.info(f"Stack {deployment.name}-stack eliminado tras error en la creación.")
            except Exception as delete_exc:
                logger.error(f"Error eliminando stack tras fallo en la creación: {str(delete_exc)}")
                raise
            raise

    def get_secret_value(self, secret_name: str) -> Optional[str]:
        try:
            response = self.secrets_manayer_client.get_secret_value(SecretId=secret_name)
            if 'SecretString' in response:
                return response['SecretString']
            else:
                return None
        except Exception as e:
            logger.error(f"Error getting secret value: {str(e)}")
            raise

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

    def delete_service(self, deployment: Deployment) -> None:
        """Elimina un servicio ECS"""
        try:
            self.ecs_client.update_service(
                cluster=deployment.aws_cluster_arn,
                service=deployment.aws_service_arn,
                desiredCount=0
            )
            self.ecs_client.delete_service(
                cluster=deployment.aws_cluster_arn,
                service=deployment.aws_service_arn
            )
        except Exception as e:
            logger.error(f"Error deleting service: {str(e)}")
            raise

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

    def create_deployment(self, deployment: Deployment, docker_images: list, environment_variables: list, ecs_config: dict) -> None:
        print(environment_variables, "***********")
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
            print(env)
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

            print("Database variables:")
            print("database_name:", database_name)
            print("database_user:", database_user)
            print("database_password:", database_password)
            print("database_host:", database_host)
            print("database_port:", database_port)
            print("database_root_password:", database_root_password)
            print("database_engine:", self.database_engine)
            parameters = [
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
                    'ParameterValue': docker_images[2].get('name') if len(docker_images) > 2 else None
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
                
            ]

            # --- NUEVO: Agregar parámetros para Load Balancer y Auto Scaling si corresponde ---
            if ecs_config.get('loadBalancer', False):
                parameters.append({
                    'ParameterKey': 'LoadBalancerEnabled',
                    'ParameterValue': 'true'
                })
            else:
                parameters.append({
                    'ParameterKey': 'LoadBalancerEnabled',
                    'ParameterValue': 'false'
                })

            if ecs_config.get('autoScaling', False):
                parameters.append({
                    'ParameterKey': 'AutoScalingEnabled',
                    'ParameterValue': 'true'
                })
                if ecs_config.get("min_count") is not None:
                    parameters.append({
                        'ParameterKey': 'MinCapacity',
                        'ParameterValue': str(min_capacity)
                    })
                if ecs_config.get("max_count") is not None:
                    parameters.append({
                        'ParameterKey': 'MaxCapacity',
                        'ParameterValue': str(max_capacity)
                    })
            else:
                parameters.append({
                    'ParameterKey': 'AutoScalingEnabled',
                    'ParameterValue': 'false'
                })


            cluster_arn = self.aws_service.create_stack(deployment, parameters)
            deployment.aws_cluster_arn = cluster_arn
            deployment.save()

        except Exception as e:
            deployment.status = 'failed'
            deployment.save()
            raise

    def update_deployment(self, deployment: Deployment) -> None:
        """Actualiza un deployment existente"""
        try:
    
            task_definition_arn = self.aws_service.create_task_definition(deployment)
            
            # Actualizar servicio
            self.aws_service.update_service(deployment, task_definition_arn)

            DeploymentLog.objects.create(
                deployment=deployment,
                message="Deployment actualizado exitosamente",
                log_type='success',
                source='system'
            )

        except Exception as e:
            DeploymentLog.objects.create(
                deployment=deployment,
                message=f"Error al actualizar deployment: {str(e)}",
                log_type='error',
                source='system'
            )
            raise

    def delete_deployment(self, deployment: Deployment) -> None:
        """Elimina un deployment"""
        try:
            DeploymentLog.objects.create(
                deployment=deployment,
                message="Iniciando eliminación del deployment",
                log_type='info',
                source='system'
            )

            deployment.status = 'deleting'
            deployment.save()

            # Eliminar servicio
            self.aws_service.delete_service(deployment)

            # Eliminar deployment de la base de datos
            deployment.delete()

        except Exception as e:
            deployment.status = 'failed'
            deployment.save()
            DeploymentLog.objects.create(
                deployment=deployment,
                message=f"Error al eliminar deployment: {str(e)}",
                log_type='error',
                source='system'
            )
            raise

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

class DockerImageService:
    def __init__(self):
        self.aws_service = AWSService()

    def validate_image(self, docker_image: DockerImage) -> bool:
        """Valida que la imagen Docker exista y sea accesible"""
        try:
            # Para imágenes en ECR
            if 'amazonaws.com' in docker_image.registry_url:
                self.aws_service.ecr_client.describe_images(
                    repositoryName=docker_image.repository_name,
                    imageIds=[{'imageTag': docker_image.tag}]
                )
            # Para imágenes en Docker Hub
            elif 'docker.io' in docker_image.registry_url:
                # Aquí podrías implementar la validación para Docker Hub
                # usando la API de Docker Hub o docker-py
                pass
            return True
        except Exception as e:
            logger.error(f"Error validating image: {str(e)}")
            return False

    def get_image_details(self, docker_image: DockerImage) -> Dict:
        """Obtiene detalles de la imagen Docker"""
        try:
            if 'amazonaws.com' in docker_image.registry_url:
                response = self.aws_service.ecr_client.describe_images(
                    repositoryName=docker_image.repository_name,
                    imageIds=[{'imageTag': docker_image.tag}]
                )
                image = response['imageDetails'][0]
                return {
                    'size': image.get('imageSizeInBytes'),
                    'pushed_at': image.get('imagePushedAt'),
                    'digest': image.get('imageDigest'),
                    'tags': image.get('imageTags', [])
                }
            return {}
        except Exception as e:
            logger.error(f"Error getting image details: {str(e)}")
            raise 