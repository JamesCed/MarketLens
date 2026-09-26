"""
app/services/choropleth_service.py
--------------------------------------
Turns the Saturation Map's per-barangay point coordinates
(app/static/data/barangay_coords.json, via geocoding_service.merged_coords)
into filled polygon "cells" for a real choropleth map, one cell per
barangay -- clipped to Tarlac City's REAL, surveyed outline instead of
a synthetic rectangle.

WHERE THE CITY OUTLINE COMES FROM
----------------------------------
app/static/data/tarlac_city_boundary.json is Tarlac City's actual
administrative boundary polygon (OpenStreetMap relation osm_id
15585454, fetched from Nominatim -- the "licence" field inside that
file is OSM's own, and is why the Saturation Map page credits
"(c) OpenStreetMap contributors" near the legend: ODbL 1.0 requires
that attribution wherever this data is displayed, see
http://osm.org/copyright). It was verified against this app's own real
barangay coordinates before being adopted: every one of the 76 real,
PhilAtlas-sourced barangay points (barangay_coords.json) falls inside
it -- see tests/test_choropleth.py.

An earlier version of this file clipped each barangay's cell to
TARLAC_CITY_BOUNDS, a plain bounding RECTANGLE -- which is why the map
used to shade a big square that bled into neighboring municipalities
(Victoria, Gerona, San Jose, La Paz, Concepcion...) well past the
city's real limits. This version clips to the real outline instead, so
the shaded area now stops exactly at Tarlac City's actual border.

THE HONEST LIMITATION THIS FILE STILL DOCUMENTS: there is no publicly
available per-BARANGAY administrative boundary dataset for Tarlac
City's 76 barangays (only the point coordinates -- see
geocoding_service.py's own docstring for the research trail). So while
the OUTER edge of the choropleth is now the real city boundary, the
internal lines DIVIDING one barangay's cell from its neighbor's are
still a computed nearest-neighbor (Voronoi) partition: the cell for a
barangay is "every point inside Tarlac City that is closer to this
barangay's real coordinate than to any other barangay's." That is a
precise, verifiable mathematical property (see
tests/test_choropleth.py's containment/area tests), not a guess -- but
it is not a traced administrative line either. If a real barangay
boundary dataset becomes available, only this file needs to change;
everything downstream (the API route, map.js) just consumes whatever
GeoJSON this function returns.

HOW THE CELLS ARE COMPUTED
----------------------------
Earlier versions of this file used scipy.spatial.Voronoi plus a
hand-rolled Sutherland-Hodgman clip against the rectangle. Clipping a
Voronoi cell against Tarlac City's real (concave, 300+ vertex) outline
needs a general polygon-clipping algorithm, not the simple
convex-vs-convex case Sutherland-Hodgman handles -- and this project
avoids adding a heavyweight computational-geometry dependency (like
shapely, which also could not be installed in this project's own
sandboxed dev environment to even test against) for that. Instead:

1. Rasterize a fine grid over the city boundary's bounding box.
2. For every grid point, test whether it's inside the real city
   polygon (vectorized ray-casting) and, if so, which barangay's real
   coordinate is nearest (plain Euclidean nearest-neighbor -- the
   textbook definition of a Voronoi cell, computed by brute force
   instead of via scipy.spatial.Voronoi, since brute force over 76
   points is fast and needs no special-cased "infinite region"
   handling at the grid's edges).
3. Trace each barangay's resulting raster region back into a vector
   polygon with marching squares (`contourpy`, the same contouring
   engine matplotlib uses), which naturally follows the real,
   concave city outline and produces smooth (not blocky/pixelated)
   edges via linear interpolation between grid points.

A barangay whose cell gets cut into more than one disconnected piece by
a concave notch in the real city outline (this does happen -- see
`tests/test_choropleth.py`) is returned as a GeoJSON MultiPolygon
rather than silently dropping a piece.
"""

import json
import os

import numpy as np
import contourpy

CITY_BOUNDARY_RELPATH = os.path.join("static", "data", "tarlac_city_boundary.json")

# Grid resolution for the rasterize-then-contour computation below.
# 900x720 over Tarlac City's ~0.20 x 0.16 degree extent is about 25m
# per pixel -- comfortably finer than meaningful barangay-to-barangay
# spacing -- and computes in ~2-3 seconds; see
# compute_choropleth_geojson()'s own docstring for why that cost is
# paid at most once per (set of coordinates), not per request.
GRID_RESOLUTION = (900, 720)

_boundary_cache = None
_result_cache = {"key": None, "value": None}


def load_city_boundary_ring(app):
    """Tarlac City's real outer boundary ring as a list of [lng, lat]
    points (GeoJSON order), read once per process from
    app/static/data/tarlac_city_boundary.json and cached in memory --
    it never changes at runtime."""
    global _boundary_cache
    if _boundary_cache is not None:
        return _boundary_cache
    path = os.path.join(app.root_path, CITY_BOUNDARY_RELPATH)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ring = data["features"][0]["geometry"]["coordinates"][0]
    _boundary_cache = [[float(x), float(y)] for x, y in ring]
    return _boundary_cache


