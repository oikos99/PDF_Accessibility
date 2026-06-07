"""Orchestrate optional report-only Textract structural analysis."""

import hashlib
import traceback
from typing import Any, Dict, Optional

import boto3

from ..config import PostprocessSettings
from .bda_html_adapter import (
    extract_bda_html_structure,
)
from .comparison import (
    build_structure_comparison,
)
from .models import TextractJob
from .report import upload_json
from .textract_parser import (
    normalize_textract_result,
)
from .textract_runner import TextractRunner


s3 = boto3.client("s3")


def _client_request_token(
    bucket: str,
    key: str,
    object_etag: str,
) -> str:
    """
    Build a deterministic Textract idempotency token.

    Including the ETag avoids accidentally reusing an older job when a
    different PDF is uploaded under the same S3 key.
    """
    raw_value = (
        f"{bucket}|{key}|{object_etag}"
    )

    return hashlib.sha256(
        raw_value.encode("utf-8")
    ).hexdigest()


def _diagnostics_prefix(
    settings: PostprocessSettings,
    filename_base: str,
) -> str:
    """Return the private diagnostics prefix for one PDF."""
    return (
        f"{settings.structure_diagnostics_prefix}/"
        f"{filename_base}"
    )


def _bounded_wait_seconds(
    settings: PostprocessSettings,
    lambda_context,
) -> int:
    """
    Keep time available for the working patron-facing HTML workflow.

    The Textract layer must never consume the Lambda invocation's final
    seconds and prevent the HTML file from being uploaded.
    """
    configured_wait = max(
        0,
        settings.textract_max_wait_seconds,
    )

    if (
        lambda_context is None
        or not hasattr(
            lambda_context,
            "get_remaining_time_in_millis",
        )
    ):
        return configured_wait

    remaining_seconds = max(
        0,
        int(
            lambda_context
            .get_remaining_time_in_millis()
            / 1000
        ),
    )

    safe_available_seconds = max(
        0,
        (
            remaining_seconds
            - settings.textract_lambda_reserve_seconds
        ),
    )

    return min(
        configured_wait,
        safe_available_seconds,
    )


def start_structure_analysis(
    bucket: str,
    key: str,
    filename_base: str,
    object_etag: str = "",
) -> TextractJob:
    """
    Start Textract early so it can run while the existing BDA conversion
    continues.

    A failure here does not stop the patron-facing HTML workflow.
    """
    settings = (
        PostprocessSettings.from_env()
    )

    job = TextractJob(
        enabled=(
            settings.textract_structure_enabled
        ),
        bucket=bucket,
        key=key,
        filename_base=filename_base,
        object_etag=object_etag,
    )

    if not job.enabled:
        print(
            "[INFO] Textract structure layer "
            "is disabled"
        )

        return job

    try:
        token = _client_request_token(
            bucket=bucket,
            key=key,
            object_etag=object_etag,
        )

        runner = TextractRunner()

        job_id = runner.start(
            bucket=bucket,
            key=key,
            client_request_token=token,
        )

        job.client_request_token = token
        job.job_id = job_id
        job.status = "STARTED"

        print(
            "[INFO] Started Textract structure "
            f"analysis: job_id={job_id}"
        )

    except Exception as exc:
        job.status = "START_FAILED"
        job.error = str(exc)

        print(
            "[WARN] Textract structure analysis "
            f"could not start: {exc}"
        )

        print(
            traceback.format_exc()
        )

    return job


