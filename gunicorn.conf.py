"""Gunicorn configuration (picked up automatically by ``gunicorn config.wsgi``).

ONE worker process with a thread pool is deliberate, not a default left alone:

* the route/geocode cache (LocMemCache), the in-memory station index and the
  DRF throttle are per-process; with a single process they are exact;
* a request is ~10 ms of CPU plus one network wait, so threads give all the
  concurrency that is useful.

To scale past one process, point Django's cache at Redis first (see README).
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 1
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = 30  # upstream timeouts (3 s connect + 12 s read) fire long before this
accesslog = "-"
errorlog = "-"
