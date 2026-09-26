"""
tests/test_business_taxonomy_and_demand.py
---------------------------------------------
Regression tests for this round's three additions:

  1. The BUSINESS_TYPES taxonomy, now the 20 PSIC top-level sections
     (app/ml/constants.py) -- every entry must have a display entry, a
     short label and a Places search term, no micro category may still
     be offered, and no search term may ask Google for micro
     establishments. Plus the micro-business exclusion filter itself.
  2. app/services/socio_demographic_service.py -- the PSA FIES-derived
     "demand" figures shown on the Saturation Map's tooltip/detail
     panel (food, utilities, transportation, recreation, education).
  3. The map's gestureHandling fix (app/static/js/map.js) and the
     Saturation Map / Home pages actually exposing window.DSS_DEMAND_SUMMARY.
"""

import os

import pytest

from app import create_app
from app.extensions import db
from app.models import User, SystemSetting
from app.ml.constants import (
    BUSINESS_TYPES,
    INDUSTRY_DISPLAY,
    FEATURED_BUSINESS_TYPES,
    DETAIL_PANEL_SECTIONS,
    short_industry_label,
)
from app.services.places_service import SEARCH_TERM_MAP, is_micro_business
from app.services.socio_demographic_service import (
    get_demand_breakdown,
    get_demand_summary,
    DEMAND_TOOLTIP_CATEGORIES,
    TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP,
)

# The 20 PSIC top-level sections this system now offers -- see
# app/ml/constants.py. The earlier 24 broad categories and the 22
# micro-business categories that briefly sat on top of them are both
# gone: this DSS targets SMEs, and app/services/industry_migration.py
# moved every stored row onto the sections below.
PSIC_SECTIONS = [
    "Agriculture, Forestry, and Fishing",
    "Mining and Quarrying",
    "Manufacturing",
    "Electricity, Gas, Steam, and Air Conditioning Supply",
    "Water Supply; Sewerage, Waste Management, and Remediation Activities",
    "Construction",
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "Transportation and Storage",
    "Accommodation and Food Service Activities",
    "Food and Beverage",
    "Information and Communication",
    "Financial and Insurance Activities",
    "Real Estate Activities",
    "Professional, Scientific, and Technical Activities",
    "Administrative and Support Service Activities",
    "Education",
    "Human Health and Social Work Activities",
    "Arts, Entertainment, and Recreation",
    "Other Service Activities",
    "Activities of Households as Employers",
]

# Names that must NOT come back as offered industries -- the micro
# categories the SME scope excludes, plus the superseded broad ones.
RETIRED_CATEGORY_NAMES = [
    "Sari-Sari Stores", "Carinderias & Eateries", "Food Carts & Street Food Stalls",
    "Piso WiFi & Internet Cafes", "Meat & Poultry Stalls", "Dry Goods & Apparel Stalls",
    "Retail", "Service", "Health & Wellness", "Food & Beverage", "Beauty & Personal Care",
]


def test_business_types_are_exactly_the_20_psic_sections():
    assert BUSINESS_TYPES == PSIC_SECTIONS


def test_retired_and_micro_categories_are_no_longer_offered():
    """The dropdown must not still offer a micro category (or an old
    broad one) after the taxonomy was reduced -- otherwise an SME could
    file a new plan under a name the rest of the system no longer
    speaks."""
    still_there = [name for name in RETIRED_CATEGORY_NAMES if name in BUSINESS_TYPES]
    assert still_there == []


def test_every_business_type_has_no_duplicates():
    assert len(BUSINESS_TYPES) == len(set(BUSINESS_TYPES))


def test_every_business_type_has_an_industry_display_entry():
    missing = [b for b in BUSINESS_TYPES if b not in INDUSTRY_DISPLAY]
    assert missing == []


def test_every_section_has_a_short_label_for_tight_ui():
    """Several PSIC names are long enough to break a card or a legend,
    so each one carries a short label -- see short_industry_label()."""
    for name in BUSINESS_TYPES:
        short = short_industry_label(name)
        assert short, f"{name!r} has no short label"
        assert len(short) <= 30, f"short label for {name!r} is still {len(short)} chars"


def test_featured_business_types_are_all_real_sections():
    assert all(b in BUSINESS_TYPES for b in FEATURED_BUSINESS_TYPES)


def test_detail_panel_sections_point_at_real_sections():
    """The Saturation Map's detail panel and the personal Trend Report
    both break out these three by name -- if one drifts from
    BUSINESS_TYPES the panel silently shows zeros."""
    for key, section in DETAIL_PANEL_SECTIONS.items():
        assert section in BUSINESS_TYPES, f"DETAIL_PANEL_SECTIONS[{key!r}] is not a real section"


