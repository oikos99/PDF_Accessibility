"""Apply conservative Textract-assisted cleanup to final HTML output."""

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from bs4 import BeautifulSoup

from ..config import PostprocessSettings


PathLike = Union[str, Path]

EXPLICIT_HEADER_MATCH_THRESHOLD = 0.90
REPEATED_HEADER_MATCH_THRESHOLD = 0.82
TOP_OF_PAGE_THRESHOLD = 0.12
PRINTED_PAGE_MIN_CONFIDENCE = 40.0


def _clean_text(text: str) -> str:
    """Collapse repeated whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def _normalise_for_match(text: str) -> str:
    """Normalize OCR text for conservative fuzzy matching."""
    text = _clean_text(text).casefold()
    text = re.sub(
        r"[^\w]+",
        " ",
        text,
        flags=re.UNICODE,
    )

    return re.sub(r"\s+", " ", text).strip()


def _similarity(
    left_text: str,
    right_text: str,
) -> float:
    """Return a fuzzy similarity score between zero and one."""
    left = _normalise_for_match(left_text)
    right = _normalise_for_match(right_text)

    if not left or not right:
        return 0.0

    if left == right:
        return 1.0

    return SequenceMatcher(
        None,
        left,
        right,
    ).ratio()


def _page_number_from_id(
    page_id: str,
) -> Optional[int]:
    """Convert page-0 into the one-based PDF page number 1."""
    match = re.fullmatch(
        r"page-(\d+)",
        page_id or "",
    )

    if not match:
        return None

    return int(match.group(1)) + 1


def _layout_elements(
    textract_normalized: Optional[
        Dict[str, Any]
    ],
) -> List[Dict[str, Any]]:
    """Return normalized Textract layout elements when available."""
    if not isinstance(
        textract_normalized,
        dict,
    ):
        return []

    elements = textract_normalized.get(
        "layout_elements",
        [],
    )

    return (
        elements
        if isinstance(elements, list)
        else []
    )


def _layout_by_page(
    layout_elements: List[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    """Group normalized Textract layout elements by PDF page."""
    grouped: Dict[
        int,
        List[Dict[str, Any]],
    ] = {}

    for element in layout_elements:
        page_number = element.get("page")

        if not page_number:
            continue

        grouped.setdefault(
            int(page_number),
            [],
        ).append(element)

    return grouped


def _humanize_filename(
    filename_base: str,
) -> str:
    """Convert an uploaded filename base into a safe title fallback."""
    title = re.sub(
        r"[_\-]+",
        " ",
        filename_base or "",
    )

    title = _clean_text(title)

    return title or "Converted PDF document"


def _find_title_candidate(
    soup: BeautifulSoup,
    layout_elements: List[Dict[str, Any]],
    filename_base: str,
) -> Tuple[str, str]:
    """
    Choose a meaningful document title conservatively.

    Prefer a substantial front-matter Textract LAYOUT_TITLE that
    closely matches an existing BDA HTML heading. Fall back to the
    uploaded filename.
    """
    existing_headings = [
        _clean_text(
            heading.get_text(
                " ",
                strip=True,
            )
        )
        for heading in soup.find_all(
            [
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            ]
        )
    ]

    scored_candidates: List[
        Tuple[float, str]
    ] = []

    for element in layout_elements:
        if (
            element.get("block_type")
            != "LAYOUT_TITLE"
        ):
            continue

        page_number = int(
            element.get("page") or 0
        )

        bounding_box = (
            element.get("bounding_box")
            or {}
        )

        top = float(
            bounding_box.get(
                "top",
                1.0,
            )
        )

        text = _clean_text(
            element.get(
                "text",
                "",
            )
        )

        word_count = len(
            text.split()
        )

        alpha_count = sum(
            character.isalpha()
            for character in text
        )

        if not 1 <= page_number <= 5:
            continue

        if (
            top > 0.60
            or word_count < 2
            or alpha_count < 8
        ):
            continue

        best_heading_similarity = max(
            (
                _similarity(
                    text,
                    heading_text,
                )
                for heading_text
                in existing_headings
            ),
            default=0.0,
        )

        if best_heading_similarity < 0.85:
            continue

        confidence = float(
            element.get(
                "confidence",
                0.0,
            )
        )

        score = (
            min(
                len(text),
                120,
            )
            + min(
                word_count,
                12,
            )
            * 8
            - (
                page_number
                - 1
            )
            * 4
            + confidence
            / 20.0
        )

        scored_candidates.append(
            (
                score,
                text,
            )
        )

    if scored_candidates:
        _, title = max(
            scored_candidates,
            key=lambda item: item[0],
        )

        return (
            title,
            "textract-layout-title",
        )

    return (
        _humanize_filename(
            filename_base
        ),
        "uploaded-filename",
    )


def _set_browser_title(
    soup: BeautifulSoup,
    title_text: str,
    source: str,
) -> None:
    """Replace an unusable browser title with a meaningful title."""
    head = soup.find("head")

    if head is None:
        return

    title = head.find("title")

    if title is None:
        title = soup.new_tag(
            "title"
        )

        head.insert(
            0,
            title,
        )

    title.string = title_text

    html_tag = soup.find("html")

    if html_tag is not None:
        html_tag[
            "data-document-title-source"
        ] = source


def _heading_records(
    soup: BeautifulSoup,
) -> List[Dict[str, Any]]:
    """Return current HTML headings with their PDF page metadata."""
    records: List[
        Dict[str, Any]
    ] = []

    for page in soup.select(
        'div.page[id^="page-"]'
    ):
        page_number = (
            _page_number_from_id(
                page.get(
                    "id",
                    "",
                )
            )
        )

        if page_number is None:
            continue

        headings = page.find_all(
            [
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            ]
        )

        for heading_order, heading in enumerate(
            headings,
            start=1,
        ):
            text = _clean_text(
                heading.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            records.append(
                {
                    "page": (
                        page_number
                    ),
                    "heading_order": (
                        heading_order
                    ),
                    "heading": heading,
                    "text": text,
                }
            )

    return records


def _best_same_page_layout_match(
    heading_text: str,
    page_elements: List[
        Dict[str, Any]
    ],
    allowed_types: Optional[
        set
    ] = None,
) -> Optional[Dict[str, Any]]:
    """Return the strongest Textract match for one heading."""
    best_match = None
    best_similarity = 0.0

    for element in page_elements:
        if (
            allowed_types
            and element.get(
                "block_type"
            )
            not in allowed_types
        ):
            continue

        similarity = _similarity(
            heading_text,
            element.get(
                "text",
                "",
            ),
        )

        if similarity <= best_similarity:
            continue

        best_similarity = similarity
        best_match = element

    if best_match is None:
        return None

    return {
        "element": (
            best_match
        ),
        "similarity": (
            best_similarity
        ),
    }


def _remove_explicit_textract_headers(
    soup: BeautifulSoup,
    layout_by_page: Dict[
        int,
        List[Dict[str, Any]],
    ],
) -> int:
    """
    Remove HTML headings that Textract clearly identifies as
    running headers.
    """
    removed_count = 0

    for record in list(
        _heading_records(soup)
    ):
        match = (
            _best_same_page_layout_match(
                heading_text=(
                    record["text"]
                ),
                page_elements=(
                    layout_by_page.get(
                        record[
                            "page"
                        ],
                        [],
                    )
                ),
                allowed_types={
                    "LAYOUT_HEADER"
                },
            )
        )

        if not match:
            continue

        element = match[
            "element"
        ]

        top = float(
            (
                element.get(
                    "bounding_box"
                )
                or {}
            ).get(
                "top",
                1.0,
            )
        )

        if (
            match[
                "similarity"
            ]
            >= EXPLICIT_HEADER_MATCH_THRESHOLD
            and top
            <= TOP_OF_PAGE_THRESHOLD
        ):
            print(
                "[INFO] Removed Textract-confirmed "
                "running header: "
                f"page={record['page']} "
                f"text={record['text']!r}"
            )

            record[
                "heading"
            ].decompose()

            removed_count += 1

    return removed_count


def _remove_repeated_top_headers(
    soup: BeautifulSoup,
    layout_by_page: Dict[
        int,
        List[Dict[str, Any]],
    ],
) -> int:
    """
    Remove later occurrences of OCR-tolerant repeated headings.

    Preserve the earliest occurrence in each repeated group.
    """
    records = (
        _heading_records(soup)
    )

    groups: List[
        Dict[str, Any]
    ] = []

    for record in records:
        matching_group = None

        for group in groups:
            if (
                _similarity(
                    record["text"],
                    group[
                        "representative_text"
                    ],
                )
                >= REPEATED_HEADER_MATCH_THRESHOLD
            ):
                matching_group = group
                break

        if matching_group is None:
            groups.append(
                {
                    "representative_text": (
                        record[
                            "text"
                        ]
                    ),
                    "records": [
                        record
                    ],
                }
            )

        else:
            matching_group[
                "records"
            ].append(record)

    removed_count = 0

    for group in groups:
        group_records = (
            group["records"]
        )

        distinct_pages = sorted(
            {
                record[
                    "page"
                ]
                for record
                in group_records
            }
        )

        if len(
            distinct_pages
        ) < 3:
            continue

        earliest_page = min(
            distinct_pages
        )

        preserved_earliest = False

        for record in group_records:
            if (
                record[
                    "page"
                ]
                == earliest_page
                and not preserved_earliest
            ):
                preserved_earliest = True
                continue

            if (
                record[
                    "heading_order"
                ]
                != 1
            ):
                continue

            match = (
                _best_same_page_layout_match(
                    heading_text=(
                        record[
                            "text"
                        ]
                    ),
                    page_elements=(
                        layout_by_page.get(
                            record[
                                "page"
                            ],
                            [],
                        )
                    ),
                    allowed_types={
                        "LAYOUT_HEADER",
                        "LAYOUT_TITLE",
                        "LAYOUT_SECTION_HEADER",
                        "LAYOUT_TEXT",
                    },
                )
            )

            if not match:
                continue

            element = match[
                "element"
            ]

            top = float(
                (
                    element.get(
                        "bounding_box"
                    )
                    or {}
                ).get(
                    "top",
                    1.0,
                )
            )

            if (
                match[
                    "similarity"
                ]
                >= REPEATED_HEADER_MATCH_THRESHOLD
                and top
                <= TOP_OF_PAGE_THRESHOLD
            ):
                print(
                    "[INFO] Removed repeated top-of-page "
                    "running header: "
                    f"page={record['page']} "
                    f"text={record['text']!r}"
                )

                record[
                    "heading"
                ].decompose()

                removed_count += 1

    return removed_count


def _ensure_document_h1(
    soup: BeautifulSoup,
    title_text: str,
    source: str,
) -> None:
    """Add or refresh one document-level H1 before the page content."""
    existing = soup.find(
        "h1",
        attrs={
            "data-generated-document-title": (
                "true"
            )
        },
    )

    if existing is None:
        existing = soup.new_tag(
            "h1"
        )

        existing[
            "class"
        ] = [
            "document-title"
        ]

        existing[
            "data-generated-document-title"
        ] = "true"

        main = soup.find(
            "main",
            id=(
                "document-content"
            ),
        )

        first_page = (
            soup.select_one(
                'div.page[id^="page-"]'
            )
        )

        if main is not None:
            main.insert(
                0,
                existing,
            )

        elif (
            first_page
            is not None
        ):
            first_page.insert_before(
                existing
            )

        elif soup.body is not None:
            soup.body.insert(
                0,
                existing,
            )

    existing[
        "data-document-title-source"
    ] = source

    existing.string = (
        title_text
    )


def _normalize_heading_levels(
    soup: BeautifulSoup,
) -> None:
    """
    Produce a conservative non-skipping outline below the generated H1.

    Existing deeper levels are preserved only when they do not skip
    a level.
    """
    previous_level = 1

    headings = soup.select(
        'div.page[id^="page-"] h1, '
        'div.page[id^="page-"] h2, '
        'div.page[id^="page-"] h3, '
        'div.page[id^="page-"] h4, '
        'div.page[id^="page-"] h5, '
        'div.page[id^="page-"] h6'
    )

    for heading in headings:
        current_level = int(
            heading.name[1]
        )

        if current_level <= 2:
            target_level = 2

        else:
            target_level = min(
                current_level,
                previous_level
                + 1,
            )

            target_level = max(
                2,
                target_level,
            )

        heading.name = (
            f"h{target_level}"
        )

        previous_level = (
            target_level
        )


def _normalise_printed_page_value(
    text: str,
) -> Optional[str]:
    """Accept short Arabic or Roman numeral printed page labels."""
    value = _clean_text(
        text
    )

    if re.fullmatch(
        r"\d{1,5}",
        value,
    ):
        return value

    if re.fullmatch(
        r"[ivxlcdmIVXLCDM]{1,12}",
        value,
    ):
        return value

    return None


def _printed_page_numbers_by_pdf_page(
    layout_elements: List[
        Dict[str, Any]
    ],
) -> Dict[int, str]:
    """Return one unambiguous printed-page value for each PDF page."""
    candidates_by_page: Dict[
        int,
        List[
            Tuple[
                float,
                str,
            ]
        ],
    ] = {}

    for element in layout_elements:
        if (
            element.get(
                "block_type"
            )
            != "LAYOUT_PAGE_NUMBER"
        ):
            continue

        page_number = int(
            element.get(
                "page"
            )
            or 0
        )

        confidence = float(
            element.get(
                "confidence",
                0.0,
            )
        )

        bounding_box = (
            element.get(
                "bounding_box"
            )
            or {}
        )

        top = float(
            bounding_box.get(
                "top",
                1.0,
            )
        )

        value = (
            _normalise_printed_page_value(
                element.get(
                    "text",
                    "",
                )
            )
        )

        if (
            not page_number
            or value is None
        ):
            continue

        if (
            confidence
            < PRINTED_PAGE_MIN_CONFIDENCE
        ):
            continue

        if not (
            top <= 0.20
            or top >= 0.75
        ):
            continue

        candidates_by_page.setdefault(
            page_number,
            [],
        ).append(
            (
                confidence,
                value,
            )
        )

    accepted: Dict[
        int,
        str,
    ] = {}

    for (
        pdf_page_number,
        candidates,
    ) in candidates_by_page.items():
        unique_values = {
            value
            for _,
            value
            in candidates
        }

        if len(
            unique_values
        ) != 1:
            print(
                "[WARN] Skipped ambiguous printed "
                "page numbers: "
                f"pdf_page={pdf_page_number} "
                f"values={sorted(unique_values)}"
            )

            continue

        accepted[
            pdf_page_number
        ] = max(
            candidates,
            key=lambda item: item[0],
        )[1]

    return accepted


def _find_simple_standalone_number_node(
    page,
    printed_value: str,
):
    """Find a redundant simple BDA node containing only a page number."""
    for node in page.find_all(
        [
            "div",
            "p",
            "span",
        ]
    ):
        if (
            "page-marker"
            in node.get(
                "class",
                [],
            )
        ):
            continue

        if (
            node.find(
                True
            )
            is not None
        ):
            continue

        if (
            _clean_text(
                node.get_text(
                    " ",
                    strip=True,
                )
            )
            == printed_value
        ):
            return node

    return None


def _update_visible_page_marker(
    page,
    pdf_page_number: int,
    printed_value: str,
) -> None:
    """Merge PDF and printed pagination into one visible marker."""
    marker = page.find(
        "p",
        class_="page-marker",
    )

    if marker is None:
        return

    visible_label = (
        f"PDF page {pdf_page_number} "
        f"· printed page {printed_value}"
    )

    accessible_label = (
        f"PDF page {pdf_page_number}, "
        f"printed page {printed_value}"
    )

    page[
        "data-pdf-page-number"
    ] = str(
        pdf_page_number
    )

    page[
        "data-printed-page-number"
    ] = printed_value

    marker[
        "role"
    ] = "doc-pagebreak"

    marker[
        "aria-label"
    ] = accessible_label

    marker[
        "data-pdf-page-number"
    ] = str(
        pdf_page_number
    )

    marker[
        "data-printed-page-number"
    ] = printed_value

    marker[
        "data-page-number-source"
    ] = "textract-layout"

    marker.string = (
        visible_label
    )


def _apply_printed_page_numbers(
    soup: BeautifulSoup,
    layout_elements: List[
        Dict[str, Any]
    ],
) -> int:
    """Preserve printed page numbers visibly and remove clear duplicates."""
    accepted = (
        _printed_page_numbers_by_pdf_page(
            layout_elements
        )
    )

    applied_count = 0

    for page in soup.select(
        'div.page[id^="page-"]'
    ):
        pdf_page_number = (
            _page_number_from_id(
                page.get(
                    "id",
                    "",
                )
            )
        )

        if pdf_page_number is None:
            continue

        printed_value = (
            accepted.get(
                pdf_page_number
            )
        )

        if printed_value is None:
            continue

        _update_visible_page_marker(
            page=page,
            pdf_page_number=(
                pdf_page_number
            ),
            printed_value=(
                printed_value
            ),
        )

        redundant_node = (
            _find_simple_standalone_number_node(
                page=page,
                printed_value=(
                    printed_value
                ),
            )
        )

        if (
            redundant_node
            is not None
        ):
            redundant_node.decompose()

        print(
            "[INFO] Added visible printed-page "
            "metadata: "
            f"pdf_page={pdf_page_number} "
            f"printed_page={printed_value}"
        )

        applied_count += 1

    return applied_count


def _atomic_write_html(
    html_path: Path,
    soup: BeautifulSoup,
) -> None:
    """Replace the HTML only after the transformed document is complete."""
    temporary_path = (
        html_path.with_suffix(
            html_path.suffix
            + ".structure-cleanup.tmp"
        )
    )

    temporary_path.write_text(
        str(soup),
        encoding="utf-8",
    )

    temporary_path.replace(
        html_path
    )


def apply_structure_html_cleanup(
    html_path: PathLike,
    filename_base: str,
    textract_normalized: Optional[
        Dict[str, Any]
    ] = None,
) -> str:
    """
    Apply a narrow and resilient structural cleanup pass.

    If the feature flag is off, return the original HTML untouched.

    If Textract is disabled or unavailable, apply only the safe
    filename-based browser-title fallback. The baseline BDA HTML
    workflow continues normally.
    """
    html_path = Path(
        html_path
    )

    settings = (
        PostprocessSettings.from_env()
    )

    if not (
        settings
        .structure_apply_html_changes_enabled
    ):
        print(
            "[INFO] Structure HTML cleanup "
            "is disabled"
        )

        return str(
            html_path
        )

    try:
        soup = BeautifulSoup(
            html_path.read_text(
                encoding="utf-8"
            ),
            "html.parser",
        )

        layout_elements = (
            _layout_elements(
                textract_normalized
            )
        )

        (
            title_text,
            title_source,
        ) = _find_title_candidate(
            soup=soup,
            layout_elements=(
                layout_elements
            ),
            filename_base=(
                filename_base
            ),
        )

        _set_browser_title(
            soup=soup,
            title_text=(
                title_text
            ),
            source=(
                title_source
            ),
        )

        if not layout_elements:
            print(
                "[INFO] Textract structure evidence "
                "is unavailable; applied filename-based "
                "browser-title fallback only"
            )

            _atomic_write_html(
                html_path,
                soup,
            )

            return str(
                html_path
            )

        layout_by_page = (
            _layout_by_page(
                layout_elements
            )
        )

        # Run the repeated-heading pass first so OCR variants remain
        # grouped even when some occurrences are also explicit headers.
        repeated_removed = (
            _remove_repeated_top_headers(
                soup=soup,
                layout_by_page=(
                    layout_by_page
                ),
            )
        )

        explicit_removed = (
            _remove_explicit_textract_headers(
                soup=soup,
                layout_by_page=(
                    layout_by_page
                ),
            )
        )

        _ensure_document_h1(
            soup=soup,
            title_text=(
                title_text
            ),
            source=(
                title_source
            ),
        )

        _normalize_heading_levels(
            soup
        )

        printed_page_count = (
            _apply_printed_page_numbers(
                soup=soup,
                layout_elements=(
                    layout_elements
                ),
            )
        )

        html_tag = soup.find(
            "html"
        )

        if html_tag is not None:
            html_tag[
                "data-structure-cleanup"
            ] = "textract-layout"

        _atomic_write_html(
            html_path,
            soup,
        )

        print(
            "[INFO] Applied Textract-assisted "
            "structure cleanup: "
            f"explicit_headers_removed="
            f"{explicit_removed}, "
            f"repeated_headers_removed="
            f"{repeated_removed}, "
            f"printed_page_markers="
            f"{printed_page_count}, "
            f"title_source={title_source}"
        )

    except Exception as exc:
        print(
            "[WARN] Structure HTML cleanup failed; "
            "preserving baseline HTML: "
            f"{exc}"
        )

    return str(
        html_path
    )