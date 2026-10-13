"""
tests/test_navigation_speed.py
--------------------------------
Moving between pages: no wasted redirect after signing in, no repeated
lookups of things that almost never change, caches that are keyed on
the data living long enough to matter, and sidebar pages fetched while
the pointer rests on their link.
"""

import json
import re

import pytest
from sqlalchemy import event

from app import create_app
from app.extensions import db
from app.models import SystemSetting, User
from tests.account_fixtures import STRONG_PASSWORD


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role):
    user = User(name=role.title(), email=email, role=role)
    user.set_password(STRONG_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


def _count_queries(app):
    seen = []
    event.listen(db.engine, "before_cursor_execute", lambda *a: seen.append(a[2]))
    return seen


@pytest.mark.parametrize("role,home", [("SME", "/home"), ("LGU", "/lgu/dashboard"), ("Admin", "/admin/dashboard")])
def test_signing_in_goes_straight_to_the_users_own_page(app, role, home):
    _user(f"{role.lower()}@nav.test", role)
    response = app.test_client().post("/login", data={"email": f"{role.lower()}@nav.test", "password": STRONG_PASSWORD})
    assert response.status_code == 302
    assert response.headers["Location"].endswith(home), "one redirect, not two through /"


def test_the_root_still_sends_everyone_to_the_right_place(app):
    assert app.test_client().get("/").headers["Location"].endswith("/login")
    _user("sme@nav.test", "SME")
    client = app.test_client()
    client.post("/login", data={"email": "sme@nav.test", "password": STRONG_PASSWORD})
    assert client.get("/").headers["Location"].endswith("/home")


def test_settings_are_read_once_and_a_saved_setting_is_seen_at_once(app):
    assert SystemSetting.get("kmeans_n_clusters") is not None
    seen = _count_queries(app)
    SystemSetting.get("kmeans_n_clusters")
    assert not [s for s in seen if "system_settings" in s], "the settings table is kept between reads"

    SystemSetting.set("saturation_alert_threshold", "61")
    assert SystemSetting.get("saturation_alert_threshold") == "61"

    row = SystemSetting.query.get("saturation_alert_threshold")
    row.setting_value = "62"
    db.session.commit()
    assert SystemSetting.get("saturation_alert_threshold") == "62", "an ORM write clears the kept copy"


def test_the_system_account_id_is_looked_up_once(app):
    from app.services.data_import_service import _system_user_id_or_none
    from app.services.forecasting_service import SYSTEM_USER_EMAIL

    assert _system_user_id_or_none() is None
    system = _user(SYSTEM_USER_EMAIL, "Admin")
    assert _system_user_id_or_none() == system.user_id, "a missing account is not remembered as missing"
    seen = _count_queries(app)
    assert _system_user_id_or_none() == system.user_id
    assert not seen


def test_admin_settings_save_drops_the_cached_figures(app, monkeypatch):
    from app.controllers import admin_controller

    calls = []
    monkeypatch.setattr(admin_controller, "_refresh_figures", lambda: calls.append(1))
    _user("admin@nav.test", "Admin")
    client = app.test_client()
    client.post("/login", data={"email": "admin@nav.test", "password": STRONG_PASSWORD})
    client.post("/admin/settings", data={"msi_weight_competitor_density": "0.5"})
    assert calls, "changed scoring weights must not wait for a cache to expire"


def test_fingerprint_keyed_caches_last_half_an_hour():
    from app.services import diversification_service, forecasting_service, trend_analytics_service

    assert forecasting_service._BATCH_CACHE_TTL_SECONDS == 1800
    assert trend_analytics_service._CACHE_TTL_SECONDS == 1800
    assert trend_analytics_service._OUTLOOK_CACHE_TTL_SECONDS == 1800
    assert diversification_service._PLAN_CACHE_TTL_SECONDS == 1800


def test_sidebar_pages_are_prefetched_on_hover_but_not_the_open_one(app):
    _user("sme@nav.test", "SME")
    client = app.test_client()
    client.post("/login", data={"email": "sme@nav.test", "password": STRONG_PASSWORD})
    page = client.get("/saturation-map").get_data(as_text=True)
    block = re.search(r'<script type="speculationrules">\s*(.+?)\s*</script>', page, re.S)
    assert block, "speculation rules missing"
    rules = json.loads(block.group(1))
    rule = rules["prefetch"][0]
    assert rule["eagerness"] == "moderate"
    assert {"selector_matches": ".dss-nav-link"} in rule["where"]["and"]
    assert {"not": {"selector_matches": ".active"}} in rule["where"]["and"]
    logout_links = re.findall(r'<a[^>]*class="[^"]*dss-nav-link[^"]*"[^>]*href="/logout"', page)
    assert not logout_links
    login = app.test_client().get("/login").get_data(as_text=True)
    assert "speculationrules" not in login