def finish_structure_analysis(
    job: Optional[TextractJob],
    html_path=None,
    lambda_context=None,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve Textract diagnostics and upload report-only comparisons.

    This function never modifies patron-facing HTML.
    """
    if job is None or not job.enabled:
        return None

    settings = (
        PostprocessSettings.from_env()
    )

    wait_seconds = _bounded_wait_seconds(
        settings=settings,
        lambda_context=lambda_context,
    )

    raw_result: Dict[str, Any] = {
        "JobStatus": job.status,
        "StatusMessage": (
            job.error
            or ""
        ),
        "Blocks": [],
    }

    try:
        if job.job_id and wait_seconds > 0:
            print(
                "[INFO] Waiting up to "
                f"{wait_seconds} seconds for "
                "Textract structure diagnostics"
            )

            raw_result = (
                TextractRunner()
                .wait_for_result(
                    job_id=job.job_id,
                    max_wait_seconds=(
                        wait_seconds
                    ),
                    poll_interval_seconds=(
                        settings
                        .textract_poll_interval_seconds
                    ),
                )
            )

        elif job.job_id:
            raw_result = {
                "JobStatus": "IN_PROGRESS",
                "StatusMessage": (
                    "No safe Lambda polling time "
                    "remained after the BDA pass."
                ),
                "Blocks": [],
            }

        normalized_result = (
            normalize_textract_result(
                raw_result
            )
        )

    except Exception as exc:
        print(
            "[WARN] Textract diagnostics could "
            f"not be retrieved: {exc}"
        )

        print(
            traceback.format_exc()
        )

        raw_result = {
            "JobStatus": "RETRIEVAL_FAILED",
            "StatusMessage": str(exc),
            "Blocks": [],
        }

        normalized_result = (
            normalize_textract_result(
                raw_result
            )
        )

    bda_html_structure = None
    comparison_report = None
    comparison_error = None

    textract_status = normalized_result.get(
        "job_status",
        "UNKNOWN",
    )

    if (
        html_path
        and textract_status
        in {
            "SUCCEEDED",
            "PARTIAL_SUCCESS",
        }
    ):
        try:
            bda_html_structure = (
                extract_bda_html_structure(
                    html_path=html_path,
                )
            )

            comparison_report = (
                build_structure_comparison(
                    bda_html_structure=(
                        bda_html_structure
                    ),
                    textract_normalized=(
                        normalized_result
                    ),
                )
            )

            print(
                "[INFO] Built report-only "
                "BDA-vs-Textract comparison"
            )

        except Exception as exc:
            comparison_error = str(exc)

            print(
                "[WARN] BDA-vs-Textract comparison "
                f"could not be built: {exc}"
            )

            print(
                traceback.format_exc()
            )

    elif not html_path:
        comparison_error = (
            "No baseline HTML path was supplied."
        )

        print(
            "[WARN] Skipping BDA-vs-Textract "
            "comparison because no baseline HTML "
            "path was supplied"
        )

    else:
        comparison_error = (
            "Textract result status was "
            f"{textract_status}; comparison skipped."
        )

        print(
            "[WARN] Skipping BDA-vs-Textract "
            "comparison because Textract status is "
            f"{textract_status}"
        )

    diagnostics_prefix = (
        _diagnostics_prefix(
            settings=settings,
            filename_base=(
                job.filename_base
            ),
        )
    )

    raw_key = (
        f"{diagnostics_prefix}/"
        "textract-raw.json"
    )

    normalized_key = (
        f"{diagnostics_prefix}/"
        "textract-normalized.json"
    )

    report_key = (
        f"{diagnostics_prefix}/"
        "structure-report.json"
    )

    bda_html_key = (
        f"{diagnostics_prefix}/"
        "bda-html-normalized.json"
    )

    comparison_key = (
        f"{diagnostics_prefix}/"
        "structure-comparison-report.json"
    )

    diagnostic_objects = {
        "raw": raw_key,
        "normalized": normalized_key,
        "report": report_key,
    }

    if bda_html_structure is not None:
        diagnostic_objects[
            "bda_html_normalized"
        ] = bda_html_key

    if comparison_report is not None:
        diagnostic_objects[
            "comparison"
        ] = comparison_key

    structure_report = {
        "layer": (
            "bda-textract-structure-comparison-report-only"
        ),
        "html_changes_applied": False,
        "nova_structure_adjudication_attempted": (
            False
        ),
        "settings": {
            "textract_structure_enabled": (
                settings
                .textract_structure_enabled
            ),
            "structure_apply_html_changes_enabled": (
                settings
                .structure_apply_html_changes_enabled
            ),
            "nova_ai_enabled": (
                settings.nova_ai_enabled
            ),
            "nova_structure_adjudication_enabled": (
                settings
                .nova_structure_adjudication_enabled
            ),
            "bounded_wait_seconds": (
                wait_seconds
            ),
        },
        "job": job.to_dict(),
        "result_status": raw_result.get(
            "JobStatus",
            "UNKNOWN",
        ),
        "result_status_message": (
            raw_result.get(
                "StatusMessage",
                "",
            )
        ),
        "summary": normalized_result.get(
            "summary",
            {},
        ),
        "comparison_generated": (
            comparison_report is not None
        ),
        "comparison_error": comparison_error,
        "comparison_summary": (
            comparison_report.get(
                "summary",
                {},
            )
            if comparison_report
            else {}
        ),
        "diagnostic_objects": (
            diagnostic_objects
        ),
    }

    try:
        upload_json(
            s3_client=s3,
            bucket=job.bucket,
            key=raw_key,
            payload=raw_result,
        )

        upload_json(
            s3_client=s3,
            bucket=job.bucket,
            key=normalized_key,
            payload=normalized_result,
        )

        if bda_html_structure is not None:
            upload_json(
                s3_client=s3,
                bucket=job.bucket,
                key=bda_html_key,
                payload=bda_html_structure,
            )

        if comparison_report is not None:
            upload_json(
                s3_client=s3,
                bucket=job.bucket,
                key=comparison_key,
                payload=comparison_report,
            )

        upload_json(
            s3_client=s3,
            bucket=job.bucket,
            key=report_key,
            payload=structure_report,
        )

    except Exception as exc:
        print(
            "[WARN] Textract diagnostic upload "
            f"failed: {exc}"
        )

        print(
            traceback.format_exc()
        )

    # Keep the normalized Textract result in memory for the optional
    # patron-facing HTML cleanup pass. This private value is added only
    # after diagnostic uploads, so it is not written into structure-report.json.
    structure_report["_textract_normalized"] = (
        normalized_result
    )


    return structure_report