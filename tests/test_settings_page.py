"""
tests/test_settings_page.py
-----------------------------
The Settings page: the panes that replaced the old single "Profile
Settings" page -- Profile, Business Preferences (SME only),
Notifications, Appearance, Security -- plus the per-account preferences
it saves.

What is worth pinning down here:

  1. The old URL still works. /profile-settings was in the nav, in
     bookmarks and in the browser history of anyone already using this.
  2. The preferences are COLUMNS on the user row, not rows in a
     preferences table -- saving one must add no rows anywhere.
  3. The switches do something. A theme is rendered into the page that
     follows it, and turning early warnings off actually stops the
     notification being created -- not merely hidden.
"""

from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import User, SmeProfile, MarketData, Notification, SystemSetting


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(app, email="sme@example.com", role="SME"):
    user = User(name="Test User", email=email, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _client(app, email="sme@example.com"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


# ---------------------------------------------------------------------
# 1. Routing and the panes
# ---------------------------------------------------------------------

def test_settings_lives_at_slash_settings(app):
    with app.app_context():
        _user(app)
        response = _client(app).get("/settings")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        for pane in ("Profile", "Notifications", "Appearance", "Security"):
            assert pane in body


def test_the_old_profile_settings_url_still_works(app):
    """It was the nav link and is in people's history -- it must not 404."""
    with app.app_context():
        _user(app)
        client = _client(app)

        redirect = client.get("/profile-settings")
        assert redirect.status_code == 301
        assert redirect.headers["Location"].endswith("/settings")

        followed = client.get("/profile-settings", follow_redirects=True)
        assert followed.status_code == 200


def test_the_nav_says_settings_not_profile_settings(app):
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings").get_data(as_text=True)
        nav = body[body.index('class="dss-nav"'):body.index("dss-sidebar-footer")]
        assert ">Settings" in nav.replace("\n", "").replace("  ", "") or "Settings\n" in nav
        assert "Profile Settings" not in nav


def _active_pane(body):
    """Which pane the server marked active. Parsed rather than
    string-matched: the template wraps the class and id onto separate
    lines, so a naive substring check silently never matches."""
    import re

    for match in re.finditer(
        r'<section class="dss-settings-pane([^"]*)"\s+id="section-([a-z]+)"', body
    ):
        if "active" in match.group(1):
            return match.group(2)
    return None


@pytest.mark.parametrize("section", ["profile", "plans", "notifications", "appearance", "security"])
def test_each_section_can_be_opened_directly(app, section):
    with app.app_context():
        _user(app)
        body = _client(app).get(f"/settings?section={section}").get_data(as_text=True)
        assert f'id="section-{section}"' in body
        assert _active_pane(body) == section, f"{section} did not open"


def test_an_unknown_section_falls_back_rather_than_erroring(app):
    with app.app_context():
        _user(app)
        response = _client(app).get("/settings?section=nonsense")
        assert response.status_code == 200
        assert _active_pane(response.get_data(as_text=True)) == "profile"


# ---------------------------------------------------------------------
# 2. Preferences cost no rows
# ---------------------------------------------------------------------

def test_saving_preferences_adds_no_rows(app):
    """They are columns on the user row. Saving must not create a
    preferences record, a settings record, or anything else."""
    with app.app_context():
        _user(app)
        client = _client(app)

        def row_counts():
            return {t.name: db.session.execute(db.select(db.func.count()).select_from(t)).scalar()
                    for t in db.metadata.sorted_tables}

        before = row_counts()
        client.post("/settings", data={"form_type": "appearance", "theme": "dark"}, follow_redirects=True)
        client.post("/settings", data={"form_type": "notifications"}, follow_redirects=True)
        after = row_counts()

        # audit_logs is expected to grow -- that is the point of an audit
        # trail. Nothing else may.
        after.pop("audit_logs", None)
        before.pop("audit_logs", None)
        assert after == before


def test_the_theme_is_stored_on_the_user_row(app):
    with app.app_context():
        user = _user(app)
        _client(app).post("/settings", data={"form_type": "appearance", "theme": "dark"},
                          follow_redirects=True)
        db.session.refresh(user)
        assert user.theme == "dark"


def test_an_unknown_theme_is_refused(app):
    with app.app_context():
        user = _user(app)
        _client(app).post("/settings", data={"form_type": "appearance", "theme": "neon"},
                          follow_redirects=True)
        db.session.refresh(user)
        assert user.theme == "light", "a hand-crafted POST set an invalid theme"


def test_the_default_theme_is_light(app):
    with app.app_context():
        user = _user(app)
        assert user.theme == "light"
        assert user.resolved_theme == "light"


def test_only_light_and_dark_are_offered(app):
    """"Match system" was dropped: the theme should be a choice the
    person made and can see, not one that flips at sunset."""
    from app.models.user import User as UserModel

    assert UserModel.THEMES == ("light", "dark")
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings?section=appearance").get_data(as_text=True)
        assert 'value="light"' in body and 'value="dark"' in body
        assert 'value="system"' not in body
        assert "Match system" not in body


def test_a_legacy_system_row_resolves_to_light(app):
    """Accounts created while 'match system' existed must not render an
    invalid data-bs-theme."""
    with app.app_context():
        user = _user(app)
        user.theme = "system"
        db.session.commit()
        assert user.resolved_theme == "light"
        body = _client(app).get("/settings").get_data(as_text=True)
        assert 'data-bs-theme="light"' in body


# ---------------------------------------------------------------------
# 3. Dark mode actually renders
# ---------------------------------------------------------------------

def test_dark_mode_reaches_the_html_element(app):
    """Server-rendered, not applied by script after paint -- otherwise
    every page flashes white first."""
    with app.app_context():
        user = _user(app)
        user.theme = "dark"
        db.session.commit()
        assert 'data-bs-theme="dark"' in _client(app).get("/settings").get_data(as_text=True)


def test_light_mode_reaches_the_html_element(app):
    with app.app_context():
        user = _user(app)
        user.theme = "light"
        db.session.commit()
        body = _client(app).get("/settings").get_data(as_text=True)
        assert 'data-bs-theme="light"' in body


def test_the_theme_is_rendered_server_side_not_applied_by_script(app):
    """Setting it from JavaScript would paint a white page and then flip
    it, visibly, on every navigation."""
    with app.app_context():
        user = _user(app)
        user.theme = "dark"
        db.session.commit()
        body = _client(app).get("/settings").get_data(as_text=True)
        head = body[: body.index("</head>")]
        assert 'data-bs-theme="dark"' in body[: body.index("<head>")]
        assert "prefers-color-scheme" not in head, "no system-preference sniffing should remain"


# ---------------------------------------------------------------------
# 4. The notification switch does something
# ---------------------------------------------------------------------

def _forecast_for(user, saturation):
    """A saturated forecast for this user, via the real engine path."""
    from app.services.forecasting_service import _maybe_fire_early_warning
    from app.models import ForecastResult

    market = MarketData(
        industry_type="Food and Beverage", location="Poblacion", competitor_count=90,
        population_density=8000, historical_success_rate=0.5, foot_traffic_index=50,
        average_rent=20000, source="Google Places API", date_recorded=date.today(),
    )
    db.session.add(market)
    profile = SmeProfile(
        user_id=user.user_id, business_name="X", industry_type="Food and Beverage",
        location="Poblacion", startup_capital=1000, business_stage="startup",
    )
    db.session.add(profile)
    db.session.commit()

    from app.services.forecasting_service import find_or_create_lgu_data
    lgu = find_or_create_lgu_data("Poblacion")
    forecast = ForecastResult(
        sme_id=profile.sme_id, market_id=market.market_id, lgu_id=lgu.lgu_id,
        input_industry_type="Food and Beverage", input_location="Poblacion",
        saturation_index=saturation, viability_score=1.0, forecast_date=date.today(),
    )
    db.session.add(forecast)
    db.session.commit()
    _maybe_fire_early_warning(forecast, profile)
    return forecast


def test_an_early_warning_fires_by_default(app):
    with app.app_context():
        user = _user(app)
        assert user.notify_saturation_change is True
        _forecast_for(user, saturation=95.0)
        assert Notification.query.filter_by(user_id=user.user_id, type="early_warning").count() == 1


def test_turning_the_switch_off_stops_the_notification_being_created(app):
    """Not merely hidden -- suppressed at the source, so turning the
    switch back on does not release a backlog of stale alerts."""
    with app.app_context():
        user = _user(app)
        user.notify_saturation_change = False
        db.session.commit()

        _forecast_for(user, saturation=95.0)
        assert Notification.query.filter_by(user_id=user.user_id).count() == 0


def test_the_switch_saves_off_when_the_checkbox_is_absent(app):
    """An unchecked checkbox submits nothing at all. Reading it as a
    value would leave every switch permanently on."""
    with app.app_context():
        user = _user(app)
        client = _client(app)

        client.post("/settings", data={"form_type": "notifications"}, follow_redirects=True)
        db.session.refresh(user)
        assert user.notify_saturation_change is False

        client.post("/settings",
                    data={"form_type": "notifications", "notify_saturation_change": "on"},
                    follow_redirects=True)
        db.session.refresh(user)
        assert user.notify_saturation_change is True


def test_all_four_notification_preferences_round_trip(app):
    from app.controllers.profile_controller import NOTIFICATION_FIELDS

    assert NOTIFICATION_FIELDS == (
        "notify_recommendations", "notify_weekly_trends",
        "notify_saturation_change", "notify_newsletter",
    )

    with app.app_context():
        user = _user(app)
        client = _client(app)

        client.post("/settings",
                    data={"form_type": "notifications", **{f: "on" for f in NOTIFICATION_FIELDS}},
                    follow_redirects=True)
        db.session.refresh(user)
        assert all(getattr(user, f) for f in NOTIFICATION_FIELDS)

        client.post("/settings", data={"form_type": "notifications"}, follow_redirects=True)
        db.session.refresh(user)
        assert not any(getattr(user, f) for f in NOTIFICATION_FIELDS)


def test_the_page_says_which_preferences_nothing_sends_yet(app):
    """Three of the four have no sender in this project. A switch that
    silently does nothing is worse than no switch, so the page has to
    admit it."""
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings?section=notifications").get_data(as_text=True)
        assert "Notification Preferences" in body
        for label in ["Email notifications for new recommendations",
                      "Weekly market trend reports",
                      "Alert me when saturation levels change",
                      "Monthly newsletter"]:
            assert label in body, label
        # Exactly three carry the caveat -- the saturation one is real.
        assert body.count("not sending yet") == 3


def test_only_the_saturation_preference_is_wired_to_anything(app):
    """Guards the claim the UI makes. If a sender is built later, wire
    it up and flip the flag -- this test will point at the label."""
    from app.controllers.profile_controller import NOTIFICATION_PREFS

    wired = [field for field, _l, _b, is_wired in NOTIFICATION_PREFS if is_wired]
    assert wired == ["notify_saturation_change"]


def test_mark_all_read_and_clear_read(app):
    with app.app_context():
        user = _user(app)
        db.session.add_all([
            Notification(user_id=user.user_id, type="info", message="one", is_read=False),
            Notification(user_id=user.user_id, type="info", message="two", is_read=False),
        ])
        db.session.commit()
        client = _client(app)

        client.post("/settings", data={"form_type": "mark_all_read"}, follow_redirects=True)
        assert Notification.query.filter_by(user_id=user.user_id, is_read=False).count() == 0

        client.post("/settings", data={"form_type": "clear_read"}, follow_redirects=True)
        assert Notification.query.filter_by(user_id=user.user_id).count() == 0


def test_clearing_read_notifications_leaves_unread_ones_alone(app):
    """Deleting an alert the person has never seen would defeat the
    purpose of raising it."""
    with app.app_context():
        user = _user(app)
        db.session.add_all([
            Notification(user_id=user.user_id, type="info", message="seen", is_read=True),
            Notification(user_id=user.user_id, type="early_warning", message="unseen", is_read=False),
        ])
        db.session.commit()

        _client(app).post("/settings", data={"form_type": "clear_read"}, follow_redirects=True)

        remaining = Notification.query.filter_by(user_id=user.user_id).all()
        assert len(remaining) == 1
        assert remaining[0].message == "unseen"


# ---------------------------------------------------------------------
# 5. Every role gets the page
# ---------------------------------------------------------------------

@pytest.mark.parametrize("role,email", [("SME", "s@x.com"), ("LGU", "l@x.com"), ("Admin", "a@x.com")])
def test_every_role_can_open_settings(app, role, email):
    with app.app_context():
        _user(app, email=email, role=role)
        response = _client(app, email=email).get("/settings")
        assert response.status_code == 200
        assert "Appearance" in response.get_data(as_text=True)


# ---------------------------------------------------------------------
# 6. Business Preferences -- where "My Plans" went
# ---------------------------------------------------------------------
# The plans used to live in a Bootstrap modal in shared/_topbar.html,
# which base.html includes on every authenticated page. These pin down
# that the move is complete in both directions: the popup is gone from
# every page, and the pane that replaced it really carries the plans and
# their controls.

def _plan(user, name="Carinderia ni Nena", industry="Food and Beverage",
          location="San Nicolas", stage="startup"):
    profile = SmeProfile(
        user_id=user.user_id, business_name=name, industry_type=industry,
        location=location, business_stage=stage, startup_capital=250000,
        employee_count=4, monthly_revenue_est=90000,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def test_the_my_plans_popup_is_gone_from_every_page(app):
    """The whole point of the move. If the modal is still being shipped,
    the page weight it cost is still being paid and there are now two
    places to edit a plan."""
    with app.app_context():
        user = _user(app)
        _plan(user)
        client = _client(app)

        for path in ("/settings", "/home", "/saturation-map", "/trend-reports"):
            body = client.get(path).get_data(as_text=True)
            assert 'id="myPlansModal"' not in body, f"the popup is still on {path}"
            assert "dss-myplans-btn" not in body, f"the top-bar button is still on {path}"


def test_the_pane_lists_the_plans_and_their_controls(app):
    with app.app_context():
        user = _user(app)
        _plan(user)
        body = _client(app).get("/settings?section=plans").get_data(as_text=True)

        assert "Business Preferences" in body
        assert "Carinderia ni Nena" in body
        assert "dss-plan-edit-btn" in body
        assert "dss-plan-delete-btn" in body


def test_the_plans_are_rendered_by_the_server_not_fetched(app):
    """The popup fetched /api/my-plans every time it opened, so the list
    flashed empty first. As a pane the rows are already in the HTML --
    which is also what makes it work with scripting off."""
    with app.app_context():
        user = _user(app)
        _plan(user, name="Bagong Tindahan")
        body = _client(app).get("/settings?section=plans").get_data(as_text=True)

        assert "Bagong Tindahan" in body
        assert "Loading your plans" not in body


def test_the_edit_form_posts_to_the_real_route(app):
    """With scripting off the Save button must still save, so the form
    needs a real action -- not a bare submit the JS intercepts."""
    with app.app_context():
        user = _user(app)
        profile = _plan(user)
        body = _client(app).get("/settings?section=plans").get_data(as_text=True)
        assert f'action="/home/plans/{profile.sme_id}/update"' in body


def test_editing_a_plan_from_settings_saves_it(app):
    """End to end against the route the pane posts to."""
    with app.app_context():
        user = _user(app)
        profile = _plan(user)
        sme_id = profile.sme_id

        response = _client(app).post(f"/home/plans/{sme_id}/update", data={
            "business_name": "Renamed Carinderia",
            "industry_type": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
            "location": "Matatalaib",
            "business_stage": "existing",
            "startup_capital": "500000",
            "employee_count": "9",
            "monthly_revenue_est": "150000",
        })

        assert response.status_code == 200
        assert response.get_json()["success"] is True

        saved = SmeProfile.query.get(sme_id)
        assert saved.business_name == "Renamed Carinderia"
        assert saved.industry_type == "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"
        assert saved.location == "Matatalaib"
        assert saved.employee_count == 9


def test_a_plan_can_be_deleted_from_settings(app):
    with app.app_context():
        user = _user(app)
        sme_id = _plan(user).sme_id

        response = _client(app).post(f"/home/plans/{sme_id}/delete")

        assert response.get_json()["success"] is True
        assert SmeProfile.query.get(sme_id) is None


def test_non_sme_accounts_get_no_business_preferences_tab(app):
    """An LGU has no sme_profile rows and never will, so a tab that can
    only ever say "you have no plans" would be noise."""
    with app.app_context():
        _user(app, email="lgu@example.com", role="LGU")
        body = _client(app, email="lgu@example.com").get("/settings").get_data(as_text=True)
        assert "Business Preferences" not in body
        assert 'id="section-plans"' not in body


def test_a_stale_plans_link_does_not_blank_the_page_for_an_lgu(app):
    """?section=plans is a link an SME could paste to a colleague. For an
    account with no such pane it has to fall back to Profile -- if it
    didn't, every pane would be hidden and no tab selected, and the page
    would render empty."""
    with app.app_context():
        _user(app, email="lgu2@example.com", role="LGU")
        body = _client(app, email="lgu2@example.com").get(
            "/settings?section=plans"
        ).get_data(as_text=True)
        assert _active_pane(body) == "profile"


def test_the_plans_pane_is_reachable_from_the_user_menu(app):
    """Removing the top-bar button without leaving a way in would just
    hide the feature."""
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings").get_data(as_text=True)
        assert "/settings?section=plans" in body


def test_an_industry_the_list_no_longer_has_is_not_silently_reclassified(app):
    """The taxonomy has been migrated once already. If a plan is still
    on an old industry name, the Edit select must keep it -- otherwise
    opening the form and pressing Save, changing nothing, would move the
    business to whatever industry happens to sort first."""
    with app.app_context():
        user = _user(app)
        profile = _plan(user)
        profile.industry_type = "Sari-sari Store"  # not in BUSINESS_TYPES
        db.session.commit()

        body = _client(app).get("/settings?section=plans").get_data(as_text=True)
        assert '<option value="Sari-sari Store" selected>' in body
