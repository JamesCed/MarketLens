"""
app/services/geocoding_service.py
------------------------------------
Resolves a Tarlac City barangay NAME into its REAL-WORLD lat/lng, so the
Saturation Map draws each barangay's saturation zone on the spot where
that barangay actually is.

WHERE COORDINATES COME FROM (first match wins)
  1. app/static/data/barangay_coords.json -- REAL, per-barangay
     coordinates for all 76 official barangays of Tarlac City, sourced
     from PhilAtlas (philatlas.com -- see that file's own "_note"/
     "source" fields), a Philippine geographic reference site that
     compiles official PSA/NAMRIA location data one page per barangay.
     This answers almost every request instantly, with no network call,
     no Google API quota, and no dependency on which Google API happens
     to be enabled on the deployment's key.

     An EARLIER version of this file spread 76 points evenly across
     Tarlac City's bounding box as a synthetic placeholder -- never real
     positions. That is what made "Laoang" render next to "Sapang
     Maragul" and similar mixups: two made-up points that merely
     happened to land near each other, not a geocoding failure at all.
     Live Google geocoding was added on top of that placeholder file in
     several follow-up rounds, but a name that had ALREADY been cached
     (right or wrong) was never looked up again, so a bad early answer
     could survive every later fix. Replacing the placeholder file with
     real, independently-sourced coordinates for all 76 names removes
     that entire failure mode at its root, instead of patching the
     symptom again.

  2. Google Geocoding API / Places API (New) Text Search -- kept ONLY as
     a fallback for a location name that ISN'T one of the 76 above (a
     typo, or a new barangay a future LGU dataset upload introduces).
     Both are called with the `requests` package, straight from the
     backend, matching the paper's own description of how this system
     talks to Google. Results are bounds-checked against Tarlac City and
     cached in instance/barangay_coords_cache.json so the same odd name
     is never looked up twice.

EVERY RESULT IS BOUNDS-CHECKED (_is_in_tarlac_city_bounds). A geocode
that lands outside Tarlac City's rectangle is DISCARDED rather than
plotted -- a wrong coordinate is worse than a missing one, because a
wrong one silently misinforms the user. A name nobody can resolve stays
unresolved and is reported as such, rather than guessed at.

CACHING
Fallback lookups are written to instance/barangay_coords_cache.json (the
Flask instance folder -- writable, and not part of the source tree, so a
redeploy never overwrites it). This file only ever holds names that
AREN'T one of the shipped 76 -- the common case never touches it, let
alone the network.
"""

import json
import os
import threading
from datetime import datetime

import requests

from app.services.places_service import TARLAC_CITY_BOUNDS

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

CACHE_FILENAME = "barangay_coords_cache.json"
STATIC_SEED_RELPATH = os.path.join("static", "data", "barangay_coords.json")

# One process-wide lock so two simultaneous requests can't interleave a
# read-modify-write of the cache file and lose each other's results.
_CACHE_LOCK = threading.Lock()

_HTTP_TIMEOUT = 8


# ---------------------------------------------------------------------
# Bounds check -- the accuracy guard rail
# ---------------------------------------------------------------------

def _is_in_tarlac_city_bounds(lat, lng):
    """True only if (lat, lng) sits inside the same Tarlac City rectangle
    places_service.py restricts competitor searches to. Anything else is
    a bad geocode (wrong "San Isidro", a province centroid, a country
    centroid) and must never reach the map."""
    low = TARLAC_CITY_BOUNDS["low"]
    high = TARLAC_CITY_BOUNDS["high"]
    return low["latitude"] <= lat <= high["latitude"] and low["longitude"] <= lng <= high["longitude"]


def _query_for(barangay_name):
    return f"Barangay {barangay_name}, Tarlac City, Tarlac, Philippines"


# ---------------------------------------------------------------------
# The two Google lookups -- fallback path only (see module docstring)
# ---------------------------------------------------------------------