def _point_in_polygon_grid(X, Y, ring):
    """Vectorized ray-casting point-in-polygon test. `ring` is an
    (n+1, 2) array, closed (ring[0] == ring[-1]). Returns a boolean
    array shaped like X/Y. Loops over the ring's edges (a few hundred
    at most for a real administrative boundary), not over grid points,
    so each iteration is a single cheap elementwise numpy op across
    the whole grid at once."""
    n_edges = len(ring) - 1
    x1, y1 = ring[:-1, 0], ring[:-1, 1]
    x2, y2 = ring[1:, 0], ring[1:, 1]
    px, py = X.ravel(), Y.ravel()
    inside = np.zeros(px.shape, dtype=bool)
    for i in range(n_edges):
        xi1, yi1, xi2, yi2 = x1[i], y1[i], x2[i], y2[i]
        cond = (yi1 > py) != (yi2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_intersect = (xi2 - xi1) * (py - yi1) / (yi2 - yi1) + xi1
        inside ^= cond & (px < x_intersect)
    return inside.reshape(X.shape)


# How many grid points to measure against every barangay at once.
#
# THIS NUMBER IS A MEMORY BUDGET, NOT A SPEED KNOB. The distance
# computation below is (n_grid x n_points) float64. Done in one shot at
# the shipped 900x720 resolution that is 648,000 x 76 x 8 bytes = 394 MB
# for the result alone, and numpy materialises two same-sized
# temporaries before it (the subtraction, then the square) -- measured
# peak was 762 MB.
#
# A free Render instance has 512 MB TOTAL and this app already holds
# about 210 MB (Flask, numpy, pandas, scikit-learn, the Random Forest).
# So the un-chunked version could not run there at all: the worker was
# OOM-killed mid-request, and the Saturation Map drew the city outline
# (a separate, cheap endpoint) with no cells inside it.
#
# Chunking bounds the peak at roughly CHUNK x n_points x 8 x 3 bytes --
# about 58 MB at this value, whatever the grid resolution. The output
# is bit-for-bit identical; only the allocation pattern changes.
_NEAREST_CHUNK = 32_768


def _nearest_index_grid(X, Y, points):
    """For every grid point, the index (into `points`) of the nearest
    one -- an ordinary Euclidean Voronoi partition, computed by brute
    force. `points` is small (Tarlac City's 76-ish barangays).

    Computed in fixed-size blocks of grid points rather than all at
    once, so peak memory is bounded by _NEAREST_CHUNK above instead of
    scaling with the grid. See that constant for why that matters.
    """
    flat_x, flat_y = X.ravel(), Y.ravel()
    out = np.empty(flat_x.shape[0], dtype=np.intp)
    px, py = points[:, 0][None, :], points[:, 1][None, :]
    for start in range(0, flat_x.shape[0], _NEAREST_CHUNK):
        stop = start + _NEAREST_CHUNK
        dx = flat_x[start:stop, None] - px
        dy = flat_y[start:stop, None] - py
        np.multiply(dx, dx, out=dx)          # in place: no third temporary
        np.multiply(dy, dy, out=dy)
        np.add(dx, dy, out=dx)
        np.argmin(dx, axis=1, out=out[start:stop])
    return out.reshape(X.shape)


def _ring_area(ring_pts):
    """Shoelace formula. `ring_pts` is an (n, 2) array; need not be
    explicitly closed."""
    x, y = ring_pts[:, 0], ring_pts[:, 1]
    return 0.5 * abs(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]) + (x[-1] * y[0] - x[0] * y[-1]))


def _cache_key(coords):
    return tuple(sorted((name, round(pt["lat"], 6), round(pt["lng"], 6)) for name, pt in coords.items()))


# A PRECOMPUTED ANSWER, COMMITTED TO THE REPO.
#
# These cells are a pure function of the 76 barangay coordinates, and
# those coordinates are a static, committed file -- so the result is a
# CONSTANT. Computing it per deployment was work with no possible
# variation in the output.
#
# That mattered: the computation rasterizes a 900x720 grid, and on a
# 512 MB instance it is the single largest thing the app ever does.
# When it fails there, the front end has no way to say so -- the map
# simply draws the city outline with nothing inside it. Reading a file
# cannot fail that way.
#
# The file carries the fingerprint of the coordinate set it was built
# from. If the app's coordinates ever differ from it -- an LGU upload
# introduces a barangay, or someone edits barangay_coords.json -- the
# fingerprint will not match and the full computation runs as before.
# So this is a fast path, never a substitute for the real thing.
#
# Rebuild it with:  python scripts/precompute_choropleth.py
PRECOMPUTED_CELLS_NAME = "barangay_cells.json"
_precomputed_cache = {"loaded": False, "fingerprint": None, "value": None}


