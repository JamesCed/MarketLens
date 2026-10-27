"""
tests/test_it_expert_revisions.py
-----------------------------------
The October 2026 IT expert review, item by item, plus the page-speed work
done alongside it. Each test names the comment it answers.
"""

import gzip
import re

import pytest
from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import LguData, SmeProfile, SystemSetting, User
from app.models.audit_log import AuditLog
from tests.account_fixtures import LGU_REQUEST, PERSONAL, STRONG_PASSWORD, account_fields


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role="SME", password=STRONG_PASSWORD, **fields):
    user = User(name=fields.pop("name", role.title()), email=email, role=role, **fields)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, email, password=STRONG_PASSWORD):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": password})
    return client


SME_PLAN = {
    "role": "sme", "business_name": "Kape Tarlac", "industry_type": "Food and Beverage",
    "location": "Poblacion", "business_stage": "startup", "capital": "250000",
}


@pytest.mark.parametrize("weak", ["password123", "PASSWORD123!", "Password!!", "Pass1!", "Password123"])
def test_a_weak_password_is_refused_at_sign_up(app, weak):
    client = app.test_client()
    client.post("/register", data={**SME_PLAN, **account_fields("weak@x.test", password=weak)})
    assert User.query.filter_by(email="weak@x.test").first() is None


def test_the_password_rule_lists_exactly_what_is_missing():
    from app.services.account_params import password_problems

    assert password_problems(STRONG_PASSWORD) == []
    message = password_problems("password")[0]
    assert "uppercase" in message and "number" in message and "special" in message
    assert "lowercase" not in message


