import time
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.csp import CSP
from django.views.decorators.csp import csp_override
from django.views.decorators.http import require_GET
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.planner.errors import envelope, problem_for
from apps.planner.presenters import present_plan
from apps.planner.serializers import (
    ErrorSerializer,
    RoutePlanQuerySerializer,
    RoutePlanResponseSerializer,
)
from apps.planner.wiring import get_route_planner
from apps.stations.index import get_station_index

# The map page loads Leaflet from a CDN and tiles from OpenStreetMap; everything
# else stays blocked. Inline <script>/<style> are allowed only with the per-request nonce.
MAP_PAGE_CSP = {
    "default-src": [CSP.NONE],
    "script-src": [CSP.NONCE],
    "style-src": [CSP.NONCE, "https://unpkg.com"],
    "img-src": [CSP.SELF, "data:", "https://*.tile.openstreetmap.org"],
    "base-uri": [CSP.NONE],
    "form-action": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
}


class RoutePlanView(APIView):
    """Plan a route between two US locations and the cheapest places to refuel."""

    @extend_schema(
        operation_id="plan_route",
        summary="Route, optimal fuel stops and total fuel cost",
        parameters=[RoutePlanQuerySerializer],
        responses={
            200: RoutePlanResponseSerializer,
            400: ErrorSerializer,
            422: ErrorSerializer,
            429: ErrorSerializer,
            502: ErrorSerializer,
            503: ErrorSerializer,
            504: ErrorSerializer,
        },
        examples=[
            OpenApiExample(
                "Unknown location",
                value={
                    "error": {
                        "code": "location_not_found",
                        "message": "Could not find a US location matching 'Atlantis'.",
                        "details": {},
                    }
                },
                response_only=True,
                status_codes=["422"],
            )
        ],
    )
    def get(self, request: Request) -> Response:
        started = time.perf_counter()
        query = RoutePlanQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        plan = get_route_planner().plan(**query.validated_data)

        map_url = request.build_absolute_uri(
            f"{reverse('planner:route-map')}?{urlencode(query.validated_data)}"
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Response(present_plan(plan, map_url=map_url, elapsed_ms=elapsed_ms))


class RouteMapView(APIView):
    """Interactive map of a plan (HTML).

    Takes the same query string as the JSON endpoint. The route comes from the
    cache populated by that call, so opening the map costs no extra upstream
    request. It is a DRF view so that it shares the JSON endpoint's throttle:
    a cold map request can trigger a routing call too.
    """

    # Only used for errors raised before the handler runs (throttling); the page
    # itself is returned as a ready-made HttpResponse.
    renderer_classes = [JSONRenderer]

    @extend_schema(exclude=True)
    def get(self, request: Request) -> HttpResponse:
        query = RoutePlanQuerySerializer(data=request.query_params)
        if not query.is_valid():
            return self._error(request, status.HTTP_400_BAD_REQUEST, "Invalid request parameters.")

        try:
            plan = get_route_planner().plan(**query.validated_data)
        except Exception as exc:
            mapped = problem_for(exc)
            if mapped is None:
                raise  # a genuine bug: let Django log it and answer 500
            problem, message, _ = mapped
            return self._error(request, problem.status, message)

        payload = present_plan(plan, map_url=request.build_absolute_uri(), elapsed_ms=0.0)
        # Render against the underlying HttpRequest: it carries the CSP nonce.
        return render(request._request, "planner/route_map.html", {"plan": payload})

    @staticmethod
    def _error(request: Request, status_code: int, message: str) -> HttpResponse:
        context = {"plan": None, "error": message}
        return render(request._request, "planner/route_map.html", context, status=status_code)


route_map = csp_override(MAP_PAGE_CSP)(RouteMapView.as_view())


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    """Liveness/readiness probe: ready only once the fuel data has been loaded."""
    stations = len(get_station_index())
    if not stations:
        return JsonResponse(
            envelope(
                "fuel_data_not_loaded",
                "Fuel station data has not been loaded. Run `python manage.py bootstrap_data`.",
            ),
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return JsonResponse({"status": "ok", "fuel_stations": stations})


def not_found(request: HttpRequest, exception: Exception) -> JsonResponse:
    """JSON 404 so that unknown URLs get the same error envelope as everything else."""
    return JsonResponse(
        envelope("not_found", "The requested resource was not found."),
        status=status.HTTP_404_NOT_FOUND,
    )


def server_error(request: HttpRequest) -> JsonResponse:
    """JSON 500. Django has already logged the traceback by the time this runs."""
    return JsonResponse(
        envelope("internal_error", "An unexpected error occurred."),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
