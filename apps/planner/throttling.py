from django.core.cache import caches
from rest_framework.throttling import AnonRateThrottle


class ClientRateThrottle(AnonRateThrottle):
    """Per-client request limit, shared by the JSON and the map endpoints.

    Both endpoints can trigger a call to the free upstream routing service, so
    both count against the same budget. Counters live in their own cache so a
    flood of requests cannot evict cached routes (and vice versa).

    The client is identified by ``REMOTE_ADDR`` unless ``NUM_PROXIES`` says how
    many trusted proxies sit in front; ``X-Forwarded-For`` is never trusted blindly.
    """

    cache = caches["throttle"]