def test_every_business_type_has_a_places_search_term():
    """Places API results are only relevant if each section has a
    realistic search term -- see places_service.SEARCH_TERM_MAP."""
    missing = [b for b in BUSINESS_TYPES if b not in SEARCH_TERM_MAP]
    assert missing == []


def test_search_terms_target_sme_scale_establishments():
    """The section terms must ask Google for SME-scale establishments,
    not the micro end of the same section -- asking for 'sari-sari
    store' would pull in exactly what the micro filter then has to throw
    away."""
    for section, term in SEARCH_TERM_MAP.items():
        lowered = term.lower()
        for micro_word in ("sari-sari", "carinderia", "food cart", "piso wifi", "tiangge"):
            assert micro_word not in lowered, f"{section!r} search term still asks for {micro_word!r}"


# ---------------------------------------------------------------------------
# Micro-business exclusion filter (places_service)
# ---------------------------------------------------------------------------


def test_micro_businesses_are_identified():
    for name in [
        "Aling Nena's Sari-Sari Store", "Nena Carinderia", "JR Vulcanizing Shop",
        "Piso WiFi Station", "Tiangge Apparel Stall", "Backyard Poultry Farm",
        "Mang Tony Food Cart", "Talipapa Fish Vendor",
    ]:
        assert is_micro_business(name), f"{name!r} should be filtered out as micro"


def test_sme_scale_businesses_are_not_filtered_out():
    """The filter is deliberately conservative -- a real SME must never
    be dropped by mistake, which matters more than catching every micro
    business (see places_service.MICRO_BUSINESS_PATTERNS)."""
    for name in [
        "SM City Tarlac", "Tarlac Hardware & Construction Supply", "Jollibee MacArthur Highway",
        "Mercury Drug Tarlac", "RDL Trucking Services", "Tarlac Provincial Hospital",
        "Central Luzon Doctors' Hospital", "BDO Tarlac Branch", "Kambingan sa Tarlac Restaurant",
    ]:
        assert not is_micro_business(name), f"{name!r} must NOT be filtered out"


# ---------------------------------------------------------------------------
# app/services/socio_demographic_service.py -- the FIES "demand" figures
# ---------------------------------------------------------------------------


def test_demand_breakdown_covers_exactly_the_five_requested_categories():
    breakdown = get_demand_breakdown()
    assert [c["key"] for c in breakdown] == DEMAND_TOOLTIP_CATEGORIES
    assert set(DEMAND_TOOLTIP_CATEGORIES) == {
        "food",
        "housing_utilities",
        "transportation",
        "recreation",
        "education",
    }


def test_demand_breakdown_amounts_derive_from_the_real_tarlac_total():
    breakdown = get_demand_breakdown()
    for entry in breakdown:
        expected = round(TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP * entry["national_share_percent"] / 100.0)
        assert entry["estimated_annual_php"] == expected
        # Every category must be a positive, plausible slice of the real
        # total -- never larger than the whole, never zero/negative.
        assert 0 < entry["estimated_annual_php"] < TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP


def test_recreation_is_flagged_as_the_softer_estimate():
    """Recreation is carried over from a different survey year (2021)
    than the other four (2023) -- see the service's module docstring.
    The UI relies on this flag to render that caveat distinctly, so it
    must be true for recreation and false for everything else."""
    breakdown = get_demand_breakdown()
    flags = {c["key"]: c["is_recreation_estimate"] for c in breakdown}
    assert flags["recreation"] is True
    assert all(v is False for k, v in flags.items() if k != "recreation")


def test_demand_summary_is_explicit_about_being_city_wide_not_barangay_specific():
    summary = get_demand_summary()
    assert "barangay" in summary["geographic_note"].lower()
    assert summary["tarlac_avg_annual_family_expenditure_php"] == TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP
    assert len(summary["categories"]) == 5


# ---------------------------------------------------------------------------
# map.js -- OpenStreetMap/Leaflet basemap (string-level checks; the full
# behavioral port is covered by the Leaflet API-contract test run under
# node, see README section 9)
# ---------------------------------------------------------------------------


def _map_js():
    path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "map.js")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_map_js_uses_leaflet_and_openstreetmap_tiles():
    contents = _map_js()
    assert "L.map(" in contents
    assert "tile.openstreetmap.org" in contents


