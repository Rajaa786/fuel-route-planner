"""Turn a user-supplied location string into coordinates.

Resolution order, cheapest first:

1. ``"lat, lon"`` literal                          -> no I/O at all
2. a city via the offline gazetteer               -> one indexed DB lookup
   (``"Chicago, IL"``, ``"Chicago IL"``, ``"Chicago, Illinois"``, ``"Chicago"``)
3. anything else via the fallback geocoder        -> one external call, cached
   (street addresses, ZIP codes, landmarks, small towns given without a state)

Steps 1 and 2 cover the overwhelming majority of inputs, so a typical request
spends its single external call on routing.

The guiding rule for step 2 is that a wrong place is worse than a slower
answer: whenever a name is genuinely ambiguous offline, it is handed to the
fallback geocoder instead of being guessed.
"""

import hashlib
import re
from dataclasses import dataclass

import numpy as np
from django.core.cache import BaseCache

from apps.stations.models import Place
from domain import geometry
from domain.normalization import normalize_place_name, normalize_state
from providers.base import Coordinate, Geocoder

# Bounding box of the contiguous United States. The fuel data has no stations in
# Alaska or Hawaii, and neither can be reached without leaving the country.
_CONTIGUOUS_US = {"south": 24.3, "north": 49.5, "west": -125.0, "east": -66.9}

_COORDINATES = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")
_COUNTRY_WORDS = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA", "AMERICA"}
_TRAILING_COUNTRY = re.compile(r"\s+(USA|U\.S\.A\.|United States(?: of America)?)$", re.I)
_TRAILING_ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?$")
_ZIP_ONLY = re.compile(r"^\d{5}(?:-\d{4})?$")
# Rejected offline, saving a geocoder call for the most common foreign inputs.
_FOREIGN_REGIONS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
    "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK", "NEWFOUNDLAND AND LABRADOR",
    "NOVA SCOTIA", "NORTHWEST TERRITORIES", "NUNAVUT", "ONTARIO", "PRINCE EDWARD ISLAND",
    "QUEBEC", "SASKATCHEWAN", "YUKON", "CANADA", "MEXICO",
}  # fmt: skip
# "Lake Michigan" and "Central Texas" are regions, not the hamlets Lake, MI and Central, TX.
_REGION_WORDS = {
    "NORTH", "SOUTH", "EAST", "WEST", "NORTHERN", "SOUTHERN", "EASTERN", "WESTERN",
    "NORTHEAST", "NORTHWEST", "SOUTHEAST", "SOUTHWEST", "CENTRAL", "UPSTATE", "DOWNSTATE",
    "DOWNTOWN", "RURAL", "COASTAL", "GREATER", "UPPER", "LOWER", "LITTLE", "MIDWEST",
    "LAKE", "CENTER", "UNIVERSITY",
}  # fmt: skip
# State names that, typed alone, almost always mean the city.
_STATE_NAMES_THAT_MEAN_A_CITY = {"NEWYORK", "WASHINGTON", "DISTRICTOFCOLUMBIA"}

# A bare name (no state) only resolves offline to a place at least this big...
_BARE_NAME_MIN_POPULATION = 50_000
# ...that is clearly the one meant: no namesake city may be within this factor of its size
# (Columbus, OH is 4x Columbus, GA; Peoria, AZ is only 1.7x Peoria, IL).
_DOMINANCE_FACTOR = 3
# An alias ("NYC", "Vegas") is not trusted while a real town of at least this size carries
# the name outright ("Jefferson City" was once a name for Beaumont, TX).
_REAL_TOWN_MIN_POPULATION = 1_000
# A geocoded street address must land this close to the city named in the query.
_ADDRESS_MAX_MILES_FROM_CITY = 60.0

_CACHE_MISS = "__miss__"

ACCEPTED_FORMATS = [
    "City, ST (e.g. 'Chicago, IL')",
    "City, State name or City ST (e.g. 'Chicago, Illinois', 'Chicago IL')",
    "A large city on its own (e.g. 'Chicago')",
    "latitude,longitude (e.g. '41.8781,-87.6298')",
    "A US street address, ZIP code or landmark (e.g. '233 S Wacker Dr, Chicago, IL')",
]


class LocationNotFoundError(Exception):
    def __init__(self, query: str):
        self.query = query
        super().__init__(f"Could not find a US location matching {query!r}.")


class LocationOutsideServiceAreaError(Exception):
    def __init__(self, query: str, matched: str | None = None):
        self.query = query
        self.matched = matched
        if matched is None:
            message = f"{query!r} is outside the contiguous United States, the area covered."
        else:
            # Say what was understood: "Naples" and "Paris" also exist in the US.
            message = (
                f"{query!r} was understood as {matched!r}, which is outside the contiguous "
                "United States. If you meant a US place, add its state, e.g. 'City, ST'."
            )
        super().__init__(message)