def _geocode_via_geocoding_api(barangay_name, api_key):
    """Google Geocoding API. Returns ({'lat','lng'} or None, status_text).
    Typical status values: OK, ZERO_RESULTS, REQUEST_DENIED ("Geocoding
    API" not enabled on this key, or the key is referrer-restricted and
    so unusable from a server), OVER_QUERY_LIMIT, INVALID_REQUEST.
    """
    low = TARLAC_CITY_BOUNDS["low"]
    high = TARLAC_CITY_BOUNDS["high"]
    params = {
        "address": _query_for(barangay_name),
        # country + a bounds hint -- NOT administrative_area, which is
        # brittle (Google matches that against its own admin-area naming
        # and a mismatch turns every lookup into ZERO_RESULTS).
        "components": "country:PH",
        "bounds": f"{low['latitude']},{low['longitude']}|{high['latitude']},{high['longitude']}",
        "key": api_key,
    }
    response = requests.get(GEOCODE_URL, params=params, timeout=_HTTP_TIMEOUT)
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"

    payload = response.json()
    status = payload.get("status", "UNKNOWN")
    if status != "OK":
        detail = payload.get("error_message")
        return None, f"{status}: {detail}" if detail else status

    for result in payload.get("results", []):
        location = ((result.get("geometry") or {}).get("location")) or {}
        lat, lng = location.get("lat"), location.get("lng")
        if lat is None or lng is None:
            continue
        if _is_in_tarlac_city_bounds(float(lat), float(lng)):
            return {"lat": round(float(lat), 6), "lng": round(float(lng), 6)}, "OK"
    # Google answered, but every candidate sat outside Tarlac City --
    # the classic wrong-"San Isidro" case. Refuse rather than plot it.
    return None, "OUT_OF_BOUNDS"


def _geocode_via_places_api(barangay_name, api_key):
    """Google Places API (New) Text Search, asked only for a location.
    Returns ({'lat','lng'} or None, status_text). The fallback that
    works with the same key places_service.py already needs.

    Text Search returns PLACES, not administrative areas, so a query for
    a barangay can come back with some business that merely sits nearby.
    Results whose own address doesn't mention the barangay are therefore
    rejected: a plausible-looking wrong centre is exactly the failure
    mode this file exists to prevent.
    """
    body = {
        "textQuery": _query_for(barangay_name),
        "locationRestriction": {"rectangle": TARLAC_CITY_BOUNDS},
        "regionCode": "PH",
        "languageCode": "en",
        "pageSize": 5,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.location,places.formattedAddress,places.displayName",
    }
    response = requests.post(SEARCH_TEXT_URL, json=body, headers=headers, timeout=_HTTP_TIMEOUT)
    if response.status_code != 200:
        try:
            detail = (response.json().get("error") or {}).get("message")
        except (ValueError, AttributeError):
            detail = None
        return None, f"HTTP {response.status_code}" + (f": {detail}" if detail else "")

    payload = response.json()
    places = payload.get("places", [])
    if not places:
        return None, "ZERO_RESULTS"

    needle = barangay_name.strip().lower()
    out_of_bounds = False
    name_mismatch = False
    for place in places:
        location = place.get("location") or {}
        lat, lng = location.get("latitude"), location.get("longitude")
        if lat is None or lng is None:
            continue
        if not _is_in_tarlac_city_bounds(float(lat), float(lng)):
            out_of_bounds = True
            continue
        haystack = " ".join(
            [
                place.get("formattedAddress") or "",
                (place.get("displayName") or {}).get("text") or "",
            ]
        ).lower()
        if needle not in haystack:
            name_mismatch = True
            continue
        return {"lat": round(float(lat), 6), "lng": round(float(lng), 6)}, "OK"

    if name_mismatch:
        return None, "NAME_MISMATCH"
    if out_of_bounds:
        return None, "OUT_OF_BOUNDS"
    return None, "NO_USABLE_RESULT"


def geocode_barangay(barangay_name, api_key, geocoding_key=None):
    """Resolve ONE barangay name to a real coordinate via Google, trying
    the Geocoding API then the Places API. Returns (coords_or_None,
    source). Used only for a name that isn't one of the shipped 76 (see
    module docstring) -- most requests never call this at all.

    Never raises -- a flaky network or a disabled API must not take the
    Saturation Map down with it.
    """
    coords, source, _status = geocode_barangay_verbose(barangay_name, api_key, geocoding_key)
    return coords, source


def geocode_barangay_verbose(barangay_name, api_key, geocoding_key=None):
    """As geocode_barangay(), but also returns a human-readable status
    describing what each attempt did."""
    if not barangay_name:
        return None, None, "no barangay name given"
    if not (api_key or geocoding_key):
        return None, None, "no API key configured"

    # Geocoding is a second billable Google API, so it honours the same
    # switch as the Places lookups: a deployment with PLACES_LIVE_FETCH
    # =false calls neither. Nothing is lost by this -- every one of the 76
    # barangays already has a real coordinate on file (see
    # precompute_barangay_coords.py and load_seed_coords()), and this
    # function only ever ran for a name that was missing from that cache.
    from app.services.places_service import live_fetch_enabled

    if not live_fetch_enabled():
        return None, None, "live Google fetching is disabled for this deployment"

    attempts = (
        ("google_geocoding", _geocode_via_geocoding_api, geocoding_key or api_key, "Geocoding API"),
        ("google_places", _geocode_via_places_api, api_key, "Places API (New)"),
    )
    notes = []
    for source, fn, key, label in attempts:
        if not key:
            notes.append(f"{label}: no key")
            continue
        try:
            coords, status = fn(barangay_name, key)
        except requests.RequestException as exc:
            notes.append(f"{label}: network error ({type(exc).__name__})")
            continue
        except (ValueError, KeyError, TypeError) as exc:
            notes.append(f"{label}: bad response ({type(exc).__name__})")
            continue
        if coords:
            return coords, source, f"{label}: OK"
        notes.append(f"{label}: {status}")
    return None, None, " | ".join(notes)