def test_map_js_no_longer_calls_the_google_maps_sdk():
    """The basemap moved to OpenStreetMap -- any leftover google.maps
    CALL would throw at runtime now that the SDK is not loaded. Comments
    are exempt on purpose: several of them explain what each Leaflet
    call replaced, which is worth keeping."""
    code_lines = [
        line for line in _map_js().splitlines() if not line.lstrip().startswith("//")
    ]
    offenders = [
        line.strip() for line in code_lines
        if "google.maps" in line or "window.google" in line
    ]
    assert offenders == [], f"map.js still calls the Google Maps SDK: {offenders}"


def test_map_js_keeps_osm_attribution_and_google_places_attribution():
    """Two separate obligations: OSM's ODbL credit for the tiles, and
    Google's Places attribution for the competitor data shown on top of
    them -- the latter pinned at the TOP of the map."""
    contents = _map_js()
    assert "openstreetmap.org/copyright" in contents
    assert "Powered by Google" in contents
    assert '"topleft"' in contents


def test_map_js_guards_against_a_missing_leaflet_stylesheet():
    """leaflet.js and leaflet.css are two separate downloads. When the JS
    arrives and the CSS does not, Leaflet still builds every layer but
    draws it with no positioning rules -- tiles scatter across the page
    and the choropleth spills out of its container over the whole
    layout. That shipped once (a wrong SRI hash silently blocked the
    stylesheet), so map.js now probes for the CSS and refuses to build a
    broken map."""
    contents = _map_js()
    assert "isLeafletCssLoaded" in contents
    assert "leaflet-pane" in contents, "the probe should test a real Leaflet CSS rule"


def test_map_is_remeasured_after_layout_settles():
    """A map measured while its Bootstrap column is still sizing renders
    tiles for the wrong width."""
    assert "invalidateSize" in _map_js()


def test_leaflet_assets_carry_no_unverified_integrity_hash(subtests=None):
    """An SRI hash that doesn't match exactly what the CDN serves makes
    the browser BLOCK the file. For leaflet.css that is catastrophic
    rather than cosmetic -- it is the bug described above. If SRI is
    wanted back, the hashes must be copied from cdnjs, not typed from
    memory."""
    import re
    here = os.path.dirname(__file__)
    for template in ("saturation_map.html", "home.html"):
        path = os.path.join(here, "..", "app", "templates", "sme", template)
        with open(path, "r", encoding="utf-8") as f:
            markup = f.read()
        offenders = re.findall(r"<(?:link|script)[^>]*leaflet[^>]*>", markup)
        for tag in offenders:
            assert "integrity=" not in tag, f"{template} has an unverified SRI hash on a Leaflet asset"


def test_leaflet_stylesheet_is_loaded_from_the_head_block():
    """A stylesheet the map depends on belongs in <head>, applied before
    the map is built -- not beside the script at the end of <body>."""
    here = os.path.dirname(__file__)
    for template in ("saturation_map.html", "home.html"):
        path = os.path.join(here, "..", "app", "templates", "sme", template)
        with open(path, "r", encoding="utf-8") as f:
            markup = f.read()
        assert "{% block extra_head %}" in markup, f"{template} has no extra_head block"
        # Pull out just the extra_head block -- splitting on the first
        # {% endblock %} would grab the title block instead.
        head_block = markup.split("{% block extra_head %}", 1)[1].split("{% endblock %}", 1)[0]
        assert "leaflet.css" in head_block, f"{template} does not load leaflet.css in <head>"


def test_map_js_normalizes_geometry_to_leaflet_multipolygon_nesting():
    """Leaflet reads a two-level ring array as a polygon WITH A HOLE, so
    a multi-piece barangay cell (Balanti, Burot) must be handed the
    three-level form or its second piece is punched out of its first."""
    contents = _map_js()
    assert "poly.map((ring) => ring.map(([lng, lat]) => ({ lat, lng })))" in contents


# ---------------------------------------------------------------------------
# Saturation Map / Home pages expose window.DSS_DEMAND_SUMMARY
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
        user = User(name="Demand Test SME", email="demand.sme@example.com", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
    client.post("/login", data={"email": "demand.sme@example.com", "password": "password123"})


def test_saturation_map_page_exposes_demand_summary(client, app):
    _login_sme(client, app)
    with app.test_request_context():
        from flask import url_for
        url = url_for("sme.saturation_map")
    response = client.get(url)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "DSS_DEMAND_SUMMARY" in body
    assert "Non-Alcoholic Beverages" in body


def test_home_page_exposes_demand_summary(client, app):
    _login_sme(client, app)
    with app.test_request_context():
        from flask import url_for
        url = url_for("sme.home")
    response = client.get(url)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "DSS_DEMAND_SUMMARY" in body
