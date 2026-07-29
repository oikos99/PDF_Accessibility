from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

try:
    import boto3  # noqa: F401
except ModuleNotFoundError:
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda *args, **kwargs: object()
    sys.modules["boto3"] = boto3_stub

try:
    import fitz  # noqa: F401
except ModuleNotFoundError:
    sys.modules["fitz"] = types.ModuleType("fitz")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ocr = load_module(
    "opendataloader_autotag_processor",
    ROOT / "opendataloader-autotag-container" / "opendataloader_autotag_processor.py",
)
finalizer = load_module(
    "post_remediation_accessibility_checker",
    ROOT / "lambda" / "post-remediation-accessibility-checker" / "main.py",
)


class OcrPolicyTests(unittest.TestCase):
    def setUp(self):
        self.previous_force_on_bad_text = os.environ.get("OCR_FORCE_FALLBACK_ON_BAD_TEXT")
        os.environ["OCR_FORCE_FALLBACK_ON_BAD_TEXT"] = "false"

    def tearDown(self):
        if self.previous_force_on_bad_text is None:
            os.environ.pop("OCR_FORCE_FALLBACK_ON_BAD_TEXT", None)
        else:
            os.environ["OCR_FORCE_FALLBACK_ON_BAD_TEXT"] = self.previous_force_on_bad_text

    def test_clean_text_has_no_encoding_issue(self):
        metrics = ocr.text_quality_metrics("Requested for: résumé, 数学, and x^2 + y^2.")
        self.assertFalse(metrics["encoding_issue_detected"])

    def test_replacement_character_is_definite_encoding_issue(self):
        metrics = ocr.text_quality_metrics("�equested For")
        self.assertTrue(metrics["encoding_issue_detected"])
        self.assertTrue(metrics["bad_ocr_detected"])

    def test_suspicious_symbols_do_not_alone_force_encoding_fallback(self):
        metrics = ocr.text_quality_metrics("total = price * quantity # invoice")
        self.assertFalse(metrics["encoding_issue_detected"])

    def test_suspicious_redo_text_does_not_trigger_force_by_default(self):
        should_force, _ = ocr.force_fallback_decision(
            "redo",
            "",
            {"bad_ocr_detected": True, "encoding_issue_detected": False},
        )
        self.assertFalse(should_force)

    def test_encoding_warning_triggers_force_fallback(self):
        should_force, reason = ocr.force_fallback_decision(
            "redo",
            "Some text cannot be mapped to characters; consider using --force-ocr",
            {"bad_ocr_detected": False, "encoding_issue_detected": False},
        )
        self.assertTrue(should_force)
        self.assertTrue(reason["redo_warning_needs_force"])


class AltTextPolicyTests(unittest.TestCase):
    def setUp(self):
        self.previous_mode = os.environ.get("AI_IMAGE_REVIEW_MODE")
        os.environ["AI_IMAGE_REVIEW_MODE"] = "apply"

    def tearDown(self):
        if self.previous_mode is None:
            os.environ.pop("AI_IMAGE_REVIEW_MODE", None)
        else:
            os.environ["AI_IMAGE_REVIEW_MODE"] = self.previous_mode

    def test_scanned_page_uses_ai_description_for_non_text_visuals(self):
        item = {
            "page": 1,
            "xref": 7,
            "bbox": [0.0, 0.0, 612.0, 792.0],
            "classification": "full_page_scan_background",
        }
        report = {
            "items": [item],
            "ai_reviews": [{
                "image": item,
                "review": {
                    "classification": "full_page_with_meaningful_visuals",
                    "confidence": 0.94,
                    "alt_text": "Portrait of the author seated beside a typewriter.",
                },
            }],
        }

        decision = finalizer.build_alt_decisions(report)[0]
        self.assertEqual(decision["source"], "bedrock_scanned_page_visuals")
        self.assertEqual(decision["alt_text"], "Portrait of the author seated beside a typewriter.")
        self.assertFalse(decision["needs_manual_review"])

    def test_previous_scanned_page_placeholder_can_be_upgraded(self):
        self.assertTrue(
            finalizer.should_update_alt_text(
                "Scanned page image; text content is available in the document text layer."
            )
        )

    def test_figure_mapping_uses_page_not_global_image_order(self):
        writer = PdfWriter()
        page_one = writer.add_blank_page(width=612, height=792)
        page_two = writer.add_blank_page(width=612, height=792)

        figure_one = DictionaryObject({
            NameObject("/S"): NameObject("/Figure"),
            NameObject("/Pg"): page_one.indirect_reference,
            NameObject("/Alt"): TextStringObject("image 1"),
        })
        figure_two = DictionaryObject({
            NameObject("/S"): NameObject("/Figure"),
            NameObject("/Pg"): page_two.indirect_reference,
            NameObject("/Alt"): TextStringObject("image 2"),
        })
        struct_root = DictionaryObject({
            NameObject("/K"): ArrayObject([
                writer._add_object(figure_two),
                writer._add_object(figure_one),
            ])
        })
        writer._root_object[NameObject("/StructTreeRoot")] = writer._add_object(struct_root)

        report = {
            "items": [
                {"page": 1, "xref": 10, "bbox": [0, 0, 100, 100], "classification": "meaningful_candidate"},
                {"page": 2, "xref": 20, "bbox": [0, 0, 100, 100], "classification": "meaningful_candidate"},
            ],
            "ai_reviews": [
                {
                    "image": {"page": 1, "xref": 10, "bbox": [0, 0, 100, 100]},
                    "review": {"classification": "meaningful_figure", "confidence": 0.9, "alt_text": "Page one image."},
                },
                {
                    "image": {"page": 2, "xref": 20, "bbox": [0, 0, 100, 100]},
                    "review": {"classification": "meaningful_figure", "confidence": 0.9, "alt_text": "Page two image."},
                },
            ],
        }

        result = finalizer.apply_image_alt_text_to_figures(writer, report)

        self.assertEqual(result["page_mapped_count"], 2)
        self.assertEqual(str(figure_one["/Alt"]), "Page one image.")
        self.assertEqual(str(figure_two["/Alt"]), "Page two image.")


if __name__ == "__main__":
    unittest.main()
