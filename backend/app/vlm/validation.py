from __future__ import annotations

import re
from typing import Any

from app.parsing.fields import parse_abv_value, parse_address_components, parse_label_fields
from app.schemas import (
    ApplicationEvidence,
    BeverageType,
    ComplianceFinding,
    FieldExtraction,
    LabelEvidence,
    ReviewStatus,
)

_APPLICATION_MATCH_FIELDS: tuple[str, ...] = (
    "brand_name",
    "fanciful_name",
    "grape_varietals",
    "wine_appellation",
    "bottler_name_address",
)
_REGULATORY_LABEL_FIELDS_BY_TYPE: dict[BeverageType, tuple[str, ...]] = {
    BeverageType.WINE: ("government_warning", "class_type", "abv", "net_contents"),
    BeverageType.DISTILLED_SPIRITS: ("government_warning", "class_type", "abv", "net_contents"),
    BeverageType.MALT_BEVERAGE: ("government_warning", "class_type", "abv", "net_contents"),
    BeverageType.UNKNOWN: ("government_warning",),
}

# Relaxed ABV: a line is valid if it contains a number followed by %, and both "alc" and "vol" on the same line.
_NUMERIC_PCT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
# Numerical net content: number + unit (mL, L, fl. oz., fl oz, oz, ounce(s), pint(s), quart(s), liter(s)).
_NET_CONTENTS_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:"
    r"ml|mL|l\b|L\b|"
    r"fl\.?\s*oz\.?|"
    r"\boz\.?|"
    r"ounces?|"
    r"pints?|"
    r"quarts?|"
    r"liters?"
    r")\b",
    re.IGNORECASE,
)
# "table wine" or "light wine" as class/type for optional ABV when wine is 7–14%.
_TABLE_OR_LIGHT_WINE_RE = re.compile(
    r"\b(?:table\s+wine|light\s+wine)\b",
    re.IGNORECASE,
)
_EXPECTED_GOVERNMENT_WARNING_TEXT = (
    "(1) According to the Surgeon General, women should not drink alcoholic beverages during pregnancy "
    "because of the risk of birth defects. (2) Consumption of alcoholic beverages impairs your ability "
    "to drive a car or operate machinery, and may cause health problems."
)


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.lower() in {
        "unknown",
        "n/a",
        "none",
        "null",
        "na",
        "missing",
        "not_detected",
        "undetected",
        "not found",
    }:
        return None
    return stripped


