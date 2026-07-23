"""
Post-remediation accessibility finalizer and QA reporter for the PDF-to-PDF pipeline.

Runs after merge + title generation. It performs deterministic cleanup and limited AI assistance:
- Sets document-level /Lang when language detection is confident.
- Sets per-page /Tabs /S so tab order follows the structure tree.
- Produces QA JSON for tags, OCR quality, language, tab order, figures/images, and generic alt text.
- Optionally uses Bedrock for language disambiguation and meaningful-image alt text suggestions.

This Lambda intentionally does not rewrite the PDF tag tree for images yet. It reports artifact
candidates and alt text suggestions so the next safe mapping step can update Figure structure
nodes with confidence.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import fitz  # PyMuPDF
from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, DictionaryObject, NameObject, TextStringObject

try:
    from langdetect import DetectorFactory, detect_langs
    DetectorFactory.seed = 0
except Exception:  # pragma: no cover - available in Lambda via requirements.txt
    detect_langs = None

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

LANG_TO_PDF_LANG = {
    "en": "en-US",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "zh": "zh",
    "ja": "ja",
    "ko": "ko",
    "ar": "ar",
    "ru": "ru",
}

GENERIC_ALT_RE = re.compile(r"^(image|figure|picture|graphic|photo)\s*\d*$", re.I)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, value, default)
        return default


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, value, default)
        return default


def parse_event(event: dict[str, Any]) -> dict[str, Any]:
    """Normalize the title-generator output payload."""
    body: Any = event.get("body", event)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {"raw_body": body}

    if isinstance(body, dict) and "Payload" in body and isinstance(body["Payload"], dict):
        body = body["Payload"].get("body", body["Payload"])

    if isinstance(body, str):
        body = json.loads(body)

    if not isinstance(body, dict):
        raise ValueError(f"Unsupported post-remediation event body: {type(body)!r}")

    bucket = body.get("bucket") or body.get("Bucket")
    save_path = body.get("save_path") or body.get("saved_path") or body.get("key")
    title = body.get("title") or body.get("Title")

    if not bucket or not save_path:
        raise ValueError(f"Post-remediation finalizer needs bucket and save_path. Event: {event}")

    return {"bucket": bucket, "save_path": save_path, "title": title, "input_body": body}


def download_s3(bucket: str, key: str, local_path: Path) -> None:
    logger.info("Downloading s3://%s/%s to %s", bucket, key, local_path)
    s3.download_file(bucket, key, str(local_path))


def upload_s3(local_path: Path, bucket: str, key: str, content_type: str = "application/pdf") -> None:
    logger.info("Uploading %s to s3://%s/%s", local_path, bucket, key)
    s3.upload_file(str(local_path), bucket, key, ExtraArgs={"ContentType": content_type})


def upload_json(data: dict[str, Any], bucket: str, key: str) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    logger.info("Uploading QA JSON to s3://%s/%s", bucket, key)
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json; charset=utf-8")


def extract_pdf_text(pdf_path: Path, max_chars: int = 25000) -> tuple[str, list[int]]:
    parts: list[str] = []
    page_counts: list[int] = []
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            text = page.get_text("text") or ""
            page_counts.append(len(text.strip()))
            if len("\n".join(parts)) < max_chars:
                parts.append(text)
    return "\n".join(parts)[:max_chars], page_counts


def ocr_quality_metrics(text: str) -> dict[str, Any]:
    stripped = text or ""
    total = len(stripped)
    if total == 0:
        return {
            "char_count": 0,
            "replacement_count": 0,
            "control_count": 0,
            "suspicious_symbol_count": 0,
            "word_count": 0,
            "bad_token_count": 0,
            "bad_token_ratio": 0.0,
            "bad_ocr_detected": False,
        }

    replacement_count = stripped.count("\ufffd")
    control_count = sum(
        1
        for ch in stripped
        if unicodedata.category(ch) in {"Cc", "Cf"} and ch not in "\n\r\t"
    )
    suspicious_symbol_count = sum(1 for ch in stripped if ch in "@#$%^*_~=�")
    words = re.findall(r"\S+", stripped)
    bad_tokens = []
    for word in words:
        if "\ufffd" in word:
            bad_tokens.append(word)
            continue
        if len(word) >= 4:
            bad_chars = sum(1 for ch in word if not (ch.isalnum() or ch in "-'’./:,;()[]#&+"))
            if bad_chars / max(len(word), 1) > 0.35:
                bad_tokens.append(word)
    word_count = len(words)
    bad_token_ratio = len(bad_tokens) / max(word_count, 1)
    suspicious_symbol_ratio = suspicious_symbol_count / max(total, 1)
    control_ratio = control_count / max(total, 1)

    bad_ocr_detected = bool(
        replacement_count > 0
        or control_count > 0
        or bad_token_ratio >= env_float("OCR_BAD_TEXT_THRESHOLD", 0.08)
        or suspicious_symbol_ratio >= 0.06
    )

    return {
        "char_count": total,
        "replacement_count": replacement_count,
        "control_count": control_count,
        "suspicious_symbol_count": suspicious_symbol_count,
        "suspicious_symbol_ratio": round(suspicious_symbol_ratio, 4),
        "word_count": word_count,
        "bad_token_count": len(bad_tokens),
        "bad_token_ratio": round(bad_token_ratio, 4),
        "sample_bad_tokens": bad_tokens[:20],
        "bad_ocr_detected": bad_ocr_detected,
    }


def normalize_lang_code(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = code.strip().replace("_", "-")
    return LANG_TO_PDF_LANG.get(cleaned.lower(), cleaned)


def local_language_detect(text: str) -> dict[str, Any]:
    sample = re.sub(r"\s+", " ", text or "").strip()[:8000]
    if not sample or len(sample) < 40:
        return {"source": "local", "language": None, "confidence": 0.0, "reason": "Not enough text."}

    if detect_langs is None:
        return {"source": "local", "language": None, "confidence": 0.0, "reason": "langdetect unavailable."}

    try:
        candidates = detect_langs(sample)
        if not candidates:
            return {"source": "local", "language": None, "confidence": 0.0, "reason": "No language candidates."}
        best = candidates[0]
        return {
            "source": "local",
            "language": normalize_lang_code(best.lang),
            "confidence": float(best.prob),
            "candidates": [{"language": normalize_lang_code(c.lang), "confidence": float(c.prob)} for c in candidates[:5]],
        }
    except Exception as exc:
        return {"source": "local", "language": None, "confidence": 0.0, "reason": str(exc)}


def bedrock_model_id(model_name: str) -> str:
    if model_name.startswith("arn:"):
        return model_name
    if model_name.startswith("us."):
        region = boto3.Session().region_name or os.environ.get("AWS_REGION", "us-west-2")
        account = boto3.client("sts").get_caller_identity()["Account"]
        return f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_name}"
    return model_name


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def bedrock_language_detect(text: str, local_result: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    model = os.environ.get("DOCUMENT_LANGUAGE_MODEL", "us.amazon.nova-lite-v1:0")
    model_id = bedrock_model_id(model)
    client = boto3.client("bedrock-runtime")
    sample = re.sub(r"\s+", " ", text or "").strip()[:12000]

    prompt = f"""
