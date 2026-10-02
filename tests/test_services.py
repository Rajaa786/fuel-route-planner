from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

import pytest

from apps.planner.services import (
    REFERENCE_AVERAGE_PAID,
    REFERENCE_CHEAPEST_ON_ROUTE,
    REFERENCE_DATASET_AVERAGE,
    FuelDataNotLoadedError,
    NoFeasibleFuelPlanError,
    SameLocationError,
)
from providers.base import ProviderUnavailableError, Route
from tests.helpers import ROAD_LATITUDE, FakeRoutingProvider, longitude_at_mile, make_station


def at(mile: float) -> str:
    """A "lat,lon" query for the point ``mile`` miles along the test road."""
    return f"{ROAD_LATITUDE},{longitude_at_mile(mile):.6f}"


def test_long_trip_buys_where_fuel_is_cheap_and_accounts_for_every_gallon(make_planner, routing):
    make_station(1, 300, "3.000")
    make_station(2, 450, "2.500")  # cheapest: fill up here
    make_station(3, 800, "3.500")  # dear: buy only what is unavoidable
    plan = make_planner(routing).plan(at(0), at(1000))

    assert plan.distance_miles == pytest.approx(1000, abs=0.5)
    assert [stop.location.station.opis_id for stop in plan.stops] == [2, 3]

    cheap, dear = plan.stops
    assert cheap.location.mile_marker == pytest.approx(450, abs=0.5)
    # Departed with 47.5 usable gallons, burned 45: arrives with 2.5 usable + 2.5 reserve.
    assert cheap.fuel_on_arrival_gallons == pytest.approx(5.0, abs=0.1)
    assert cheap.gallons == pytest.approx(Decimal("45.00"), abs=Decimal("0.10"))  # fills up
    assert dear.gallons == pytest.approx(Decimal("7.50"), abs=Decimal("0.10"))  # the remainder
    assert dear.fuel_on_arrival_gallons == pytest.approx(12.5 + 2.5, abs=0.1)

    # Every line is self-consistent to the cent...
    for stop in plan.stops:
        exact = stop.gallons * stop.location.station.price
        assert stop.cost == exact.quantize(Decimal("0.01"), ROUND_HALF_UP)
    assert plan.fuel_purchased_cost == cheap.cost + dear.cost
    # ...and the total prices all the fuel burned at 10 mpg.
    assert plan.trip_fuel_gallons == Decimal("100.00")  # 1,000 miles at 10 mpg
    assert plan.starting_fuel_gallons_used + plan.gallons_purchased == plan.trip_fuel_gallons
    assert plan.starting_fuel_gallons_used == pytest.approx(Decimal("47.50"), abs=Decimal("0.02"))
    assert plan.reference_price_basis == REFERENCE_AVERAGE_PAID
    assert plan.total_fuel_cost == plan.fuel_purchased_cost + plan.starting_fuel_cost_estimate
    assert plan.total_fuel_cost == pytest.approx(
        plan.trip_fuel_gallons * plan.reference_price, abs=Decimal("0.10")
    )


def test_no_leg_exceeds_the_planning_range(make_planner, routing):
    for opis_id, mile in enumerate(range(50, 1500, 70), start=1):
        make_station(opis_id, mile, f"{2.5 + (opis_id * 37 % 11) / 10:.3f}")
    plan = make_planner(routing).plan(at(0), at(1500))

    markers = [0.0, *(stop.location.mile_marker for stop in plan.stops), plan.distance_miles]
    assert max(b - a for a, b in pairwise(markers)) <= 475 + 0.5
    assert all(stop.fuel_on_arrival_gallons >= 2.5 - 1e-6 for stop in plan.stops)  # reserve kept
    assert all(stop.gallons > 0 for stop in plan.stops)


