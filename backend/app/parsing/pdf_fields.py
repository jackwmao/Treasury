from __future__ import annotations

import asyncio
import io
import logging
import html
import re
from dataclasses import dataclass
from typing import Any, Optional

from app import error_messages
from app.schemas import ApplicationEvidence, BeverageType

try:
    from pypdf import PdfReader
    from pypdf.errors import DependencyError, FileNotDecryptedError, PdfReadError
except Exception:  # pragma: no cover
    PdfReader = None
    DependencyError = Exception
    FileNotDecryptedError = Exception
    PdfReadError = Exception

try:
    import pdfplumber
except Exception:  # pragma: no cover
    pdfplumber = None

try:
    import fitz  # pymupdf
except Exception:  # pragma: no cover
    fitz = None

logger = logging.getLogger("uvicorn.error")

PDF_PICKER_RESOLUTION = 2.0  # pixels per PDF point (72 pt = 1 inch)


class ApplicationExtractionRejectedError(Exception):
    """Raised when extracted application PDF fields fail required validation."""


@dataclass(frozen=True)
class _TemplateFieldSpec:
    aliases: tuple[str, ...]
    max_row_multiplier: float = 1.8


# Fixed ROI bboxes (x0, top, x1, bottom) for TTB F 5100.31 application form
_ROI_SPECS: list[tuple[str, tuple[float, float, float, float], str]] = [
    ("domestic", (141, 129, 147, 134), "boolean"),
    ("imported", (200, 129, 206, 134), "boolean"),
    ("year", (22, 180, 54, 203), "year"),
    ("wine", (147, 170, 155, 178), "boolean"),
    ("distilled_spirits", (147, 181, 155, 189), "boolean"),
    ("malt_beverages", (147, 192, 155, 200), "boolean"),
    ("brand_name", (20, 215, 245, 228), "string"),
    ("fanciful_name", (20, 240, 245, 252), "string"),
    ("grape_varietals", (144, 264, 385, 285), "string"),
    ("wine_appellation", (20, 298, 385, 320), "string"),
    ("bottler_name_address", (249, 139, 592, 195), "string"),
]


def _extract_pdf_text(contents: bytes) -> str:
    if not PdfReader:
        return ""
    try:
        reader = PdfReader(io.BytesIO(contents))
    except DependencyError as exc:
        raise ValueError("Encrypted PDF requires cryptography dependency on the backend.") from exc
    except (FileNotDecryptedError, PdfReadError) as exc:
        raise ValueError("Unable to read PDF. Provide an unencrypted, readable application PDF.") from exc

    if getattr(reader, "is_encrypted", False):
        # Empty password handles many owner-encrypted PDFs.
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("Encrypted PDF is not supported without a password.") from exc
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _pick(line_prefixes: tuple[str, ...], text: str) -> Optional[str]:
    for line in text.splitlines():
        candidate = line.strip()
        lowered = candidate.lower()
        if any(lowered.startswith(prefix) for prefix in line_prefixes):
            parts = candidate.split(":", 1)
            return parts[1].strip() if len(parts) > 1 else candidate
    return None


def _is_data_like_line(line: str) -> bool:
    if not line or len(line) < 2:
        return False
    lowered = line.lower()
    if re.search(r"^\d[\d\s-]*$", line):
        return False
    if any(
        token in lowered
        for token in (
            "ttb",
            "for ttb use only",
            "certificate",
            "application",
            "label/bottle",
            "required",
            "date issued",
            "authorized signature",
            "part i",
            "part ii",
            "part iii",
        )
    ):
        return False
    return True


def _city_state_line(line: str) -> bool:
    return bool(re.search(r"[A-Za-z][A-Za-z .'-]+,\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?", line))


