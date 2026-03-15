import io
import asyncio

import pytest
from pypdf import PdfWriter

from app.parsing import pdf_fields
from app.parsing.pdf_fields import (
    ApplicationExtractionRejectedError,
    parse_application_pdf,
    parse_application_pdf_async,
    parse_application_text,
)
from app.schemas import BeverageType


def test_parse_application_pdf_rejects_invalid_pdf():
    with pytest.raises(ValueError):
        parse_application_pdf(b"not-a-real-pdf")


def test_parse_application_text_extracts_core_fields_from_tail_block():
    text = """
    ... static form text omitted ...
    Lighthouse
    Stormchaser White
    Lighthouse Vintners
    Kingston, NY
    Chardonnay
    Hudson River Region
    (123) 123-1234 hello@abc.com
    """
    evidence = parse_application_text(text)
    assert evidence.brand_name == "Stormchaser White"
    assert evidence.bottler_name_address == "Lighthouse Vintners, Kingston, NY"


def test_parse_application_pdf_prefers_layout_extraction_for_template_form(monkeypatch):
    class _FakePage:
        width = 612
        height = 792

        @staticmethod
        def extract_words(**kwargs):
            return [
                {"text": "Brand", "x0": 20, "x1": 60, "top": 50, "bottom": 62},
                {"text": "Name", "x0": 62, "x1": 100, "top": 50, "bottom": 62},
                {"text": "Stormchaser", "x0": 260, "x1": 340, "top": 50, "bottom": 62},
                {"text": "White", "x0": 343, "x1": 390, "top": 50, "bottom": 62},
                {"text": "Class/Type", "x0": 20, "x1": 110, "top": 80, "bottom": 92},
                {"text": "Chardonnay", "x0": 260, "x1": 350, "top": 80, "bottom": 92},
                {"text": "Name", "x0": 20, "x1": 50, "top": 110, "bottom": 122},
                {"text": "and", "x0": 55, "x1": 78, "top": 110, "bottom": 122},
                {"text": "Address", "x0": 82, "x1": 130, "top": 110, "bottom": 122},
                {"text": "Lighthouse", "x0": 260, "x1": 330, "top": 110, "bottom": 122},
                {"text": "Vintners", "x0": 334, "x1": 395, "top": 110, "bottom": 122},
                {"text": "Kingston,", "x0": 260, "x1": 320, "top": 126, "bottom": 138},
                {"text": "NY", "x0": 324, "x1": 345, "top": 126, "bottom": 138},
                {"text": "Alcohol", "x0": 20, "x1": 70, "top": 160, "bottom": 172},
                {"text": "by", "x0": 73, "x1": 86, "top": 160, "bottom": 172},
                {"text": "Volume", "x0": 90, "x1": 145, "top": 160, "bottom": 172},
                {"text": "12.5%", "x0": 260, "x1": 305, "top": 160, "bottom": 172},
                {"text": "Net", "x0": 20, "x1": 48, "top": 190, "bottom": 202},
                {"text": "Contents", "x0": 52, "x1": 112, "top": 190, "bottom": 202},
                {"text": "750", "x0": 260, "x1": 280, "top": 190, "bottom": 202},
                {"text": "mL", "x0": 283, "x1": 302, "top": 190, "bottom": 202},
            ]

    class _FakePdf:
        pages = [_FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakePdfPlumber:
        @staticmethod
        def open(_):
            return _FakePdf()

    monkeypatch.setattr(pdf_fields, "pdfplumber", _FakePdfPlumber())
    monkeypatch.setattr(pdf_fields, "_validate_required_pdfplumber_fields", lambda *_args, **_kwargs: None)

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)

    stream = io.BytesIO()
    writer.write(stream)

    evidence = parse_application_pdf(stream.getvalue())
    assert evidence.brand_name == "Stormchaser White"
    assert evidence.bottler_name_address == "Lighthouse Vintners Kingston, NY"


