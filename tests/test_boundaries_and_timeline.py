"""
tests/test_boundaries_and_timeline.py
---------------------------------------
Two Saturation Map changes:

  1. REAL BARANGAY BOUNDARIES. The map draws the official PSA/NAMRIA
     (2023) barangay polygons instead of computed nearest-neighbour
     cells, and a differently-written name ("Baras") is folded into its
     official barangay ("Baras-baras") instead of showing up as a 77th.

  2. THE MONTH TIMELINE. The map can be scored for any month from
     January 2020 to a year ahead -- history, current, or the model's
     prediction -- and every row says where its count came from.
"""

import json
import os
import re
from datetime import date, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.ml.seed_data import BARANGAY_NAMES, canonical_barangay
from app.models import LguData, MarketData, SmeProfile, SystemSetting, User

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOUNDARIES = os.path.join(ROOT, "app", "static", "data", "barangay_boundaries.json")
COORDS = os.path.join(ROOT, "app", "static", "data", "barangay_coords.json")
FOOD = "Food and Beverage"


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _client(app):
    with app.app_context():
        user = User(name="Map Viewer", email="viewer@map.test", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    client.post("/login", data={"email": "viewer@map.test", "password": "password123"})
    return client


def _market(location, count, recorded, industry=FOOD, source="Google Places API"):
    db.session.add(MarketData(
        industry_type=industry, location=location, competitor_count=count,
        population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
        average_rent=20000, source=source, date_recorded=recorded,
    ))


def _point_in_ring(x, y, ring):
    inside = False
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


# ---------------------------------------------------------------------
# 1. Official boundaries
# ---------------------------------------------------------------------

def test_the_boundaries_file_holds_all_76_official_barangays():
    with open(BOUNDARIES, encoding="utf-8") as handle:
        data = json.load(handle)
    names = [f["properties"]["name"] for f in data["features"]]
    assert sorted(names) == sorted(BARANGAY_NAMES)
    assert "PSA" in data["source"]["publisher"] and "NAMRIA" in data["source"]["publisher"]
    for feature in data["features"]:
        assert feature["properties"]["psgc"].startswith("PH0306916")
        ring = feature["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1] and len(ring) >= 8, feature["properties"]["name"]
    # Real boundaries, not a handful of straight lines per barangay.
    assert sum(len(f["geometry"]["coordinates"][0]) for f in data["features"]) > 3000


def test_every_barangay_point_falls_inside_its_own_official_polygon():
    """Two independent sources agreeing: PhilAtlas's barangay locations
    and PSA/NAMRIA's boundaries."""
    with open(BOUNDARIES, encoding="utf-8") as handle:
        polygons = {f["properties"]["name"]: f["geometry"]["coordinates"][0]
                    for f in json.load(handle)["features"]}
    with open(COORDS, encoding="utf-8") as handle:
        points = json.load(handle)["barangays"]
    outside = [name for name, pt in points.items()
               if name in polygons and not _point_in_ring(pt["lng"], pt["lat"], polygons[name])]
    assert outside == []


def test_the_map_endpoint_serves_the_official_polygons(app):
    client = _client(app)
    payload = client.get("/api/barangay-choropleth").get_json()
    assert payload["type"] == "FeatureCollection"
    assert {f["properties"]["name"] for f in payload["features"]} == set(BARANGAY_NAMES)
    assert all("psgc" in f["properties"] for f in payload["features"])
    assert "NAMRIA" in (payload.get("source") or "")


def test_the_computed_cells_remain_as_a_fallback(app, monkeypatch):
    from app.services import choropleth_service as cs

    monkeypatch.setitem(cs._official_cache, "loaded", True)
    monkeypatch.setitem(cs._official_cache, "value", None)
    with app.app_context():
        assert cs.barangay_boundaries_geojson(app) is None
    payload = _client(app).get("/api/barangay-choropleth").get_json()
    assert len(payload["features"]) == 76
    assert "psgc" not in payload["features"][0]["properties"]


def test_the_page_credits_the_boundary_source(app):
    body = _client(app).get("/saturation-map").get_data(as_text=True)
    assert "PSA &amp; NAMRIA (2023)" in body
    assert "approximations" not in body


# ---------------------------------------------------------------------
# 1b. "Baras" is Baras-baras
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Baras", "Baras-baras"),
    ("BARAS-BARAS", "Baras-baras"),
    ("Baras Baras", "Baras-baras"),
    ("Buhilit (Bubulit)", "Buhilit"),
    ("Santa Cruz (Alvindia Primero)", "Santa Cruz"),
    ("Sto. Niño", "Santo Nino"),
    ("Brgy. San Vicente", "San Vicente"),
    ("Capas", None),
    ("", None),
])
def test_canonical_barangay(raw, expected):
    assert canonical_barangay(raw) == expected


