# Approach and Assumptions

## Product intent

This repository implements a local-first decision-support workflow for TTB-style label review.
The backend extracts structured data from application PDFs and label imagery, then runs deterministic
validation and matching checks to produce `pass` or `needs_review` outcomes.

The system is designed for human review acceleration, not autonomous legal determination.

## Current architecture

- Frontend: React + TypeScript + Vite.
- Backend: FastAPI (Python).
- PDF extraction: `pypdf` + `pdfplumber` + `PyMuPDF` (for rendering/cropping and picker debug flows).
- Label OCR: local VLM/OCR adapter in `backend/app/vlm/client.py` (DeepSeek-OCR style interface, Ollama/OpenAI-compatible modes).
- Matching and scoring: deterministic logic in `backend/app/matching/reconcile.py`.
- Rule evaluation: deterministic checks in `backend/app/vlm/validation.py`.

## Why DeepSeek-OCR + deterministic PDF parsing?

I had conducted a relatively extensive experimentation with a number of open-source OCR and VLMs (Vision Language Models), including tesseract, PaddleOCR, Qwen2.5VL-7B, and DeepSeek-OCR, and a variety of approaches, including extracting all text, extracting specific fields, and leveraging VLMs to determine pass/fail based on a given set of criteria (with prompt-tuning).

I decided on utilizing DeepSeek-OCR to extract all of the text in label images, as it perfectly balanced speed (~5 second processing time) and accuracy (>90%) while remaining open-source, three key requirements for the project. In comparison, Tesseract and PaddleOCR's accuracy were horrendous, and Qwen2.5VL-7B took too long (~30 seconds) and did not offer superior accuracy compared to DeepSeek-OCR. That said, DeepSeek is notoriously difficult with custom instructions and isn't very reliable with recognizing markdown/formatting, so the major tradeoff is the lack of ability to detect whether the GOVERNMENT WARNING is in bolded formatting (though other checks, like whether the phrase is in all caps and verbatim, are able to be performed deterministically).

However, for the application PDF parsing, I decided to take a deterministic approach, as we can expect that the application formatting will always be the same, as applicants are required to use the standard form prescribed by TTB. VLMs and OCR are also not great at table-based PDF layouts, especially one where could be multiple boxes with different dimensions and placements within any given horizontal row of text across the page, which is how VLMs and OCRs traditionally process text. Hence, because the expected PDF formatting is deterministic, I decided to take a deterministic approach to the problem and custom drew bounding boxes for a simple PDF parser to parse the text (or checkmarks) within the given bounding box. Checkmarks were particularly difficult to work with due to the non-text-based nature of the shapes and their varying sizes. The current prototype does have some limitations on recognizing hand-drawn checkmarks if they do not cross the center of the box sufficiently.

## End-to-end verification flow (as implemented)

1. `POST /api/verify` receives only an `application_pdf`.
2. The backend always crops label pixels from a fixed PDF bounding box (`page=0, x0=26, y0=682, x1=586, y1=976`).
3. The backend runs in parallel:
   - application PDF parsing (`parse_application_pdf_async`),
   - label OCR/text extraction (`extract_label_text_with_local_vlm`).
4. PDF parsing extracts structured fields and validates required form constraints:
   - required text fields present (`brand_name`, `fanciful_name`, `bottler_name_address`),
   - exactly one source checkbox selected (`domestic` xor `imported`),
   - exactly one beverage checkbox selected (`wine` xor `distilled_spirits` xor `malt_beverages`).
5. If required PDF checks fail, the API returns a rejection payload (`{"result":"rejected","message":"..."}`).
6. OCR text is parsed into label evidence and evaluated against:
   - application-to-label match checks,
   - regulatory label presence/format checks.
7. Reconciliation scores are calculated (to allow for noise) for:
   - `brand_match_score`,
   - `address_match_score`,
   - per-field score map.
8. Final status is merged from finding results and returned as a `VerificationResult`.