def test_changing_the_password_with_a_mistake_answers_in_place_without_a_reload(app):
    _user("pw@x.test")
    client = _login(app, "pw@x.test")
    response = client.post("/settings", data={
        "form_type": "password", "current_password": "wrong-one", "new_password": "weak", "confirm_password": "weak",
    }, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    assert response.status_code == 400
    body = response.get_json()
    assert body["success"] is False
    assert set(body["errors"]) >= {"current_password", "new_password"}
    assert User.query.filter_by(email="pw@x.test").one().check_password(STRONG_PASSWORD)


def test_a_valid_password_change_succeeds_in_place(app):
    _user("pw2@x.test")
    client = _login(app, "pw2@x.test")
    response = client.post("/settings", data={
        "form_type": "password", "current_password": STRONG_PASSWORD,
        "new_password": "Brand#New2026", "confirm_password": "Brand#New2026",
    }, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    assert response.status_code == 200 and response.get_json()["success"] is True
    db.session.expire_all()
    assert User.query.filter_by(email="pw2@x.test").one().check_password("Brand#New2026")


def test_the_settings_page_shows_the_password_checklist(app):
    _user("pw3@x.test")
    page = _login(app, "pw3@x.test").get("/settings").get_data(as_text=True)
    assert 'id="passwordForm"' in page and 'data-pw-rules-for="set-new-pw"' in page
    assert "One special character" in page


def test_an_admin_cannot_create_an_account_with_a_weak_password(app):
    _user("admin@x.test", role="Admin")
    client = _login(app, "admin@x.test")
    client.post("/admin/users/create", data={"first_name": "Ana", "last_name": "Reyes", "email": "ana@x.test",
                                             "password": "password123", "role": "LGU"})
    assert User.query.filter_by(email="ana@x.test").first() is None
    client.post("/admin/users/create", data={"first_name": "Ana", "last_name": "Reyes", "email": "ana@x.test",
                                             "password": STRONG_PASSWORD, "role": "LGU"})
    created = User.query.filter_by(email="ana@x.test").one()
    assert created.name == "Ana Reyes" and created.first_name == "Ana"


def test_sign_up_stores_the_full_personal_information(app):
    app.test_client().post("/register", data={**SME_PLAN, **account_fields("full@x.test", suffix="Jr.")})
    user = User.query.filter_by(email="full@x.test").one()
    assert (user.first_name, user.middle_name, user.last_name, user.suffix) == ("Juan", "Santos", "Dela Cruz", "Jr.")
    assert user.name == "Juan Santos Dela Cruz Jr."
    assert user.gender == "Male" and user.birthdate.isoformat() == "1990-05-14"
    assert user.age >= 36
    assert user.contact_number == "09171234567"
    assert user.address_display == "123 Rizal St., Poblacion, Tarlac City"
    assert user.terms_accepted_at is not None
    assert user.personal_info_complete


def test_personal_details_are_encrypted_in_the_database(app):
    app.test_client().post("/register", data={**SME_PLAN, **account_fields("enc@x.test")})
    row = db.session.execute(text(
        "SELECT contact_number_enc, gender_enc, birthdate_enc, address_line_enc, address_barangay_enc, contact_number "
        "FROM user WHERE email = 'enc@x.test'"
    )).one()
    stored = " ".join(str(v) for v in row[:5])
    for plain in ("09171234567", "Male", "1990-05-14", "Rizal", "Poblacion"):
        assert plain not in stored, f"{plain} is stored in plain text"
    assert all(str(v).startswith("enc1:") for v in row[:5])
    assert row[5] is None


def test_details_written_under_the_secret_key_still_read_after_a_dedicated_key_is_added(app):
    user = _user("rotate@x.test", contact_number="09179998888")
    app.config["PII_ENCRYPTION_KEY"] = "a-brand-new-dedicated-key-for-personal-details"
    db.session.expire_all()
    assert User.query.filter_by(email="rotate@x.test").one().contact_number == "09179998888"
    assert user.user_id


@pytest.mark.parametrize("field,value,message", [
    ("first_name", "", "First name is required"),
    ("last_name", "", "Last name is required"),
    ("gender", "", "Choose a gender"),
    ("birthdate", "2015-01-01", "at least 18"),
    ("contact_number", "12345", "Philippine mobile number"),
    ("address_line", "", "House number and street"),
    ("address_barangay", "Makati", "Choose your barangay"),
    ("accept_terms", None, "Terms and Conditions"),
])
def test_sign_up_refuses_missing_or_invalid_personal_details(app, field, value, message):
    data = {**SME_PLAN, **account_fields("bad@x.test")}
    if value is None:
        data.pop(field)
    else:
        data[field] = value
    page = app.test_client().post("/register", data=data).get_data(as_text=True)
    assert User.query.filter_by(email="bad@x.test").first() is None
    assert message in page


def test_an_lgu_registration_asks_only_for_names(app):
    """LGU staff register on /register/lgu: name, work e-mail and office
    details -- no gender, birthday, contact number or address (only what
    the purpose needs)."""
    app.test_client().post("/register/lgu", data={**LGU_REQUEST,
                                                  **account_fields("lgu@x.test", first_name="Maria")})
    user = User.query.filter_by(email="lgu@x.test").one()
    assert user.role == "LGU" and user.first_name == "Maria" and user.last_name == "Dela Cruz"
    assert user.gender is None and user.contact_number is None and user.address_line is None
    app.test_client().post("/register", data={"role": "lgu", "email": "lgu2@x.test",
                                              "password": STRONG_PASSWORD, "confirm_password": STRONG_PASSWORD,
                                              "accept_terms": "1"})
    assert User.query.filter_by(email="lgu2@x.test").first() is None


def test_the_sign_up_form_has_the_new_steps_and_dropdowns(app):
    page = app.test_client().get("/register").get_data(as_text=True)
    assert 'data-step="4"' in page and "About you" in page
    assert '<select id="ml-location" name="location"' in page
    assert '<select id="ml-addr-brgy" name="address_barangay"' in page
    assert page.count('<option value="Poblacion"') >= 2
    assert 'name="accept_terms"' in page and 'id="termsModal"' in page
    assert 'data-pw-rules-for="ml-pw"' in page
    assert f'min="5000"' in page


def test_the_terms_page_is_public_and_names_the_data_privacy_act(app):
    page = app.test_client().get("/terms").get_data(as_text=True)
    assert "Data Privacy Act of 2012" in page and "10173" in page
    assert "encrypted" in page


def test_settings_lets_an_older_account_complete_its_details(app):
    _user("old@x.test", name="Old Account")
    client = _login(app, "old@x.test")
    page = client.get("/settings").get_data(as_text=True)
    assert "Please complete your personal information" in page
    client.post("/settings", data={"form_type": "profile", **{k: v for k, v in PERSONAL.items() if k != "accept_terms"}})
    db.session.expire_all()
    user = User.query.filter_by(email="old@x.test").one()
    assert user.personal_info_complete and user.name == "Juan Santos Dela Cruz"
    entry = AuditLog.query.filter_by(action="update_profile").order_by(AuditLog.id.desc()).first()
    assert entry is not None and "09171234567" not in (entry.changes or "")
    assert "Rizal" not in (entry.changes or "")


def test_a_contact_number_left_in_plain_text_is_encrypted_at_start_up(app):
    from app.services.startup_migrations import _encrypt_legacy_contact_numbers

    user = _user("legacy@x.test")
    db.session.execute(text("UPDATE user SET contact_number = '09170000001' WHERE email = 'legacy@x.test'"))
    db.session.commit()
    db.session.expire_all()
    assert _encrypt_legacy_contact_numbers() == 1
    db.session.expire_all()
    row = db.session.execute(text("SELECT contact_number, contact_number_enc FROM user WHERE email='legacy@x.test'")).one()
    assert row[0] is None and row[1].startswith("enc1:")
    assert User.query.get(user.user_id).contact_number == "09170000001"


def test_a_one_peso_capital_is_refused_and_five_thousand_is_accepted(app):
    from app.services.plan_params import parse_plan_form

    base = {"business_name": "X", "industry_type": "Food and Beverage", "location": "Poblacion"}
    _data, errors = parse_plan_form({**base, "capital": "1"})
    assert any("at least ₱5,000" in e for e in errors)
    data, errors = parse_plan_form({**base, "capital": "5000"})
    assert not errors and data["capital"] == 5000.0


def test_a_plan_location_must_be_one_of_the_76_barangays(app):
    from app.services.plan_params import parse_plan_form

    base = {"business_name": "X", "industry_type": "Food and Beverage", "capital": "50000"}
    _data, errors = parse_plan_form({**base, "location": "Makati"})
    assert any("76 barangays" in e for e in errors)
    data, errors = parse_plan_form({**base, "location": "poblacion"})
    assert not errors and data["location"] == "Poblacion"
    data, errors = parse_plan_form({**base, "location": "Old Name"}, keep_location="Old Name")
    assert not errors


INTERNALS = ("_API_KEY", "LLM_PROVIDER", "USE_LLM", ".env", ".py", "find_or_create", "<code>competitor_count",
             "market_data rows")


@pytest.mark.parametrize("path", ["/admin/settings", "/admin/datasets", "/admin/dashboard", "/admin/users"])
def test_admin_pages_do_not_describe_the_internals(app, path):
    _user("admin2@x.test", role="Admin")
    page = _login(app, "admin2@x.test").get(path).get_data(as_text=True)
    for internal in INTERNALS:
        assert internal not in page, f"{internal!r} shown on {path}"
    assert "<!--" not in page, f"an HTML comment ships with {path}"


def test_uploader_and_account_emails_are_masked(app):
    admin = _user("admin3@x.test", role="Admin")
    lgu = _user("officer.santos@lgu.test", role="LGU", name="Officer Santos")
    db.session.add(LguData(source="DTI", barangay="Poblacion", permit_count=3, uploaded_by=lgu.user_id))
    db.session.commit()
    client = _login(app, admin.email)
    datasets = client.get("/admin/datasets").get_data(as_text=True)
    assert "Officer Santos" in datasets and "of•••@lgu.test" in datasets
    users = client.get("/admin/users").get_data(as_text=True)
    assert 'class="dss-masked"' in users and ">of•••@lgu.test<" in users


def test_masking_helpers():
    from app.utils.privacy import mask_email, mask_phone

    assert mask_email("juan.delacruz@gmail.com") == "ju•••@gmail.com"
    assert mask_phone("09171234567") == "0917•••4567"


@pytest.mark.parametrize("role,path", [("SME", "/home"), ("SME", "/recommendations"), ("LGU", "/lgu/dashboard"),
                                       ("Admin", "/admin/audit-log")])
def test_every_signed_in_page_has_the_quick_links_and_no_footer_bar(app, role, path):
    _user(f"{role.lower()}-nav@x.test", role=role)
    page = _login(app, f"{role.lower()}-nav@x.test").get(path).get_data(as_text=True)
    assert 'class="dss-quick-links"' in page
    for label in ("PSA Data Source", "Community Forum", "Contact Us"):
        assert f'aria-label="{label}"' in page
    assert "dss-home-footer" not in page
    assert "DTI-Verified" not in page and "dti.gov.ph" not in page


@pytest.mark.parametrize("path", ["/login", "/register", "/forgot-password"])
def test_the_loading_screen_is_on_the_public_pages_too(app, path):
    page = app.test_client().get(path).get_data(as_text=True)
    assert 'id="dssPageLoader"' in page and "dss-spinner" in page


def test_the_loading_screen_script_skips_downloads_and_new_tabs():
    import os

    source = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "main.js"), encoding="utf-8").read()
    assert "PAGE LOADER" in source and "beforeunload" in source and "pageshow" in source
    assert "DOWNLOAD_RE" in source and "data-no-loader" in source


