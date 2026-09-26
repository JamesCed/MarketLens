"""
tests/test_recommendation_llm.py
------------------------------------
Covers the 3rd item of the "improve the system" request: every SME
forecast now generates its "AI-Powered Recommendation" from the SME's
own input business parameters compared against real market/competitor
data, optionally rewritten by GPT-4o-mini (via OpenRouter) or Claude
when USE_LLM_RECOMMENDATIONS is on, and always falling back to a
deterministic rule-based recommendation otherwise.

None of these tests make a real network call or need the `openai` /
`anthropic` packages installed -- llm_service.py's _get_openai_client()
/ _get_anthropic_client() already catch ImportError internally (no key
configured = no client = no call attempted), and the LLM-enabled tests
below monkeypatch generate_recommendation_json() directly rather than
exercising the real HTTP call.

Uses the in-memory SQLite TestingConfig, like tests/test_app.py.

Run with:
    pytest tests/test_recommendation_llm.py -v
"""

import json

import pytest

from app import create_app
from app.extensions import db
from app.models import SystemSetting, User, SmeProfile
from app.services import recommendation_service as rec_service


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------
# _coerce_llm_payload -- pure JSON-validation logic, no network needed.
# ---------------------------------------------------------------------

def _valid_payload_json():
    return json.dumps({
        "headline": "HIGH OPPORTUNITY -- favorable conditions for market entry.",
        "opportunity_type": "High Opportunity",
        "summary": "A coffee shop in Poblacion looks strong given the low competitor count.",
        "reasons": ["Only 2 competitors on file", "Model confidence is 82%"],
        "risks": ["Brand-awareness building required"],
    })


def test_coerce_llm_payload_valid_json():
    from app.services.llm_service import _coerce_llm_payload

    payload = _coerce_llm_payload(_valid_payload_json())
    assert payload is not None
    assert payload["headline"].startswith("HIGH OPPORTUNITY")
    assert payload["opportunity_type"] == "High Opportunity"
    assert payload["reasons"] == ["Only 2 competitors on file", "Model confidence is 82%"]
    assert payload["risks"] == ["Brand-awareness building required"]


def test_coerce_llm_payload_strips_markdown_code_fence():
    from app.services.llm_service import _coerce_llm_payload

    fenced = "```json\n" + _valid_payload_json() + "\n```"
    payload = _coerce_llm_payload(fenced)
    assert payload is not None
    assert payload["opportunity_type"] == "High Opportunity"


def test_coerce_llm_payload_missing_required_key_is_rejected():
    from app.services.llm_service import _coerce_llm_payload

    incomplete = json.dumps({"headline": "X", "opportunity_type": "High Opportunity", "summary": "Y"})
    assert _coerce_llm_payload(incomplete) is None


def test_coerce_llm_payload_reasons_must_be_a_list():
    from app.services.llm_service import _coerce_llm_payload

    bad_shape = json.dumps({
        "headline": "X", "opportunity_type": "High Opportunity", "summary": "Y",
        "reasons": "not a list", "risks": [],
    })
    assert _coerce_llm_payload(bad_shape) is None


def test_coerce_llm_payload_malformed_json_returns_none():
    from app.services.llm_service import _coerce_llm_payload

    assert _coerce_llm_payload("{not valid json at all") is None


def test_coerce_llm_payload_empty_input_returns_none():
    from app.services.llm_service import _coerce_llm_payload

    assert _coerce_llm_payload("") is None
    assert _coerce_llm_payload(None) is None


# ---------------------------------------------------------------------
# serialize_recommendation / parse_recommendation -- storage round trip
# in forecast_result.recommendation's single TEXT column, including
# backward compatibility with the OLD plain-text format.
# ---------------------------------------------------------------------

def test_serialize_then_parse_round_trip():
    original = {
        "headline": "MODERATE OPPORTUNITY -- viable with a solid differentiation strategy.",
        "opportunity_type": "Moderate Opportunity",
        "summary": "A retail store in Tibag is moderately saturated.",
        "reasons": ["Only 4 competitors found in Tibag", "Model confidence for this estimate is 71%"],
        "risks": ["Initial brand-awareness building will still be required"],
        "generated_by": "rule_based",
    }
    stored = rec_service.serialize_recommendation(original)
    assert stored.startswith("{")

    parsed = rec_service.parse_recommendation(stored)
    assert parsed == original


def test_parse_recommendation_legacy_plain_text_format():
    # Exactly the shape the OLD rule_based_text() in a prior version of
    # this module produced -- a real row seeded before this change
    # would look like this.
    legacy_text = (
        "HIGH SATURATION -- avoid this market or pivot to a clearly differentiated niche.\n\n"
        "A retail business in Matatalaib is currently classified as 'Saturated' saturation (88%), "
        "with a viability score of 1.2/10.\n\n"
        "Why: 40 existing competitor(s) already serve this area; model confidence for this estimate is 90%.\n\n"
        "Considerations: the market is already 88% saturated in this area; expect strong price/brand "
        "competition from existing businesses."
    )
    parsed = rec_service.parse_recommendation(legacy_text)
    assert parsed["headline"].startswith("HIGH SATURATION")
    assert parsed["opportunity_type"] == "High Saturation"
    assert "classified as 'Saturated'" in parsed["summary"]
    assert len(parsed["reasons"]) == 2
    assert len(parsed["risks"]) == 2
    assert parsed["generated_by"] == "legacy"


