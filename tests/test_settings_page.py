"""
tests/test_settings_page.py
-----------------------------
The Settings page: the panes that replaced the old single "Profile
Settings" page -- Profile, Notifications, Appearance, Security and
Tutorial, the same five for every role -- plus the per-account
preferences it saves.

What is worth pinning down here:

  1. The old URL still works. /profile-settings was in the nav, in
     bookmarks and in the browser history of anyone already using this.
  2. The preferences are COLUMNS on the user row, not rows in a
     preferences table -- saving one must add no rows anywhere.
  3. The switches do something. A theme is rendered into the page that
     follows it, and turning early warnings off actually stops the
     notification being created -- not merely hidden.
  4. Business Preferences is GONE, not hidden: the plans are managed on
     the Home page now (edit, move to Trash, restore -- see
     tests/test_plan_trash.py, which owns the plan routes' tests), and
     an old ?section=plans link still lands somewhere useful.

The Tutorial pane and where the Settings link sits in the sidebar are
tested in tests/test_settings_tutorial.py.
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


def test_the_sidebar_says_settings_not_profile_settings(app):
    """The link is in the sidebar FOOTER now, above Support, rather than
    in the list of pages -- but it still says "Settings"."""
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings").get_data(as_text=True)
        sidebar = body[body.index('class="dss-sidebar"'):body.index("</aside>")]
        footer = sidebar[sidebar.index('class="dss-sidebar-footer"'):]
        assert "</i> Settings" in footer
        assert "Profile Settings" not in sidebar


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


@pytest.mark.parametrize("section", ["profile", "notifications", "appearance", "security", "tutorial"])
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
    """Only the newsletter has no sender now that market_alert_service
    exists. A switch that silently does nothing is worse than no switch,
    so the page has to admit which one that is -- and only that one."""
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings?section=notifications").get_data(as_text=True)
        assert "Notification Preferences" in body
        for label in ["Email notifications for new recommendations",
                      "Weekly market trend reports",
                      "Alert me when saturation levels change",
                      "Monthly newsletter"]:
            assert label in body, label

        # Exactly one carries the caveat: the monthly newsletter, which
        # needs a person to write it. The other three now send.
        assert body.count("not sending yet") == 1

        # And it is attached to the newsletter specifically, not merely
        # present somewhere on the page. Counting alone would pass if the
        # badge moved to the wrong switch.
        # The badge is rendered immediately after its own label, so the
        # newsletter's label must be the one just before it.
        newsletter_at = body.index("Monthly newsletter")
        badge_at = body.index("not sending yet")
        assert 0 < badge_at - newsletter_at < 400, \
            "the 'not sending yet' badge is not the newsletter's"


def test_every_preference_claiming_to_be_wired_has_a_sender(app):
    """Guards the claim the UI makes, structurally.

    The badge is drawn from the `wired` flag, and the flag is a hand-
    maintained boolean -- exactly the kind of thing that goes stale. So
    this does not hardcode a list: it asserts that the set of switches
    the page presents as working is the same set market_alert_service
    actually checks before it sends. Wire up a sender and forget the
    flag, or flip the flag without a sender, and this fails.
    """
    from app.controllers.profile_controller import NOTIFICATION_PREFS
    from app.services.market_alert_service import _PREF_DEFAULTS

    wired = {field for field, _l, _b, is_wired in NOTIFICATION_PREFS if is_wired}
    assert wired == set(_PREF_DEFAULTS)

    unwired = {field for field, _l, _b, is_wired in NOTIFICATION_PREFS if not is_wired}
    assert unwired == {"notify_newsletter"}, \
        "a newly unwired preference needs its own 'not sending yet' badge"


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
# 6. Business Preferences is gone -- the plans are managed on Home
# ---------------------------------------------------------------------
# The plans first lived in a "My Plans" modal in shared/_topbar.html
# (on every authenticated page), then in a Settings pane called Business
# Preferences. They are now managed on the Home page, next to the plan
# itself: a pencil edits, a bin moves the plan to Trash, Trash restores.
# These pin down that the Settings side of that move is complete -- no
# pane, no tab, no plan forms or script left behind -- and that an old
# link still lands somewhere useful. The plan routes themselves (update,
# trash, restore, the legacy /delete alias) are tested in
# tests/test_plan_trash.py.

def _plan(user, name="Carinderia ni Nena", industry="Food and Beverage",
          location="San Nicolas", stage="startup"):
    profile = SmeProfile(
        user_id=user.user_id, business_name=name, industry_type=industry,
        location=location, business_stage=stage, startup_capital=250000,
        employee_count=4,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def test_the_my_plans_popup_is_gone_from_every_page(app):
    """If the modal were still being shipped, the page weight it cost
    would still be paid and there would be two places to edit a plan."""
    with app.app_context():
        user = _user(app)
        _plan(user)
        client = _client(app)

        for path in ("/settings", "/planning", "/saturation-map", "/trend-reports"):
            body = client.get(path).get_data(as_text=True)
            assert 'id="myPlansModal"' not in body, f"the popup is still on {path}"
            assert "dss-myplans-btn" not in body, f"the top-bar button is still on {path}"


@pytest.mark.parametrize("role,email", [("SME", "s@x.com"), ("LGU", "l@x.com"), ("Admin", "a@x.com")])
def test_no_role_gets_a_business_preferences_pane(app, role, email):
    """Not for an LGU or Admin (who never had one) and, now, not for an
    SME either: there is no plans tab, no plans pane, and no leftover
    plan list in Settings at all."""
    with app.app_context():
        user = _user(app, email=email, role=role)
        if role == "SME":
            _plan(user)
        body = _client(app, email=email).get("/settings").get_data(as_text=True)
        assert "Business Preferences" not in body
        assert 'id="section-plans"' not in body
        assert 'data-section="plans"' not in body
        assert "Carinderia ni Nena" not in body, "a plan is still listed in Settings"


def test_settings_ships_no_plan_editing_forms_or_script(app):
    """The pane took its forms, plan_form.js and the inline edit/delete
    script with it. Leaving any of that behind would be dead weight on
    every Settings load -- and a second, stale way to change a plan."""
    with app.app_context():
        user = _user(app)
        profile = _plan(user)
        body = _client(app).get("/settings").get_data(as_text=True)
        assert "js/plan_form.js" not in body
        assert "data-plan-form" not in body
        assert "dss-plan-edit-btn" not in body and "dss-plan-delete-btn" not in body
        assert f"/home/plans/{profile.sme_id}/" not in body
        assert "/home/plans/" not in body


def test_the_profile_pane_still_counts_saved_plans(app):
    """Only the list moved. The activity summary keeps its counts."""
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings?section=profile").get_data(as_text=True)
        assert "Saved Plans" in body
        assert "Forecasts Run" in body


def _profile_counts(client):
    """The two activity numbers on the Profile pane, as rendered."""
    import re

    body = client.get("/settings?section=profile").get_data(as_text=True)
    counts = {}
    for label in ("Saved Plans", "Forecasts Run"):
        match = re.search(rf"{label}</span><strong>(\d+)</strong>", body)
        assert match, f"the {label} count is not on the Profile pane"
        counts[label] = int(match.group(1))
    return counts


def test_a_plan_in_trash_drops_out_of_both_profile_counts(app):
    """Trash hides a plan from every ordinary query -- the settings
    counts included. Trashing keeps the plan's forecast_result and
    plan_saves rows (that is what makes restore lossless), and neither
    table carries an archive stamp of its own, so a "Saved Plans" count
    that read plan_saves alone kept counting the trashed plan while
    "Forecasts Run", which joins to the plan, dropped it: the two
    numbers side by side disagreed. Restoring brings both back."""
    from app.models import ForecastResult, PlanSave
    from app.services.forecasting_service import find_or_create_lgu_data

    with app.app_context():
        user = _user(app)
        profile = _plan(user)
        market = MarketData(
            industry_type="Food and Beverage", location="San Nicolas", competitor_count=12,
            population_density=8000, historical_success_rate=0.5, foot_traffic_index=50,
            average_rent=15000, source="Google Places API", date_recorded=date.today(),
        )
        db.session.add(market)
        db.session.commit()
        lgu = find_or_create_lgu_data("San Nicolas")
        forecast = ForecastResult(
            sme_id=profile.sme_id, market_id=market.market_id, lgu_id=lgu.lgu_id,
            input_industry_type="Food and Beverage", input_location="San Nicolas",
            saturation_index=40.0, viability_score=6.0, forecast_date=date.today(),
        )
        db.session.add(forecast)
        db.session.commit()
        db.session.add(PlanSave(user_id=user.user_id, forecast_result_id=forecast.forecast_id))
        db.session.commit()
        sme_id = profile.sme_id

        client = _client(app)
        assert _profile_counts(client) == {"Saved Plans": 1, "Forecasts Run": 1}

        assert client.post(f"/home/plans/{sme_id}/trash").status_code == 302
        assert _profile_counts(client) == {"Saved Plans": 0, "Forecasts Run": 0}
        # Kept, not deleted: the bookmark row is still there to come back.
        assert PlanSave.query.filter_by(user_id=user.user_id).count() == 1

        assert client.post(f"/home/plans/{sme_id}/restore").status_code == 302
        assert _profile_counts(client) == {"Saved Plans": 1, "Forecasts Run": 1}


def test_a_stale_plans_link_sends_an_sme_to_home(app):
    """?section=plans was the top-bar shortcut and is in bookmarks. For an
    SME it now goes where the plans are."""
    with app.app_context():
        _user(app)
        response = _client(app).get("/settings?section=plans")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/planning")


def test_a_stale_plans_link_does_not_blank_the_page_for_an_lgu(app):
    """?section=plans is a link an SME could paste to a colleague. For an
    account with no such pane it has to fall back to Profile -- if it
    didn't, every pane would be hidden and no tab selected, and the page
    would render empty."""
    with app.app_context():
        _user(app, email="lgu2@example.com", role="LGU")
        response = _client(app, email="lgu2@example.com").get("/settings?section=plans")
        assert response.status_code == 200
        assert _active_pane(response.get_data(as_text=True)) == "profile"


def test_the_user_menu_no_longer_links_to_business_preferences(app):
    """The top-bar shortcut pointed at a pane that no longer exists."""
    with app.app_context():
        _user(app)
        body = _client(app).get("/settings").get_data(as_text=True)
        assert "section=plans" not in body
