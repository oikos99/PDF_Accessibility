"""Apply image-classification decisions to the final HTML DOM."""

import re
from typing import Dict, Optional

from bs4 import BeautifulSoup, Tag

from .models import ImageAnalysis, ImageClassification


GENERIC_ALT_TEXT = {
    "",
    "image",
    "diagram",
    "photo",
    "picture",
    "graphic",
    "icon",
    "logo",
    "chart",
    "graph",
    "figure",
    "illustration",
}


def _clean_text(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def extract_context(img: Tag) -> Dict[str, str]:
    """Collect nearby text to help Nova interpret the crop."""
    nearby_text = []

    for node in (
        img.find_previous(
            ["p", "li", "figcaption", "h1", "h2", "h3"]
        ),
        img.find_next(
            ["p", "li", "figcaption", "h1", "h2", "h3"]
        ),
    ):
        if node:
            text = _clean_text(
                node.get_text(" ", strip=True),
                700,
            )

            if text and text not in nearby_text:
                nearby_text.append(text)

    caption = ""

    figure = img.find_parent("figure")

    if figure:
        figcaption = figure.find("figcaption")

        if figcaption:
            caption = _clean_text(
                figcaption.get_text(" ", strip=True),
                500,
            )

    return {
        "existing_alt": _clean_text(img.get("alt", ""), 300),
        "caption": caption,
        "surrounding_text": _clean_text(
            " ".join(nearby_text),
            1500,
        ),
    }


def _usable_alt(text: str) -> bool:
    return bool(
        text
        and text.strip().lower() not in GENERIC_ALT_TEXT
    )


def _best_alt(generated: str, existing: str) -> str:
    if _usable_alt(generated):
        return generated

    if _usable_alt(existing):
        return existing

    return "Image description unavailable; manual review required."


def _remove_empty_wrapper(wrapper: Optional[Tag]) -> None:
    if not wrapper or wrapper.name not in {
        "figure",
        "p",
        "div",
        "span",
    }:
        return

    if wrapper.get("id"):
        return

    if "page" in set(wrapper.get("class", [])):
        return

    if not wrapper.get_text(strip=True) and not wrapper.find(True):
        wrapper.decompose()


def remove_image(img: Tag) -> None:
    wrapper = img.parent if isinstance(img.parent, Tag) else None
    img.decompose()
    _remove_empty_wrapper(wrapper)


def replace_with_thematic_break(
    soup: BeautifulSoup,
    img: Tag,
) -> None:
    hr = soup.new_tag("hr")
    hr["class"] = "thematic-break"
    img.replace_with(hr)


def add_long_description(
    soup: BeautifulSoup,
    img: Tag,
    image_index: int,
    description: str,
) -> None:
    if not description:
        return

    description_id = f"image-description-{image_index}"

    details = soup.new_tag("details")
    details["class"] = "image-long-description"

    summary = soup.new_tag("summary")
    summary.string = "Detailed image description"

    paragraph = soup.new_tag("p")
    paragraph["id"] = description_id
    paragraph.string = description

    details.append(summary)
    details.append(paragraph)

    img["aria-describedby"] = description_id
    img.insert_after(details)


def apply_analysis(
    soup: BeautifulSoup,
    img: Tag,
    analysis: ImageAnalysis,
    image_index: int,
    decorative_threshold: float,
    thematic_break_threshold: float,
) -> str:
    """Apply one model decision and return a reportable action name."""
    classification = analysis.classification
    confidence = analysis.confidence
    existing_alt = img.get("alt", "")

    img["data-image-classification"] = classification.value
    img["data-image-confidence"] = f"{confidence:.2f}"

    if (
        classification == ImageClassification.DECORATIVE
        and confidence >= decorative_threshold
    ):
        remove_image(img)
        return "removed-decorative"

    if (
        classification == ImageClassification.THEMATIC_BREAK
        and confidence >= thematic_break_threshold
    ):
        replace_with_thematic_break(soup, img)
        return "replaced-with-hr"

    if classification == ImageClassification.REDUNDANT:
        img["alt"] = ""
        return "kept-with-empty-alt"

    if classification in {
        ImageClassification.INFORMATIVE_SIMPLE,
        ImageClassification.FUNCTIONAL,
    }:
        img["alt"] = _best_alt(
            analysis.alt_text,
            existing_alt,
        )

        return "updated-alt-text"

    if classification == ImageClassification.INFORMATIVE_COMPLEX:
        img["alt"] = _best_alt(
            analysis.alt_text,
            existing_alt,
        )

        add_long_description(
            soup,
            img,
            image_index,
            analysis.long_description,
        )

        img["data-accessibility-review"] = "recommended"

        return "added-alt-and-long-description"

    img["alt"] = _best_alt(
        analysis.alt_text,
        existing_alt,
    )

    img["data-accessibility-review"] = "required"

    return "kept-for-review"