import pytest
from django.core.cache import caches
from django.urls import reverse
from rest_framework import serializers
from rest_framework.test import APIClient

from apps.planner import views
from apps.planner.serializers import RoutePlanResponseSerializer
from apps.planner.throttling import ClientRateThrottle
from apps.stations.models import Place
from providers.base import (
    Coordinate,
    GeocodeHit,
    NoRouteFoundError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Route,
)
from tests.helpers import (
    ROAD_LATITUDE,
    FakeGeocoder,
    FakeRoutingProvider,
    longitude_at_mile,
    make_station,
)

URL = reverse("planner:route-plan")
MAP_URL = reverse("planner:route-map")
TRIP = {"start": "Westville, NE", "finish": "Eastville, OH"}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def world(db, make_planner, monkeypatch):
    """Two towns 1,000 miles apart on the test road, three stations between them."""
    Place.objects.bulk_create(
        [
            Place(key="WESTVILLE", state="NE", name="Westville", latitude=ROAD_LATITUDE,
                  longitude=longitude_at_mile(0), population=10),
            Place(key="EASTVILLE", state="OH", name="Eastville", latitude=ROAD_LATITUDE,
                  longitude=longitude_at_mile(1000), population=10),
            Place(key="NEARBURG", state="NE", name="Nearburg", latitude=ROAD_LATITUDE,
                  longitude=longitude_at_mile(300), population=10),
        ]
    )  # fmt: skip
    make_station(1, 300, "3.000", name='PILOT <script>alert("x")</script>')
    make_station(2, 450, "2.500")
    make_station(3, 800, "3.500")
    routing = FakeRoutingProvider()
    monkeypatch.setattr(views, "get_route_planner", lambda: make_planner(routing))
    return routing


def assert_matches_contract(payload):
    """The payload has exactly the documented fields, with the documented types."""
    _assert_same_fields(payload, RoutePlanResponseSerializer())
    # "testserver" is not a URL the URLField accepts; the host is irrelevant to the contract.
    candidate = {**payload, "map_url": payload["map_url"].replace("testserver", "localhost")}
    serializer = RoutePlanResponseSerializer(data=candidate)
    assert serializer.is_valid(), serializer.errors


def _assert_same_fields(payload, serializer, path="response"):
    assert set(payload) == set(serializer.fields), f"{path}: field mismatch"
    for name, field in serializer.fields.items():
        value = payload[name]
        if isinstance(field, serializers.ListSerializer):
            for index, item in enumerate(value):
                _assert_same_fields(item, field.child, f"{path}.{name}[{index}]")
        elif isinstance(field, serializers.Serializer) and value is not None:
            _assert_same_fields(value, field, f"{path}.{name}")


def test_plan_returns_route_stops_total_cost_and_map_link(client, world):
    response = client.get(URL, {"start": "Westville, NE", "finish": "Eastville, OH"})

    assert response.status_code == 200
    body = response.json()
    assert_matches_contract(body)

    assert body["start"] == {
        "query": "Westville, NE",
        "name": "Westville, NE",
        "latitude": 40.0,
        "longitude": -100.0,
        "resolved_by": "gazetteer",
    }
    assert body["route"]["distance_miles"] == pytest.approx(1000, abs=0.5)
    geometry = body["route"]["geometry"]
    assert geometry["type"] == "LineString" and len(geometry["coordinates"]) >= 2
    assert geometry["coordinates"][0] == [-100.0, 40.0]  # GeoJSON is [lon, lat]

    assert [stop["opis_id"] for stop in body["fuel_stops"]] == [2, 3]
    assert [stop["stop"] for stop in body["fuel_stops"]] == [1, 2]
    summary = body["summary"]
    assert summary["fuel_stops"] == 2
    assert summary["fuel_purchased_cost"] == pytest.approx(
        sum(stop["cost"] for stop in body["fuel_stops"])
    )
    assert summary["total_fuel_cost"] == pytest.approx(
        summary["fuel_purchased_cost"] + summary["starting_fuel_cost_estimate"]
    )
    assert summary["trip_fuel_gallons"] == pytest.approx(100, abs=0.1)  # 1,000 mi at 10 mpg
    assert body["assumptions"]["vehicle_range_miles"] == 500
    assert body["assumptions"]["miles_per_gallon"] == 10

    assert body["meta"]["external_api_calls"] == {"routing": 1, "geocoding": 0}
    assert body["map_url"].startswith("http://testserver" + MAP_URL)
    assert "start=Westville" in body["map_url"]


