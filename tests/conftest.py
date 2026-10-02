import pytest
from django.core.cache import cache, caches

from apps.planner.geocoding import LocationResolver
from apps.planner.services import PlannerOptions, RoutePlanner, VehicleProfile
from apps.stations.index import get_station_index, reset_station_index
from tests.helpers import FakeRoutingProvider


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """The cache and the station index are process-wide; reset them around each test."""
    for alias in ("default", "throttle"):
        caches[alias].clear()
    reset_station_index()
    yield
    for alias in ("default", "throttle"):
        caches[alias].clear()
    reset_station_index()


@pytest.fixture
def routing():
    return FakeRoutingProvider()


@pytest.fixture
def make_planner(db):
    """Build a planner against whatever stations the test has created."""

    def build(routing, *, fallback=None, **option_overrides) -> RoutePlanner:
        options = {
            "stop_penalty_usd": 2.0,
            "corridor_tiers_miles": (5.0, 10.0, 25.0),
            "sample_spacing_miles": 0.25,
            "geometry_tolerance_miles": 0.05,
            "route_cache_ttl_seconds": 60,
        } | option_overrides
        return RoutePlanner(
            resolver=LocationResolver(fallback, cache, hit_ttl_seconds=60, miss_ttl_seconds=60),
            routing=routing,
            station_index=get_station_index(),
            cache=cache,
            vehicle=VehicleProfile(range_miles=500.0, miles_per_gallon=10.0, reserve_miles=25.0),
            options=PlannerOptions(**options),
        )

    return build
