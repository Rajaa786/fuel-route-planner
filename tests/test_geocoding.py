from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command

from apps.planner.geocoding import (
    LocationAmbiguousError,
    LocationNotFoundError,
    LocationOutsideServiceAreaError,
    LocationResolver,
)
from apps.stations.models import Place
from providers.base import Coordinate, GeocodeHit, ProviderUnavailableError
from tests.helpers import FakeGeocoder


def place(key, state, name, latitude, longitude, population):
    return Place(
        key=key,
        state=state,
        name=name,
        latitude=latitude,
        longitude=longitude,
        population=population,
    )


@pytest.fixture
def places(db):
    Place.objects.bulk_create(
        [
            place("CHICAGO", "IL", "Chicago", 41.85, -87.65, 2_700_000),
            place("STLOUIS", "MO", "St. Louis", 38.63, -90.2, 280_000),
            place("SPRINGFIELD", "IL", "Springfield", 39.8, -89.64, 114_000),
            place("SPRINGFIELD", "MO", "Springfield", 37.22, -93.3, 169_000),
            place("BIGCABIN", "OK", "Big Cabin", 36.54, -95.22, 262),
            place("HONOLULU", "HI", "Honolulu", 21.31, -157.86, 350_000),
            place("KANSASCITY", "MO", "Kansas City", 39.1, -94.58, 508_000),
            place("WASHINGTON", "DC", "Washington", 38.9, -77.04, 690_000),
            # New York City, reachable by its own name and by two alternate names.
            place("NEWYORKCITY", "NY", "New York City", 40.71, -74.0, 8_800_000),
            place("NEWYORK", "NY", "New York City", 40.71, -74.0, 8_800_000),
            place("NYC", "NY", "New York City", 40.71, -74.0, 8_800_000),
            place("NEWYORK", "MO", "New York", 39.69, -93.93, 0),  # a hamlet of that name
            # Old alternate names of big cities that collide with real towns.
            place("MANCHESTER", "NJ", "Paterson", 40.92, -74.17, 160_000),
            place("MANCHESTER", "NH", "Manchester", 42.99, -71.45, 115_000),
            place("JEFFERSONCITY", "TX", "Beaumont", 30.09, -94.1, 115_000),
            place("JEFFERSONCITY", "MO", "Jefferson City", 38.58, -92.17, 43_000),
            # Places whose names collide with states or regions.
            place("WYOMING", "MI", "Wyoming", 42.91, -85.71, 76_000),
            place("LAKE", "MI", "Lake", 43.86, -85.0, 0),
            place("WEST", "TX", "West", 31.8, -97.09, 2_900),
            place("CENTRAL", "PA", "Central", 41.27, -76.29, 0),
            place("COLUMBUS", "OH", "Columbus", 39.96, -83.0, 906_000),
            place("COLUMBUS", "GA", "Columbus", 32.46, -84.99, 207_000),
            place("RICHMOND", "VA", "Richmond", 37.55, -77.46, 226_000),
            place("RICHMOND", "CA", "Richmond", 37.94, -122.35, 109_000),  # 2.07x smaller
            place("PORTWASHINGTON", "NY", "Port Washington", 40.83, -73.7, 60_000),
            # Alternate names of big cities versus a small real town and a mere hamlet.
            place("FRENCHLICK", "TN", "Nashville", 36.17, -86.78, 690_000),
            place("FRENCHLICK", "IN", "French Lick", 38.55, -86.62, 1_800),
            place("MUSICCITY", "TN", "Nashville", 36.17, -86.78, 690_000),
            place("MUSICCITY", "KS", "Music City", 38.0, -97.0, 40),
        ]
    )


def resolver(fallback=None):
    return LocationResolver(fallback, cache, hit_ttl_seconds=60, miss_ttl_seconds=60)


def offline(query):
    """Resolve with no fallback geocoder: a miss means "would cost an external call"."""
    return resolver().resolve(query)


# --- coordinates ----------------------------------------------------------------------------


def test_coordinates_need_no_lookup(db):
    location = offline(" 41.8781 , -87.6298 ")
    assert location.source == "coordinates"
    assert location.coordinate == Coordinate(41.8781, -87.6298)
    assert location.external_calls == 0


