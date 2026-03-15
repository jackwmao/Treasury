from __future__ import annotations

import re
import string
from typing import Dict, Optional, Tuple

from app.schemas import FieldExtraction, LabelEvidence

WARNING_BODY = (
    "according to the surgeon general, women should not drink alcoholic beverages "
    "during pregnancy because of the risk of birth defects. consumption of alcoholic "
    "beverages impairs your ability to drive a car or operate machinery, and may cause "
    "health problems."
)

ABV_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:alc(?:ohol)?\.?\s*/?\s*vol\.?)?", re.IGNORECASE)
NET_CONTENTS_RE = re.compile(
    r"(\d{1,4}\s?(?:ml|mL|l|L|fl\.?\s?oz|oz|liter|liters))",
    re.IGNORECASE,
)
CITY_STATE_RE = re.compile(r"\b[A-Za-z][A-Za-z .'-]+,\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\b")
CLASS_RE = re.compile(
    r"\b("
    r"chardonnay|cabernet|merlot|pinot|sauvignon|riesling|syrah|zinfandel|"
    r"whiskey|whisky|bourbon|rum|vodka|gin|tequila|brandy|liqueur|"
    r"ipa|lager|ale|stout|porter|pilsner|malt beverage|beer"
    r")\b",
    re.IGNORECASE,
)
COMPANY_HINT_RE = re.compile(
    r"\b(vintners|vineyards|winery|cellars|brewery|brewing|distillery|distilling|"
    r"llc|inc|co\.?|company|produced|bottled|imported)\b",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    table = str.maketrans("", "", string.punctuation)
    return " ".join(value.lower().translate(table).split())


def extract_first(regex: re.Pattern, text: str) -> Optional[str]:
    match = regex.search(text)
    return match.group(0).strip() if match else None


def _line_candidates(text: str, prefixes: Tuple[str, ...]) -> Optional[str]:
    for line in text.splitlines():
        raw = line.strip()
        lowered = raw.lower()
        if any(lowered.startswith(p) for p in prefixes):
            parts = raw.split(":", 1)
            return parts[1].strip() if len(parts) > 1 else raw
    return None


def _is_noise_line(line: str) -> bool:
    lowered = line.lower()
    if len(line) < 3:
        return True
    if re.search(r"\b(government warning|barcode|contains sulfites|www\.|http)\b", lowered):
        return True
    if re.search(r"^\d[\d\s\-%./]*$", line):
        return True
    return False


def _extract_brand(raw_text: str) -> Optional[str]:
    prefixed = _line_candidates(raw_text, ("brand", "brand name"))
    if prefixed:
        return prefixed

    best: Optional[str] = None
    best_score = -1
    for line in (ln.strip() for ln in raw_text.splitlines() if ln.strip()):
        if _is_noise_line(line) or CITY_STATE_RE.search(line) or CLASS_RE.search(line):
            continue
        if re.search(r"\d", line):
            continue
        words = line.split()
        if not words:
            continue
        score = 0
        if line.isupper():
            score += 3
        if 1 < len(words) <= 4:
            score += 2
        if len(words) == 1:
            score += 1
        if COMPANY_HINT_RE.search(line):
            score -= 1
        if score > best_score:
            best_score = score
            best = line
    return best


def _extract_class_type(raw_text: str) -> Optional[str]:
    prefixed = _line_candidates(raw_text, ("class", "class/type", "type"))
    if prefixed:
        return prefixed
    match = CLASS_RE.search(raw_text)
    return match.group(0).strip() if match else None


def _extract_address(raw_text: str) -> Optional[str]:
    prefixed = _line_candidates(raw_text, ("address", "bottled by", "produced by", "imported by"))
    if prefixed and prefixed.lower() not in {"unknown", "n/a"}:
        return prefixed

    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        if not CITY_STATE_RE.search(line):
            continue
        company = lines[idx - 1] if idx > 0 else ""
        if company and COMPANY_HINT_RE.search(company) and not _is_noise_line(company):
            return f"{company}, {line}"
        return line
    return None


def parse_label_fields(raw_text: str, confidence: float) -> LabelEvidence:
    brand = _extract_brand(raw_text)
    class_type = _extract_class_type(raw_text)
    address = _extract_address(raw_text)
    warning_line = " ".join(raw_text.splitlines())

    abv = extract_first(ABV_RE, raw_text)
    net_contents = extract_first(NET_CONTENTS_RE, raw_text)

    normalized_warning = normalize_text(warning_line)
    warning_conf = (
        0.9
        if normalize_text(WARNING_BODY) in normalized_warning
        or (
            "surgeon general" in normalized_warning
            and "pregnancy" in normalized_warning
            and "birth defects" in normalized_warning
        )
        else 0.4
    )
    warning_text = warning_line if warning_conf > 0.5 else None

    field_conf = max(0.3, confidence - 0.1)
    return LabelEvidence(
        brand_name=FieldExtraction(value=brand, confidence=field_conf),
        class_type=FieldExtraction(value=class_type, confidence=field_conf),
        abv=FieldExtraction(value=abv, confidence=field_conf if abv else 0.2),
        net_contents=FieldExtraction(
            value=net_contents, confidence=field_conf if net_contents else 0.2
        ),
        address=FieldExtraction(value=address, confidence=field_conf if address else 0.2),
        government_warning=FieldExtraction(value=warning_text, confidence=warning_conf),
        raw_text=raw_text,
        confidence_score=confidence,
        low_confidence=confidence < 0.65,
    )


def parse_abv_value(abv_text: Optional[str]) -> Optional[float]:
    if not abv_text:
        return None
    match = ABV_RE.search(abv_text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_address_components(value: Optional[str]) -> Dict[str, str]:
    if not value:
        return {}
    text = value.strip()
    chunks = [c.strip() for c in text.split(",") if c.strip()]
    components: Dict[str, str] = {}
    if chunks:
        components["street"] = chunks[0]
    if len(chunks) > 1:
        components["city"] = chunks[1]
    if len(chunks) > 2:
        state_zip = chunks[2].split()
        if state_zip:
            components["state"] = state_zip[0]
        if len(state_zip) > 1:
            components["postal_code"] = state_zip[-1]
    return components

