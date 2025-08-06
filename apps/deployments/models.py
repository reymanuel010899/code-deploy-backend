from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator, MaxValueValidator

User = get_user_model()

class DockerImage(models.Model):
    """Modelo para almacenar información de imágenes Docker"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='docker_images')
    name = models.CharField(max_length=255)
    tag = models.CharField(max_length=100, default='latest')
    port = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
        blank=True, null=True,
        help_text="Puerto expuesto por la imagen Docker"
    )
    registry_url = models.URLField(max_length=500, blank=True, null=True,  help_text="URL del registro Docker (ej: docker.io, ecr.amazonaws.com)")
    repository_name = models.CharField(max_length=255, blank=True, null=True, help_text="Nombre del repositorio en el registro")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['user', 'name', 'tag', 'registry_url']
        
    def __str__(self):
        return f"{self.registry_url}/{self.repository_name}:{self.tag}"
    
    @property
    def full_image_name(self):
        return f"{self.registry_url}/{self.repository_name}:{self.tag}"

class Deployment(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('creating', 'Creating'),
        ('running', 'Running'),
        ('stopped', 'Stopped'),
        ('failed', 'Failed'),
        ('deleting', 'Deleting'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deployments')
    name = models.CharField(max_length=255)
    service = models.CharField(max_length=15, blank=True, null=True)
    domain_name = models.CharField(max_length=255, blank=True, null=True, help_text="Domain name for the deployment")
    regions = models.JSONField( default=list,
        help_text="List of Docker images in format [{'name': 'image_name', 'tag': 'latest', 'port': 80, 'registry_url': 'docker.io', 'repository_name': 'my_repo'}]",
        blank=True,)
    deployment_url = models.CharField(max_length=50, blank=True)
    # docker_image = models.ForeignKey(DockerImage, on_delete=models.PROTECT, blank=True, null=True, related_name='deployments')
    docker_images = models.JSONField(
        default=list,
        help_text="List of Docker images in format [{'name': 'image_name', 'tag': 'latest', 'port': 80, 'registry_url': 'docker.io', 'repository_name': 'my_repo'}]",
        blank=True,)
    cpu_units = models.IntegerField(
        validators=[MinValueValidator(256), MaxValueValidator(4096)],
        help_text="CPU units (256 = 0.25 vCPU, 1024 = 1 vCPU)"
    )
    memory_mb = models.IntegerField(
        validators=[MinValueValidator(512), MaxValueValidator(8192)],
        help_text="Memory in MB"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    

    # AWS ECS Fargate specific fields
    aws_task_arn = models.CharField(max_length=255, null=True, blank=True)
    aws_cluster_arn = models.CharField(max_length=255, null=True, blank=True)
    aws_service_arn = models.CharField(max_length=255, null=True, blank=True)
    aws_region = models.CharField(max_length=50, default='us-east-1')
    

    # Auto-scaling configuration
    auto_scaling_enabled = models.BooleanField(default=False)
    scaling_policies = models.JSONField(
        default=list,
        help_text="List of scaling policies in format [{'type': 'cpu|memory', 'target_value': 75, 'scale_in_cooldown': 60, 'scale_out_cooldown': 60}]",
        blank=True,
        null=True
    )
    desired_count = models.IntegerField(default=1, validators=[MinValueValidator(1)], blank=True, null=True)
    min_count = models.IntegerField(default=1, validators=[MinValueValidator(1)] , blank=True, null=True)
    max_count = models.IntegerField(default=1, validators=[MinValueValidator(1)] , blank=True, null=True)
    
    
    # Network configuration
    port_mappings = models.JSONField(
        default=list,
        help_text="List of port mappings in format [{'containerPort': 80, 'hostPort': 80, 'protocol': 'tcp'}]",
        blank=True,
        null=True
    )
    
    # Environment and secrets
    environment_variables = models.JSONField(
        default=dict,
        help_text="Environment variables for the container",
        blank=True,
        null=True
    )
    secrets = models.JSONField(
        default=dict,
        help_text="Secrets to be retrieved from AWS Secrets Manager",
        blank=True,
        null=True   
    )
    
    network_mode = models.CharField(
        max_length=20,
        default='awsvpc',
        choices=[
            ('awsvpc', 'awsvpc'),
            ('bridge', 'bridge'),
            ('host', 'host'),
            ('none', 'none')
        ]
    )
    load_balancer = models.BooleanField(default=False, help_text="Whether to attach a load balancer to the service")
    # # Container configuration
    # container_name = models.CharField(max_length=255, default='app')
    # command = models.JSONField(
    #     default=list,
    #     blank=True,
    #     help_text="Command to run in the container (overrides Dockerfile CMD)"
    # )
    # entrypoint = models.JSONField(
    #     default=list,
    #     blank=True,
    #     help_text="Entrypoint for the container (overrides Dockerfile ENTRYPOINT)"
    # )
    # Storage configuration
    # volumes = models.JSONField(
    #     default=list,
    #     help_text="List of volume mounts in format [{'name': 'my-volume', 'hostPath': '/data', 'containerPath': '/app/data'}]"
    # )
    
    # Health check configuration
    # health_check = models.JSONField(
    #     default=dict,
    #     help_text="Health check configuration for the container"
    # )
    
    # Logging configuration
    # log_group_name = models.CharField(max_length=255, null=True, blank=True)
    # log_stream_prefix = models.CharField(max_length=255, null=True, blank=True)
    
    # Scaling configuration
    
   
    # target_tracking_policies = models.JSONField(
    #     default=list,
    #     help_text="List of target tracking policies in format [{'type': 'CPUUtilization|MemoryUtilization', 'target_value': 75}]"
    # )
    # scheduled_actions = models.JSONField(
    #     default=list,
    #     help_text="List of scheduled scaling actions in format [{'schedule': 'cron(0 8 * * ? *)', 'min_capacity': 2, 'max_capacity': 10}]"
    # )




    #  'id', 'name', 'docker_image', 'status', 'status_details',
    #         'created_at', 'updated_at', 'cpu_units', 'memory_mb',
    #          'command', 'entrypoint', 'port_mappings',
    #         'network_mode', 'environment_variables', 'secrets',
    #         'volumes', 'health_check', 'desired_count', 'min_count',
    #         'max_count'
    
    class Meta:
        ordering = ['-created_at']
        
    def __str__(self):
        return f"{self.name} - {self.status}"
    
    # @property
    # def container_definition(self):
    #     """Returns the container definition in AWS ECS format"""
    #     return {
    #         'name': self.container_name,
    #         'image': self.docker_image.full_image_name,
    #         'cpu': self.cpu_units,
    #         'memory': self.memory_mb,
    #         'essential': True,
    #         'portMappings': self.port_mappings,
    #         'environment': [{'name': k, 'value': str(v)} for k, v in self.environment_variables.items()],
    #         'secrets': [{'name': k, 'valueFrom': v} for k, v in self.secrets.items()],
    #         'mountPoints': self.volumes,
    #         'healthCheck': self.health_check,
    #         'logConfiguration': {
    #             'logDriver': 'awslogs',
    #             'options': {
    #                 'awslogs-group': self.log_group_name,
    #                 'awslogs-region': self.aws_region,
    #                 'awslogs-stream-prefix': self.log_stream_prefix,
    #             }
    #         } if self.log_group_name else None,
    #         'command': self.command if self.command else None,
    #         'entryPoint': self.entrypoint if self.entrypoint else None,
    #     }

# class DeploymentLog(models.Model):
#     deployment = models.ForeignKey(Deployment, on_delete=models.CASCADE, related_name='logs')
#     message = models.TextField()
#     timestamp = models.DateTimeField(auto_now_add=True)
#     log_type = models.CharField(max_length=20, choices=[
#         ('info', 'Info'),
#         ('error', 'Error'),
#         ('warning', 'Warning'),
#         ('success', 'Success')
#     ])
#     source = models.CharField(max_length=50, choices=[
#         ('system', 'System'),
#         ('aws', 'AWS'),
#         ('container', 'Container'),
#         ('user', 'User')
#     ], default='system')
    
#     class Meta:
#         ordering = ['-timestamp']
        
#     def __str__(self):
#         return f"{self.deployment.name} - {self.log_type} - {self.timestamp}"

# class DeploymentMetrics(models.Model):
#     deployment = models.ForeignKey(Deployment, on_delete=models.CASCADE, related_name='metrics')
#     timestamp = models.DateTimeField(auto_now_add=True)
#     cpu_utilization = models.FloatField(help_text="CPU utilization percentage")
#     memory_utilization = models.FloatField(help_text="Memory utilization percentage")
#     network_bytes_in = models.BigIntegerField(help_text="Network bytes received")
#     network_bytes_out = models.BigIntegerField(help_text="Network bytes sent")
#     running_tasks = models.IntegerField(default=0, help_text="Number of running tasks")
#     pending_tasks = models.IntegerField(default=0, help_text="Number of pending tasks")
    
#     class Meta:
#         ordering = ['-timestamp']
        
#     def __str__(self):
#         return f"{self.deployment.name} - {self.timestamp}"
