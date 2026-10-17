import pytest

from app.parsing.location import guess_location


@pytest.mark.parametrize("text,expected", [
    ("Jane Doe\nLocation: Manama, Bahrain\nPhone: +973 1234 5678", "Manama, Bahrain"),
    ("Jane Doe\nAddress - Riffa, Bahrain\n", "Riffa, Bahrain"),
    ("Jane Doe\nBased in: London\nEXPERIENCE", "London"),
    ("Jane Doe\nberkeley, CA | jane@x.com | (510) 555-0100\nEDUCATION", "Berkeley, CA"),
    ("JANE DOE\nNew York, NY  •  jane@x.com  •  +1 212 555 0100", "New York, NY"),
    ("Jane Doe\nDubai, United Arab Emirates\njane@x.com", "Dubai, United Arab Emirates"),
    ("Jane Doe\njane@x.com | Singapore, Singapore", "Singapore, Singapore"),
])
def test_finds_location_in_the_header(text, expected) -> None:
    assert (guess_location(text) or "").lower() == expected.lower()


@pytest.mark.parametrize("text", [
    "Jane Doe\njane@x.com\n+973 1234 5678\nEXPERIENCE\nAnalyst, Goldman Sachs",
    "Jane Doe\nSummary\nI work in finance, banking and risk",
    "Jane Doe\nProject Manager, Acme Ltd\nEDUCATION",
    "",
])
def test_returns_none_instead_of_guessing(text) -> None:
    assert guess_location(text) is None


def test_ignores_a_location_label_far_down_the_document() -> None:
    text = "Jane Doe\n" + "filler line\n" * 60 + "Location: Paris, France"
    assert guess_location(text) is None


def test_label_value_is_cleaned_and_bounded() -> None:
    assert guess_location("Jane\nLocation: " + "x" * 400) is None
    assert guess_location("Jane\nLocation: Doha, Qatar | LinkedIn: linkedin.com/in/jane") == "Doha, Qatar"
