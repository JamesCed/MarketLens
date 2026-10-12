"""
app/utils/http_perf.py
------------------------
Two transport-level speed-ups, installed once from create_app():

1. LONG-LIVED, SELF-INVALIDATING STATIC FILES
   Every url_for('static', ...) gets ?v=<content hash>. A URL that
   carries the hash is served with a one-year, immutable Cache-Control,
   so after the first visit the browser does not even ask for style.css,
   main.js, tour.js and the rest again -- previously it re-validated
   each of them (one round trip and one gunicorn thread each) on EVERY
   page change. Changing a file changes its hash, so a deploy is picked
   up immediately; there is no stale-cache window. A static URL WITHOUT
   the hash keeps Flask's default (revalidate every time), so nothing
   referenced by a hand-written path can ever go stale.

2. GZIP
   HTML, CSS, JavaScript, JSON and SVG are compressed when the browser
   accepts it -- typically to a fifth of their size (the Planning page
   is ~140 KB of HTML, the barangay boundary data ~90 KB). Static files
   are compressed once and kept, so a hit costs nothing. A response that
   is already encoded (e.g. compressed by a proxy) is left alone.
"""

import gzip
import hashlib
import os
import threading

from flask import request

ONE_YEAR = 31536000
_COMPRESSIBLE = {
    "text/html", "text/css", "text/plain", "text/csv", "text/javascript",
    "application/javascript", "application/json", "image/svg+xml",
}
_MIN_BYTES = 1024
_GZIP_LEVEL = 6
_GZ_SUFFIX = "-gz"


def _static_hashes(app):
    cache = {}
    lock = threading.Lock()

    def version_for(filename):
        if not filename:
            return None
        path = os.path.join(app.static_folder, filename)
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = (filename, stat.st_mtime_ns, stat.st_size)
        hit = cache.get(filename)
        if hit and hit[0] == key:
            return hit[1]
        with lock:
            digest = hashlib.md5()  # noqa: S324 -- a cache-busting token, not security
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    digest.update(chunk)
            token = digest.hexdigest()[:10]
            cache[filename] = (key, token)
            return token

    return version_for


def install_static_versioning(app):
    version_for = _static_hashes(app)

    @app.url_defaults
    def _add_static_version(endpoint, values):
        if endpoint == "static" and "v" not in values:
            token = version_for(values.get("filename"))
            if token:
                values["v"] = token

    default_max_age = app.get_send_file_max_age

    def _max_age(filename):
        # Only a URL carrying the content hash is safe to cache for a
        # year; anything else falls back to Flask's own behaviour.
        if request and request.args.get("v"):
            return ONE_YEAR
        return default_max_age(filename)

    app.get_send_file_max_age = _max_age


_GZ_STATIC_CACHE = {}
_GZ_STATIC_LOCK = threading.Lock()
_GZ_STATIC_MAX_ENTRIES = 64


def install_compression(app):
    @app.before_request
    def _strip_gzip_etag_suffix():
        # The gzip variant of a static file carries ETag "<etag>-gz".
        # Strip the suffix from a revalidation so Flask's own conditional
        # check still matches and answers 304.
        header = request.environ.get("HTTP_IF_NONE_MATCH")
        if header and _GZ_SUFFIX in header:
            request.environ["HTTP_IF_NONE_MATCH"] = header.replace(_GZ_SUFFIX + '"', '"')
            request.environ["dss.gzip_etag"] = True

    @app.after_request
    def _gzip(response):
        try:
            if request.endpoint == "static" and request.args.get("v") and response.status_code in (200, 304):
                response.cache_control.immutable = True
            if response.status_code == 304 and request.environ.get("dss.gzip_etag"):
                # A 304 must carry the same validators as the 200 it
                # confirms: the gzip variant's ETag, and Vary.
                etag, _weak = response.get_etag()
                if etag and not etag.endswith(_GZ_SUFFIX):
                    response.set_etag(etag + _GZ_SUFFIX)
                response.vary.add("Accept-Encoding")
            if request.method == "HEAD":
                return response
            return _maybe_compress(response)
        except Exception:  # noqa: BLE001 -- compression must never break a page
            return response


def _maybe_compress(response):
    if response.status_code != 200 or response.is_streamed and not response.direct_passthrough:
        return response
    # quality(), not a substring test: "gzip;q=0" means "never gzip".
    if request.accept_encodings.quality("gzip") <= 0:
        return response
    if response.headers.get("Content-Encoding") or request.headers.get("Range"):
        return response
    if (response.mimetype or "") not in _COMPRESSIBLE:
        return response

    is_static = request.endpoint == "static"
    etag, _weak = response.get_etag()
    cache_key = (request.path, etag) if is_static and etag else None

    body = _GZ_STATIC_CACHE.get(cache_key) if cache_key else None
    if body is None:
        response.direct_passthrough = False
        raw = response.get_data()
        if len(raw) < _MIN_BYTES:
            return response
        body = gzip.compress(raw, compresslevel=_GZIP_LEVEL)
        if len(body) >= len(raw):
            return response
        if cache_key:
            with _GZ_STATIC_LOCK:
                if len(_GZ_STATIC_CACHE) >= _GZ_STATIC_MAX_ENTRIES:
                    _GZ_STATIC_CACHE.clear()
                _GZ_STATIC_CACHE[cache_key] = body
    else:
        response.direct_passthrough = False

    response.set_data(body)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(body))
    response.vary.add("Accept-Encoding")
    if etag:
        response.set_etag(etag + _GZ_SUFFIX)
    return response
