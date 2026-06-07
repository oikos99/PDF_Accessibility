"""Compare baseline BDA HTML structure against normalized Textract evidence."""

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional


HEADING_SIGNAL_TYPES = {
    "LAYOUT_HEADER",
    "LAYOUT_SECTION_HEADER",
    "LAYOUT_TITLE",
}

MATCH_THRESHOLD = 0.82


def _normalise_for_match(text: str) -> str:
    """Normalize OCR text for conservative fuzzy comparison."""
    text = (text or "").casefold()
    text = re.sub(r"[\W_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(
    left_text: str,
    right_text: str,
) -> float:
    """Return a fuzzy text-similarity score between zero and one."""
    left = _normalise_for_match(left_text)
    right = _normalise_for_match(right_text)

    if not left or not right:
        return 0.0

    if left == right:
        return 1.0

    return round(
        SequenceMatcher(
            None,
            left,
            right,
        ).ratio(),
        4,
    )


def _best_layout_match(
    heading_text: str,
    layout_elements: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Find the strongest Textract structural signal for one HTML heading."""
    candidates = [
        element
        for element in layout_elements
        if element.get("block_type")
        in HEADING_SIGNAL_TYPES
    ]

    if not candidates:
        return None

    scored_candidates = []

    for candidate in candidates:
        scored_candidates.append(
            {
                "similarity": _similarity(
                    heading_text,
                    candidate.get(
                        "text",
                        "",
                    ),
                ),
                "block_type": candidate.get(
                    "block_type",
                    "",
                ),
                "confidence": candidate.get(
                    "confidence",
                    0.0,
                ),
                "text": candidate.get(
                    "text",
                    "",
                ),
                "bounding_box": candidate.get(
                    "bounding_box",
                    {},
                ),
            }
        )

    return max(
        scored_candidates,
        key=lambda item: item["similarity"],
    )


def _heading_assessment(
    best_match: Optional[Dict[str, Any]],
) -> str:
    """Classify one comparison result without applying HTML changes."""
    if not best_match:
        return "review-heading-role"

    similarity = best_match.get(
        "similarity",
        0.0,
    )

    block_type = best_match.get(
        "block_type",
        "",
    )

    if similarity < MATCH_THRESHOLD:
        return "review-heading-role"

    if block_type == "LAYOUT_HEADER":
        return "review-remove-running-header"

    if block_type in {
        "LAYOUT_TITLE",
        "LAYOUT_SECTION_HEADER",
    }:
        return "supported-heading-candidate"

    return "review-heading-role"


def _repeated_heading_groups(
    bda_html_structure: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Group similar headings repeated across distinct pages.

    These are review candidates only. Repetition is useful evidence for
    running headers, but it is not enough to remove content automatically.
    """
    groups: List[Dict[str, Any]] = []

    for page in bda_html_structure.get(
        "pages",
        [],
    ):
        page_number = page.get("page")

        for heading in page.get(
            "headings",
            [],
        ):
            text = heading.get(
                "text",
                "",
            )

            matching_group = None

            for group in groups:
                if (
                    _similarity(
                        text,
                        group["representative_text"],
                    )
                    >= MATCH_THRESHOLD
                ):
                    matching_group = group
                    break

            occurrence = {
                "page": page_number,
                "tag": heading.get("tag"),
                "text": text,
            }

            if matching_group is None:
                groups.append(
                    {
                        "representative_text": text,
                        "occurrences": [
                            occurrence
                        ],
                    }
                )

            else:
                matching_group[
                    "occurrences"
                ].append(occurrence)

    repeated_groups = []

    for group in groups:
        distinct_pages = sorted(
            {
                occurrence["page"]
                for occurrence in group[
                    "occurrences"
                ]
            }
        )

        if len(distinct_pages) < 2:
            continue

        repeated_groups.append(
            {
                "representative_text": group[
                    "representative_text"
                ],
                "distinct_pages": distinct_pages,
                "occurrence_count": len(
                    group["occurrences"]
                ),
                "occurrences": group[
                    "occurrences"
                ],
                "assessment": (
                    "review-possible-running-header"
                ),
            }
        )

    return repeated_groups


def build_structure_comparison(
    bda_html_structure: Dict[str, Any],
    textract_normalized: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a report-only BDA-vs-Textract structural comparison.

    This function does not mutate HTML.
    """
    textract_layout_by_page: Dict[
        int,
        List[Dict[str, Any]],
    ] = {}

    for element in textract_normalized.get(
        "layout_elements",
        [],
    ):
        page_number = element.get("page")

        if not page_number:
            continue

        textract_layout_by_page.setdefault(
            int(page_number),
            [],
        ).append(element)

    textract_tables_by_page: Dict[
        int,
        List[Dict[str, Any]],
    ] = {}

    for table in textract_normalized.get(
        "tables",
        [],
    ):
        page_number = table.get("page")

        if not page_number:
            continue

        textract_tables_by_page.setdefault(
            int(page_number),
            [],
        ).append(table)

    page_reports = []
    review_candidates = []

    for page in bda_html_structure.get(
        "pages",
        [],
    ):
        page_number = int(
            page.get("page", 0)
        )

        layout_elements = (
            textract_layout_by_page.get(
                page_number,
                [],
            )
        )

        heading_comparisons = []

        for heading in page.get(
            "headings",
            [],
        ):
            best_match = _best_layout_match(
                heading_text=heading.get(
                    "text",
                    "",
                ),
                layout_elements=layout_elements,
            )

            assessment = _heading_assessment(
                best_match
            )

            comparison = {
                "bda_heading": heading,
                "best_textract_match": best_match,
                "assessment": assessment,
            }

            heading_comparisons.append(
                comparison
            )

            if assessment.startswith("review-"):
                review_candidates.append(
                    {
                        "page": page_number,
                        "category": assessment,
                        "details": comparison,
                    }
                )

        textract_page_numbers = [
            element
            for element in layout_elements
            if element.get("block_type")
            == "LAYOUT_PAGE_NUMBER"
        ]

        textract_figures = [
            element
            for element in layout_elements
            if element.get("block_type")
            == "LAYOUT_FIGURE"
        ]

        textract_headers = [
            element
            for element in layout_elements
            if element.get("block_type")
            == "LAYOUT_HEADER"
        ]

        textract_titles = [
            element
            for element in layout_elements
            if element.get("block_type")
            in {
                "LAYOUT_TITLE",
                "LAYOUT_SECTION_HEADER",
            }
        ]

        textract_tables = (
            textract_tables_by_page.get(
                page_number,
                [],
            )
        )

        bda_tables = page.get(
            "tables",
            [],
        )

        bda_images = page.get(
            "images",
            [],
        )

        if len(bda_tables) != len(textract_tables):
            review_candidates.append(
                {
                    "page": page_number,
                    "category": (
                        "review-table-count-disagreement"
                    ),
                    "details": {
                        "bda_table_count": len(
                            bda_tables
                        ),
                        "textract_table_count": len(
                            textract_tables
                        ),
                    },
                }
            )

        if len(bda_images) != len(textract_figures):
            review_candidates.append(
                {
                    "page": page_number,
                    "category": (
                        "review-image-figure-count-disagreement"
                    ),
                    "details": {
                        "bda_image_count": len(
                            bda_images
                        ),
                        "textract_figure_count": len(
                            textract_figures
                        ),
                    },
                }
            )

        page_reports.append(
            {
                "page": page_number,
                "bda": {
                    "headings": page.get(
                        "headings",
                        [],
                    ),
                    "tables": bda_tables,
                    "images": bda_images,
                    "text_sample": page.get(
                        "text_sample",
                        "",
                    ),
                },
                "textract": {
                    "headers": textract_headers,
                    "titles_and_section_headers": (
                        textract_titles
                    ),
                    "page_numbers": (
                        textract_page_numbers
                    ),
                    "figures": textract_figures,
                    "tables": textract_tables,
                },
                "heading_comparisons": (
                    heading_comparisons
                ),
            }
        )

    repeated_heading_groups = (
        _repeated_heading_groups(
            bda_html_structure
        )
    )

    for group in repeated_heading_groups:
        review_candidates.append(
            {
                "page": None,
                "category": group[
                    "assessment"
                ],
                "details": group,
            }
        )

    category_counts: Dict[str, int] = {}

    for candidate in review_candidates:
        category = candidate.get(
            "category",
            "unknown",
        )

        category_counts[category] = (
            category_counts.get(
                category,
                0,
            )
            + 1
        )

    return {
        "layer": (
            "bda-textract-structure-comparison-report-only"
        ),
        "html_changes_applied": False,
        "matching_threshold": MATCH_THRESHOLD,
        "summary": {
            "page_count": len(page_reports),
            "review_candidate_count": len(
                review_candidates
            ),
            "review_candidate_counts_by_category": (
                dict(
                    sorted(
                        category_counts.items()
                    )
                )
            ),
            "repeated_heading_group_count": len(
                repeated_heading_groups
            ),
            "bda_summary": (
                bda_html_structure.get(
                    "summary",
                    {},
                )
            ),
            "textract_summary": (
                textract_normalized.get(
                    "summary",
                    {},
                )
            ),
        },
        "repeated_heading_groups": (
            repeated_heading_groups
        ),
        "review_candidates": review_candidates,
        "pages": page_reports,
    }