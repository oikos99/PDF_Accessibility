"""Start, poll, and retrieve Textract structural-analysis results."""

import time
from typing import Any, Dict, Optional

from .textract_client import TextractClient


TERMINAL_STATUSES = {
    "SUCCEEDED",
    "FAILED",
    "PARTIAL_SUCCESS",
}


class TextractRunner:
    """Coordinate an asynchronous Textract analysis job."""

    def __init__(
        self,
        client: Optional[TextractClient] = None,
    ):
        self._client = client or TextractClient()

    def start(
        self,
        bucket: str,
        key: str,
        client_request_token: str,
    ) -> str:
        """Start the asynchronous Textract job."""
        return self._client.start_document_analysis(
            bucket=bucket,
            key=key,
            client_request_token=client_request_token,
        )

    def wait_for_result(
        self,
        job_id: str,
        max_wait_seconds: int,
        poll_interval_seconds: float,
    ) -> Dict[str, Any]:
        """
        Poll Textract for a bounded amount of time.

        If the result is not ready, return an IN_PROGRESS diagnostic
        payload rather than failing the patron-facing HTML conversion.
        """
        deadline = (
            time.monotonic()
            + max(0, max_wait_seconds)
        )

        last_response: Dict[str, Any] = {
            "JobStatus": "IN_PROGRESS",
            "Blocks": [],
        }

        while True:
            response = (
                self._client.get_document_analysis(
                    job_id=job_id,
                )
            )

            last_response = response

            status = response.get(
                "JobStatus",
                "UNKNOWN",
            )

            print(
                "[INFO] Textract structure job status: "
                f"{status}"
            )

            if status in TERMINAL_STATUSES:
                return self._retrieve_all_pages(
                    job_id=job_id,
                    first_response=response,
                )

            if time.monotonic() >= deadline:
                return {
                    "JobStatus": status,
                    "StatusMessage": (
                        "Textract result was not ready within "
                        "the bounded Lambda polling window."
                    ),
                    "Blocks": [],
                    "DocumentMetadata": (
                        response.get(
                            "DocumentMetadata",
                            {},
                        )
                    ),
                }

            time.sleep(
                max(0.5, poll_interval_seconds)
            )

    def _retrieve_all_pages(
        self,
        job_id: str,
        first_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Retrieve all Textract blocks using NextToken pagination.
        """
        combined_blocks = list(
            first_response.get(
                "Blocks",
                [],
            )
        )

        combined_warnings = list(
            first_response.get(
                "Warnings",
                [],
            )
        )

        next_token = first_response.get(
            "NextToken"
        )

        response_page_count = 1

        while next_token:
            response = (
                self._client.get_document_analysis(
                    job_id=job_id,
                    next_token=next_token,
                )
            )

            combined_blocks.extend(
                response.get(
                    "Blocks",
                    [],
                )
            )

            combined_warnings.extend(
                response.get(
                    "Warnings",
                    [],
                )
            )

            next_token = response.get(
                "NextToken"
            )

            response_page_count += 1

        return {
            "JobStatus": first_response.get(
                "JobStatus",
                "UNKNOWN",
            ),
            "StatusMessage": first_response.get(
                "StatusMessage",
                "",
            ),
            "AnalyzeDocumentModelVersion": (
                first_response.get(
                    "AnalyzeDocumentModelVersion",
                    "",
                )
            ),
            "DocumentMetadata": (
                first_response.get(
                    "DocumentMetadata",
                    {},
                )
            ),
            "Warnings": combined_warnings,
            "Blocks": combined_blocks,
            "ResponsePageCount": (
                response_page_count
            ),
        }