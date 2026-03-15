from app.matching.reconcile import address_match_score, brand_match_score


def test_brand_match_tolerates_punctuation():
    assert brand_match_score("STONE'S THROW", "Stones Throw") > 0.85


def test_address_semantic_match():
    score = address_match_score(
        "123 Main St, Springfield, IL 62704",
        "123 Main Street, Springfield, IL 62704",
    )
    assert score > 0.8


def test_address_mismatch():
    score = address_match_score(
        "123 Main St, Springfield, IL 62704",
        "19 Pine Road, Othercity, NY 10001",
    )
    assert score < 0.6
