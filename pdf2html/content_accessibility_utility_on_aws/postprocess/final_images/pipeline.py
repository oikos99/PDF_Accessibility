"""Orchestrate the final HTML image-processing pass."""

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Union

from bs4 import BeautifulSoup

from .classifier import NovaImageClassifier
from .html_actions import (
    apply_analysis,
    extract_context,
)
from .models import ImageAnalysis, ImageClassification
from .report import write_image_report
from .resolver import (
    embed_local_images_as_base64,
    resolve_local_image_path,
)
from .sizing import apply_final_html_pixel_widths


PathLike = Union[str, Path]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _ensure_styles(soup: BeautifulSoup) -> None:
    head = soup.find("head")

    if not head:
        return

    if soup.find(
        "style",
        attrs={"data-final-image-processing": "true"},
    ):
        return

    style = soup.new_tag("style")
    style["data-final-image-processing"] = "true"

    style.string = """
img {
  max-width: 100%;
  height: auto;
}

.image-long-description {
  margin-top: 0.5rem;
  margin-bottom: 1rem;
}

.thematic-break {
  margin-top: 1.5rem;
  margin-bottom: 1.5rem;
}
""".strip()

    head.append(style)


def process_final_html_images(
    html_path: PathLike,
    search_roots: Iterable[PathLike],
) -> Tuple[str, str]:
    """
    Classify, remediate, report, and embed images in the final HTML file.

    Returns:
        updated_html_path, report_path
    """
    html_path = Path(html_path)
    search_roots = list(search_roots)

    model_id = os.environ.get(
        "IMAGE_ANALYSIS_MODEL_ID",
        "us.amazon.nova-lite-v1:0",
    )

    decorative_threshold = _env_float(
        "DECORATIVE_AUTO_REMOVE_THRESHOLD",
        0.98,
    )

    thematic_break_threshold = _env_float(
        "THEMATIC_BREAK_THRESHOLD",
        0.95,
    )

    soup = BeautifulSoup(
        html_path.read_text(encoding="utf-8"),
        "html.parser",
    )

    _ensure_styles(soup)

    apply_final_html_pixel_widths(
        soup=soup,
        search_roots=search_roots,
    )

    classifier = NovaImageClassifier(model_id=model_id)
    report_items: List[Dict[str, Any]] = []

    for image_index, img in enumerate(
        list(soup.find_all("img")),
        start=1,
    ):
        src = img.get("src", "")
        existing_alt = img.get("alt", "")

        image_path = resolve_local_image_path(
            html_path=html_path,
            src=src,
            search_roots=search_roots,
        )

        if not image_path:
            img["data-accessibility-review"] = "required"

            report_items.append(
                {
                    "image_index": image_index,
                    "src": src,
                    "existing_alt": existing_alt,
                    "classification": "UNCERTAIN",
                    "confidence": 0.0,
                    "action": "kept-path-unresolved",
                    "review_required": True,
                }
            )

            continue

        try:
            analysis = classifier.classify(
                image_path=image_path,
                context=extract_context(img),
            )

        except Exception as exc:
            analysis = ImageAnalysis(
                classification=ImageClassification.UNCERTAIN,
                confidence=0.0,
                reason=f"Nova analysis failed: {exc}",
            )

        action = apply_analysis(
            soup=soup,
            img=img,
            analysis=analysis,
            image_index=image_index,
            decorative_threshold=decorative_threshold,
            thematic_break_threshold=thematic_break_threshold,
        )

        report_items.append(
            {
                "image_index": image_index,
                "src": src,
                "existing_alt": existing_alt,
                **analysis.to_dict(),
                "classification": analysis.classification.value,
                "action": action,
                "review_required": (
                    action in {
                        "kept-for-review",
                        "added-alt-and-long-description",
                    }
                ),
            }
        )

    html_path.write_text(str(soup), encoding="utf-8")

    report_path = write_image_report(
        html_path=str(html_path),
        model_id=model_id,
        items=report_items,
    )

    embedded_html_path = embed_local_images_as_base64(
        html_path=html_path,
        search_roots=search_roots,
    )

    return embedded_html_path, report_path