def test_repeat_request_makes_no_external_call(client, world):
    params = {"start": "Westville, NE", "finish": "Eastville, OH"}
    client.get(URL, params)
    body = client.get(URL, params).json()

    assert body["meta"]["external_api_calls"] == {"routing": 0, "geocoding": 0}
    assert world.calls == 1


def test_short_trip_reports_optional_top_up_and_a_non_zero_total(client, world):
    body = client.get(URL, {"start": "Westville, NE", "finish": "Nearburg, NE"}).json()

    assert body["fuel_stops"] == []
    assert body["optional_top_up"]["opis_id"] == 1
    assert body["summary"]["total_fuel_cost"] == pytest.approx(90.0, abs=0.2)  # 30 gal at $3
    assert_matches_contract(body)


def test_stop_penalty_parameter_is_applied(client, world):
    params = {"start": "Westville, NE", "finish": "Eastville, OH", "stop_penalty": "0"}
    assert client.get(URL, params).json()["assumptions"]["stop_penalty_usd"] == 0


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"start": "Westville, NE"}, "finish"),
        ({"finish": "Westville, NE"}, "start"),
        ({"start": "", "finish": "Eastville, OH"}, "start"),
        ({"start": "x" * 201, "finish": "Eastville, OH"}, "start"),
        ({**TRIP, "stop_penalty": "-1"}, "stop_penalty"),
        ({**TRIP, "stop_penalty": "abc"}, "stop_penalty"),
    ],
)
def test_invalid_parameters_return_400_with_field_errors(client, world, params, field):
    response = client.get(URL, params)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert field in error["details"]
    assert world.calls == 0


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"start": "Nowhere, NE", "finish": "Eastville, OH"}, "location_not_found"),
        ({"start": "Toronto, ON", "finish": "Eastville, OH"}, "location_outside_service_area"),
        ({"start": "Westville, NE", "finish": "Westville, NE"}, "same_start_and_finish"),
    ],
)
def test_unusable_locations_return_422(client, world, params, code):
    response = client.get(URL, params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert world.calls == 0


def test_route_with_unbridgeable_gap_returns_422_with_the_gap(client, world):
    far = f"{ROAD_LATITUDE},{longitude_at_mile(1600):.5f}"
    response = client.get(URL, {"start": "Westville, NE", "finish": far})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "no_feasible_fuel_plan"
    assert error["details"]["gap_start_miles"] == pytest.approx(800, abs=0.5)
    assert error["details"]["gap_end_miles"] == pytest.approx(1600, abs=0.5)


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (ProviderUnavailableError("down"), 502, "upstream_unavailable"),
        (ProviderTimeoutError("slow"), 504, "upstream_timeout"),
        (ProviderRateLimitedError("busy", retry_after_seconds=9), 503, "upstream_rate_limited"),
        (NoRouteFoundError("No drivable route."), 422, "no_route_found"),
    ],
)
def test_upstream_failures_are_translated(client, world, failure, status, code):
    world.error = failure
    response = client.get(URL, {"start": "Westville, NE", "finish": "Eastville, OH"})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    if status == 503:
        assert response["Retry-After"] == "9"


def test_malformed_upstream_geometry_returns_502(client, world, monkeypatch):
    intact = FakeRoutingProvider.route

    def truncated(self, start, finish):
        route = intact(self, start, finish)
        return Route(route.distance_miles, route.duration_seconds, route.encoded_polyline[:-1])

    monkeypatch.setattr(FakeRoutingProvider, "route", truncated)
    response = client.get(URL, TRIP)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_json_and_map_endpoints_share_one_throttle_budget(client, world, monkeypatch):
    monkeypatch.setattr(ClientRateThrottle, "THROTTLE_RATES", {"anon": "3/min"})

    statuses = [client.get(URL, TRIP).status_code for _ in range(2)]
    statuses += [client.get(MAP_URL, TRIP).status_code for _ in range(2)]

    assert statuses == [200, 200, 200, 429]
    limited = client.get(URL, TRIP)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited["Retry-After"]) > 0


