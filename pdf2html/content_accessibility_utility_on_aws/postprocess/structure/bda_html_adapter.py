"""Extract a normalized snapshot of the baseline BDA-generated HTML."""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Union
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup


PathLike = Union[str, Path]


def _clean_text(text: str) -> str:
    """Collapse repeated whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def _shorten(text: str, max_length: int = 240) -> str:
    """Return a compact diagnostic text sample."""
    cleaned = _clean_text(text)

    if len(cleaned) <= max_length:
        return cleaned

    return cleaned[: max_length - 3].rstrip() + "..."


def _page_number_from_id(
    page_id: str,
    fallback_number: int,
) -> int:
    """Convert page-0 into the one-based PDF page number 1."""
    match = re.fullmatch(r"page-(\d+)", page_id or "")

    if not match:
        return fallback_number

    return int(match.group(1)) + 1


def _src_label(src: str) -> str:
    """Return a readable image-source label without storing base64 data."""
    src = src or ""

    if src.startswith("data:"):
        return "[embedded-data-uri]"

    parsed_path = urlparse(src).path
    filename = os.path.basename(unquote(parsed_path))

    return filename or src[:160]


def _extract_headings(page) -> List[Dict[str, Any]]:
    """Extract existing BDA HTML headings without changing them."""
    headings: List[Dict[str, Any]] = []

    for order, heading in enumerate(
        page.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6"]
        ),
        start=1,
    ):
        text = _clean_text(
            heading.get_text(" ", strip=True)
        )

        if not text:
            continue

        headings.append(
            {
                "order": order,
                "tag": heading.name,
                "level": int(heading.name[1]),
                "text": text,
            }
        )

    return headings


def _extract_tables(page) -> List[Dict[str, Any]]:
    """Extract compact diagnostics for existing BDA HTML tables."""
    tables: List[Dict[str, Any]] = []

    for order, table in enumerate(
        page.find_all("table"),
        start=1,
    ):
        rows = table.find_all("tr")
        cells = table.find_all(["th", "td"])
        headers = table.find_all("th")
        caption = table.find("caption")

        tables.append(
            {
                "order": order,
                "row_count": len(rows),
                "cell_count": len(cells),
                "header_cell_count": len(headers),
                "caption": (
                    _clean_text(
                        caption.get_text(" ", strip=True)
                    )
                    if caption
                    else ""
                ),
                "text_sample": _shorten(
                    table.get_text(" ", strip=True)
                ),
            }
        )

    return tables


def _extract_images(page) -> List[Dict[str, Any]]:
    """Extract compact diagnostics for existing BDA HTML images."""
    images: List[Dict[str, Any]] = []

    for order, image in enumerate(
        page.find_all("img"),
        start=1,
    ):
        images.append(
            {
                "order": order,
                "src": _src_label(
                    image.get("src", "")
                ),
                "alt": _clean_text(
                    image.get("alt", "")
                ),
                "classification": image.get(
                    "data-image-classification",
                    "",
                ),
            }
        )

    return images


def extract_bda_html_structure(
    html_path: PathLike,
) -> Dict[str, Any]:
    """
    Normalize the baseline BDA-generated HTML for report-only comparison.

    This function reads HTML but never mutates it.
    """
    html_path = Path(html_path)

    soup = BeautifulSoup(
        html_path.read_text(encoding="utf-8"),
        "html.parser",
    )

    page_nodes = soup.select('div[id^="page-"]')

    if not page_nodes:
        body = soup.find("body")

        page_nodes = [body or soup]

    pages: List[Dict[str, Any]] = []

    total_headings = 0
    total_tables = 0
    total_images = 0

    for fallback_number, page in enumerate(
        page_nodes,
        start=1,
    ):
        page_id = page.get(
            "id",
            f"page-{fallback_number - 1}",
        )

        headings = _extract_headings(page)
        tables = _extract_tables(page)
        images = _extract_images(page)

        total_headings += len(headings)
        total_tables += len(tables)
        total_images += len(images)

        pages.append(
            {
                "page": _page_number_from_id(
                    page_id=page_id,
                    fallback_number=fallback_number,
                ),
                "page_id": page_id,
                "headings": headings,
                "tables": tables,
                "images": images,
                "text_sample": _shorten(
                    page.get_text(" ", strip=True),
                    max_length=400,
                ),
            }
        )

    navigation_links = []

    for link in soup.select(
        'nav a[href^="#page-"]'
    ):
        navigation_links.append(
            {
                "href": link.get("href", ""),
                "text": _clean_text(
                    link.get_text(" ", strip=True)
                ),
            }
        )

    return {
        "source": "bda-generated-html",
        "html_filename": html_path.name,
        "summary": {
            "page_count": len(pages),
            "heading_count": total_headings,
            "table_count": total_tables,
            "image_count": total_images,
            "navigation_link_count": len(
                navigation_links
            ),
        },
        "navigation_links": navigation_links,
        "pages": pages,
    }