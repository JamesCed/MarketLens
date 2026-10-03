"""
tests/test_revisions_part2.py
-------------------------------
The "Changes & Revisions, part 2" round:

  1. Home is a SUMMARY; the planning itself is on the new Planning page.
  2. Planning shows the direct competitors with an AI insight (short),
     and Recommendations shows it in full.
  3. Trash keeps a plan for 30 days, says so, then deletes it -- logged.
  4. "Add to Plans" (not "Run AI Forecast"), and the barangay sentence
     under the industry slider reads correctly.
  5. Trend Reports: the distribution is a bar chart, every chart carries
     a plain-language note.
  6. LGU: the Diversification Plan (page, CSV, the HHI measure) and the
     dashboard's slider + map; LGU pages are LGU-only.
  7. Admin: its own monitoring dashboard; Manage Users search / filter /
     last seen / timed suspension / link to a user's trail; the audit
     trail's importance filter, per-user view, Details and folding.
"""

from datetime import datetime, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import AuditLog, SmeProfile, SystemSetting, User
from app.models.archive import get_including_archived


@pytest.fixture
def app():
    app = create_app("testing")

    @app.before_request
    def _forget_cached_login():
        from flask import g

        g.pop("_login_user", None)

    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role="SME", name=None):
    user = User(name=name or f"{role} Person", email=email, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


def _plan(user, name="Patola Pandesal", **extra):
    fields = dict(business_name=name, industry_type="Food and Beverage", location="Tibag",
                  startup_capital=300000, employee_count=2, business_stage="startup")
    fields.update(extra)
    profile = SmeProfile(user_id=user.user_id, **fields)
    db.session.add(profile)
    db.session.commit()
    return profile


# =====================================================================
# 1. Home is a summary, Planning is the workspace
# =====================================================================
def test_home_is_an_overview_that_links_to_every_section(app):
    sme = _user("home@p2.test")
    _plan(sme)
    page = _login(app, sme.email).get("/home").get_data(as_text=True)
    for link in ("/planning", "/saturation-map", "/trend-reports", "/recommendations"):
        assert f'href="{link}' in page
    assert "Patola Pandesal" in page
    # The workspace itself is not on Home.
    assert 'data-plan-chip=' not in page and 'id="addPlanModal"' not in page


def test_planning_holds_the_plans_and_the_sidebar_names_it(app):
    sme = _user("plan@p2.test")
    _plan(sme)
    page = _login(app, sme.email).get("/planning").get_data(as_text=True)
    assert 'data-plan-chip=' in page and 'id="addPlanModal"' in page
    assert 'data-tour="nav-planning"' in page


def test_add_plan_button_says_add_to_plans_and_lands_on_planning(app):
    sme = _user("add@p2.test")
    client = _login(app, sme.email)
    page = client.get("/planning").get_data(as_text=True)
    assert "Add to Plans" in page and "Run AI Forecast" not in page
    response = client.post("/home/analyze", data={
        "business_name": "New Bakery", "industry_type": "Food and Beverage", "location": "Tibag",
        "business_stage": "startup", "capital": "200000",
    })
    assert response.status_code == 302 and "/planning?plan=" in response.headers["Location"]


def test_the_barangay_sentence_names_the_plan_as_opening_there(app):
    sme = _user("sentence@p2.test")
    _plan(sme)
    page = _login(app, sme.email).get("/planning").get_data(as_text=True)
    assert "the barangay of" not in page
    assert "the barangay where your plan" in page


# =====================================================================
# 2. Competitor insight
# =====================================================================
def test_competitor_insight_is_shown_short_on_planning_and_full_on_recommendations(app):
    sme = _user("insight@p2.test")
    _plan(sme, subcategory="bakery", innovation_idea="Malunggay pandesal")
    client = _login(app, sme.email)
    planning = client.get("/planning").get_data(as_text=True)
    assert "dss-competitor-panel" in planning and "Full insight and recommendations" in planning
    recs = client.get("/recommendations").get_data(as_text=True)
    assert "dss-competitor-panel" in recs


def test_rule_based_insight_names_a_high_novelty_idea_as_a_real_chance():
    from app.services.recommendation_service import competitor_insight_from

    analysis = {"label": "Bakery", "direct_count": 12, "is_estimated": False, "density_ratio": 1.4,
                "source": "Google Places API"}
    insight = competitor_insight_from(analysis, 40, "Food and Beverage", "Tibag",
                                      {"has_idea": True, "novelty": "High", "differentiation_need": "High"})
    assert insight["direct_count"] == 12 and insight["industry_count"] == 40
    assert "12 bakery businesses already trade in Tibag" in insight["short"]
    assert "real chance" in insight["detail"] and "heavier" in insight["detail"]


def test_an_old_stored_forecast_still_gets_an_insight():
    from app.services.recommendation_service import parse_recommendation
    import json

    raw = json.dumps({"headline": "H", "summary": "S", "reasons": [], "risks": [],
                      "forecast": {"market": {"competitor_count": 9}, "inputs": {"industry_type": "Food and Beverage",
                                                                                 "location": "Tibag"}}})
    insight = parse_recommendation(raw)["competitor_insight"]
    assert insight and "9 food and beverage businesses" in insight["short"]


def test_an_ai_insight_that_invents_a_number_is_replaced():
    from app.services import recommendation_service as rs

    context = {"subcategory_analysis": None, "competitor_count": 7, "industry_type": "Food and Beverage",
               "location": "Tibag", "forecast": None}
    rule = rs.competitor_insight_from(None, 7, "Food and Beverage", "Tibag", None)
    invented = {"short": "There are 250 shops here.", "detail": "250 shops already trade here and 90% fail."}
    assert rs._choose_competitor_insight(invented, "llm:gemini:x", rule, context) == rule


# =====================================================================
# 3. Trash: 30 days, then deleted for good
# =====================================================================
def test_trash_says_thirty_days_and_counts_down(app):
    sme = _user("trash@p2.test")
    plan = _plan(sme)
    client = _login(app, sme.email)
    client.post(f"/home/plans/{plan.sme_id}/trash")
    page = client.get("/planning").get_data(as_text=True)
    assert "archived for 30 days, then deleted permanently" in page
    assert "deleted permanently in 30 days" in page or "deleted permanently in 29 days" in page


def test_a_plan_left_in_trash_over_thirty_days_is_purged_and_logged(app):
    sme = _user("purge@p2.test")
    old = _plan(sme, name="Old Plan")
    recent = _plan(sme, name="Recent Plan")
    old.archive(sme.user_id, "test")
    old.archived_at = datetime.utcnow() - timedelta(days=31)
    recent.archive(sme.user_id, "test")
    recent.archived_at = datetime.utcnow() - timedelta(days=29)
    db.session.commit()
    old_id, recent_id = old.sme_id, recent.sme_id
    _login(app, sme.email).get("/planning")
    db.session.expire_all()
    assert get_including_archived(SmeProfile, old_id) is None
    assert get_including_archived(SmeProfile, recent_id) is not None
    assert AuditLog.query.filter_by(action="purge_plan").count() == 1


# =====================================================================
# 5. Trend Reports
# =====================================================================
def test_trend_reports_use_a_bar_chart_and_explain_every_chart(app):
    for role in ("SME", "LGU", "Admin"):
        user = _user(f"{role.lower()}-trend@p2.test", role=role)
        page = _login(app, user.email).get("/trend-reports").get_data(as_text=True)
        assert 'type: "pie"' not in page and 'indexAxis: "y"' in page
        for note in ("monthlyNote", "distributionNote", "quarterlyReading", "topIndustriesNote"):
            assert f'id="{note}"' in page, (role, note)
        assert "How to read this chart" in page
        assert 'id="monthlyFocus"' in page


# =====================================================================
# 6. LGU
# =====================================================================
def test_diversity_score_is_the_normalised_hhi():
    from app.services.diversification_service import diversity_score

    assert diversity_score([10] + [0] * 19) == 0.0
    assert diversity_score([5] * 20) == 100.0
    assert diversity_score([]) is None


def test_lgu_gets_the_diversification_plan_and_admin_does_not(app):
    lgu = _user("lgu@p2.test", role="LGU")
    admin = _user("admin@p2.test", role="Admin")
    lgu_client = _login(app, lgu.email)
    page = lgu_client.get("/lgu/diversification").get_data(as_text=True)
    assert "Diversification Plan" in page and 'id="stanceChart"' in page and 'id="stanceNote"' in page
    csv_response = lgu_client.get("/lgu/diversification.csv")
    assert csv_response.status_code == 200 and csv_response.data.startswith(b"barangay,")
    dashboard = lgu_client.get("/lgu/dashboard").get_data(as_text=True)
    assert 'data-industry-slider' in dashboard and 'id="dss-map"' in dashboard
    assert 'href="/lgu/diversification"' in dashboard
    admin_client = _login(app, admin.email)
    for path in ("/lgu/dashboard", "/lgu/diversification", "/lgu/government-data-upload"):
        assert admin_client.get(path).status_code == 403, path


def test_admin_menu_has_no_lgu_pages(app):
    admin = _user("menu-admin@p2.test", role="Admin")
    page = _login(app, admin.email).get("/admin/dashboard").get_data(as_text=True)
    assert 'data-tour="nav-lgu-dashboard"' not in page and 'data-tour="nav-data-upload"' not in page


# =====================================================================
# 7. Admin
# =====================================================================
def test_admin_dashboard_monitors_users_activity_and_health(app):
    admin = _user("dash@p2.test", role="Admin")
    page = _login(app, admin.email).get("/admin/dashboard").get_data(as_text=True)
    for text in ("Online now", "Inactive", "Suspended", "System health", "System activity, last 14 days",
                 'id="activityNote"'):
        assert text in page, text


def test_failed_sign_in_bursts_raise_an_alert(app):
    admin = _user("alert@p2.test", role="Admin")
    victim = _user("victim@p2.test")
    for _ in range(5):
        app.test_client().post("/login", data={"email": victim.email, "password": "wrong"})
    page = _login(app, admin.email).get("/admin/dashboard").get_data(as_text=True)
    assert "Possible password guessing" in page and victim.email in page


def test_sign_in_records_last_seen(app):
    sme = _user("seen@p2.test")
    assert sme.last_seen_at is None
    _login(app, sme.email)
    db.session.expire_all()
    assert db.session.get(User, sme.user_id).last_seen_at is not None


def test_manage_users_searches_by_uid_and_filters_inactive(app):
    admin = _user("users@p2.test", role="Admin")
    dormant = _user("dormant@p2.test", name="Dora Dormant")
    dormant.last_seen_at = datetime.utcnow() - timedelta(days=45)
    db.session.commit()
    client = _login(app, admin.email)
    by_uid = client.get(f"/admin/users?q={dormant.user_id}").get_data(as_text=True)
    assert "Dora Dormant" in by_uid and f"UID #{dormant.user_id}" in by_uid
    inactive = client.get("/admin/users?status=inactive").get_data(as_text=True)
    assert "Dora Dormant" in inactive
    assert f'href="/admin/audit-log?user={dormant.user_id}' in inactive


def test_a_timed_suspension_ends_by_itself(app):
    admin = _user("susp@p2.test", role="Admin")
    sme = _user("suspended@p2.test")
    client = _login(app, admin.email)
    client.post(f"/admin/users/{sme.user_id}/toggle-active", data={"reason": "testing timed", "duration": "3"})
    db.session.expire_all()
    user = db.session.get(User, sme.user_id)
    assert user.status == "inactive" and user.suspended_until is not None
    assert 2.9 < (user.suspended_until - datetime.utcnow()).total_seconds() / 86400 < 3.1
    entry = AuditLog.query.filter_by(action="admin_toggle_active").order_by(AuditLog.id.desc()).first()
    assert entry.changes and "suspended_until" in entry.changes

    # Still suspended: refused, and told until when.
    response = app.test_client().post("/login", data={"email": sme.email, "password": "password123"})
    assert b"suspended until" in response.data
    # Time passes: the suspension is over at the next sign-in.
    user.suspended_until = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    response = app.test_client().post("/login", data={"email": sme.email, "password": "password123"})
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(User, sme.user_id).status == "active"


def test_audit_trail_hides_routine_rows_by_default_and_shows_them_on_request(app):
    admin = _user("audit@p2.test", role="Admin")
    from app.utils.audit import log_action

    log_action("update_theme", details="MARK-routine", user_id=admin.user_id)
    log_action("admin_update_settings", details="MARK-important", user_id=admin.user_id)
    client = _login(app, admin.email)
    default = client.get("/admin/audit-log").get_data(as_text=True)
    assert "MARK-important" in default and "MARK-routine" not in default
    everything = client.get("/admin/audit-log?importance=all").get_data(as_text=True)
    assert "MARK-routine" in everything
    # Asking for a routine action by name always finds it.
    assert "MARK-routine" in client.get("/admin/audit-log?action=update_theme").get_data(as_text=True)


def test_a_users_trail_shows_what_they_did_and_what_was_done_to_them(app):
    admin = _user("trail@p2.test", role="Admin")
    sme = _user("subject@p2.test", name="Sam Subject")
    client = _login(app, admin.email)
    client.post(f"/admin/users/{sme.user_id}/toggle-active", data={"reason": "testing trail", "duration": "1"})
    page = client.get(f"/admin/audit-log?user={sme.user_id}&importance=all").get_data(as_text=True)
    assert "Showing the activity of" in page and "Sam Subject" in page
    assert "Details" in page  # the suspension's before/after


def test_editing_a_plan_records_exactly_what_changed(app):
    sme = _user("diff@p2.test")
    plan = _plan(sme)
    client = _login(app, sme.email)
    client.post(f"/home/plans/{plan.sme_id}/update", data={
        "business_name": "Patola Pandesal", "industry_type": "Food and Beverage", "location": "Tibag",
        "business_stage": "startup", "capital": "450000", "employee_count": "2",
    })
    import json

    entry = AuditLog.query.filter_by(action="update_plan").order_by(AuditLog.id.desc()).first()
    changes = json.loads(entry.changes)
    assert changes["capital"] == [300000.0, 450000.0]
    assert "business_name" not in changes


def test_repeated_actions_fold_into_one_row():
    from types import SimpleNamespace

    from app.controllers.admin_controller import _group_repeats

    now = datetime(2026, 10, 1, 8, 0)
    rows = [SimpleNamespace(who_name="A", action="login_failed", target_label=None, when_utc=now - timedelta(minutes=i),
                            changes=None) for i in range(4)]
    rows.append(SimpleNamespace(who_name="B", action="login", target_label=None, when_utc=now - timedelta(minutes=5),
                                changes=None))
    groups = _group_repeats(rows)
    assert len(groups) == 2 and len(groups[0]["repeats"]) == 3
