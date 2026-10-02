"""OSRM routing client (https://project-osrm.org/docs/v5.24.0/api/)."""

import httpx

from domain.geometry import METERS_PER_MILE
from providers.base import Coordinate, NoRouteFoundError, ProviderUnavailableError, Route
from providers.http import get_json

# OSRM answers these with HTTP 400; they mean "your points are not joined by road".
_NO_ROUTE_CODES = {"NoRoute", "NoSegment"}


class OSRMRoutingProvider:
    """Fetches a full-resolution driving route in a single HTTP call."""

    name = "osrm"

    def __init__(self, client: httpx.Client, base_url: str):
        self._client = client
        self._base_url = base_url.rstrip("/")

    def route(self, start: Coordinate, finish: Coordinate) -> Route:
        # OSRM expects lon,lat order.
        path = (
            f"{start.longitude:.6f},{start.latitude:.6f};"
            f"{finish.longitude:.6f},{finish.latitude:.6f}"
        )
        params = {
            "overview": "full",  # full geometry: needed to place stations along the road
            "geometries": "polyline6",  # ~7x smaller than GeoJSON on the wire
            "steps": "false",
            "alternatives": "false",
        }
        _, payload = get_json(
            self._client, f"{self._base_url}/route/v1/driving/{path}", params, provider="OSRM"
        )

        code = payload.get("code") if isinstance(payload, dict) else None
        if code in _NO_ROUTE_CODES:
            raise NoRouteFoundError("No drivable route between the two locations.")
        if code != "Ok" or not payload.get("routes"):
            message = payload.get("message", "") if isinstance(payload, dict) else ""
            raise ProviderUnavailableError(f"OSRM error {code!r}: {message}")

        try:
            best = payload["routes"][0]
            route = Route(
                distance_miles=float(best["distance"]) / METERS_PER_MILE,
                duration_seconds=float(best["duration"]),
                encoded_polyline=best["geometry"],
                polyline_precision=6,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderUnavailableError("OSRM returned an unexpected payload.") from exc
        if not isinstance(route.encoded_polyline, str) or route.distance_miles <= 0:
            raise ProviderUnavailableError("OSRM returned an unexpected payload.")
        return route
