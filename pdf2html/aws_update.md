After redeploying the PDF-to-HTML backend, the new container image may be pushed to ECR with the latest tag, but the Lambda function may still be pinned to the previous image digest. Run the following commands to force Lambda to use the current latest image from your AWS account and Region.

```
AWS_REGION="${AWS_REGION:-us-west-2}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO_NAME="pdf2html-lambda"
LAMBDA_FUNCTION_NAME="Pdf2HtmlPipeline"
IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:latest"
aws lambda update-function-code --function-name "${LAMBDA_FUNCTION_NAME}" --region "${AWS_REGION}" --image-uri "${IMAGE_URI}"
aws lambda wait function-updated --function-name "${LAMBDA_FUNCTION_NAME}" --region "${AWS_REGION}"
aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" --region "${AWS_REGION}" --query '{LastModified:Configuration.LastModified,ResolvedImageUri:Code.ResolvedImageUri}' --output table
```
