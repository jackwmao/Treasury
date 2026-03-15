from app.parsing.fields import parse_abv_value, parse_address_components, parse_label_fields


def test_parse_abv_value():
    assert parse_abv_value("45% Alc./Vol.") == 45.0
    assert parse_abv_value("0.5%") == 0.5
    assert parse_abv_value("n/a") is None


def test_parse_address_components():
    parts = parse_address_components("123 Main St, Springfield, IL 62704")
    assert parts["street"].startswith("123")
    assert parts["city"] == "Springfield"
    assert parts["state"] == "IL"
    assert parts["postal_code"] == "62704"


def test_parse_label_fields():
    text = (
        "Brand Name: OLD TOM DISTILLERY\n"
        "Class/Type: Kentucky Straight Bourbon Whiskey\n"
        "Address: 123 Main St, Springfield, IL 62704\n"
        "45% Alc./Vol. (90 Proof)\n"
        "750 mL\n"
    )
    result = parse_label_fields(text, confidence=0.91)
    assert result.brand_name.value == "OLD TOM DISTILLERY"
    assert result.class_type.value == "Kentucky Straight Bourbon Whiskey"
    assert result.address.value == "123 Main St, Springfield, IL 62704"
    assert result.abv.value is not None
    assert result.net_contents.value is not None
