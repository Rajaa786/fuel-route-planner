"""In-memory spatial index over all fuel stations.

The station table is tiny (a few thousand rows) and read-only at request time,
so each process loads it once into numpy arrays. Matching stations to a route is
then pure vector maths: no SQL and no per-station Python loop on the hot path.
"""

import threading
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from scipy.spatial import cKDTree

from apps.stations.models import FuelStation
from domain import geometry


@dataclass(frozen=True, slots=True)
class StationRecord:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price: Decimal


@dataclass(frozen=True, slots=True)
class RouteMatches:
    """Stations inside the route corridor, ordered by mile marker."""

    station_indices: np.ndarray  # positions into StationIndex.records
    mile_markers: np.ndarray  # distance along the route of the closest route sample
    off_route_miles: np.ndarray  # great-circle distance from the station to that sample
    route_points: np.ndarray  # (lat, lon) of that sample: where the driver leaves the road

    def take(self, selection: np.ndarray) -> "RouteMatches":
        return RouteMatches(
            self.station_indices[selection],
            self.mile_markers[selection],
            self.off_route_miles[selection],
            self.route_points[selection],
        )

    def __len__(self) -> int:
        return len(self.station_indices)


class StationIndex:
    def __init__(self, records: list[StationRecord]):
        self.records = tuple(records)
        self.prices = np.array([float(r.price) for r in records], dtype=np.float64)
        self._xyz = geometry.to_unit_xyz(
            np.array([r.latitude for r in records], dtype=np.float64),
            np.array([r.longitude for r in records], dtype=np.float64),
        )

    def __len__(self) -> int:
        return len(self.records)

    @classmethod
    def from_database(cls) -> "StationIndex":
        rows = FuelStation.objects.order_by("opis_id").values_list(
            "opis_id", "name", "address", "city", "state", "latitude", "longitude", "retail_price"
        )
        return cls([StationRecord(*row) for row in rows.iterator()])

    def match_route(
        self, sample_points: np.ndarray, sample_miles: np.ndarray, corridor_miles: float
    ) -> RouteMatches:
        """Find every station within ``corridor_miles`` of a (densely sampled) route.

        A KD-tree is built over the route samples and queried once with all
        stations, so each station gets its single nearest route sample: that
        sample's mile marker is where the driver would pull off.
        """
        if not len(self) or not len(sample_points):
            empty = np.empty(0)
            return RouteMatches(np.empty(0, dtype=np.intp), empty, empty, np.empty((0, 2)))

        tree = cKDTree(geometry.to_unit_xyz(sample_points[:, 0], sample_points[:, 1]))
        chord, nearest = tree.query(
            self._xyz, k=1, distance_upper_bound=geometry.miles_to_chord(corridor_miles)
        )
        inside = np.flatnonzero(np.isfinite(chord))  # misses come back as inf
        samples = nearest[inside]
        order = np.argsort(sample_miles[samples], kind="stable")
        return RouteMatches(
            station_indices=inside[order],
            mile_markers=sample_miles[samples][order],
            off_route_miles=geometry.chord_to_miles(chord[inside][order]),
            route_points=sample_points[samples][order],
        )


_index: StationIndex | None = None
_index_lock = threading.Lock()


def get_station_index() -> StationIndex:
    """Return the process-wide index, building it on first use.

    Built lazily (after gunicorn forks) and behind a lock, so concurrent first
    requests on a threaded worker trigger exactly one load.
    """
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                index = StationIndex.from_database()
                if not len(index):
                    # Not cached: the data may be loaded while this process is running.
                    return index
                _index = index
    return _index


def reset_station_index() -> None:
    """Drop the cached index (after an import, and between tests)."""
    global _index
    with _index_lock:
        _index = None
