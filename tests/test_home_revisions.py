"""
tests/test_home_revisions.py
------------------------------
The Home page after the revisions round:

  * MAJOR 1 -- a plan "choice bar": every saved plan with its own
    viability score, one click to switch, nothing deleted or re-entered.
    (Editing a plan and moving it to Trash from the same bar is covered
    in tests/test_plan_trash.py.)
  * MAJOR 2 -- the mini map follows the chosen plan, and is bigger.
  * MINOR 2 -- location by map, a visible industry box, and Clear.
  * The Add New Plan dialog carries the broader business parameters and
    no monthly revenue.
"""

import json
import re

import pytest

from app import create_app
from app.extensions import db
from app.models import AuditLog, SmeProfile, SystemSetting, User

FOOD = "Food and Beverage"
RETAIL = "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email="owner@home.test"):
    user = User(name="Owner", email=email, role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _plan(user, name, industry, location, subcategory=None):
    profile = SmeProfile(user_id=user.user_id, business_name=name, industry_type=industry,
                         location=location, subcategory=subcategory, business_stage="startup")
    db.session.add(profile)
    db.session.commit()
    return profile


def _client(app, email="owner@home.test"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
    return client


def _global(page, name):
    match = re.search(rf"window\.{name}\s*=\s*(.+?);\s*$", page, re.MULTILINE)
    assert match, f"{name} is not set on the page"
    return json.loads(match.group(1))


@pytest.fixture
def two_plans(app):
    with app.app_context():
        user = _user()
        tibag = _plan(user, "Tibag Pandesal", FOOD, "Tibag", subcategory="bakery")
        balibago = _plan(user, "Balibago Gulong", RETAIL, "Balibago I", subcategory="tires_auto_parts")
        return {"tibag": tibag.sme_id, "balibago": balibago.sme_id}


# ---------------------------------------------------------------------
# The choice bar
# ---------------------------------------------------------------------

def test_every_plan_is_on_the_choice_bar(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert 'data-tour="plan-selector"' in page
    assert len(re.findall(r'class="dss-plan-chip(?: is-selected)?"', page)) == 2
    assert f'href="/home?plan={two_plans["tibag"]}"' in page
    assert f'href="/home?plan={two_plans["balibago"]}"' in page


def test_choosing_a_plan_switches_the_forecast_and_the_map(app, two_plans):
    with app.app_context():
        client = _client(app)
        page = client.get(f"/home?plan={two_plans['tibag']}").get_data(as_text=True)
        assert f"{FOOD} in Tibag" in page
        assert _global(page, "DSS_FOCUS_LOCATION") == "Tibag"
        assert _global(page, "DSS_DEFAULT_INDUSTRY") == FOOD

        page = client.get(f"/home?plan={two_plans['balibago']}").get_data(as_text=True)
        assert f"{RETAIL} in Balibago I" in page
        assert _global(page, "DSS_FOCUS_LOCATION") == "Balibago I"
        assert _global(page, "DSS_DEFAULT_INDUSTRY") == RETAIL
        # The industry box starts on the chosen plan's industry.
        assert re.search(r'<option value="' + re.escape(RETAIL) + r'"\s+selected>', page)


def test_the_choice_is_remembered_and_both_plans_keep_their_scores(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['tibag']}")
        client.get(f"/home?plan={two_plans['balibago']}")
        page = client.get("/home").get_data(as_text=True)
        assert f"{RETAIL} in Balibago I" in page, "coming back to Home should keep the chosen plan"
        # Both plans were scored by visiting them, and neither was lost.
        # The chip label is the plan model's viability now, not the
        # market-only score it used to be.
        assert len(re.findall(r'dss-plan-chip-score[^"]*">\s*Viability \d+\.\d/10', page)) == 2
        assert "Market score" not in page
        assert SmeProfile.query.count() == 2
        # The chip is a container; its first child is the selection link.
        selected = re.search(
            r'class="dss-plan-chip is-selected"[^>]*>\s*<a [^>]*aria-current="true"[^>]*>\s*'
            r'<span class="dss-plan-chip-name">([^<]+)', page)
        assert selected and selected.group(1) == "Balibago Gulong"


def test_someone_elses_plan_cannot_be_chosen(app, two_plans):
    with app.app_context():
        stranger = _user("stranger@home.test")
        theirs = _plan(stranger, "Not Yours", FOOD, "Poblacion")
        page = _client(app).get(f"/home?plan={theirs.sme_id}").get_data(as_text=True)
    assert "Not Yours" not in page


def test_switching_is_audited_once_not_on_every_reload(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['tibag']}")
        client.get(f"/home?plan={two_plans['tibag']}")
        client.get("/home")
        logs = AuditLog.query.filter_by(action="select_plan").all()
        assert len(logs) == 1
        assert logs[0].target_label and "Tibag Pandesal" in logs[0].target_label


# ---------------------------------------------------------------------
# The search row and the map
# ---------------------------------------------------------------------

def test_the_search_row_has_a_map_picker_a_visible_industry_box_and_clear(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert 'id="locationPickerModal"' in page
    assert 'data-tour="location-picker"' in page
    assert '<label class="form-label small fw-semibold mb-1" for="smeSearchIndustry">Industry type</label>' in page
    assert 'id="smeSearchClear"' in page
    assert "js/location_picker.js" in page
    # The search box is not a dropdown of 76 names any more.
    assert not re.search(r'id="smeSearchInput"[^>]*list=', page)


def test_the_map_is_bigger_and_fills_its_card(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert 'class="dss-mini-map dss-home-map' in page
    with open("app/static/css/style.css", encoding="utf-8") as handle:
        css = handle.read()
    rule = re.search(r"#dss-map\.dss-mini-map\.dss-home-map\s*\{([^}]+)\}", css)
    assert rule and "min-height: 440px" in rule.group(1) and "flex: 1 1 auto" in rule.group(1)


@pytest.mark.parametrize("anchor", [
    "plan-selector", "search", "location-picker", "industry-select", "clear-search",
    "industry-cards", "saved-plans", "add-plan", "mini-map", "forecast-panel", "recommendation-summary",
])
def test_the_walkthrough_anchors_are_on_home(app, two_plans, anchor):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert f'data-tour="{anchor}"' in page


# ---------------------------------------------------------------------
# Add New Plan
# ---------------------------------------------------------------------

def test_the_add_plan_dialog_has_the_new_fields_and_no_revenue(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert 'name="monthly_revenue_est"' not in page
    for name in ("subcategory", "product_offering", "innovation_idea", "offering_item", "capital"):
        assert f'name="{name}"' in page
    assert "window.DSS_SUBCATEGORIES" in page
    add = page[page.index('id="addPlanModal"'):page.index('id="locationPickerModal"')]
    assert re.search(r'id="ap-capital" name="capital"[^>]*required', add), "capital is required on a new plan"


def test_a_new_plan_is_selected_without_logging_a_switch(app, two_plans):
    with app.app_context():
        client = _client(app)
        response = client.post("/home/analyze", data={
            "business_name": "Kape sa Tibag", "industry_type": FOOD, "subcategory": "coffee_shop",
            "location": "Tibag", "product_offering": "Brewed coffee", "capital": "150000",
            "offering_item": ["Americano"], "offering_price": ["85"],
        })
        assert response.status_code == 302
        created = SmeProfile.query.filter_by(business_name="Kape sa Tibag").one()
        assert response.headers["Location"].endswith(f"/home?plan={created.sme_id}")
        assert created.subcategory == "coffee_shop"
        assert created.offering_items == [{"item": "Americano", "price": 85.0}]
        assert created.capital == 150000.0

        client.get(response.headers["Location"])
        assert AuditLog.query.filter_by(action="select_plan").count() == 0
        assert AuditLog.query.filter_by(action="run_forecast").count() == 1


def test_a_bad_new_plan_is_refused_with_the_reason(app, two_plans):
    with app.app_context():
        client = _client(app)
        page = client.post("/home/analyze", data={"business_name": "", "industry_type": FOOD, "location": "Tibag",
                                                  "capital": "150000"},
                           follow_redirects=True).get_data(as_text=True)
        assert "Business name is required." in page
        assert SmeProfile.query.count() == 2


# ---------------------------------------------------------------------
# Output encoding on the page's scripts
# ---------------------------------------------------------------------

def test_client_side_output_is_escaped():
    with open("app/static/js/sme_search.js", encoding="utf-8") as handle:
        search = handle.read()
    assert "${e(scores.location)}" in search
    assert "${scores.location}" not in search
    with open("app/templates/sme/home.html", encoding="utf-8") as handle:
        home = handle.read()
    assert "${esc(r.location)}" in home
    with open("app/static/js/location_picker.js", encoding="utf-8") as handle:
        picker = handle.read()
    assert "label.textContent = name" in picker


# ---------------------------------------------------------------------
# MINOR 1 -- the interior wears the sign-in page's look
# ---------------------------------------------------------------------

def test_signed_in_pages_carry_the_skin_and_the_landing_does_not(app, two_plans):
    with app.app_context():
        client = _client(app)
        for path in ("/home", "/settings", "/saturation-map", "/recommendations"):
            page = client.get(path).get_data(as_text=True)
            assert '<body class="dss-body dss-skin">' in page, path
    landing = app.test_client().get("/login").get_data(as_text=True)
    assert '<body class="dss-body">' in landing


def test_the_skin_keeps_the_prototype_content_colours():
    """Every skin rule is scoped to body.dss-skin (so it can be switched
    off in one place), the primary button stays the prototype blue, and
    the saturation tier colours are never restyled."""
    with open("app/static/css/style.css", encoding="utf-8") as handle:
        css = handle.read()
    skin = css.split("INTERIOR SKIN (body.dss-skin)", 1)[1].split("END INTERIOR SKIN", 1)[0]
    skin = skin.split("*/", 1)[1]                      # the block's own header comment
    skin = re.sub(r"/\*.*?\*/", "", skin, flags=re.S)  # and every comment inside it
    rules = [r.strip() for r in re.findall(r"(?:^|\})\s*([^{}@/]+)\{", skin) if r.strip()]
    for selector in rules:
        if selector.startswith(("from", "to", "0%", "100%")):
            continue
        for part in selector.split(","):
            assert "dss-skin" in part, f"unscoped skin rule: {part.strip()}"
    assert "var(--dss-blue)" in skin.split("body.dss-skin .btn-primary {", 1)[1].split("}", 1)[0]
    for tier in ("dss-pill-low", "dss-pill-moderate", "dss-pill-high", "dss-pill-saturated", "--dss-orange", "--dss-yellow"):
        assert tier not in skin
