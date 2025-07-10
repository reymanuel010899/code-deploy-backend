from rest_framework.views import APIView
from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.shortcuts import get_object_or_404
from .models import Deployment
from .serializers import (
    ContainerImageSerializer, DeploymentCreateSerializer, DeploymentUpdateSerializer,
    DeploymentDetailSerializer, DeploymentListSerializer,

)
from .services import DeploymentService, DockerImageService

class IsOwner(permissions.BasePermission):
    """
    Permiso personalizado para permitir solo a los propietarios ver y editar sus deployments
    """
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user

# Vistas para Docker Images
# @api_view(['GET'])
# @permission_classes([permissions.IsAuthenticated])
# def list_docker_images(request):
#     """Lista todas las imágenes Docker del usuario"""
#     images = DockerImage.objects.filter(user=request.user)
#     serializer = DockerImageSerializer(images, many=True)
#     return Response(serializer.data)

# @api_view(['POST'])
# # @permission_classes([permissions.IsAuthenticated])
# def create_docker_image(request):
#     """Crea una nueva imagen Docker"""
#     serializer = DockerImageSerializer(data=request.data)
#     if serializer.is_valid():
#         serializer.save(user=request.user)
#         return Response(serializer.data, status=status.HTTP_201_CREATED)
#     return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# @api_view(['GET'])
# @permission_classes([permissions.IsAuthenticated, IsOwner])
# def get_docker_image(request, image_id):
#     """Obtiene detalles de una imagen Docker"""
#     image = get_object_or_404(DockerImage, id=image_id)
#     serializer = DockerImageSerializer(image)
#     return Response(serializer.data)

# @api_view(['PUT'])
# # @permission_classes([permissions.IsAuthenticated, IsOwner])
# def update_database_container(request, image_id):
#     # image = get_object_or_404(DockerImage, id=image_id)
#     serializer = ContainerImageSerializer(data=request.data, partial=True)
#     if serializer.is_valid():
#         serializer.save()
#         return Response(serializer.data)
#     return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# @api_view(['DELETE'])
# @permission_classes([permissions.IsAuthenticated, IsOwner])
# def delete_docker_image(request, image_id):
#     """Elimina una imagen Docker"""
#     image = get_object_or_404(DockerImage, id=image_id)
#     image.delete()
#     return Response(status=status.HTTP_204_NO_CONTENT)

# @api_view(['POST'])
# @permission_classes([permissions.IsAuthenticated, IsOwner])
# def validate_docker_image(request, image_id):
#     """Valida que la imagen Docker exista y sea accesible"""
#     image = get_object_or_404(DockerImage, id=image_id)
#     docker_service = DockerImageService()
    
#     if docker_service.validate_image(image):
#         return Response({'status': 'valid'})
#     return Response(
#         {'status': 'invalid', 'message': 'Image not found or not accessible'},
#         status=status.HTTP_400_BAD_REQUEST
#     )

# @api_view(['GET'])
# @permission_classes([permissions.IsAuthenticated, IsOwner])
# def get_docker_image_details(request, image_id):
#     """Obtiene detalles técnicos de una imagen Docker"""
#     image = get_object_or_404(DockerImage, id=image_id)
#     docker_service = DockerImageService()
    
#     try:
#         details = docker_service.get_image_details(image)
#         return Response(details)
#     except Exception as e:
#         return Response(
#             {'error': str(e)},
#             status=status.HTTP_400_BAD_REQUEST
#         )

# Vistas para Deployments
@api_view(['GET'])
# @permission_classes([permissions.IsAuthenticated])
def list_deployments(request):
    deployments = Deployment.objects.filter(status='pending')
    
    serializer = DeploymentListSerializer(deployments, many=True)
    return Response(serializer.data)

@api_view(['POST'])
# @permission_classes([permissions.IsAuthenticated])
def create_deployment(request):
    serializer = DeploymentCreateSerializer(data=request.data, context={'request': request})
    if serializer.is_valid():
        try:
            user = request.user
            docker_images = serializer.validated_data.get('docker_images')
            ecs_config = serializer.validated_data.get('ecs_config', {})
            deployment = serializer.save()
            deployment_service = DeploymentService()
            deployment_service.create_deployment(deployment, docker_images=docker_images, environment_variables=ecs_config.get('environmentVariables', []), ecs_config=ecs_config, user=user)
            
            return Response(
                DeploymentDetailSerializer(deployment).data,
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
        return Response(status=status.HTTP_204_NO_CONTENT)
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
    """Detiene un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    deployment.desired_count = 0
    deployment.save()
    
    deployment_service = DeploymentService()
    try:
        deployment_service.update_deployment(deployment)
        return Response({'status': 'stopping'})
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def start_deployment(request, deployment_id):
    """Inicia un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    deployment.desired_count = max(1, deployment.min_count)
    deployment.save()
    
    deployment_service = DeploymentService()
    try:
        deployment_service.update_deployment(deployment)
        return Response({'status': 'starting'})
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def get_deployment_logs(request, deployment_id):
    """Obtiene los logs de un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    logs = deployment.logs.all().order_by('-timestamp')[:100]
    serializer = DeploymentLogSerializer(logs, many=True)
    return Response(serializer.data)

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def get_deployment_metrics(request, deployment_id):
    """Obtiene las métricas de un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    metrics = deployment.metrics.all().order_by('-timestamp')[:100]
    serializer = DeploymentMetricsSerializer(metrics, many=True)
    return Response(serializer.data)

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOwner])
def get_deployment_status(request, deployment_id):
    """Obtiene el estado detallado de un deployment"""
    deployment = get_object_or_404(Deployment, id=deployment_id)
    deployment_service = DeploymentService()
    try:
        status_details = deployment_service.get_deployment_status(deployment)
        return Response(status_details)
    except Exception as e:
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
