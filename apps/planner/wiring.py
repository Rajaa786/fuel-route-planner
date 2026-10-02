"""Composition root: builds the long-lived objects from Django settings.

Everything here is created lazily on first use (i.e. after gunicorn has forked),
so sockets and locks are never shared across processes.
"""

import threading
from functools import cache

from django.conf import settings
from django.core.cache import cache as django_cache

from apps.planner.geocoding import LocationResolver
from apps.planner.services import PlannerOptions, RoutePlanner, VehicleProfile
from apps.stations.index import get_station_index
from providers.http import build_client
from providers.nominatim import NominatimGeocoder
from providers.osrm import OSRMRoutingProvider

_build_lock = threading.Lock()


@cache
def _build_shared() -> tuple[OSRMRoutingProvider, LocationResolver]:
    client = build_client(
        user_agent=settings.HTTP_USER_AGENT,
        connect_timeout=settings.HTTP_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.HTTP_READ_TIMEOUT_SECONDS,
    )
    resolver = LocationResolver(
        fallback=NominatimGeocoder(client, settings.NOMINATIM_BASE_URL),
        cache=django_cache,
        hit_ttl_seconds=settings.GEOCODE_CACHE_TTL_SECONDS,
        miss_ttl_seconds=settings.GEOCODE_MISS_CACHE_TTL_SECONDS,
    )
    return OSRMRoutingProvider(client, settings.OSRM_BASE_URL), resolver


def _shared() -> tuple[OSRMRoutingProvider, LocationResolver]:
    """The process-wide HTTP client, router and resolver, built exactly once.

    ``functools.cache`` alone does not stop several threads from each building
    their own copy on a cold start, which would mean several geocoder rate
    limiters; the lock makes the first call single-flight.
    """
    with _build_lock:
        return _build_shared()


def get_route_planner() -> RoutePlanner:
    """Assemble a planner. Cheap: every collaborator is a cached singleton."""
    routing, resolver = _shared()
    return RoutePlanner(
        resolver=resolver,
        routing=routing,
        station_index=get_station_index(),
        cache=django_cache,
        vehicle=VehicleProfile(
            range_miles=settings.VEHICLE_RANGE_MILES,
            miles_per_gallon=settings.VEHICLE_MILES_PER_GALLON,
            reserve_miles=settings.FUEL_RESERVE_MILES,
        ),
        options=PlannerOptions(
            stop_penalty_usd=settings.FUEL_STOP_PENALTY_USD,
            corridor_tiers_miles=settings.FUEL_CORRIDOR_TIERS_MILES,
            sample_spacing_miles=settings.ROUTE_SAMPLE_SPACING_MILES,
            geometry_tolerance_miles=settings.ROUTE_GEOMETRY_TOLERANCE_MILES,
            route_cache_ttl_seconds=settings.ROUTE_CACHE_TTL_SECONDS,
        ),
    )