## What the frontend currently does

- Main flow uploads one or more PDFs and calls `/api/verify` once per file.
- In the current UI path, label extraction uses a fixed PDF crop box from page 0 (no separate label upload in normal operation).
- Multi-file processing is handled client-side with bounded concurrency (`MAX_CONCURRENT = 3`).
- A debug page (`#debug`) exposes:
  - `POST /api/debug/pdf-table` for ROI/table overlay inspection (custom bounding box created specific to the TTB F 5100.31),
  - `POST /api/debug/pdf-picker` for coordinate selection.

## Data extraction approach

### Application PDF extraction

- Uses three complementary strategies:
  - full-text extraction (`pypdf`) for coarse signals,
  - fixed ROI extraction (`pdfplumber`) for known form regions/checkboxes,
  - layout-based anchor extraction for selected fields (`brand_name`, `bottler_name_address`).
- Beverage type is determined from checkbox state.
- Checkbox detection includes image-based handling for small ROIs and layout-object overlap fallback.

### Label text extraction

- OCR prompt is intentionally minimal and specific to the model (`"Free OCR."`).
- Image bytes are optionally resized/compressed before OCR (`VLM_IMAGE_*` environment controls).
- The client supports:
  - OpenAI-compatible `/v1/chat/completions` mode, or
  - Ollama-style `/api/chat` mode.

## Validation and matching rules (current)

The backend currently enforces deterministic pass/fail checks in two groups.

### 1) Application-to-label checks

- Brand name appears on label.
- Fanciful name appears on label.
- Grape varietals and wine appellation checks (required for wine flows).
- Bottler/importer address plus city/state presence and match.

### 2) Regulatory label checks

- Government warning exact body presence (normalized case/whitespace).
- Government warning heading in uppercase (`GOVERNMENT WARNING:`).
- Class/type present.
- ABV present and ABV format valid.
- Numerical net contents present with recognized units.
- Wine-specific ABV rule:
  - ABV >14% requires numerical ABV statement.
  - ABV 7-14% may pass with ABV statement or `table wine` / `light wine`.

Any failed required check drives overall status to `fail` (displayed as `needs_review` in the UI). The checks are intentionally limited for the prototype due to time and resource constraints. Adding all of the checks will also decrease performance, as some of the qualitative checks cannot be performed deterministically and would require the use of LLMs. However, the existing checks are intended to largely replicate what a TTB employee would check for within roughly the same amount of time.

## Reconciliation scoring approach

- Brand score: normalized string similarity (SequenceMatcher ratio).
- Address score:
  - parse components (street/city/state/postal code),
  - weighted similarity (`street` 0.45, `city` 0.2, `state` 0.15, `postal_code` 0.2),
  - fallback to full-string similarity when component parsing fails.
- Common abbreviations (`st`, `rd`, `ave`, etc.) are canonicalized before scoring.

## Runtime and configuration assumptions

- The backend auto-loads `backend/.env.local` on startup.
- OCR runtime must be reachable at `DEEPSEEK_OCR_BASE_URL` (defaults to `http://127.0.0.1:11434/v1`).
- No mandatory cloud dependency is required by the application path.
- API CORS is restricted to localhost-style origins for development.

## Known constraints and trade-offs

- Current PDF extraction is optimized for a specific TTB form layout and fixed ROIs, not arbitrary document families.
- OCR prompting is intentionally generic; field-level extraction quality depends on downstream regex/heuristic parsing.
- Regulatory logic is a focused prototype rule set, not exhaustive CFR codification.
- The frontend's main workflow currently assumes labels are affixed in predictable PDF regions (fixed crop defaults) and that the PDF is in legal 8.5" x 14" dimensions as required.
- Batch endpoint (`/api/verify/batch`) exists server-side, but the UI currently performs client-managed per-file calls to `/api/verify`.

## Tools used

Cursor, Ollama, Git