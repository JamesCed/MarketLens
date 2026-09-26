"""
tests/test_choropleth.py
----------------------------
Regression/unit tests for the Saturation Map's choropleth, now clipped
to Tarlac City's REAL administrative border instead of a bounding
rectangle (see app/services/choropleth_service.py's module docstring
for the full story: an earlier version clipped to
places_service.TARLAC_CITY_BOUNDS, a plain rectangle, which is why the
map used to shade a big square that bled into neighboring
municipalities well past Tarlac City's real limits).

Covers:

  1. app/static/data/tarlac_city_boundary.json -- Tarlac City's real
     OpenStreetMap boundary polygon. The one invariant that actually
     matters here: every one of the 76 real, PhilAtlas-sourced barangay
     coordinates (barangay_coords.json) must fall inside it, or the
     "real boundary" isn't actually consistent with the rest of this
     app's real, sourced data.

  2. app/services/choropleth_service.compute_choropleth_geojson() --
     each barangay's cell is the region inside the real city boundary
     closer to that barangay's real coordinate than to any other's
     (an ordinary nearest-neighbor/Voronoi partition), rasterized and
     traced back into a vector polygon. Tested invariants: every cell
     contains its own barangay's real point and no other's, every
     cell's vertices stay inside (or negligibly close to) the real
     city boundary, the cells' areas sum to the real city polygon's
     own area, and a barangay whose cell the real (concave) boundary
     splits into more than one piece comes back as a valid
     MultiPolygon rather than silently losing a piece.

  3. GET /api/barangay-choropleth and GET /api/tarlac-city-boundary --
     the two endpoints map.js fetches. Covers auth (login required,
     matching every other /api/* route) and response shape.

Uses the in-memory SQLite TestingConfig, like tests/test_app.py, so it
never touches real MySQL data and never makes a network call (the 76
shipped barangays resolve from the static, real coordinate file --
see app/services/geocoding_service.py -- with no Google API key
needed).

Run with:
    pytest tests/test_choropleth.py -v
"""

import json
import os

import pytest

from app import create_app
from app.extensions import db
from app.models import User, SystemSetting
from app.ml.seed_data import BARANGAY_NAMES
from app.services.choropleth_service import (
    compute_choropleth_geojson,
    load_city_boundary_ring,
    _ring_area,
)


# ---------------------------------------------------------------------------
# Shared geometry helpers (plain ray-casting -- no shapely/geopandas
# dependency; this project's own dev sandbox couldn't install either
# to test against, see choropleth_service.py's docstring).
# ---------------------------------------------------------------------------


def _point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    for i in range(n - 1):  # ring is closed (first == last)
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
    return inside


def _rings_of(feature):
    """Every exterior ring of a Feature's geometry, whether it's a
    Polygon (1 ring) or a MultiPolygon (one ring per disconnected
    piece -- see choropleth_service.py's docstring on why a cell can
    split)."""
    geom = feature["geometry"]
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    assert geom["type"] == "MultiPolygon"
    return [poly[0] for poly in geom["coordinates"]]


@pytest.fixture(scope="module")
def flask_app():
    return create_app("testing")


@pytest.fixture(scope="module")
def city_ring(flask_app):
    with flask_app.app_context():
        return load_city_boundary_ring(flask_app)


@pytest.fixture(scope="module")
def real_coords(flask_app):
    from app.services.geocoding_service import load_seed_coords

    with flask_app.app_context():
        return load_seed_coords(flask_app)


# ---------------------------------------------------------------------------
# app/static/data/tarlac_city_boundary.json -- the real OSM boundary itself.
# ---------------------------------------------------------------------------


