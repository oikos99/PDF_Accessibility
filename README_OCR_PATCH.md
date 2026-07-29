# OpenDataLoader OCRmyPDF/Tesseract Patch

This patch is derived for the `opendataloader-pdf2pdf` branch of `oikos99/PDF_Accessibility`.

It adds an OCR preflight step inside `opendataloader-autotag-container` before OpenDataLoader runs.

## New behavior

Default:

```text
OCR_MODE=auto
```

Flow:

```text
PDF chunk
→ PyMuPDF text-layer preflight
→ if text is usable, skip OCR
→ if one or more pages are low-text/image-only, run OCRmyPDF/Tesseract
→ if existing text looks corrupt, try redo OCR
→ only if redo still reports a definite character-encoding problem, use force OCR
→ feed OCRed PDF into OpenDataLoader
→ upload tagged PDF/artifacts using the existing S3 contract
```

Force OCR is therefore an emergency fallback, not the normal path. Broad suspicious-text
heuristics may trigger the non-rasterizing redo pass, but do not trigger force OCR by default.
Force output uses OCRmyPDF optimization level 1 and records its input/output size ratio in
`ocr_report.json`.

## Supported environment variables

```text
OCR_MODE=auto|always|off|redo|force
OCR_TEXT_THRESHOLD=20
OCR_LANGUAGE=eng+spa+fra
OCR_ON_FAILURE=fail|fallback
OCR_OPTIMIZE=1
OCR_BAD_TEXT_ACTION=redo|force|off
OCR_BAD_TEXT_THRESHOLD=0.08
OCR_FORCE_FALLBACK=true
OCR_FORCE_FALLBACK_ON_BAD_TEXT=false
```

Recommended first test:

```bash
export PDF_STACK_NAME="PDFAccessibilityOdlDev"
export TAGGING_ENGINE="opendataloader"
export OCR_MODE="auto"
export OCR_TEXT_THRESHOLD="20"
export OCR_LANGUAGE="eng+spa+fra"
export OCR_ON_FAILURE="fail"
export OCR_OPTIMIZE="1"
export OCR_FORCE_FALLBACK="true"
export OCR_FORCE_FALLBACK_ON_BAD_TEXT="false"
./deploy.sh
```

Choose PDF-to-PDF, then backend-only.

## Install/apply

From the repository root:

```bash
unzip opendataloader_ocr_patch.zip
python3 opendataloader_ocr_patch/apply_ocr_patch.py
python3 -m py_compile opendataloader-autotag-container/opendataloader_autotag_processor.py

git diff
git add app.py deploy.sh opendataloader-autotag-container/Dockerfile opendataloader-autotag-container/requirements.txt opendataloader-autotag-container/opendataloader_autotag_processor.py
git commit -m "Add OCRmyPDF preflight before OpenDataLoader"
git push origin opendataloader-pdf2pdf
```

Then pull/deploy from CloudShell.

## Notes

- `OCR_MODE=auto` avoids wasting compute on born-digital/text PDFs.
- `OCR_MODE=always` runs OCRmyPDF with `--skip-text`, so OCRmyPDF still skips pages that already contain text.
- `OCR_MODE=redo` is for PDFs with a bad existing OCR layer.
- `OCR_MODE=force` is advanced and can rasterize/reprocess existing content.
- OCR/OpenDataLoader settings are passed to the OpenDataLoader ECS task. The legacy alt-text
  container does not consume them.

## AI image alt text

The post-remediation finalizer reviews native image candidates and full-page scan images with
Bedrock when `AI_IMAGE_REVIEW_MODE=apply`.

- Native images receive concise AI alt text when model confidence meets the configured minimum.
- For a full-page scan, the model describes only meaningful non-text visuals such as portraits,
  charts, maps, or diagrams; OCR text is not repeated in the alt text.
- Figure decisions are matched by PDF page before `/Alt` is updated. Global order is used only
  when page references are absent and the remaining counts match exactly.
- When a meaningful visual is baked into a single scanned-page raster, its description is applied
  to that page's existing Figure tag. Creating a separate independently selectable Figure region
  would require reconstructing the page content and structure tree.

Relevant settings:

```text
AI_IMAGE_REVIEW_MODE=apply|report|off
AI_IMAGE_REVIEW_MODEL=us.amazon.nova-lite-v1:0
AI_IMAGE_REVIEW_MAX_IMAGES=8
AI_IMAGE_APPLY_MIN_CONFIDENCE=0.60
```