def test_throttle_cannot_be_dodged_with_a_forged_forwarded_header(client, world, monkeypatch):
    monkeypatch.setattr(ClientRateThrottle, "THROTTLE_RATES", {"anon": "3/min"})

    statuses = [
        client.get(URL, TRIP, HTTP_X_FORWARDED_FOR=f"203.0.113.{n}").status_code for n in range(5)
    ]
    assert statuses == [200, 200, 200, 429, 429]


def test_throttle_counters_do_not_share_the_route_cache(client, world):
    client.get(URL, TRIP)
    assert all(not str(key).startswith(":1:throttle") for key in caches["default"]._cache)
    assert any("throttle" in str(key) for key in caches["throttle"]._cache)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_only_get_is_allowed(client, world, method):
    response = getattr(client, method)(URL, TRIP)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert world.calls == 0


def test_errors_tell_the_caller_what_to_send_instead(client, world):
    missing = client.get(URL, {"start": "Westville, NE"}).json()["error"]
    assert "?start=Chicago, IL&finish=Dallas, TX" in missing["message"]
    assert list(missing["details"]) == ["finish"]

    # The same message must not misdescribe a different mistake.
    negative = client.get(URL, {**TRIP, "stop_penalty": "-1"}).json()["error"]
    assert "required" not in negative["message"] and list(negative["details"]) == ["stop_penalty"]

    unknown = client.get(URL, {"start": "Nowhere, NE", "finish": "Eastville, OH"}).json()["error"]
    assert unknown["details"]["query"] == "Nowhere, NE"
    assert any("Chicago, IL" in example for example in unknown["details"]["accepted_formats"])

    same = client.get(URL, {"start": "Westville, NE", "finish": "westville ne"}).json()["error"]
    assert "Westville, NE" in same["message"]  # says what both sides resolved to


