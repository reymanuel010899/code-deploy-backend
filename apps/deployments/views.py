import boto3
import re
import json
import logging
import time
from datetime import datetime, timedelta
from rest_framework.views import APIView
from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.shortcuts import get_object_or_404
from django.conf import settings
from .services import DeploymentService
from .models import Deployment
from .serializers import (
    ContainerImageSerializer, DeploymentCreateSerializer, DeploymentUpdateSerializer,
    DeploymentDetailSerializer, DeploymentListSerializer, checkDomainSerializer, 

)
logger = logging.getLogger(__name__)
class IsOwner(permissions.BasePermission):
    """
    Permiso personalizado para permitir solo a los propietarios ver y editar sus deployments
    """
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user

# Vistas para Deployments
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def list_deployments(request):
    deployments = Deployment.objects.filter(user=request.user).order_by('-created_at')
    serializer = DeploymentListSerializer(deployments, many=True)
    return Response(serializer.data)

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def create_deployment(request):
    print(request.data)

    serializer = DeploymentCreateSerializer(data=request.data, context={'request': request})
    if serializer.is_valid():
        try:
            user = request.user
            service = serializer.validated_data.get('service')
            deployment = serializer.save()
            deployment_service = DeploymentService()

            if service == 'lambda':
                # El serializer resuelve el config en cualquier formato y lo guarda en _lambda_config
                lambda_config = getattr(deployment, '_lambda_config', None) or {}
                import threading
                thread = threading.Thread(
                    target=deployment_service.create_lambda_deployment,
                    args=(deployment, lambda_config),
                    daemon=True,
                )
                thread.start()
            else:
                docker_images = (
                    serializer.validated_data.get('dockerImages') or
                    serializer.validated_data.get('docker_images', [])
                )
                ecs_config = (
                    serializer.validated_data.get('ecsConfig') or
                    serializer.validated_data.get('ecs_config', {})
                )
                deployment_service.create_deployment(
                    deployment,
                    docker_images=docker_images,
                    environment_variables=ecs_config.get('environmentVariables', []),
                    ecs_config=ecs_config,
                    user=user,
                )

            return Response(
                {
                    "success": True,
                    "message": "Deployment created successfully",
                    "deployment_id": str(deployment.id),
                },
                status=status.HTTP_201_CREATED,
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['PUT'])
@permission_classes([permissions.IsAuthenticated])
def redeploy_ecs(request, deployment_id):
    """Re-despliega un ECS/EC2 existente con nueva configuración, sin crear uno nuevo."""
    try:
        deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user)
        raw = request.data

        ecs_config = raw.get('ecs_config') or {}
        docker_images = raw.get('docker_images') or []

        # Actualizar campos del modelo
        deployment.cpu_units    = ecs_config.get('taskCpu',      deployment.cpu_units)
        deployment.memory_mb    = ecs_config.get('taskMemory',   deployment.memory_mb)
        deployment.desired_count = ecs_config.get('desiredCount', deployment.desired_count)
        deployment.min_count    = ecs_config.get('minCapacity',  deployment.min_count)
        deployment.max_count    = ecs_config.get('maxCapacity',  deployment.max_count)
        deployment.network_mode = ecs_config.get('networkMode',  deployment.network_mode)
        deployment.load_balancer = ecs_config.get('loadBalancer', deployment.load_balancer)
        deployment.auto_scaling_enabled = ecs_config.get('autoScaling', deployment.auto_scaling_enabled)
        if docker_images:
            deployment.docker_images = docker_images
        env_vars = ecs_config.get('environmentVariables', [])
        if env_vars:
            deployment.environment_variables = env_vars
        deployment.status = 'creating'
        deployment.save()

        import threading
        thread = threading.Thread(
            target=DeploymentService().create_deployment,
            kwargs={
                'deployment': deployment,
                'docker_images': docker_images or deployment.docker_images,
                'environment_variables': env_vars,
                'ecs_config': ecs_config,
                'user': deployment.user,
            },
            daemon=True,
        )
        thread.start()

        return Response({
            'success': True,
            'message': 'Re-desplegando servicio...',
            'deployment_id': str(deployment.id),
        })
    except Exception as e:
        import traceback
        logger.error(f"Error en redeploy_ecs: {str(e)}\n{traceback.format_exc()}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def get_deployment(request, deployment_id):
    """Obtiene detalles de un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    serializer = DeploymentDetailSerializer(deployment)
    return Response(serializer.data)

@api_view(['PUT'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def update_deployment(request, deployment_id):
    """Actualiza un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    serializer = DeploymentUpdateSerializer(deployment, data=request.data, partial=True)
    
    if serializer.is_valid():
        try:
            updated_deployment = serializer.save()
            deployment_service = DeploymentService()
            deployment_service.update_deployment(updated_deployment)
            return Response(DeploymentDetailSerializer(updated_deployment).data)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['DELETE'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def delete_deployment(request, deployment_id):
    """Elimina un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    deployment_service = DeploymentService()
    try:
        deployment_service.delete_deployment(deployment)
        return Response({"message": "Deployment deleted successfully"}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def scale_deployment(request, deployment_id):
    """Escala un deployment a un número específico de tareas"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    desired_count = request.data.get('desired_count')
    
    if not desired_count or not isinstance(desired_count, int):
        return Response(
            {'error': 'desired_count is required and must be an integer'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    if desired_count < deployment.min_count or desired_count > deployment.max_count:
        return Response(
            {'error': f'desired_count must be between {deployment.min_count} and {deployment.max_count}'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    deployment.desired_count = desired_count
    deployment.save()
    
    deployment_service = DeploymentService()
    try:
        deployment_service.update_deployment(deployment)
        return Response({'status': 'scaling'})
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def stop_deployment(request, deployment_id):
    """Detiene un deployment poniendo desiredCount=0 en todos los ECS services."""
    deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user)
    deployment.desired_count = 0
    deployment.status = 'stopped'
    deployment.save()

    deployment_service = DeploymentService()
    try:
        deployment_service.update_deployment(deployment)
        return Response({'status': 'stopped'})
    except Exception as e:
        logger.error("Error deteniendo deployment %s: %s", deployment_id, e)
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def start_deployment(request, deployment_id):
    """Inicia un deployment restaurando desiredCount en todos los ECS services."""
    deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user)
    deployment.desired_count = max(1, deployment.min_count or 1)
    deployment.status = 'creating'
    deployment.save()

    deployment_service = DeploymentService()
    try:
        deployment_service.update_deployment(deployment)
        return Response({'status': 'starting'})
    except Exception as e:
        logger.error("Error iniciando deployment %s: %s", deployment_id, e)
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_deployment_logs(request, deployment_id):
    """Obtiene logs reales de CloudWatch para ECS o EC2."""
    try:
        deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user)

        import json as _json

        # Si el cliente pide una región específica la usamos; si no, la primera disponible
        requested_region = request.query_params.get('region', '').strip()
        if requested_region:
            region = requested_region
        else:
            regions_field = deployment.regions
            if isinstance(regions_field, list) and regions_field:
                region = regions_field[0]
            elif isinstance(regions_field, str):
                try:
                    parsed = _json.loads(regions_field)
                    region = parsed[0] if parsed else (deployment.aws_region or 'us-east-1')
                except Exception:
                    region = regions_field
            else:
                region = deployment.aws_region or 'us-east-1'
            region = str(region).strip().strip('[]"\' ')

        limit = int(request.query_params.get('limit', 100))
        logs_client = boto3.client('logs', region_name=region)

        # Para ECS multi-región el log group usa el stack_name de esa región
        svc = deployment.service or 'ecs'
        arn = deployment.aws_service_arn or ''

        # Buscar el stack_name correspondiente a la región pedida en aws_cluster_arn
        stack_name_for_region = None
        try:
            cluster_arns = _json.loads(deployment.aws_cluster_arn or '[]')
            for item in cluster_arns:
                if item.get('region') == region:
                    stack_name_for_region = item.get('stack_name')
                    break
        except Exception:
            pass

        if svc == 'ecs':
            if stack_name_for_region:
                log_group = f'/ecs/{stack_name_for_region}'
            else:
                service_name = arn.split('/')[-1] if arn else deployment.name
                log_group = f'/ecs/{service_name}'
        elif svc == 'lambda':
            # ARN de Lambda: arn:aws:lambda:region:acct:function:function-name
            function_name = arn.split(':')[-1] if arn else deployment.name
            log_group = f'/aws/lambda/{function_name}'
        elif svc == 'ec2':
            # aws_service_arn contiene el instance-id directamente o un ARN
            if arn.startswith('arn:'):
                instance_id = arn.split('/')[-1]
            else:
                instance_id = arn or deployment.name
            log_group = f'/ec2/{instance_id}'
        else:
            log_group = f'/ecs/{deployment.name}'

        try:
            streams_resp = logs_client.describe_log_streams(
                logGroupName=log_group,
                orderBy='LastEventTime',
                descending=True,
                limit=5,
            )
            streams = streams_resp.get('logStreams', [])
        except logs_client.exceptions.ResourceNotFoundException:
            return Response({'logs': [], 'log_group': log_group, 'region': region,
                             'message': 'No hay logs disponibles aún. El servicio debe generar actividad primero.'})

        if not streams:
            return Response({'logs': [], 'log_group': log_group, 'region': region,
                             'message': 'No hay streams de log aún.'})

        all_events = []
        for stream in streams:
            try:
                events_resp = logs_client.get_log_events(
                    logGroupName=log_group,
                    logStreamName=stream['logStreamName'],
                    limit=limit,
                    startFromHead=False,
                )
                for ev in events_resp.get('events', []):
                    all_events.append({
                        'timestamp': ev['timestamp'],
                        'message': ev['message'].rstrip('\n'),
                        'stream': stream['logStreamName'],
                    })
            except Exception:
                continue

        all_events.sort(key=lambda x: x['timestamp'], reverse=True)
        return Response({
            'logs': all_events[:limit],
            'log_group': log_group,
            'region': region,
            'function_name': deployment.name,
        })

    except Exception as e:
        import traceback
        logger.error(f"Error obteniendo logs deployment: {str(e)}\n{traceback.format_exc()}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_deployment_metrics(request, deployment_id):
    """Obtiene métricas reales de CloudWatch para ECS o EC2."""
    deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user)

    hours  = int(request.query_params.get('hours', 24))
    end_time   = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)
    period = 3600  # 1 punto por hora

    regions_field = deployment.regions
    if isinstance(regions_field, list) and regions_field:
        region = regions_field[0]
    elif isinstance(regions_field, str):
        import json as _json
        try:
            parsed = _json.loads(regions_field)
            region = parsed[0] if parsed else (deployment.aws_region or 'us-east-1')
        except Exception:
            region = regions_field
    else:
        region = deployment.aws_region or 'us-east-1'
    # strip any surrounding brackets/quotes just in case
    region = str(region).strip().strip('[]"\' ')
    cw = boto3.client('cloudwatch', region_name=region)

    def cw_metric(namespace, metric_name, dimensions, stat='Average'):
        try:
            resp = cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start_time,
                EndTime=end_time,
                Period=period,
                Statistics=[stat],
            )
            pts = sorted(resp.get('Datapoints', []), key=lambda p: p['Timestamp'])
            return [{'timestamp': p['Timestamp'].isoformat(), 'value': round(p[stat], 2)} for p in pts]
        except Exception:
            return []

    result = {
        'service_type': deployment.service,
        'region': region,
        'period_hours': hours,
        'name': deployment.name,
        'status': deployment.status,
        'deployment_url': deployment.deployment_url or '',
        'created_at': deployment.created_at.isoformat(),
        'cpu_units': deployment.cpu_units,
        'memory_mb': deployment.memory_mb,
    }

    if deployment.service == 'ecs' and deployment.aws_cluster_arn:
        cluster_name = deployment.aws_cluster_arn.split('/')[-1]
        service_name = deployment.aws_service_arn.split('/')[-1] if deployment.aws_service_arn else deployment.name
        dims_service = [
            {'Name': 'ClusterName', 'Value': cluster_name},
            {'Name': 'ServiceName', 'Value': service_name},
        ]
        result.update({
            'cpu':              cw_metric('AWS/ECS', 'CPUUtilization',    dims_service),
            'memory':           cw_metric('AWS/ECS', 'MemoryUtilization', dims_service),
            'running_tasks':    cw_metric('ECS/ContainerInsights', 'RunningTaskCount', dims_service, 'Average'),
            'desired_tasks':    cw_metric('ECS/ContainerInsights', 'DesiredTaskCount', dims_service, 'Average'),
            'network_rx':       cw_metric('ECS/ContainerInsights', 'NetworkRxBytes',   dims_service, 'Sum'),
            'network_tx':       cw_metric('ECS/ContainerInsights', 'NetworkTxBytes',   dims_service, 'Sum'),
        })
    elif deployment.service == 'ec2' and deployment.aws_service_arn:
        # aws_service_arn stores instance-id for EC2
        instance_id = deployment.aws_service_arn.split('/')[-1]
        dims_ec2 = [{'Name': 'InstanceId', 'Value': instance_id}]
        result.update({
            'cpu':          cw_metric('AWS/EC2', 'CPUUtilization',       dims_ec2),
            'network_rx':   cw_metric('AWS/EC2', 'NetworkIn',            dims_ec2, 'Sum'),
            'network_tx':   cw_metric('AWS/EC2', 'NetworkOut',           dims_ec2, 'Sum'),
            'disk_read':    cw_metric('AWS/EC2', 'DiskReadBytes',         dims_ec2, 'Sum'),
            'disk_write':   cw_metric('AWS/EC2', 'DiskWriteBytes',        dims_ec2, 'Sum'),
            'status_checks':cw_metric('AWS/EC2', 'StatusCheckFailed',    dims_ec2, 'Sum'),
        })
    else:
        result['error'] = 'No hay ARN de cluster/servicio disponible para este deployment.'

    return Response(result)

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def get_deployment_status(request, deployment_id):
    """Obtiene el estado detallado de un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)

    # Para Lambda retornamos directamente el estado del modelo
    if deployment.service == 'lambda':
        progress = 100 if deployment.status == 'running' else (50 if deployment.status == 'creating' else (0 if deployment.status == 'pending' else 0))
        return Response({
            'status': 'completed' if deployment.status == 'running' else deployment.status,
            'progress': progress,
            'currentStep': 'Lambda desplegada y API Gateway conectado' if deployment.status == 'running' else 'Desplegando función Lambda...',
            'deployment_url': deployment.deployment_url or '',
            'function_arn': deployment.aws_service_arn or '',
            'resources': {
                'serviceArn': deployment.aws_service_arn or '',
                'apiUrl': deployment.deployment_url or '',
            }
        })

    deployment_service = DeploymentService()
    try:
        status_details = deployment_service.get_deployment_status(deployment)
        return Response(status_details)
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def list_lambda_functions(request):
    """Lista todos los deployments Lambda del usuario con su config completa."""
    deployments = Deployment.objects.filter(user=request.user, service='lambda').order_by('-created_at')
    data = []
    for d in deployments:
        env_config = d.environment_variables or {}
        # environment_variables puede ser dict o lista, normalizamos a string KEY=VAL
        if isinstance(env_config, dict):
            env_str = '\n'.join(f"{k}={v}" for k, v in env_config.items())
        elif isinstance(env_config, list):
            env_str = '\n'.join(f"{item.get('name','')}={item.get('value','')}" for item in env_config)
        else:
            env_str = str(env_config)

        # Use the persisted lambda_config field (saved on each deploy)
        stored_cfg = d.lambda_config or {}
        data.append({
            'id': d.id,
            'name': d.name,
            'status': d.status,
            'deployment_url': d.deployment_url or '',
            'function_arn': d.aws_service_arn or '',
            'region': d.aws_region or 'us-east-1',
            'created_at': d.created_at.isoformat(),
            'updated_at': d.updated_at.isoformat(),
            'memory_mb': d.memory_mb,
            'lambda_config': {
                'runtime':        stored_cfg.get('runtime', 'nodejs18.x'),
                'handler':        stored_cfg.get('handler', 'index.handler'),
                'timeout':        stored_cfg.get('timeout', 30),
                'memory':         stored_cfg.get('memory', d.memory_mb),
                'environmentVars': stored_cfg.get('environmentVars', env_str),
                'trigger':        stored_cfg.get('trigger', 'api-gateway'),
                'httpMethod':     stored_cfg.get('httpMethod', 'POST'),
                'deadLetterQueue': stored_cfg.get('deadLetterQueue', False),
                'codeFiles':      stored_cfg.get('codeFiles', []),
            },
        })
    return Response(data)


@api_view(['PUT'])
@permission_classes([permissions.IsAuthenticated])
def update_lambda_function(request, deployment_id):
    """Re-despliega una función Lambda existente con nueva config/código."""
    try:
        deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user, service='lambda')
        raw = request.data

        logger.info(f"update_lambda_function raw data keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")

        lambda_config = {
            'runtime':        raw.get('runtime', 'nodejs18.x'),
            'handler':        raw.get('handler', 'index.handler'),
            'timeout':        min(int(raw.get('timeout', 30)), 900),
            'memory':         int(raw.get('memory_mb', 128)),
            'environmentVars': raw.get('environment_vars', '') or '',
            'trigger':        raw.get('trigger', 'api-gateway'),
            'http_method':    (raw.get('http_method') or 'POST').upper(),
            'deadLetterQueue': bool(raw.get('dead_letter_queue', False)),
            'codeFiles':      [dict(f) for f in (raw.get('code_files') or [])],
        }

        logger.info(f"update_lambda_function lambda_config: runtime={lambda_config['runtime']} timeout={lambda_config['timeout']} files={len(lambda_config['codeFiles'])}")

        deployment.memory_mb = lambda_config['memory']
        deployment.status = 'creating'
        deployment.save()

        import threading
        from .services import DeploymentService
        thread = threading.Thread(
            target=DeploymentService().create_lambda_deployment,
            args=(deployment, lambda_config),
            daemon=True,
        )
        thread.start()

        return Response({
            'success': True,
            'message': 'Actualizando función Lambda...',
            'deployment_id': str(deployment.id),
        })
    except Exception as e:
        import traceback
        logger.error(f"Error en update_lambda_function: {str(e)}\n{traceback.format_exc()}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_lambda_logs(request, deployment_id):
    """Obtiene los logs de CloudWatch de una función Lambda."""
    try:
        deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user, service='lambda')

        region = (deployment.regions or ['us-east-1'])[0]
        function_name = deployment.name
        limit = int(request.query_params.get('limit', 50))

        logs_client = boto3.client('logs', region_name=region)
        log_group = f'/aws/lambda/{deployment.aws_service_arn.split(":")[-1]}' if deployment.aws_service_arn else f'/aws/lambda/{function_name}'

        # Obtener los log streams más recientes
        try:
            streams_resp = logs_client.describe_log_streams(
                logGroupName=log_group,
                orderBy='LastEventTime',
                descending=True,
                limit=5,
            )
            streams = streams_resp.get('logStreams', [])
        except logs_client.exceptions.ResourceNotFoundException:
            return Response({'logs': [], 'message': 'No hay logs disponibles aún. Invoca la función para generar logs.'})

        if not streams:
            return Response({'logs': [], 'message': 'No hay streams de log aún.'})

        # Leer eventos de los últimos streams
        all_events = []
        for stream in streams:
            try:
                events_resp = logs_client.get_log_events(
                    logGroupName=log_group,
                    logStreamName=stream['logStreamName'],
                    limit=limit,
                    startFromHead=False,
                )
                for ev in events_resp.get('events', []):
                    all_events.append({
                        'timestamp': ev['timestamp'],
                        'message': ev['message'].rstrip('\n'),
                        'stream': stream['logStreamName'],
                    })
            except Exception:
                continue

        # Ordenar por timestamp descendente y limitar
        all_events.sort(key=lambda x: x['timestamp'], reverse=True)
        all_events = all_events[:limit]

        return Response({
            'logs': all_events,
            'log_group': log_group,
            'function_name': deployment.aws_service_arn.split(':')[-1] if deployment.aws_service_arn else function_name,
            'region': region,
        })

    except Exception as e:
        import traceback
        logger.error(f"Error obteniendo logs Lambda: {str(e)}\n{traceback.format_exc()}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_lambda_metrics(request, deployment_id):
    """Obtiene métricas reales de CloudWatch para una función Lambda."""
    try:
        deployment = get_object_or_404(Deployment, id=deployment_id, user=request.user, service='lambda')

        region = (deployment.regions or ['us-east-1'])[0]
        function_name = deployment.aws_service_arn.split(':')[-1] if deployment.aws_service_arn else deployment.name

        hours = int(request.query_params.get('hours', 24))
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=hours)
        period = 3600  # 1 hora por punto

        cw = boto3.client('cloudwatch', region_name=region)

        def get_metric(metric_name, stat='Sum'):
            resp = cw.get_metric_statistics(
                Namespace='AWS/Lambda',
                MetricName=metric_name,
                Dimensions=[{'Name': 'FunctionName', 'Value': function_name}],
                StartTime=start_time,
                EndTime=end_time,
                Period=period,
                Statistics=[stat],
            )
            points = sorted(resp.get('Datapoints', []), key=lambda p: p['Timestamp'])
            return [{'timestamp': p['Timestamp'].isoformat(), 'value': p[stat]} for p in points]

        invocations = get_metric('Invocations', 'Sum')
        errors      = get_metric('Errors', 'Sum')
        throttles   = get_metric('Throttles', 'Sum')

        # Duration: necesita Average, Min, Max por separado
        dur_avg  = get_metric('Duration', 'Average')
        dur_min  = get_metric('Duration', 'Minimum')
        dur_max  = get_metric('Duration', 'Maximum')
        # p99 no está disponible en get_metric_statistics estándar, usamos percentile via get_metric_data
        try:
            p99_resp = cw.get_metric_data(
                MetricDataQueries=[{
                    'Id': 'p99',
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'AWS/Lambda',
                            'MetricName': 'Duration',
                            'Dimensions': [{'Name': 'FunctionName', 'Value': function_name}],
                        },
                        'Period': period,
                        'Stat': 'p99',
                    },
                    'ReturnData': True,
                }],
                StartTime=start_time,
                EndTime=end_time,
            )
            p99_vals = p99_resp['MetricDataResults'][0].get('Values', [])
            p99 = p99_vals[0] if p99_vals else 0
        except Exception:
            p99 = 0

        total_invocations = sum(p['value'] for p in invocations)
        total_errors      = sum(p['value'] for p in errors)
        error_rate        = round((total_errors / total_invocations * 100), 2) if total_invocations > 0 else 0

        avg_vals = [p['value'] for p in dur_avg]
        min_vals = [p['value'] for p in dur_min]
        max_vals = [p['value'] for p in dur_max]

        duration = {
            'avg': round(sum(avg_vals) / len(avg_vals), 2) if avg_vals else 0,
            'min': round(min(min_vals), 2) if min_vals else 0,
            'max': round(max(max_vals), 2) if max_vals else 0,
            'p99': round(p99, 2),
            'series': dur_avg,  # serie temporal del promedio
        }

        return Response({
            'function_name': function_name,
            'region': region,
            'period_hours': hours,
            'invocations': invocations,
            'errors': errors,
            'throttles': throttles,
            'duration': duration,
            'totalInvocations': int(total_invocations),
            'totalErrors': int(total_errors),
            'errorRate': error_rate,
        })

    except Exception as e:
        import traceback
        logger.error(f"Error obteniendo métricas Lambda: {str(e)}\n{traceback.format_exc()}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


DOMAIN_PATTERN = re.compile(r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$")  # validación básica

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def check_domain_availability(request):
    # Desempaquetar si viene como wrapper (e.g., {'body': '{"domain":"..."}', ...})
    raw = request.data
    if isinstance(raw, dict) and 'body' in raw and isinstance(raw['body'], str):
        try:
            payload = json.loads(raw['body'])
            logger.debug(f"Raw request data: {payload}")
        except json.JSONDecodeError:
            return Response({'error': 'Malformed JSON in body'}, status=status.HTTP_400_BAD_REQUEST)
    else:
        payload = raw

    serializer = checkDomainSerializer(data=payload)
    logger.debug(f"checkDomain initial_data: {serializer.initial_data}")
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    domain = serializer.validated_data.get('domain')
    if not domain or not DOMAIN_PATTERN.match(domain):
        return Response({'error': 'Domain is required or format invalid'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        client_kwargs = {'region_name': 'us-east-1'}
        aws_key = getattr(settings, 'AWS_ACCESS_KEY_ID', None)
        aws_secret = getattr(settings, 'AWS_SECRET_ACCESS_KEY', None)
        if aws_key and aws_secret:
            client_kwargs.update({
                'aws_access_key_id': aws_key,
                'aws_secret_access_key': aws_secret
            })

        route53_client = boto3.client('route53domains', **client_kwargs)

        availability_resp = route53_client.check_domain_availability(DomainName=domain)
        availability = availability_resp.get('Availability')

        result = {'domain': domain, 'available': False, 'message': ''}

        if availability in ('AVAILABLE', 'AVAILABLE_RESERVED', 'AVAILABLE_PREORDER'):
            result['available'] = True
            result['message'] = f'Domain {domain} is available for registration'
            # obtener precio básico del TLD
            tld = domain.split('.', 1)[-1]
            try:
                prices_resp = route53_client.list_prices(Tld=f".{tld}")
                prices = prices_resp.get('Prices', [])
                if prices:
                    price_info = prices[0].get('RegistrationPrice', {})
                    price = price_info.get('Price')
                    currency = price_info.get('Currency')
                    if price is not None:
                        result['price'] = f"{price} {currency}"
                    else:
                        result['price'] = "Unknown"
                else:
                    result['price'] = "Unknown"
            except Exception:
                result['price'] = "Unknown"
        else:
            result['available'] = False
            result['message'] = f'Domain {domain} is not available for registration'
            try:
                suggestions_resp = route53_client.get_domain_suggestions(
                    DomainName=domain,
                    SuggestionCount=3,
                    OnlyAvailable=True
                )
                suggestions = [
                    s.get('DomainName')
                    for s in suggestions_resp.get('SuggestionsList', [])
                    if s.get('Availability') == 'AVAILABLE'
                ]
                if suggestions:
                    result['suggestions'] = suggestions
            except Exception:
                pass

        return Response(result)
    except Exception as e:
        return Response({'error': f'Error checking domain availability: {str(e)}'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def purchase_domain(request):
    """Purchase domain in Route53 and configure DNS"""
    domain = request.data.get('domain')
    load_balancer_dns = request.data.get('load_balancer_dns')
    
    if not domain or not load_balancer_dns:
        return Response(
            {'error': 'Domain and load balancer DNS are required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
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
            DomainName=domain,
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
            if zone['Name'] == f'{domain}.':
                domain_hosted_zone = zone
                break
        
        if not domain_hosted_zone:
            # Create hosted zone for the domain
            hosted_zone_response = route53_client.create_hosted_zone(
                Name=domain,
                CallerReference=f'{domain}-{int(time.time())}'
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
                            'Name': domain,
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
        
        return Response({
            'success': True,
            'message': f'Domain {domain} purchased and configured successfully',
            'operation_id': registration_response.get('OperationId')
        })
        
    except Exception as e:
        return Response(
            {'error': f'Error purchasing domain: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ── Docker Hub proxy endpoints ────────────────────────────────────────────────

import urllib.request
import urllib.error

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def dockerhub_login(request):
    """Proxy de login a Docker Hub para evitar CORS en el frontend."""
    username = request.data.get('username', '').strip()
    password = request.data.get('password', '').strip()

    if not username or not password:
        return Response({'error': 'Usuario y contraseña requeridos'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        import urllib.request, urllib.error, json as _json
        payload = _json.dumps({'username': username, 'password': password}).encode()
        req = urllib.request.Request(
            'https://hub.docker.com/v2/users/login/',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())

        token = data.get('token')
        if not token:
            return Response({'error': 'Credenciales inválidas'}, status=status.HTTP_401_UNAUTHORIZED)

        # Fetch user profile
        user_req = urllib.request.Request(
            f'https://hub.docker.com/v2/users/{username}/',
            headers={'Authorization': f'Bearer {token}'},
        )
        with urllib.request.urlopen(user_req, timeout=10) as uresp:
            user_data = _json.loads(uresp.read())

        return Response({
            'token': token,
            'username': user_data.get('username', username),
            'full_name': user_data.get('full_name', ''),
            'company': user_data.get('company', ''),
            'gravatar_url': user_data.get('gravatar_url', ''),
        })

    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = _json.loads(body).get('detail', 'Credenciales inválidas')
        except Exception:
            msg = 'Credenciales inválidas'
        return Response({'error': msg}, status=status.HTTP_401_UNAUTHORIZED)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def dockerhub_repositories(request):
    """Lista los repositorios de un usuario de Docker Hub."""
    username = request.query_params.get('username', '').strip()
    token    = request.query_params.get('token', '').strip()
    page     = request.query_params.get('page', '1')
    page_size = request.query_params.get('page_size', '25')

    if not username or not token:
        return Response({'error': 'username y token requeridos'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        import urllib.request, urllib.error, json as _json, base64
        url = f'https://hub.docker.com/v2/repositories/{username}/?page={page}&page_size={page_size}&ordering=last_updated'
        # Docker Hub v2 API accepts Basic Auth (username:PAT) or Bearer token from /users/login
        # Try Bearer first, fall back to Basic if it fails
        def _fetch(auth_header):
            req = urllib.request.Request(url, headers={'Authorization': auth_header})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read())

        try:
            data = _fetch(f'Bearer {token}')
        except urllib.error.HTTPError:
            # token may be a PAT — try Basic Auth
            creds = base64.b64encode(f'{username}:{token}'.encode()).decode()
            data = _fetch(f'Basic {creds}')

        results = [
            {
                'name': r.get('name') or '',
                'full_name': r.get('full_name') or f"{username}/{r.get('name', '')}",
                'description': r.get('description', ''),
                'is_private': r.get('is_private', False),
                'pull_count': r.get('pull_count', 0),
                'star_count': r.get('star_count', 0),
                'last_updated': r.get('last_updated', ''),
            }
            for r in data.get('results', [])
            if r.get('name')
        ]

        return Response({
            'count': data.get('count', 0),
            'next': data.get('next'),
            'previous': data.get('previous'),
            'results': results,
        })

    except Exception as e:
        import traceback
        return Response({'error': str(e), 'detail': traceback.format_exc()}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def dockerhub_tags(request):
    """Lista los tags de un repositorio de Docker Hub."""
    full_name = request.query_params.get('full_name', '').strip()
    token     = request.query_params.get('token', '').strip()

    if not full_name or not token:
        return Response({'error': 'full_name y token requeridos'}, status=status.HTTP_400_BAD_REQUEST)

    # username needed for Basic Auth fallback — extract from full_name (owner/repo)
    username = full_name.split('/')[0] if '/' in full_name else ''

    try:
        import urllib.request, urllib.error, json as _json, base64
        url = f'https://hub.docker.com/v2/repositories/{full_name}/tags/?page_size=20&ordering=last_updated'

        def _fetch(auth_header):
            req = urllib.request.Request(url, headers={'Authorization': auth_header})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read())

        try:
            data = _fetch(f'Bearer {token}')
        except urllib.error.HTTPError:
            creds = base64.b64encode(f'{username}:{token}'.encode()).decode()
            data = _fetch(f'Basic {creds}')

        tags = [
            {
                'name': t.get('name'),
                'full_size': t.get('full_size', 0),
                'last_updated': t.get('last_updated', ''),
            }
            for t in data.get('results', [])
            if t.get('name')
        ]

        return Response({'tags': tags})

    except Exception as e:
        import traceback
        return Response({'error': str(e), 'detail': traceback.format_exc()}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
