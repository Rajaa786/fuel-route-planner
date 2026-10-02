"""Turn a user-supplied location string into coordinates.

Resolution order, cheapest first:

1. ``"lat, lon"`` literal                      -> no I/O at all
2. ``"City, ST"`` via the offline gazetteer   -> one indexed DB lookup
3. anything else via the fallback geocoder    -> one external call, cached

Steps 1 and 2 cover the overwhelming majority of inputs, so a typical request
spends its single external call on routing.
"""

import hashlib
import re
from dataclasses import dataclass

from django.core.cache import BaseCache

from apps.stations.models import Place
from domain.normalization import normalize_place_name, normalize_state
from providers.base import Coordinate, Geocoder

# Bounding box of the contiguous United States. The fuel data has no stations in
# Alaska or Hawaii, and neither can be reached without leaving the country.
_CONTIGUOUS_US = {"south": 24.3, "north": 49.5, "west": -125.0, "east": -66.9}

_COORDINATES = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$")
_COUNTRY_SUFFIXES = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}
# Checked before the fallback geocoder, which is restricted to the US and would
# otherwise "find" some unrelated US street named after the foreign place.
_CANADIAN_REGIONS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
    "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK", "NEWFOUNDLAND AND LABRADOR",
    "NOVA SCOTIA", "NORTHWEST TERRITORIES", "NUNAVUT", "ONTARIO", "PRINCE EDWARD ISLAND",
    "QUEBEC", "SASKATCHEWAN", "YUKON", "CANADA", "MEXICO",
}  # fmt: skip
_CACHE_MISS = "__miss__"


class LocationNotFoundError(Exception):
    def __init__(self, query: str):
        self.query = query
        super().__init__(f"Could not find a US location matching {query!r}.")


class LocationOutsideServiceAreaError(Exception):
    def __init__(self, query: str):
        self.query = query
        super().__init__(
            f"{query!r} is outside the contiguous United States, which is the area covered."
        )


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    query: str
    label: str
    coordinate: Coordinate
    source: str  # "coordinates" | "gazetteer" | "geocoder"
    external_calls: int = 0


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
        location = (
            self._from_coordinates(query)
            or self._from_gazetteer(query)
            or self._from_fallback(query)
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
    def _from_gazetteer(query: str) -> ResolvedLocation | None:
        parts = [part.strip() for part in query.split(",") if part.strip()]
        if len(parts) > 1 and parts[-1].upper().replace(".", "") in _COUNTRY_SUFFIXES:
            parts.pop()
        if len(parts) > 1 and parts[-1].upper() in _CANADIAN_REGIONS:
            raise LocationOutsideServiceAreaError(query)

        if len(parts) == 1:
            # Accept "Chicago IL" as well as "Chicago, IL".
            city, _, tail = parts[0].rpartition(" ")
            if city and len(tail) == 2 and normalize_state(tail):
                parts = [city, tail]

        places = Place.objects.all()
        if len(parts) == 2:
            state = normalize_state(parts[1])
            if state is None:
                return None
            places = places.filter(key=normalize_place_name(parts[0]), state=state)
        elif len(parts) == 1:
            # A bare city name: take the most populous match, but only when it is a
            # real city; otherwise let the fallback geocoder disambiguate.
            places = places.filter(key=normalize_place_name(parts[0]), population__gte=50_000)
        else:
            return None  # street addresses etc. are the fallback geocoder's job

        place = places.order_by("-population").first()
        if place is None:
            return None
        return ResolvedLocation(
            query=query,
            label=f"{place.name}, {place.state}",
            coordinate=Coordinate(place.latitude, place.longitude),
            source="gazetteer",
        )

    def _from_fallback(self, query: str) -> ResolvedLocation | None:
        if self._fallback is None:
            return None
        normalised = " ".join(query.lower().split())
        cache_key = "geocode:" + hashlib.sha256(normalised.encode()).hexdigest()

        cached = self._cache.get(cache_key)
        if cached == _CACHE_MISS:
            return None
        if cached is not None:
            label, latitude, longitude = cached
            return ResolvedLocation(query, label, Coordinate(latitude, longitude), "geocoder")

        hit = self._fallback.geocode(query)  # provider errors propagate and are never cached
        if hit is None:
            self._cache.set(cache_key, _CACHE_MISS, self._miss_ttl)
            return None
        self._cache.set(
            cache_key, (hit.label, hit.coordinate.latitude, hit.coordinate.longitude), self._hit_ttl
        )
        return ResolvedLocation(query, hit.label, hit.coordinate, "geocoder", external_calls=1)


def _in_contiguous_us(coordinate: Coordinate) -> bool:
    box = _CONTIGUOUS_US
    return (
        box["south"] <= coordinate.latitude <= box["north"]
        and box["west"] <= coordinate.longitude <= box["east"]
    )
