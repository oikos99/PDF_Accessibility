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

# langdetect can otherwise return slightly different results between runs.
DetectorFactory.seed = 0


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
        "[INFO] Added <main id=\"document-content\"> landmark"
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


def _text_for_language_detection(
    soup: BeautifulSoup,
) -> str:
    """
    Extract readable text while excluding generated navigation and styles.

    Limit the sample size so language detection remains fast on long books.
    """
    clone = BeautifulSoup(
        str(soup),
        "html.parser",
    )

    for selector in [
        "script",
        "style",
        "nav",
        ".skip-link",
        ".page-marker",
    ]:
        for node in clone.select(selector):
            node.decompose()

    text = clone.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)

    return text[:50000]


def _normalise_language_tag(language: str) -> str:
    """
    Normalise common language-detector values into HTML language tags.

    HTML lang values use BCP 47 syntax, such as en, de, zh-Hans, or zh-Hant.
    """
    language = (language or "").strip().lower()
    language = language.replace("_", "-")

    mappings = {
        "zh-cn": "zh-Hans",
        "zh-tw": "zh-Hant",
    }

    return mappings.get(language, language)


def _detect_language(
    soup: BeautifulSoup,
) -> Tuple[str, float, Optional[str]]:
    """
    Detect the predominant document language.

    Return:
        applied language tag,
        confidence score,
        detected candidate
    """
    text = _text_for_language_detection(soup)

    alphabetic_character_count = sum(
        character.isalpha()
        for character in text
    )

    if alphabetic_character_count < 80:
        return "und", 0.0, None

    try:
        candidates = detect_langs(text)

    except LangDetectException:
        return "und", 0.0, None

    if not candidates:
        return "und", 0.0, None

    best_candidate = candidates[0]
    candidate_language = _normalise_language_tag(
        best_candidate.lang
    )
    confidence = float(best_candidate.prob)

    if confidence < 0.90:
        return "und", confidence, candidate_language

    return candidate_language, confidence, candidate_language


def _set_page_language(
    soup: BeautifulSoup,
) -> None:
    """Set the predominant document language on the <html> element."""
    html_tag = soup.find("html")

    if html_tag is None:
        return

    language, confidence, candidate = _detect_language(soup)

    html_tag["lang"] = language
    html_tag["data-language-source"] = "automatic-detection"
    html_tag["data-language-confidence"] = (
        f"{confidence:.2f}"
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
            "[WARN] Page language could not be determined "
            f"confidently; candidate={candidate}, "
            f"confidence={confidence:.2f}"
        )

    else:
        html_tag.attrs.pop(
            "data-language-review",
            None,
        )

        print(
            "[INFO] Applied page language: "
            f"{language} "
            f"(confidence={confidence:.2f})"
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

    _set_page_language(soup)

    html_path.write_text(
        str(soup),
        encoding="utf-8",
    )

    return str(html_path)