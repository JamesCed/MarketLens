"""
tests/test_mobile_layout.py
-----------------------------
The phone layout: a top bar where the MarketLens name has room, charts
that are tall enough to read, an ordered map legend, no page wider than
the screen, and a loading circle that shows the moment a link is tapped.
"""

import os
import re

import pytest

from app import create_app
from app.extensions import db
from app.models import SmeProfile, SystemSetting, User
from tests.account_fixtures import STRONG_PASSWORD

ROOT = os.path.join(os.path.dirname(__file__), "..", "app")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
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


def _sme(app):
    user = User(name="Phone User", email="phone@x.test", role="SME")
    user.set_password(STRONG_PASSWORD)
    db.session.add(user)
    db.session.commit()
    client = app.test_client()
    client.post("/login", data={"email": "phone@x.test", "password": STRONG_PASSWORD})
    return user, client


def test_the_quick_links_move_into_the_menu_on_a_phone(app):
    _user, client = _sme(app)
    page = client.get("/saturation-map").get_data(as_text=True)
    sidebar = page[page.index('id="dssSidebar"'):page.index("</aside>")]
    assert 'class="dss-sidebar-quick"' in sidebar
    for label in ("PSA Data Source", "Community Forum", "Contact Us"):
        assert label in sidebar
    css = _read("static", "css", "style.css")
    phone = css[css.index("MOBILE (phones up to 575px"):]
    assert ".dss-topbar .dss-quick-links { display: none; }" in phone
    assert ".dss-topbar-brand { flex: 1 1 auto; min-width: 0; overflow: hidden; }" in phone


def test_the_outlook_chart_is_taller_and_labelled_on_a_phone():
    page = _read("templates", "sme", "planning.html")
    assert '<div class="dss-chart-box dss-outlook-chart">' in page
    assert "maintainAspectRatio: false" in page
    assert "outlookValueLabels" in page and "plugins: [outlookValueLabels]" in page
    assert "'Viability ' + (v / 6).toFixed(1) + '/10'" in page
    css = _read("static", "css", "style.css")
    assert ".dss-outlook-chart { aspect-ratio: auto; height: 300px; }" in css


def test_the_quarterly_chart_is_not_squashed_on_a_phone():
    page = _read("templates", "sme", "trend_reports.html")
    assert '<div class="dss-chart-box dss-quarterly-chart">' in page
    assert 'id="quarterlyChart" height=' not in page
    assert "String(label).split(\" \")" in page
    css = _read("static", "css", "style.css")
    assert ".dss-quarterly-chart { aspect-ratio: auto; height: 340px; }" in css


def test_the_map_legend_reads_from_least_to_most_crowded(app):
    _user, client = _sme(app)
    page = client.get("/saturation-map").get_data(as_text=True)
    legend = page[page.index('id="saturationLegend"'):page.index('id="densityLegend"')]
    order = re.findall(r'data-tier-filter="(\w+)"', legend)
    assert order == ["Low", "Moderate", "High", "Saturated"]
    assert 'id="tierShowAll"' in legend and 'class="dss-map-legend-head"' in legend
    css = _read("static", "css", "map.css")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css


def test_the_plan_picker_never_pushes_the_page_sideways(app):
    user, client = _sme(app)
    for name in ("Plan One", "Plan Two"):
        db.session.add(SmeProfile(user_id=user.user_id, business_name=name, location="Tibag",
                                  industry_type="Water Supply; Sewerage, Waste Management, and Remediation Activities"))
    db.session.commit()
    template = _read("templates", "sme", "recommendations.html")
    assert 'style="width:auto"' not in template
    assert 'class="dss-plan-select-form"' in template and "dss-plan-select" in template
    css = _read("static", "css", "style.css")
    assert ".dss-plan-select-form { flex: 1 1 100%; width: 100%; flex-direction: column;" in css
    assert ".dss-card { width: 100%; max-width: 100%; box-sizing: border-box; min-width: 0; }" in css


def test_the_loader_shows_at_once_on_a_phone():
    source = _read("static", "js", "main.js")
    assert 'const MOBILE = window.matchMedia("(max-width: 900px), (pointer: coarse)").matches;' in source
    assert "const LOADER_DELAY_MS = MOBILE ? 0 : 120;" in source
    click = source[source.index('document.addEventListener("click", (event) => {\n    if (event.button'):]
    assert "if (MOBILE) {\n      show();" in click
