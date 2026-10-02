"""Shared test doubles and a synthetic geography.

The test world is a single road running due east along latitude 40N. One degree
of longitude there is about 53 miles, so positions are easy to reason about and
no test ever needs the network.
"""

from decimal import Decimal

import numpy as np

from apps.stations.models import FuelStation
from domain import geometry
from providers.base import Coordinate, Route

ROAD_LATITUDE = 40.0
ROAD_START_LONGITUDE = -100.0


def encode_polyline(points, precision=6) -> str:
    """Reference encoder (textbook algorithm), the inverse of ``decode_polyline``."""
    factor = 10**precision
    output, previous = [], (0, 0)
    for lat, lon in points:
        current = (round(lat * factor), round(lon * factor))
        for delta in (current[0] - previous[0], current[1] - previous[1]):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                output.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            output.append(chr(value + 63))
        previous = current
    return "".join(output)


def road_points(start_longitude: float, finish_longitude: float) -> np.ndarray:
    longitudes = np.linspace(start_longitude, finish_longitude, 400)
    return np.column_stack((np.full_like(longitudes, ROAD_LATITUDE), longitudes))


def road_miles(start_longitude: float, finish_longitude: float) -> float:
    return float(geometry.cumulative_miles(road_points(start_longitude, finish_longitude))[-1])


def longitude_at_mile(mile: float, start_longitude: float = ROAD_START_LONGITUDE) -> float:
    """Longitude of the point ``mile`` miles east of ``start_longitude`` on the road."""
    miles_per_degree = road_miles(start_longitude, start_longitude + 1.0)
    return start_longitude + mile / miles_per_degree


class FakeRoutingProvider:
    """Returns the straight road between two points and counts its calls."""

    name = "fake"

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.error = error

    def route(self, start: Coordinate, finish: Coordinate) -> Route:
        self.calls += 1
        if self.error is not None:
            raise self.error
        points = road_points(start.longitude, finish.longitude)
        distance = float(geometry.cumulative_miles(points)[-1])
        return Route(
            distance_miles=distance,
            duration_seconds=distance / 60 * 3600,
            encoded_polyline=encode_polyline(points),
        )


class FakeGeocoder:
    name = "fake-geocoder"

    def __init__(self, hits: dict | None = None, error: Exception | None = None):
        self.hits = hits or {}
        self.error = error
        self.calls = 0

    def geocode(self, query: str):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.hits.get(query)


def make_station(opis_id: int, mile: float, price: str, *, miles_north: float = 0.0, **extra):
    """Create a station ``mile`` miles along the road, optionally offset to the north."""
    return FuelStation.objects.create(
        opis_id=opis_id,
        name=extra.pop("name", f"Station {opis_id}"),
        address=extra.pop("address", f"I-80, EXIT {opis_id}"),
        city=extra.pop("city", f"Town {opis_id}"),
        state="NE",
        retail_price=Decimal(price),
        latitude=ROAD_LATITUDE + miles_north / 69.09,  # one degree of latitude ~ 69.09 mi
        longitude=longitude_at_mile(mile),
        **extra,
    )