# ---------------------------------------------------------------------
# Cache (instance/barangay_coords_cache.json) -- fallback names only
# ---------------------------------------------------------------------

def _cache_path(app):
    os.makedirs(app.instance_path, exist_ok=True)
    return os.path.join(app.instance_path, CACHE_FILENAME)


def load_cache(app):
    """{'Name': {'lat','lng','source','resolved_at'}} for any location
    that had to go through live geocoding because it wasn't one of the
    76 shipped barangays. Empty dict if nothing has needed that yet."""
    path = _cache_path(app)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("barangays", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(app, barangays):
    path = _cache_path(app)
    payload = {
        "_note": (
            "Coordinates resolved live from Google for location names that are NOT one "
            "of the 76 barangays shipped in app/static/data/barangay_coords.json. Safe "
            "to delete -- it will simply be rebuilt the next time an unrecognised name "
            "is looked up."
        ),
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "barangays": barangays,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)  # atomic -- never leaves a half-written cache


def load_seed_coords(app):
    """The shipped, REAL, PhilAtlas-sourced coordinates for all 76
    Tarlac City barangays (app/static/data/barangay_coords.json). Despite
    the name (kept for continuity with older code/tests), these are not
    placeholders -- they are the app's primary coordinate source."""
    path = os.path.join(app.root_path, STATIC_SEED_RELPATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("barangays", {})
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------
# Public entry point used by the /api/barangay-coords endpoint
# ---------------------------------------------------------------------

def resolve_missing(app, names, api_key, limit=20, geocoding_key=None):
    """Geocode up to `limit` of `names` that are neither in the shipped
    76 nor already cached, persist whatever resolved, and return (cache,
    newly_resolved_count, still_pending_count).

    In normal operation `names` is exactly the 76 shipped barangays, so
    this has nothing to do and returns immediately -- it only does real
    work for a location string outside that list.
    """
    seed = load_seed_coords(app)
    with _CACHE_LOCK:
        cache = load_cache(app)

        missing = [n for n in names if n not in seed and n not in cache]
        if not missing or not (api_key or geocoding_key):
            pending = len([n for n in names if n not in seed and n not in cache])
            return cache, 0, pending

        resolved_now = 0
        for name in missing[: max(0, int(limit))]:
            coords, source, _status = geocode_barangay_verbose(name, api_key, geocoding_key=geocoding_key)
            if not coords:
                continue
            cache[name] = {
                "lat": coords["lat"],
                "lng": coords["lng"],
                "source": source,
                "resolved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }
            resolved_now += 1

        if resolved_now:
            try:
                _save_cache(app, cache)
            except OSError:
                pass  # in-memory result is still usable for this request

        still_pending = len([n for n in names if n not in seed and n not in cache])
        return cache, resolved_now, still_pending


def merged_coords(app, names, api_key=None, limit=0, geocoding_key=None):
    """What the map actually consumes: {name: {lat, lng, source}} for
    every requested barangay. For the 76 shipped barangays this is
    always the real, static PhilAtlas-sourced coordinate (source
    "real") -- no network call, no "still resolving" state. A name
    outside that list falls back to live Google geocoding, cached from
    then on; if that fails too the name is simply omitted (a missing
    zone rather than a wrong one).

    Pass limit>0 to also attempt geocoding that many unresolved
    fallback-only names during this call.
    """
    seed = load_seed_coords(app)
    if limit and (api_key or geocoding_key):
        cache, resolved_now, pending = resolve_missing(
            app, names, api_key, limit=limit, geocoding_key=geocoding_key
        )
    else:
        cache = load_cache(app)
        resolved_now = 0
        pending = len([n for n in names if n not in seed and n not in cache])

    out = {}
    for name in names:
        if name in seed:
            point = seed[name]
            out[name] = {"lat": point["lat"], "lng": point["lng"], "source": "real"}
            continue
        entry = cache.get(name)
        if entry:
            out[name] = {"lat": entry["lat"], "lng": entry["lng"], "source": entry.get("source", "google")}
    return out, resolved_now, pending
