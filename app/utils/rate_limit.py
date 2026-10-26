"""
app/utils/rate_limit.py
-------------------------
Sliding-window rate limits, kept in memory per application.

Used for the things a bot or a careless script would hammer: failed
sign-ins, sign-ups and password resets, and the routes that run the AI
models or spend Google Places quota. The app runs one web process (see
render.yaml), so an in-memory counter is shared by every request it
serves; a restart simply starts the windows again.

limit(bucket, key) returns (allowed, retry_after_seconds). Disabled with
RATE_LIMIT_ENABLED=false (the test configuration does this; tests that
check the limits switch it back on).
"""

import threading
import time
from collections import deque

from flask import current_app, has_request_context, request

LIMITS = {
    "login_failed": (5, 15 * 60),
    "login_ip": (30, 15 * 60),
    "register": (10, 60 * 60),
    "lgu_request": (5, 24 * 60 * 60),
    "forgot_password": (5, 60 * 60),
    "send_code": (10, 60 * 60),
    "analysis": (30, 60 * 60),
    "api_forecast": (120, 10 * 60),
    "places_nearby": (30, 10 * 60),
    "places_refresh": (60, 60 * 60),
    "transcript": (30, 10 * 60),
    "captcha_verify": (30, 10 * 60),
}

_lock = threading.Lock()


def _store():
    return current_app.extensions.setdefault("dss.rate_limits", {})


def client_ip():
    if not has_request_context():
        return "-"
    return (request.remote_addr or "-")[:64]


def _enabled():
    return current_app.config.get("RATE_LIMIT_ENABLED", True)


def peek(bucket, key):
    """(allowed, retry_after) without recording a hit."""
    if not _enabled():
        return True, 0
    maximum, window = LIMITS[bucket]
    now = time.monotonic()
    with _lock:
        hits = _store().get((bucket, key))
        if not hits:
            return True, 0
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) < maximum:
            return True, 0
        return False, int(window - (now - hits[0])) + 1


def hit(bucket, key):
    """Record one hit; (allowed, retry_after). A refused hit is not
    recorded, so waiting out the window always works."""
    if not _enabled():
        return True, 0
    maximum, window = LIMITS[bucket]
    now = time.monotonic()
    with _lock:
        hits = _store().setdefault((bucket, key), deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= maximum:
            return False, int(window - (now - hits[0])) + 1
        hits.append(now)
        return True, 0


def reset(bucket, key):
    with _lock:
        _store().pop((bucket, key), None)


def wait_text(seconds):
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
