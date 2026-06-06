"""Normalize raw Textract blocks into readable structural evidence."""

from collections import Counter
from typing import Any, Dict, List, Set


LAYOUT_BLOCK_TYPES = {
    "LAYOUT_TITLE",
    "LAYOUT_HEADER",
    "LAYOUT_FOOTER",
    "LAYOUT_SECTION_HEADER",
    "LAYOUT_PAGE_NUMBER",
    "LAYOUT_LIST",
    "LAYOUT_FIGURE",
    "LAYOUT_TABLE",
    "LAYOUT_KEY_VALUE",
    "LAYOUT_TEXT",
}


def _relationship_ids(
    block: Dict[str, Any],
) -> List[str]:
    """Return child IDs referenced by one Textract block."""
    child_ids: List[str] = []

    for relationship in block.get(
        "Relationships",
        [],
    ):
        if relationship.get("Type") == "CHILD":
            child_ids.extend(
                relationship.get(
                    "Ids",
                    [],
                )
            )

    return child_ids


def _collect_text(
    block: Dict[str, Any],
    blocks_by_id: Dict[str, Dict[str, Any]],
    visited_ids: Set[str],
) -> str:
    """
    Resolve visible text recursively through CHILD relationships.
    """
    block_id = block.get("Id")

    if block_id and block_id in visited_ids:
        return ""

    if block_id:
        visited_ids.add(block_id)

    direct_text = (
        block.get("Text")
        or ""
    ).strip()

    child_text_parts: List[str] = []

    for child_id in _relationship_ids(block):
        child = blocks_by_id.get(child_id)

        if not child:
            continue

        child_text = _collect_text(
            block=child,
            blocks_by_id=blocks_by_id,
            visited_ids=visited_ids,
        )

        if child_text:
            child_text_parts.append(
                child_text
            )

    if child_text_parts:
        return " ".join(
            child_text_parts
        ).strip()

    return direct_text


def _bounding_box(
    block: Dict[str, Any],
) -> Dict[str, float]:
    """Return a normalized bounding box."""
    bounding_box = (
        block.get("Geometry", {})
        .get("BoundingBox", {})
    )

    return {
        "left": float(
            bounding_box.get(
                "Left",
                0.0,
            )
        ),
        "top": float(
            bounding_box.get(
                "Top",
                0.0,
            )
        ),
        "width": float(
            bounding_box.get(
                "Width",
                0.0,
            )
        ),
        "height": float(
            bounding_box.get(
                "Height",
                0.0,
            )
        ),
    }


def normalize_textract_result(
    raw_result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Convert raw Textract blocks into readable structural evidence.

    This function does not change HTML.
    """
    blocks = raw_result.get(
        "Blocks",
        [],
    )

    blocks_by_id = {
        block["Id"]: block
        for block in blocks
        if block.get("Id")
    }

    block_counts = Counter(
        block.get(
            "BlockType",
            "UNKNOWN",
        )
        for block in blocks
    )

    layout_elements = []

    for reading_order, block in enumerate(
        blocks,
        start=1,
    ):
        block_type = block.get(
            "BlockType",
            "UNKNOWN",
        )

        if block_type not in LAYOUT_BLOCK_TYPES:
            continue

        layout_elements.append(
            {
                "reading_order": (
                    reading_order
                ),
                "block_id": block.get(
                    "Id"
                ),
                "block_type": (
                    block_type
                ),
                "page": block.get(
                    "Page"
                ),
                "confidence": float(
                    block.get(
                        "Confidence",
                        0.0,
                    )
                ),
                "text": _collect_text(
                    block=block,
                    blocks_by_id=blocks_by_id,
                    visited_ids=set(),
                ),
                "bounding_box": (
                    _bounding_box(
                        block
                    )
                ),
                "child_ids": (
                    _relationship_ids(
                        block
                    )
                ),
            }
        )

    layout_counts = Counter(
        item["block_type"]
        for item in layout_elements
    )

    return {
        "job_status": raw_result.get(
            "JobStatus",
            "UNKNOWN",
        ),
        "status_message": raw_result.get(
            "StatusMessage",
            "",
        ),
        "analyze_document_model_version": (
            raw_result.get(
                "AnalyzeDocumentModelVersion",
                "",
            )
        ),
        "document_pages": (
            raw_result.get(
                "DocumentMetadata",
                {},
            ).get(
                "Pages",
                0,
            )
        ),
        "response_page_count": (
            raw_result.get(
                "ResponsePageCount",
                0,
            )
        ),
        "warnings": raw_result.get(
            "Warnings",
            [],
        ),
        "summary": {
            "total_blocks": len(
                blocks
            ),
            "block_counts": dict(
                sorted(
                    block_counts.items()
                )
            ),
            "layout_element_count": len(
                layout_elements
            ),
            "layout_counts": dict(
                sorted(
                    layout_counts.items()
                )
            ),
        },
        "layout_elements": (
            layout_elements
        ),
    }