def test_city_boundary_file_is_a_real_osm_export(flask_app):
    path = os.path.join(flask_app.root_path, "static", "data", "tarlac_city_boundary.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["type"] == "FeatureCollection"
    assert "openstreetmap" in data["licence"].lower()
    feature = data["features"][0]
    assert feature["properties"]["name"] == "Tarlac City"
    assert feature["properties"]["osm_type"] == "relation"
    assert feature["geometry"]["type"] == "Polygon"

    ring = feature["geometry"]["coordinates"][0]
    assert len(ring) > 50  # a real municipal boundary, not a 4-point rectangle
    assert ring[0] == ring[-1]  # closed linear ring


def test_every_real_barangay_point_falls_inside_the_real_city_boundary(city_ring, real_coords):
    outside = [
        name
        for name, pt in real_coords.items()
        if not _point_in_ring(pt["lng"], pt["lat"], city_ring)
    ]
    assert outside == [], f"real barangay coordinate(s) outside the real city boundary: {outside}"


# ---------------------------------------------------------------------------
# compute_choropleth_geojson()
# ---------------------------------------------------------------------------


def test_compute_choropleth_returns_one_cell_per_barangay(city_ring, real_coords):
    geojson = compute_choropleth_geojson(real_coords, city_boundary_ring=city_ring)

    assert geojson["type"] == "FeatureCollection"
    names = {f["properties"]["name"] for f in geojson["features"]}
    assert names == set(real_coords.keys())
    assert len(geojson["features"]) == len(BARANGAY_NAMES) == 76


def test_every_cell_contains_only_its_own_barangays_point(city_ring, real_coords):
    geojson = compute_choropleth_geojson(real_coords, city_boundary_ring=city_ring)
    cells = {f["properties"]["name"]: _rings_of(f) for f in geojson["features"]}

    for name, point in real_coords.items():
        px, py = point["lng"], point["lat"]
        own_rings = cells[name]
        assert any(_point_in_ring(px, py, r) for r in own_rings), (
            f"{name}'s own point should fall inside its own cell"
        )
        for other_name, other_rings in cells.items():
            if other_name == name:
                continue
            assert not any(_point_in_ring(px, py, r) for r in other_rings), (
                f"{name}'s point should NOT fall inside {other_name}'s cell"
            )


def test_cell_vertices_stay_within_the_real_city_boundary(city_ring, real_coords):
    """The whole point of this round's fix: the choropleth's OUTER edge
    must be the real city border, not a rectangle that overshoots it.
    Every cell vertex must be inside the real boundary, or on it within
    a small numerical/rasterization tolerance (a few grid cells wide)."""
    geojson = compute_choropleth_geojson(real_coords, city_boundary_ring=city_ring)

    xs = [p[0] for p in city_ring]
    ys = [p[1] for p in city_ring]
    # A generous but finite tolerance -- a real bounding-rectangle bug
    # (the old behavior) overshoots by tens of kilometers, not a grid cell.
    tol = 0.01  # ~1.1km at this latitude

    for feature in geojson["features"]:
        for ring in _rings_of(feature):
            for x, y in ring:
                assert min(xs) - tol <= x <= max(xs) + tol
                assert min(ys) - tol <= y <= max(ys) + tol


def test_cell_areas_sum_to_the_real_city_areas(city_ring, real_coords):
    import numpy as np

    geojson = compute_choropleth_geojson(real_coords, city_boundary_ring=city_ring)
    total = sum(
        _ring_area(np.array(ring)) for f in geojson["features"] for ring in _rings_of(f)
    )
    city_area = _ring_area(np.array(city_ring))
    # Cells tile the real city polygon almost exactly; the shortfall is
    # ordinary raster/marching-squares rounding at the grid resolution.
    assert total / city_area > 0.999


def test_empty_input_returns_empty_collection(city_ring):
    assert compute_choropleth_geojson({}, city_boundary_ring=city_ring) == {
        "type": "FeatureCollection",
        "features": [],
    }


def test_a_cell_the_real_boundary_splits_becomes_a_valid_multipolygon():
    """Construct a small, deliberately concave boundary shaped like a
    "U" that slices all the way through the middle barangay's Voronoi
    region, splitting it into two disconnected pieces -- and confirm
    compute_choropleth_geojson() reports that as a MultiPolygon (both
    pieces present) instead of silently keeping only one."""
    # A 10x10 square with a notch cut from the top-middle down to y=4,
    # splitting anything centered around x=5 into a left and right half
    # once the notch reaches deep enough into the shape.
    boundary = [
        [0, 0], [10, 0], [10, 10], [6, 10], [6, 4], [4, 4], [4, 10], [0, 10], [0, 0],
    ]
    coords = {
        "Left": {"lat": 5, "lng": 2},
        "Middle": {"lat": 8, "lng": 5},  # sits right under the notch
        "Right": {"lat": 5, "lng": 8},
    }
    geojson = compute_choropleth_geojson(
        coords, city_boundary_ring=boundary, resolution=(300, 300), force=True
    )
    by_name = {f["properties"]["name"]: f for f in geojson["features"]}
    assert set(by_name) == {"Left", "Middle", "Right"}
    assert by_name["Middle"]["geometry"]["type"] == "MultiPolygon"
    assert len(by_name["Middle"]["geometry"]["coordinates"]) == 2


def test_force_bypasses_the_cache():
    boundary = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    coords = {"A": {"lat": 5, "lng": 3}, "B": {"lat": 5, "lng": 7}}

    low_res = compute_choropleth_geojson(
        coords, city_boundary_ring=boundary, resolution=(20, 20), force=True
    )
    cached = compute_choropleth_geojson(
        coords, city_boundary_ring=boundary, resolution=(200, 200)
    )
    # Same (coords) cache key, so the second call returns the cached
    # low-res result even though resolution looks different here...
    assert cached == low_res
    # ...until force=True actually recomputes at the new resolution.
    high_res = compute_choropleth_geojson(
        coords, city_boundary_ring=boundary, resolution=(200, 200), force=True
    )
    assert high_res != low_res


# ---------------------------------------------------------------------------
# GET /api/barangay-choropleth and GET /api/tarlac-city-boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _login_sme(client, app):
    with app.app_context():
        user = User(name="Choropleth Test SME", email="choropleth.sme@example.com", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
    client.post(
        "/login",
        data={"email": "choropleth.sme@example.com", "password": "password123"},
    )


def test_barangay_choropleth_requires_login(client):
    response = client.get("/api/barangay-choropleth")
    assert response.status_code in (302, 401)


def test_tarlac_city_boundary_requires_login(client):
    response = client.get("/api/tarlac-city-boundary")
    assert response.status_code in (302, 401)


def test_barangay_choropleth_endpoint_shape(client, app, city_ring):
    _login_sme(client, app)

    response = client.get("/api/barangay-choropleth")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 76

    names = {f["properties"]["name"] for f in payload["features"]}
    assert names == set(BARANGAY_NAMES)

    sample = next(f for f in payload["features"] if f["properties"]["name"] == "San Isidro")
    assert sample["geometry"]["type"] in ("Polygon", "MultiPolygon")
    for ring in _rings_of(sample):
        assert len(ring) >= 4
        assert ring[0] == ring[-1]
        for lng, lat in ring:
            # Every vertex must fall within Tarlac City's real boundary
            # bounding box -- not just "somewhere in Luzon".
            assert min(p[0] for p in city_ring) - 0.01 <= lng <= max(p[0] for p in city_ring) + 0.01
            assert min(p[1] for p in city_ring) - 0.01 <= lat <= max(p[1] for p in city_ring) + 0.01


def test_tarlac_city_boundary_endpoint_shape(client, app):
    _login_sme(client, app)

    response = client.get("/api/tarlac-city-boundary")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1

    feature = payload["features"][0]
    assert feature["properties"]["name"] == "Tarlac City"
    assert feature["geometry"]["type"] == "Polygon"
    ring = feature["geometry"]["coordinates"][0]
    assert len(ring) > 50
    assert ring[0] == ring[-1]