def _normalize_for_contains(value: str) -> str:
    """Normalize for substring/token matching: lowercase, collapse non-alphanumeric to space. Case-insensitive."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _line_has_abv_phrase(line: str) -> bool:
    """True if this line contains a number followed by %, and both 'alc' and 'vol' (case-insensitive)."""
    lower = line.lower()
    if "alc" not in lower or "vol" not in lower:
        return False
    return bool(_NUMERIC_PCT_RE.search(line))


def _abv_format_valid(raw_label_text: str, abv_value: str | None) -> bool:
    """Valid if any line contains a number followed by %, and both 'alc' and 'vol' on the same line."""
    for line in raw_label_text.splitlines():
        if _line_has_abv_phrase(line):
            return True
    if abv_value:
        for line in abv_value.splitlines():
            if _line_has_abv_phrase(line):
                return True
    return False


def _normalize_warning_text(value: str) -> str:
    """Normalize warning text for case/newline-insensitive exact matching."""
    return re.sub(r"\s+", " ", value).strip().lower()


def _government_warning_text_exact_match(raw_label_text: str) -> bool:
    expected = _normalize_warning_text(_EXPECTED_GOVERNMENT_WARNING_TEXT)
    actual = _normalize_warning_text(raw_label_text)
    return expected in actual


def _government_warning_heading_all_caps(raw_label_text: str) -> bool:
    return "GOVERNMENT WARNING:" in raw_label_text


def _net_contents_numerical_valid(net_contents_value: str | None, raw_label_text: str) -> bool:
    """Label must contain a numerical net content (e.g. mL, L, fl. oz., oz, ounce, pint, quart, liter)."""
    if net_contents_value and _NET_CONTENTS_RE.search(net_contents_value):
        return True
    if _NET_CONTENTS_RE.search(raw_label_text):
        return True
    return False


def _wine_abv_rule(
    beverage_type: BeverageType,
    abv_value: str | None,
    class_type: str | None,
    raw_label_text: str,
) -> str:
    """For wines >14% ABV numerical statement mandatory; for 7–14% optional if 'table wine' or 'light wine' on label."""
    if beverage_type != BeverageType.WINE:
        return "pass"
    abv_float = parse_abv_value(abv_value)
    if abv_float is None:
        abv_float = parse_abv_value(raw_label_text)
    has_table_or_light = bool(class_type and _TABLE_OR_LIGHT_WINE_RE.search(class_type))
    has_abv = bool(_normalize_optional_string(abv_value)) or _abv_format_valid(raw_label_text, abv_value)
    if abv_float is None:
        return "pass" if has_abv else "fail"
    if abv_float > 14:
        return "pass" if has_abv else "fail"
    if 7 <= abv_float <= 14:
        return "pass" if (has_abv or has_table_or_light) else "fail"
    return "pass" if has_abv else "fail"


def _bottler_importer_name_and_city_state_on_label_and_match(
    application: ApplicationEvidence,
    label_address: str | None,
    raw_label_text: str,
) -> bool:
    """Bottler or importer name and address (city and state) must appear on label and match application."""
    app_addr = _normalize_optional_string(application.bottler_name_address)
    if not app_addr:
        return False
    label_norm = _normalize_for_contains(raw_label_text)
    addr_norm = _normalize_for_contains(app_addr)
    if not addr_norm:
        return False
    if addr_norm not in label_norm:
        app_tokens = [t for t in addr_norm.split() if len(t) >= 2]
        if not app_tokens:
            return False
        hits = sum(1 for t in app_tokens if t in label_norm)
        if hits < max(2, len(app_tokens) // 2):
            return False
    app_components = parse_address_components(application.bottler_name_address)
    city_app = _normalize_optional_string(app_components.get("city"))
    state_app = _normalize_optional_string(app_components.get("state"))
    if city_app and _normalize_for_contains(city_app) not in label_norm:
        return False
    if state_app and _normalize_for_contains(state_app) not in label_norm:
        return False
    return True


def _is_application_value_present_on_label(
    field_name: str,
    application_value: str,
    label_values: dict[str, str],
    raw_label_text: str,
) -> bool:
    """Return True if the application value appears on the label. Matching is case-insensitive (e.g. all-caps label matches mixed-case application)."""
    app_norm = _normalize_for_contains(application_value)
    if not app_norm:
        return False
    label_raw_norm = _normalize_for_contains(raw_label_text)
    if app_norm in label_raw_norm:
        return True

    if field_name in {"brand_name", "class_type", "abv", "net_contents", "address", "bottler_name_address"}:
        label_field = "address" if field_name == "bottler_name_address" else field_name
        candidate = label_values.get(label_field, "")
        candidate_norm = _normalize_for_contains(candidate)
        if app_norm == candidate_norm or (app_norm and app_norm in candidate_norm):
            return True

    # Token overlap fallback for noisier extracted label text.
    app_tokens = [token for token in app_norm.split() if len(token) >= 3]
    if not app_tokens:
        return False
    token_hits = sum(1 for token in app_tokens if token in label_raw_norm)
    return token_hits >= max(1, min(2, len(app_tokens)))


def _infer_application_match_checks(
    application: ApplicationEvidence,
    *,
    brand_name: str | None,
    class_type: str | None,
    abv: str | None,
    net_contents: str | None,
    address: str | None,
    raw_label_text: str,
) -> dict[str, str]:
    """Application-to-label checks; capitalization is ignored (e.g. label all caps, application mixed case)."""
    label_values = {
        "brand_name": brand_name or "",
        "class_type": class_type or "",
        "abv": abv or "",
        "net_contents": net_contents or "",
        "address": address or "",
    }
    checks: dict[str, str] = {}
    app_brand = _normalize_optional_string(application.brand_name)
    checks["brand_name"] = (
        "pass" if app_brand and _is_application_value_present_on_label("brand_name", app_brand, label_values, raw_label_text) else "fail"
    )
    app_fanciful = _normalize_optional_string(application.fanciful_name)
    checks["fanciful_name"] = (
        "fail"
        if not app_fanciful
        else (
            "pass"
            if _is_application_value_present_on_label("fanciful_name", app_fanciful, label_values, raw_label_text)
            else "fail"
        )
    )
    app_varietal = _normalize_optional_string(application.grape_varietals)
    checks["grape_varietals"] = (
        "pass"
        if application.beverage_type != BeverageType.WINE
        else (
            "fail"
            if not app_varietal
            else (
                "pass"
                if _is_application_value_present_on_label("grape_varietals", app_varietal, label_values, raw_label_text)
                else "fail"
            )
        )
    )
    app_appellation = _normalize_optional_string(application.wine_appellation)
    checks["wine_appellation"] = (
        "pass"
        if application.beverage_type != BeverageType.WINE
        else (
            "fail"
            if not app_appellation
            else (
                "pass"
                if _is_application_value_present_on_label("wine_appellation", app_appellation, label_values, raw_label_text)
                else "fail"
            )
        )
    )
    # Bottler or importer name and address (city and state) must appear on label and match application.
    checks["bottler_name_address"] = (
        "pass"
        if _bottler_importer_name_and_city_state_on_label_and_match(
            application, label_values.get("address") or None, raw_label_text
        )
        else "fail"
    )
    return checks


def _infer_regulatory_label_checks(
    beverage_type: BeverageType,
    *,
    class_type: str | None,
    abv: str | None,
    net_contents: str | None,
    warning_value: str | None,
    raw_label_text: str = "",
) -> dict[str, str]:
    required = _REGULATORY_LABEL_FIELDS_BY_TYPE.get(beverage_type, ("government_warning",))
    checks: dict[str, str] = {}
    if "government_warning" in required:
        checks["government_warning"] = "pass" if _government_warning_text_exact_match(raw_label_text) else "fail"
        checks["government_warning_heading_caps"] = (
            "pass" if _government_warning_heading_all_caps(raw_label_text) else "fail"
        )
    if "class_type" in required:
        checks["class_type"] = "pass" if _normalize_optional_string(class_type) else "fail"
    if "abv" in required:
        checks["abv"] = "pass" if _normalize_optional_string(abv) else "fail"
    # Alcohol content must be in one of the allowed formats (e.g. "Alcohol 12% by volume", "12% Alc. By Vol.").
    if "abv" in required:
        checks["abv_format"] = "pass" if _abv_format_valid(raw_label_text, abv) else "fail"
    if "net_contents" in required:
        # Label must contain numerical net content (mL, L, fl. oz., oz, ounce, pint, quart, liter).
        checks["net_contents"] = (
            "pass" if _net_contents_numerical_valid(net_contents, raw_label_text) else "fail"
        )
    # Wine: >14% ABV requires numerical statement; 7–14% optional if "table wine" or "light wine" on label.
    if beverage_type == BeverageType.WINE:
        checks["wine_abv_mandatory"] = _wine_abv_rule(beverage_type, abv, class_type, raw_label_text)
    return checks


def _status_from_two_check_groups(
    application_match_checks: dict[str, str],
    regulatory_label_checks: dict[str, str],
) -> ReviewStatus:
    for field_name in _APPLICATION_MATCH_FIELDS:
        status = application_match_checks.get(field_name, "fail")
        if status == "fail":
            return ReviewStatus.FAIL
    for status in regulatory_label_checks.values():
        if status == "fail":
            return ReviewStatus.FAIL
    return ReviewStatus.PASS


# Human-readable titles for finding codes (for API/frontend display).
_FINDING_TITLES: dict[str, str] = {
    "brand_name_application_match": "Brand name matches application",
    "fanciful_name_application_match": "Fanciful name matches application",
    "grape_varietals_application_match": "Grape varietal on application and label (wine)",
    "wine_appellation_application_match": "Appellation of origin on label when in application (wine)",
    "bottler_name_address_application_match": "Bottler/importer name and address (city, state) on label and match application",
    "government_warning_regulatory_label_presence": "Government warning statement exactly matches required text (case/newline-insensitive)",
    "government_warning_heading_caps_regulatory_label_presence": "Government warning heading appears as uppercase 'GOVERNMENT WARNING:'",
    "class_type_regulatory_label_presence": "Class/type designation present",
    "abv_regulatory_label_presence": "Alcohol content present",
    "abv_format_regulatory_label_presence": "Alcohol content in allowed format (e.g. “Alcohol __% by volume” or “__% Alc. By Vol.”)",
    "net_contents_regulatory_label_presence": "Numerical net content (mL, L, fl. oz., oz, ounce, pint, quart, liter)",
    "wine_abv_mandatory_regulatory_label_presence": "Wine ABV rule (>14% mandatory; 7–14% optional if “table wine”/“light wine”)",
}


def _finding_message(cfr_section: str, code: str, status_value: str) -> str:
    title = _FINDING_TITLES.get(code)
    if title:
        return f"{title}: {status_value}"
    if cfr_section == "application_match":
        return f"{code.replace('_application_match', '')} application-to-label match: {status_value}"
    return f"{code.replace('_regulatory_label_presence', '')} regulatory check: {status_value}"


def _findings_from_two_check_groups(
    application_match_checks: dict[str, str],
    regulatory_label_checks: dict[str, str],
) -> list[ComplianceFinding]:
    findings: list[ComplianceFinding] = []
    for field_name in _APPLICATION_MATCH_FIELDS:
        status_value = application_match_checks.get(field_name, "fail")
        status = ReviewStatus.PASS if status_value == "pass" else ReviewStatus.FAIL
        code = f"{field_name}_application_match"
        findings.append(
            ComplianceFinding(
                cfr_part="vlm",
                cfr_section="application_match",
                code=code,
                status=status,
                message=_finding_message("application_match", code, status_value),
                confidence=0.82,
            )
        )
    for field_name, status_value in regulatory_label_checks.items():
        status = ReviewStatus.PASS if status_value == "pass" else ReviewStatus.FAIL
        code = f"{field_name}_regulatory_label_presence"
        findings.append(
            ComplianceFinding(
                cfr_part="vlm",
                cfr_section="regulatory_label_presence",
                code=code,
                status=status,
                message=_finding_message("regulatory_label_presence", code, status_value),
                confidence=0.82,
            )
        )
    return findings


def evaluate_vlm_text(
    application: ApplicationEvidence,
    vlm_text: str,
    *,
    side: str = "label",
) -> tuple[LabelEvidence, list[ComplianceFinding], ReviewStatus]:
    field_conf = 0.82
    parsed_label = parse_label_fields(vlm_text, confidence=field_conf)
    brand_name = _normalize_optional_string(parsed_label.brand_name.value)
    class_type = _normalize_optional_string(parsed_label.class_type.value)
    abv = _normalize_optional_string(parsed_label.abv.value)
    net_contents = _normalize_optional_string(parsed_label.net_contents.value)
    address = _normalize_optional_string(parsed_label.address.value)
    warning_value = _normalize_optional_string(parsed_label.government_warning.value)

    application_match_checks = _infer_application_match_checks(
        application,
        brand_name=brand_name,
        class_type=class_type,
        abv=abv,
        net_contents=net_contents,
        address=address,
        raw_label_text=vlm_text,
    )
    regulatory_label_checks = _infer_regulatory_label_checks(
        application.beverage_type,
        class_type=class_type,
        abv=abv,
        net_contents=net_contents,
        warning_value=warning_value,
        raw_label_text=vlm_text,
    )
    findings = _findings_from_two_check_groups(application_match_checks, regulatory_label_checks)
    status = _status_from_two_check_groups(application_match_checks, regulatory_label_checks)

    label = LabelEvidence(
        brand_name=FieldExtraction(
            value=brand_name,
            confidence=field_conf,
        ),
        class_type=FieldExtraction(
            value=class_type,
            confidence=field_conf,
        ),
        abv=FieldExtraction(
            value=abv,
            confidence=field_conf,
        ),
        net_contents=FieldExtraction(
            value=net_contents,
            confidence=field_conf,
        ),
        address=FieldExtraction(
            value=address,
            confidence=field_conf,
        ),
        government_warning=FieldExtraction(
            value=warning_value,
            confidence=0.9 if warning_value else 0.35,
        ),
        # Surface the literal VLM output to the frontend raw-text panel.
        raw_text=vlm_text,
        confidence_score=field_conf,
    )
    if not findings:
        findings = [
            ComplianceFinding(
                cfr_part="vlm",
                cfr_section="label_review",
                code="vlm_summary",
                status=status,
                message=f"{side.title()} label VLM review completed.",
                confidence=0.7,
            )
        ]
    return label, findings, status
