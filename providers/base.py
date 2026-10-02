"""Provider-agnostic contracts for the external map services.

Nothing in this package imports Django: clients receive their configuration
through constructors, which keeps them unit-testable with ``httpx.MockTransport``
and makes swapping a provider a wiring change rather than a code change.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Coordinate:
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Route:
    """A driving route. Geometry stays encoded: compact to cache, cheap to decode."""

    distance_miles: float
    duration_seconds: float
    encoded_polyline: str
    polyline_precision: int = 6


@dataclass(frozen=True, slots=True)
class GeocodeHit:
    coordinate: Coordinate
    label: str
    # ISO 3166-1 alpha-2, lower case, when the provider reports it.
    country_code: str | None = None


class ProviderError(Exception):
    """Base class for failures talking to an external map service."""


class ProviderUnavailableError(ProviderError):
    """Network failure, 5xx or an unintelligible response."""


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider did not answer within the configured timeout."""


class ProviderRateLimitedError(ProviderUnavailableError):
    """The provider throttled us (HTTP 429)."""

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class NoRouteFoundError(ProviderError):
    """The provider is healthy but no drivable route joins the two points."""


class RoutingProvider(Protocol):
    name: str

    def route(self, start: Coordinate, finish: Coordinate) -> Route: ...


class Geocoder(Protocol):
    name: str

    def geocode(self, query: str) -> GeocodeHit | None:
        """Best match for free text, anywhere in the world."""

    def geocode_address(self, street: str, city: str, state: str) -> GeocodeHit | None:
        """A street address inside a known US city."""