def test_parse_application_pdf_populates_table_fields(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)

    monkeypatch.setattr(pdf_fields, "_extract_pdf_text", lambda _: "")
    monkeypatch.setattr(pdf_fields, "_extract_layout_fields", lambda _: {})
    monkeypatch.setattr(
        pdf_fields,
        "_extract_roi_fields_and_text",
        lambda _: (
            {
                "source_of_product": "Domestic",
                "brand_name": "STORMCHASER",
                "fanciful_name": "WHITE RESERVE",
                "bottler_name_address": "Lighthouse Vintners, Kingston, NY",
                "grape_varietals": "Chardonnay",
                "wine_appellation": "Hudson River Region",
            },
            {"domestic": True, "imported": False, "wine": True, "distilled_spirits": False, "malt_beverages": False},
            "[T0:R1:C2] Brand Name STORMCHASER",
        ),
    )

    evidence = parse_application_pdf(stream.getvalue())
    assert evidence.domestic is True
    assert evidence.imported is False
    assert evidence.wine is True
    assert evidence.distilled_spirits is False
    assert evidence.malt_beverages is False
    assert evidence.source_of_product == "Domestic"
    assert evidence.beverage_type == BeverageType.WINE
    assert evidence.brand_name == "STORMCHASER"
    assert evidence.fanciful_name == "WHITE RESERVE"
    assert evidence.bottler_name_address == "Lighthouse Vintners, Kingston, NY"
    assert evidence.grape_varietals == "Chardonnay"
    assert evidence.wine_appellation == "Hudson River Region"

def test_parse_application_pdf_rejects_missing_required_pdfplumber_fields(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)

    monkeypatch.setattr(pdf_fields, "_extract_pdf_text", lambda _: "")
    monkeypatch.setattr(pdf_fields, "_extract_layout_fields", lambda _: {})
    monkeypatch.setattr(
        pdf_fields,
        "_extract_roi_fields_and_text",
        lambda _: (
            {
                "brand_name": "STORMCHASER",
                "fanciful_name": "WHITE RESERVE",
                "grape_varietals": "Chardonnay",
            },
            {"domestic": True, "imported": False, "wine": True, "distilled_spirits": False, "malt_beverages": False},
            "raw",
        ),
    )

    with pytest.raises(ApplicationExtractionRejectedError, match="missing required fields"):
        parse_application_pdf(stream.getvalue())


def test_parse_application_pdf_rejects_invalid_checkbox_group(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)

    monkeypatch.setattr(pdf_fields, "_extract_pdf_text", lambda _: "")
    monkeypatch.setattr(pdf_fields, "_extract_layout_fields", lambda _: {})
    monkeypatch.setattr(
        pdf_fields,
        "_extract_roi_fields_and_text",
        lambda _: (
            {
                "brand_name": "STORMCHASER",
                "fanciful_name": "WHITE RESERVE",
                "bottler_name_address": "Lighthouse Vintners, Kingston, NY",
                "grape_varietals": "Chardonnay",
                "wine_appellation": "Hudson River Region",
            },
            # Both source checkboxes selected => reject
            {"domestic": True, "imported": True, "wine": True, "distilled_spirits": False, "malt_beverages": False},
            "raw",
        ),
    )

    with pytest.raises(ApplicationExtractionRejectedError, match="Domestic or Imported"):
        parse_application_pdf(stream.getvalue())


def test_parse_application_pdf_rejects_with_all_reasons(monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)

    monkeypatch.setattr(pdf_fields, "_extract_pdf_text", lambda _: "")
    monkeypatch.setattr(pdf_fields, "_extract_layout_fields", lambda _: {})
    monkeypatch.setattr(
        pdf_fields,
        "_extract_roi_fields_and_text",
        lambda _: (
            {
                "brand_name": "STORMCHASER",
                "fanciful_name": "WHITE RESERVE",
                "grape_varietals": "Chardonnay",
                "wine_appellation": "Hudson River Region",
            },
            # Invalid source group (none selected) + invalid beverage group (multiple selected)
            {"domestic": False, "imported": False, "wine": True, "distilled_spirits": True, "malt_beverages": False},
            "raw",
        ),
    )

    with pytest.raises(ApplicationExtractionRejectedError) as exc_info:
        parse_application_pdf(stream.getvalue())

    message = str(exc_info.value)
    assert "missing required fields" in message
    assert "Bottler Name / Address" in message
    assert "Domestic or Imported" in message
    assert "Wine, Distilled Spirits, or Malt Beverages" in message
