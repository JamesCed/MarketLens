"""
tests/test_market_alerts.py
------------------------------
app/services/market_alert_service.py -- automatic notification when the
market actually moves.

WHAT THESE TESTS ARE GUARDING

The feature exists because three of the four switches on Settings >
Notifications were stored and never acted on, which the page admitted
with a "not sending yet" badge. The risk in removing that badge is
obvious: a switch that claims to send and does not is worse than one
that says it does not send. So the tests below are weighted towards the
claims rather than the mechanics --

  * a move has to be a real move (the delta bar AND a competitor change)
  * an SME hears only about their own barangay/industry for warnings,
    but their whole industry for opportunities, because those are
    different questions
  * a switch that is OFF sends nothing, in-app or by email
  * the cooldown actually stops a burst
  * a broken LLM, a broken mail transport and a broken sweep each fail
    without taking the upload or the alert down with them

No test here reaches the network. The email transport is stubbed at
email_service, which is also where the real one is guarded.
"""

from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import MarketData, Notification, SmeProfile, SystemSetting
from app.models.user import User
from app.services import market_alert_service as alerts


# ---------------------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------------------

@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(role="SME", email=None, **prefs):
    """One account, with its notification switches set explicitly.

    Every switch this feature reads is passed in by name rather than
    left to the column default, so a test asserting "this account is
    notified" is asserting about a switch the test itself set.
    """
    user = User(
        name=f"{role} tester",
        email=email or f"{role.lower()}-{User.query.count()}@example.com",
        role=role,
    )
    user.set_password("Testing123!")
    for field, value in prefs.items():
        setattr(user, field, value)
    db.session.add(user)
    db.session.flush()
    return user


def _plan(user, industry, location):
    profile = SmeProfile(
        user_id=user.user_id,
        business_name=f"{industry} in {location}",
        industry_type=industry,
        location=location,
        business_stage="startup",
    )
    db.session.add(profile)
    db.session.flush()
    return profile


