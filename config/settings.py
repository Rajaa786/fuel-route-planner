"""Django settings, driven by environment variables (12-factor).

Copy ``.env.example`` to ``.env`` for local development. Production defaults are
the safe ones: ``DEBUG`` is off and ``DJANGO_SECRET_KEY`` is mandatory.
"""

from pathlib import Path

import environ
from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# --- Core -----------------------------------------------------------------------------------

DEBUG = env.bool("DJANGO_DEBUG", default=False)
# No default outside DEBUG: a missing key must fail the boot, not silently weaken security.
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="insecure-key-for-local-development-only" if DEBUG else environ.Env.NOTSET,
)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# API-only service: no users, sessions or admin, so none of those apps are installed.
INSTALLED_APPS = [
    "rest_framework",
    "drf_spectacular",
    "apps.stations",
    "apps.planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",  # route geometry compresses ~4x
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
            ],
        },
    },
]

# --- Data stores ----------------------------------------------------------------------------

# The dataset is small and read-only at request time (stations are served from an
# in-memory index), so SQLite is enough and keeps setup to zero.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env("SQLITE_PATH", default=str(BASE_DIR / "db.sqlite3")),
    }
}

# Per-process cache. The service is meant to run as ONE worker process with threads
# (see README), which makes this cache, the station index and the throttle exact.
# Scaling out to several processes means pointing this at Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "routes-and-geocodes",
        "OPTIONS": {"MAX_ENTRIES": 512},
    },
    # Kept apart so throttle counters and cached routes cannot evict each other.
    "throttle": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "throttle",
        "OPTIONS": {"MAX_ENTRIES": 10_000},
    },
}

# --- Security -------------------------------------------------------------------------------

# Deny everything by default: JSON responses need no sub-resources. The two HTML
# pages (route map, API docs) opt in to exactly what they load via csp_override().
SECURE_CSP = {
    "default-src": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
}
# No cookies, sessions or state-changing endpoints exist, so there is nothing for CSRF
# protection to protect.
SILENCED_SYSTEM_CHECKS = ["security.W003"]
SECURE_CONTENT_TYPE_NOSNIFF = True
# OpenStreetMap's tile servers reject requests that carry no Referer.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
if env.bool("DJANGO_BEHIND_TLS_PROXY", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# --- Internationalisation -------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

# --- REST framework -------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    # Protects the free upstream routing service from being hammered through us.
    "DEFAULT_THROTTLE_CLASSES": ["apps.planner.throttling.ClientRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": env("API_THROTTLE_RATE", default="120/min")},
    # Trusted reverse proxies in front of the app. 0 = identify clients by REMOTE_ADDR and
    # ignore X-Forwarded-For, which any client can forge. Set to 1 behind one proxy.
    "NUM_PROXIES": env.int("NUM_PROXIES", default=0),
    "EXCEPTION_HANDLER": "apps.planner.errors.exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel Route Planner API",
    "DESCRIPTION": (
        "Plans a driving route between two US locations and the cheapest places to refuel "
        "along it, for a vehicle with a 500-mile range that achieves 10 miles per gallon."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Application ----------------------------------------------------------------------------

FUEL_PRICES_PATH = Path(
    env("FUEL_PRICES_PATH", default=str(BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv"))
)
GAZETTEER_PATH = Path(env("GAZETTEER_PATH", default=str(BASE_DIR / "data" / "us_places.csv.gz")))

# Vehicle model (from the assignment brief).
VEHICLE_RANGE_MILES = env.float("VEHICLE_RANGE_MILES", default=500.0)
VEHICLE_MILES_PER_GALLON = env.float("VEHICLE_MILES_PER_GALLON", default=10.0)

# Planner tuning. Rationale for each lives next to its use in apps/planner/services.py.
# Range never planned into, because station positions are only city-accurate.
FUEL_RESERVE_MILES = env.float("FUEL_RESERVE_MILES", default=25.0)
# Dollar value of avoiding one stop. 0 = strictly cheapest fuel, however many stops.
FUEL_STOP_PENALTY_USD = env.float("FUEL_STOP_PENALTY_USD", default=2.0)
# How far from the route a station may be. Wider tiers are tried only if needed.
FUEL_CORRIDOR_TIERS_MILES = tuple(
    env.list("FUEL_CORRIDOR_TIERS_MILES", cast=float, default=[5.0, 10.0, 25.0])
)
ROUTE_SAMPLE_SPACING_MILES = env.float("ROUTE_SAMPLE_SPACING_MILES", default=0.25)
ROUTE_GEOMETRY_TOLERANCE_MILES = env.float("ROUTE_GEOMETRY_TOLERANCE_MILES", default=0.05)

# External services. Both are free and keyless; base URLs are configurable so a
# self-hosted OSRM/Nominatim is a config change.
OSRM_BASE_URL = env("OSRM_BASE_URL", default="https://router.project-osrm.org")
NOMINATIM_BASE_URL = env("NOMINATIM_BASE_URL", default="https://nominatim.openstreetmap.org")
# OSM-hosted services ask for a User-Agent that identifies the application.
HTTP_USER_AGENT = env(
    "HTTP_USER_AGENT",
    default="fuel-route-planner/1.0 (+https://github.com/Rajaa786/fuel-route-planner)",
)
# Kept well below gunicorn's 30 s worker timeout so a slow upstream yields a clean 504.
HTTP_CONNECT_TIMEOUT_SECONDS = env.float("HTTP_CONNECT_TIMEOUT_SECONDS", default=3.0)
HTTP_READ_TIMEOUT_SECONDS = env.float("HTTP_READ_TIMEOUT_SECONDS", default=12.0)

ROUTE_CACHE_TTL_SECONDS = env.int("ROUTE_CACHE_TTL_SECONDS", default=60 * 60 * 24)
GEOCODE_CACHE_TTL_SECONDS = env.int("GEOCODE_CACHE_TTL_SECONDS", default=60 * 60 * 24)
GEOCODE_MISS_CACHE_TTL_SECONDS = env.int("GEOCODE_MISS_CACHE_TTL_SECONDS", default=60 * 5)

# --- Logging --------------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}
