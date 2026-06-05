"""Preserve extracted image sizes relative to their scanned PDF pages."""

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


def _filename_stem(path_or_uri: str) -> str:
    parsed_path = urlparse(path_or_uri or "").path
    filename = os.path.basename(parsed_path)
    return os.path.splitext(filename)[0].lower()


def _normalized_width_to_percent(
    location: Dict[str, Any],
) -> Optional[float]:
    """
    Convert a normalized BDA bounding-box width into a CSS percentage.

    Bedrock Data Automation normally reports normalized dimensions in the
    range 0 to 1. Return None instead of guessing if metadata is missing.
    """
    bounding_box = (location or {}).get("bounding_box") or {}

    try:
        width = float(bounding_box.get("width"))
    except (TypeError, ValueError):
        return None

    if not 0 < width <= 1:
        logger.warning("Unexpected BDA width value: %s", width)
        return None

    return round(width * 100, 2)


def _get_width_entries(
    result_data: Dict[str, Any],
    page_index: int,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []

    for element in result_data.get("elements", []):
        crop_images = element.get("crop_images") or []
        locations = element.get("locations") or []
        page_indices = element.get("page_indices") or []

        for crop_index, crop_path in enumerate(crop_images):
            location = (
                locations[crop_index]
                if crop_index < len(locations)
                else locations[0]
                if len(locations) == 1
                else {}
            )

            detected_page_index = location.get("page_index")

            if detected_page_index is None:
                detected_page_index = (
                    page_indices[crop_index]
                    if crop_index < len(page_indices)
                    else page_indices[0]
                    if len(page_indices) == 1
                    else None
                )

            if detected_page_index != page_index:
                continue

            width_percent = _normalized_width_to_percent(location)

            if width_percent is None:
                continue

            entries.append(
                {
                    "stem": _filename_stem(crop_path),
                    "width_percent": width_percent,
                    "reading_order": element.get(
                        "reading_order",
                        999999,
                    ),
                }
            )

    return sorted(entries, key=lambda item: item["reading_order"])


def _add_style(img_tag, declaration: str) -> None:
    current = img_tag.get("style", "").strip()

    if current and not current.endswith(";"):
        current += ";"

    img_tag["style"] = f"{current} {declaration}".strip()


def _apply_width(img_tag, width_percent: float) -> None:
    classes = img_tag.get("class", [])

    if isinstance(classes, str):
        classes = classes.split()

    if "document-image" not in classes:
        classes.append("document-image")

    img_tag["class"] = classes
    img_tag["data-bda-relative-width"] = f"{width_percent:.2f}"

    _add_style(
        img_tag,
        f"width: {width_percent:.2f}%; "
        "max-width: 100%; "
        "height: auto;",
    )


def apply_relative_image_widths(
    page_html: str,
    result_data: Dict[str, Any],
    page_index: int,
) -> str:
    """
    Apply BDA-derived relative widths to images on one HTML page.

    Match by crop filename first. Fall back to reading order only when
    the number of unresolved images exactly matches the remaining entries.
    """
    if not page_html:
        return page_html

    soup = BeautifulSoup(page_html, "html.parser")
    images = soup.find_all("img")
    entries = _get_width_entries(result_data, page_index)

    if not images or not entries:
        return str(soup)

    remaining_entries = list(entries)
    unresolved_images = []

    for img_tag in images:
        stem = _filename_stem(img_tag.get("src", ""))

        match = next(
            (
                entry
                for entry in remaining_entries
                if entry["stem"] == stem
            ),
            None,
        )

        if match:
            _apply_width(img_tag, match["width_percent"])
            remaining_entries.remove(match)
        else:
            unresolved_images.append(img_tag)

    if (
        unresolved_images
        and len(unresolved_images) == len(remaining_entries)
    ):
        for img_tag, entry in zip(
            unresolved_images,
            remaining_entries,
        ):
            _apply_width(img_tag, entry["width_percent"])

    elif unresolved_images:
        logger.warning(
            "Could not safely map %s image width(s) on page %s",
            len(unresolved_images),
            page_index + 1,
        )

    return str(soup)