def test_one_tier_vocabulary_on_the_map(app):
    import os

    _user("sme-map@x.test")
    page = _login(app, "sme-map@x.test").get("/saturation-map").get_data(as_text=True)
    assert "High Opportunity" not in page and "Low-Moderate" not in page and "High Saturation" not in page
    for tier in ("Saturated", "High", "Moderate", "Low"):
        assert f'data-tier-filter="{tier}"' in page
    source = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "map.js"), encoding="utf-8").read()
    assert 'Moderate: "Moderate saturation"' in source and 'High: "High saturation"' in source
    assert "dssIsolatedLocation !== null && location !== dssIsolatedLocation" in source
    assert "function periodGuidanceHtml" in source and "Saturation now" in source


def test_the_trend_report_exports_as_a_document(app):
    _user("lgu-print@x.test", role="LGU")
    client = _login(app, "lgu-print@x.test")
    page = client.get("/trend-reports").get_data(as_text=True)
    assert 'id="exportReport"' in page and "/trend-reports/print" in page and "window.print()" not in page
    doc = client.get("/trend-reports/print?as_of=2026-06").get_data(as_text=True)
    assert "marketlens-wordmark.png" in doc
    assert 'counter(page)' in doc and "Trend Report &amp; Market Analytics" in doc
    assert "dss-sidebar" not in doc and "June 2026" in doc
    assert AuditLog.query.filter_by(action="export_trend_report").count() == 1


