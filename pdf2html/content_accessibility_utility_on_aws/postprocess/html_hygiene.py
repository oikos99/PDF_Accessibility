"""Apply deterministic accessibility improvements to final HTML output."""

import re
from pathlib import Path
from typing import Optional, Tuple, Union

from bs4 import BeautifulSoup
from langdetect import (
    DetectorFactory,
    LangDetectException,
    detect_langs,
)


PathLike = Union[str, Path]

# langdetect can otherwise return slightly different results between runs
# when the text is short or ambiguous.
DetectorFactory.seed = 0

DOCUMENT_MIN_ALPHA_CHARACTERS = 80
DOCUMENT_MIN_CONFIDENCE = 0.90
DOCUMENT_MIN_MARGIN = 0.20

PAGE_MIN_ALPHA_CHARACTERS = 160
PAGE_MIN_CONFIDENCE = 0.95
PAGE_MIN_MARGIN = 0.20


def _ensure_styles(soup: BeautifulSoup) -> None:
    """Add styles for a keyboard-accessible skip link."""
    head = soup.find("head")

    if not head:
        return

    existing_style = soup.find(
        "style",
        attrs={"data-html-hygiene-style": "true"},
    )

    if existing_style:
        return

    style = soup.new_tag("style")
    style["data-html-hygiene-style"] = "true"
    style.string = """
.skip-link {
  position: absolute;
  left: 0.5rem;
  top: 0.5rem;
  padding: 0.75rem 1rem;
  background: #ffffff;
  color: #000000;
  border: 2px solid currentColor;
  transform: translateY(-200%);
  z-index: 1000;
}

.skip-link:focus {
  transform: translateY(0);
}
""".strip()

    head.append(style)


def _ensure_main_landmark(soup: BeautifulSoup):
    """
    Wrap converted PDF page containers in one main landmark.

    Expected page containers resemble:
        <div class="page" id="page-0">
    """
    body = soup.find("body")

    if not body:
        return None

    main = soup.find("main", id="document-content")

    if main:
        main["tabindex"] = "-1"
        return main

    pages = [
        page
        for page in soup.select('div.page[id^="page-"]')
        if page.find_parent("main") is None
    ]

    if not pages:
        print(
            "[WARN] No page containers found for main landmark"
        )
        return None

    main = soup.new_tag("main")
    main["id"] = "document-content"
    main["tabindex"] = "-1"

    pages[0].insert_before(main)

    for page in pages:
        main.append(page.extract())

    print(
        '[INFO] Added <main id="document-content"> landmark'
    )

    return main


def _ensure_skip_link(
    soup: BeautifulSoup,
    main,
) -> None:
    """Add a skip link as the first element inside <body>."""
    body = soup.find("body")

    if not body or main is None:
        return

    skip_link = soup.find(
        "a",
        attrs={"class": "skip-link"},
    )

    if skip_link is None:
        skip_link = soup.new_tag(
            "a",
            href="#document-content",
        )
        skip_link["class"] = ["skip-link"]
        skip_link.string = "Skip to document content"

    else:
        skip_link.extract()
        skip_link["href"] = "#document-content"
        skip_link.string = "Skip to document content"

    body.insert(0, skip_link)

    print("[INFO] Added skip link")


def _clean_text(text: str) -> str:
    """Collapse repeated whitespace in extracted text."""
    return re.sub(r"\s+", " ", text or "").strip()


def _text_for_language_detection(
    node,
    max_characters: int,
) -> str:
    """
    Extract readable text from a document or page container.

    Exclude generated navigation, styles, scripts, and visible page markers.
    """
    clone = BeautifulSoup(
        str(node),
        "html.parser",
    )

    for selector in [
        "script",
        "style",
        "nav",
        ".skip-link",
        ".page-marker",
    ]:
        for selected_node in clone.select(selector):
            selected_node.decompose()

    text = clone.get_text(" ", strip=True)

    return _clean_text(text)[:max_characters]


