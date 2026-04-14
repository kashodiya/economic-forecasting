terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }

  backend "s3" {
    key     = "tfstate/terraform.tfstate"
    encrypt = true
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile
}

resource "aws_s3_bucket" "project" {
  bucket = var.project_bucket_name

  tags = {
    Project = "economic-forecasting"
  }
}

resource "aws_s3_bucket_versioning" "project" {
  bucket = aws_s3_bucket.project.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "project" {
  bucket = aws_s3_bucket.project.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "project" {
  bucket = aws_s3_bucket.project.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- Networking ---

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name    = "economic-forecasting-vpc"
    Project = "economic-forecasting"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name    = "economic-forecasting-igw"
    Project = "economic-forecasting"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = "${var.aws_region}a"

  tags = {
    Name    = "economic-forecasting-public"
    Project = "economic-forecasting"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name    = "economic-forecasting-rt"
    Project = "economic-forecasting"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --- EC2 Instance ---

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_security_group" "ec2" {
  name        = "economic-forecasting-ec2-sg"
  description = "Security group for economic-forecasting EC2"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }

  ingress {
    description = "code-server"
    from_port   = 8443
    to_port     = 8443
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }

  ingress {
    description = "JupyterLab"
    from_port   = 8888
    to_port     = 8888
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Project = "economic-forecasting"
  }
}

resource "aws_iam_role" "ec2" {
  name = "economic-forecasting-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = {
    Project = "economic-forecasting"
  }
}

resource "aws_iam_role_policy_attachment" "ec2_admin" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "economic-forecasting-ec2-profile"
  role = aws_iam_role.ec2.name
}

resource "tls_private_key" "ec2" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "ec2" {
  key_name   = "economic-forecasting-key"
  public_key = tls_private_key.ec2.public_key_openssh

  tags = {
    Project = "economic-forecasting"
  }
}

resource "local_file" "private_key" {
  content         = tls_private_key.ec2.private_key_pem
  filename        = "${path.module}/../../notes/economic-forecasting-key.pem"
  file_permission = "0400"
}

resource "aws_instance" "main" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = var.ec2_instance_type
  key_name               = aws_key_pair.ec2.key_name
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  root_block_device {
    volume_size = var.ec2_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  tags = {
    Name    = "economic-forecasting"
    Project = "economic-forecasting"
  }
}

# --- Secrets ---

resource "aws_secretsmanager_secret" "app_password" {
  name = "economic-forecasting/app-password"

  tags = { Project = "economic-forecasting" }
}

resource "aws_secretsmanager_secret_version" "app_password" {
  secret_id     = aws_secretsmanager_secret.app_password.id
  secret_string = var.app_password
}

# --- Dashboard Lambda ---

data "archive_file" "dashboard" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/dashboard"
  output_path = "${path.module}/../lambda/dashboard.zip"
}

resource "aws_iam_role" "lambda_dashboard" {
  name = "economic-forecasting-lambda-dashboard"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = { Project = "economic-forecasting" }
}

resource "aws_iam_role_policy" "lambda_ec2" {
  name = "ec2-start-stop"
  role = aws_iam_role.lambda_dashboard.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:StartInstances",
          "ec2:StopInstances"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.project.arn}/config/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_dashboard.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "dashboard" {
  function_name    = "economic-forecasting-dashboard"
  role             = aws_iam_role.lambda_dashboard.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 15
  filename         = data.archive_file.dashboard.output_path
  source_code_hash = data.archive_file.dashboard.output_base64sha256

  environment {
    variables = {
      EC2_INSTANCE_ID = aws_instance.main.id
      CONFIG_BUCKET   = aws_s3_bucket.project.id
      APPS_CONFIG_KEY = "config/dashboard-apps.json"
    }
  }

  tags = { Project = "economic-forecasting" }
}

# --- Lambda Function URL (visible in Lambda console) ---

resource "aws_lambda_function_url" "dashboard" {
  function_name      = aws_lambda_function.dashboard.function_name
  authorization_type = "NONE"
}

# --- API Gateway ---

resource "aws_apigatewayv2_api" "dashboard" {
  name          = "economic-forecasting-dashboard"
  protocol_type = "HTTP"

  tags = { Project = "economic-forecasting" }
}

resource "aws_apigatewayv2_integration" "dashboard" {
  api_id                 = aws_apigatewayv2_api.dashboard.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.dashboard.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.dashboard.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.dashboard.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.dashboard.id
  name        = "$default"
  auto_deploy = true

  tags = { Project = "economic-forecasting" }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dashboard.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.dashboard.execution_arn}/*/*"
}

output "dashboard_url" {
  value = aws_apigatewayv2_api.dashboard.api_endpoint
}

output "ec2_instance_id" {
  value = aws_instance.main.id
}

output "ec2_public_ip" {
  value = aws_instance.main.public_ip
}

# --- Environment Info File ---

resource "local_file" "env_info" {
  filename = "${path.module}/../../notes/env-info.txt"
  content  = <<-EOT
    # Auto-generated by Terraform — do not edit manually
    # Re-generated on every terraform apply

    # Region & Profile
    AWS_REGION=${var.aws_region}
    AWS_PROFILE=${var.aws_profile}

    # VPC
    VPC_ID=${aws_vpc.main.id}
    SUBNET_ID=${aws_subnet.public.id}
    SECURITY_GROUP_ID=${aws_security_group.ec2.id}

    # EC2
    EC2_INSTANCE_ID=${aws_instance.main.id}
    EC2_PUBLIC_IP=${aws_instance.main.public_ip}
    EC2_KEY_NAME=${aws_key_pair.ec2.key_name}
    EC2_KEY_FILE=notes/economic-forecasting-key.pem

    # S3
    S3_BUCKET=${aws_s3_bucket.project.id}

    # Lambda Dashboard
    LAMBDA_FUNCTION=${aws_lambda_function.dashboard.function_name}
    DASHBOARD_URL=${aws_apigatewayv2_api.dashboard.api_endpoint}

    # SSH Command
    SSH_CMD=ssh -i notes/economic-forecasting-key.pem ec2-user@${aws_instance.main.public_ip}
  EOT
}
