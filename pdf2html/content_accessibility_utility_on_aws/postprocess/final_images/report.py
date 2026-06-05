"""Write final-image QA reports."""

import json
from pathlib import Path
from typing import Any, Dict, List


def write_image_report(
    html_path: str,
    model_id: str,
    items: List[Dict[str, Any]],
) -> str:
    html_path_object = Path(html_path)

    report_path = html_path_object.with_name(
        f"{html_path_object.stem}.image-report.json"
    )

    report = {
        "model_id": model_id,
        "image_count": len(items),
        "review_required_count": sum(
            1
            for item in items
            if item.get("review_required")
        ),
        "images": items,
    }

    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return str(report_path)