def test_parse_recommendation_empty_or_none_returns_shaped_default():
    for value in (None, "", "   "):
        parsed = rec_service.parse_recommendation(value)
        assert set(parsed.keys()) == {"headline", "opportunity_type", "summary", "reasons", "risks", "generated_by"}
        assert parsed["reasons"] == []
        assert parsed["risks"] == []


# ---------------------------------------------------------------------
# Small display-only helpers.
# ---------------------------------------------------------------------

def test_competition_level_label_tiers():
    assert rec_service.competition_level_label(0) == "Very Low (0 competitors)"
    assert rec_service.competition_level_label(1) == "Low (1 competitors)"
    assert rec_service.competition_level_label(5) == "Moderate (5 competitors)"
    assert rec_service.competition_level_label(20) == "High (20 competitors)"


def test_estimate_breakeven_months():
    assert rec_service.estimate_breakeven_months(0, 50000) is None
    assert rec_service.estimate_breakeven_months(500000, 0) is None
    assert rec_service.estimate_breakeven_months(500000, 50000) == 10


# ---------------------------------------------------------------------
# build_recommendation() -- rule-based default, LLM opt-in (mocked).
# ---------------------------------------------------------------------

def _sample_context():
    return {
        "business_name": "Juan's Coffee Corner",
        "industry_type": "Food & Beverage",
        "location": "Poblacion",
        "business_stage": "startup",
        "years_in_operation": 0.0,
        "startup_capital": 300000.0,
        "employee_count": 3,
        "monthly_revenue_est": 40000.0,
        "saturation_index": 35.0,
        "cluster_label": "Moderate",
        "viability_score": 6.5,
        "confidence_level": 80.0,
        "population": 12000,
        "competitor_count": 3,
        "competitor_simulated": True,
        "competitor_sample": [],
    }


def test_build_recommendation_is_rule_based_by_default(app):
    with app.app_context():
        SystemSetting.set("use_llm_recommendations", "false")
        recommendation = rec_service.build_recommendation(_sample_context())
        assert recommendation["generated_by"] == "rule_based"
        assert recommendation["opportunity_type"] == "Moderate Opportunity"
        assert len(recommendation["reasons"]) >= 2
        assert len(recommendation["risks"]) >= 2


def test_build_recommendation_uses_llm_when_enabled_and_available(app, monkeypatch):
    with app.app_context():
        SystemSetting.set("use_llm_recommendations", "true")

        mock_payload = {
            "headline": "MODERATE OPPORTUNITY -- GPT-written verdict.",
            "opportunity_type": "Moderate Opportunity",
            "summary": "GPT-generated summary for this plan.",
            "reasons": ["GPT reason one", "GPT reason two"],
            "risks": ["GPT risk one"],
            "generated_by": "llm:openai",
        }
        monkeypatch.setattr(
            "app.services.llm_service.generate_recommendation_json",
            lambda context: mock_payload,
        )

        recommendation = rec_service.build_recommendation(_sample_context())
        assert recommendation == mock_payload


def test_build_recommendation_falls_back_to_rule_based_when_llm_unavailable(app, monkeypatch):
    with app.app_context():
        SystemSetting.set("use_llm_recommendations", "true")

        # Simulates: no API key configured, network failure, or a
        # malformed response -- generate_recommendation_json() always
        # returns None in that case (see llm_service.py), never raises.
        monkeypatch.setattr(
            "app.services.llm_service.generate_recommendation_json",
            lambda context: None,
        )

        recommendation = rec_service.build_recommendation(_sample_context())
        assert recommendation["generated_by"] == "rule_based"


# ---------------------------------------------------------------------
# End-to-end: generate_forecast_for_profile() persists a valid,
# parseable structured recommendation either way.
# ---------------------------------------------------------------------

def test_generate_forecast_for_profile_stores_parseable_recommendation(app):
    with app.app_context():
        from app.services.forecasting_service import generate_forecast_for_profile

        user = User(name="Rec Test Owner", email="rectestowner@example.com", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

        profile = SmeProfile(
            user_id=user.user_id,
            business_name="Test Sari-Sari Store",
            industry_type="Retail",
            location="Poblacion",
            startup_capital=250000,
            monthly_revenue_est=30000,
            employee_count=2,
        )
        db.session.add(profile)
        db.session.commit()

        forecast = generate_forecast_for_profile(profile)

        # Stored value is JSON (this version's format), not the old
        # freeform text -- and it parses straight back into the full
        # structured shape every template expects.
        stored = json.loads(forecast.recommendation)
        assert set(stored.keys()) == {"headline", "opportunity_type", "summary", "reasons", "risks", "generated_by"}

        parsed = rec_service.parse_recommendation(forecast.recommendation)
        assert parsed["headline"]
        assert parsed["opportunity_type"] in (
            "High Opportunity", "Moderate Opportunity", "Low Opportunity", "High Saturation",
        )
        # No OPENAI_API_KEY/ANTHROPIC_API_KEY is configured in the test
        # environment, so even though use_llm_recommendations now
        # defaults to true, the LLM client never initializes and this
        # silently falls back to the rule-based generator.
        assert parsed["generated_by"] == "rule_based"
