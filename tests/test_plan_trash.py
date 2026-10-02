"""
tests/test_plan_trash.py
--------------------------
Plans are managed on the Home page now, and removing one never deletes
it. What has to hold:

  1. Trash is an ARCHIVE. A trashed plan disappears from every ordinary
     query -- Home, Recommendations, the alert sweeps -- because
     SmeProfile carries HiddenWhenArchived, yet the row (and every
     forecast built on it) is still there, and Restore brings it all
     back. The legacy /delete URL trashes too; nothing hard-deletes.
  2. Ownership is checked on every plan route, INCLUDING for plans in
     Trash, so a trashed plan is never a way around the check (IDOR).
  3. The routes answer both callers: JSON for the page's fetch() (asked
     for with Accept: application/json), flash + redirect for a plain
     form post with scripting off.
  4. Capital is required, must be > 0, is called "capital" (the legacy
     field name startup_capital is still accepted), and is labelled
     "Capital" on every form.
  5. The Home markup: icon-only Edit / Move to Trash / Trash / Restore
     buttons that still have words for a screen reader, and no trace of
     Settings > Business Preferences.
"""

import json
import re
from datetime import date

import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from app.extensions import db
from app.models import AuditLog, ForecastResult, SmeProfile, SystemSetting, User
from app.models.archive import get_including_archived
from app.services.plan_params import apply_plan_data, parse_plan_form