def _normalise_language_tag(language: str) -> str:
    """
    Normalise detector values into HTML language tags.

    HTML lang values use BCP 47 syntax, such as:
        en
        de
        zh-Hans
        zh-Hant
    """
    language = (language or "").strip().lower()
    language = language.replace("_", "-")

    mappings = {
        "zh-cn": "zh-Hans",
        "zh-tw": "zh-Hant",
    }

    return mappings.get(language, language)


def _detect_language_from_text(
    text: str,
    min_alpha_characters: int,
    min_confidence: float,
    min_margin: float,
) -> Tuple[str, float, Optional[str], float]:
    """
    Detect one predominant language conservatively.

    Return:
        applied language tag or "und",
        confidence score,
        best detected candidate,
        probability margin between the first and second candidates
    """
    alphabetic_character_count = sum(
        character.isalpha()
        for character in text
    )

    if alphabetic_character_count < min_alpha_characters:
        return "und", 0.0, None, 0.0

    try:
        candidates = detect_langs(text)

    except LangDetectException:
        return "und", 0.0, None, 0.0

    if not candidates:
        return "und", 0.0, None, 0.0

    best_candidate = candidates[0]

    candidate_language = _normalise_language_tag(
        best_candidate.lang
    )

    confidence = float(best_candidate.prob)

    second_confidence = (
        float(candidates[1].prob)
        if len(candidates) > 1
        else 0.0
    )

    margin = confidence - second_confidence

    if (
        confidence < min_confidence
        or margin < min_margin
    ):
        return (
            "und",
            confidence,
            candidate_language,
            margin,
        )

    return (
        candidate_language,
        confidence,
        candidate_language,
        margin,
    )


def _detect_document_language(
    soup: BeautifulSoup,
) -> Tuple[str, float, Optional[str], float]:
    """Detect the predominant language of the complete document."""
    text = _text_for_language_detection(
        node=soup,
        max_characters=50000,
    )

    return _detect_language_from_text(
        text=text,
        min_alpha_characters=DOCUMENT_MIN_ALPHA_CHARACTERS,
        min_confidence=DOCUMENT_MIN_CONFIDENCE,
        min_margin=DOCUMENT_MIN_MARGIN,
    )


def _set_document_language(
    soup: BeautifulSoup,
) -> str:
    """Set the predominant document language on the <html> element."""
    html_tag = soup.find("html")

    if html_tag is None:
        return "und"

    (
        language,
        confidence,
        candidate,
        margin,
    ) = _detect_document_language(soup)

    html_tag["lang"] = language
    html_tag["data-language-source"] = "automatic-detection"
    html_tag["data-language-confidence"] = (
        f"{confidence:.2f}"
    )
    html_tag["data-language-margin"] = (
        f"{margin:.2f}"
    )

    if candidate:
        html_tag["data-detected-language-candidate"] = (
            candidate
        )

    else:
        html_tag.attrs.pop(
            "data-detected-language-candidate",
            None,
        )

    if language == "und":
        html_tag["data-language-review"] = "required"

        print(
            "[WARN] Document language could not be determined "
            f"confidently; candidate={candidate}, "
            f"confidence={confidence:.2f}, "
            f"margin={margin:.2f}"
        )

    else:
        html_tag.attrs.pop(
            "data-language-review",
            None,
        )

        print(
            "[INFO] Applied document language: "
            f"{language} "
            f"(confidence={confidence:.2f}, "
            f"margin={margin:.2f})"
        )

    return language


def _clear_generated_page_language_metadata(page) -> None:
    """
    Remove language values previously generated by this module.

    Preserve a manually assigned lang value when it was not generated here.
    """
    generated_source = page.get(
        "data-page-language-source"
    )

    if generated_source == "automatic-detection":
        page.attrs.pop("lang", None)

    for attribute_name in [
        "data-page-language-source",
        "data-page-language-confidence",
        "data-page-language-margin",
        "data-detected-page-language-candidate",
        "data-page-language-review",
    ]:
        page.attrs.pop(attribute_name, None)