def test_impossible_coordinates_are_not_found(db):
    with pytest.raises(LocationNotFoundError):
        offline("123.4, -200")


# --- city names, offline --------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "Chicago, IL",
        "Chicago,IL",
        "chicago,il",
        "Chicago, Illinois",
        "Chicago IL",
        "chicago il",
        "Chicago Illinois",
        "Chicago, IL, USA",
        "Chicago IL USA",
        "Chicago, IL 60601",
        "Chicago, IL, 60601",
        "Chicago IL 60601-1234",
        "Chicago",
    ],
)
def test_state_and_comma_are_optional_for_a_large_city(places, query):
    location = offline(query)
    assert (location.label, location.source, location.external_calls) == (
        "Chicago, IL",
        "gazetteer",
        0,
    )


@pytest.mark.parametrize(
    ("query", "label"),
    [
        ("Saint Louis, MO", "St. Louis, MO"),  # abbreviation variants share a key
        ("Springfield, IL", "Springfield, IL"),  # the state disambiguates
        ("Springfield Missouri", "Springfield, MO"),
        ("Columbus", "Columbus, OH"),  # bare: four times the size of its namesake
        ("Big Cabin Oklahoma", "Big Cabin, OK"),  # small town: fine once the state is given
        ("Kansas City", "Kansas City, MO"),  # ends in a word, not a state
        ("Kansas City Missouri", "Kansas City, MO"),
        ("Washington DC", "Washington, DC"),
        ("Washington District of Columbia", "Washington, DC"),
        ("Washington", "Washington, DC"),
        ("New York", "New York City, NY"),  # an alias no real town competes for
        ("New York New York", "New York City, NY"),
        ("NYC", "New York City, NY"),
        ("West, TX", "West, TX"),
        ("Port Washington", "Port Washington, NY"),  # not the town of "Port" in Washington
        ("Music City", "Nashville, TN"),  # a nickname only a hamlet competes for
    ],
)
def test_names_that_resolve_offline(places, query, label):
    location = offline(query)
    assert (location.label, location.source) == (label, "gazetteer")


def test_a_town_literally_named_x_beats_a_bigger_city_once_called_x(places):
    # "Manchester" is an old name for Paterson, NJ (the more populous row for that key).
    assert offline("Manchester").label == "Manchester, NH"


@pytest.mark.parametrize(
    "query",
    [
        "Big Cabin",  # too small to guess without a state
        "Jefferson City",  # an alias of Beaumont, TX, but a real town of 43,000 owns the name
        "French Lick",  # likewise, and the real town has only 1,800 people
        "Lake Michigan",  # a region, not the hamlet of Lake, MI
        "West Texas",
        "West TX",
        "Central PA",
        "Springfield 62701",  # the ZIP says which Springfield; population would say another
        "Springfield, 62701",
        "Springfield 62701-1234",
        "Nowhere, IL",
    ],
)
def test_ambiguous_names_are_left_to_the_geocoder_rather_than_guessed(places, query):
    with pytest.raises(LocationNotFoundError):
        offline(query)

    geocoder = FakeGeocoder()
    with pytest.raises(LocationNotFoundError):
        resolver(geocoder).resolve(query)
    assert geocoder.calls == 1


def test_namesakes_of_comparable_size_are_refused_with_the_candidates(places):
    geocoder = FakeGeocoder()
    with pytest.raises(LocationAmbiguousError) as excinfo:
        resolver(geocoder).resolve("Springfield")

    assert excinfo.value.candidates == ["Springfield, MO", "Springfield, IL"]
    assert "Add the state" in str(excinfo.value)
    assert geocoder.calls == 0  # neither a guess nor an external call

    # "Comparable" means within a factor of three: Richmond, VA is only twice Richmond, CA.
    with pytest.raises(LocationAmbiguousError):
        offline("Richmond")


@pytest.mark.parametrize("query", ["Wyoming", "wyoming", "West Virginia", "North Dakota"])
def test_a_bare_state_is_refused_rather_than_mapped_to_a_town_or_its_centre(places, query):
    geocoder = FakeGeocoder()
    with pytest.raises(LocationAmbiguousError) as excinfo:
        resolver(geocoder).resolve(query)
    assert excinfo.value.candidates == [] and "is a state" in str(excinfo.value)
    assert geocoder.calls == 0


