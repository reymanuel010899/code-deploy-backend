from django.urls import path
from . import views

urlpatterns = [
    # Docker Images URLs
    path('images/', views.list_docker_images, name='list-docker-images'),
    path('images/create/', views.create_docker_image, name='create-docker-image'),
    path('images/<int:image_id>/', views.get_docker_image, name='get-docker-image'),
    path('images/<int:image_id>/update/', views.update_docker_image, name='update-docker-image'),
    path('images/<int:image_id>/delete/', views.delete_docker_image, name='delete-docker-image'),
    path('images/<int:image_id>/validate/', views.validate_docker_image, name='validate-docker-image'),
    path('images/<int:image_id>/details/', views.get_docker_image_details, name='get-docker-image-details'),
    
    # Deployments URLs
    path('deployments/', views.list_deployments, name='list-deployments'),
    path('deployments/create/', views.create_deployment, name='create-deployment'),
    path('deployments/<int:deployment_id>/', views.get_deployment, name='get-deployment'),
    path('deployments/<int:deployment_id>/update/', views.update_deployment, name='update-deployment'),
    path('deployments/<int:deployment_id>/delete/', views.delete_deployment, name='delete-deployment'),
    path('deployments/<int:deployment_id>/scale/', views.scale_deployment, name='scale-deployment'),
    path('deployments/<int:deployment_id>/stop/', views.stop_deployment, name='stop-deployment'),
    path('deployments/<int:deployment_id>/start/', views.start_deployment, name='start-deployment'),
    path('deployments/<int:deployment_id>/logs/', views.get_deployment_logs, name='get-deployment-logs'),
    path('deployments/<int:deployment_id>/metrics/', views.get_deployment_metrics, name='get-deployment-metrics'),
    path('deployments/<int:deployment_id>/status/', views.get_deployment_status, name='get-deployment-status'),
] 