def test_short_trip_needs_no_stop_and_suggests_the_cheapest_station(make_planner, routing):
    make_station(1, 100, "3.200")
    make_station(2, 200, "2.800")
    plan = make_planner(routing).plan(at(0), at(300))

    assert plan.stops == ()
    assert plan.optional_top_up.station.opis_id == 2
    assert plan.fuel_purchased_cost == Decimal("0.00")
    assert plan.reference_price_basis == REFERENCE_CHEAPEST_ON_ROUTE
    assert plan.starting_fuel_gallons_used == pytest.approx(Decimal("30.00"), abs=Decimal("0.02"))
    assert plan.total_fuel_cost == pytest.approx(Decimal("84.00"), abs=Decimal("0.10"))


def test_short_trip_with_no_station_nearby_uses_the_dataset_average(make_planner, routing):
    make_station(1, 100, "3.000", miles_north=200)
    make_station(2, 200, "4.000", miles_north=200)
    plan = make_planner(routing).plan(at(0), at(300))

    assert plan.stops == () and plan.optional_top_up is None
    assert plan.reference_price_basis == REFERENCE_DATASET_AVERAGE
    assert plan.reference_price == Decimal("3.500")


def test_only_the_cheapest_station_in_a_town_is_considered(make_planner, routing):
    make_station(1, 400, "3.400", city="Sametown")
    make_station(2, 400, "2.900", city="Sametown")
    make_station(3, 400, "3.100", city="Sametown")
    plan = make_planner(routing).plan(at(0), at(800))

    assert plan.candidate_stations == 1
    assert [stop.location.station.opis_id for stop in plan.stops] == [2]


def test_stop_penalty_trades_a_little_fuel_cost_for_fewer_stops(make_planner, routing):
    make_station(1, 300, "2.990")
    make_station(2, 320, "3.000")
    make_station(3, 600, "3.500")
    planner = make_planner(routing)

    cheapest = planner.plan(at(0), at(900), stop_penalty=0.0)
    practical = planner.plan(at(0), at(900), stop_penalty=2.0)

    assert len(cheapest.stops) == 3 and len(practical.stops) == 2
    assert practical.fuel_purchased_cost > cheapest.fuel_purchased_cost
    assert practical.cheapest_possible_purchase_cost == cheapest.fuel_purchased_cost
    assert cheapest.cheapest_possible_purchase_cost == cheapest.fuel_purchased_cost


def test_corridor_widens_only_when_the_narrow_one_is_infeasible(make_planner, routing):
    make_station(1, 400, "3.000", miles_north=8)  # outside 5 mi, inside 10 mi
    plan = make_planner(routing).plan(at(0), at(800))

    assert plan.corridor_miles == 10.0
    assert [stop.location.station.opis_id for stop in plan.stops] == [1]
    assert plan.stops[0].location.off_route_miles == pytest.approx(8, abs=0.3)
    assert len(plan.warnings) == 1 and "10 miles" in plan.warnings[0]


def test_narrow_corridor_is_kept_when_it_works(make_planner, routing):
    make_station(1, 400, "3.500")
    make_station(2, 410, "2.000", miles_north=8)  # cheaper, but not needed for feasibility
    plan = make_planner(routing).plan(at(0), at(800))

    assert plan.corridor_miles == 5.0 and plan.warnings == ()
    assert [stop.location.station.opis_id for stop in plan.stops] == [1]


def test_stretch_with_no_station_is_reported_as_infeasible(make_planner, routing):
    make_station(1, 100, "3.000")
    make_station(2, 700, "3.000")
    with pytest.raises(NoFeasibleFuelPlanError) as excinfo:
        make_planner(routing).plan(at(0), at(1300))

    error = excinfo.value
    assert (error.gap_start_miles, error.gap_end_miles) == pytest.approx((100, 700), abs=0.5)
    assert error.reachable_miles == 475.0
    assert error.corridor_miles == 25.0


def test_stop_coordinates_are_the_point_on_the_route(make_planner, routing):
    make_station(1, 400, "3.000", miles_north=3)
    stop = make_planner(routing).plan(at(0), at(800)).stops[0]

    assert stop.location.route_latitude == pytest.approx(ROAD_LATITUDE, abs=1e-3)
    assert stop.location.route_longitude == pytest.approx(longitude_at_mile(400), abs=0.01)
    assert stop.location.off_route_miles == pytest.approx(3, abs=0.2)


