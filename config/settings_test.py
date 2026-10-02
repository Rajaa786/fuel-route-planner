"""Settings for the test suite.

Everything the tests depend on is pinned here, so a developer's local ``.env``
cannot change their outcome.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")

from config.settings import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
SECURE_SSL_REDIRECT = False

# Tests must never reach the network: point the providers at an unroutable host.
OSRM_BASE_URL = "http://osrm.invalid"
NOMINATIM_BASE_URL = "http://nominatim.invalid"

REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {"anon": "1000/min"}}  # noqa: F405