def test_ambiguous_city_returns_the_candidates_without_any_external_call(client, world):
    Place.objects.bulk_create(
        [
            Place(key="PEORIA", state="AZ", name="Peoria", latitude=33.58, longitude=-112.24,
                  population=190_000),
            Place(key="PEORIA", state="IL", name="Peoria", latitude=40.69, longitude=-89.59,
                  population=115_000),
        ]
    )  # fmt: skip
    response = client.get(URL, {"start": "Peoria", "finish": "Eastville, OH"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "location_ambiguous"
    assert error["details"] == {"query": "Peoria", "candidates": ["Peoria, AZ", "Peoria, IL"]}
    assert world.calls == 0


def test_format_query_parameter_is_ignored(client, world):
    response = client.get(URL, {**TRIP, "format": "xml"})
    assert response.status_code == 200 and response["Content-Type"] == "application/json"
    assert client.get(MAP_URL, {**TRIP, "format": "xml"}).status_code == 200
    # ...but only on the plan endpoints: the schema keeps its documented ?format=json.
    schema = client.get("/api/schema/", {"format": "json"})
    assert schema.status_code == 200 and "json" in schema["Content-Type"]


def test_foreign_match_reports_what_was_understood(client, world, make_planner, monkeypatch):
    hit = GeocodeHit(Coordinate(43.65, -79.38), "Napoli, Campania, Italia", "it")
    planner = make_planner(world, fallback=FakeGeocoder({"Naples": hit}))
    monkeypatch.setattr(views, "get_route_planner", lambda: planner)

    response = client.get(URL, {"start": "Naples", "finish": "Eastville, OH"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "location_outside_service_area"
    assert error["details"] == {"query": "Naples", "matched": "Napoli, Campania, Italia"}
    assert "add its state" in error["message"]


def test_any_provider_error_is_a_502_never_a_500(client, world):
    world.error = ProviderError("a kind of failure added later")
    response = client.get(URL, TRIP)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_unexpected_failure_is_a_json_500(world, monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(views, "get_route_planner", broken)
    response = APIClient(raise_request_exception=False).get(URL, TRIP)

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred.",
            "details": {},
        }
    }


def test_missing_fuel_data_returns_503(client, db, make_planner, monkeypatch):
    monkeypatch.setattr(views, "get_route_planner", lambda: make_planner(FakeRoutingProvider()))
    response = client.get(URL, {"start": "40,-100", "finish": "40,-95"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "fuel_data_not_loaded"


def test_json_responses_carry_a_deny_all_content_security_policy(client, world):
    response = client.get(URL, {"start": "Westville, NE", "finish": "Eastville, OH"})
    assert response["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert response["X-Content-Type-Options"] == "nosniff"


# --- map page ------------------------------------------------------------------------------


def test_map_page_renders_plan_without_an_extra_routing_call(client, world):
    params = {"start": "Westville, NE", "finish": "Eastville, OH"}
    client.get(URL, params)
    response = client.get(MAP_URL, params)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    html = response.content.decode()
    assert 'id="plan-data"' in html and "leaflet" in html
    assert "Westville, NE" in html and "Station 2" in html
    assert world.calls == 1  # served from the route cache


def test_map_page_escapes_untrusted_text_and_sets_a_nonce_policy(client, world):
    params = {"start": "Westville, NE", "finish": "Nearburg, NE"}  # top-up = station 1
    response = client.get(MAP_URL, params)
    html = response.content.decode()

    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;alert" in html  # escaped in the HTML panel
    assert "\\u003Cscript\\u003E" in html  # and in the embedded JSON

    policy = response["Content-Security-Policy"]
    assert "default-src 'none'" in policy and "'unsafe-inline'" not in policy
    nonce = policy.split("script-src 'nonce-")[1].split("'")[0]
    assert html.count(f'nonce="{nonce}"') == 3  # style, leaflet, inline script


def test_map_page_is_served_to_a_client_that_only_accepts_html(client, world):
    response = client.get(MAP_URL, TRIP, HTTP_ACCEPT="text/html")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")


def test_map_page_shows_errors_as_html(client, world):
    bad_input = client.get(MAP_URL, {"start": "Westville, NE"})
    unknown = client.get(MAP_URL, {"start": "Nowhere, NE", "finish": "Eastville, OH"})
    state = client.get(MAP_URL, {"start": "Wyoming", "finish": "Eastville, OH"})

    assert state.status_code == 422 and b"is a state" in state.content
    assert b"?start=Chicago, IL&amp;finish=Dallas, TX" in bad_input.content  # how to fix it

    assert bad_input.status_code == 400 and b"Invalid request parameters" in bad_input.content
    assert unknown.status_code == 422 and b"Could not find a US location" in unknown.content


# --- service endpoints ---------------------------------------------------------------------


def test_health_reflects_whether_fuel_data_is_loaded(client, db):
    assert client.get("/health/").status_code == 503
    make_station(1, 100, "3.000")
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "fuel_stations": 1}


def test_site_root_leads_to_the_api_docs(client, db):
    response = client.get("/")
    assert response.status_code == 302 and response["Location"] == "/api/docs/"


def test_health_check_is_not_redirected_to_https(client, db, settings):
    settings.SECURE_SSL_REDIRECT = True
    make_station(1, 100, "3.000")

    assert client.get("/health/").status_code == 200  # a redirect fails a load balancer probe
    assert client.get(URL, TRIP).status_code == 301


def test_swagger_ui_assets_are_pinned(client, db):
    html = client.get("/api/docs/").content.decode()
    assert "swagger-ui-dist@5.33.1" in html and "@latest" not in html


def test_unknown_url_returns_the_json_error_envelope(client, db):
    response = client.get("/api/v1/nope/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_openapi_schema_documents_the_endpoint(client, db):
    response = client.get("/api/schema/", HTTP_ACCEPT="application/json")
    assert response.status_code == 200
    operation = response.json()["paths"]["/api/v1/route-plan/"]["get"]
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert set(parameters) == {"start", "finish", "stop_penalty"}
    # The documented limits are the ones the serializer enforces...
    assert parameters["start"]["required"] and parameters["start"]["schema"]["maxLength"] == 200
    assert parameters["stop_penalty"]["schema"]["minimum"] == 0
    assert parameters["stop_penalty"]["schema"]["maximum"] == 1000
    # ...and each location parameter comes with examples to try.
    assert len(parameters["start"]["examples"]) == 5 and len(parameters["finish"]["examples"]) == 3
    assert client.get("/api/docs/").status_code == 200
