"""Use case: plan a route and its fuel stops.

``RoutePlanner`` is the only place where the pieces meet: location resolution,
the routing provider, the station index and the optimiser. It knows nothing
about HTTP; views translate its results and exceptions.

Fuel model (every figure is echoed back to the client under ``assumptions``):

* The vehicle departs with a full tank and may arrive with only its reserve.
* A reserve (``FUEL_RESERVE_MILES``) is never planned into: station positions are
  city-level, so a plan that arrives at a pump with exactly 0.0 gallons would
  run dry in practice. Planning range = range - reserve, so no leg exceeds the
  vehicle's range.
* ``total_fuel_cost`` prices *every* gallon the trip burns: what is bought at the
  stops, plus the fuel burned out of the starting tank valued at a stated
  reference price. Otherwise a 300-mile trip would "cost" $0.
"""

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from django.core.cache import BaseCache

from apps.planner.geocoding import LocationResolver, ResolvedLocation
from apps.stations.index import RouteMatches, StationIndex, StationRecord
from domain import geometry
from domain.optimizer import InfeasibleRouteError, Purchase, plan_fuel_stops
from providers.base import (
    NoRouteFoundError,
    ProviderUnavailableError,
    Route,
    RoutingProvider,
)

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_HUNDREDTH = Decimal("0.01")
_MILL = Decimal("0.001")

REFERENCE_AVERAGE_PAID = "average_price_paid_at_stops"
REFERENCE_CHEAPEST_ON_ROUTE = "cheapest_station_on_route"
REFERENCE_DATASET_AVERAGE = "dataset_average_price"


class SameLocationError(Exception):
    """Start and finish resolve to the same point."""


class FuelDataNotLoadedError(Exception):
    """The station table is empty: ``manage.py bootstrap_data`` has not been run."""


class NoFeasibleFuelPlanError(Exception):
    """Some stretch of the route has no station within the vehicle's range."""

    def __init__(self, cause: InfeasibleRouteError, corridor_miles: float):
        self.gap_start_miles = round(cause.gap_start_miles, 1)
        self.gap_end_miles = round(cause.gap_end_miles, 1)
        self.reachable_miles = round(cause.reachable_miles, 1)
        self.corridor_miles = corridor_miles
        super().__init__(
            f"No fuel station within {corridor_miles:g} miles of the route between mile "
            f"{self.gap_start_miles} and mile {self.gap_end_miles}, a stretch longer than the "
            f"{self.reachable_miles}-mile planning range. (Routes that leave the United "
            "States for long stretches cannot be served: the fuel data is US-only.)"
        )


@dataclass(frozen=True, slots=True)
class VehicleProfile:
    range_miles: float
    miles_per_gallon: float
    reserve_miles: float

    @property
    def tank_capacity_gallons(self) -> float:
        return self.range_miles / self.miles_per_gallon

    @property
    def reserve_gallons(self) -> float:
        return self.reserve_miles / self.miles_per_gallon

    @property
    def usable_gallons(self) -> float:
        """Tank capacity the planner is allowed to burn between fill-ups."""
        return self.tank_capacity_gallons - self.reserve_gallons


@dataclass(frozen=True, slots=True)
class PlannerOptions:
    stop_penalty_usd: float
    # Tried in order; a wider corridor is used only if the narrower one leaves a
    # stretch with no reachable station.
    corridor_tiers_miles: tuple[float, ...]
    sample_spacing_miles: float
    geometry_tolerance_miles: float
    route_cache_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class StationOnRoute:
    station: StationRecord
    mile_marker: float
    off_route_miles: float
    route_latitude: float
    route_longitude: float


@dataclass(frozen=True, slots=True)
class FuelStop:
    sequence: int
    location: StationOnRoute
    gallons: Decimal
    cost: Decimal
    fuel_on_arrival_gallons: float