You are determining the primary document language for a PDF accessibility metadata field.
Return strict JSON only. Do not include markdown.

Task:
- Identify the primary language of the document as a BCP 47 language tag suitable for PDF /Lang.
- Use document-level primary language, not isolated quotations, names, captions, or code-switched phrases.
- If the text is too corrupt or confidence is low, return null language and needs_manual_review=true.

Local language guess:
{json.dumps(local_result, ensure_ascii=False)}

OCR quality metrics:
{json.dumps(quality, ensure_ascii=False)}

Text sample:
{sample}

Return JSON shape:
{{
  "language": "en-US" | "es" | "fr" | "zh-TW" | null,
  "confidence": 0.0,
  "other_languages_present": [],
  "needs_manual_review": true,
  "reason": "brief reason"
}}
""".strip()

    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 500, "temperature": 0},
    )
    output_text = response["output"]["message"]["content"][0]["text"]
    parsed = extract_json_object(output_text)
    parsed["source"] = "bedrock"
    parsed["model"] = model
    parsed["language"] = normalize_lang_code(parsed.get("language"))
    return parsed


def choose_document_language(text: str, quality: dict[str, Any]) -> dict[str, Any]:
    mode = os.environ.get("DOCUMENT_LANGUAGE_MODE", "auto").strip().lower()
    override = os.environ.get("DOCUMENT_LANGUAGE_OVERRIDE", "").strip()
    threshold = env_float("DOCUMENT_LANGUAGE_CONFIDENCE_THRESHOLD", 0.75)
    use_bedrock = env_bool("DOCUMENT_LANGUAGE_USE_BEDROCK", True)

    if mode == "off":
        return {"set_lang": False, "language": None, "source": "off", "confidence": 0.0, "needs_manual_review": True}

    if override:
        return {"set_lang": True, "language": normalize_lang_code(override), "source": "override", "confidence": 1.0, "needs_manual_review": False}

    local = local_language_detect(text)
    local_conf = float(local.get("confidence") or 0.0)

    if mode == "local" or (local.get("language") and local_conf >= threshold and not quality.get("bad_ocr_detected")):
        return {
            "set_lang": bool(local.get("language") and local_conf >= threshold),
            "language": local.get("language"),
            "source": "local",
            "confidence": local_conf,
            "needs_manual_review": not bool(local.get("language") and local_conf >= threshold),
            "local_result": local,
        }

    if use_bedrock:
        try:
            bedrock = bedrock_language_detect(text, local, quality)
            conf = float(bedrock.get("confidence") or 0.0)
            language = bedrock.get("language")
            bedrock["set_lang"] = bool(language and conf >= threshold and not bedrock.get("needs_manual_review"))
            bedrock["needs_manual_review"] = not bedrock["set_lang"]
            bedrock["local_result"] = local
            return bedrock
        except Exception as exc:
            logger.exception("Bedrock language detection failed: %s", exc)

    return {
        "set_lang": bool(local.get("language") and local_conf >= threshold),
        "language": local.get("language"),
        "source": "local_fallback",
        "confidence": local_conf,
        "needs_manual_review": not bool(local.get("language") and local_conf >= threshold),
        "local_result": local,
    }


def get_root_object(obj: Any) -> Any:
    return obj.get_object() if hasattr(obj, "get_object") else obj


def walk_struct(elem: Any, depth: int = 0) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        elem = get_root_object(elem)
    except Exception:
        return records

    if isinstance(elem, DictionaryObject):
        s_value = elem.get("/S")
        alt = elem.get("/Alt")
        actual = elem.get("/ActualText")
        if s_value:
            records.append({
                "type": str(s_value).lstrip("/"),
                "depth": depth,
                "alt": str(alt) if alt is not None else None,
                "actual_text": str(actual) if actual is not None else None,
            })
        kids = elem.get("/K")
        if isinstance(kids, list):
            for kid in kids:
                records.extend(walk_struct(kid, depth + 1))
        elif kids is not None and not isinstance(kids, (int, float)):
            records.extend(walk_struct(kids, depth + 1))
    return records


def analyze_structure(reader: PdfReader) -> dict[str, Any]:
    root = reader.trailer.get("/Root")
    root_obj = get_root_object(root)
    struct_root = root_obj.get("/StructTreeRoot") if isinstance(root_obj, DictionaryObject) else None
    mark_info = root_obj.get("/MarkInfo") if isinstance(root_obj, DictionaryObject) else None

    records: list[dict[str, Any]] = []
    if struct_root:
        struct = get_root_object(struct_root)
        kids = struct.get("/K") if isinstance(struct, DictionaryObject) else None
        if isinstance(kids, list):
            for kid in kids:
                records.extend(walk_struct(kid, 0))
        elif kids is not None:
            records.extend(walk_struct(kids, 0))

    counts: dict[str, int] = {}
    generic_alt_count = 0
    missing_alt_count = 0
    for rec in records:
        tag = rec["type"]
        counts[tag] = counts.get(tag, 0) + 1
        if tag.lower() == "figure":
            alt = (rec.get("alt") or "").strip()
            if not alt:
                missing_alt_count += 1
            elif GENERIC_ALT_RE.match(alt):
                generic_alt_count += 1

    marked = False
    try:
        marked = bool(get_root_object(mark_info).get("/Marked")) if mark_info else False
    except Exception:
        marked = False

    return {
        "has_struct_tree_root": bool(struct_root),
        "marked": marked,
        "tag_counts": counts,
        "figure_count": counts.get("Figure", 0),
        "figure_missing_alt_count": missing_alt_count,
        "figure_generic_alt_count": generic_alt_count,
        "total_structure_elements": len(records),
    }


def analyze_page_images(pdf_path: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_index, page in enumerate(doc, start=1):
            page_area = abs(page.rect.width * page.rect.height) or 1
            seen: set[tuple[int, tuple[float, float, float, float]]] = set()
            for image in page.get_images(full=True):
                xref = image[0]
                rects = page.get_image_rects(xref)
                for rect in rects:
                    rect_tuple = (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
                    key = (xref, rect_tuple)
                    if key in seen:
                        continue
                    seen.add(key)
                    area_ratio = abs(rect.width * rect.height) / page_area
                    if area_ratio >= 0.70:
                        classification = "full_page_scan_background"
                    elif area_ratio < 0.01:
                        classification = "small_decorative_or_noise"
                    else:
                        classification = "meaningful_candidate"
                    results.append({
                        "page": page_index,
                        "xref": xref,
                        "bbox": list(rect_tuple),
                        "area_ratio": round(area_ratio, 4),
                        "classification": classification,
                    })
    return results


def render_image_crop(pdf_path: Path, page_number: int, bbox: list[float], max_dim: int = 1400) -> bytes:
    with fitz.open(str(pdf_path)) as doc:
        page = doc[page_number - 1]
        rect = fitz.Rect(*bbox)
        zoom = min(3.0, max_dim / max(rect.width, rect.height, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
        return pix.tobytes("png")


def bedrock_alt_for_image(pdf_path: Path, item: dict[str, Any], nearby_text: str) -> dict[str, Any]:
    model = os.environ.get("AI_IMAGE_REVIEW_MODEL", os.environ.get("DOCUMENT_LANGUAGE_MODEL", "us.amazon.nova-lite-v1:0"))
    model_id = bedrock_model_id(model)
    client = boto3.client("bedrock-runtime")
    image_bytes = render_image_crop(pdf_path, int(item["page"]), item["bbox"])
    prompt = f"""
