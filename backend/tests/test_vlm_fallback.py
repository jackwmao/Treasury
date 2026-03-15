from app.schemas import ApplicationEvidence, BeverageType
from app.vlm.client import _vlm_label_compliance_prompt
from app.vlm.validation import (
    _findings_from_two_check_groups,
    _infer_application_match_checks,
)


def test_vlm_prompt_disallows_not_applicable() -> None:
    prompt = _vlm_label_compliance_prompt("label")
    assert prompt
    assert "not_applicable" not in prompt


def test_inferred_application_checks_never_emit_not_applicable() -> None:
    checks = _infer_application_match_checks(
        ApplicationEvidence(beverage_type=BeverageType.DISTILLED_SPIRITS),
        brand_name=None,
        class_type=None,
        abv=None,
        net_contents=None,
        address=None,
        raw_label_text="",
    )
    assert checks["fanciful_name"] == "fail"
    assert checks["grape_varietals"] == "pass"
    assert checks["wine_appellation"] == "pass"
    assert "not_applicable" not in checks.values()


def test_findings_treat_not_applicable_as_fail() -> None:
    findings = _findings_from_two_check_groups(
        {
            "brand_name": "pass",
            "fanciful_name": "not_applicable",
            "grape_varietals": "pass",
            "wine_appellation": "pass",
            "bottler_name_address": "pass",
        },
        {"government_warning": "pass", "abv": "pass", "net_contents": "pass"},
    )
    assert any(f.code == "fanciful_name_application_match" and f.status.value == "fail" for f in findings)
