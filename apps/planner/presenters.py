"""Shape a :class:`~apps.planner.services.RoutePlan` into the public JSON contract.

The contract is documented by ``RoutePlanResponseSerializer``; a test asserts
that this module and that serializer describe the same fields.
"""

import numpy as np

from apps.planner.geocoding import ResolvedLocation
from apps.planner.services import FuelStop, RoutePlan, StationOnRoute


def present_plan(plan: RoutePlan, *, map_url: str, elapsed_ms: float) -> dict:
    vehicle = plan.vehicle
    return {
        "start": _location(plan.start),
        "finish": _location(plan.finish),
        "route": {
            "distance_miles": round(plan.distance_miles, 1),
            "duration_hours": round(plan.duration_seconds / 3600, 2),
            "geometry": {
                "type": "LineString",
                # GeoJSON order is [longitude, latitude]; 5 dp is ~1 m.
                "coordinates": np.round(plan.geometry[:, ::-1], 5).tolist(),
            },
        },
        "fuel_stops": [_fuel_stop(stop) for stop in plan.stops],
        "optional_top_up": (
            _station_on_route(plan.optional_top_up) if plan.optional_top_up else None
        ),
        "summary": {
            "total_fuel_cost": float(plan.total_fuel_cost),
            "fuel_purchased_cost": float(plan.fuel_purchased_cost),
            "starting_fuel_cost_estimate": float(plan.starting_fuel_cost_estimate),
            "trip_fuel_gallons": float(plan.trip_fuel_gallons),
            "gallons_purchased": float(plan.gallons_purchased),
            "starting_fuel_gallons_used": float(plan.starting_fuel_gallons_used),
            "reference_price_per_gallon": float(plan.reference_price),
            "reference_price_basis": plan.reference_price_basis,
            "fuel_stops": len(plan.stops),
        },
        "assumptions": {
            "vehicle_range_miles": vehicle.range_miles,
            "miles_per_gallon": vehicle.miles_per_gallon,
            "tank_capacity_gallons": round(vehicle.tank_capacity_gallons, 2),
            "reserve_miles": vehicle.reserve_miles,
            "stop_penalty_usd": plan.stop_penalty_usd,
            "notes": _notes(plan),
        },
        "warnings": list(plan.warnings),
        "map_url": map_url,
        "meta": {
            "routing_provider": plan.routing_provider,
            "external_api_calls": {
                "routing": plan.routing_calls,
                "geocoding": plan.geocoding_calls,
            },
            "candidate_stations": plan.candidate_stations,
            "corridor_miles": plan.corridor_miles,
            "cheapest_possible_purchase_cost": float(plan.cheapest_possible_purchase_cost),
            "upstream_ms": round(plan.upstream_ms, 1),
            "elapsed_ms": round(elapsed_ms, 1),
        },
    }


def _location(location: ResolvedLocation) -> dict:
    return {
        "query": location.query,
        "name": location.label,
        "latitude": round(location.coordinate.latitude, 5),
        "longitude": round(location.coordinate.longitude, 5),
        "resolved_by": location.source,
    }


def _station_on_route(location: StationOnRoute) -> dict:
    station = location.station
    return {
        "opis_id": station.opis_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "price_per_gallon": float(station.price),
        "mile_marker": round(location.mile_marker, 1),
        "miles_off_route": round(location.off_route_miles, 1),
        "latitude": round(location.route_latitude, 5),
        "longitude": round(location.route_longitude, 5),
    }


def _fuel_stop(stop: FuelStop) -> dict:
    return {
        "stop": stop.sequence,
        **_station_on_route(stop.location),
        "fuel_on_arrival_gallons": round(stop.fuel_on_arrival_gallons, 2),
        "gallons": float(stop.gallons),
        "cost": float(stop.cost),
    }


def _notes(plan: RoutePlan) -> list[str]:
    vehicle = plan.vehicle
    planning_range = vehicle.range_miles - vehicle.reserve_miles
    notes = [
        "The vehicle departs with a full tank.",
        f"Legs are planned to at most {planning_range:g} miles, keeping a "
        f"{vehicle.reserve_miles:g}-mile reserve, because station positions are city-level.",
        "total_fuel_cost prices every gallon the trip burns: fuel bought at the stops plus "
        "fuel burned from the starting tank valued at reference_price_per_gallon.",
        "Stations are located by city; latitude/longitude of a stop is the point on the route "
        "nearest to that city. Detour mileage is not included.",
    ]
    if not plan.stops:
        notes.append("The trip is within range of the starting tank, so no stop is required.")
    return notes
