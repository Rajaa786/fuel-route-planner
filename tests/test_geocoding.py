import pytest
from django.core.cache import cache

from apps.planner.geocoding import (
    LocationNotFoundError,
    LocationOutsideServiceAreaError,
    LocationResolver,
)
from apps.stations.models import Place
from providers.base import Coordinate, GeocodeHit, ProviderUnavailableError
from tests.helpers import FakeGeocoder


@pytest.fixture
def places(db):
    Place.objects.bulk_create(
        [
            Place(key="CHICAGO", state="IL", name="Chicago", latitude=41.85, longitude=-87.65,
                  population=2_700_000),
            Place(key="STLOUIS", state="MO", name="St. Louis", latitude=38.63, longitude=-90.2,
                  population=280_000),
            Place(key="SPRINGFIELD", state="IL", name="Springfield", latitude=39.8,
                  longitude=-89.64, population=114_000),
            Place(key="SPRINGFIELD", state="MO", name="Springfield", latitude=37.22,
                  longitude=-93.3, population=169_000),
            Place(key="BIGCABIN", state="OK", name="Big Cabin", latitude=36.54, longitude=-95.22,
                  population=262),
            Place(key="HONOLULU", state="HI", name="Honolulu", latitude=21.31, longitude=-157.86,
                  population=350_000),
        ]
    )  # fmt: skip


def resolver(fallback=None):
    return LocationResolver(fallback, cache, hit_ttl_seconds=60, miss_ttl_seconds=60)


def test_coordinates_need_no_lookup(db):
    location = resolver().resolve(" 41.8781 , -87.6298 ")
    assert location.source == "coordinates"
    assert location.coordinate == Coordinate(41.8781, -87.6298)
    assert location.external_calls == 0


@pytest.mark.parametrize(
    "query",
    ["Chicago, IL", "chicago,il", "Chicago, Illinois", "Chicago IL", "Chicago, IL, USA"],
)
def test_city_and_state_resolve_from_the_gazetteer(places, query):
    location = resolver().resolve(query)
    assert (location.label, location.source, location.external_calls) == (
        "Chicago, IL",
        "gazetteer",
        0,
    )


def test_abbreviation_variants_share_a_key(places):
    assert resolver().resolve("Saint Louis, MO").label == "St. Louis, MO"


def test_state_disambiguates_same_named_cities(places):
    assert resolver().resolve("Springfield, IL").coordinate.latitude == 39.8
    assert resolver().resolve("Springfield, MO").coordinate.latitude == 37.22


def test_bare_city_name_picks_the_most_populous_large_city(places):
    assert resolver().resolve("Springfield").label == "Springfield, MO"


def test_bare_small_town_is_not_guessed(places):
    with pytest.raises(LocationNotFoundError):
        resolver().resolve("Big Cabin")


def test_unknown_place_falls_back_to_the_geocoder_and_is_cached(places):
    hit = GeocodeHit(Coordinate(38.8977, -77.0365), "White House, Washington, DC")
    geocoder = FakeGeocoder({"1600 Pennsylvania Ave NW, Washington, DC": hit})

    first = resolver(geocoder).resolve("1600 Pennsylvania Ave NW, Washington, DC")
    second = resolver(geocoder).resolve("1600  pennsylvania ave nw, washington, dc")

    assert (first.source, first.external_calls) == ("geocoder", 1)
    assert (second.label, second.external_calls) == ("White House, Washington, DC", 0)
    assert geocoder.calls == 1


def test_geocoder_misses_are_cached_too(places):
    geocoder = FakeGeocoder()
    for _ in range(2):
        with pytest.raises(LocationNotFoundError):
            resolver(geocoder).resolve("zzz nowhere 123")
    assert geocoder.calls == 1


def test_geocoder_failures_propagate_and_are_not_cached(places):
    geocoder = FakeGeocoder(error=ProviderUnavailableError("down"))
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            resolver(geocoder).resolve("zzz nowhere 123")
    assert geocoder.calls == 2


@pytest.mark.parametrize("query", ["Toronto, ON", "Vancouver, British Columbia", "Paris, Canada"])
def test_canadian_locations_are_rejected_without_calling_the_geocoder(places, query):
    geocoder = FakeGeocoder()
    with pytest.raises(LocationOutsideServiceAreaError):
        resolver(geocoder).resolve(query)
    assert geocoder.calls == 0


@pytest.mark.parametrize("query", ["Honolulu, HI", "51.5074, -0.1278", "64.2, -149.5"])
def test_locations_outside_the_contiguous_us_are_rejected(places, query):
    with pytest.raises(LocationOutsideServiceAreaError):
        resolver().resolve(query)


def test_impossible_coordinates_are_not_found(db):
    with pytest.raises(LocationNotFoundError):
        resolver().resolve("123.4, -200")
