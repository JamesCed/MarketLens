"""
tests/test_map_revisions.py
------------------------------
Client revisions to the Saturation Map and the Home page's mini map:

  1. The barangay detail panel shows the barangay's THREE biggest
     industries by business count, ranked -- not the storyboard's fixed
     Food / Service / Retail rows, which hid whatever a barangay was
     actually known for. Counts are the same reconciled figures the rest
     of the app uses, and a generated placeholder is flagged as an
     estimate rather than passed off as measured.

  2. The Saturation Map page no longer reads as a wall of text: the
     three provenance paragraphs became one caption plus a folded
     "About this map's data" note, while the required OpenStreetMap,
     Leaflet and Google attributions stay on the page.

  3. map.js can open on a named barangay (window.DSS_FOCUS_LOCATION, set
     by the Home page from the chosen plan), and its popups auto-pan so
     they are never cut off in a small map.

The browser-side behaviour of (3) -- the popup actually fitting, the
hover card never being clipped, the focus not isolating -- is exercised
against a strict Leaflet stub by tests/leaflet_contract_test.js, which
the last test here runs under node.

Uses the in-memory SQLite TestingConfig, like the rest of the suite, so
it never touches real data and never makes a network call.

Run with:
    pytest tests/test_map_revisions.py -v
"""

import os
import re
import shutil
import subprocess
from datetime import date, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.ml.seed_data import BARANGAY_NAMES
from app.models import MarketData, SystemSetting, User

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MAP_JS = os.path.join(ROOT, "app", "static", "js", "map.js")
MAP_TEMPLATE = os.path.join(ROOT, "app", "templates", "sme", "saturation_map.html")

