"""Optional Textract-based structural diagnostics."""

from .pipeline import (
    finish_structure_analysis,
    start_structure_analysis,
)

__all__ = [
    "finish_structure_analysis",
    "start_structure_analysis",
]