FOOD = "Food and Beverage"
RETAIL = "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"
JSON = {"Accept": "application/json"}


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email="owner@trash.test"):
    user = User(name="Owner", email=email, role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _plan(user, name, industry=FOOD, location="Tibag", capital=250000, **extra):
    profile = SmeProfile(user_id=user.user_id, business_name=name, industry_type=industry,
                         location=location, business_stage=extra.pop("business_stage", "startup"),
                         startup_capital=capital, **extra)
    db.session.add(profile)
    db.session.commit()
    return profile


def _client(app, email="owner@trash.test"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
    return client


def _row(sme_id):
    """The row as the database holds it, archived or not. Expunges first
    so the identity map cannot hand back a stale object."""
    db.session.expire_all()
    return get_including_archived(SmeProfile, sme_id)


def _live_ids():
    db.session.expire_all()
    return {p.sme_id for p in SmeProfile.query.all()}


def _edit_form(**fields):
    base = {"business_name": "Tibag Pandesal", "industry_type": FOOD, "location": "Tibag", "capital": "250000"}
    base.update(fields)
    return base


def _section(page, element_id):
    """The markup of the modal with this id, up to the next modal."""
    start = page.index(f'id="{element_id}"')
    nxt = page.find('<div class="modal fade"', start + 1)
    return page[start: nxt if nxt != -1 else len(page)]


@pytest.fixture
def two_plans(app):
    with app.app_context():
        user = _user()
        keep = _plan(user, "Balibago Gulong", industry=RETAIL, location="Balibago I")
        bin_ = _plan(user, "Tibag Pandesal")
        return {"keep": keep.sme_id, "bin": bin_.sme_id}


# ---------------------------------------------------------------------
# 1. Trash is an archive
# ---------------------------------------------------------------------

def test_trash_hides_the_plan_from_home_but_keeps_the_row(app, two_plans):
    with app.app_context():
        client = _client(app)
        response = client.post(f"/home/plans/{two_plans['bin']}/trash")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/home")

        row = _row(two_plans["bin"])
        assert row is not None, "trash must never delete the row"
        assert row.archived_at is not None and row.is_archived
        assert two_plans["bin"] not in _live_ids()

        page = client.get("/home").get_data(as_text=True)
        assert f'href="/home?plan={two_plans["bin"]}"' not in page
        assert f'href="/home?plan={two_plans["keep"]}"' in page
        # ...but it is listed in the Trash dialog.
        trash = _section(page, "planTrashModal")
        assert "Tibag Pandesal" in trash
        assert "Moved to Trash on" in trash
        assert f'action="/home/plans/{two_plans["bin"]}/restore"' in trash


def test_trash_flashes_the_plans_name(app, two_plans):
    with app.app_context():
        page = _client(app).post(f"/home/plans/{two_plans['bin']}/trash",
                                 follow_redirects=True).get_data(as_text=True)
        assert "“Tibag Pandesal” moved to Trash." in page


def test_a_trashed_plan_is_off_the_recommendations_page(app, two_plans):
    with app.app_context():
        client = _client(app)
        before = client.get("/recommendations").get_data(as_text=True)
        assert "Tibag Pandesal" in before
        # Followed, so the "moved to Trash" flash is shown (and used up) on Home.
        client.post(f"/home/plans/{two_plans['bin']}/trash", follow_redirects=True)
        after = client.get("/recommendations").get_data(as_text=True)
        assert "Tibag Pandesal" not in after
        assert "Balibago Gulong" in after


def test_a_trashed_plan_gets_no_market_alerts(app, two_plans):
    from app.services.market_alert_service import _sme_recipients, _sme_recipients_by_industry

    with app.app_context():
        owner = User.query.filter_by(email="owner@trash.test").one().user_id
        move = {"industry_type": FOOD, "location": "Tibag"}
        assert owner in _sme_recipients([move])
        assert owner in _sme_recipients_by_industry([move])

        _client(app).post(f"/home/plans/{two_plans['bin']}/trash")
        db.session.expire_all()
        assert owner not in _sme_recipients([move])
        assert owner not in _sme_recipients_by_industry([move])


def test_restore_brings_back_the_plan_and_its_forecasts(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")      # forecasts it
        forecasts = ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count()
        assert forecasts >= 1

        client.post(f"/home/plans/{two_plans['bin']}/trash")
        assert ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count() == forecasts

        response = client.post(f"/home/plans/{two_plans['bin']}/restore")
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/home?plan={two_plans['bin']}")
        assert _row(two_plans["bin"]).archived_at is None
        assert two_plans["bin"] in _live_ids()
        assert ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count() >= forecasts

        page = client.get(response.headers["Location"]).get_data(as_text=True)
        assert f'href="/home?plan={two_plans["bin"]}"' in page
        assert "Trash is empty." in _section(page, "planTrashModal")


def test_restore_flashes_and_lands_on_the_plan(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        page = client.post(f"/home/plans/{two_plans['bin']}/restore",
                           follow_redirects=True).get_data(as_text=True)
        assert "“Tibag Pandesal” restored." in page
        assert f"{FOOD} in Tibag" in page


def test_the_legacy_delete_url_only_trashes(app, two_plans):
    """It used to hard-delete, cascading through every forecast. Old
    clients may still call it; it must now do exactly what Trash does."""
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        forecasts = ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count()

        response = client.post(f"/home/plans/{two_plans['bin']}/delete", headers=JSON)
        assert response.status_code == 200
        assert response.get_json()["success"] is True

        rows = (SmeProfile.query.execution_options(include_archived=True)
                .filter_by(sme_id=two_plans["bin"]).all())
        assert len(rows) == 1 and rows[0].archived_at is not None
        assert two_plans["bin"] not in _live_ids()
        assert ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count() == forecasts
        assert AuditLog.query.filter_by(action="trash_plan").count() == 1


def test_trashing_twice_changes_nothing_the_second_time(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        first = _row(two_plans["bin"]).archived_at
        response = client.post(f"/home/plans/{two_plans['bin']}/trash", headers=JSON)
        assert response.status_code == 200
        assert _row(two_plans["bin"]).archived_at == first
        assert AuditLog.query.filter_by(action="trash_plan").count() == 1


def test_trash_and_restore_are_audited_with_their_target(app, two_plans):
    from app.utils.audit_labels import label_for

    with app.app_context():
        client = _client(app)
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        client.post(f"/home/plans/{two_plans['bin']}/restore")
        for action in ("trash_plan", "restore_plan"):
            entry = AuditLog.query.filter_by(action=action).one()
            assert entry.target_type == "SmeProfile"
            assert entry.target_id == str(two_plans["bin"])
            assert "Tibag Pandesal" in (entry.target_label or "")
        assert label_for("trash_plan") == "Moved a business plan to Trash"
        assert label_for("restore_plan") == "Restored a business plan from Trash"


def test_trashing_the_selected_plan_clears_the_selection(app, two_plans):
    from app.controllers.sme_controller import HOME_PLAN_SESSION_KEY

    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        with client.session_transaction() as sess:
            assert sess[HOME_PLAN_SESSION_KEY] == two_plans["bin"]

        client.post(f"/home/plans/{two_plans['bin']}/trash")
        with client.session_transaction() as sess:
            assert HOME_PLAN_SESSION_KEY not in sess

        page = client.get("/home").get_data(as_text=True)
        assert f"{RETAIL} in Balibago I" in page


def test_a_trashed_plan_cannot_be_chosen_by_url(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        page = client.get(f"/home?plan={two_plans['bin']}").get_data(as_text=True)
        assert f"{FOOD} in Tibag" not in page
        assert f"{RETAIL} in Balibago I" in page


def test_a_historical_forecast_still_resolves_its_trashed_plan(app, two_plans):
    """Relationship loads are not filtered (app/models/archive.py): a
    forecast built on a plan now in Trash must still name its plan."""
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        forecast_id = ForecastResult.query.filter_by(sme_id=two_plans["bin"]).first().forecast_id
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        db.session.expunge_all()
        forecast = db.session.get(ForecastResult, forecast_id)
        assert forecast.sme_profile is not None
        assert forecast.sme_profile.business_name == "Tibag Pandesal"


def test_editing_a_trashed_plan_is_refused_until_it_is_restored(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        response = client.post(f"/home/plans/{two_plans['bin']}/update",
                               data=_edit_form(business_name="Sneaky"), headers=JSON)
        assert response.status_code == 404
        assert response.get_json()["success"] is False
        assert _row(two_plans["bin"]).business_name == "Tibag Pandesal"


def test_a_missing_plan_is_404(app, two_plans):
    with app.app_context():
        client = _client(app)
        for verb in ("update", "trash", "restore", "delete"):
            assert client.post(f"/home/plans/99999/{verb}", data=_edit_form()).status_code == 404, verb


def test_industry_migration_remaps_trashed_plans_too(app):
    from app.services import industry_migration

    with app.app_context():
        user = _user()
        old = _plan(user, "Old Sari-Sari", industry="Retail")
        old.archive(user.user_id, "trash")
        db.session.commit()
        summary = industry_migration.migrate(force=True)
        assert summary["profiles_remapped"] == 1
        assert _row(old.sme_id).industry_type == RETAIL


def test_the_startup_migration_adds_the_trash_columns():
    from app.services.startup_migrations import _ADDITIVE_COLUMNS

    for column in ("archived_at", "archived_by", "archive_reason"):
        assert column in _ADDITIVE_COLUMNS["sme_profile"]


# ---------------------------------------------------------------------
# 2. Ownership, including plans in Trash (no IDOR)
# ---------------------------------------------------------------------

@pytest.fixture
def strangers_plans(app):
    with app.app_context():
        _user()                                   # the signed-in owner
        stranger = _user("stranger@trash.test")
        live = _plan(stranger, "Not Yours")
        trashed = _plan(stranger, "Not Yours Either")
        trashed.archive(stranger.user_id, "trash")
        db.session.commit()
        return {"live": live.sme_id, "trashed": trashed.sme_id}


@pytest.mark.parametrize("which", ["live", "trashed"])
@pytest.mark.parametrize("verb", ["update", "trash", "restore", "delete"])
@pytest.mark.parametrize("headers", [{}, JSON])
def test_someone_elses_plan_is_refused(app, strangers_plans, which, verb, headers):
    with app.app_context():
        sme_id = strangers_plans[which]
        before = _row(sme_id)
        name, archived = before.business_name, before.archived_at

        response = _client(app).post(f"/home/plans/{sme_id}/{verb}",
                                     data=_edit_form(business_name="Hijacked"), headers=headers)
        assert response.status_code in (403, 404), (verb, which)
        if headers:
            assert response.get_json()["success"] is False

        after = _row(sme_id)
        assert after.business_name == name
        assert after.archived_at == archived


def test_someone_elses_trash_is_not_listed(app, strangers_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
        assert "Not Yours Either" not in page
        assert "Not Yours" not in page
        assert "Trash is empty." in _section(page, "planTrashModal")


# ---------------------------------------------------------------------
# 3. JSON for the page's script, redirects for a plain form post
# ---------------------------------------------------------------------

def test_update_answers_json_to_fetch(app, two_plans):
    with app.app_context():
        response = _client(app).post(f"/home/plans/{two_plans['bin']}/update", headers=JSON, data=_edit_form(
            business_name="Renamed Pandesal", capital="750,000", employee_count="2",
            offering_item=["Pandesal"], offering_price=["5"],
        ))
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["success"] is True
        assert payload["plan"]["capital"] == 750000.0
        assert payload["plan"]["startup_capital"] == 750000.0
        assert payload["redirect"].endswith(f"/home?plan={two_plans['bin']}")

        saved = _row(two_plans["bin"])
        assert saved.business_name == "Renamed Pandesal"
        assert saved.capital == 750000.0
        assert saved.employee_count == 2
        assert saved.offering_items == [{"item": "Pandesal", "price": 5.0}]


def test_update_redirects_a_plain_form_post(app, two_plans):
    with app.app_context():
        client = _client(app)
        response = client.post(f"/home/plans/{two_plans['bin']}/update",
                               data=_edit_form(business_name="Plain Post Pandesal"))
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/home?plan={two_plans['bin']}")
        page = client.get(response.headers["Location"]).get_data(as_text=True)
        assert "“Plain Post Pandesal” updated" in page
        # A browser's ordinary Accept header prefers HTML: still a redirect.
        browser = client.post(f"/home/plans/{two_plans['bin']}/update", data=_edit_form(),
                              headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        assert browser.status_code == 302


def test_x_requested_with_also_gets_json(app, two_plans):
    with app.app_context():
        response = _client(app).post(f"/home/plans/{two_plans['bin']}/trash",
                                     headers={"X-Requested-With": "XMLHttpRequest"})
        assert response.status_code == 200
        assert response.get_json()["success"] is True


def test_edit_validation_errors_come_back_as_json_text(app, two_plans):
    with app.app_context():
        client = _client(app)
        response = client.post(f"/home/plans/{two_plans['bin']}/update", headers=JSON, data=_edit_form(
            capital="", offering_item=["<img src=x onerror=alert(1)>"], offering_price=["abc"],
        ))
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["success"] is False
        assert "Capital is required." in payload["error"]
        assert "must be a number" in payload["error"]
        assert _row(two_plans["bin"]).business_name == "Tibag Pandesal"
        # The page script writes the message as text, never as markup.
        with open("app/static/js/plan_manage.js", encoding="utf-8") as handle:
            script = handle.read()
        assert "box.textContent = message" in script
        assert "Accept: \"application/json\"" in script


def test_edit_validation_errors_are_flashed_without_js(app, two_plans):
    with app.app_context():
        client = _client(app)
        response = client.post(f"/home/plans/{two_plans['bin']}/update", data=_edit_form(capital="0"))
        assert response.status_code == 302
        page = client.get(response.headers["Location"]).get_data(as_text=True)
        assert "Capital must be greater than zero." in page
        assert _row(two_plans["bin"]).capital == 250000.0


def test_editing_keeps_a_registration_date_the_form_did_not_send(app):
    with app.app_context():
        user = _user()
        profile = _plan(user, "Old Shop", business_stage="existing", registration_date=date(2019, 5, 1))
        _client(app).post(f"/home/plans/{profile.sme_id}/update", headers=JSON,
                          data=_edit_form(business_name="Old Shop", business_stage="existing"))
        assert _row(profile.sme_id).registration_date == date(2019, 5, 1)


def test_editing_keeps_a_legacy_industry_name(app):
    with app.app_context():
        user = _user()
        profile = _plan(user, "Legacy", industry="Sari-sari Store (legacy)")
        response = _client(app).post(f"/home/plans/{profile.sme_id}/update", headers=JSON,
                                     data=_edit_form(business_name="Legacy", industry_type="Sari-sari Store (legacy)"))
        assert response.get_json()["success"] is True


# ---------------------------------------------------------------------
# 4. Capital
# ---------------------------------------------------------------------

def _form(**fields):
    base = {"business_name": "Tibag Pandesal", "industry_type": FOOD, "location": "Tibag", "capital": "500000"}
    base.update(fields)
    md = MultiDict()
    for key, value in base.items():
        if value is None:
            continue
        for item in (value if isinstance(value, list) else [value]):
            md.add(key, item)
    return md


def test_capital_is_parsed_under_both_keys():
    data, errors = parse_plan_form(_form(capital="1,250,000.50"))
    assert errors == []
    assert data["capital"] == data["startup_capital"] == 1250000.5
    json.dumps(data)


@pytest.mark.parametrize("raw,message", [
    (None, "Capital is required."),
    ("", "Capital is required."),
    ("   ", "Capital is required."),
    ("lots", "Capital must be a number."),
    ("nan", "Capital must be a number."),
    ("inf", "Capital must be a number."),
    ("1e400", "Capital must be a number."),
    ("0", "Capital must be greater than zero."),
    ("-5000", "Capital must be greater than zero."),
    # Under half a centavo: > 0 as typed, but stored as 0.00. The check
    # runs on the rounded value -- what is checked is what is stored.
    ("0.001", "Capital must be greater than zero."),
    ("0.004", "Capital must be greater than zero."),
])
def test_capital_is_required_and_positive(raw, message):
    data, errors = parse_plan_form(_form(capital=raw))
    assert message in errors
    assert data["capital"] is None


def test_a_sub_centavo_capital_never_reaches_the_column(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.post("/home/analyze", data={"business_name": "Half a Centavo", "industry_type": FOOD,
                                           "location": "Tibag", "capital": "0.001"})
        assert SmeProfile.query.filter_by(business_name="Half a Centavo").count() == 0
        response = client.post(f"/home/plans/{two_plans['bin']}/update", headers=JSON,
                               data=_edit_form(capital="0.004"))
        assert response.status_code == 400
        assert _row(two_plans["bin"]).capital == 250000.0


def test_the_legacy_field_name_is_still_accepted():
    data, errors = parse_plan_form(_form(capital=None, startup_capital="300000"))
    assert errors == []
    assert data["capital"] == 300000.0
    # When both are sent, the current name wins.
    data, _ = parse_plan_form(_form(capital="400000", startup_capital="1"))
    assert data["capital"] == 400000.0


def test_apply_plan_data_writes_the_capital_column(app):
    with app.app_context():
        profile = SmeProfile(user_id=1, business_name="x", industry_type=FOOD, location="Tibag")
        data, _ = parse_plan_form(_form(capital="650000"))
        apply_plan_data(profile, data)
        assert float(profile.startup_capital) == 650000.0
        assert profile.capital == 650000.0
        assert profile.to_dict()["capital"] == 650000.0


def test_a_legacy_plan_without_capital_reads_zero_and_is_flagged(app):
    with app.app_context():
        user = _user()
        legacy = _plan(user, "No Capital Yet", capital=None)
        assert legacy.capital == 0.0 and legacy.capital_missing
        page = _client(app).get(f"/home?plan={legacy.sme_id}").get_data(as_text=True)
        assert "Capital missing" in page
        assert "Add your capital to get an accurate forecast." in page
        assert f'data-bs-target="#editPlanModal-{legacy.sme_id}"' in page


def test_a_new_plan_without_capital_is_refused(app, two_plans):
    with app.app_context():
        client = _client(app)
        page = client.post("/home/analyze", data={"business_name": "No Money", "industry_type": FOOD,
                                                  "location": "Tibag"},
                           follow_redirects=True).get_data(as_text=True)
        assert "Capital is required." in page
        assert SmeProfile.query.filter_by(business_name="No Money").count() == 0


def test_every_plan_form_on_home_asks_for_a_required_capital(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    capital_inputs = re.findall(r'<input type="number" id="[^"]+-capital" name="capital"[^>]*>', page)
    assert len(capital_inputs) == 3                     # Add + one Edit per plan
    for tag in capital_inputs:
        assert "required" in tag and 'min="1"' in tag
    assert page.count("Capital (&#8369;)") == 3 or page.count("Capital (₱)") == 3
    assert "Startup capital" not in page
    assert 'name="startup_capital"' not in page


def test_recommendations_say_capital_and_break_even(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        page = client.get("/recommendations").get_data(as_text=True)
        assert "Startup Capital" not in page
        assert "> Capital</div>" in page
        assert "Break-even" in page
        assert "₱250,000" in page


# ---------------------------------------------------------------------
# 5. The Home markup
# ---------------------------------------------------------------------

def _visible_text(html):
    html = re.sub(r"<span class=\"visually-hidden\">.*?</span>", "", html, flags=re.S)
    return re.sub(r"<[^>]+>", "", html).strip()


def test_plan_chips_have_icon_only_edit_and_trash_buttons(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)

    actions = re.findall(r'<div class="dss-plan-chip-actions">(.*?)</form>\s*</div>', page, flags=re.S)
    assert len(actions) == 2
    for block in actions:
        assert _visible_text(block) == "", "the edit/trash buttons must be icon only"
        assert 'class="bi bi-pencil"' in block and 'class="bi bi-trash"' in block

    assert 'aria-label="Edit plan “Tibag Pandesal”"' in page
    assert 'title="Edit plan “Tibag Pandesal”"' in page
    assert 'aria-label="Move “Tibag Pandesal” to Trash"' in page
    assert f'action="/home/plans/{two_plans["bin"]}/trash"' in page
    assert f'action="/home/plans/{two_plans["bin"]}/update"' in page
    # The selection link holds no button: nothing interactive is nested.
    for link in re.findall(r'<a href="/home\?plan=\d+" class="dss-plan-chip-link".*?</a>', page, flags=re.S):
        assert "<button" not in link and "<form" not in link


def test_each_plan_has_a_prefilled_edit_dialog(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    dialog = _section(page, f"editPlanModal-{two_plans['bin']}")
    assert 'value="Tibag Pandesal"' in dialog
    assert 'value="250000"' in dialog
    assert "data-plan-edit-form" in dialog and "data-plan-form" in dialog
    for name in ("business_name", "industry_type", "subcategory", "location", "business_stage",
                 "capital", "employee_count", "product_offering", "innovation_idea", "offering_item"):
        assert f'name="{name}"' in dialog, name
    assert "data-plan-form-error" in dialog
    assert "js/plan_manage.js" in page


def test_the_trash_button_is_icon_only_with_a_count(app, two_plans):
    with app.app_context():
        client = _client(app)
        page = client.get("/home").get_data(as_text=True)
        assert 'aria-label="Trash (0)"' in page
        assert 'title="Trash"' in page

        client.post(f"/home/plans/{two_plans['bin']}/trash")
        page = client.get("/home").get_data(as_text=True)
    button = re.search(r'<a href="#planTrashModal"[^>]*>(.*?)</a>', page, flags=re.S)
    assert button and 'aria-label="Trash (1)"' in button.group(0)
    assert 'class="bi bi-trash3"' in button.group(1)
    assert re.sub(r"<[^>]+>", "", button.group(1)).strip() == "1", "only the count badge, no label"


def test_restore_buttons_are_icon_only(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        trash = _section(client.get("/home").get_data(as_text=True), "planTrashModal")
    button = re.search(r'<button type="submit" class="dss-icon-btn dss-icon-btn-restore"[^>]*>(.*?)</button>',
                       trash, flags=re.S)
    assert button
    assert 'aria-label="Restore “Tibag Pandesal”"' in button.group(0)
    assert _visible_text(button.group(1)) == ""
    assert "Plans in Trash are kept, not deleted." in trash


def test_the_trash_button_shows_when_every_plan_is_in_trash(app):
    with app.app_context():
        user = _user()
        only = _plan(user, "Only Plan")
        client = _client(app)
        client.post(f"/home/plans/{only.sme_id}/trash")
        page = client.get("/home").get_data(as_text=True)
    assert 'aria-label="Trash (1)"' in page
    assert "in Trash" in page and "Only Plan" in _section(page, "planTrashModal")


def _chip(page, sme_id):
    start = page.index(f'data-plan-chip="{sme_id}"')
    return page[start: page.index('<div class="dss-plan-chip-actions">', start)]


def _strip_payload(forecast):
    """Make a stored row look like one written before the plan model."""
    stored = json.loads(forecast.recommendation)
    stored.pop("forecast", None)
    stored.pop("explanation", None)
    forecast.recommendation = json.dumps(stored)


def test_a_chip_scored_before_the_plan_model_says_market_score(app, two_plans):
    """Only the plan on screen is regenerated, so another plan's chip can
    still hold the old market-only figure. It must not sit beside plan
    scores under the same "Viability" name."""
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['keep']}")
        client.get(f"/home?plan={two_plans['bin']}")
        old = ForecastResult.query.filter_by(sme_id=two_plans["keep"]).first()
        _strip_payload(old)
        old.viability_score = 7.3
        db.session.commit()

        page = client.get(f"/home?plan={two_plans['bin']}").get_data(as_text=True)
        stale, fresh = _chip(page, two_plans["keep"]), _chip(page, two_plans["bin"])
        assert re.search(r"Market score 7\.3/10 &middot; open to update", stale)
        assert "Viability " not in stale
        assert re.search(r"Viability \d+\.\d/10", fresh) and "Market score" not in fresh

        # Opening it re-runs the forecast, and the chip becomes a plan score.
        page = client.get(f"/home?plan={two_plans['keep']}").get_data(as_text=True)
        assert "Market score" not in _chip(page, two_plans["keep"])


@pytest.mark.parametrize("score,pill", [(8.2, "dss-pill-low"), (6.5, "dss-pill-low"),
                                        (5.0, "dss-pill-moderate"), (2.4, "dss-pill-saturated")])
def test_the_chip_pill_is_coloured_by_its_own_score_not_the_market_tier(app, two_plans, score, pill):
    """A plan with almost no capital in a quiet market: green "Low
    saturation" tier, low plan viability. The pill shows the viability,
    so it is coloured by it."""
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        row = ForecastResult.query.filter_by(sme_id=two_plans["bin"]).first()
        row.viability_score = score
        row.saturation_index = 10.0          # a "Low" saturation tier, whatever the score
        db.session.commit()
        page = client.get(f"/home?plan={two_plans['keep']}").get_data(as_text=True)
        chip = _chip(page, two_plans["bin"])
    assert f"dss-stat-pill {pill}" in chip
    assert f"Viability {score:.1f}/10" in chip


def test_the_trash_row_names_the_kind_of_score_it_shows(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        trash = _section(client.get("/home").get_data(as_text=True), "planTrashModal")
        assert re.search(r"last viability\s+\d+\.\d/10", trash)

        _strip_payload(ForecastResult.query.filter_by(sme_id=two_plans["bin"]).first())
        db.session.commit()
        trash = _section(client.get("/home").get_data(as_text=True), "planTrashModal")
        assert re.search(r"last market score\s+\d+\.\d/10", trash)
        assert "last viability" not in trash


def test_the_trash_date_is_shown_in_philippine_time(app, two_plans):
    """archived_at is stored in UTC. 23:30 UTC on 1 March is 07:30 on
    2 March in Tarlac -- the day the owner actually trashed it."""
    from datetime import datetime

    with app.app_context():
        client = _client(app)
        client.post(f"/home/plans/{two_plans['bin']}/trash")
        row = _row(two_plans["bin"])
        row.archived_at = datetime(2026, 3, 1, 23, 30)
        db.session.commit()
        trash = _section(client.get("/home").get_data(as_text=True), "planTrashModal")
    assert "Moved to Trash on March 02, 2026" in trash


def test_recommendations_name_a_pre_model_score_for_what_it_is(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        page = client.get("/recommendations").get_data(as_text=True)
        assert "Plan Viability" in page and "Open on Home to update" not in page

        _strip_payload(ForecastResult.query.filter_by(sme_id=two_plans["bin"]).first())
        db.session.commit()
        page = client.get("/recommendations").get_data(as_text=True)
        assert "Market Score" in page
        assert f'href="/home?plan={two_plans["bin"]}"' in page and "Open on Home to update" in page


def test_the_idea_help_text_no_longer_says_it_changes_nothing(app, two_plans):
    """Having an idea is the plan model's differentiation input, and that
    model's score is the one on the gauge and the chips."""
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert "never changes the market score" not in page
    assert "counts towards your plan's viability score" in page
    assert "never changes the market saturation figure" in page


def test_home_no_longer_points_to_business_preferences(app, two_plans):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert "Business Preferences" not in page
    assert "section=plans" not in page
    assert "Viability " in page and "Market score" not in page


def test_the_shared_form_macro_is_used_by_add_and_edit():
    with open("app/templates/sme/home.html", encoding="utf-8") as handle:
        home = handle.read()
    assert home.count("plan_core_fields(") == 2
    with open("app/templates/shared/_plan_fields.html", encoding="utf-8") as handle:
        fields = handle.read()
    assert "macro plan_core_fields" in fields
    assert "(current)" in fields


def test_the_chip_actions_are_styled_for_both_themes():
    with open("app/static/css/style.css", encoding="utf-8") as handle:
        css = handle.read()
    for selector in (".dss-icon-btn", ".dss-trash-count", ".dss-trash-item", ".dss-driver-bar.is-up",
                     ".dss-capital-warning"):
        assert re.search(r"(^|\n)" + re.escape(selector) + r"[\s{:,]", css), selector
        assert '[data-bs-theme="dark"] ' + selector in css, f"{selector} has no dark-theme rule"
    rule = re.search(r"\n\.dss-icon-btn \{([^}]+)\}", css).group(1)
    assert "width: 32px" in rule and "height: 32px" in rule


# ---------------------------------------------------------------------
# 6. The forecast panel reads the trained model's payload
# ---------------------------------------------------------------------

def test_an_old_forecast_without_the_model_payload_is_regenerated(app, two_plans):
    with app.app_context():
        client = _client(app)
        client.get(f"/home?plan={two_plans['bin']}")
        old = (ForecastResult.query.filter_by(sme_id=two_plans["bin"])
               .order_by(ForecastResult.forecast_id.desc()).first())
        stored = json.loads(old.recommendation)
        stored.pop("forecast", None)
        stored.pop("explanation", None)
        old.recommendation = json.dumps(stored)
        db.session.commit()
        before = ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count()

        client.get(f"/home?plan={two_plans['bin']}")
        assert ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count() == before + 1
        newest = (ForecastResult.query.filter_by(sme_id=two_plans["bin"])
                  .order_by(ForecastResult.forecast_id.desc()).first())
        assert json.loads(newest.recommendation).get("forecast"), "the regenerated row carries the payload"

        # ...and a fresh one is NOT regenerated on every visit.
        client.get(f"/home?plan={two_plans['bin']}")
        assert ForecastResult.query.filter_by(sme_id=two_plans["bin"]).count() == before + 1


def test_the_forecast_panel_shows_the_plan_model_breakdown(app, two_plans):
    with app.app_context():
        page = _client(app).get(f"/home?plan={two_plans['bin']}").get_data(as_text=True)
    assert "Plan viability" in page
    assert "Break-even:" in page
    assert "Capital runway:" in page
    assert "How this forecast was computed" in page
    assert 'class="dss-driver"' in page
    assert re.search(r"(Explained by Gemini|AI-written|Rule-based explanation)", page)
