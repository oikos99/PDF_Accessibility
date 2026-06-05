"""Public interface for final HTML image processing."""

from .pipeline import process_final_html_images
from .sizing import apply_relative_image_widths

__all__ = [
    "apply_relative_image_widths",
    "process_final_html_images",
]