FOOD = "Food and Beverage"
RETAIL = "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"
SERVICES = "Other Service Activities"


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


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
    client = app.test_client()
    user = User(name="Map Revisions SME", email="map.revisions@example.com", role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    client.post("/login", data={"email": "map.revisions@example.com", "password": "password123"})
    return client


def _market_row(industry, location, count, source, recorded=None):
    return MarketData(
        industry_type=industry,
        location=location,
        competitor_count=count,
        population_density=8000,
        historical_success_rate=0.5,
        foot_traffic_index=45,
        average_rent=15000,
        source=source,
        date_recorded=recorded or date.today(),
    )


def _detail(client, location, industry=FOOD):
    response = client.get("/api/barangay-detail", query_string={"location": location, "industry_type": industry})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


# ---------------------------------------------------------------------------
# 1. Top-3 industries in the barangay detail
# ---------------------------------------------------------------------------


def test_detail_ranks_the_three_biggest_industries(app, client):
    """Most businesses first, at most three, measured vs estimated told
    apart -- and every count the reconciled one, not the raw newest row."""
    here, elsewhere = BARANGAY_NAMES[3], BARANGAY_NAMES[4]
    yesterday = date.today() - timedelta(days=1)
    archived = _market_row("Real Estate Activities", here, 500, "Google Places API")
    archived.archive(None, "test: archived rows must not count")
    db.session.add_all(
        [
            # A generated placeholder -- the biggest number, but not a real one.
            _market_row(FOOD, here, 70, "Manual"),
            # Stale snapshot of the same combo: only the freshest counts.
            _market_row(FOOD, here, 999, "Manual", date(2020, 1, 1)),
            # The newest row is a generated placeholder of 8 (what gets
            # written when an old Google row is refreshed with no live
            # lookup available), but Google saw 55 and the permit
            # register 12 the day before. Reconciled -- each real source
            # at its freshest, then the larger -- it is 55, FROM GOOGLE:
            # so a real, measured count, even though the row it rides on
            # is the placeholder. "Newest wins" would show 8, and reading
            # the row's own source would wrongly call it an estimate.
            _market_row("Manufacturing", here, 55, "Google Places API", yesterday),
            _market_row("Manufacturing", here, 12, "DTI", yesterday),
            _market_row("Manufacturing", here, 8, "Manual"),
            # From an LGU permit upload: a real source.
            _market_row(SERVICES, here, 50, "DTI"),
            # Fourth place: left out by the limit.
            _market_row("Construction", here, 10, "Google Places API"),
            # No businesses: never a "top" industry.
            _market_row("Mining and Quarrying", here, 0, "Manual"),
            archived,
            # Another barangay's businesses are not this one's.
            _market_row("Education", elsewhere, 300, "Google Places API"),
        ]
    )
    db.session.commit()

    detail = _detail(client, here)

    assert detail["top_industries"] == [
        {"industry": FOOD, "label": "Food & Beverage", "count": 70, "is_estimated": True},
        {"industry": "Manufacturing", "label": "Manufacturing", "count": 55, "is_estimated": False},
        {"industry": SERVICES, "label": "Other Services", "count": 50, "is_estimated": False},
    ]
    # Total still covers every industry on file here (reconciled), not
    # just the three shown: 70 + 55 + 50 + 10 + 0.
    assert detail["total_businesses"] == 185


def test_detail_breaks_ties_by_name(app, client):
    """Equal counts come back in a stable order, so the panel doesn't
    reshuffle on every refresh with unchanged data."""
    here = BARANGAY_NAMES[5]
    db.session.add_all(
        [
            _market_row("Manufacturing", here, 20, "Manual"),
            _market_row("Construction", here, 20, "Manual"),
            _market_row(FOOD, here, 20, "Manual"),
            _market_row("Education", here, 20, "Manual"),
        ]
    )
    db.session.commit()

    ranked = [item["industry"] for item in _detail(client, here)["top_industries"]]

    assert ranked == ["Construction", "Education", FOOD]


def test_detail_no_longer_carries_the_fixed_food_service_retail_fields(app, client):
    here = BARANGAY_NAMES[6]
    db.session.add_all([_market_row(FOOD, here, 8, "Google Places API"), _market_row(RETAIL, here, 5, "Manual")])
    db.session.commit()

    detail = _detail(client, here)

    for gone in ("food_count", "service_count", "retail_count"):
        assert gone not in detail, f"{gone} should be gone -- the panel ranks industries now"
    assert "total_businesses" in detail
    assert len(detail["top_industries"]) <= 3


def test_detail_for_a_barangay_with_no_business_counts_is_an_empty_list(app, client):
    """Nothing but zero counts on file: no industry is a "top" one, and
    the page shows its "No business counts on file yet" line."""
    here = BARANGAY_NAMES[7]
    db.session.add_all([_market_row(FOOD, here, 0, "Manual"), _market_row("Construction", here, 0, "Google Places API")])
    db.session.commit()

    detail = _detail(client, here)

    assert detail["top_industries"] == []
    assert detail["total_businesses"] == 0


def test_detail_for_a_never_scored_barangay_lists_only_the_flagged_estimate(app, client):
    """With NO rows at all, scoring the barangay creates the requested
    industry's placeholder (forecasting_service.find_or_create_market_data
    -- the same thing the map's own load does for all 76 barangays), so
    that one entry is what comes back. It must be flagged as an estimate,
    never presented as a measured count."""
    here = BARANGAY_NAMES[8]
    assert MarketData.query.filter_by(location=here).count() == 0

    top = _detail(client, here)["top_industries"]

    assert len(top) == 1
    assert top[0]["industry"] == FOOD
    assert top[0]["count"] > 0
    assert top[0]["is_estimated"] is True


# ---------------------------------------------------------------------------
# 2. A cleaner Saturation Map page
# ---------------------------------------------------------------------------


def _rendered_map_page(client):
    from flask import url_for

    with client.application.test_request_context():
        url = url_for("sme.saturation_map")
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_saturation_map_drops_the_long_paragraphs(app, client):
    body = _rendered_map_page(client)
    source = _read(MAP_TEMPLATE)
    for heading in ("How accurate is this?", "Why synthetic data is used at all.", "About the boundaries."):
        assert heading not in body, f"old paragraph '{heading}' is still on the page"
        assert heading not in source


def test_saturation_map_keeps_a_short_honest_about_section(app, client):
    body = _rendered_map_page(client)

    about = re.search(r"<details[^>]*>(.*?)</details>", body, re.S)
    assert about, "the data note should be a collapsible <details> section"
    about_html = about.group(1)
    assert re.search(r"<summary>\s*About this map", about_html), "collapsible should be titled 'About this map...'"
    bullets = re.findall(r"<li>", about_html)
    assert 3 <= len(bullets) <= 5, f"expected 3-5 concise bullets, found {len(bullets)}"
    # The same honest facts, just shorter: what is measured, what is
    # estimated (and that it is marked), why, and what the lines mean.
    text = re.sub(r"<[^>]+>", " ", about_html)
    # The boundaries bullet changed when the official PSA/NAMRIA barangay
    # boundaries replaced the computed cells: it now names that source
    # instead of calling the inner lines approximations.
    for fact in ("Google Places", "2024 PSA", "Estimated", "(est.)", "thousands of paid Google calls",
                 "official boundary", "PSA / NAMRIA", "city border", "not a survey"):
        assert fact in text, f"the About section lost: {fact!r}"
    # One short caption instead of the paragraphs.
    assert "Click a barangay for details." in body


def test_saturation_map_keeps_every_required_attribution(app, client):
    body = _rendered_map_page(client)
    assert "openstreetmap.org/copyright" in body
    assert "ODbL" in body
    assert "leafletjs.com" in body
    assert "powered by google" in body.lower()
    # map.css is this page's own stylesheet, loaded from <head>.
    head = body.split("</head>", 1)[0]
    assert "css/map.css" in head


def test_every_element_map_js_looks_up_is_on_the_saturation_map_page():
    """The page was restructured; map.js finds its controls by id and
    data-attribute, and a missing one fails silently (the feature just
    stops working). So check every one of them is still there."""
    js = _read(MAP_JS)
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    template = _read(MAP_TEMPLATE)
    template_ids = set(re.findall(r'\bid="([^"]+)"', template))

    queried = set(re.findall(r'getElementById\("([^"]+)"\)', code))
    queried |= set(re.findall(r'querySelector(?:All)?\(\s*["\'`]#([A-Za-z0-9_-]+)', code))
    assert {"dss-map", "detailPanel", "tierShowAll", "saturationLegend"} <= queried, "id extraction broke"

    # Built by map.js itself, inside HTML it injects.
    created_by_js = set(re.findall(r'\bid="([^"$]+)"', js))
    # renderLocationTable() is a guarded no-op on a page without a
    # #locationTable, and no current page has one.
    optional = {"locationTable"}
    missing = sorted(queried - template_ids - created_by_js - optional)
    assert missing == [], f"map.js looks up ids the Saturation Map no longer has: {missing}"

    for attribute in ("data-tier-filter", "data-tier-count", "data-overlay-caption"):
        assert attribute in code and attribute in template, f"{attribute} missing from map.js or the page"
    for tier in ("Low", "Moderate", "High", "Saturated"):
        assert f'data-tier-filter="{tier}"' in template and f'data-tier-count="{tier}"' in template


# ---------------------------------------------------------------------------
# 3. map.js: focus a barangay, popups that fit a small map
# ---------------------------------------------------------------------------


def _map_js_code():
    return "\n".join(line for line in _read(MAP_JS).splitlines() if not line.lstrip().startswith("//"))


def test_map_js_reads_the_focus_setting_and_auto_pans_its_popup():
    code = _map_js_code()
    assert "window.DSS_FOCUS_LOCATION" in code
    assert "autoPan: true" in code
    assert "keepInView: true" in code
    assert "autoPanPaddingTopLeft" in code and "autoPanPaddingBottomRight" in code
    assert "maxHeight" in code
    assert 'direction: "auto"' in code, "the hover tooltip should use Leaflet's 'auto' direction"


def test_map_js_renders_top_industries_not_fixed_rows():
    code = _map_js_code()
    assert "top_industries" in code
    for gone in ("food_count", "service_count", "retail_count", "Food Industry", "Service Industry", "Retail Industry"):
        assert gone not in code, f"map.js still renders {gone!r}"
    assert "(est.)" in code
    assert "No business counts on file yet" in code


def test_every_window_setting_map_js_reads_is_documented_at_the_top():
    """Home and the Saturation Map share map.js and differ only through
    window.DSS_* settings -- so the header must list every one it reads."""
    source = _read(MAP_JS)
    header = source.split("const CLUSTER_COLORS", 1)[0]
    read = set(re.findall(r"window\.(DSS_[A-Z_]+)", _map_js_code()))
    assert "DSS_FOCUS_LOCATION" in read and "DSS_DEFAULT_INDUSTRY" in read
    undocumented = sorted(name for name in read if name not in header)
    assert undocumented == [], f"window settings read but not documented in map.js's header: {undocumented}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_leaflet_contract_under_node():
    """Runs map.js against the strict Leaflet stub: focus, popup fit,
    hover-card fit, isolate/restore, the top-3 panel."""
    result = subprocess.run(
        ["node", os.path.join(ROOT, "tests", "leaflet_contract_test.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL LEAFLET CONTRACT CHECKS PASSED" in result.stdout


# ---------------------------------------------------------------------------
# Layout follow-up: controls on the right, map as tall as the side panel,
# and a tab that folds the side panel away.
# ---------------------------------------------------------------------------

def test_the_controls_sit_in_the_header_to_the_right_of_the_title():
    template = _read(MAP_TEMPLATE)
    head = template.split('class="dss-sat-head', 1)[1].split('id="satLayout"', 1)[0]
    title_at = head.index("dss-sat-title")
    toolbar = head.split('class="dss-sat-toolbar"', 1)[1]
    assert head.index('class="dss-sat-toolbar"') > title_at
    for control in ('id="mapSearchForm"', 'id="businessTypeSelect"', 'id="mapOverlaySelect"'):
        assert control in toolbar
    css = _read(os.path.join(ROOT, "app", "static", "css", "map.css"))
    rule = re.search(r"\.dss-sat-toolbar\s*\{([^}]+)\}", css).group(1)
    assert "margin-left: auto" in rule, "the controls should stay on the right when they wrap"


def test_the_map_grows_to_the_side_panels_height():
    css = _read(os.path.join(ROOT, "app", "static", "css", "map.css"))
    assert re.search(r"\.dss-sat-map-card\s*\{[^}]*height:\s*100%", css)
    map_rule = re.search(r"\.dss-sat-map-card #dss-map\s*\{([^}]+)\}", css).group(1)
    assert "flex: 1 1 auto" in map_rule and "height: auto" in map_rule and "min-height" in map_rule
    assert re.search(r"\.dss-sat-layout\s*\{[^}]*align-items:\s*stretch", css)


def test_the_side_panel_can_be_folded_away_and_brought_back():
    template = _read(MAP_TEMPLATE)
    assert 'id="satSideToggle"' in template
    assert 'aria-controls="satSide"' in template and 'id="satSide"' in template
    assert 'aria-expanded="true"' in template
    # Both panels live inside the foldable side column.
    side = template.split('id="satSide"', 1)[1]
    assert 'id="detailPanel"' in side and 'id="locationList"' in side
    script = template.split('id="satSideToggle"', 1)[1]
    assert 'classList.toggle("is-side-collapsed"' in script
    assert "try { collapsed = window.localStorage" in script, "storage must be guarded"
    css = _read(os.path.join(ROOT, "app", "static", "css", "map.css"))
    assert re.search(r"\.is-side-collapsed\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) 0", css)


def test_a_folded_panel_sends_details_to_the_map_popup():
    js = _read(MAP_JS)
    body = js.split("function hasDetailPanel()", 1)[1].split("\n}", 1)[0]
    assert 'closest(".is-side-collapsed")' in body
    assert 'addEventListener("dss:side-panel"' in js
