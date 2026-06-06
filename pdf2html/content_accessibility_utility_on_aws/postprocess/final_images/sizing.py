"""Preserve extracted image sizes relative to their scanned PDF pages."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
from PIL import Image


logger = logging.getLogger(__name__)


def _filename_stem(path_or_uri: str) -> str:
    """Return the lowercase filename stem from a path, URL, or S3 URI."""
    parsed_path = urlparse(path_or_uri or "").path
    filename = os.path.basename(parsed_path)
    return os.path.splitext(filename)[0].lower()


def _filename(path_or_uri: str) -> str:
    """Return the decoded filename from a path, URL, or S3 URI."""
    parsed_path = urlparse(path_or_uri or "").path
    return os.path.basename(unquote(parsed_path))


def _find_local_file(
    output_dir: str,
    path_or_uri: str,
) -> Optional[Path]:
    """
    Locate an extracted image file within the local BDA output directory.

    Match by filename only. Do not substitute a different image.
    """
    filename = _filename(path_or_uri)

    if not filename:
        return None

    output_path = Path(output_dir)

    for candidate in output_path.rglob(filename):
        if candidate.is_file():
            return candidate

    return None


def _image_width_pixels(image_path: Path) -> Optional[int]:
    """Read an image's pixel width with Pillow."""
    try:
        with Image.open(image_path) as image:
            width, _ = image.size

        return int(width) if width > 0 else None

    except Exception as exc:
        logger.warning(
            "Could not read image dimensions from %s: %s",
            image_path,
            exc,
        )

        return None


def _bounding_box_width_percent(
    location: Dict[str, Any],
) -> Optional[float]:
    """
    Convert a normalized BDA bounding-box width into a percentage.

    BDA normally reports normalized bounding-box values between 0 and 1.
    """
    bounding_box = (location or {}).get("bounding_box") or {}

    try:
        width = float(bounding_box.get("width"))
    except (TypeError, ValueError):
        return None

    if not 0 < width <= 1:
        logger.warning(
            "Unexpected BDA bounding-box width: %s",
            width,
        )

        return None

    return round(width * 100, 2)


def _page_width_pixels(
    result_data: Dict[str, Any],
    page_index: int,
    output_dir: str,
) -> Optional[int]:
    """
    Determine the width of the rectified source-page image.

    Priority:
    1. Use BDA page-width metadata when available.
    2. Use the rectified-image path from BDA metadata when available.
    3. Fall back to the predictable local filename:
       rectified_image_<page_index>.png
    """
    pages = result_data.get("pages") or []

    if page_index >= len(pages):
        logger.warning(
            "Page index %s is outside the BDA pages array",
            page_index,
        )
        return None

    page = pages[page_index]
    asset_metadata = page.get("asset_metadata") or {}

    metadata_width = asset_metadata.get(
        "rectified_image_width_pixels"
    )

    try:
        metadata_width = int(metadata_width)
    except (TypeError, ValueError):
        metadata_width = None

    if metadata_width and metadata_width > 0:
        return metadata_width

    candidate_paths = []

    metadata_rectified_image = asset_metadata.get(
        "rectified_image",
        "",
    )

    if metadata_rectified_image:
        candidate_paths.append(metadata_rectified_image)

    # BDA downloads this file even when the metadata path is absent.
    candidate_paths.append(
        f"rectified_image_{page_index}.png"
    )

    for candidate_path in candidate_paths:
        local_page_image = _find_local_file(
            output_dir=output_dir,
            path_or_uri=candidate_path,
        )

        if not local_page_image:
            continue

        page_width = _image_width_pixels(local_page_image)

        if page_width:
            logger.info(
                "Using rectified page image for relative sizing: %s",
                local_page_image,
            )
            return page_width

    logger.warning(
        "Could not locate rectified page image for page %s; tried: %s",
        page_index + 1,
        candidate_paths,
    )

    return None


def _get_bounding_box_entries(
    result_data: Dict[str, Any],
    page_index: int,
) -> List[Dict[str, Any]]:
    """
    Collect crop filenames and normalized widths from BDA element metadata.
    """
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

            width_percent = _bounding_box_width_percent(location)

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

    return sorted(
        entries,
        key=lambda item: item["reading_order"],
    )


