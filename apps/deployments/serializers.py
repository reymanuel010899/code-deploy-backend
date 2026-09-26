from rest_framework import serializers
from .models import DockerImage, Deployment
import uuid
import logging
from .services import AWSService

logger = logging.getLogger(__name__)
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


class SchedulerConfigSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False, default=False)
    start_cron = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    stop_cron = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    start_desired_count = serializers.IntegerField(required=False, min_value=1)


class CodeFileSerializer(serializers.Serializer):
    name = serializers.CharField()
    content = serializers.CharField(allow_blank=True)
    language = serializers.CharField(required=False, default='javascript')
    isMain = serializers.BooleanField(required=False, default=False)


class LambdaConfigSerializer(serializers.Serializer):
    runtime = serializers.CharField(default='nodejs18.x')
    handler = serializers.CharField(default='index.handler')
    timeout = serializers.IntegerField(default=30)
    memory = serializers.IntegerField(default=128)
    environmentVars = serializers.CharField(allow_blank=True, required=False, default='')
    trigger = serializers.CharField(default='api-gateway')
    deadLetterQueue = serializers.BooleanField(default=False)
    codeFiles = CodeFileSerializer(many=True, required=False, default=list)


class ECSConfigSerializer(serializers.Serializer):
    regions = serializers.ListField(child=serializers.CharField(), required=False)
    clusterName = serializers.CharField()
    domain_name = serializers.CharField(required=False, allow_blank=True, allow_null=True)
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
    scheduler = SchedulerConfigSerializer(required=False)

class DeploymentCreateSerializer(serializers.Serializer):
    service = serializers.CharField()
    regions = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    # ── Formato plano que envía page.tsx para Lambda ──────────────────────
    name = serializers.CharField(required=False, allow_blank=True)
    runtime = serializers.CharField(required=False)
    handler = serializers.CharField(required=False)
    timeout = serializers.IntegerField(required=False)
    memory_mb = serializers.IntegerField(required=False)
    environment_vars = serializers.CharField(required=False, allow_blank=True)
    trigger = serializers.CharField(required=False)
    dead_letter_queue = serializers.BooleanField(required=False, default=False)
    domain_name = serializers.CharField(required=False, allow_blank=True)
    code_files = serializers.ListField(child=serializers.DictField(), required=False, default=list)

    # ── Formato anidado camelCase (useDeployment.ts) ──────────────────────
    dockerImages = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    lambdaConfig = LambdaConfigSerializer(required=False)
    ecsConfig = ECSConfigSerializer(required=False)

    # ── Formato anidado snake_case ────────────────────────────────────────
    docker_images = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    lambda_config = LambdaConfigSerializer(required=False)
    ecs_config = ECSConfigSerializer(required=False)

    def create(self, validated_data):
        user = self.context['request'].user
        service = validated_data.get('service')
        regions = validated_data.get('regions') or ['us-east-1']

        if service == 'lambda':
            # Construir lambda_config unificado desde cualquier formato
            lambda_config = self._resolve_lambda_config(validated_data)
            region = regions[0] if regions else 'us-east-1'
            deployment_name = validated_data.get('name') or f"zaplet-{user.username}-{str(uuid.uuid4())[:8]}"
            deployment = Deployment.objects.create(
                user=user,
                name=deployment_name,
                service='lambda',
                aws_region=region,
                regions=regions,
                cpu_units=256,
                memory_mb=lambda_config.get('memory', 128),
                environment_variables={},
                status='pending',
            )
            deployment.save()
            # Guardar el config resuelto para que la view lo use
            deployment._lambda_config = lambda_config
            return deployment

        # ECS / EC2
        docker_images = validated_data.get('dockerImages') or validated_data.get('docker_images', [])
        ecs_data = validated_data.get('ecsConfig') or validated_data.get('ecs_config') or {}
        service_type = 'ec2' if service == 'ecs' else service
        deployment = Deployment.objects.create(
            user=user,
            aws_cluster_arn=f"{ecs_data.get('clusterName')}_{str(uuid.uuid4())[:8]}",
            name=ecs_data.get('clusterName') + "-deploy",
            cpu_units=ecs_data.get('taskCpu'),
            memory_mb=ecs_data.get('taskMemory'),
            docker_images=docker_images,
            service=service_type,
            auto_scaling_enabled=ecs_data.get('autoScaling', False),
            load_balancer=ecs_data.get('loadBalancer', False),
            desired_count=ecs_data.get('desiredCount', 1),
            min_count=ecs_data.get('minCapacity', 1),
            max_count=ecs_data.get('maxCapacity', 1),
            network_mode=ecs_data.get('networkMode', 'awsvpc'),
            port_mappings=[{
                'containerPort': ecs_data.get('containerPort', 80),
                'hostPort': ecs_data.get('containerPort', 80),
                'protocol': ecs_data.get('protocol', 'tcp')
            }],
            environment_variables=ecs_data.get('environmentVariables', []),
            secrets=ecs_data.get('secrets', []),
        )
        deployment.save()
        return deployment

    def _resolve_lambda_config(self, data):
        """
        Construye un dict unificado con la config Lambda
        sin importar si llegó en formato plano (page.tsx) o anidado (useDeployment.ts).
        """
        # Intentar formato anidado primero
        nested = data.get('lambdaConfig') or data.get('lambda_config')
        if nested:
            cfg = dict(nested)
            raw = cfg.get('codeFiles') or cfg.get('code_files') or []
            cfg['codeFiles'] = [dict(f) for f in raw]
            return cfg

        # Formato plano de page.tsx
        raw_files = data.get('code_files', [])
        return {
            'runtime':        data.get('runtime', 'nodejs18.x'),
            'handler':        data.get('handler', 'index.handler'),
            'timeout':        int(data.get('timeout', 30)),
            'memory':         int(data.get('memory_mb', 128)),
            'environmentVars': data.get('environment_vars', '') or '',
            'trigger':        data.get('trigger', 'api-gateway'),
            'deadLetterQueue': bool(data.get('dead_letter_queue', False)),
            'codeFiles':      [dict(f) for f in raw_files],
        }


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
    region_urls = serializers.SerializerMethodField()

    class Meta:
        model = Deployment
        fields = [
            'id', 'name', 'regions', 'docker_images', 'status', 'service',
            'created_at', 'updated_at', 'cpu_units', 'memory_mb',
            'port_mappings', 'network_mode', 'environment_variables', 'secrets',
            'desired_count', 'min_count', 'max_count',
            'aws_cluster_arn', 'aws_service_arn', 'aws_region',
            'deployment_url', 'region_urls', 'lambda_config',
        ]
        read_only_fields = ['created_at', 'updated_at', 'status']

    def get_region_urls(self, obj):
        import json as _json
        # Return persisted region_urls if available; otherwise try to fetch from AWS
        if obj.region_urls:
            return obj.region_urls
        try:
            cluster_arns = _json.loads(obj.aws_cluster_arn or "[]")
        except Exception:
            cluster_arns = []
        if not cluster_arns:
            url = obj.deployment_url or ""
            if url:
                return [{'region': obj.aws_region, 'url': url, 'status': 'running'}]
            return []
        results = []
        any_running = False
        for item in cluster_arns:
            region = item.get('region', obj.aws_region)
            stack_name = item.get('stack_name', f"{obj.name}-{region}-stack")
            try:
                aws_svc = AWSService(region)
                public_ip = aws_svc.get_first_task_public_ip(stack_name=stack_name)
                if isinstance(public_ip, dict):
                    results.append({'region': region, 'url': '', 'status': 'pending'})
                else:
                    url = f"http://{public_ip}"
                    results.append({'region': region, 'url': url, 'status': 'running'})
                    any_running = True
            except Exception as e:
                logger.warning("No se pudo obtener IP de %s (%s): %s", stack_name, region, e)
                results.append({'region': region, 'url': '', 'status': 'error'})
        if results:
            obj.region_urls = results
            if any_running:
                obj.deployment_url = results[0]['url'] if results[0]['url'] else (obj.deployment_url or "")
                if obj.status != 'running':
                    obj.status = 'running'
            obj.save(update_fields=['region_urls', 'deployment_url', 'status'])
        return results
    
    def get_status_details(self, obj):
        from .services import DeploymentService
        try:
            deployment_service = DeploymentService()
            return deployment_service.get_deployment_status(obj)
        except Exception:
            return None

