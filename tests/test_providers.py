"""Provider clients against ``httpx.MockTransport``: no sockets are opened."""

import httpx
import pytest

from providers.base import (
    Coordinate,
    NoRouteFoundError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from providers.nominatim import NominatimGeocoder
from providers.osrm import OSRMRoutingProvider
from tests.helpers import encode_polyline

START, FINISH = Coordinate(41.85, -87.65), Coordinate(32.78, -96.8)
GEOMETRY = encode_polyline([(41.85, -87.65), (37.0, -92.0), (32.78, -96.8)])


def osrm(handler) -> OSRMRoutingProvider:
    return OSRMRoutingProvider(
        httpx.Client(transport=httpx.MockTransport(handler)), "http://osrm.test/"
    )


def test_osrm_requests_one_full_geometry_route_in_lon_lat_order():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {
            "code": "Ok",
            "routes": [{"distance": 1609.344 * 925, "duration": 50_000, "geometry": GEOMETRY}],
        }
        return httpx.Response(200, json=body)

    route = osrm(handler).route(START, FINISH)

    assert len(seen) == 1
    assert seen[0].url.path == "/route/v1/driving/-87.650000,41.850000;-96.800000,32.780000"
    assert dict(seen[0].url.params) == {
        "overview": "full",
        "geometries": "polyline6",
        "steps": "false",
        "alternatives": "false",
    }
    assert route.distance_miles == pytest.approx(925)
    assert (route.duration_seconds, route.encoded_polyline, route.polyline_precision) == (
        50_000.0,
        GEOMETRY,
        6,
    )


@pytest.mark.parametrize("code", ["NoRoute", "NoSegment"])
def test_osrm_no_route_is_a_client_problem_not_an_outage(code):
    def handler(request):
        return httpx.Response(400, json={"code": code, "message": "Impossible route"})

    with pytest.raises(NoRouteFoundError):
        osrm(handler).route(START, FINISH)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(500, text="boom"), ProviderUnavailableError),
        (httpx.Response(200, text="<html>not json</html>"), ProviderUnavailableError),
        (httpx.Response(400, json={"code": "InvalidQuery"}), ProviderUnavailableError),
        (httpx.Response(200, json={"code": "Ok", "routes": []}), ProviderUnavailableError),
        (httpx.Response(200, json={"code": "Ok", "routes": [{}]}), ProviderUnavailableError),
        (httpx.Response(200, json={"code": "Ok", "routes": [None]}), ProviderUnavailableError),
        (httpx.Response(200, json=[1, 2]), ProviderUnavailableError),
        (
            httpx.Response(
                200,
                json={"code": "Ok", "routes": [{"distance": None, "duration": 1, "geometry": "a"}]},
            ),
            ProviderUnavailableError,
        ),
        (
            httpx.Response(
                200,
                json={"code": "Ok", "routes": [{"distance": 5.0, "duration": 1, "geometry": 7}]},
            ),
            ProviderUnavailableError,
        ),
        (httpx.Response(429, headers={"Retry-After": "7"}), ProviderRateLimitedError),
    ],
)  # fmt: skip
def test_osrm_failures_map_to_provider_errors(response, expected):
    with pytest.raises(expected) as excinfo:
        osrm(lambda request: response).route(START, FINISH)
    if expected is ProviderRateLimitedError:
        assert excinfo.value.retry_after_seconds == 7


def test_osrm_timeout_and_connection_errors():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderTimeoutError):
        osrm(timeout).route(START, FINISH)
    with pytest.raises(ProviderUnavailableError):
        osrm(refused).route(START, FINISH)


def nominatim(handler) -> NominatimGeocoder:
    geocoder = NominatimGeocoder(
        httpx.Client(transport=httpx.MockTransport(handler)), "http://nominatim.test"
    )
    geocoder._MIN_INTERVAL_SECONDS = 0.0  # no politeness delay against a mock
    return geocoder


def test_nominatim_restricts_to_the_us_and_parses_the_best_hit():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json=[{"lat": "38.8977", "lon": "-77.0365", "display_name": "White House"}]
        )

    hit = nominatim(handler).geocode("1600 Pennsylvania Ave NW")

    assert dict(seen[0].url.params) == {
        "q": "1600 Pennsylvania Ave NW",
        "format": "jsonv2",
        "limit": "1",
        "countrycodes": "us",
    }
    assert (hit.coordinate, hit.label) == (Coordinate(38.8977, -77.0365), "White House")


def test_nominatim_no_result_is_none_and_errors_raise():
    assert nominatim(lambda request: httpx.Response(200, json=[])).geocode("zzz") is None
    with pytest.raises(ProviderUnavailableError):
        nominatim(lambda request: httpx.Response(403, json={"error": "blocked"})).geocode("zzz")


@pytest.mark.parametrize("payload", [[{}], [None], [{"lat": "north", "lon": "1"}], {"a": 1}])
def test_nominatim_unexpected_payloads_are_provider_errors(payload):
    with pytest.raises(ProviderUnavailableError):
        nominatim(lambda request: httpx.Response(200, json=payload)).geocode("zzz")


def test_nominatim_spaces_calls_without_holding_the_lock_and_fails_fast_when_queued(monkeypatch):
    geocoder = NominatimGeocoder(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))),
        "http://nominatim.test",
    )
    clock = {"now": 100.0}
    sleeps = []
    monkeypatch.setattr("providers.nominatim.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("providers.nominatim.time.sleep", sleeps.append)

    # Six callers arriving at the same instant are booked one second apart...
    for _ in range(6):
        geocoder.geocode("zzz")
        assert not geocoder._lock.locked()  # nothing is held across the HTTP call
    assert sleeps == [1.0, 2.0, 3.0, 4.0, 5.0]

    # ...and the seventh would wait longer than the cap, so it is refused instead.
    with pytest.raises(ProviderRateLimitedError) as excinfo:
        geocoder.geocode("zzz")
    assert excinfo.value.retry_after_seconds == 6