def test_a_stray_name_in_the_data_is_not_a_77th_barangay(app):
    with app.app_context():
        _market("Baras", 5, date.today())
        db.session.commit()
    rows = _client(app).get(f"/api/locations-forecast?industry_type={FOOD}").get_json()
    names = [r["location"] for r in rows]
    assert "Baras" not in names
    assert len(names) == 76


def test_startup_folds_aliases_into_the_official_name(app):
    from app.services.startup_migrations import _merge_barangay_aliases

    with app.app_context():
        _market("Baras", 5, date.today())
        _market("Capas", 3, date.today())            # not a Tarlac City barangay: left alone
        db.session.add(SmeProfile(user_id=1, business_name="x", industry_type=FOOD, location="BARAS"))
        db.session.commit()

        merged = _merge_barangay_aliases()
        assert merged["market_data"] == {"Baras": "Baras-baras"}
        assert merged["sme_profile"] == {"BARAS": "Baras-baras"}
        locations = {row.location for row in MarketData.query.all()}
        assert "Baras" not in locations and "Baras-baras" in locations and "Capas" in locations
        # Idempotent.
        assert _merge_barangay_aliases() == {}


def test_permit_counts_are_filed_under_the_official_name(tmp_path):
    from app.services.data_import_service import derive_permit_counts

    path = tmp_path / "permits.csv"
    path.write_text(
        "business_name,industry_type,barangay,status\n"
        "A,Food and Beverage,Baras,Active\n"
        "B,Food and Beverage,Baras-Baras,Active\n"
        "C,Food and Beverage,Somewhere Else,Active\n",
        encoding="utf-8",
    )
    counts = derive_permit_counts(str(path))
    assert counts == {(FOOD, "Baras-baras"): 2}


def test_an_upload_accepts_an_alias(app):
    from app.services.data_import_service import _resolve_barangay

    errors = []
    with app.app_context():
        assert _resolve_barangay("Baras", 2, errors) == "Baras-baras"
    assert errors == []


# ---------------------------------------------------------------------
# 2. The month timeline
# ---------------------------------------------------------------------

def test_periods_are_resolved_and_clamped():
    from app.services.saturation_timeline_service import resolve_period, timeline_bounds

    today = date(2026, 9, 15)
    bounds = timeline_bounds(today)
    assert bounds["start"] == date(2020, 1, 1)
    assert bounds["current"] == date(2026, 9, 1)
    assert bounds["end"] == date(2027, 9, 1)
    assert resolve_period(None, today) == (date(2026, 9, 1), "current")
    assert resolve_period("2024-03", today) == (date(2024, 3, 1), "history")
    assert resolve_period("2027-02", today) == (date(2027, 2, 1), "future")
    assert resolve_period("1999-01", today) == (date(2020, 1, 1), "history")
    assert resolve_period("2099-12", today) == (date(2027, 9, 1), "future")
    assert resolve_period("not-a-month", today) == (date(2026, 9, 1), "current")