def test_the_quarterly_legend_uses_the_series_colours_and_the_guide_is_a_dialog(app):
    _user("sme-trend@x.test")
    page = _login(app, "sme-trend@x.test").get("/trend-reports").get_data(as_text=True)
    assert "generateLabels" in page and 'id="quarterlyGuideModal"' in page
    assert 'id="topIndustriesModal"' in page and "TOP_INDUSTRIES_SHOWN = 5" in page


def test_the_lgu_dashboard_counts_only_official_barangays(app):
    officer = _user("lgu-count@x.test", role="LGU")
    for name in ("Poblacion", "poblacion", "Baras", "Not A Barangay"):
        db.session.add(LguData(source="DTI", barangay=name, permit_count=1, uploaded_by=officer.user_id))
    db.session.commit()
    page = _login(app, officer.email).get("/lgu/dashboard").get_data(as_text=True)
    tile = page[page.index("Barangays On File"):]
    tile = tile[:tile.index("</div>\n    </div>")]
    assert re.search(r">\s*2\s*<span[^>]*>of 76</span>", tile), tile
    assert "1 uploaded name(s) not matched" in tile


def test_warm_up_loads_models_and_compiles_templates(app):
    from app.services.warmup import warm_up

    timings = warm_up(app)
    assert set(timings) == {"models", "templates", "reference_data"}
    assert "sme/home.html" in app.jinja_env.cache.__repr__() or len(app.jinja_env.cache) > 20