def test_route_is_fetched_once_and_then_served_from_cache(make_planner, routing):
    make_station(1, 400, "3.000")
    planner = make_planner(routing)

    first = planner.plan(at(0), at(800))
    second = planner.plan(at(0), at(800))
    planner.plan(at(0), at(800), stop_penalty=0.0)  # a different penalty reuses the route too

    assert routing.calls == 1
    assert (first.routing_calls, second.routing_calls) == (1, 0)
    assert second.stops == first.stops and second.total_fuel_cost == first.total_fuel_cost


def test_routing_failures_are_not_cached(make_planner):
    make_station(1, 400, "3.000")
    failing = FakeRoutingProvider(error=ProviderUnavailableError("down"))
    planner = make_planner(failing)
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            planner.plan(at(0), at(800))
    assert failing.calls == 2


def test_same_start_and_finish_is_rejected_before_routing(make_planner, routing):
    make_station(1, 400, "3.000")
    with pytest.raises(SameLocationError):
        make_planner(routing).plan(at(10), at(10))
    assert routing.calls == 0


def test_missing_fuel_data_is_a_distinct_failure(make_planner, routing):
    with pytest.raises(FuelDataNotLoadedError):
        make_planner(routing).plan(at(0), at(800))
    assert routing.calls == 0


@pytest.mark.parametrize(
    "trip_miles", [475.01, 475.02, 475.04, 475.1, 475.3, 476.0, 480.0, 499.9, 500.0, 525.0]
)
def test_trips_just_beyond_the_planning_range_are_priced_sanely(make_planner, routing, trip_miles):
    """Regression: a purchase of a few hundredths of a gallon must not distort the total."""
    make_station(1, 200, "3.199")
    plan = make_planner(routing).plan(at(0), at(trip_miles))

    [stop] = plan.stops
    assert stop.gallons >= Decimal("0.01")  # never a "0.00 gal" stop
    assert plan.reference_price == Decimal("3.199")  # one stop: exactly its price
    assert plan.trip_fuel_gallons == Decimal(str(plan.distance_miles / 10)).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    )
    assert plan.starting_fuel_gallons_used + plan.gallons_purchased == plan.trip_fuel_gallons
    # Every gallon is priced at $3.199, give or take per-line cent rounding.
    expected = plan.trip_fuel_gallons * Decimal("3.199")
    assert plan.total_fuel_cost == pytest.approx(expected, abs=Decimal("0.02"))


def test_reported_lower_bound_never_exceeds_the_plan(make_planner, routing):
    prices = ["2.899", "3.149", "2.959", "3.282", "3.059", "2.921", "3.014", "3.199", "2.979"]
    for opis_id, mile in enumerate(range(40, 2000, 37), start=1):
        make_station(opis_id, mile, prices[opis_id * 7 % len(prices)])
    planner = make_planner(routing)

    for trip_miles in range(600, 1700, 97):  # the test road leaves the US box near mile 1,760
        for penalty in (0.5, 2.0, 10.0):
            plan = planner.plan(at(0), at(trip_miles), stop_penalty=penalty)
            assert plan.cheapest_possible_purchase_cost <= plan.fuel_purchased_cost
            assert all(stop.gallons >= Decimal("0.01") for stop in plan.stops)


def test_malformed_route_geometry_is_an_upstream_failure_and_is_not_cached(make_planner):
    class BrokenGeometryProvider(FakeRoutingProvider):
        def route(self, start, finish):
            route = super().route(start, finish)
            return Route(route.distance_miles, route.duration_seconds, route.encoded_polyline[:-1])

    make_station(1, 400, "3.000")
    provider = BrokenGeometryProvider()
    planner = make_planner(provider)
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            planner.plan(at(0), at(800))
    assert provider.calls == 2
