"""
app/services/response_cache.py
---------------------------------
Caching for the Trend Reports endpoints, on top of Flask-Caching.

WHY A WRAPPER RATHER THAN @cache.cached() ON THE ROUTE

Two reasons, and the first one is a correctness problem rather than a
preference.

1. A PLAIN TIMEOUT CAN SERVE NUMBERS THE DATABASE DISAGREES WITH.
   @cache.cached(timeout=300) keys on the URL and expires on a clock.
   Import a batch of market_data, or run a Places refresh, and for up
   to five minutes the page keeps showing the previous answer with
   nothing to indicate it. For a decision-support tool whose whole
   claim is that the figures are real, that is worse than being slow.

   So the cache key here includes a FINGERPRINT of market_data --
   row count, newest id, newest date, total competitor count (see
   trend_analytics_service._data_fingerprint). Any insert, delete or
   edit moves at least one of those, which changes the key, which
   misses. Invalidation is not a timer; the timer is only a backstop
   for things the fingerprint cannot see, such as a retrained model.

2. IT HAS TO WORK WITH THE PACKAGE MISSING. Flask-Caching is imported
   defensively in app/extensions.py (see the note there). When it is
   absent these helpers become pass-throughs: slower, still correct,
   still boots. Routes do not have to care which case they are in.

WHAT IS ACTUALLY WORTH CACHING HERE
The expensive part of a trend report is the city-wide AI sweep, and
that already has its own two-layer cache (in-process, then on disk --
see trend_analytics_service). This layer sits in front of the whole
RESPONSE, so a repeat view also skips the JSON assembly, the history
back-projection and the serialisation on top of the sweep. That is
what makes a revisit feel instant rather than merely quick.
"""

import functools
import hashlib

from flask import current_app, request

from app.extensions import CACHING_AVAILABLE, cache

# The backstop mentioned above, not the primary invalidator.
DEFAULT_TIMEOUT = 900


def _fingerprint_token():
    """A short digest of the market_data fingerprint, safe to put in a
    cache key. Returns "" when it cannot be read (no app context, no
    database yet) -- which simply means this request is not cached,
    never that it is served something stale."""
    try:
        from app.services.trend_analytics_service import _data_fingerprint

        # [1:] drops the engine id: it is a memory address, useful for
        # keeping test databases apart in-process but meaningless in a
        # key that may outlive the process on disk.
        parts = "|".join(str(p) for p in _data_fingerprint()[1:])
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:12]
    except Exception:  # pragma: no cover - defensive; caching is optional
        return ""


def cached_on_data(prefix, timeout=DEFAULT_TIMEOUT, query_args=()):
    """Cache a JSON endpoint on (prefix, chosen query args, data
    fingerprint).

    `query_args` names the request arguments that change the answer --
    for the trend endpoints that is the industry filter and the chosen
    month. Anything not named is deliberately ignored, so a stray
    tracking parameter cannot fragment the cache.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if not CACHING_AVAILABLE or cache is None:
                return view(*args, **kwargs)

            token = _fingerprint_token()
            if not token:
                return view(*args, **kwargs)

            key_parts = [prefix, token]
            for name in query_args:
                key_parts.append(f"{name}={request.args.get(name) or ''}")
            key = "trend:" + hashlib.sha1("|".join(key_parts).encode("utf-8")).hexdigest()

            try:
                hit = cache.get(key)
            except Exception:  # pragma: no cover - a broken cache must not 500
                return view(*args, **kwargs)

            if hit is not None:
                return hit

            value = view(*args, **kwargs)
            try:
                cache.set(key, value, timeout=timeout)
            except Exception:  # pragma: no cover
                current_app.logger.warning("response cache write failed", exc_info=True)
            return value

        return wrapper

    return decorator


def clear_response_cache():
    """Drop everything this module cached. Called alongside
    clear_trend_caches() after a bulk Places refresh -- the fingerprint
    would catch that anyway, but an explicit "forget everything" should
    not leave a layer behind holding the old answer."""
    if not CACHING_AVAILABLE or cache is None:
        return
    try:
        cache.clear()
    except Exception:  # pragma: no cover
        pass
