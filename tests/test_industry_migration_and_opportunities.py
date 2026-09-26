"""
tests/test_industry_migration_and_opportunities.py
-----------------------------------------------------
Covers the two behaviors this round added on the data side:

  1. app/services/industry_migration.py -- the one-time move of every
     stored industry_type onto the PSIC sections, the deletion of the
     micro-business rows this SME-scoped system no longer counts, the
     collapse of duplicates that the remap creates, the fact that it
     runs once, and that undo() puts everything back.

  2. app/services/location_opportunity_service.py -- the "where else
     could I open this?" engine behind the Recommendations page: 15+
     ranked barangays for one industry, every figure and every
     reason/risk traceable to a real competitor count, a real PSA
     population, or the model's own output.

Both use the in-memory SQLite TestingConfig, so neither touches real
MySQL data and neither makes a network call.
"""

from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    User, SmeProfile, MarketData, ForecastResult, SystemSetting, IndustryMigrationLog,
)
from app.ml.constants import BUSINESS_TYPES
from app.services import industry_migration as im
from app.services.location_opportunity_service import (
    rank_location_opportunities,
    RANK_WEIGHTS,
    DEFAULT_LIMIT,
)


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _market_row(industry, location, count, recorded=None):
    return MarketData(
        industry_type=industry,
        location=location,
        competitor_count=count,
        population_density=1000,
        historical_success_rate=0.5,
        foot_traffic_index=50,
        average_rent=20000,
        source="Google Places API",
        date_recorded=recorded or date.today(),
    )


def _sme(user_id, industry, location="Poblacion"):
    return SmeProfile(
        user_id=user_id,
        business_name="Test Plan",
        industry_type=industry,
        location=location,
        startup_capital=500000,
        monthly_revenue_est=80000,
        business_stage="startup",
    )


