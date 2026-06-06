"""Write Textract diagnostics to the existing S3 bucket."""

import json
from typing import Any


def upload_json(
    s3_client,
    bucket: str,
    key: str,
    payload: Any,
) -> str:
    """Upload readable UTF-8 JSON to S3."""
    body = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=(
            "application/json; charset=utf-8"
        ),
    )

    print(
        "[INFO] Uploaded structure diagnostic: "
        f"s3://{bucket}/{key}"
    )

    return key