def _crop_width_percent(
    img_src: str,
    output_dir: str,
    page_width_pixels: Optional[int],
) -> Optional[float]:
    """
    Calculate a crop's relative width from its actual pixel dimensions.
    """
    if not page_width_pixels:
        return None

    local_crop = _find_local_file(
        output_dir=output_dir,
        path_or_uri=img_src,
    )

    if not local_crop:
        logger.warning(
            "Could not locate extracted crop for relative sizing: %s",
            img_src,
        )

        return None

    crop_width_pixels = _image_width_pixels(local_crop)

    if not crop_width_pixels:
        return None

    width_percent = (
        float(crop_width_pixels)
        / float(page_width_pixels)
        * 100.0
    )

    if not 0 < width_percent <= 100:
        logger.warning(
            "Unexpected relative width %.2f%% for crop %s",
            width_percent,
            img_src,
        )

        return None

    return round(width_percent, 2)


def _replace_sizing_styles(
    img_tag,
    width_percent: float,
) -> None:
    """
    Set responsive image sizing without duplicating prior sizing declarations.
    """
    existing_parts = [
        part.strip()
        for part in img_tag.get("style", "").split(";")
        if part.strip()
    ]

    preserved_parts = [
        part
        for part in existing_parts
        if not part.lower().startswith(
            (
                "width:",
                "max-width:",
                "height:",
            )
        )
    ]

    preserved_parts.extend(
        [
            f"width: {width_percent:.2f}%",
            "max-width: 100%",
            "height: auto",
        ]
    )

    img_tag["style"] = "; ".join(preserved_parts) + ";"


def _apply_width(
    img_tag,
    width_percent: float,
    source: str,
) -> None:
    """Attach a percentage width and debugging metadata to an image."""
    classes = img_tag.get("class", [])

    if isinstance(classes, str):
        classes = classes.split()

    if "document-image" not in classes:
        classes.append("document-image")

    img_tag["class"] = classes
    img_tag["data-bda-relative-width"] = f"{width_percent:.2f}"
    img_tag["data-image-width-source"] = source

    _replace_sizing_styles(
        img_tag=img_tag,
        width_percent=width_percent,
    )


def apply_relative_image_widths(
    page_html: str,
    result_data: Dict[str, Any],
    page_index: int,
    output_dir: str,
) -> str:
    """
    Apply responsive percentage widths to images on one HTML page.

    Priority:
    1. Match the HTML image to a BDA element bounding box.
    2. Fall back to crop-pixel width divided by source-page pixel width.
    3. Use BDA reading order only when the remaining counts match exactly.
    """
    if not page_html:
        return page_html

    soup = BeautifulSoup(page_html, "html.parser")
    images = soup.find_all("img")

    if not images:
        return str(soup)

    bounding_box_entries = _get_bounding_box_entries(
        result_data=result_data,
        page_index=page_index,
    )

    page_width = _page_width_pixels(
        result_data=result_data,
        page_index=page_index,
        output_dir=output_dir,
    )

    remaining_entries = list(bounding_box_entries)
    unresolved_images = []

    for img_tag in images:
        img_src = img_tag.get("src", "")
        img_stem = _filename_stem(img_src)

        matching_entry = next(
            (
                entry
                for entry in remaining_entries
                if entry["stem"] == img_stem
            ),
            None,
        )

        if matching_entry:
            _apply_width(
                img_tag=img_tag,
                width_percent=matching_entry["width_percent"],
                source="bda-bounding-box",
            )

            remaining_entries.remove(matching_entry)
            continue

        fallback_percent = _crop_width_percent(
            img_src=img_src,
            output_dir=output_dir,
            page_width_pixels=page_width,
        )

        if fallback_percent is not None:
            _apply_width(
                img_tag=img_tag,
                width_percent=fallback_percent,
                source="crop-pixels-over-page-pixels",
            )

            continue

        unresolved_images.append(img_tag)

    # Final conservative fallback:
    # use BDA reading order only when the remaining counts align exactly.
    if (
        unresolved_images
        and len(unresolved_images) == len(remaining_entries)
    ):
        for img_tag, entry in zip(
            unresolved_images,
            remaining_entries,
        ):
            _apply_width(
                img_tag=img_tag,
                width_percent=entry["width_percent"],
                source="bda-reading-order",
            )

        unresolved_images = []

    if unresolved_images:
        logger.warning(
            "Could not determine relative width for %s image(s) on page %s",
            len(unresolved_images),
            page_index + 1,
        )

    return str(soup)