You are reviewing an image from a PDF for accessibility remediation.
Return strict JSON only. Do not include markdown.

Classify the image and write alt text only if it is meaningful.
Use nearby OCR text/captions when helpful.

Allowed classifications:
- full_page_scan_background: page scan where OCR text already carries the content; should usually be artifact.
- decorative: decorative/non-informative graphic; should usually be artifact.
- meaningful_figure: photo, chart, illustration, or image that conveys information.
- uncertain: cannot determine safely.

Nearby text/caption candidate:
{nearby_text[:2000]}

Return JSON:
{{
  "classification": "meaningful_figure" | "decorative" | "full_page_scan_background" | "uncertain",
  "confidence": 0.0,
  "alt_text": "" | "concise alt text",
  "caption_text": "" | "nearby caption text",
  "artifact_recommended": true,
  "needs_manual_review": true,
  "reason": "brief reason"
}}
""".strip()
    response = client.converse(
        modelId=model_id,
        messages=[{
            "role": "user",
            "content": [
                {"text": prompt},
                {"image": {"format": "png", "source": {"bytes": image_bytes}}},
            ],
        }],
        inferenceConfig={"maxTokens": 700, "temperature": 0},
    )
    output_text = response["output"]["message"]["content"][0]["text"]
    parsed = extract_json_object(output_text)
    parsed["source"] = "bedrock"
    parsed["model"] = model
    return parsed


def image_review(pdf_path: Path, text: str) -> dict[str, Any]:
    mode = os.environ.get("AI_IMAGE_REVIEW_MODE", "report").strip().lower()
    max_images = env_int("AI_IMAGE_REVIEW_MAX_IMAGES", 8)
    images = analyze_page_images(pdf_path)
    full_page = [i for i in images if i["classification"] == "full_page_scan_background"]
    candidates = [i for i in images if i["classification"] == "meaningful_candidate"]

    report: dict[str, Any] = {
        "mode": mode,
        "image_instances": len(images),
        "full_page_scan_background_count": len(full_page),
        "meaningful_candidate_count": len(candidates),
        "small_decorative_or_noise_count": len([i for i in images if i["classification"] == "small_decorative_or_noise"]),
        "items": images[:50],
        "ai_reviews": [],
    }

    if mode in {"off", "false", "none"}:
        return report

    # Only call Bedrock for non-full-page candidates, capped to avoid surprise cost.
    for item in candidates[:max_images]:
        try:
            review = bedrock_alt_for_image(pdf_path, item, text)
            report["ai_reviews"].append({"image": item, "review": review})
        except Exception as exc:
            logger.exception("AI image review failed for page %s xref %s: %s", item.get("page"), item.get("xref"), exc)
            report["ai_reviews"].append({"image": item, "error": str(exc), "needs_manual_review": True})
    return report



def should_update_alt_text(value: Any) -> bool:
    alt = str(value or "").strip()
    if not alt:
        return True
    return bool(GENERIC_ALT_RE.match(alt))


def struct_figure_elements(node: Any) -> list[DictionaryObject]:
    figures: list[DictionaryObject] = []

    try:
        node = get_root_object(node)
    except Exception:
        return figures

    if isinstance(node, DictionaryObject):
        tag = str(node.get("/S") or "").lstrip("/")
        if tag.lower() == "figure":
            figures.append(node)

        kids = node.get("/K")
        if isinstance(kids, list):
            for kid in kids:
                figures.extend(struct_figure_elements(kid))
        elif kids is not None and not isinstance(kids, (int, float)):
            figures.extend(struct_figure_elements(kids))

    return figures


def image_key(item: dict[str, Any]) -> tuple[Any, Any, str]:
    return (
        item.get("page"),
        item.get("xref"),
        json.dumps(item.get("bbox", []), sort_keys=True),
    )


def build_alt_decisions(image_report: dict[str, Any]) -> list[dict[str, Any]]:
    min_conf = env_float("AI_IMAGE_APPLY_MIN_CONFIDENCE", 0.60)

    review_by_key: dict[tuple[Any, Any, str], dict[str, Any]] = {}
    for entry in image_report.get("ai_reviews", []):
        item = entry.get("image") or {}
        review = entry.get("review") or {}
        review_by_key[image_key(item)] = review

    decisions: list[dict[str, Any]] = []

    for item in image_report.get("items", []):
        classification = item.get("classification")
        review = review_by_key.get(image_key(item), {})

        alt_text = ""
        source = "none"
        needs_manual_review = False

        if classification == "full_page_scan_background":
            alt_text = "Scanned page image; text content is available in the document text layer."
            source = "deterministic_full_page_scan"
        elif classification == "small_decorative_or_noise":
            alt_text = "Decorative image."
            source = "deterministic_decorative"
        elif classification == "meaningful_candidate":
            review_class = str(review.get("classification") or "").strip()
            review_conf = float(review.get("confidence") or 0.0)
            candidate_alt = str(review.get("alt_text") or "").strip()

            if review_class == "meaningful_figure" and candidate_alt and review_conf >= min_conf:
                alt_text = candidate_alt
                source = "bedrock"
            elif review_class in {"decorative", "full_page_scan_background"} and review_conf >= min_conf:
                alt_text = (
                    "Decorative image."
                    if review_class == "decorative"
                    else "Scanned page image; text content is available in the document text layer."
                )
                source = f"bedrock_{review_class}"
            else:
                needs_manual_review = True
                source = "manual_review_required"
        else:
            needs_manual_review = True
            source = "unknown_image_classification"

        decisions.append({
            "page": item.get("page"),
            "xref": item.get("xref"),
            "bbox": item.get("bbox"),
            "classification": classification,
            "alt_text": alt_text[:1000],
            "source": source,
            "needs_manual_review": needs_manual_review,
        })

    return decisions


def apply_image_alt_text_to_figures(writer: PdfWriter, image_report: dict[str, Any]) -> dict[str, Any]:
    mode = os.environ.get("AI_IMAGE_REVIEW_MODE", "report").strip().lower()

    result: dict[str, Any] = {
        "mode": mode,
        "attempted": False,
        "figure_count": 0,
        "decisions_count": 0,
        "updated_count": 0,
        "skipped_existing_specific_alt_count": 0,
        "skipped_no_decision_count": 0,
        "manual_review_count": 0,
        "notes": [],
    }

    if mode not in {"apply", "write", "true", "yes", "on"}:
        result["notes"].append("AI image review is not in apply mode; leaving PDF /Alt values unchanged.")
        return result

    root = writer._root_object
    struct_root = root.get("/StructTreeRoot")
    if not struct_root:
        result["notes"].append("No StructTreeRoot found; cannot update Figure /Alt values.")
        return result

    struct_root = get_root_object(struct_root)
    kids = struct_root.get("/K") if isinstance(struct_root, DictionaryObject) else None

    figures: list[DictionaryObject] = []
    if isinstance(kids, list):
        for kid in kids:
            figures.extend(struct_figure_elements(kid))
    elif kids is not None:
        figures.extend(struct_figure_elements(kids))

    decisions = build_alt_decisions(image_report)
    result["attempted"] = True
    result["figure_count"] = len(figures)
    result["decisions_count"] = len(decisions)

    for index, fig in enumerate(figures):
        if index >= len(decisions):
            result["skipped_no_decision_count"] += 1
            continue

        current_alt = fig.get("/Alt")
        if not should_update_alt_text(current_alt):
            result["skipped_existing_specific_alt_count"] += 1
            continue

        decision = decisions[index]
        alt_text = str(decision.get("alt_text") or "").strip()

        if not alt_text:
            result["manual_review_count"] += 1
            continue

        fig[NameObject("/Alt")] = TextStringObject(alt_text)
        result["updated_count"] += 1

        if decision.get("needs_manual_review"):
            result["manual_review_count"] += 1

    if len(figures) != len(decisions):
        result["notes"].append(
            f"Figure count ({len(figures)}) and image decision count ({len(decisions)}) differ; mapping is best-effort by order."
        )

    return result


def finalize_pdf(
    input_pdf: Path,
    output_pdf: Path,
    language_result: dict[str, Any],
    image_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reader = PdfReader(str(input_pdf))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    root = writer._root_object
    before_lang = root.get("/Lang")

    if language_result.get("set_lang") and language_result.get("language"):
        root.update({NameObject("/Lang"): TextStringObject(str(language_result["language"]))})

    if env_bool("FINALIZER_SET_TABS", True):
        tab_order = os.environ.get("PDF_TAB_ORDER", "S").strip() or "S"
        if not tab_order.startswith("/"):
            tab_order = f"/{tab_order}"
        for page in writer.pages:
            page[NameObject("/Tabs")] = NameObject(tab_order)

    alt_writeback = apply_image_alt_text_to_figures(writer, image_report or {})

    try:
        writer.create_viewer_preferences()
        writer.viewer_preferences.display_doctitle = True
    except Exception:
        root[NameObject("/ViewerPreferences")] = DictionaryObject({NameObject("/DisplayDocTitle"): BooleanObject(True)})

    with output_pdf.open("wb") as f:
        writer.write(f)

    verified = PdfReader(str(output_pdf))
    verified_root = verified.trailer["/Root"]

    tabs_values = []
    for page in verified.pages:
        tabs_values.append(str(page.get("/Tabs")) if page.get("/Tabs") is not None else None)

    verified_structure = analyze_structure(verified)

    return {
        "before_lang": str(before_lang) if before_lang is not None else None,
        "after_lang": str(verified_root.get("/Lang")) if verified_root.get("/Lang") is not None else None,
        "tabs_values_sample": tabs_values[:10],
        "all_pages_tabs_s": all(value == "/S" for value in tabs_values) if tabs_values else False,
        "image_alt_writeback": alt_writeback,
        "post_writeback_structure": verified_structure,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    info = parse_event(event)
    bucket = info["bucket"]
    save_path = info["save_path"]
    file_name = Path(save_path).name
    base = file_name.replace("COMPLIANT_", "", 1).replace(".pdf", "")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_pdf = tmp / file_name
        output_pdf = tmp / f"finalized_{file_name}"
        download_s3(bucket, save_path, input_pdf)

        text, page_counts = extract_pdf_text(input_pdf)
        quality = ocr_quality_metrics(text)
        language_result = choose_document_language(text, quality)
        images = image_review(input_pdf, text)
        finalize_result = finalize_pdf(input_pdf, output_pdf, language_result, images)

        # Analyze finalized file so QA reflects what users receive.
        finalized_reader = PdfReader(str(output_pdf))
        structure = analyze_structure(finalized_reader)
        # Image review already ran before finalization so the same decisions can be written into /Alt.

        qa = {
            "file": file_name,
            "bucket": bucket,
            "save_path": save_path,
            "page_count": len(finalized_reader.pages),
            "title": info.get("title"),
            "text_layer": {
                "page_text_char_counts": page_counts,
                "ocr_quality": quality,
            },
            "document_language": language_result,
            "pdf_finalizer": finalize_result,
            "structure": structure,
            "images": images,
            "needs_manual_review": bool(
                language_result.get("needs_manual_review")
                or quality.get("bad_ocr_detected")
                or structure.get("figure_generic_alt_count", 0) > 0
                or structure.get("figure_missing_alt_count", 0) > 0
                or images.get("meaningful_candidate_count", 0) > 0
            ),
        }

        upload_s3(output_pdf, bucket, save_path, "application/pdf")
        upload_json(qa, bucket, f"result/QA_{base}.json")

    body = dict(info.get("input_body", {}))
    body.update({
        "bucket": bucket,
        "save_path": save_path,
        "finalized": True,
        "qa_report": f"result/QA_{base}.json",
        "document_language": language_result.get("language"),
        "needs_manual_review": qa["needs_manual_review"],
    })
    return {"statusCode": 200, "body": body}
