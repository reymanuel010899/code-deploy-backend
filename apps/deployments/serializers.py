from rest_framework import serializers
from .models import DockerImage, Deployment
from botocore.exceptions import ClientError # Importante añadir esta importación
import uuid
from .services import AWSService
class DockerImageSerializer(serializers.ModelSerializer):
    full_image_name = serializers.CharField(read_only=True)
    
    class Meta:
        model = DockerImage
        fields = ['id', 'name', 'tag', 'registry_url', 'repository_name', 
                 'full_image_name', 'created_at']
        read_only_fields = ['created_at']



class ContainerImageSerializer(serializers.Serializer):
    name = serializers.CharField()
    tag = serializers.CharField(required=False)

class checkDomainSerializer(serializers.Serializer):
    domain = serializers.CharField()
    tld = serializers.CharField(required=False, default='com')


class ECSConfigSerializer(serializers.Serializer):
    regions = serializers.ListField(child=serializers.CharField(), required=False)
    clusterName = serializers.CharField()
    IsRepoPrivate = serializers.BooleanField(default=False)
    privateRegistryCredentials = serializers.DictField(required=False)
    taskCpu = serializers.IntegerField()
    taskMemory = serializers.IntegerField()
    desiredCount = serializers.IntegerField()
    loadBalancer = serializers.BooleanField()
    autoScaling = serializers.BooleanField()
    minCapacity = serializers.IntegerField()
    maxCapacity = serializers.IntegerField()
    networkMode = serializers.CharField()
    platformVersion = serializers.CharField()
    assignPublicIp = serializers.BooleanField()
    subnets = serializers.ListField(child=serializers.CharField())
    securityGroups = serializers.ListField(child=serializers.CharField())
    containerPort = serializers.IntegerField()
    protocol = serializers.CharField()
    essential = serializers.BooleanField()
    logGroup = serializers.CharField()
    logRegion = serializers.CharField()
    logStreamPrefix = serializers.CharField()
    environmentVariables = serializers.ListField(child=serializers.DictField(), required=False)
    secrets = serializers.ListField(child=serializers.DictField(), required=False)
    healthCheckEnabled = serializers.BooleanField()
    healthCheckPath = serializers.CharField()
    healthCheckInterval = serializers.IntegerField()
    healthCheckTimeout = serializers.IntegerField()
    healthCheckRetries = serializers.IntegerField()


class DeploymentCreateSerializer(serializers.Serializer):
    service = serializers.CharField()
    docker_images = serializers.ListField(child=serializers.DictField())
    ecs_config = ECSConfigSerializer()

    def create(self, validated_data):
        ecs_data = validated_data.pop('ecs_config')
        docker_images = validated_data.pop('docker_images')
        user = self.context['request'].user
        service = 'ec2' if validated_data.get('service') == 'ecs' else validated_data.get('service')
        deployment = Deployment.objects.create(
            user=user,
            aws_cluster_arn=f"{ecs_data.get('clusterName')}_{str(uuid.uuid4())[:8]}",
            name=ecs_data.get('clusterName') + "-deploy",
            cpu_units=ecs_data.get('taskCpu'),
            memory_mb=ecs_data.get('taskMemory'),
            docker_images=docker_images,
            service=service,
            auto_scaling_enabled= ecs_data.get('autoScaling', False),
            load_balancer = ecs_data.get('loadBalancer', False),
            desired_count=ecs_data.get('desiredCount', 1),
            min_count=ecs_data.get('minCapacity', 1),
            max_count=ecs_data.get('maxCapacity', 1),
            network_mode=ecs_data.get('networkMode', 'awsvpc'),
            port_mappings=[
                {
                    'containerPort': ecs_data.get('containerPort', 80),
                    'hostPort': ecs_data.get('containerPort', 80),
                    'protocol': ecs_data.get('protocol', 'tcp')
                }
            ],
            environment_variables=ecs_data.get('environmentVariables', []),
            secrets=ecs_data.get('secrets', []),
        )
        deployment.save()
        print("Deployment created with ID:", deployment.auto_scaling_enabled)
        return deployment


class DeploymentUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deployment
        fields = [
            'cpu_units', 'memory_mb', 'command', 'entrypoint',
            'port_mappings', 'environment_variables', 'secrets',
            'volumes', 'health_check', 'desired_count', 'min_count',
            'max_count'
        ]

class DeploymentDetailSerializer(serializers.ModelSerializer):
    # docker_image = DockerImageSerializer(read_only=True)
    # status_details = serializers.SerializerMethodField()
    
    class Meta:
        model = Deployment
        fields = [
            'id', 'name', 'regions', 'docker_images', 'status',
            'created_at', 'updated_at', 'cpu_units', 'memory_mb',
             'port_mappings',
            'network_mode', 'environment_variables', 'secrets',
              'desired_count', 'min_count',
            'max_count'
        ]
        read_only_fields = ['created_at', 'updated_at', 'status']
    
    def get_status_details(self, obj):
        from .services import DeploymentService
        try:
            deployment_service = DeploymentService()
            return deployment_service.get_deployment_status(obj)
        except Exception:
            return None

class DeploymentListSerializer(serializers.ModelSerializer):
    deploymet_url =  serializers.SerializerMethodField()
    class Meta:
        model = Deployment
        fields = [
            'id', 'name',  'regions','status','service',
            'created_at', 'updated_at', 'cpu_units', 'memory_mb',
            'deploymet_url'
        ]
        read_only_fields = ['created_at', 'updated_at', 'status']
        
    def get_deploymet_url(self, obj):
        aws_Services = AWSService()
        stack_name = f"{obj.name}-{obj.aws_region}-stack"

        try:
            # Intentamos obtener la IP
            public_ip = aws_Services.get_first_task_public_ip(stack_name=stack_name)
            print(f"Public IP obtenida para {stack_name}: {public_ip}")
            # Validamos si la respuesta es un error manejado por tu service
            if isinstance(public_ip, dict) and "error" in public_ip:
                return obj.deployment_url or ""
            if public_ip and obj.status != "running":
                obj.status = "running"
                obj.save()

            # Si todo bien, actualizamos la URL en BD
            if public_ip:
                obj.deployment_url = f"http://{public_ip}"
                obj.save()
                return obj.deployment_url
                
        except ClientError as e:
            # Si AWS dice que el stack no existe, capturamos el error aquí
            print(f"Error en AWS para {stack_name}: {e}")
            return obj.deployment_url or ""
        except Exception as e:
            # Cualquier otro error inesperado
            print(f"Error inesperado: {e}")
            return obj.deployment_url or ""
            
        return obj.deployment_url or ""

    def get_status_details(self, obj):
        from .services import DeploymentService
        try:
            deployment_service = DeploymentService()
            return deployment_service.get_deployment_status(obj)
        except Exception:
            return None 