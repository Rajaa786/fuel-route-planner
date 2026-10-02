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
        (httpx.Response(200, json={"code": ["Ok"], "routes": [{}]}), ProviderUnavailableError),
        (httpx.Response(400, json={"code": {"a": 1}}), ProviderUnavailableError),
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


@pytest.mark.parametrize(
    "route",
    [
        '{"distance": NaN, "duration": 1, "geometry": "a"}',
        '{"distance": 1e999, "duration": 1, "geometry": "a"}',
        '{"distance": 1e30, "duration": 1, "geometry": "a"}',
        '{"distance": -5, "duration": 1, "geometry": "a"}',
        '{"distance": 5000, "duration": NaN, "geometry": "a"}',
        '{"distance": 5000, "duration": -1, "geometry": "a"}',
        '{"distance": 5000, "duration": 1e999, "geometry": "a"}',
        '{"distance": 1' + "0" * 400 + ', "duration": 1, "geometry": "a"}',  # int too big for float
    ],
)
def test_osrm_rejects_numbers_that_are_not_a_road(route):
    body = '{"code": "Ok", "routes": [' + route + "]}"
    response = httpx.Response(200, content=body, headers={"Content-Type": "application/json"})
    with pytest.raises(ProviderUnavailableError):
        osrm(lambda request: response).route(START, FINISH)


def test_osrm_zero_length_route_is_passed_through_for_the_planner_to_judge():
    body = {"code": "Ok", "routes": [{"distance": 0, "duration": 0, "geometry": GEOMETRY}]}
    assert (
        osrm(lambda request: httpx.Response(200, json=body)).route(START, FINISH).distance_miles
        == 0
    )


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


def test_nominatim_free_text_searches_worldwide_and_reports_the_country():
    seen = []

    def handler(request):
        seen.append(request)
        body = [
            {
                "lat": "19.4326",
                "lon": "-99.1332",
                "display_name": "Ciudad de México",
                "address": {"country_code": "MX"},
            }
        ]
        return httpx.Response(200, json=body)

    hit = nominatim(handler).geocode("Mexico City")

    # No country restriction: restricted to the US, this query "finds" a street in Pittsburgh.
    assert dict(seen[0].url.params) == {
        "q": "Mexico City",
        "format": "jsonv2",
        "limit": "1",
        "addressdetails": "1",
    }
    assert (hit.coordinate, hit.label, hit.country_code) == (
        Coordinate(19.4326, -99.1332),
        "Ciudad de México",
        "mx",
    )


@pytest.mark.parametrize("query", ["60601", " 60601 ", "60601-1234"])
def test_nominatim_zip_codes_are_a_us_postcode_search(query):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[{"lat": "41.88", "lon": "-87.62"}])

    hit = nominatim(handler).geocode(query)

    assert seen[0]["postalcode"] == "60601" and seen[0]["countrycodes"] == "us"
    assert "q" not in seen[0]
    assert (hit.label, hit.country_code) == (query, None)


def test_nominatim_address_lookup_is_structured():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[{"lat": "40.7484", "lon": "-73.9857"}])

    nominatim(handler).geocode_address("350 Fifth Avenue", "New York City", "NY")

    assert seen[0] == {
        "street": "350 Fifth Avenue",
        "city": "New York City",
        "state": "NY",
        "countrycodes": "us",
        "format": "jsonv2",
        "limit": "1",
        "addressdetails": "1",
    }


def test_nominatim_no_result_is_none_and_errors_raise():
    assert nominatim(lambda request: httpx.Response(200, json=[])).geocode("zzz") is None
    with pytest.raises(ProviderUnavailableError):
        nominatim(lambda request: httpx.Response(403, json={"error": "blocked"})).geocode("zzz")


@pytest.mark.parametrize(
    "payload",
    [
        [{}],
        [None],
        [{"lat": "north", "lon": "1"}],
        [{"lat": "NaN", "lon": "1"}],
        [{"lat": "inf", "lon": "1"}],
        [{"lat": int("1" + "0" * 400), "lon": "1"}],
        {"a": 1},
    ],
)
def test_nominatim_unexpected_payloads_are_provider_errors(payload):
    with pytest.raises(ProviderUnavailableError):
        nominatim(lambda request: httpx.Response(200, json=payload)).geocode("zzz")


def test_nominatim_never_holds_its_lock_while_waiting_or_calling(monkeypatch):
    """With eight request threads, a lock held across a slow call stalls the whole service."""
    held_during = []

    def handler(request):
        held_during.append(("http", geocoder._lock.locked()))
        return httpx.Response(200, json=[])

    geocoder = NominatimGeocoder(
        httpx.Client(transport=httpx.MockTransport(handler)), "http://nominatim.test"
    )
    clock = {"now": 100.0}
    sleeps = []

    def fake_sleep(seconds):
        held_during.append(("sleep", geocoder._lock.locked()))
        sleeps.append(seconds)

    monkeypatch.setattr("providers.nominatim.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("providers.nominatim.time.sleep", fake_sleep)

    # Six callers arriving at the same instant are booked one second apart...
    for _ in range(6):
        geocoder.geocode("zzz")
    assert sleeps == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert len(held_during) == 11 and not any(held for _, held in held_during)

    # ...and the seventh would wait longer than the cap, so it is refused instead.
    with pytest.raises(ProviderRateLimitedError) as excinfo:
        geocoder.geocode("zzz")
    assert excinfo.value.retry_after_seconds == 6
