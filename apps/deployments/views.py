import boto3
import re
import json
import time
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

DOMAIN_PATTERN = re.compile(r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$")  # validación básica

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def check_domain_availability(request):
    # Desempaquetar si viene como wrapper (e.g., {'body': '{"domain":"..."}', ...})
    raw = request.data
    if isinstance(raw, dict) and 'body' in raw and isinstance(raw['body'], str):
        try:
            payload = json.loads(raw['body'])
            print(f"Raw request data: {payload}")
        except json.JSONDecodeError:
            return Response({'error': 'Malformed JSON in body'}, status=status.HTTP_400_BAD_REQUEST)
    else:
        payload = raw

    serializer = checkDomainSerializer(data=payload)
    print(serializer.initial_data)  # Imprimir los datos iniciales para depuración
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