@pytest.mark.parametrize("query", ["USA", "United States", "U.S.A."])
def test_a_country_is_not_a_location(places, query):
    geocoder = FakeGeocoder()
    with pytest.raises(LocationNotFoundError):
        resolver(geocoder).resolve(query)
    assert geocoder.calls == 0


# --- fallback geocoder ----------------------------------------------------------------------


def test_unknown_place_falls_back_to_the_geocoder_and_is_cached(places):
    hit = GeocodeHit(Coordinate(40.6892, -74.0445), "Statue of Liberty, New York", "us")
    geocoder = FakeGeocoder({"Statue of Liberty": hit})

    first = resolver(geocoder).resolve("Statue of Liberty")
    second = resolver(geocoder).resolve("statue  of liberty")

    assert (first.source, first.external_calls) == ("geocoder", 1)
    assert (second.label, second.external_calls) == ("Statue of Liberty, New York", 0)
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


@pytest.mark.parametrize(
    ("query", "country"), [("Mexico City", "mx"), ("Toronto ON", "ca"), ("London", "gb")]
)
def test_a_foreign_match_is_rejected_not_swapped_for_a_us_namesake(places, query, country):
    # Toronto's coordinates: inside the US bounding box, so only the country code can tell.
    hit = GeocodeHit(Coordinate(43.65, -79.38), "Somewhere abroad", country)
    geocoder = FakeGeocoder({query: hit})
    for _ in range(2):
        with pytest.raises(LocationOutsideServiceAreaError) as excinfo:
            resolver(geocoder).resolve(query)
        # The message says what was understood, so "Naples" can be retyped as "Naples, FL".
        assert excinfo.value.matched == "Somewhere abroad"
        assert "Somewhere abroad" in str(excinfo.value) and "add its state" in str(excinfo.value)
    assert geocoder.calls == 1  # the verdict is cached like any other answer


@pytest.mark.parametrize("query", ["Toronto, ON", "Vancouver, British Columbia", "Paris, Canada"])
def test_canadian_locations_are_rejected_without_calling_the_geocoder(places, query):
    geocoder = FakeGeocoder()
    with pytest.raises(LocationOutsideServiceAreaError):
        resolver(geocoder).resolve(query)
    assert geocoder.calls == 0


@pytest.mark.parametrize("query", ["Honolulu, HI", "51.5074, -0.1278", "64.2, -149.5"])
def test_locations_outside_the_contiguous_us_are_rejected(places, query):
    with pytest.raises(LocationOutsideServiceAreaError):
        offline(query)


# --- street addresses -----------------------------------------------------------------------

ADDRESS = "350 Fifth Avenue, New York, NY"
STRUCTURED = ("350 Fifth Avenue", "New York City", "NY")  # canonical city name, not "New York"


def test_address_is_looked_up_inside_the_city_it_names(places):
    hit = GeocodeHit(Coordinate(40.7484, -73.9857), "Empire State Building", "us")
    geocoder = FakeGeocoder(address_hits={STRUCTURED: hit})

    location = resolver(geocoder).resolve(ADDRESS)

    assert geocoder.address_queries == [STRUCTURED]
    assert (location.label, location.source, location.external_calls) == (
        "Empire State Building",
        "geocoder",
        1,
    )


def test_address_hit_far_from_the_named_city_is_discarded(places):
    # The real-world failure: a "350 5th Avenue" in Watervliet, 135 miles up the Hudson.
    watervliet = GeocodeHit(Coordinate(42.71, -73.71), "350 5th Avenue, Watervliet", "us")
    geocoder = FakeGeocoder(address_hits={STRUCTURED: watervliet})

    location = resolver(geocoder).resolve(ADDRESS)

    assert (location.label, location.source) == ("New York City, NY", "gazetteer")
    assert location.coordinate == Coordinate(40.71, -74.0)
    assert location.note == (
        "'350 Fifth Avenue' was not found in New York City, NY; the city centre is used instead."
    )


def test_address_not_found_falls_back_to_its_city(places):
    geocoder = FakeGeocoder()
    location = resolver(geocoder).resolve(ADDRESS)
    assert (location.label, location.external_calls) == ("New York City, NY", 1)
    assert location.note is not None

    cached = resolver(geocoder).resolve(ADDRESS)  # the note survives the cache
    assert (cached.external_calls, cached.note) == (0, location.note)


