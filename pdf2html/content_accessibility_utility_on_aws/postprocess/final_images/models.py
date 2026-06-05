"""Shared models for final HTML image processing."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict


class ImageClassification(str, Enum):
    """Supported actions for images extracted from scanned documents."""

    DECORATIVE = "DECORATIVE"
    THEMATIC_BREAK = "THEMATIC_BREAK"
    REDUNDANT = "REDUNDANT"
    INFORMATIVE_SIMPLE = "INFORMATIVE_SIMPLE"
    INFORMATIVE_COMPLEX = "INFORMATIVE_COMPLEX"
    FUNCTIONAL = "FUNCTIONAL"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class ImageAnalysis:
    """Normalized response from the multimodal model."""

    classification: ImageClassification
    confidence: float
    alt_text: str = ""
    long_description: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)