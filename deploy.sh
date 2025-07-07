#!/bin/bash
set -e

if ! command -v jq &> /dev/null; then
    echo "❌ jq is required. Install with: brew install jq"
    exit 1
fi

if ! command -v aws &> /dev/null; then
    echo "❌ AWS CLI is required but not installed."
    exit 1
fi

AWS_REGION="us-east-1"
REPO_NAME="form-validation-ecr-final"
IMAGE_TAG="latest"
FUNCTION_NAME="form-validator-api-final-check"
IAM_ROLE_ARN="arn:aws:iam::855341045026:role/service-role/form-validation-lambda-func-role-3jewuol0"
TIMEOUT=10
MEMORY_SIZE=128

export DOCKER_DEFAULT_PLATFORM=linux/amd64

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}"

echo "Building Docker image..."
docker buildx build --platform=linux/amd64 -t ${REPO_NAME}:${IMAGE_TAG} --output type=docker .

aws ecr describe-repositories --repository-names "${REPO_NAME}" --region "${AWS_REGION}" > /dev/null 2>&1 || \
  aws ecr create-repository --repository-name "${REPO_NAME}" --region "${AWS_REGION}"

echo "Pushing image to ECR..."
docker tag ${REPO_NAME}:${IMAGE_TAG} ${ECR_URI}:${IMAGE_TAG}
docker push ${ECR_URI}:${IMAGE_TAG}

echo "Getting image digest..."
IMAGE_DETAILS=$(aws ecr describe-images --repository-name "${REPO_NAME}" --region "${AWS_REGION}" \
  --query 'imageDetails[?imageSizeInBytes > `1000`] | sort_by(@, &imagePushedAt) | [-1]' --output json)

if [ "$IMAGE_DETAILS" = "null" ] || [ -z "$IMAGE_DETAILS" ]; then
    echo "❌ No valid image found in ECR repository."
    exit 1
fi

IMAGE_DIGEST=$(echo $IMAGE_DETAILS | jq -r '.imageDigest')
ACTUAL_IMAGE_URI="${ECR_URI}@${IMAGE_DIGEST}"

echo "Creating Lambda function..."
aws lambda create-function \
  --function-name "$FUNCTION_NAME" \
  --package-type Image \
  --code ImageUri="$ACTUAL_IMAGE_URI" \
  --role "$IAM_ROLE_ARN" \
  --timeout "$TIMEOUT" \
  --memory-size "$MEMORY_SIZE" \
  --region "$AWS_REGION"

echo "Creating Function URL..."
aws lambda create-function-url-config \
  --function-name "$FUNCTION_NAME" \
  --auth-type NONE \
  --region "$AWS_REGION"

aws lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --statement-id "FunctionURLAllowPublicAccess" \
  --action lambda:InvokeFunctionUrl \
  --principal "*" \
  --function-url-auth-type NONE \
  --region "$AWS_REGION"

FUNCTION_URL=$(aws lambda get-function-url-config \
  --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION" \
  --query 'FunctionUrl' \
  --output text)

echo ""
echo "Deployment completed successfully!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Function URL: ${FUNCTION_URL}"
echo " Function Name: ${FUNCTION_NAME}"
echo " Region: ${AWS_REGION}"
echo " Memory: ${MEMORY_SIZE} MB | Timeout: ${TIMEOUT}s"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Test your API: curl ${FUNCTION_URL}"