class LocationAmbiguousError(Exception):
    """The text names more than one plausible place, so resolving it would be a guess."""

    def __init__(self, query: str, candidates: list[str] | None = None):
        self.query = query
        self.candidates = candidates or []
        if self.candidates:
            message = (
                f"{query!r} matches several US cities: {'; '.join(self.candidates)}. "
                "Add the state to say which."
            )
        else:
            message = f"{query!r} is a state, not a place to drive to. Name a city in it."
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    query: str
    label: str
    coordinate: Coordinate
    source: str  # "coordinates" | "gazetteer" | "geocoder"
    external_calls: int = 0
    # Set when the answer is less precise than what was asked for.
    note: str | None = None


class LocationResolver:
    def __init__(
        self,
        fallback: Geocoder | None,
        cache: BaseCache,
        *,
        hit_ttl_seconds: int,
        miss_ttl_seconds: int,
    ):
        self._fallback = fallback
        self._cache = cache
        self._hit_ttl = hit_ttl_seconds
        self._miss_ttl = miss_ttl_seconds

    def resolve(self, query: str) -> ResolvedLocation:
        location = self._from_coordinates(query)
        if location is None:
            parts, had_zip = _split_query(query)
            if len(parts) > 1 and parts[-1].upper() in _FOREIGN_REGIONS:
                raise LocationOutsideServiceAreaError(query)
            if len(parts) == 1 and parts[0].upper().replace(".", "") in _COUNTRY_WORDS:
                raise LocationNotFoundError(query)  # a country is not a trip endpoint
            location = self._from_gazetteer(query, parts, had_zip) or self._from_fallback(
                query, parts
            )
        if location is None:
            raise LocationNotFoundError(query)
        if not _in_contiguous_us(location.coordinate):
            raise LocationOutsideServiceAreaError(query)
        return location

    @staticmethod
    def _from_coordinates(query: str) -> ResolvedLocation | None:
        match = _COORDINATES.match(query)
        if match is None:
            return None
        latitude, longitude = float(match[1]), float(match[2])
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise LocationNotFoundError(query)
        return ResolvedLocation(
            query=query,
            label=f"{latitude:.5f}, {longitude:.5f}",
            coordinate=Coordinate(latitude, longitude),
            source="coordinates",
        )

    @staticmethod
    def _from_gazetteer(query: str, parts: list[str], had_zip: bool) -> ResolvedLocation | None:
        place = None
        if len(parts) == 2:
            state = normalize_state(parts[1])
            if state is not None:
                place = _place_in_state(parts[0], state)
        elif len(parts) == 1:
            # No comma: "Chicago IL" and "Chicago Illinois" are read as city + state, and
            # failing that the whole text is a bare city name ("New York", "Kansas City").
            split = _split_trailing_state(parts[0])
            if split is not None:
                place = _place_in_state(*split)
            # A ZIP code with no state ("Springfield 62701") already says which namesake is
            # meant, and only the geocoder can read it; picking by population would be wrong.
            if place is None and not had_zip:
                place = _place_by_bare_name(query, parts[0])
        # Three or more parts is a street address: the fallback geocoder's job.

        if place is None:
            return None
        return ResolvedLocation(
            query=query,
            label=f"{place.name}, {place.state}",
            coordinate=Coordinate(place.latitude, place.longitude),
            source="gazetteer",
        )

    def _from_fallback(self, query: str, parts: list[str]) -> ResolvedLocation | None:
        if self._fallback is None:
            return None
        normalised = " ".join(query.lower().split())
        cache_key = "geocode:" + hashlib.sha256(normalised.encode()).hexdigest()

        cached = self._cache.get(cache_key)
        external_calls = 0
        if cached is None:
            # Provider errors propagate from here and are never cached.
            cached = self._ask_fallback(query, parts) or _CACHE_MISS
            self._cache.set(
                cache_key, cached, self._miss_ttl if cached == _CACHE_MISS else self._hit_ttl
            )
            external_calls = 1
        if cached == _CACHE_MISS:
            return None

        label, latitude, longitude, source, country_code, note = cached
        if country_code not in (None, "us"):
            raise LocationOutsideServiceAreaError(query, matched=label)
        return ResolvedLocation(
            query, label, Coordinate(latitude, longitude), source, external_calls, note
        )

    def _ask_fallback(self, query: str, parts: list[str]) -> tuple | None:
        """One geocoder call; returns ``(label, lat, lon, source, country_code, note)``."""
        city = _city_named_in_address(parts)
        if city is None:
            hit = self._fallback.geocode(query)
            if hit is None:
                return None
            point = hit.coordinate
            return hit.label, point.latitude, point.longitude, "geocoder", hit.country_code, None

        # The query ends in a city we know, so the answer must be in or near that city.
        # A house number means a street address: ask for the street *within the city*, by its
        # canonical name, because free text lets the geocoder misread "New York, NY" as the
        # state and return a Fifth Avenue 135 miles upstate. Anything else ("Willis Tower,
        # Chicago, IL") is a landmark, which only free text can find.
        specific = ", ".join(parts[:-2])
        if specific[:1].isdigit():
            hit = self._fallback.geocode_address(specific, city.name, city.state)
        else:
            hit = self._fallback.geocode(query)
        if hit is not None and hit.country_code in (None, "us"):
            anchor = Coordinate(city.latitude, city.longitude)
            if _miles_between(hit.coordinate, anchor) <= _ADDRESS_MAX_MILES_FROM_CITY:
                point = hit.coordinate
                return hit.label, point.latitude, point.longitude, "geocoder", "us", None
        # Not found there: the city itself is the honest answer, and we say so.
        label = f"{city.name}, {city.state}"
        note = f"{specific!r} was not found in {label}; the city centre is used instead."
        return label, city.latitude, city.longitude, "gazetteer", "us", note