def _fingerprint(coords):
    """A short, stable digest of a coordinate set, for deciding whether
    the committed cells still describe it."""
    import hashlib

    parts = ["{}|{:.6f}|{:.6f}".format(n, la, ln) for n, la, ln in _cache_key(coords)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def load_precomputed_cells(app):
    """The committed cells, or None if the file is absent or unreadable.
    Never raises: a missing file just means "compute it", which is
    always correct, only slower."""
    if _precomputed_cache["loaded"]:
        return _precomputed_cache["fingerprint"], _precomputed_cache["value"]

    _precomputed_cache["loaded"] = True
    try:
        path = os.path.join(app.root_path, "static", "data", PRECOMPUTED_CELLS_NAME)
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        geojson = payload.get("geojson")
        if geojson and geojson.get("features"):
            _precomputed_cache["fingerprint"] = payload.get("fingerprint")
            _precomputed_cache["value"] = geojson
    except (OSError, ValueError, AttributeError):  # pragma: no cover - defensive
        pass
    return _precomputed_cache["fingerprint"], _precomputed_cache["value"]


def compute_choropleth_geojson(coords, city_boundary_ring=None, app=None, resolution=None, force=False):
    """`coords` is {name: {"lat":, "lng":, ...}} -- the same shape
    geocoding_service.merged_coords() returns. `city_boundary_ring` is
    a list of [lng, lat] points (closed ring); if not given, it's
    loaded from app/static/data/tarlac_city_boundary.json via `app`
    (one of the two must be provided).

    Returns a GeoJSON FeatureCollection, one Polygon (or, for the rare
    barangay whose cell the real city outline splits into more than
    one piece, MultiPolygon) per name, with `properties.name` set to
    that barangay's name. Ring coordinates are in GeoJSON's own
    [lng, lat] order.

    The result is cached in-process (keyed on the exact set of
    coordinates given), since the underlying computation is a
    deterministic, moderately expensive (rasterize + contour) function
    of the app's own coordinate data, which changes only on the rare
    occasion a fallback-geocoded location is newly resolved. Pass
    force=True to bypass the cache (mirrors merged_coords()'s own
    fallback-resolution refresh path).
    """
    if city_boundary_ring is None:
        if app is None:
            raise ValueError("compute_choropleth_geojson needs either city_boundary_ring or app")
        city_boundary_ring = load_city_boundary_ring(app)

    key = _cache_key(coords)
    if not force and _result_cache["key"] == key:
        return _result_cache["value"]

    # The committed answer, when it still describes these coordinates.
    # See PRECOMPUTED_CELLS_NAME above for why this exists.
    if not force and app is not None:
        stored_fingerprint, stored = load_precomputed_cells(app)
        if stored is not None and stored_fingerprint == _fingerprint(coords):
            _result_cache["key"], _result_cache["value"] = key, stored
            return stored

    names = list(coords.keys())
    if len(names) < 1:
        result = {"type": "FeatureCollection", "features": []}
        _result_cache["key"], _result_cache["value"] = key, result
        return result

    ring = np.asarray(city_boundary_ring, dtype=float)
    points = np.array([[coords[name]["lng"], coords[name]["lat"]] for name in names])

    xmin, xmax = ring[:, 0].min(), ring[:, 0].max()
    ymin, ymax = ring[:, 1].min(), ring[:, 1].max()
    res_x, res_y = resolution or GRID_RESOLUTION
    xs = np.linspace(xmin, xmax, res_x)
    ys = np.linspace(ymin, ymax, res_y)
    X, Y = np.meshgrid(xs, ys)

    inside_city = _point_in_polygon_grid(X, Y, ring)
    nearest = _nearest_index_grid(X, Y, points)
    labels = np.where(inside_city, nearest, -1)

    # A raster/marching-squares fragment smaller than a handful of
    # pixels is discretization noise at a boundary crossing, not a
    # real sliver of a barangay -- see tests/test_choropleth.py.
    dx = (xmax - xmin) / max(res_x - 1, 1)
    dy = (ymax - ymin) / max(res_y - 1, 1)
    min_area = 4 * dx * dy

    features = []
    for i, name in enumerate(names):
        mask = (labels == i).astype(np.float64)
        if not mask.any():
            continue
        cg = contourpy.contour_generator(x=X, y=Y, z=mask, fill_type="ChunkCombinedOffset")
        chunk_points, chunk_offsets = cg.filled(0.5, 1.5)
        pts = chunk_points[0]
        if pts is None:
            continue
        offsets = chunk_offsets[0]

        rings = []
        for j in range(len(offsets) - 1):
            ring_pts = pts[offsets[j] : offsets[j + 1]]
            if _ring_area(ring_pts) < min_area:
                continue
            closed = ring_pts.tolist()
            if closed[0] != closed[-1]:
                closed.append(closed[0])
            rings.append([[round(float(x), 6), round(float(y), 6)] for x, y in closed])

        if not rings:
            continue

        if len(rings) == 1:
            geometry = {"type": "Polygon", "coordinates": [rings[0]]}
        else:
            geometry = {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}

        features.append(
            {
                "type": "Feature",
                "properties": {"name": name},
                "geometry": geometry,
            }
        )

    result = {"type": "FeatureCollection", "features": features}
    _result_cache["key"], _result_cache["value"] = key, result
    return result
