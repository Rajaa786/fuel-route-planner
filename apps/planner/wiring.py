"""Composition root: builds the long-lived objects from Django settings.

Everything here is created lazily on first use (i.e. after gunicorn has forked),
so sockets and locks are never shared across processes.
"""

from functools import cache

from django.conf import settings
from django.core.cache import cache as django_cache

from apps.planner.geocoding import LocationResolver
from apps.planner.services import PlannerOptions, RoutePlanner, VehicleProfile
from apps.stations.index import get_station_index
from providers.http import build_client
from providers.nominatim import NominatimGeocoder
from providers.osrm import OSRMRoutingProvider


@cache
def _http_client():
    return build_client(
        user_agent=settings.HTTP_USER_AGENT,
        connect_timeout=settings.HTTP_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.HTTP_READ_TIMEOUT_SECONDS,
    )


@cache
def _routing_provider() -> OSRMRoutingProvider:
    return OSRMRoutingProvider(_http_client(), settings.OSRM_BASE_URL)


@cache
def _location_resolver() -> LocationResolver:
    return LocationResolver(
        fallback=NominatimGeocoder(_http_client(), settings.NOMINATIM_BASE_URL),
        cache=django_cache,
        hit_ttl_seconds=settings.GEOCODE_CACHE_TTL_SECONDS,
        miss_ttl_seconds=settings.GEOCODE_MISS_CACHE_TTL_SECONDS,
    )


def get_route_planner() -> RoutePlanner:
    """Assemble a planner. Cheap: every collaborator is a cached singleton."""
    return RoutePlanner(
        resolver=_location_resolver(),
        routing=_routing_provider(),
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
