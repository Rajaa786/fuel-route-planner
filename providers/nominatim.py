"""Nominatim (OpenStreetMap) geocoding client.

Only used as a fallback for inputs the offline gazetteer cannot resolve, such as
street addresses. See https://operations.osmfoundation.org/policies/nominatim/.
"""

import math
import threading
import time

import httpx

from providers.base import (
    Coordinate,
    GeocodeHit,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from providers.http import get_json


class NominatimGeocoder:
    name = "nominatim"

    # The public instance allows at most one request per second.
    _MIN_INTERVAL_SECONDS = 1.0
    # Beyond this queue we fail fast rather than park request threads behind the limiter.
    _MAX_QUEUE_SECONDS = 5.0

    def __init__(self, client: httpx.Client, base_url: str):
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def geocode(self, query: str) -> GeocodeHit | None:
        self._wait_for_slot()
        params = {"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"}
        status, payload = get_json(
            self._client, f"{self._base_url}/search", params, provider="Nominatim"
        )
        if status != httpx.codes.OK or not isinstance(payload, list):
            raise ProviderUnavailableError(f"Nominatim returned HTTP {status}.")
        if not payload:
            return None
        try:
            hit = payload[0]
            coordinate = Coordinate(latitude=float(hit["lat"]), longitude=float(hit["lon"]))
            label = str(hit.get("display_name") or query)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderUnavailableError("Nominatim returned an unexpected payload.") from exc
        return GeocodeHit(coordinate=coordinate, label=label)

    def _wait_for_slot(self) -> None:
        """Space calls one interval apart, per process.

        The lock is held only long enough to book a start time. Sleeping and the
        HTTP call happen outside it, so one slow response cannot stall every
        other thread.
        """
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_slot)
            if start_at - now > self._MAX_QUEUE_SECONDS:
                raise ProviderRateLimitedError(
                    "Too many geocoding requests are queued.",
                    retry_after_seconds=math.ceil(start_at - now),
                )
            self._next_slot = start_at + self._MIN_INTERVAL_SECONDS
        if start_at > now:
            time.sleep(start_at - now)
