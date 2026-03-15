from __future__ import annotations

import re
import string
from difflib import SequenceMatcher
from typing import Dict

from app.parsing.fields import parse_address_components
from app.schemas import ApplicationEvidence, LabelEvidence, ReconciliationResult

ABBREVIATIONS = {
    "st": "street",
    "rd": "road",
    "ave": "avenue",
    "blvd": "boulevard",
    "dr": "drive",
    "ln": "lane",
    "ct": "court",
}


def _canonical(value: str) -> str:
    lowered = value.lower().strip()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    tokens = [ABBREVIATIONS.get(t, t) for t in lowered.split()]
    return " ".join(tokens)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return float(SequenceMatcher(None, a, b).ratio())


def brand_match_score(app_brand: str | None, label_brand: str | None) -> float:
    if not app_brand or not label_brand:
        return 0.0
    return _ratio(_canonical(app_brand), _canonical(label_brand))


def address_match_score(app_address: str | None, label_address: str | None) -> float:
    if not app_address or not label_address:
        return 0.0

    app_components = parse_address_components(app_address)
    label_components = parse_address_components(label_address)
    if not app_components or not label_components:
        return _ratio(_canonical(app_address), _canonical(label_address))

    weights: Dict[str, float] = {
        "street": 0.45,
        "city": 0.2,
        "state": 0.15,
        "postal_code": 0.2,
    }
    score = 0.0
    for key, weight in weights.items():
        score += weight * _ratio(_canonical(app_components.get(key, "")), _canonical(label_components.get(key, "")))
    return score


def reconcile_documents(application: ApplicationEvidence, label: LabelEvidence) -> ReconciliationResult:
    brand = brand_match_score(application.brand_name, label.brand_name.value)
    address = address_match_score(application.bottler_name_address, label.address.value)
    field_scores = {
        "brand_name": brand,
        "bottler_name_address": address,
    }
    return ReconciliationResult(
        brand_match_score=brand,
        address_match_score=address,
        field_match_scores=field_scores,
    )