def _lgu_row(location):
    """The barangay's lgu_data row, created once.

    Required, not decoration. saturation_for_counts() scores only
    combinations that have BOTH a market_data and an lgu_data row --
    read-only by design, so a barangay the city has no record for is
    skipped rather than invented. A test without this would be
    asserting on the skip path.
    """
    from app.models import LguData

    existing = LguData.query.filter_by(barangay=location).first()
    if existing is not None:
        return existing

    owner = User.query.first()
    if owner is None:
        owner = _user("Admin", f"seed-{location.lower().replace(' ', '-')}@example.com")

    row = LguData(
        source="CLUP",
        barangay=location,
        zoning_info="Commercial",
        closure_records=2,
        permit_count=40,
        business_density=3.5,
        effective_date=date(2026, 1, 1),
        upload_date=date(2026, 1, 1),
        uploaded_by=owner.user_id,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _market_history(industry, location, counts, start_day=1):
    """Two or more market_data rows for one combination, oldest first.

    The dates matter: detect_fluctuations() takes the newest two rows
    per combination, so a test that wrote them all with the same date
    would be asserting against row insertion order rather than history.

    The other columns are filled with the same shape the real importers
    write, because compute_scores_batch() feeds them to the model -- a
    row with a NULL population would be testing the imputation path
    rather than the detector.
    """
    _lgu_row(location)

    rows = []
    for offset, count in enumerate(counts):
        row = MarketData(
            industry_type=industry,
            location=location,
            competitor_count=count,
            population_density=5995,
            historical_success_rate=0.65,
            foot_traffic_index=48,
            average_rent=23000,
            date_recorded=date(2026, 1, start_day + offset),
            source="Manual",
        )
        db.session.add(row)
        rows.append(row)
    db.session.flush()
    return rows


@pytest.fixture
def sent(monkeypatch):
    """Captures every email the service tries to send, and sends none.

    Stubbed at email_service rather than at the transport: that is the
    seam the service itself imports through, so a refactor that started
    calling a different sender would show up here as a missing capture
    instead of passing silently.
    """
    from app.services import email_service

    captured = []

    def _capture(kind):
        def _send(to_email, to_name, subject, body):
            captured.append({"kind": kind, "to": to_email, "name": to_name,
                             "subject": subject, "body": body})
            return True
        return _send

    monkeypatch.setattr(email_service, "send_market_alert", _capture("market"))
    monkeypatch.setattr(email_service, "send_recommendation_alert", _capture("recommendation"))
    monkeypatch.setattr(email_service, "send_trend_digest", _capture("digest"))
    return captured


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Deterministic wording by default.

    The LLM is exercised in its own tests below. Everywhere else the
    rule-based text is what is asserted against, because a test that
    depended on generated prose would be a test of the model.
    """
    from app.services import recommendation_service

    monkeypatch.setattr(recommendation_service, "llm_recommendations_enabled", lambda: False)


# ---------------------------------------------------------------------
# DETECTION
# ---------------------------------------------------------------------

def test_a_big_move_with_more_competitors_is_detected(app):
    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [4, 40])

        moves = alerts.detect_fluctuations()

        assert len(moves) == 1
        move = moves[0]
        assert move["industry_type"] == "Food and Beverage"
        assert move["location"] == "San Roque"
        assert move["competitors_before"] == 4
        assert move["competitors_now"] == 40
        assert move["direction"] == "rose"
        assert move["delta"] > 0


def test_one_recorded_state_is_not_a_change(app):
    """A brand-new combination has no previous state. Reporting its
    first reading as a "move" would make every upload of new data look
    like a city-wide upheaval."""
    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [12])

        assert alerts.detect_fluctuations() == []


def test_saturation_drift_with_no_competitor_change_is_not_reported(app):
    """The second bar, and the one that keeps this from being noise.

    Saturation can move because a population or rent figure was
    revised. That is real, but it is not "the market moved", and an
    alert that cannot point at a cause is one people learn to ignore.

    The two rows below hold the SAME competitor count and wildly
    different population, rent and foot-traffic figures -- a spread
    that moves the model's own output by about 13 points, well past
    MIN_SATURATION_DELTA. So this is a case that WOULD be reported if
    the guarantee were not there.
    """
    with app.app_context():
        _lgu_row("San Roque")
        revisions = [(1200, 5000, 10), (14000, 45000, 95)]
        for offset, (density, rent, foot) in enumerate(revisions):
            db.session.add(MarketData(
                industry_type="Food and Beverage", location="San Roque",
                competitor_count=15, population_density=density,
                historical_success_rate=0.65, foot_traffic_index=foot,
                average_rent=rent, date_recorded=date(2026, 1, 1 + offset),
                source="Manual",
            ))
        db.session.commit()

        assert alerts.detect_fluctuations() == []


def test_a_small_move_within_the_same_tier_is_not_reported(app):
    with app.app_context():
        # One extra competitor out of fifty cannot shift the index ten
        # points, and it cannot cross a tier boundary either.
        _market_history("Food and Beverage", "San Roque", [50, 51])

        assert alerts.detect_fluctuations() == []


# ---------------------------------------------------------------------
# THE REPORTING RULE ITSELF
# ---------------------------------------------------------------------
# Tested directly rather than through the detector. Going through the
# detector meant the boundary cases were never actually reached -- the
# model's output for a given pair of competitor counts never landed
# near a tier edge, so the assertions passed whether or not the rule
# was right. Deleting the tier floor entirely left every test green.

def test_a_big_move_is_reportable_even_inside_one_tier():
    assert alerts.is_reportable(12.0, "High", "High") is True
    assert alerts.is_reportable(-12.0, "High", "High") is True


def test_a_small_move_inside_one_tier_is_not():
    assert alerts.is_reportable(4.0, "High", "High") is False
    assert alerts.is_reportable(-4.0, "High", "High") is False


def test_a_tier_crossing_is_reportable_below_the_main_bar():
    """The tier is what the rest of the app displays, so crossing one
    is news even when the index barely moved."""
    assert alerts.is_reportable(5.0, "Moderate", "High") is True


def test_a_knife_edge_tier_crossing_is_not_reportable():
    """The failure mode this floor exists for: a combination sitting at
    49.8 crosses into High when one shop opens and back to Moderate
    when one closes. Without the floor it alerts forever over a
    one-point move, and an alert stream like that gets muted."""
    assert alerts.is_reportable(0.9, "Moderate", "High") is False
    assert alerts.is_reportable(-0.9, "High", "Moderate") is False


def test_the_tier_floor_is_well_below_the_main_bar():
    """If the two were equal the tier branch would be dead code, and a
    crossing would need a full ten points like anything else."""
    assert 0 < alerts.MIN_TIER_CROSS_DELTA < alerts.MIN_SATURATION_DELTA


def test_moves_are_ordered_biggest_first(app):
    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [4, 40])
        _market_history("Manufacturing", "Matatalaib", [5, 12])

        moves = alerts.detect_fluctuations()

        deltas = [abs(move["delta"]) for move in moves]
        assert deltas == sorted(deltas, reverse=True)


# ---------------------------------------------------------------------
# SCOPING: WHO HEARS WHAT
# ---------------------------------------------------------------------

def test_an_sme_is_warned_only_about_their_own_barangay_and_industry(app, sent):
    with app.app_context():
        mine = _user("SME", "mine@example.com", notify_saturation_change=True)
        _plan(mine, "Food and Beverage", "San Roque")

        # A move in their plan, and two that are none of their business.
        _market_history("Food and Beverage", "San Roque", [4, 40])
        _market_history("Construction", "Matatalaib", [3, 30])
        _market_history("Food and Beverage", "Maliwalo", [3, 30])
        db.session.commit()

        alerts.run_market_alert_sweep(force=True)

        warnings = [row for row in Notification.query
                    .filter_by(user_id=mine.user_id, type="early_warning").all()]
        assert len(warnings) == 1
        assert "San Roque" in warnings[0].message
        assert "Matatalaib" not in warnings[0].message
        assert "Maliwalo" not in warnings[0].message


def test_an_opportunity_reaches_their_industry_anywhere_in_the_city(app, sent):
    """The scope that differs from the warning scope, and the reason
    there are two.

    A recommendation is about where to go NEXT. Scoping it to the
    barangay they already chose would leave only the one place they are
    not asking about.
    """
    with app.app_context():
        user = _user("SME", "opp@example.com",
                     notify_saturation_change=False, notify_recommendations=True)
        _plan(user, "Food and Beverage", "San Roque")

        # Their trade, a barangay they have no plan in, getting better.
        _market_history("Food and Beverage", "Maliwalo", [40, 4])
        db.session.commit()

        result = alerts.run_market_alert_sweep(force=True)

        assert result["opportunities"] == 1
        rows = Notification.query.filter_by(user_id=user.user_id).all()
        assert len(rows) == 1
        assert rows[0].type == "info"
        assert "Maliwalo" in rows[0].message
        assert [row["kind"] for row in sent] == ["recommendation"]


def test_a_worsening_market_is_not_offered_as_a_recommendation(app, sent):
    with app.app_context():
        user = _user("SME", "nope@example.com",
                     notify_saturation_change=False, notify_recommendations=True)
        _plan(user, "Food and Beverage", "San Roque")

        _market_history("Food and Beverage", "Maliwalo", [4, 40])  # got worse
        db.session.commit()

        result = alerts.run_market_alert_sweep(force=True)

        assert result["opportunities"] == 0
        assert Notification.query.filter_by(user_id=user.user_id).count() == 0
        assert sent == []


def test_lgu_and_admin_hear_the_city_wide_summary(app, sent):
    with app.app_context():
        lgu = _user("LGU", "lgu@example.com", notify_saturation_change=True)
        admin = _user("Admin", "admin@example.com", notify_saturation_change=True)

        _market_history("Food and Beverage", "San Roque", [4, 40])
        _market_history("Construction", "Matatalaib", [3, 30])
        db.session.commit()

        alerts.run_market_alert_sweep(force=True)

        for user in (lgu, admin):
            rows = Notification.query.filter_by(user_id=user.user_id).all()
            assert len(rows) == 1, user.email
            assert "Tarlac City" in rows[0].message


def test_an_inactive_account_is_not_notified(app, sent):
    with app.app_context():
        user = _user("LGU", "gone@example.com", notify_saturation_change=True)
        user.status = "inactive"
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        alerts.run_market_alert_sweep(force=True)

        assert Notification.query.filter_by(user_id=user.user_id).count() == 0
        assert sent == []


# ---------------------------------------------------------------------
# THE SWITCHES
# ---------------------------------------------------------------------

def test_a_switch_that_is_off_sends_nothing_at_all(app, sent):
    """Not "no email but still a bell notification".

    The check is deliberately before the Notification row is written,
    matching the existing early-warning behaviour: somebody who turned
    these off should not accumulate a hidden backlog that all appears
    the moment they turn them back on.
    """
    with app.app_context():
        user = _user("SME", "quiet@example.com",
                     notify_saturation_change=False, notify_recommendations=False)
        _plan(user, "Food and Beverage", "San Roque")
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        alerts.run_market_alert_sweep(force=True)

        assert Notification.query.filter_by(user_id=user.user_id).count() == 0
        assert sent == []


def test_the_in_app_copy_is_written_even_when_email_is_not_configured(app, monkeypatch):
    """The bell must fill up on a deployment with no mail transport.

    This is what the Settings page now promises in the "email is not
    configured" branch, and it is the honest half of the feature: an
    in-app notification is free and cannot bounce.
    """
    with app.app_context():
        from app.services import email_service

        monkeypatch.setattr(email_service, "is_configured", lambda: False)

        user = _user("LGU", "nomail@example.com", notify_saturation_change=True)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        result = alerts.run_market_alert_sweep(force=True)

        assert result["notified"] == 1
        assert result["emailed"] == 0
        assert Notification.query.filter_by(user_id=user.user_id).count() == 1


def test_the_email_goes_to_the_account_that_asked_for_it(app, sent):
    with app.app_context():
        _user("LGU", "wants@example.com", notify_saturation_change=True)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        alerts.run_market_alert_sweep(force=True)

        assert [row["to"] for row in sent] == ["wants@example.com"]
        assert "Tarlac City" in sent[0]["subject"]


def test_the_defaults_table_matches_the_column_defaults(app):
    """An account created before these columns existed falls back to
    _PREF_DEFAULTS. Getting one of those backwards would email every
    existing account something they never opted into, so the table is
    checked against the model rather than trusted.
    """
    with app.app_context():
        for field, expected in alerts._PREF_DEFAULTS.items():
            column = User.__table__.columns[field]
            assert bool(column.default.arg) is expected, field


# ---------------------------------------------------------------------
# THE COOLDOWN
# ---------------------------------------------------------------------

def test_the_second_sweep_in_the_hour_does_nothing(app, sent):
    """Importing three files in a row is one event to a person, and a
    free mail tier is measured in hundreds of messages a day."""
    with app.app_context():
        _user("LGU", "burst@example.com", notify_saturation_change=True)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        first = alerts.run_market_alert_sweep()
        second = alerts.run_market_alert_sweep()

        assert first["ran"] is True
        assert second["ran"] is False
        assert second["reason"] == "cooldown"
        assert len(sent) == 1


def test_force_overrides_the_cooldown(app, sent):
    with app.app_context():
        _user("LGU", "forced@example.com", notify_saturation_change=True)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        alerts.run_market_alert_sweep()
        assert alerts.run_market_alert_sweep(force=True)["ran"] is True
        assert len(sent) == 2


def test_an_unreadable_cooldown_stamp_does_not_wedge_alerting(app):
    """A hand-edited or truncated settings value must not be able to
    stop alerts forever. One early alert is cheaper than silence."""
    with app.app_context():
        SystemSetting.set(alerts.LAST_SWEEP_KEY, "not-a-timestamp")

        assert alerts._may_sweep_now() is True


# ---------------------------------------------------------------------
# THE WEEKLY DIGEST
# ---------------------------------------------------------------------

def test_the_digest_is_due_on_a_fresh_install(app):
    with app.app_context():
        assert alerts.digest_is_due() is True


def test_the_digest_is_not_due_again_the_same_day(app, sent):
    with app.app_context():
        _user("LGU", "digest@example.com", notify_weekly_trends=True)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        first = alerts.run_weekly_trend_digest()
        second = alerts.run_weekly_trend_digest()

        assert first["notified"] == 1
        assert second["ran"] is False
        assert second["reason"] == "not_due"
        assert [row["kind"] for row in sent] == ["digest"]


def test_a_quiet_week_sends_no_digest_and_keeps_the_clock_running(app, sent):
    """A weekly email that says "nothing changed" fifty-two times a year
    is one people filter -- and filtering it also loses the fifty-third
    that mattered. So a quiet period produces no digest AND does not
    stamp the clock, meaning the next real movement goes out at once
    rather than waiting out another week.
    """
    with app.app_context():
        _user("LGU", "quietweek@example.com", notify_weekly_trends=True)
        _market_history("Food and Beverage", "San Roque", [15, 15])  # no real move
        db.session.commit()

        result = alerts.run_weekly_trend_digest()

        assert result["reason"] == "nothing_moved"
        assert sent == []
        assert SystemSetting.get(alerts.WEEKLY_DIGEST_KEY, "") == ""
        assert alerts.digest_is_due() is True


def test_the_digest_respects_its_own_switch_not_the_saturation_one(app, sent):
    with app.app_context():
        _user("LGU", "onlywarn@example.com",
              notify_saturation_change=True, notify_weekly_trends=False)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        result = alerts.run_weekly_trend_digest()

        assert result["notified"] == 0
        assert sent == []


# ---------------------------------------------------------------------
# THE AI WORDING
# ---------------------------------------------------------------------

def test_the_ai_writes_the_wording_when_it_is_enabled(app, monkeypatch):
    with app.app_context():
        from app.services import llm_service, recommendation_service

        monkeypatch.setattr(recommendation_service, "llm_recommendations_enabled", lambda: True)
        seen = {}

        def _fake(prompt):
            seen["prompt"] = prompt
            return "Saturation in San Roque climbed sharply this month.", "llm:gemini"

        monkeypatch.setattr(llm_service, "generate_alert_summary", _fake)

        _market_history("Food and Beverage", "San Roque", [4, 40])
        moves = alerts.detect_fluctuations()
        summary, source = alerts._ai_summary(moves, "sme", "warning")

        assert source == "llm:gemini"
        assert summary == "Saturation in San Roque climbed sharply this month."


def test_every_figure_is_in_the_prompt_before_the_model_is_called(app, monkeypatch):
    """The model writes sentences, not numbers.

    An alert is not a place to let a model improvise a figure, so the
    prompt has to carry the computed values and say they are the only
    ones that exist. If that instruction is ever dropped, this fails.
    """
    with app.app_context():
        from app.services import llm_service, recommendation_service

        monkeypatch.setattr(recommendation_service, "llm_recommendations_enabled", lambda: True)
        seen = {}

        def _fake(prompt):
            seen["prompt"] = prompt
            return "ok", "llm:gemini"

        monkeypatch.setattr(llm_service, "generate_alert_summary", _fake)

        _market_history("Food and Beverage", "San Roque", [4, 40])
        moves = alerts.detect_fluctuations()
        alerts._ai_summary(moves, "sme", "warning")

        prompt = seen["prompt"]
        assert "do not invent" in prompt.lower()
        assert "San Roque" in prompt
        assert str(moves[0]["competitors_now"]) in prompt
        assert str(moves[0]["saturation_now"]) in prompt


def test_a_broken_model_falls_back_to_the_deterministic_wording(app, monkeypatch):
    """The alert still goes out. A flaky third party must not be able to
    turn a real market move into silence."""
    with app.app_context():
        from app.services import llm_service, recommendation_service

        monkeypatch.setattr(recommendation_service, "llm_recommendations_enabled", lambda: True)

        def _explode(prompt):
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(llm_service, "generate_alert_summary", _explode)

        _market_history("Food and Beverage", "San Roque", [4, 40])
        moves = alerts.detect_fluctuations()
        summary, source = alerts._ai_summary(moves, "sme", "warning")

        assert source == "rule_based"
        assert "San Roque" in summary
        assert "40" in summary


def test_a_model_that_returns_nothing_usable_falls_back(app, monkeypatch):
    with app.app_context():
        from app.services import llm_service, recommendation_service

        monkeypatch.setattr(recommendation_service, "llm_recommendations_enabled", lambda: True)
        monkeypatch.setattr(llm_service, "generate_alert_summary", lambda prompt: None)

        _market_history("Food and Beverage", "San Roque", [4, 40])
        moves = alerts.detect_fluctuations()
        _summary, source = alerts._ai_summary(moves, "sme", "warning")

        assert source == "rule_based"


def test_the_alert_wording_uses_the_same_provider_order_as_recommendations(app, monkeypatch):
    """The user's own requirement: the notifications use the same AI as
    the Recommendations page, not a parallel implementation.

    Asserted structurally -- generate_alert_summary must try the
    configured provider first and then fall through to the others,
    which is what makes a deployment with LLM_PROVIDER pointed at a
    dead provider still produce real AI text.
    """
    with app.app_context():
        from app.services import llm_service

        app.config["LLM_PROVIDER"] = "openai"
        attempted = []

        def _generator(name, result):
            def _generate(prompt):
                attempted.append(name)
                return result
            return _generate

        monkeypatch.setattr(llm_service, "_GENERATORS", {
            "openai": _generator("openai", None),          # configured, dead
            "anthropic": _generator("anthropic", None),    # no key
            "gemini": _generator("gemini", '{"summary": "written by gemini"}'),
        })

        result = llm_service.generate_alert_summary("anything")

        assert attempted[0] == "openai", "the configured provider must be tried first"
        assert result == ("written by gemini", "llm:gemini")


def test_plain_prose_from_the_model_is_accepted(app, monkeypatch):
    """Unlike a recommendation card there is only one field wanted, so a
    model that answered in sentences rather than JSON has still
    answered. Rejecting that would throw away a usable alert."""
    with app.app_context():
        from app.services import llm_service

        app.config["LLM_PROVIDER"] = "gemini"
        monkeypatch.setattr(llm_service, "_GENERATORS", {
            "gemini": lambda prompt: "Competition in San Roque has risen sharply.",
        })

        result = llm_service.generate_alert_summary("anything")

        assert result == ("Competition in San Roque has risen sharply.", "llm:gemini")


# ---------------------------------------------------------------------
# THE HOOKS, AND FAILING SAFE
# ---------------------------------------------------------------------

def test_a_broken_sweep_is_reported_not_raised(app, monkeypatch):
    """This runs off the back of an LGU upload. An alerting problem must
    never be reported to the uploader as a failed import of data that is
    in fact safely stored."""
    with app.app_context():
        monkeypatch.setattr(alerts, "detect_fluctuations",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        result = alerts.run_market_alert_sweep(force=True)

        assert result["ran"] is False
        assert result["reason"] == "error"


def test_a_failing_email_transport_does_not_lose_the_in_app_alert(app, monkeypatch):
    with app.app_context():
        from app.services import email_service

        monkeypatch.setattr(email_service, "send_market_alert",
                            lambda *a, **k: False)

        user = _user("LGU", "bounce@example.com", notify_saturation_change=True)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        result = alerts.run_market_alert_sweep(force=True)

        assert result["emailed"] == 0
        assert result["notified"] == 1
        assert Notification.query.filter_by(user_id=user.user_id).count() == 1


def test_run_all_alert_sweeps_covers_both_the_sweep_and_the_digest(app, sent):
    """One entry point, so a new caller cannot wire up the fluctuation
    sweep and silently forget the digest."""
    with app.app_context():
        _user("LGU", "both@example.com",
              notify_saturation_change=True, notify_weekly_trends=True)
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        result = alerts.run_all_alert_sweeps(force=True)

        assert result["market"]["ran"] is True
        assert result["digest"]["ran"] is True
        assert sorted(row["kind"] for row in sent) == ["digest", "market"]


def test_a_successful_upload_triggers_the_sweep(app, monkeypatch):
    """The wiring, asserted at the import path rather than by hand.

    There is no scheduler on a free host, so "automatically" means "when
    the data changes". If that hook is ever dropped, the feature silently
    becomes manual.
    """
    with app.app_context():
        from app.services import data_import_service

        called = []
        monkeypatch.setattr(alerts, "run_all_alert_sweeps",
                            lambda *a, **k: called.append(True) or {})

        data_import_service._run_alert_sweeps()

        assert called == [True]


def test_the_upload_path_calls_the_sweep_after_the_commit(app):
    """Order matters and cannot be asserted by mocking alone: the sweep
    re-reads market_data, so running it before the commit would compare
    the new data against itself and find nothing."""
    import inspect

    from app.services import data_import_service

    source = inspect.getsource(data_import_service.process_upload)
    assert source.index("db.session.commit()") < source.index("_run_alert_sweeps()")
    assert source.index("_clear_analytics_caches()") < source.index("_run_alert_sweeps()")


def test_a_broken_sweep_does_not_fail_the_upload(app, monkeypatch):
    with app.app_context():
        from app.services import data_import_service

        monkeypatch.setattr(alerts, "run_all_alert_sweeps",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        # No exception escapes -- that is the whole assertion.
        data_import_service._run_alert_sweeps()


def test_a_places_refresh_that_changed_nothing_does_not_alert(app, monkeypatch):
    """A batch where every combination fell back to a simulated count
    has changed nothing worth telling anyone about."""
    import inspect

    from app.services import market_refresh_service

    source = inspect.getsource(market_refresh_service.refresh_batch)
    assert "if refreshed:" in source
    assert source.index("if refreshed:") < source.index("run_all_alert_sweeps")


# ---------------------------------------------------------------------
# THE EMAIL BODIES
# ---------------------------------------------------------------------

def test_every_notification_email_says_how_to_stop_it(app, monkeypatch):
    """An automated email that does not say how to turn it off is the
    definition of spam, and the switch it names has to be the right
    one -- a reader sent to the wrong switch turns off the wrong alert.
    """
    with app.app_context():
        from app.services import email_service

        app.config["BREVO_API_KEY"] = "xkeysib-test"
        app.config["MAIL_FROM_ADDRESS"] = "smesystem2026@gmail.com"
        bodies = {}

        def _fake_send(to_email, to_name, subject, body):
            bodies[subject] = body
            return True

        monkeypatch.setattr(email_service, "_send", _fake_send)

        expected = {
            email_service.send_market_alert: "Alert me when saturation levels change",
            email_service.send_recommendation_alert:
                "Email notifications for new recommendations",
            email_service.send_trend_digest: "Weekly market trend reports",
        }
        for index, (sender, switch) in enumerate(expected.items()):
            subject = f"subject {index}"
            assert sender("a@example.com", "Ana", subject, "The market moved.") is True
            body = bodies[subject]
            assert switch in body, switch
            assert "Settings > Notifications" in body
            # And no OTHER switch is named, which is the failure that
            # would send someone to turn off the wrong thing.
            for other in expected.values():
                if other != switch:
                    assert other not in body


def test_a_notification_email_is_not_attempted_when_mail_is_unconfigured(app, monkeypatch):
    """A sweep over a hundred recipients on a deployment with no
    transport should be a hundred cheap no-ops, not a hundred failed
    connections."""
    with app.app_context():
        from app.services import email_service

        app.config["BREVO_API_KEY"] = ""
        app.config["GMAIL_ADDRESS"] = ""
        app.config["GMAIL_APP_PASSWORD"] = ""

        attempted = []
        monkeypatch.setattr(email_service, "_send",
                            lambda *a, **k: attempted.append(True) or True)

        assert email_service.send_market_alert("a@example.com", "Ana", "s", "b") is False
        assert attempted == []


# ---------------------------------------------------------------------
# WHO THE MAIL COMES FROM
# ---------------------------------------------------------------------
# "Is there no sender yet?" was the question that started this feature,
# and the answer used to depend on which of three variables happened to
# be set. A BREVO_API_KEY with no GMAIL_ADDRESS resolved to no sender at
# all, and every send was refused for a missing "from" -- which reads in
# the logs like a credential problem and is not one.

def test_the_sender_falls_back_to_the_support_address(app, monkeypatch):
    """A deployment with a Brevo key and nothing else still has a sender,
    and it is the address the Contact Us panel already gives out -- so a
    reader can reply to an alert and reach somebody."""
    from app.config import Config
    from app.services import email_service

    with app.app_context():
        app.config["MAIL_FROM_ADDRESS"] = Config.MAIL_FROM_ADDRESS
        app.config["GMAIL_ADDRESS"] = ""

        _name, address = email_service._from_address()

        assert address == Config.SUPPORT_EMAIL
        assert address, "a deployment must never resolve to an empty sender"


def test_an_explicit_sender_still_wins(app):
    """A verified Brevo sender is sometimes a different address from the
    Gmail account, and that case needs an explicit answer."""
    from app.services import email_service

    with app.app_context():
        app.config["MAIL_FROM_ADDRESS"] = "noreply@tarlaccity.example"
        app.config["GMAIL_ADDRESS"] = "someone.else@gmail.com"

        assert email_service._from_address()[1] == "noreply@tarlaccity.example"


def test_the_settings_page_names_the_sending_address(app, monkeypatch):
    """The page now answers the question directly instead of leaving an
    account to wonder where the email went."""
    from app.services import email_service

    with app.app_context():
        user = _user("SME", "asks@example.com")
        user.set_password("password123")
        db.session.commit()

        monkeypatch.setattr(email_service, "is_configured", lambda: True)
        app.config["MAIL_FROM_ADDRESS"] = "smesystem2026@gmail.com"

        client = app.test_client()
        client.post("/login", data={"email": "asks@example.com",
                                    "password": "password123"})
        body = client.get("/settings?section=notifications").get_data(as_text=True)

        assert "smesystem2026@gmail.com" in body


def test_the_settings_page_says_so_when_email_is_off(app, monkeypatch):
    """And the honest other branch: the switches still do something real,
    because the in-app copy is written either way."""
    from app.services import email_service

    with app.app_context():
        user = _user("SME", "nomailpage@example.com")
        user.set_password("password123")
        db.session.commit()

        monkeypatch.setattr(email_service, "is_configured", lambda: False)

        client = app.test_client()
        client.post("/login", data={"email": "nomailpage@example.com",
                                    "password": "password123"})
        body = client.get("/settings?section=notifications").get_data(as_text=True)

        assert "Email is not configured on this deployment" in body
        assert "Your notifications" in body


# ---------------------------------------------------------------------
# THE DETECTOR READS; IT DOES NOT WRITE
# ---------------------------------------------------------------------

def test_detection_never_creates_a_market_data_row(app):
    """The bug this guards is subtle and was real.

    compute_scores_batch() falls back to a per-pair path for any
    combination missing an lgu_data row, and that path CREATES a
    market_data snapshot -- inventing a simulated competitor count when
    no Places key is set. A detector calling it would write a new state
    while measuring the old one, and then measure what it had just
    written: the "previous" count it compared against was whatever
    random number it had generated a moment earlier.
    """
    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()
        before = MarketData.query.count()

        alerts.detect_fluctuations()

        assert MarketData.query.count() == before


def test_a_barangay_with_no_city_record_is_skipped_not_guessed(app):
    """No lgu_data row means nothing to score against. Skipping is the
    honest answer; an alert nobody can trace back to a recorded state
    is worse than no alert."""
    with app.app_context():
        from datetime import date as _date

        for offset, count in enumerate([4, 40]):
            db.session.add(MarketData(
                industry_type="Food and Beverage", location="Matatalaib",
                competitor_count=count, population_density=7006,
                historical_success_rate=0.63, foot_traffic_index=53,
                average_rent=24500, date_recorded=_date(2026, 1, 1 + offset),
                source="Manual",
            ))
        db.session.commit()

        assert alerts.detect_fluctuations() == []


def test_both_states_are_scored_rather_than_one_inferred(app):
    """The previous saturation must come from the model, not from
    `now_saturation * was_count / now_count`.

    That ratio assumed a linear response to the competitor count. A
    random forest is a step function, so the assumption is simply
    false -- and it also assumed the scored count equals the recorded
    one, which the reconciled cross-source figure need not.
    """
    from app.services import forecasting_service

    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        asked = []
        real = forecasting_service.saturation_for_counts

        def _spy(requests_):
            requests_ = list(requests_)
            asked.extend(requests_)
            return real(requests_)

        forecasting_service.saturation_for_counts = _spy
        try:
            alerts.detect_fluctuations()
        finally:
            forecasting_service.saturation_for_counts = real

        counts = {count for _industry, _location, count in asked}
        assert counts == {4, 40}, \
            "both the old and the new competitor count must be scored"


def test_scoring_a_lower_count_gives_a_lower_saturation(app):
    """The direction the whole feature rests on. If more competitors
    did not mean more saturation, every 'rose'/'fell' in every alert
    would be backwards."""
    from app.services.forecasting_service import saturation_for_counts

    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [30])
        db.session.commit()

        few, many = saturation_for_counts([
            ("Food and Beverage", "San Roque", 3),
            ("Food and Beverage", "San Roque", 120),
        ])

        assert few is not None and many is not None
        assert few < many


def test_scoring_is_repeatable(app):
    """Two identical requests must give the same number. An alert whose
    figures move on their own would be untraceable."""
    from app.services.forecasting_service import saturation_for_counts

    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [30])
        db.session.commit()

        first = saturation_for_counts([("Food and Beverage", "San Roque", 30)])
        second = saturation_for_counts([("Food and Beverage", "San Roque", 30)])

        assert first == second


def test_an_unknown_combination_scores_as_none_without_raising(app):
    from app.services.forecasting_service import saturation_for_counts

    with app.app_context():
        assert saturation_for_counts([("Food and Beverage", "Nowhere", 5)]) == [None]
        assert saturation_for_counts([]) == []


def test_unchanged_pairs_are_never_handed_to_the_scorer(app):
    """Bar 2's remaining job, stated honestly.

    It is no longer the thing that stops drift being reported -- both
    figures are now predicted from the same row varying only the
    competitor count, so a pure feature revision cannot produce a delta
    at all. What bar 2 still does is keep unchanged pairs out of the
    scoring matrix, which on a city-wide re-import is most of the city.
    Without it the sweep would score every combination twice on every
    upload to discover that nothing moved.
    """
    from app.services import forecasting_service

    with app.app_context():
        _market_history("Food and Beverage", "San Roque", [4, 40])   # moved
        _market_history("Construction", "Matatalaib", [12, 12])      # did not
        db.session.commit()

        asked = []
        real = forecasting_service.saturation_for_counts

        def _spy(requests_):
            requests_ = list(requests_)
            asked.extend(requests_)
            return real(requests_)

        forecasting_service.saturation_for_counts = _spy
        try:
            alerts.detect_fluctuations()
        finally:
            forecasting_service.saturation_for_counts = real

        scored_pairs = {(industry, location) for industry, location, _count in asked}
        assert ("Food and Beverage", "San Roque") in scored_pairs
        assert ("Construction", "Matatalaib") not in scored_pairs