def _a_user():
    user = User(name="Migration Test", email="migration@example.com", role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


# ---------------------------------------------------------------------------
# 1. The industry taxonomy migration
# ---------------------------------------------------------------------------


def test_micro_rows_are_deleted_and_sme_scale_rows_are_remapped(app):
    with app.app_context():
        db.session.add_all([
            _market_row("Retail", "Poblacion", 30),
            _market_row("Sari-Sari Stores", "Poblacion", 58),
            _market_row("Carinderias & Eateries", "Tibag", 41),
            _market_row("Hardware & Construction Supply Stores", "Tibag", 9),
            _market_row("Health & Wellness", "San Nicolas", 7),
        ])
        db.session.commit()

        summary = im.migrate()

        assert summary["deleted_micro"] == 2, "both micro rows should be deleted"
        remaining = {(r.industry_type, r.location) for r in MarketData.query.all()}
        assert ("Sari-Sari Stores", "Poblacion") not in remaining
        assert ("Carinderias & Eateries", "Tibag") not in remaining
        # Non-micro rows keep their data, under a PSIC section name.
        assert (
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles", "Poblacion"
        ) in remaining
        assert ("Human Health and Social Work Activities", "San Nicolas") in remaining
        # A hardware store is SME-scale, so it is remapped, not deleted.
        assert (
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles", "Tibag"
        ) in remaining
        for industry, _ in remaining:
            assert industry in BUSINESS_TYPES


def test_every_remaining_row_speaks_the_new_vocabulary(app):
    with app.app_context():
        for old_name in list(im.OLD_TO_PSIC)[:8] + list(im.MICRO_ONLY)[:5]:
            db.session.add(_market_row(old_name, "Poblacion", 5))
        db.session.commit()

        im.migrate()

        # A few section names are identical in both taxonomies
        # ("Construction", "Manufacturing"), so the test is "is every
        # surviving row a valid PSIC section", not "is its name absent
        # from the old map".
        surviving = {r.industry_type for r in MarketData.query.all()}
        assert surviving <= set(BUSINESS_TYPES), (
            f"rows left speaking the old vocabulary: {surviving - set(BUSINESS_TYPES)}"
        )
        assert not (surviving & im.MICRO_ONLY), "a micro category survived the migration"


def test_duplicates_created_by_the_remap_are_collapsed(app):
    """Retail and Wholesale Trade both become the same PSIC section, so
    in one barangay on one date they would otherwise become two rows for
    the same (industry, location, date) -- and double-count."""
    with app.app_context():
        db.session.add_all([
            _market_row("Retail", "Poblacion", 30),
            _market_row("Wholesale Trade", "Poblacion", 12),
        ])
        db.session.commit()

        summary = im.migrate()

        assert summary["deleted_duplicate"] == 1
        rows = MarketData.query.filter_by(location="Poblacion").all()
        assert len(rows) == 1


def test_unknown_lgu_uploaded_categories_are_left_alone(app):
    """A free-text industry a real LGU upload introduced is that
    office's data -- the migration must not rewrite or delete it."""
    with app.app_context():
        db.session.add(_market_row("Some LGU Uploaded Category", "Aguso", 3))
        db.session.commit()

        im.migrate()

        row = MarketData.query.filter_by(location="Aguso").one()
        assert row.industry_type == "Some LGU Uploaded Category"
        assert row.competitor_count == 3


def test_sme_plans_are_remapped_never_deleted(app):
    """A micro market_data row is disposable; a person's saved plan is
    not. A micro plan gets moved to the closest section instead."""
    with app.app_context():
        user = _a_user()
        db.session.add(_sme(user.user_id, "Sari-Sari Stores"))
        db.session.commit()

        summary = im.migrate()

        assert summary["profiles_remapped"] == 1
        profile = SmeProfile.query.one()
        assert profile.industry_type in BUSINESS_TYPES
        assert profile.business_name == "Test Plan"


def test_migration_runs_only_once(app):
    with app.app_context():
        db.session.add(_market_row("Retail", "Poblacion", 30))
        db.session.commit()

        first = im.migrate()
        second = im.migrate()

        assert first["skipped"] is False
        assert second["skipped"] is True
        assert im.has_run() is True


def test_every_change_is_recorded_for_audit(app):
    with app.app_context():
        db.session.add_all([
            _market_row("Retail", "Poblacion", 30),
            _market_row("Sari-Sari Stores", "Poblacion", 58),
        ])
        db.session.commit()

        im.migrate()

        entries = IndustryMigrationLog.query.all()
        assert len(entries) == 2
        actions = {e.action for e in entries}
        assert actions == {"remapped", "deleted_micro"}
        # A deleted row must be recoverable from its stored JSON.
        deleted = next(e for e in entries if e.action == "deleted_micro")
        assert deleted.row_json and "58" in deleted.row_json


def test_undo_restores_deleted_rows_and_original_names(app):
    with app.app_context():
        user = _a_user()
        db.session.add_all([
            _market_row("Retail", "Poblacion", 30),
            _market_row("Sari-Sari Stores", "Poblacion", 58),
            _sme(user.user_id, "Health & Wellness"),
        ])
        db.session.commit()
        before = {(r.industry_type, r.location, r.competitor_count) for r in MarketData.query.all()}

        im.migrate()
        im.undo()

        after = {(r.industry_type, r.location, r.competitor_count) for r in MarketData.query.all()}
        assert after == before
        assert SmeProfile.query.one().industry_type == "Health & Wellness"
        assert IndustryMigrationLog.query.count() == 0
        assert im.has_run() is False


# ---------------------------------------------------------------------------
# 2. The location opportunity engine behind the Recommendations page
# ---------------------------------------------------------------------------


@pytest.fixture
def sme_plan(app):
    with app.app_context():
        user = _a_user()
        profile = _sme(user.user_id, "Food and Beverage", "Poblacion")
        db.session.add(profile)
        db.session.commit()
        yield profile


def test_shows_a_page_of_ranked_recommendations(app, sme_plan):
    """The page guarantees a minimum of DEFAULT_LIMIT cards, with the
    rest behind "Explore more recommendations"."""
    with app.app_context():
        locations = ["Barangay %02d" % i for i in range(1, 31)]
        result = rank_location_opportunities("Food and Beverage", locations, use_llm=False)

        assert len(result["opportunities"]) == DEFAULT_LIMIT
        assert [o["rank"] for o in result["opportunities"]] == list(
            range(1, len(result["opportunities"]) + 1)
        )
        assert result["recommended_count"] >= len(result["opportunities"])


def test_ranking_weights_market_size_not_just_emptiness(app):
    """Ranking on the model's viability alone puts the emptiest, tiniest
    barangays on top, which is a useless recommendation. The blend must
    prefer a populous barangay over a near-empty one when both are
    similarly uncontested -- see RANK_WEIGHTS."""
    with app.app_context():
        assert pytest.approx(sum(RANK_WEIGHTS.values())) == 1.0
        assert RANK_WEIGHTS["population"] > 0, "market size must count for something"

        locations = ["Barangay %02d" % i for i in range(1, 31)]
        result = rank_location_opportunities("Food and Beverage", locations, use_llm=False)
        top = result["opportunities"][0]
        populations = [o["population"] for o in result["opportunities"] if o["population"]]
        if populations:
            median_population = sorted(populations)[len(populations) // 2]
            assert top["population"] >= median_population, (
                "the top-ranked barangay should not be one of the city's smallest"
            )


def test_every_card_carries_the_real_figures_it_reasons_from(app, sme_plan):
    with app.app_context():
        result = rank_location_opportunities(
            "Food and Beverage", ["Poblacion", "Tibag", "San Nicolas", "Aguso", "Matatalaib"], use_llm=False
        )
        for opp in result["opportunities"]:
            assert opp["location"]
            assert opp["competitor_count"] is not None
            assert 0 <= opp["viability_score"] <= 10
            assert 0 <= opp["saturation_index"] <= 100
            assert opp["opportunity_type"] in (
                "High Opportunity", "Moderate Opportunity", "Low Opportunity", "High Saturation",
            )
            # The storyboard's two lists, and they must never be empty --
            # an empty "Why This Works" is what a fabricated card looks
            # like when its data is missing.
            assert opp["reasons"], f"{opp['location']} has no reasons"
            assert opp["risks"], f"{opp['location']} has no considerations"
            # Percentiles back the ranking up.
            assert set(opp["percentiles"]) == {"viability", "residents_per_business", "population"}


def test_reasons_quote_the_competitor_count_and_city_median(app):
    """Every reason has to be a statement about a real number -- the
    point of the engine is that "3 competitors against a city median of
    11" is checkable, where "great location!" is not."""
    with app.app_context():
        locations = ["Poblacion", "Tibag", "San Nicolas", "Aguso", "Matatalaib", "San Roque"]
        result = rank_location_opportunities("Food and Beverage", locations, use_llm=False)
        top = result["opportunities"][0]

        joined = " ".join(top["reasons"]).lower()
        assert str(top["competitor_count"]) in joined
        assert "city median" in joined
        assert result["city"]["barangays_scored"] == len(locations)


def test_simulated_competitor_counts_are_disclosed_as_a_consideration(app):
    """With no Places API key the counts are simulated, and a card that
    leans on them must say so rather than presenting them as measured."""
    with app.app_context():
        result = rank_location_opportunities("Food and Beverage", ["Poblacion", "Tibag", "Aguso"], use_llm=False)
        risks = " ".join(result["opportunities"][0]["risks"]).lower()
        assert "simulated" in risks


def test_the_plans_own_barangay_is_flagged_not_hidden(app, sme_plan):
    with app.app_context():
        profile = SmeProfile.query.one()
        result = rank_location_opportunities(
            "Food and Beverage", ["Poblacion", "Tibag", "Aguso"], sme_profile=profile, use_llm=False
        )
        own = [o for o in result["opportunities"] if o["location"] == "Poblacion"]
        assert len(own) == 1
        assert own[0]["is_current_plan_location"] is True


def test_recommendations_page_renders_the_opportunity_cards(app, sme_plan):
    with app.app_context():
        client = app.test_client()
        client.post("/login", data={"email": "migration@example.com", "password": "password123"})
        from flask import url_for
        with app.test_request_context():
            url = url_for("sme.recommendations")
        response = client.get(url)

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Where else could you open this?" in body
        assert "Why This Works" in body
        assert "residents per existing business" in body
