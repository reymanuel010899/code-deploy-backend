"""
Configuración local para el proyecto deploy.
Este archivo no debe ser versionado y debe ser agregado a .gitignore
"""

# AWS Configuration
AWS_ACCESS_KEY_ID = 'your-access-key-id'
AWS_SECRET_ACCESS_KEY = 'your-secret-access-key'
AWS_DEFAULT_REGION = 'us-east-1'

# AWS ECS Configuration
AWS_ECS_CLUSTER_NAME = 'default'
AWS_ECS_EXECUTION_ROLE_ARN = 'arn:aws:iam::your-account-id:role/ecsTaskExecutionRole'
AWS_ECS_TASK_ROLE_ARN = 'arn:aws:iam::your-account-id:role/ecsTaskRole'
AWS_ECS_SUBNETS = ['subnet-xxxxxx', 'subnet-yyyyyy']  # Tus subnets
AWS_ECS_SECURITY_GROUPS = ['sg-xxxxxx']  # Tus security groups

# AWS ECR Configuration
AWS_ECR_REPOSITORY_PREFIX = 'your-repo-prefix'

# Logging Configuration
AWS_CLOUDWATCH_LOG_GROUP = '/ecs/deployments'
AWS_CLOUDWATCH_LOG_STREAM_PREFIX = 'deployment' 