def test_static_files_are_versioned_cached_for_a_year_and_gzipped(app):
    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)
    url = re.search(r'/static/css/style\.css\?v=[0-9a-f]{10}', page).group(0)
    response = client.get(url, headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert "max-age=31536000" in response.headers["Cache-Control"] and "immutable" in response.headers["Cache-Control"]
    assert response.headers.get("Content-Encoding") == "gzip"
    assert b".dss-page-loader" in gzip.decompress(response.data)
    again = client.get(url, headers={"Accept-Encoding": "gzip", "If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304
    plain = client.get("/static/css/style.css")
    assert "max-age=31536000" not in (plain.headers.get("Cache-Control") or "")


def test_pages_are_gzipped_for_browsers_that_ask(app):
    response = app.test_client().get("/login", headers={"Accept-Encoding": "gzip, br"})
    assert response.headers.get("Content-Encoding") == "gzip"
    assert b"dssPageLoader" in gzip.decompress(response.data)
    assert app.test_client().get("/login").headers.get("Content-Encoding") is None


def test_repeated_questions_cost_one_query_per_request(app):
    from sqlalchemy import event

    _user("memo@x.test")
    client = _login(app, "memo@x.test")
    client.get("/home")
    seen = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        client.get("/home")
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    unread_counts = [s for s in seen if "count(*)" in s.lower() and "notifications" in s.lower()]
    assert len(unread_counts) == 1, unread_counts


def test_the_population_figures_are_parsed_once():
    from app.ml import seed_data

    seed_data._parse_source_comments()
    calls = {"n": 0}
    original = seed_data._read_source_comments

    def counting():
        calls["n"] += 1
        return original()

    seed_data._read_source_comments = counting
    try:
        for name in seed_data.BARANGAY_NAMES:
            seed_data.get_real_population(name)
    finally:
        seed_data._read_source_comments = original
    assert calls["n"] == 0


def test_a_low_score_is_red_and_the_recommendation_has_a_brief(app):
    from app.services.forecasting_service import generate_forecast_for_profile

    owner = _user("rec@x.test")
    profile = SmeProfile(user_id=owner.user_id, business_name="Kape", industry_type="Food and Beverage",
                         location="Poblacion", startup_capital=6000, business_stage="startup")
    db.session.add(profile)
    db.session.commit()
    forecast = generate_forecast_for_profile(profile)
    page = _login(app, owner.email).get("/recommendations").get_data(as_text=True)
    tone = "good" if float(forecast.viability_score) >= 6.5 else ("fair" if float(forecast.viability_score) >= 4 else "low")
    assert f"dss-score-text-{tone}" in page
    assert "In brief" in page and "Biggest risk" in page and "Next step" in page
    assert '<details class="dss-full-analysis' in page


def test_score_tone_cuts():
    app = create_app("testing")
    tone = app.jinja_env.globals["score_tone"]
    assert (tone(3.3), tone(4), tone(6.4), tone(6.5), tone("x")) == ("low", "fair", "fair", "good", "fair")


def test_the_request_memo_forgets_after_a_bulk_update(app):
    from app.models import Notification

    user = _user("bulk@x.test")
    db.session.add(Notification(user_id=user.user_id, message="hello", is_read=False))
    db.session.commit()
    with app.test_request_context("/"):
        assert Notification.unread_count_for(user.user_id) == 1
        Notification.query.filter_by(user_id=user.user_id).update({"is_read": True}, synchronize_session=False)
        db.session.commit()
        assert Notification.unread_count_for(user.user_id) == 0


def test_the_audit_trail_records_that_personal_details_changed_not_what_they_are(app):
    _user("audit-pii@x.test", name="Audit Person")
    client = _login(app, "audit-pii@x.test")
    client.post("/settings", data={"form_type": "profile", **{k: v for k, v in PERSONAL.items() if k != "accept_terms"},
                                   "gender": "Female"})
    entry = AuditLog.query.filter_by(action="update_profile").order_by(AuditLog.id.desc()).first()
    import json

    changes = json.loads(entry.changes)
    for field in ("gender", "birthdate", "contact_number", "address_line"):
        assert changes[field][1] == "(hidden)", field
    assert "Female" not in entry.changes and "F•" not in entry.changes


def test_old_audit_entries_with_contact_numbers_are_scrubbed_at_start_up(app):
    import json

    from app.services.startup_migrations import _hide_personal_details_in_audit_trail

    db.session.add(AuditLog(action="update_profile", details="changed contact_number",
                            changes=json.dumps({"contact_number": ["09170000000", "09181111111"], "name": ["A", "B"]})))
    db.session.commit()
    assert _hide_personal_details_in_audit_trail() == 1
    row = AuditLog.query.filter_by(action="update_profile").one()
    assert json.loads(row.changes) == {"contact_number": ["(hidden)", "(hidden)"], "name": ["A", "B"]}
    assert _hide_personal_details_in_audit_trail() == 0


def test_the_newer_plain_text_number_wins_when_both_columns_hold_one(app):
    from app.services.startup_migrations import _encrypt_legacy_contact_numbers

    user = _user("both@x.test", contact_number="09170000001")
    db.session.execute(text("UPDATE user SET contact_number = '09179999999' WHERE email = 'both@x.test'"))
    db.session.commit()
    db.session.expire_all()
    _encrypt_legacy_contact_numbers()
    db.session.expire_all()
    assert User.query.get(user.user_id).contact_number == "09179999999"


def test_an_old_plan_below_the_minimum_can_still_be_edited_without_raising_its_capital(app):
    from app.services.plan_params import parse_plan_form

    form = {"business_name": "Renamed", "industry_type": "Food and Beverage", "location": "Poblacion", "capital": "1000"}
    _data, errors = parse_plan_form(form)
    assert errors
    data, errors = parse_plan_form(form, keep_capital=1000)
    assert not errors and data["capital"] == 1000.0
    _data, errors = parse_plan_form({**form, "capital": "2000"}, keep_capital=1000)
    assert errors, "a CHANGED capital still has to meet the minimum"


def test_a_gzip_revalidation_keeps_the_gzip_validators_and_gzip_q0_is_respected(app):
    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)
    url = re.search(r'/static/css/style\.css\?v=[0-9a-f]{10}', page).group(0)
    first = client.get(url, headers={"Accept-Encoding": "gzip"})
    again = client.get(url, headers={"Accept-Encoding": "gzip", "If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304
    assert again.headers["ETag"] == first.headers["ETag"]
    assert "Accept-Encoding" in again.headers.get("Vary", "")
    refused = client.get("/login", headers={"Accept-Encoding": "gzip;q=0, identity"})
    assert refused.headers.get("Content-Encoding") is None


def test_the_diversification_plan_follows_lgu_data_too(app):
    from app.services.diversification_service import _PLAN_CACHE, build_diversification_plan
    from app.services.forecasting_service import _scoring_inputs_fingerprint

    build_diversification_plan()
    before = _PLAN_CACHE["plan"][0]
    officer = _user("lgu-plan@x.test", role="LGU")
    db.session.add(LguData(source="DTI", barangay="Tibag", permit_count=2, uploaded_by=officer.user_id))
    db.session.commit()
    assert _scoring_inputs_fingerprint() != before
    build_diversification_plan()
    assert _PLAN_CACHE["plan"][0] != before
