"""Single error envelope for the whole API.

Every failure, whether raised by DRF, the planner or an upstream provider,
leaves the service as::

    {"error": {"code": "<stable_machine_code>", "message": "<human text>", "details": {...}}}
"""

import logging
from dataclasses import dataclass

from rest_framework import status
from rest_framework.exceptions import APIException, Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.planner.geocoding import LocationNotFoundError, LocationOutsideServiceAreaError
from apps.planner.services import (
    FuelDataNotLoadedError,
    NoFeasibleFuelPlanError,
    SameLocationError,
)
from providers.base import (
    NoRouteFoundError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Problem:
    status: int
    code: str
    message: str | None = None  # None: use str(exception)


# Ordered: subclasses must precede their base classes.
_PROBLEMS: tuple[tuple[type[Exception], Problem], ...] = (
    (LocationNotFoundError, Problem(status.HTTP_422_UNPROCESSABLE_ENTITY, "location_not_found")),
    (
        LocationOutsideServiceAreaError,
        Problem(status.HTTP_422_UNPROCESSABLE_ENTITY, "location_outside_service_area"),
    ),
    (
        SameLocationError,
        Problem(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "same_start_and_finish",
            "Start and finish resolve to the same location.",
        ),
    ),
    (NoRouteFoundError, Problem(status.HTTP_422_UNPROCESSABLE_ENTITY, "no_route_found")),
    (
        NoFeasibleFuelPlanError,
        Problem(status.HTTP_422_UNPROCESSABLE_ENTITY, "no_feasible_fuel_plan"),
    ),
    (
        FuelDataNotLoadedError,
        Problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "fuel_data_not_loaded",
            "Fuel station data has not been loaded. Run `python manage.py bootstrap_data`.",
        ),
    ),
    (
        ProviderRateLimitedError,
        Problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "upstream_rate_limited",
            "The upstream map service is rate limiting requests. Please retry shortly.",
        ),
    ),
    (
        ProviderTimeoutError,
        Problem(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "upstream_timeout",
            "The upstream map service did not respond in time.",
        ),
    ),
    (
        ProviderUnavailableError,
        Problem(
            status.HTTP_502_BAD_GATEWAY,
            "upstream_unavailable",
            "The upstream map service is unavailable.",
        ),
    ),
)


def problem_for(exc: Exception) -> tuple[Problem, str, dict] | None:
    """Map a planner/provider exception to ``(problem, message, details)``."""
    for exception_type, problem in _PROBLEMS:
        if isinstance(exc, exception_type):
            details = {}
            if isinstance(exc, NoFeasibleFuelPlanError):
                details = {
                    "gap_start_miles": exc.gap_start_miles,
                    "gap_end_miles": exc.gap_end_miles,
                    "reachable_miles": exc.reachable_miles,
                    "corridor_miles": exc.corridor_miles,
                }
            return problem, problem.message or str(exc), details
    return None


def envelope(code: str, message: str, details: dict | list | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def exception_handler(exc: Exception, context: dict) -> Response | None:
    mapped = problem_for(exc)
    if mapped is not None:
        problem, message, details = mapped
        if problem.status >= 500:
            logger.warning("planner_error code=%s detail=%s", problem.code, exc)
        headers = {}
        if isinstance(exc, ProviderRateLimitedError) and exc.retry_after_seconds:
            headers["Retry-After"] = str(exc.retry_after_seconds)
        return Response(envelope(problem.code, message, details), problem.status, headers=headers)

    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # genuine bug: let Django turn it into a 500 and log the traceback

    if isinstance(exc, ValidationError):
        response.data = envelope("invalid_request", "Invalid request parameters.", response.data)
    elif isinstance(exc, Throttled):
        response.data = envelope(
            "rate_limited", "Too many requests.", {"retry_after_seconds": exc.wait}
        )
    elif isinstance(exc, APIException):
        response.data = envelope(exc.default_code, str(exc.detail))
    else:  # Http404 / PermissionDenied raised as Django exceptions
        response.data = envelope("error", str(response.data.get("detail", "Request failed.")))
    return response
