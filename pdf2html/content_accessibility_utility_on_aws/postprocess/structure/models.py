"""Data models for optional structural analysis."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TextractJob:
    """Track one optional Textract structural-analysis job."""

    enabled: bool
    bucket: str
    key: str
    filename_base: str
    object_etag: str = ""

    job_id: Optional[str] = None
    client_request_token: Optional[str] = None

    status: str = "DISABLED"
    error: Optional[str] = None

    started_at: str = field(
        default_factory=_utc_now_iso
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the job for diagnostic reports."""
        return asdict(self)