def test_history_uses_counts_on_file_and_joins_up_with_them(app):
    from app.services.saturation_timeline_service import saturation_map_at

    with app.app_context():
        _market("Poblacion", 10, date(2024, 3, 15))
        _market("Poblacion", 20, date.today() - timedelta(days=2))
        db.session.commit()

        def at(month):
            return next(r for r in saturation_map_at(FOOD, ["Poblacion", "Tibag"], as_of=month)
                        if r["location"] == "Poblacion")

        recorded = at("2024-06")
        assert recorded["basis"] == "recorded" and recorded["competitor_count"] == 10
        before = at("2024-02")
        assert before["basis"] == "back-projected"
        # Anchored on the first real count (10), not today's (20).
        assert 8 <= before["competitor_count"] <= 11
        assert at("2021-01")["competitor_count"] <= before["competitor_count"]
        now = at(None)
        assert now["basis"] == "current" and now["competitor_count"] == 20
        # Fewer businesses then -> a lower score then.
        assert recorded["scores"]["saturation_index"] <= now["scores"]["saturation_index"]


def test_the_future_is_a_model_prediction_with_falling_confidence(app):
    from app.services.saturation_timeline_service import saturation_map_at

    with app.app_context():
        _market("Poblacion", 10, date.today() - timedelta(days=400))
        _market("Poblacion", 20, date.today() - timedelta(days=2))
        db.session.commit()
        month = lambda n: (date.today().replace(day=1) + timedelta(days=31 * n)).strftime("%Y-%m")
        now = next(r for r in saturation_map_at(FOOD, ["Poblacion"]) if r["location"] == "Poblacion")
        near = saturation_map_at(FOOD, ["Poblacion"], as_of=month(3))[0]
        far = saturation_map_at(FOOD, ["Poblacion"], as_of=month(11))[0]
        assert near["basis"] == far["basis"] == "predicted"
        # Its own recorded trend is upward, so the count keeps rising.
        assert now["competitor_count"] < near["competitor_count"] < far["competitor_count"]
        assert far["scores"]["confidence_level"] < near["scores"]["confidence_level"] < now["scores"]["confidence_level"]


def test_the_api_scores_the_map_for_the_chosen_month(app):
    client = _client(app)
    now = client.get(f"/api/locations-forecast?industry_type={FOOD}").get_json()
    past = client.get(f"/api/locations-forecast?industry_type={FOOD}&as_of=2022-05").get_json()
    ahead_month = (date.today().replace(day=1) + timedelta(days=95)).strftime("%Y-%m")
    ahead = client.get(f"/api/locations-forecast?industry_type={FOOD}&as_of={ahead_month}").get_json()
    assert len(now) == len(past) == len(ahead) == 76
    assert {r["period"] for r in now} == {"current"}
    assert {r["period"] for r in past} == {"history"} and past[0]["as_of"] == "2022-05"
    assert {r["basis"] for r in past} <= {"recorded", "back-projected"}
    assert {r["period"] for r in ahead} == {"future"} and {r["basis"] for r in ahead} == {"predicted"}


def test_the_page_has_the_timeline(app):
    body = _client(app).get("/saturation-map").get_data(as_text=True)
    assert 'id="mapTimeline"' in body
    current = date.today().replace(day=1)
    assert f'data-start="2020-01"' in body
    assert f'data-current="{current:%Y-%m}"' in body
    for zone in ("History", "Current", "Future"):
        assert f">{zone}<" in body
    assert "js/map_timeline.js" in body


def test_map_js_passes_the_month_and_labels_it():
    with open(os.path.join(ROOT, "app", "static", "js", "map.js"), encoding="utf-8") as handle:
        js = handle.read()
    assert "&as_of=${encodeURIComponent(asOf)}" in js
    assert "window.dssReloadMap = function" in js
    assert '"dss:locations-loaded"' in js
    assert "timelineDetailHtml(detail.location)" in js
    with open(os.path.join(ROOT, "app", "static", "js", "map_timeline.js"), encoding="utf-8") as handle:
        timeline = handle.read()
    assert "window.DSS_MAP_AS_OF" in timeline
    assert re.search(r"queued\s*=\s*true", timeline), "changes made mid-load must not be lost"