def test_street_with_several_parts_is_passed_whole(places):
    geocoder = FakeGeocoder()
    resolver(geocoder).resolve("350 Fifth Avenue, Floor 2, New York, NY 10118")
    assert geocoder.address_queries == [("350 Fifth Avenue, Floor 2", "New York City", "NY")]


def test_landmark_in_a_named_city_uses_free_text_and_must_be_near_that_city(places):
    query = "Willis Tower, Chicago, IL"
    near = GeocodeHit(Coordinate(41.8789, -87.6359), "Willis Tower, Chicago", "us")
    geocoder = FakeGeocoder({query: near})

    location = resolver(geocoder).resolve(query)

    assert geocoder.address_queries == []  # no house number, so not a street lookup
    assert (location.label, location.source, location.note) == (
        "Willis Tower, Chicago",
        "geocoder",
        None,
    )


@pytest.mark.parametrize(
    "hit",
    [
        GeocodeHit(Coordinate(34.05, -118.24), "Willis Tower replica, Los Angeles", "us"),
        # Close enough to pass the distance test, so only the country code rules it out
        # (think Windsor, Ontario for a Detroit query).
        GeocodeHit(Coordinate(41.9, -87.7), "Willis Tower Pub, across the border", "ca"),
        None,
    ],
)
def test_landmark_found_elsewhere_or_not_at_all_falls_back_to_the_named_city(places, hit):
    query = "Willis Tower, Chicago, IL"
    cache.clear()
    location = resolver(FakeGeocoder({query: hit} if hit else {})).resolve(query)

    assert (location.label, location.source) == ("Chicago, IL", "gazetteer")
    assert "'Willis Tower' was not found in Chicago, IL" in location.note


def test_address_in_an_unknown_city_uses_free_text(places):
    hit = GeocodeHit(Coordinate(44.26, -72.58), "1 Main St, Montpelier, Vermont", "us")
    geocoder = FakeGeocoder({"1 Main St, Montpelier, VT": hit})

    location = resolver(geocoder).resolve("1 Main St, Montpelier, VT")

    assert geocoder.address_queries == []
    assert location.label == "1 Main St, Montpelier, Vermont"


# --- the committed gazetteer ----------------------------------------------------------------


def test_shipped_gazetteer_resolves_real_names_correctly(db):
    """The rules above, checked against the real data they were written for."""
    call_command("load_places", stdout=StringIO())

    expected = {
        "New York": "New York City, NY",
        "NYC": "New York City, NY",
        "LA": "Los Angeles, CA",
        "Chicago Illinois": "Chicago, IL",
        "Dallas": "Dallas, TX",
        "Washington": "Washington, DC",
        "Kansas City": "Kansas City, MO",
        "Columbus": "Columbus, OH",
        "Portland": "Portland, OR",
        "Jersey City New Jersey": "Jersey City, NJ",
        "Charleston West Virginia": "Charleston, WV",  # the longest state name wins
        "District of Columbia": "Washington, DC",
        # Names that used to resolve to a bigger city's obsolete alternate name.
        "Manchester": "Manchester, NH",
        "Troy": "Troy, MI",
        "Reading": "Reading, PA",
        "Newton": "Newton, MA",
        "Victoria": "Victoria, TX",
        "Great Falls": "Great Falls, MT",
    }
    assert {query: offline(query).label for query in expected} == expected

    # Left to the geocoder: a mid-sized or small real town owns the name, or it is a region.
    for query in ["Jefferson City", "Fort Washington", "French Lick", "Mystic", "Lake Michigan"]:
        with pytest.raises(LocationNotFoundError):
            offline(query)

    # Refused: comparable namesakes, with both named.
    for query, states in {"Peoria": ("AZ", "IL"), "Springfield": ("MO", "MA", "IL")}.items():
        with pytest.raises(LocationAmbiguousError) as excinfo:
            offline(query)
        largest_first = [f"{query}, {state}" for state in states]
        assert excinfo.value.candidates[: len(states)] == largest_first

    # Refused: states.
    for query in ["Wyoming", "Maine", "Georgia", "Texas"]:
        with pytest.raises(LocationAmbiguousError):
            offline(query)