def _set_page_language_overrides(
    soup: BeautifulSoup,
    document_language: str,
) -> None:
    """
    Add page-level lang overrides only when strongly supported.

    Example:
        <div class="page" id="page-0" lang="en">

    Do not add redundant page-level lang attributes when a page language
    matches the predominant document language. In that case, the page
    inherits the value from <html lang="...">.
    """
    pages = soup.select('div.page[id^="page-"]')

    if not pages:
        print(
            "[WARN] No page containers found for page-language detection"
        )
        return

    if document_language == "und":
        print(
            "[WARN] Skipping page-language overrides because "
            "the document language is uncertain"
        )
        return

    for page in pages:
        existing_lang = page.get("lang")
        generated_source = page.get(
            "data-page-language-source"
        )

        # Preserve a manually added page-level lang value.
        if (
            existing_lang
            and generated_source != "automatic-detection"
        ):
            print(
                "[INFO] Preserved manual page-language override: "
                f"#{page.get('id')} lang={existing_lang}"
            )
            continue

        _clear_generated_page_language_metadata(page)

        text = _text_for_language_detection(
            node=page,
            max_characters=10000,
        )

        (
            language,
            confidence,
            candidate,
            margin,
        ) = _detect_language_from_text(
            text=text,
            min_alpha_characters=PAGE_MIN_ALPHA_CHARACTERS,
            min_confidence=PAGE_MIN_CONFIDENCE,
            min_margin=PAGE_MIN_MARGIN,
        )

        # An uncertain result inherits the predominant document language.
        # Preserve review metadata only when there is evidence of a
        # potentially different page language.
        if language == "und":
            if (
                candidate
                and candidate != document_language
            ):
                page["data-page-language-source"] = (
                    "automatic-detection"
                )
                page[
                    "data-detected-page-language-candidate"
                ] = candidate
                page["data-page-language-confidence"] = (
                    f"{confidence:.2f}"
                )
                page["data-page-language-margin"] = (
                    f"{margin:.2f}"
                )
                page["data-page-language-review"] = (
                    "required"
                )

                print(
                    "[WARN] Possible page-language change "
                    "requires review: "
                    f"#{page.get('id')} "
                    f"candidate={candidate}, "
                    f"confidence={confidence:.2f}, "
                    f"margin={margin:.2f}"
                )

            continue

        # Matching pages inherit the document-level language.
        if language == document_language:
            continue

        page["lang"] = language
        page["data-page-language-source"] = (
            "automatic-detection"
        )
        page["data-detected-page-language-candidate"] = (
            candidate or language
        )
        page["data-page-language-confidence"] = (
            f"{confidence:.2f}"
        )
        page["data-page-language-margin"] = (
            f"{margin:.2f}"
        )

        print(
            "[INFO] Applied page-language override: "
            f"#{page.get('id')} "
            f"lang={language} "
            f"(document={document_language}, "
            f"confidence={confidence:.2f}, "
            f"margin={margin:.2f})"
        )


def apply_html_hygiene(
    html_path: PathLike,
) -> str:
    """
    Apply deterministic HTML accessibility cleanup.

    Current scope:
    - Add one main landmark around converted PDF pages.
    - Add a keyboard-accessible skip link.
    - Set the predominant document language.
    - Add conservative page-level language overrides.
    """
    html_path = Path(html_path)

    soup = BeautifulSoup(
        html_path.read_text(encoding="utf-8"),
        "html.parser",
    )

    _ensure_styles(soup)

    main = _ensure_main_landmark(soup)

    _ensure_skip_link(
        soup=soup,
        main=main,
    )

    document_language = _set_document_language(soup)

    _set_page_language_overrides(
        soup=soup,
        document_language=document_language,
    )

    html_path.write_text(
        str(soup),
        encoding="utf-8",
    )

    return str(html_path)