@dataclass(frozen=True, slots=True)
class RoutePlan:
    start: ResolvedLocation
    finish: ResolvedLocation
    distance_miles: float
    duration_seconds: float
    geometry: np.ndarray  # simplified (lat, lon) rows, for display only
    stops: tuple[FuelStop, ...]
    # Only set when no stop is needed: the cheapest place to top up on the way.
    optional_top_up: StationOnRoute | None

    fuel_purchased_cost: Decimal
    gallons_purchased: Decimal
    trip_fuel_gallons: Decimal  # distance / mpg
    starting_fuel_gallons_used: Decimal  # trip_fuel_gallons - gallons_purchased
    starting_fuel_cost_estimate: Decimal
    reference_price: Decimal
    reference_price_basis: str
    total_fuel_cost: Decimal
    # Cost of the strictly cheapest purchases (stop_penalty = 0), for comparison.
    cheapest_possible_purchase_cost: Decimal

    vehicle: VehicleProfile
    stop_penalty_usd: float
    corridor_miles: float
    candidate_stations: int
    warnings: tuple[str, ...]
    routing_provider: str
    routing_calls: int
    geocoding_calls: int
    upstream_ms: float


class RoutePlanner:
    def __init__(
        self,
        *,
        resolver: LocationResolver,
        routing: RoutingProvider,
        station_index: StationIndex,
        cache: BaseCache,
        vehicle: VehicleProfile,
        options: PlannerOptions,
    ):
        self._resolver = resolver
        self._routing = routing
        self._stations = station_index
        self._cache = cache
        self._vehicle = vehicle
        self._options = options

    def plan(self, start: str, finish: str, stop_penalty: float | None = None) -> RoutePlan:
        if not len(self._stations):
            raise FuelDataNotLoadedError

        origin = self._resolver.resolve(start)
        destination = self._resolver.resolve(finish)
        if origin.coordinate == destination.coordinate:
            raise SameLocationError

        route, points, display_geometry, routing_calls, upstream_ms = self._load_route(
            origin, destination
        )

        # Mile markers come from the route geometry, rescaled so that the last one
        # equals the provider's own road distance (haversine over vertices drifts ~0.1%).
        miles = geometry.cumulative_miles(points)
        if miles[-1] > 0:
            miles *= route.distance_miles / miles[-1]
        samples, sample_miles = geometry.resample(points, miles, self._options.sample_spacing_miles)

        penalty = self._options.stop_penalty_usd if stop_penalty is None else stop_penalty
        candidates, purchases, corridor = self._plan_purchases(
            samples, sample_miles, route.distance_miles, penalty
        )

        stops = self._price(purchases, candidates)
        purchased_cost = _total_cost(stops)
        gallons_purchased = sum((stop.gallons for stop in stops), Decimal("0.00"))
        if penalty > 0 and stops:
            cheapest = self._optimise(candidates, route.distance_miles, stop_penalty=0.0)
            # Both figures are sums of per-stop rounded lines, so the lower bound could land
            # a cent or two above the plan it bounds; never report it that way.
            cheapest_cost = min(_total_cost(self._price(cheapest, candidates)), purchased_cost)
        else:
            cheapest_cost = purchased_cost

        # The trip burns distance / mpg. Whatever was not bought on the way came out of
        # the starting tank, so the two parts always add up to the trip's fuel.
        trip_gallons = _quantize(route.distance_miles / self._vehicle.miles_per_gallon, _HUNDREDTH)
        starting_used = max(trip_gallons - gallons_purchased, Decimal("0.00"))

        optional_top_up = None
        if stops:
            # Volume-weighted average over the *unrounded* purchases. Dividing rounded
            # dollars by rounded gallons is badly wrong for a small purchase (0.03 gal
            # for $0.10 would read as $3.333/gal) and that error is then multiplied by
            # a whole tank.
            prices = self._stations.prices[candidates.station_indices]
            volume = sum(purchase.gallons for purchase in purchases)
            spend = sum(purchase.gallons * prices[purchase.index] for purchase in purchases)
            reference_price, basis = _quantize(spend / volume, _MILL), REFERENCE_AVERAGE_PAID
        elif len(candidates):
            optional_top_up = self._station_on_route(
                candidates, int(np.argmin(self._stations.prices[candidates.station_indices]))
            )
            reference_price, basis = optional_top_up.station.price, REFERENCE_CHEAPEST_ON_ROUTE
        else:
            reference_price = _quantize(float(self._stations.prices.mean()), _MILL)
            basis = REFERENCE_DATASET_AVERAGE
        starting_cost = (starting_used * reference_price).quantize(_CENT, ROUND_HALF_UP)

        warnings = []
        if corridor > self._options.corridor_tiers_miles[0]:
            warnings.append(
                f"No workable plan within {self._options.corridor_tiers_miles[0]:g} miles of the "
                f"route; stations up to {corridor:g} miles away were considered. Detour mileage "
                "is not included in the fuel figures."
            )

        plan = RoutePlan(
            start=origin,
            finish=destination,
            distance_miles=route.distance_miles,
            duration_seconds=route.duration_seconds,
            geometry=display_geometry,
            stops=tuple(stops),
            optional_top_up=optional_top_up,
            fuel_purchased_cost=purchased_cost,
            gallons_purchased=gallons_purchased,
            trip_fuel_gallons=trip_gallons,
            starting_fuel_gallons_used=starting_used,
            starting_fuel_cost_estimate=starting_cost,
            reference_price=reference_price,
            reference_price_basis=basis,
            total_fuel_cost=purchased_cost + starting_cost,
            cheapest_possible_purchase_cost=cheapest_cost,
            vehicle=self._vehicle,
            stop_penalty_usd=penalty,
            corridor_miles=corridor,
            candidate_stations=len(candidates),
            warnings=tuple(warnings),
            routing_provider=self._routing.name,
            routing_calls=routing_calls,
            geocoding_calls=origin.external_calls + destination.external_calls,
            upstream_ms=upstream_ms,
        )
        logger.info(
            "route_plan start=%r finish=%r miles=%.1f candidates=%d stops=%d total_cost=%s "
            "routing_calls=%d geocoding_calls=%d upstream_ms=%.0f",
            origin.label,
            destination.label,
            plan.distance_miles,
            plan.candidate_stations,
            len(plan.stops),
            plan.total_fuel_cost,
            plan.routing_calls,
            plan.geocoding_calls,
            plan.upstream_ms,
        )
        return plan

    # --- steps -------------------------------------------------------------------------------

    def _load_route(
        self, origin: ResolvedLocation, destination: ResolvedLocation
    ) -> tuple[Route, np.ndarray, np.ndarray, int, float]:
        """Return ``(route, points, display_geometry, external_calls, upstream_ms)``.

        This is the only place a routing call can happen, and it happens at most
        once per request. The simplified display geometry is cached alongside the
        route because simplifying is the most expensive local step (~15 ms for a
        coast-to-coast route), which would otherwise dominate cache-hit requests.
        """
        a, b = origin.coordinate, destination.coordinate
        # 5 decimal places is ~1 m: inputs that close share a route.
        cache_key = (
            f"route:{self._routing.name}:{a.latitude:.5f},{a.longitude:.5f}:"
            f"{b.latitude:.5f},{b.longitude:.5f}"
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            route, display_geometry = cached
            points = geometry.decode_polyline(route.encoded_polyline, route.polyline_precision)
            return route, points, display_geometry, 0, 0.0

        started = time.perf_counter()
        route = self._routing.route(a, b)  # provider errors propagate and are never cached
        upstream_ms = (time.perf_counter() - started) * 1000

        try:
            points = geometry.decode_polyline(route.encoded_polyline, route.polyline_precision)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderUnavailableError("The routing provider returned bad geometry.") from exc
        if len(points) < 2:
            raise NoRouteFoundError("The routing provider returned an empty route.")
        display_geometry = geometry.simplify(points, self._options.geometry_tolerance_miles)
        self._cache.set(cache_key, (route, display_geometry), self._options.route_cache_ttl_seconds)
        return route, points, display_geometry, 1, upstream_ms

    def _plan_purchases(
        self, samples: np.ndarray, sample_miles: np.ndarray, distance: float, penalty: float
    ) -> tuple[RouteMatches, list[Purchase], float]:
        """Optimise within the narrowest corridor that yields a feasible plan."""
        tiers = self._options.corridor_tiers_miles
        for corridor in tiers:
            matches = self._stations.match_route(samples, sample_miles, corridor)
            candidates = self._cheapest_per_mile_marker(matches)
            try:
                return candidates, self._optimise(candidates, distance, penalty), corridor
            except InfeasibleRouteError as exc:
                if corridor == tiers[-1]:
                    raise NoFeasibleFuelPlanError(exc, corridor) from exc
        raise AssertionError("corridor_tiers_miles must not be empty")

    def _optimise(
        self, candidates: RouteMatches, distance: float, stop_penalty: float
    ) -> list[Purchase]:
        return plan_fuel_stops(
            candidates.mile_markers.tolist(),
            self._stations.prices[candidates.station_indices].tolist(),
            distance,
            # The reserve is carved out of the tank, so the optimiser's "empty" is "on reserve".
            tank_capacity_gallons=self._vehicle.usable_gallons,
            miles_per_gallon=self._vehicle.miles_per_gallon,
            start_fuel_gallons=self._vehicle.usable_gallons,  # departs full
            stop_penalty=stop_penalty,
        )

    def _cheapest_per_mile_marker(self, matches: RouteMatches) -> RouteMatches:
        """Keep only the cheapest station at each mile marker.

        Stations are geocoded to city centroids, so every truck stop in a town
        lands on the same marker. Only the cheapest of them can ever be part of
        an optimal plan, so dropping the rest is lossless and hands the
        optimiser the strictly increasing positions it requires.
        """
        if not len(matches):
            return matches
        prices = self._stations.prices[matches.station_indices]
        order = np.lexsort((prices, matches.mile_markers))  # by marker, then price
        first_of_marker = np.concatenate(([True], np.diff(matches.mile_markers[order]) > 0))
        return matches.take(order[first_of_marker])

    def _station_on_route(self, candidates: RouteMatches, index: int) -> StationOnRoute:
        return StationOnRoute(
            station=self._stations.records[candidates.station_indices[index]],
            mile_marker=float(candidates.mile_markers[index]),
            off_route_miles=float(candidates.off_route_miles[index]),
            route_latitude=float(candidates.route_points[index, 0]),
            route_longitude=float(candidates.route_points[index, 1]),
        )

    def _price(self, purchases: list[Purchase], candidates: RouteMatches) -> list[FuelStop]:
        """Turn optimiser output into priced stops.

        Money is computed from the rounded quantities shown to the client, so
        ``gallons x price == cost`` holds exactly for every line of the response.
        """
        stops = []
        for sequence, purchase in enumerate(purchases, start=1):
            location = self._station_on_route(candidates, purchase.index)
            # A stop that buys less than the display precision is still a stop.
            gallons = max(_quantize(purchase.gallons, _HUNDREDTH), _HUNDREDTH)
            stops.append(
                FuelStop(
                    sequence=sequence,
                    location=location,
                    gallons=gallons,
                    cost=(gallons * location.station.price).quantize(_CENT, ROUND_HALF_UP),
                    fuel_on_arrival_gallons=(
                        max(purchase.fuel_on_arrival, 0.0) + self._vehicle.reserve_gallons
                    ),
                )
            )
        return stops


def _total_cost(stops: list[FuelStop]) -> Decimal:
    return sum((stop.cost for stop in stops), Decimal("0.00"))


def _quantize(value: float, quantum: Decimal) -> Decimal:
    return Decimal(repr(float(value))).quantize(quantum, ROUND_HALF_UP)