class DeploymentListSerializer(serializers.ModelSerializer):
    deploymet_url = serializers.SerializerMethodField()
    region_urls = serializers.SerializerMethodField()

    class Meta:
        model = Deployment
        fields = [
            'id', 'name', 'regions', 'status', 'service',
            'created_at', 'updated_at', 'cpu_units', 'memory_mb',
            'deploymet_url', 'region_urls',
            # campos necesarios para recargar la config al editar
            'docker_images', 'load_balancer', 'auto_scaling_enabled',
            'desired_count', 'min_count', 'max_count', 'network_mode',
            'aws_cluster_arn', 'aws_service_arn', 'aws_region',
            'port_mappings', 'environment_variables', 'lambda_config',
        ]
        read_only_fields = ['created_at', 'updated_at', 'status']

    def get_deploymet_url(self, obj):
        """Devuelve la URL de la primera región disponible."""
        if obj.service == 'lambda':
            return obj.deployment_url or ""
        # Si ya tenemos region_urls guardadas, devolver la primera
        if obj.region_urls:
            return obj.region_urls[0].get('url', obj.deployment_url or "")
        return obj.deployment_url or ""

    def get_region_urls(self, obj):
        """Devuelve [{region, url}] para cada región desplegada, consultando AWS en tiempo real."""
        if obj.service == 'lambda':
            return []

        import json as _json
        # Parsear cluster_arns guardado en aws_cluster_arn
        try:
            cluster_arns = _json.loads(obj.aws_cluster_arn or "[]")
        except Exception:
            cluster_arns = []

        # Si no hay multi-región, construir entrada única con aws_region
        if not cluster_arns:
            cluster_arns = [{'region': obj.aws_region, 'stack_name': f"{obj.name}-{obj.aws_region}-stack"}]

        results = []
        any_running = False

        for item in cluster_arns:
            region = item.get('region', obj.aws_region)
            stack_name = item.get('stack_name', f"{obj.name}-{region}-stack")
            try:
                aws_svc = AWSService(region)
                public_ip = aws_svc.get_first_task_public_ip(stack_name=stack_name)
                if isinstance(public_ip, dict):
                    # No está corriendo aún
                    results.append({'region': region, 'url': '', 'status': 'pending'})
                else:
                    url = f"http://{public_ip}"
                    results.append({'region': region, 'url': url, 'status': 'running'})
                    any_running = True
            except Exception as e:
                logger.warning("No se pudo obtener IP de %s (%s): %s", stack_name, region, e)
                results.append({'region': region, 'url': '', 'status': 'error'})

        # Persistir las URLs y actualizar estado si hay al menos una corriendo
        if results:
            obj.region_urls = results
            if any_running:
                obj.deployment_url = results[0]['url'] if results[0]['url'] else (obj.deployment_url or "")
                if obj.status != 'running':
                    obj.status = 'running'
            obj.save(update_fields=['region_urls', 'deployment_url', 'status'])

        return results

    def get_status_details(self, obj):
        from .services import DeploymentService
        try:
            deployment_service = DeploymentService()
            return deployment_service.get_deployment_status(obj)
        except Exception:
            return None 