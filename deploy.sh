#!/bin/bash
set -e

AWS_REGION="us-east-1"
REPO_NAME="form-validation-ecr"
IMAGE_TAG="latest"
FUNCTION_NAME="form-validation-lambda"
PYTHON_VERSION="3.10"

export DOCKER_DEFAULT_PLATFORM=linux/amd64

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}"

echo "🛠 Building Docker image for linux/amd64..."
docker buildx build --platform=linux/amd64 -t ${REPO_NAME}:${IMAGE_TAG} --output type=docker .

aws ecr describe-repositories --repository-names "${REPO_NAME}" \
  --region "${AWS_REGION}" > /dev/null 2>&1 || \
  aws ecr create-repository --repository-name "${REPO_NAME}" --region "${AWS_REGION}"

echo "🚀 Pushing image to ECR..."
docker tag ${REPO_NAME}:${IMAGE_TAG} ${ECR_URI}:${IMAGE_TAG}
docker push ${ECR_URI}:${IMAGE_TAG}
