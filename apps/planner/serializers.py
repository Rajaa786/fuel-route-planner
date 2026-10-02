"""Request validation and the documented response contract.

The response serializers describe the JSON for OpenAPI; the payload itself is
assembled by :func:`apps.planner.presenters.present_plan`. A test asserts the
two never drift apart.
"""

from rest_framework import serializers

LOCATION_HELP = (
    "A US location. The state and the comma are optional for large cities: "
    "`Chicago, IL`, `Chicago IL`, `Chicago, Illinois` and `Chicago` all work, as does "
    "`41.8781,-87.6298`. Street addresses, ZIP codes, landmarks and small towns given "
    "without a state also work, at the cost of one geocoding call."
)


class RoutePlanQuerySerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text="Start. " + LOCATION_HELP)
    finish = serializers.CharField(max_length=200, help_text="Finish. " + LOCATION_HELP)
    stop_penalty = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=1000,
        help_text=(
            "Dollars a fuel stop must save to be worth making. 0 returns the strictly "
            "cheapest plan; the server default is 2."
        ),
    )


class LocationSerializer(serializers.Serializer):
    query = serializers.CharField()
    name = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    resolved_by = serializers.ChoiceField(choices=["coordinates", "gazetteer", "geocoder"])


class LineStringSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["LineString"])
    coordinates = serializers.ListField(
        child=serializers.ListField(child=serializers.FloatField(), min_length=2, max_length=2),
        help_text="GeoJSON positions: [longitude, latitude].",
    )


class RouteSerializer(serializers.Serializer):
    distance_miles = serializers.FloatField()
    duration_hours = serializers.FloatField()
    geometry = LineStringSerializer(help_text="Simplified route line, ready for any GeoJSON map.")


class StationOnRouteSerializer(serializers.Serializer):
    opis_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    price_per_gallon = serializers.FloatField()
    mile_marker = serializers.FloatField(help_text="Miles from the start along the route.")
    miles_off_route = serializers.FloatField(
        help_text="Distance from the route to the station's city-level position."
    )
    latitude = serializers.FloatField(help_text="Point on the route nearest the station.")
    longitude = serializers.FloatField()


class FuelStopSerializer(StationOnRouteSerializer):
    stop = serializers.IntegerField(help_text="1-based order along the route.")
    fuel_on_arrival_gallons = serializers.FloatField()
    gallons = serializers.FloatField(help_text="Gallons to buy at this stop.")
    cost = serializers.FloatField(help_text="gallons x price_per_gallon, USD.")


class SummarySerializer(serializers.Serializer):
    total_fuel_cost = serializers.FloatField(
        help_text=(
            "USD cost of all fuel the trip burns at 10 mpg: "
            "fuel_purchased_cost + starting_fuel_cost_estimate."
        )
    )
    fuel_purchased_cost = serializers.FloatField(help_text="USD paid at the fuel stops.")
    starting_fuel_cost_estimate = serializers.FloatField(
        help_text="starting_fuel_gallons_used x reference_price_per_gallon."
    )
    trip_fuel_gallons = serializers.FloatField(
        help_text="Fuel burned: (route distance + detour_miles) / mpg."
    )
    detour_miles = serializers.FloatField(
        help_text=(
            "Miles driven off the route to reach the stops. 0 unless the search corridor "
            "had to be widened (see warnings)."
        )
    )
    gallons_purchased = serializers.FloatField()
    starting_fuel_gallons_used = serializers.FloatField()
    reference_price_per_gallon = serializers.FloatField()
    reference_price_basis = serializers.ChoiceField(
        choices=[
            "average_price_paid_at_stops",
            "cheapest_station_on_route",
            "dataset_average_price",
        ]
    )
    fuel_stops = serializers.IntegerField()


class AssumptionsSerializer(serializers.Serializer):
    vehicle_range_miles = serializers.FloatField()
    miles_per_gallon = serializers.FloatField()
    tank_capacity_gallons = serializers.FloatField()
    reserve_miles = serializers.FloatField()
    stop_penalty_usd = serializers.FloatField()
    notes = serializers.ListField(child=serializers.CharField())


class ExternalCallsSerializer(serializers.Serializer):
    routing = serializers.IntegerField()
    geocoding = serializers.IntegerField()


class MetaSerializer(serializers.Serializer):
    routing_provider = serializers.CharField()
    external_api_calls = ExternalCallsSerializer(
        help_text="Calls made to third-party map APIs for this response (0 when cached)."
    )
    candidate_stations = serializers.IntegerField()
    corridor_miles = serializers.FloatField()
    cheapest_possible_purchase_cost = serializers.FloatField(
        help_text="fuel_purchased_cost of the strictly cheapest plan (stop_penalty=0)."
    )
    upstream_ms = serializers.FloatField()
    elapsed_ms = serializers.FloatField()


class RoutePlanResponseSerializer(serializers.Serializer):
    start = LocationSerializer()
    finish = LocationSerializer()
    route = RouteSerializer()
    fuel_stops = FuelStopSerializer(many=True)
    optional_top_up = StationOnRouteSerializer(
        allow_null=True,
        help_text="Only when no stop is required: the cheapest station on the route.",
    )
    summary = SummarySerializer()
    assumptions = AssumptionsSerializer()
    warnings = serializers.ListField(child=serializers.CharField())
    map_url = serializers.URLField(help_text="Open in a browser for an interactive map.")
    meta = MetaSerializer()


class ErrorBodySerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()
    details = serializers.JSONField()


class ErrorSerializer(serializers.Serializer):
    error = ErrorBodySerializer()