def _extract_application_fields_from_lines(text: str) -> tuple[Optional[str], Optional[str]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    data_lines = [ln for ln in lines if _is_data_like_line(ln)]

    bottler_name_address: Optional[str] = None
    brand_name: Optional[str] = None

    city_idx = -1
    for idx, line in enumerate(data_lines):
        if _city_state_line(line):
            city_idx = idx
            break

    if city_idx >= 0:
        city_line = data_lines[city_idx]
        company_line = data_lines[city_idx - 1] if city_idx > 0 else None
        if company_line and re.search(r"\b(vintners|winery|vineyards|cellars|brewery|distillery|llc|inc)\b", company_line, re.I):
            bottler_name_address = f"{company_line}, {city_line}"
        else:
            bottler_name_address = city_line

        if city_idx >= 2:
            candidate = data_lines[city_idx - 2]
            if len(candidate.split()) >= 2:
                brand_name = candidate
        if not brand_name and city_idx >= 1:
            brand_name = data_lines[city_idx - 1]

    return brand_name, bottler_name_address


def _normalize_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _clean_value_text(value: str) -> Optional[str]:
    cleaned = re.sub(r"\s+", " ", value).strip(" \t:.-")
    cleaned = re.sub(r"[_\u2014-]{2,}", " ", cleaned).strip()
    if not cleaned:
        return None
    if cleaned.lower() in {"n/a", "na", "none", "unknown"}:
        return None
    return cleaned


def _extract_text_from_bbox(page, bbox: tuple[float, float, float, float]) -> str:
    if not hasattr(page, "within_bbox"):
        return ""
    try:
        crop = page.within_bbox(bbox)
        return (crop.extract_text(x_tolerance=1, y_tolerance=1, layout=True) or "").strip()
    except Exception:
        return ""


def _rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """Check if two rects (x0, y0, x1, y1) overlap. y increases upward (pdfminer convention)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1)


def _overlap_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Return overlap area of two rects (x0, y0, x1, y1). Returns 0 if no overlap."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _checkbox_bbox_to_pdfminer(
    bbox: tuple[float, float, float, float],
    page_height: float,
) -> tuple[float, float, float, float]:
    """Convert pdfplumber bbox (x0, top, x1, bottom) to pdfminer (x0, y0, x1, y1)."""
    x0, top, x1, bottom = bbox
    y0 = page_height - bottom
    y1 = page_height - top
    return (x0, y0, x1, y1)


# Minimum overlap (as fraction of target bbox area) to count as a checkmark.
# Filters out grazing touches from borders or nearby text.
_CHECKBOX_MIN_OVERLAP_RATIO = 0.25

# For small checkboxes (domestic, imported): stricter rules to avoid false positives.
_SMALL_CHECKBOX_AREA_THRESHOLD = 35.0  # point²; domestic/imported are ~20-30
# Ignore LTChar (checkmarks are drawn as paths; text = labels bleeding in).
# Ignore elongated objects (form lines); checkmark strokes are compact.
_SMALL_CHECKBOX_MAX_OBJECT_DIM = 18.0  # points; checkmark strokes ~5-10; form lines 100+
_SMALL_CHECKBOX_IMAGE_SCALE = 8.0
_SMALL_CHECKBOX_EXPAND_PT = 0.6
_SMALL_CHECKBOX_INSET_RATIO = 0.18
_SMALL_CHECKBOX_DARK_THRESHOLD = 170
_SMALL_CHECKBOX_MIN_DARK_RATIO = 0.02
_SMALL_CHECKBOX_MIN_COMPONENT_RATIO = 0.008


def _is_checkbox_checked_small_image(contents: bytes, bbox: tuple[float, float, float, float]) -> Optional[bool]:
    """Image-based small-checkbox detection on interior pixels.

    Renders a tiny clip around the checkbox and checks for a coherent dark component
    in an inset interior region. This is more stable than layout-object overlap for
    very small checkbox ROIs.
    """
    if not fitz:
        return None

    x0, top, x1, bottom = bbox
    clip = fitz.Rect(
        x0 - _SMALL_CHECKBOX_EXPAND_PT,
        top - _SMALL_CHECKBOX_EXPAND_PT,
        x1 + _SMALL_CHECKBOX_EXPAND_PT,
        bottom + _SMALL_CHECKBOX_EXPAND_PT,
    )

    doc = None
    try:
        doc = fitz.open(stream=contents, filetype="pdf")
        if len(doc) == 0:
            return None
        page = doc[0]
        clip = clip.intersect(page.rect)
        if clip.is_empty:
            return None

        pix = page.get_pixmap(
            matrix=fitz.Matrix(_SMALL_CHECKBOX_IMAGE_SCALE, _SMALL_CHECKBOX_IMAGE_SCALE),
            clip=clip,
            alpha=False,
        )
        w, h, n = pix.width, pix.height, pix.n
        if w <= 2 or h <= 2 or n <= 0:
            return None

        inset_x = max(1, int(w * _SMALL_CHECKBOX_INSET_RATIO))
        inset_y = max(1, int(h * _SMALL_CHECKBOX_INSET_RATIO))
        if inset_x * 2 >= w:
            inset_x = max(0, w // 6)
        if inset_y * 2 >= h:
            inset_y = max(0, h // 6)
        x_start, x_end = inset_x, w - inset_x
        y_start, y_end = inset_y, h - inset_y
        if x_end - x_start < 2 or y_end - y_start < 2:
            return None

        samples = pix.samples
        interior_w = x_end - x_start
        interior_h = y_end - y_start
        interior_total = interior_w * interior_h
        if interior_total <= 0:
            return None

        dark_mask: list[bool] = [False] * interior_total
        dark_count = 0
        for iy in range(interior_h):
            y = y_start + iy
            for ix in range(interior_w):
                x = x_start + ix
                idx = (y * w + x) * n
                if n >= 3:
                    gray = (samples[idx] + samples[idx + 1] + samples[idx + 2]) // 3
                else:
                    gray = samples[idx]
                is_dark = gray < _SMALL_CHECKBOX_DARK_THRESHOLD
                pos = iy * interior_w + ix
                dark_mask[pos] = is_dark
                if is_dark:
                    dark_count += 1

        dark_ratio = dark_count / interior_total
        if dark_ratio < _SMALL_CHECKBOX_MIN_DARK_RATIO:
            return False

        # Require a coherent dark blob to avoid counting scattered anti-alias pixels.
        visited: list[bool] = [False] * interior_total
        largest_component = 0
        neighbors = (-1, 0, 1)
        for pos in range(interior_total):
            if visited[pos] or not dark_mask[pos]:
                continue
            stack = [pos]
            visited[pos] = True
            component_size = 0
            while stack:
                cur = stack.pop()
                component_size += 1
                cy, cx = divmod(cur, interior_w)
                for dy in neighbors:
                    ny = cy + dy
                    if ny < 0 or ny >= interior_h:
                        continue
                    for dx in neighbors:
                        nx = cx + dx
                        if dx == 0 and dy == 0:
                            continue
                        if nx < 0 or nx >= interior_w:
                            continue
                        nxt = ny * interior_w + nx
                        if visited[nxt] or not dark_mask[nxt]:
                            continue
                        visited[nxt] = True
                        stack.append(nxt)
            if component_size > largest_component:
                largest_component = component_size

        min_component = max(3, int(interior_total * _SMALL_CHECKBOX_MIN_COMPONENT_RATIO))
        return largest_component >= min_component
    except Exception:
        return None
    finally:
        if doc is not None:
            doc.close()


def _has_content_in_bbox_pdfminer(contents: bytes, bbox: tuple[float, float, float, float], page_height: float) -> bool:
    """Use pdfminer to detect checkmark content (char, line, curve) overlapping the bbox.
    Ignores LTRect (form borders) and requires meaningful overlap to avoid false positives.
    For small checkboxes: only LTLine/LTCurve (no text), and object must be compact."""
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTChar, LTCurve, LTLine, LTRect
    except ImportError:
        return False

    target = _checkbox_bbox_to_pdfminer(bbox, page_height)
    target_area = (target[2] - target[0]) * (target[3] - target[1])
    min_overlap = target_area * _CHECKBOX_MIN_OVERLAP_RATIO
    is_small = target_area < _SMALL_CHECKBOX_AREA_THRESHOLD

    def visit(obj, target_rect: tuple[float, float, float, float]) -> bool:
        if hasattr(obj, "bbox") and obj.bbox is not None:
            if isinstance(obj, LTRect):
                pass  # skip form borders
            elif isinstance(obj, (LTChar, LTLine, LTCurve)):
                # Small checkboxes: ignore text (LTChar); only drawing primitives
                if is_small and isinstance(obj, LTChar):
                    pass
                elif _rects_overlap(obj.bbox, target_rect):
                    overlap = _overlap_area(obj.bbox, target_rect)
                    if overlap >= min_overlap:
                        if is_small:
                            # Filter elongated objects (form lines, underlines)
                            ow = obj.bbox[2] - obj.bbox[0]
                            oh = obj.bbox[3] - obj.bbox[1]
                            if max(ow, oh) > _SMALL_CHECKBOX_MAX_OBJECT_DIM:
                                return False
                            # Object must be mostly inside: overlap >= 25% of object area
                            obj_area = ow * oh
                            if obj_area > 0 and overlap < obj_area * 0.25:
                                return False
                        return True
        if isinstance(obj, (str, bytes)):
            return False
        try:
            for child in obj:
                if visit(child, target_rect):
                    return True
        except (TypeError, AttributeError):
            pass
        return False

    for page_layout in extract_pages(io.BytesIO(contents)):
        if visit(page_layout, target):
            return True
        break
    return False


def _is_checkbox_checked(page, bbox: tuple[float, float, float, float], contents: bytes) -> bool:
    """True if there is any layout object (char, line, curve, rect) overlapping the bbox; False if blank."""
    x0, top, x1, bottom = bbox
    bbox_area = max(0.0, x1 - x0) * max(0.0, bottom - top)
    if bbox_area < _SMALL_CHECKBOX_AREA_THRESHOLD:
        image_decision = _is_checkbox_checked_small_image(contents, bbox)
        if image_decision is not None:
            return image_decision

    page_height = float(getattr(page, "height", 792))
    return _has_content_in_bbox_pdfminer(contents, bbox, page_height)


def _extract_roi_fields_and_text(contents: bytes) -> tuple[dict[str, str], dict[str, bool], str]:
    if not pdfplumber:
        return {}, {}, ""

    extracted: dict[str, str] = {}
    extracted_bool: dict[str, bool] = {}
    raw_lines: list[str] = []
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        if not pdf.pages:
            return {}, {}, ""
        page = pdf.pages[0]

        for field_key, bbox, field_type in _ROI_SPECS:
            text = _extract_text_from_bbox(page, bbox)
            if field_type == "boolean":
                checked = _is_checkbox_checked(page, bbox, contents)
                extracted_bool[field_key] = checked
                raw_lines.append(f"{field_key}: {checked}")
                if checked:
                    if field_key == "domestic":
                        extracted["source_of_product"] = "Domestic"
                    elif field_key == "imported":
                        extracted["source_of_product"] = "Imported"
            else:
                raw_lines.append(f"{field_key}: {repr(text)}")
            if field_type == "string":
                cleaned = _clean_value_text(text)
                if cleaned:
                    if field_key == "brand_name":
                        extracted["brand_name"] = cleaned
                    elif field_key == "fanciful_name":
                        extracted["fanciful_name"] = cleaned
                    elif field_key == "grape_varietals":
                        extracted["grape_varietals"] = cleaned
                    elif field_key == "wine_appellation":
                        extracted["wine_appellation"] = cleaned
                    elif field_key == "bottler_name_address":
                        extracted["bottler_name_address"] = cleaned

    return extracted, extracted_bool, "\n".join(raw_lines)


def _normalize_required_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _validate_required_pdfplumber_fields(fields: dict[str, Any], bool_fields: dict[str, bool]) -> None:
    required_text_number_fields: tuple[tuple[str, str], ...] = (
        ("brand_name", "Brand Name"),
        ("fanciful_name", "Fanciful Name"),
        ("bottler_name_address", "Bottler Name / Address"),
    )
    missing_labels: list[str] = []
    reject_reasons: list[str] = []
    for key, label in required_text_number_fields:
        normalized_value = _normalize_required_text(fields.get(key))
        if not normalized_value:
            missing_labels.append(label)
            continue
        fields[key] = normalized_value
    if missing_labels:
        reject_reasons.append(
            error_messages.PDF_REQUIRED_FIELDS_EMPTY.format(fields=", ".join(missing_labels))
        )

    source_checked = sum(1 for key in ("domestic", "imported") if bool_fields.get(key) is True)
    if source_checked != 1:
        reject_reasons.append(error_messages.PDF_SOURCE_CHECKBOX_INVALID)

    beverage_checked = sum(
        1
        for key in ("wine", "distilled_spirits", "malt_beverages")
        if bool_fields.get(key) is True
    )
    if beverage_checked != 1:
        reject_reasons.append(error_messages.PDF_BEVERAGE_CHECKBOX_INVALID)

    if reject_reasons:
        raise ApplicationExtractionRejectedError("\n".join(reject_reasons))




def debug_application_pdf_table(contents: bytes) -> dict[str, Any]:
    if not pdfplumber:
        return {"error": "pdfplumber not available"}

    extracted, extracted_bool, raw_text = _extract_roi_fields_and_text(contents)
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        if not pdf.pages:
            return {"error": "empty pdf", "extracted": extracted, "extracted_bool": extracted_bool, "raw_text": raw_text}
        page = pdf.pages[0]
        pw, ph = float(page.width), float(page.height)

        svg_lines = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(pw)}" height="{int(ph)}" viewBox="0 0 {pw} {ph}">',
            f'<rect x="0" y="0" width="{pw}" height="{ph}" fill="white" stroke="#222" stroke-width="1"/>',
        ]
        for idx, (field_key, (x0, top, x1, bottom), _) in enumerate(_ROI_SPECS):
            w, h = max(0.0, x1 - x0), max(0.0, bottom - top)
            svg_lines.append(
                f'<rect x="{x0:.2f}" y="{top:.2f}" width="{w:.2f}" height="{h:.2f}" fill="#ff5b5b22" stroke="#d33" stroke-width="0.7"/>'
            )
            svg_lines.append(
                f'<text x="{x0 + 1.5:.2f}" y="{top + 8:.2f}" font-size="6" fill="#9a1d1d">{field_key}</text>'
            )
        svg_lines.append("</svg>")

        return {
            "page_width": pw,
            "page_height": ph,
            "vertical_lines": [],
            "horizontal_lines": [],
            "cell_count": len(_ROI_SPECS),
            "table_count": 0,
            "table_preview": [],
            "extracted": extracted,
            "extracted_bool": extracted_bool,
            "raw_text": raw_text,
            "svg_overlay": html.unescape("\n".join(svg_lines)),
        }


def render_pdf_page_for_picker(contents: bytes, page_index: int = 0) -> dict[str, Any]:
    """Render a PDF page to PNG for coordinate picking. Returns image_base64, page dimensions, and pixel dimensions."""
    import base64

    if not fitz:
        return {"error": "pymupdf (fitz) not available; install with: pip install pymupdf"}

    try:
        doc = fitz.open(stream=contents, filetype="pdf")
    except Exception as exc:
        return {"error": f"Could not open PDF: {exc}"}

    if page_index < 0 or page_index >= len(doc):
        doc.close()
        return {"error": f"Page index {page_index} out of range (0..{len(doc) - 1})"}

    page = doc[page_index]
    rect = page.rect
    page_width_pt = float(rect.width)
    page_height_pt = float(rect.height)

    scale = PDF_PICKER_RESOLUTION
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")
    doc.close()

    img_w = pix.width
    img_h = pix.height

    return {
        "page_width": round(page_width_pt, 2),
        "page_height": round(page_height_pt, 2),
        "image_width": img_w,
        "image_height": img_h,
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
    }


def extract_pdf_region_as_png(
    contents: bytes,
    *,
    page_index: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    scale: float = PDF_PICKER_RESOLUTION,
) -> bytes:
    """Crop a PDF region and return PNG bytes for OCR input.

    Coordinates are PDF points in pdfplumber-like space: (x0, top, x1, bottom).
    """
    if not fitz:
        raise ValueError("pymupdf (fitz) not available; install with: pip install pymupdf")

    try:
        doc = fitz.open(stream=contents, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not open PDF: {exc}") from exc

    try:
        if page_index < 0 or page_index >= len(doc):
            raise ValueError(f"Page index {page_index} out of range (0..{len(doc) - 1})")

        page = doc[page_index]
        rect = page.rect
        page_width_pt = float(rect.width)
        page_height_pt = float(rect.height)

        # Clamp coordinates to page bounds and enforce a non-empty rectangle.
        cx0 = max(0.0, min(float(x0), page_width_pt))
        cx1 = max(0.0, min(float(x1), page_width_pt))
        cy0 = max(0.0, min(float(y0), page_height_pt))
        cy1 = max(0.0, min(float(y1), page_height_pt))
        if cx1 <= cx0 or cy1 <= cy0:
            raise ValueError("Invalid bbox: ensure x1>x0 and y1>y0 within page bounds.")

        clip = fitz.Rect(cx0, cy0, cx1, cy1)
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _apply_structured_fields(
    parsed: ApplicationEvidence,
    fields: dict[str, str],
    bool_fields: dict[str, bool] | None = None,
) -> None:
    if bool_fields:
        for key in ("domestic", "imported", "wine", "distilled_spirits", "malt_beverages"):
            if key in bool_fields:
                setattr(parsed, key, bool_fields[key])
        if bool_fields.get("wine") is True:
            parsed.beverage_type = BeverageType.WINE
        elif bool_fields.get("distilled_spirits") is True:
            parsed.beverage_type = BeverageType.DISTILLED_SPIRITS
        elif bool_fields.get("malt_beverages") is True:
            parsed.beverage_type = BeverageType.MALT_BEVERAGE
    if fields.get("source_of_product"):
        parsed.source_of_product = fields["source_of_product"]
    if fields.get("brand_name"):
        parsed.brand_name = fields["brand_name"]
    if fields.get("fanciful_name"):
        parsed.fanciful_name = fields["fanciful_name"]
    if fields.get("bottler_name_address"):
        parsed.bottler_name_address = fields["bottler_name_address"]
    if fields.get("grape_varietals"):
        parsed.grape_varietals = fields["grape_varietals"]
    if fields.get("wine_appellation"):
        parsed.wine_appellation = fields["wine_appellation"]


def _extract_field_from_anchor_line(
    words: list[dict],
    line_words: list[dict],
    anchor: dict,
    alias_token_count: int,
    page_width: float,
    page_height: float,
    max_row_multiplier: float,
) -> Optional[str]:
    alias_count = max(1, alias_token_count)
    alias_end_idx = anchor["line_word_idx"] + alias_count - 1
    if alias_end_idx < anchor["line_word_idx"] or alias_end_idx >= len(line_words):
        alias_end_idx = anchor["line_word_idx"]

    alias_end_word = line_words[alias_end_idx]
    row_height = max(6.0, float(anchor["line_bottom"] - anchor["line_top"]))
    x_left = max(0.0, float(alias_end_word["x1"]) + page_width * 0.01)
    x_right = page_width * 0.99
    y_top = max(0.0, float(anchor["line_top"]) - row_height * 0.3)
    y_bottom = min(page_height, float(anchor["line_bottom"]) + row_height * max_row_multiplier)

    captured: list[dict] = []
    for word in words:
        if float(word["x0"]) < x_left or float(word["x0"]) > x_right:
            continue
        if float(word["top"]) < y_top or float(word["bottom"]) > y_bottom:
            continue
        captured.append(word)

    if not captured:
        return None

    captured.sort(key=lambda item: (float(item["top"]), float(item["x0"])))
    text = " ".join(str(item["text"]).strip() for item in captured if str(item["text"]).strip())
    return _clean_value_text(text)


def _extract_layout_fields(contents: bytes) -> dict[str, str]:
    if not pdfplumber:
        return {}

    field_specs: dict[str, _TemplateFieldSpec] = {
        "brand_name": _TemplateFieldSpec(("brand name", "brand")),
        "bottler_name_address": _TemplateFieldSpec(
            ("name and address", "name & address", "address"),
            max_row_multiplier=3.2,
        ),
    }

    line_tolerance = 3.5
    extracted: dict[str, str] = {}
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(
                x_tolerance=2,
                y_tolerance=2,
                keep_blank_chars=False,
                use_text_flow=True,
            )
            if not words:
                continue
            sorted_words = sorted(words, key=lambda item: (float(item["top"]), float(item["x0"])))
            lines: list[dict] = []
            for word in sorted_words:
                text = str(word.get("text", "")).strip()
                if not text:
                    continue
                top = float(word["top"])
                bottom = float(word["bottom"])
                if lines and abs(lines[-1]["top"] - top) <= line_tolerance:
                    lines[-1]["words"].append(word)
                    lines[-1]["top"] = min(lines[-1]["top"], top)
                    lines[-1]["bottom"] = max(lines[-1]["bottom"], bottom)
                else:
                    lines.append({"top": top, "bottom": bottom, "words": [word]})

            for line in lines:
                line["words"].sort(key=lambda item: float(item["x0"]))
                normalized_words = [_normalize_for_match(str(item["text"])) for item in line["words"]]
                token_stream: list[tuple[str, int]] = []
                for word_idx, normalized in enumerate(normalized_words):
                    for token in normalized.split():
                        token_stream.append((token, word_idx))

                for field_name, spec in field_specs.items():
                    if field_name in extracted:
                        continue
                    for alias in spec.aliases:
                        alias_tokens = _normalize_for_match(alias).split()
                        token_count = len(alias_tokens)
                        if token_count == 0 or len(token_stream) < token_count:
                            continue
                        for idx in range(0, len(token_stream) - token_count + 1):
                            window = [token for token, _ in token_stream[idx : idx + token_count]]
                            if window != alias_tokens:
                                continue
                            anchor_word_idx = token_stream[idx][1]
                            anchor = {
                                "line_top": line["top"],
                                "line_bottom": line["bottom"],
                                "line_word_idx": anchor_word_idx,
                            }
                            value = _extract_field_from_anchor_line(
                                words=sorted_words,
                                line_words=line["words"],
                                anchor=anchor,
                                alias_token_count=token_count,
                                page_width=float(page.width),
                                page_height=float(page.height),
                                max_row_multiplier=spec.max_row_multiplier,
                            )
                            if value:
                                extracted[field_name] = value
                                break
                        if field_name in extracted:
                            break

            if {"brand_name", "bottler_name_address"} <= set(extracted.keys()):
                break
    return extracted

def infer_beverage_type(text: str) -> BeverageType:
    lowered = text.lower()
    if "wine" in lowered:
        return BeverageType.WINE
    if "distilled spirit" in lowered or "bourbon" in lowered or "whiskey" in lowered:
        return BeverageType.DISTILLED_SPIRITS
    if "malt beverage" in lowered or "beer" in lowered or "near beer" in lowered:
        return BeverageType.MALT_BEVERAGE
    return BeverageType.UNKNOWN


def parse_application_text(text: str) -> ApplicationEvidence:
    brand_guess, bottler_name_address_guess = _extract_application_fields_from_lines(text)

    return ApplicationEvidence(
        brand_name=_pick(("brand", "brand name"), text) or brand_guess,
        bottler_name_address=_pick(("address", "name and address"), text) or bottler_name_address_guess,
        beverage_type=infer_beverage_type(text),
        raw_text=text,
    )


def parse_application_pdf(contents: bytes) -> ApplicationEvidence:
    text = _extract_pdf_text(contents)
    parsed = parse_application_text(text)
    roi_fields, roi_bool, table_raw_text = _extract_roi_fields_and_text(contents)
    _validate_required_pdfplumber_fields(roi_fields, roi_bool)
    layout_fields = _extract_layout_fields(contents)

    if layout_fields.get("brand_name"):
        parsed.brand_name = layout_fields["brand_name"]
    if layout_fields.get("bottler_name_address"):
        parsed.bottler_name_address = layout_fields["bottler_name_address"]
    _apply_structured_fields(parsed, roi_fields, roi_bool)
    parsed.raw_pdfplumber_text = table_raw_text
    return parsed


async def parse_application_pdf_async(contents: bytes, use_vlm: bool = False) -> ApplicationEvidence:
    logger.info("PDF parse start source=pdfplumber")
    text_task = asyncio.to_thread(_extract_pdf_text, contents)
    table_task = asyncio.to_thread(_extract_roi_fields_and_text, contents)
    layout_task = asyncio.to_thread(_extract_layout_fields, contents)
    text, (roi_fields, roi_bool, table_raw_text), layout_fields = await asyncio.gather(
        text_task,
        table_task,
        layout_task,
    )

    parsed = parse_application_text(text)
    _validate_required_pdfplumber_fields(roi_fields, roi_bool)
    if layout_fields.get("brand_name"):
        parsed.brand_name = layout_fields["brand_name"]
    if layout_fields.get("bottler_name_address"):
        parsed.bottler_name_address = layout_fields["bottler_name_address"]
    _apply_structured_fields(parsed, roi_fields, roi_bool)
    parsed.raw_pdfplumber_text = table_raw_text

    if use_vlm:
        logger.info("Application VLM fallback is removed; using parsed PDF fields only.")
    return parsed

