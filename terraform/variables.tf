variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "aws_profile" {
  description = "AWS CLI profile name"
  type        = string
}

variable "project_bucket_name" {
  description = "S3 bucket name for project artifacts and state"
  type        = string
}

variable "ec2_instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.xlarge"
}

variable "ec2_volume_size" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 50
}

variable "ssh_cidr" {
  description = "CIDR block allowed for SSH and app access"
  type        = string
  default     = "3.83.200.219/32"
}

variable "app_password" {
  description = "Shared password for code-server and JupyterLab"
  type        = string
  sensitive   = true
}
