"""Small Boto3 wrapper for Amazon Textract."""

from typing import Any, Dict, Optional

import boto3


class TextractClient:
    """Wrap the Textract API calls used by this prototype."""

    def __init__(self, client=None):
        self._client = client or boto3.client("textract")

    def start_document_analysis(
        self,
        bucket: str,
        key: str,
        client_request_token: str,
    ) -> str:
        """
        Start asynchronous Layout and Tables analysis.

        The PDF remains in the existing uploads/ S3 location.
        """
        response = self._client.start_document_analysis(
            DocumentLocation={
                "S3Object": {
                    "Bucket": bucket,
                    "Name": key,
                }
            },
            FeatureTypes=[
                "LAYOUT",
                "TABLES",
            ],
            ClientRequestToken=client_request_token,
        )

        return response["JobId"]

    def get_document_analysis(
        self,
        job_id: str,
        next_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retrieve one paginated Textract response."""
        request: Dict[str, Any] = {
            "JobId": job_id,
            "MaxResults": 1000,
        }

        if next_token:
            request["NextToken"] = next_token

        return self._client.get_document_analysis(
            **request
        )