# --- query parsing ---------------------------------------------------------------------------


def _split_query(query: str) -> tuple[list[str], bool]:
    """Comma-separated parts without a trailing country or ZIP code, and whether a ZIP was cut."""
    parts = [part.strip() for part in query.split(",") if part.strip()]
    if len(parts) > 1 and parts[-1].upper().replace(".", "") in _COUNTRY_WORDS:
        parts.pop()
    had_zip = False
    if len(parts) > 1 and _ZIP_ONLY.match(parts[-1]):  # "Chicago, IL, 60601"
        parts.pop()
        had_zip = True
    if parts:
        last = _TRAILING_COUNTRY.sub("", parts[-1])
        without_zip = _TRAILING_ZIP.sub("", last).strip()  # "Chicago IL 60601"
        if without_zip and without_zip != last.strip():
            had_zip = True
        if without_zip:
            parts[-1] = without_zip
    return parts, had_zip


def _split_trailing_state(text: str) -> tuple[str, str] | None:
    """Split ``"Jersey City New Jersey"`` into ``("Jersey City", "NJ")``.

    State names are up to three words ("District of Columbia"); the longest match
    wins. A state after a lone region word ("Central PA") is left alone.
    """
    words = text.split()
    for size in (3, 2, 1):
        if len(words) <= size:
            continue
        tail = " ".join(words[-size:])
        state = normalize_state(tail)
        if state is None:
            continue
        city = " ".join(words[:-size])
        if city.upper() in _REGION_WORDS:
            return None
        return city, state
    return None


def _city_named_in_address(parts: list[str]) -> Place | None:
    """The ``City, ST`` at the end of ``"street, City, ST"``, if the gazetteer knows it."""
    if len(parts) < 3:
        return None
    state = normalize_state(parts[-1])
    return _place_in_state(parts[-2], state) if state else None


# --- gazetteer lookups -----------------------------------------------------------------------


def _place_in_state(city: str, state: str) -> Place | None:
    return Place.objects.filter(key=normalize_place_name(city), state=state).first()


def _place_by_bare_name(query: str, text: str) -> Place | None:
    """Resolve a name given without a state, decline (``None``) or refuse (raise).

    ``None`` hands the name to the fallback geocoder. Raising says that no lookup
    could settle it and the caller has to be more specific.
    """
    key = normalize_place_name(text)
    if not key:
        return None
    # "Wyoming" means the state, not Wyoming, MI. Two-letter codes are exempt ("LA").
    if len(key) > 2 and normalize_state(text) and key not in _STATE_NAMES_THAT_MEAN_A_CITY:
        raise LocationAmbiguousError(query)

    places = list(Place.objects.filter(key=key).order_by("-population", "state")[:100])
    # A gazetteer key is either a place's own name or an alternate name of a big city.
    literal = [place for place in places if normalize_place_name(place.name) == key]
    if literal:
        largest = literal[0]
        if largest.population >= _BARE_NAME_MIN_POPULATION:
            rivals = [
                place
                for place in literal[1:]
                if place.population >= _BARE_NAME_MIN_POPULATION
                and place.population * _DOMINANCE_FACTOR > largest.population
            ]
            if rivals:
                names = [f"{place.name}, {place.state}" for place in [largest, *rivals]]
                raise LocationAmbiguousError(query, names)
            return largest
        if largest.population >= _REAL_TOWN_MIN_POPULATION:
            return None  # a real town owns this name: let the geocoder decide
    aliases = [place for place in places if place not in literal]
    if aliases and aliases[0].population >= _BARE_NAME_MIN_POPULATION:
        return aliases[0]
    return None


# --- geometry --------------------------------------------------------------------------------


def _miles_between(a: Coordinate, b: Coordinate) -> float:
    path = np.array([[a.latitude, a.longitude], [b.latitude, b.longitude]])
    return float(geometry.cumulative_miles(path)[-1])


def _in_contiguous_us(coordinate: Coordinate) -> bool:
    box = _CONTIGUOUS_US
    return (
        box["south"] <= coordinate.latitude <= box["north"]
        and box["west"] <= coordinate.longitude <= box["east"]
    )
