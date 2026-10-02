import pytest

from domain.normalization import normalize_place_name, normalize_state


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Big Cabin", "BIGCABIN"),
        ("  Effingham   ", "EFFINGHAM"),
        ("Mc Calla", "MCCALLA"),
        ("McCalla", "MCCALLA"),
        ("St. Louis", "STLOUIS"),
        ("Saint Louis", "STLOUIS"),
        ("Fort Smith", "FTSMITH"),
        ("Ft. Smith", "FTSMITH"),
        ("Mount Jackson", "MTJACKSON"),
        ("Coeur d'Alene", "COEURDALENE"),
        ("Cañon City", "CANONCITY"),
        ("Winston-Salem", "WINSTONSALEM"),
    ],
)
def test_normalize_place_name(raw, expected):
    assert normalize_place_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("TX", "TX"), ("tx", "TX"), ("Texas", "TX"), ("new  york", "NY"), ("D.C.", "DC")],
)
def test_normalize_state_accepts_codes_and_names(raw, expected):
    assert normalize_state(raw) == expected


@pytest.mark.parametrize("raw", ["ON", "Ontario", "XX", ""])
def test_normalize_state_rejects_non_us(raw):
    assert normalize_state(raw) is None
