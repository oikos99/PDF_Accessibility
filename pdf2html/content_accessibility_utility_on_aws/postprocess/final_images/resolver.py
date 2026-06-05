"""Resolve local crop files and embed retained images as data URIs."""

import base64
import mimetypes
from pathlib import Path
from typing import Iterable, Optional, Union
from urllib.parse import unquote

from bs4 import BeautifulSoup


PathLike = Union[str, Path]


def resolve_local_image_path(
    html_path: PathLike,
    src: str,
    search_roots: Iterable[PathLike],
) -> Optional[Path]:
    """
    Resolve a local image path deterministically.

    Never substitute an unrelated crop when the requested file is missing.
    """
    if not src or src.startswith(("data:", "http://", "https://")):
        return None

    html_path = Path(html_path)

    clean_src = src.split("#", 1)[0].split("?", 1)[0]
    clean_src = unquote(clean_src)
    clean_path = Path(clean_src)

    candidates = [
        (html_path.parent / clean_path).resolve(),
    ]

    for root in search_roots:
        root = Path(root)

        candidates.extend(
            [
                (root / clean_path).resolve(),
                (root / "extracted_html" / clean_path).resolve(),
                (
                    root
                    / "extracted_html"
                    / "images"
                    / clean_path.name
                ).resolve(),
                (root / "images" / clean_path.name).resolve(),
            ]
        )

    candidates = list(dict.fromkeys(candidates))

    return next(
        (
            candidate
            for candidate in candidates
            if candidate.exists() and candidate.is_file()
        ),
        None,
    )


def embed_local_images_as_base64(
    html_path: PathLike,
    search_roots: Iterable[PathLike],
) -> str:
    """Replace retained local image references with base64 data URIs."""
    html_path = Path(html_path)

    soup = BeautifulSoup(
        html_path.read_text(encoding="utf-8"),
        "html.parser",
    )

    for img in soup.find_all("img"):
        src = img.get("src", "")

        if not src or src.startswith(
            ("data:", "http://", "https://")
        ):
            continue

        image_path = resolve_local_image_path(
            html_path=html_path,
            src=src,
            search_roots=search_roots,
        )

        if not image_path:
            img["data-accessibility-review"] = "required"
            continue

        mime_type = (
            mimetypes.guess_type(str(image_path))[0]
            or "image/png"
        )

        encoded = base64.b64encode(
            image_path.read_bytes()
        ).decode("ascii")

        img["src"] = f"data:{mime_type};base64,{encoded}"

    html_path.write_text(str(soup), encoding="utf-8")

    return str(html_path)