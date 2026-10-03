"""
tests/test_forecast_transcript.py
------------------------------------
"This should not be rule-based explanation. It should be AI
transcription": the trained models compute the forecast, and Gemini
transcribes that computation. These tests pin how:

  * the DEDICATED transcription call (llm_service.transcribe_forecast)
    hands Gemini the whole computation -- inputs, market stage, costs,
    the seven-part scorecard, the baseline and signed drivers, the
    confidences, the break-even window -- and asks Gemini first;
  * a draft that quotes a figure it was not given is sent back ONCE
    with the offending figures listed; a rewrite that still has some is
    pruned sentence by sentence and kept only if it is still a
    transcript (three or more sentences, the viability stated);
    otherwise the rule-based model summary stands;
  * build_recommendation asks for that dedicated transcript when the
    one call's transcript is missing or rejected;
  * a STORED forecast with only the model summary is upgraded through
    POST /api/forecasts/<id>/transcript -- owner only, nothing but the
    "explanation" key changes, a failure backs off for 15 minutes;
  * the page renders the upgrade hook only when Gemini can be asked,
    and the badges say who wrote what;
  * Gemini is the configured default, and an sk- key in its slot sends
    the calls to OpenAI instead of to Google.

No network: generators and requests.post are monkeypatched.

Run with:
    pytest tests/test_forecast_transcript.py -v
"""

import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.extensions import db
from app.models import ForecastResult, SmeProfile, SystemSetting, User
from app.services import llm_service
from app.services import recommendation_service as rec_service

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GEMINI_KEY = "AQ.Ab_test_transcript_key"
MODEL = "gemini-3.8-flash"


@pytest.fixture
def app(monkeypatch):
    # The switch is decided per test; an operator's shell must not.
    monkeypatch.delenv("USE_LLM_RECOMMENDATIONS", raising=False)
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        app.config["GEMINI_API_KEY"] = ""
        app.config["OPENAI_API_KEY"] = ""
        app.config["ANTHROPIC_API_KEY"] = ""
        app.config["GEMINI_MODEL"] = MODEL
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _clean_llm_state():
    llm_service.reset_client_cache()
    yield
    llm_service.reset_client_cache()


def _gemini_on(app, monkeypatch, key=GEMINI_KEY):
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
    app.config["GEMINI_API_KEY"] = key
    app.config["LLM_PROVIDER"] = "gemini"


# ---------------------------------------------------------------------
# A payload in the contract's shape, with all seven scorecard parts
# ---------------------------------------------------------------------

SEVEN_COMPONENTS = [
    {"key": "market_opportunity", "label": "Market opportunity", "aspect": "Market",
     "score": 0.58, "weight": 0.40, "points": 23.2, "inputs": "industry, sub-category, location"},
    {"key": "capital_adequacy", "label": "Capital adequacy", "aspect": "Financial",
     "score": 0.69, "weight": 0.20, "points": 13.8, "inputs": "capital, employees, rent"},
    {"key": "price_coverage", "label": "Price coverage", "aspect": "Financial",
     "score": 0.69, "weight": 0.10, "points": 6.9, "inputs": "price list"},
    {"key": "operating_experience", "label": "Operating experience", "aspect": "Technical",
     "score": 0.0, "weight": 0.08, "points": 0.0, "inputs": "business stage"},
    {"key": "staffing", "label": "Staffing", "aspect": "Technical",
     "score": 1.0, "weight": 0.07, "points": 7.0, "inputs": "employees"},
    {"key": "offering_definition", "label": "Offering defined", "aspect": "Product",
     "score": 1.0, "weight": 0.07, "points": 7.0, "inputs": "offering, price list"},
    {"key": "differentiation", "label": "Differentiation", "aspect": "Product",
     "score": 0.0, "weight": 0.08, "points": 0.0, "inputs": "innovation idea"},
]


def _payload():
    return {
        "version": "plan_v1",
        "market": {"saturation_index": 42.1, "industry_saturation_index": 48.0, "cluster_label": "Moderate",
                   "competitor_count": 7, "confidence": 88.2, "model_version": "rf_v1"},
        "plan": {"viability_index": 61.3, "viability_score": 6.1, "confidence": 84.0,
                 "model_version": "plan_rf_v1", "scorecard_index": 57.9},
        "inputs": {"capital": 500000.0, "employee_count": 3, "business_stage": "startup",
                   "years_in_operation": 0.0, "priced_item_count": 5, "average_price": 85.0,
                   "has_offering_description": True, "has_innovation_idea": False,
                   "industry_type": "Food and Beverage", "subcategory_label": "Bakery / Pastries",
                   "location": "Tibag", "population": 12450, "residents_per_business": 1556.3},
        "financials": {"monthly_rent": 15000.0, "daily_wage": 590.0, "monthly_payroll": 46020.0,
                       "monthly_fixed_cost": 61020.0, "capital_runway_months": 8.2, "ramp_up_months": 11.9,
                       "capital_adequacy": 0.69, "gross_margin": 0.4, "operating_days": 26,
                       "required_daily_sales": 69.1, "daily_sales_ceiling": 222.3,
                       "break_even": {"low_months": 10, "high_months": 15, "label": "10-15 months"}},
        "components": copy.deepcopy(SEVEN_COMPONENTS),
        "baseline": 52.0,
        "drivers": [
            {"key": "market", "label": "Market saturation (industry · sub-category · location)", "points": 8.3},
            {"key": "capital", "label": "Capital vs. running costs", "points": -5.1},
            {"key": "pricing", "label": "Pricing (price list)", "points": 3.4},
            {"key": "staffing", "label": "Staffing", "points": 0.2},
        ],
    }


def _context(**overrides):
    context = {
        "business_name": "Pan de Tibag", "industry_type": "Food and Beverage",
        "subcategory": "bakery", "subcategory_label": "Bakery / Pastries",
        "product_offering": "Pandesal and ensaymada", "innovation_idea": "",
        "offering_items": [], "price_summary": None,
        "location": "Tibag", "business_stage": "startup", "years_in_operation": 0.0,
        "capital": 500000.0, "startup_capital": 500000.0, "employee_count": 3,
        "saturation_index": 42.1, "industry_saturation_index": 48.0, "cluster_label": "Moderate",
        "viability_score": 6.1, "confidence_level": 84.0, "population": 12450, "competitor_count": 7,
        "competitor_simulated": False, "competitor_sample": [], "subcategory_analysis": None,
        "forecast": _payload(),
    }
    context.update(overrides)
    return context


# Six sentences, every figure one the computation block shows.
TRANSCRIPT = (
    "The trained models rate this bakery plan's viability at 6.1/10, with 84% confidence. "
    "Tibag is 42.1% saturated for this kind of business, which puts it in the Moderate tier. "
    "Starting from a baseline of 52, market saturation added 8.3 points while capital vs. running costs "
    "took off 5.1 points.\n\n"
    "Your fixed costs come to ₱61,020 a month, so your ₱500,000 capital lasts 8.2 months against an "
    "11.9-month ramp-up. At ₱85 a sale you need about 69.1 sales a day, well within the 222.3 the area "
    "can support. Plan for a break-even window of 10-15 months and keep some capital in reserve."
)

INVENTED_SENTENCE = " Expect about ₱2,400,000 in sales in your first year."


def _reply(text):
    return json.dumps({"transcript": text})


def _generators(monkeypatch, replies):
    """Every generator replaced by one that records its prompt and pops
    its next reply from replies[name] (a list; None = no answer)."""
    calls = []

    def make(name):
        def generate(prompt):
            calls.append((name, prompt))
            queue = replies.get(name) or []
            return queue.pop(0) if queue else None
        return generate

    for name in ("openai", "anthropic", "gemini"):
        monkeypatch.setitem(llm_service._GENERATORS, name, make(name))
    return calls


# ---------------------------------------------------------------------
# 1. The block: the whole computation, and nothing else to quote
# ---------------------------------------------------------------------

def test_the_prompt_hands_gemini_the_whole_computation(app):
    with app.app_context():
        prompt = llm_service._transcript_prompt(llm_service._transcript_lines(_context()))

    # The owner's inputs.
    for piece in ("Capital: ₱500,000", "Paid staff: 3 employee(s)", "Stage: a startup, not yet opened",
                  "Price list: 5 priced item(s); average price ₱85",
                  "Industry: Food and Beverage; sub-category: Bakery / Pastries", "Location: Tibag",
                  'What they will sell or serve (the owner\'s words): "Pandesal and ensaymada"',
                  "What makes the business different: not described yet"):
        assert piece in prompt, piece
    # The market stage.
    assert "Market Saturation Index: 42.1% (tier: Moderate)" in prompt
    assert "before the sub-category adjustment: 48%" in prompt
    assert "Competitors counted: 7" in prompt and "Market-stage confidence: 88.2%" in prompt
    assert "residents per business: 1,556.3" in prompt
    # The derived financials.
    for piece in ("Monthly rent for a site in Tibag: ₱15,000", "Monthly payroll: ₱46,020 (3 employee(s) × ₱590 × 26 days)",
                  "Monthly fixed cost: ₱61,020", "Capital runway: 8.2 months", "ramp-up before steady sales: 11.9 months",
                  "Capital adequacy score", ": 0.69", "Gross margin assumed: 40%",
                  "Sales a day needed just to cover the fixed costs: 69.1",
                  "plausibly support: 222.3", "Break-even window: 10-15 months"):
        assert piece in prompt, piece
    # All seven scorecard parts, as score × weight = points.
    assert "Market opportunity (Market; from industry, sub-category, location): 0.58 × 0.4 = 23.2 points" in prompt
    scorecard = prompt.split("PLAN SCORECARD", 1)[1].split("PLAN MODEL", 1)[0]
    assert len(re.findall(r" × .+ = .+ points", scorecard)) == 7
    for component in SEVEN_COMPONENTS:
        assert component["label"] in scorecard
    # The trained model's baseline and signed drivers summing to the PVI.
    assert "Baseline (the average plan the model learned from): 52" in prompt
    assert "Capital vs. running costs: -5.1 points" in prompt
    assert "Market saturation (industry · sub-category · location): +8.3 points" in prompt
    assert "Plan Viability Index: 61.3 out of 100" in prompt and "viability score of 6.1/10" in prompt
    assert "Plan-model confidence: 84%" in prompt
    # The rules.
    assert "5-8 plain-language sentences" in prompt
    assert "copied exactly as written" in prompt and "round it differently" in prompt
    assert "in digits" in prompt and "₱ sign" in prompt and "no markdown" in prompt
    assert '{"transcript"' in prompt


def test_the_block_labels_carry_no_figures_of_their_own(app):
    """Every number the transcript may quote is a number in the block, so
    the block's own labels must add none -- not as digits, not as words."""
    with app.app_context():
        lines = llm_service._transcript_lines(_context())
    for text, kind in lines:
        if kind == "head":
            assert not re.search(r"\d", text), text
            assert rec_service._numbers_in(text) == [], text
    allowed = rec_service.transcript_allowed_figures(
        [(text, kind == "percent") for text, kind in lines if kind != "head"], _payload())
    # None of these is a payload figure; each would be easy to leak from a
    # label ("Stage 2", "the two stages", "/100").
    for value in (2.0, 4.0, 100.0):
        assert value not in allowed[0], f"{value} leaked into the block from a label"
    # The wording used when a figure is absent adds nothing either.
    with app.app_context():
        bare = llm_service._transcript_lines(_context(employee_count=0, product_offering="",
                                                      forecast={**_payload(), "inputs": {
                                                          **_payload()["inputs"], "employee_count": 0,
                                                          "priced_item_count": 0, "capital": 0.0}}))
    for text, kind in bare:
        if text.startswith(("Paid staff", "Capital:", "Price list", "What they will", "What makes", "Stage")):
            assert rec_service._numbers_in(text) == [], text


def test_what_the_block_showed_grounds_and_nothing_else_does(app):
    with app.app_context():
        lines = llm_service._transcript_lines(_context())
    allowed = rec_service.transcript_allowed_figures(
        [(text, kind == "percent") for text, kind in lines if kind != "head"], _payload())

    def ungrounded(text):
        return rec_service.review_transcript(text, allowed, _payload())["ungrounded"]

    assert ungrounded(TRANSCRIPT) == []
    # Shown only in the transcript block: the scorecard and the adequacy.
    assert ungrounded("Market opportunity earned 23.2 points; the capital adequacy score is 0.69.") == []
    # The margin is shown as a percentage, so it grounds only a percentage.
    assert ungrounded("a 40% gross margin") == []
    assert ungrounded("40 months to break even") == ["40"]
    assert ungrounded("Expect ₱2,400,000 in your first year.") == ["2,400,000"]
    assert ungrounded("Break even in 4 months.") == ["4"]


# ---------------------------------------------------------------------
# 2. transcribe_forecast: Gemini first, retry, prune, fall back
# ---------------------------------------------------------------------

def test_gemini_writes_the_transcript_first(app, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "sk-proj-0000000000000000"
        calls = _generators(monkeypatch, {"gemini": [_reply(TRANSCRIPT)], "openai": [_reply(TRANSCRIPT)]})

        result = llm_service.transcribe_forecast(_context())

    assert [name for name, _ in calls] == ["gemini"]
    assert result == {"text": TRANSCRIPT, "generated_by": f"llm:gemini:{MODEL}"}
    assert "\n\n" in result["text"], "the two paragraphs are kept apart"


def test_an_sk_key_is_never_sent_to_google(app, monkeypatch):
    with app.app_context():
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
        app.config["LLM_PROVIDER"] = "gemini"
        app.config["OPENAI_API_KEY"] = "sk-or-v1-0000000000000000"
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"
        app.config["OPENAI_MODEL"] = "openai/gpt-4o-mini"
        calls = _generators(monkeypatch, {"openai": [_reply(TRANSCRIPT)]})

        result = llm_service.transcribe_forecast(_context())
        available = llm_service.gemini_transcription_available()

    assert [name for name, _ in calls] == ["openai"]
    assert result["generated_by"] == "llm:openai:openai/gpt-4o-mini"
    assert available is False


def test_a_draft_with_an_invented_figure_is_sent_back_once_with_feedback(app, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        calls = _generators(monkeypatch, {
            "gemini": [_reply(TRANSCRIPT + INVENTED_SENTENCE), _reply(TRANSCRIPT)],
            "openai": [_reply(TRANSCRIPT)],
        })
        result = llm_service.transcribe_forecast(_context())

    assert [name for name, _ in calls] == ["gemini", "gemini"], "the retry goes back to Gemini, once"
    retry_prompt = calls[1][1]
    assert "YOUR PREVIOUS DRAFT WAS REJECTED" in retry_prompt
    assert "2,400,000" in retry_prompt, "the offending figure is named"
    assert "Only figures that appear in the FORECAST COMPUTATION may appear" in retry_prompt
    assert "YOUR PREVIOUS DRAFT" not in calls[0][1]
    assert result == {"text": TRANSCRIPT, "generated_by": f"llm:gemini:{MODEL}"}


def test_a_rewrite_that_still_invents_is_pruned_sentence_by_sentence(app, monkeypatch):
    still_wrong = TRANSCRIPT + INVENTED_SENTENCE
    with app.app_context():
        _gemini_on(app, monkeypatch)
        calls = _generators(monkeypatch, {"gemini": [_reply(still_wrong), _reply(still_wrong)]})
        result = llm_service.transcribe_forecast(_context())

    assert len(calls) == 2
    assert result["generated_by"] == f"llm:gemini:{MODEL}"
    assert "2,400,000" not in result["text"]
    assert result["text"] == TRANSCRIPT, "only the sentence with the invented figure is dropped"


def test_pruning_that_leaves_too_little_falls_back(app, monkeypatch):
    """Two good sentences and three with invented figures: what survives
    is not a transcript, so the model summary stands."""
    thin = ("The trained models rate this plan's viability at 6.1/10. Tibag is 42.1% saturated. "
            "You will sell 900 items a day. Profit is ₱75,000 a month. Break even in 4 months.")
    with app.app_context():
        _gemini_on(app, monkeypatch)
        _generators(monkeypatch, {"gemini": [_reply(thin), _reply(thin)]})
        assert llm_service.transcribe_forecast(_context()) is None


def test_pruning_that_loses_the_viability_falls_back(app, monkeypatch):
    lost = ("The plan's viability is 9.2/10. Tibag is 42.1% saturated, in the Moderate tier. "
            "Your capital lasts 8.2 months against an 11.9-month ramp-up. "
            "Plan for a break-even window of 10-15 months.")
    with app.app_context():
        _gemini_on(app, monkeypatch)
        _generators(monkeypatch, {"gemini": [_reply(lost), _reply(lost)]})
        assert llm_service.transcribe_forecast(_context()) is None


def test_no_answer_at_all_is_none(app, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        calls = _generators(monkeypatch, {})
        assert llm_service.transcribe_forecast(_context()) is None
    assert [name for name, _ in calls] == ["gemini", "openai", "anthropic"]


def test_no_payload_means_no_transcript_call(app, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        calls = _generators(monkeypatch, {"gemini": [_reply(TRANSCRIPT)]})
        assert llm_service.transcribe_forecast(_context(forecast=None)) is None
    assert calls == []


@pytest.mark.parametrize("raw, expected", [
    (json.dumps({"transcript": "**One.** Two.\n\n- Three."}), "One. Two.\n\nThree."),
    ("```json\n" + json.dumps({"transcript": "A. B."}) + "\n```", "A. B."),
    ("Plain prose. Not JSON.", "Plain prose. Not JSON."),
    ('{"transcript": "broken', None),
    (json.dumps({"something_else": "x"}), None),
])
def test_the_reply_is_read_as_plain_prose(raw, expected):
    assert llm_service._coerce_transcript(raw) == expected


# ---------------------------------------------------------------------
# 3. build_recommendation asks for the dedicated transcript
# ---------------------------------------------------------------------

def _one_call(explanation):
    body = {"headline": "MODERATE OPPORTUNITY -- viable.", "opportunity_type": "Moderate Opportunity",
            "summary": "S", "reasons": ["r"], "risks": ["k"]}
    if explanation is not None:
        body["explanation"] = explanation
    return {**llm_service._coerce_llm_payload(json.dumps(body)), "generated_by": "llm:gemini", "model": MODEL}


@pytest.mark.parametrize("explanation", [TRANSCRIPT + INVENTED_SENTENCE, None])
def test_a_rejected_or_missing_one_call_transcript_asks_the_dedicated_call(app, monkeypatch, explanation):
    asked = []
    with app.app_context():
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: _one_call(explanation))
        monkeypatch.setattr(llm_service, "transcribe_forecast",
                            lambda c: asked.append(c) or {"text": TRANSCRIPT, "generated_by": f"llm:gemini:{MODEL}"})
        recommendation = rec_service.build_recommendation(_context())

    assert len(asked) == 1
    assert recommendation["explanation"] == {"text": TRANSCRIPT, "generated_by": f"llm:gemini:{MODEL}"}
    assert recommendation["forecast"] == _payload()


def test_a_grounded_one_call_transcript_needs_no_second_call(app, monkeypatch):
    grounded = rec_service._rule_based_explanation(_context())["text"]
    with app.app_context():
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: _one_call(grounded))

        def must_not_run(_context):
            raise AssertionError("the dedicated call was made although the one call's transcript passed")

        monkeypatch.setattr(llm_service, "transcribe_forecast", must_not_run)
        recommendation = rec_service.build_recommendation(_context())

    assert recommendation["explanation"]["generated_by"] == f"llm:gemini:{MODEL}"


def test_when_every_provider_failed_only_a_gemini_key_earns_a_second_try(app, monkeypatch):
    asked = []
    with app.app_context():
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: None)
        monkeypatch.setattr(llm_service, "transcribe_forecast", lambda c: asked.append(c) or None)

        # No usable key anywhere: nothing would differ the second time.
        recommendation = rec_service.build_recommendation(_context())
        assert asked == []
        assert recommendation["explanation"]["generated_by"] == "rule-based"

        # A Gemini key: the shorter dedicated call may succeed.
        app.config["GEMINI_API_KEY"] = GEMINI_KEY
        recommendation = rec_service.build_recommendation(_context())
        assert len(asked) == 1
        assert recommendation["explanation"]["generated_by"] == "rule-based", "it failed, so the summary stands"

        # ...and the failure is stamped, so the page showing this fresh
        # forecast does not fire another (doomed) Gemini call at once.
        assert recommendation["explanation"]["transcript_failed_at"]
        stored = rec_service.parse_recommendation(rec_service.serialize_recommendation(recommendation))
        assert stored["explanation"]["transcript_failed_at"]
        assert rec_service.transcript_upgrade_due(stored) is False


def test_the_switch_off_asks_nobody(app, monkeypatch):
    with app.app_context():
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
        app.config["GEMINI_API_KEY"] = GEMINI_KEY

        def must_not_run(_context):
            raise AssertionError("the LLM was asked although the switch is off")

        monkeypatch.setattr(llm_service, "transcribe_forecast", must_not_run)
        monkeypatch.setattr(llm_service, "generate_recommendation_json", must_not_run)
        recommendation = rec_service.build_recommendation(_context())
    assert recommendation["explanation"]["generated_by"] == "rule-based"


# ---------------------------------------------------------------------
# 4. Upgrading a stored forecast: POST /api/forecasts/<id>/transcript
# ---------------------------------------------------------------------

def _user(email, role="SME"):
    user = User(name=email.split("@")[0], email=email, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _client(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
    return client


@pytest.fixture
def stored(app, monkeypatch):
    """One SME's plan with a forecast whose transcript is the rule-based
    model summary -- generated the real way, with the LLM switched off,
    the state of every forecast made before a Gemini key was set."""
    from app.services.forecasting_service import generate_forecast_for_profile

    with app.app_context():
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
        owner = _user("owner@transcript.test")
        _user("other@transcript.test")
        _user("lgu@transcript.test", role="LGU")
        plan = SmeProfile(user_id=owner.user_id, business_name="Pan de Tibag", industry_type="Food and Beverage",
                          location="Tibag", business_stage="startup", startup_capital=500000, employee_count=3)
        db.session.add(plan)
        db.session.commit()
        forecast = generate_forecast_for_profile(plan)
        monkeypatch.delenv("USE_LLM_RECOMMENDATIONS", raising=False)
        raw = forecast.recommendation
        assert json.loads(raw)["explanation"]["generated_by"] == "rule-based"
        return {"forecast_id": forecast.forecast_id, "sme_id": plan.sme_id, "raw": raw}


def _stored_json(forecast_id):
    db.session.expire_all()
    return json.loads(db.session.get(ForecastResult, forecast_id).recommendation)


def _fake_transcriber(monkeypatch, result):
    seen = []

    def transcribe(context):
        seen.append(context)
        return result

    monkeypatch.setattr(llm_service, "transcribe_forecast", transcribe)
    return seen


def test_the_owner_gets_gemini_s_transcript_and_only_the_explanation_changes(app, stored, monkeypatch):
    transcript = {"text": "A transcript.\n\nIn two paragraphs.", "generated_by": f"llm:gemini:{MODEL}"}
    with app.app_context():
        _gemini_on(app, monkeypatch)
        seen = _fake_transcriber(monkeypatch, transcript)
        before = json.loads(stored["raw"])

        response = _client(app, "owner@transcript.test").post(f"/api/forecasts/{stored['forecast_id']}/transcript")
        data = response.get_json()
        after = _stored_json(stored["forecast_id"])

    assert response.status_code == 200
    assert data["ok"] is True and data["text"] == transcript["text"]
    assert data["generated_by"] == f"llm:gemini:{MODEL}"
    assert "Transcribed by Gemini" in data["badge_html"] and MODEL in data["badge_html"]
    # The context was rebuilt from the STORED payload, not re-scored.
    assert seen[0]["forecast"] == before["forecast"]
    assert after["explanation"] == transcript
    assert {k: v for k, v in after.items() if k != "explanation"} == \
        {k: v for k, v in before.items() if k != "explanation"}


def test_an_already_transcribed_forecast_is_not_asked_again(app, stored, monkeypatch):
    transcript = {"text": "Already done.", "generated_by": f"llm:gemini:{MODEL}"}
    with app.app_context():
        _gemini_on(app, monkeypatch)
        row = db.session.get(ForecastResult, stored["forecast_id"])
        row.recommendation = rec_service.store_transcript(row.recommendation, transcript)
        db.session.commit()
        seen = _fake_transcriber(monkeypatch, None)
        data = _client(app, "owner@transcript.test").post(
            f"/api/forecasts/{stored['forecast_id']}/transcript").get_json()
    assert seen == []
    assert data["ok"] is True and data["text"] == "Already done."


@pytest.mark.parametrize("email", ["other@transcript.test", "lgu@transcript.test"])
def test_nobody_but_the_owner_can_ask(app, stored, monkeypatch, email):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        seen = _fake_transcriber(monkeypatch, {"text": "x", "generated_by": "llm:gemini"})
        response = _client(app, email).post(f"/api/forecasts/{stored['forecast_id']}/transcript")
        assert response.status_code in (403, 404)
        assert response.status_code == 404, "the same answer as a forecast that does not exist"
        assert _stored_json(stored["forecast_id"]) == json.loads(stored["raw"])
    assert seen == []


def test_anonymous_callers_are_turned_away(app, stored, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        seen = _fake_transcriber(monkeypatch, {"text": "x", "generated_by": "llm:gemini"})
        response = app.test_client().post(f"/api/forecasts/{stored['forecast_id']}/transcript")
    assert response.status_code in (302, 401)
    assert seen == []


def test_an_unknown_forecast_is_404(app, stored, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        response = _client(app, "owner@transcript.test").post("/api/forecasts/999999/transcript")
    assert response.status_code == 404


def test_a_trashed_plan_s_forecast_is_404(app, stored, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        client = _client(app, "owner@transcript.test")
        client.post(f"/home/plans/{stored['sme_id']}/trash")
        response = client.post(f"/api/forecasts/{stored['forecast_id']}/transcript")
    assert response.status_code == 404


def test_a_failure_is_stamped_and_backs_off_for_15_minutes(app, stored, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        seen = _fake_transcriber(monkeypatch, None)
        client = _client(app, "owner@transcript.test")
        url = f"/api/forecasts/{stored['forecast_id']}/transcript"

        first = client.post(url).get_json()
        after = _stored_json(stored["forecast_id"])
        stamp = after["explanation"]["transcript_failed_at"]
        second = client.post(url).get_json()

    assert first == {"ok": False, "reason": "failed"}
    assert second == {"ok": False, "reason": "retry_later"}
    assert len(seen) == 1, "the second request inside the window spent no call"
    # Stamped in UTC; the summary itself and every other key unchanged.
    when = datetime.fromisoformat(stamp)
    assert when.tzinfo is not None and abs(datetime.now(timezone.utc) - when) < timedelta(minutes=1)
    before = json.loads(stored["raw"])
    assert after["explanation"]["text"] == before["explanation"]["text"]
    assert after["explanation"]["generated_by"] == "rule-based"
    assert {k: v for k, v in after.items() if k != "explanation"} == \
        {k: v for k, v in before.items() if k != "explanation"}


def test_the_backoff_window_is_15_minutes():
    explanation = {"text": "t", "generated_by": "rule-based"}
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    for minutes, recent in ((1, True), (14, True), (15, False), (60, False)):
        explanation["transcript_failed_at"] = (now - timedelta(minutes=minutes)).isoformat()
        assert rec_service.transcript_failed_recently(explanation, now=now) is recent, minutes
    # A naive stamp is read as UTC.
    explanation["transcript_failed_at"] = (now - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    assert rec_service.transcript_failed_recently(explanation, now=now) is True


def test_the_stamp_survives_parsing_so_the_page_can_honour_it():
    raw = json.dumps({"headline": "H", "forecast": _payload(),
                      "explanation": {"text": "t", "generated_by": "rule-based"}})
    stamped = rec_service.mark_transcript_failed(raw)
    parsed = rec_service.parse_recommendation(stamped)
    assert parsed["explanation"]["transcript_failed_at"]
    assert json.loads(stamped)["forecast"] == _payload()


def test_without_a_usable_gemini_key_the_endpoint_asks_nobody(app, stored, monkeypatch):
    with app.app_context():
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"
        seen = _fake_transcriber(monkeypatch, {"text": "x", "generated_by": "llm:openai"})
        data = _client(app, "owner@transcript.test").post(
            f"/api/forecasts/{stored['forecast_id']}/transcript").get_json()
        assert _stored_json(stored["forecast_id"]) == json.loads(stored["raw"]), "nothing stamped either"
    assert data == {"ok": False, "reason": "unavailable"}
    assert seen == []


def test_a_forecast_from_before_the_plan_model_has_nothing_to_transcribe(app, stored, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        row = db.session.get(ForecastResult, stored["forecast_id"])
        old = json.loads(row.recommendation)
        old.pop("forecast")
        row.recommendation = json.dumps(old)
        db.session.commit()
        data = _client(app, "owner@transcript.test").post(
            f"/api/forecasts/{stored['forecast_id']}/transcript").get_json()
    assert data == {"ok": False, "reason": "no_payload"}


def test_end_to_end_through_gemini_s_native_endpoint(app, stored, monkeypatch):
    """The real transcribe_forecast and _generate_with_gemini, with only
    requests.post faked: Gemini is sent the stored computation and its
    transcript is stored. The reply is the forecast's own model summary,
    which quotes only the payload's figures, so it passes the check."""
    import requests

    with app.app_context():
        _gemini_on(app, monkeypatch)
        summary = _stored_json(stored["forecast_id"])["explanation"]["text"]
        seen = {}

        class _Response:
            status_code = 200
            text = ""

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": _reply(summary)}]},
                                        "finishReason": "STOP"}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, prompt=json["contents"][0]["parts"][0]["text"], headers=headers)
            return _Response()

        monkeypatch.setattr(requests, "post", fake_post)
        data = _client(app, "owner@transcript.test").post(
            f"/api/forecasts/{stored['forecast_id']}/transcript").get_json()
        after = _stored_json(stored["forecast_id"])

    assert seen["url"].endswith(f"/models/{MODEL}:generateContent")
    assert seen["headers"]["x-goog-api-key"] == GEMINI_KEY
    assert "FORECAST COMPUTATION" in seen["prompt"] and "PLAN SCORECARD" in seen["prompt"]
    assert data["ok"] is True
    assert after["explanation"] == {"text": summary, "generated_by": f"llm:gemini:{MODEL}"}


# ---------------------------------------------------------------------
# 5. The page: the hook, the badges, the script
# ---------------------------------------------------------------------

def _render(app, rec, forecast=SimpleNamespace(forecast_id=41), macro="explanation_paragraph"):
    from flask import get_template_attribute

    with app.test_request_context("/"):
        return str(get_template_attribute("shared/_plan_insights.html", macro)(rec, forecast=forecast))


def _rec(generated_by="rule-based", **explanation):
    return {"forecast": _payload(), "subcategory_analysis": None,
            "explanation": {"text": "First paragraph.\n\nSecond paragraph.", "generated_by": generated_by,
                            **explanation}}


def test_the_hook_is_rendered_only_when_gemini_can_be_asked(app, monkeypatch):
    with app.app_context():
        # Nothing configured: no hook, no script, no spinner.
        page = _render(app, _rec())
        assert "data-forecast-transcript" not in page and "forecast_transcript.js" not in page

        _gemini_on(app, monkeypatch)
        page = _render(app, _rec())
        assert "data-forecast-transcript" in page
        assert 'data-transcript-url="/api/forecasts/41/transcript"' in page
        assert 'data-forecast-id="41"' in page
        assert 'aria-live="polite"' in page and "Gemini is transcribing the forecast" in page
        assert "js/forecast_transcript.js" in page

        # Not without the forecast row, not for an AI transcript, not
        # inside the back-off, not with the switch off, not with an sk- key.
        assert "data-forecast-transcript" not in _render(app, _rec(), forecast=None)
        assert "data-forecast-transcript" not in _render(app, _rec(generated_by=f"llm:gemini:{MODEL}"))
        recent = datetime.now(timezone.utc).isoformat()
        assert "data-forecast-transcript" not in _render(app, _rec(transcript_failed_at=recent))
        stale = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        assert "data-forecast-transcript" in _render(app, _rec(transcript_failed_at=stale))
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
        assert "data-forecast-transcript" not in _render(app, _rec())
        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"
        assert "data-forecast-transcript" not in _render(app, _rec())


def test_the_breakdown_passes_the_forecast_through(app, monkeypatch):
    with app.app_context():
        _gemini_on(app, monkeypatch)
        page = _render(app, _rec(), macro="forecast_breakdown")
    assert "How this forecast was computed" in page
    assert 'data-transcript-url="/api/forecasts/41/transcript"' in page


def test_the_badges_say_who_wrote_it(app):
    with app.app_context():
        gemini = _render(app, _rec(generated_by=f"llm:gemini:{MODEL}"))
        other = _render(app, _rec(generated_by="llm:openai:openai/gpt-4o-mini"))
        summary = _render(app, _rec())

    assert "Forecast transcript" in summary and "What the model forecast" not in summary
    assert "Transcribed by Gemini" in gemini and f'title="Transcribed by {MODEL}' in gemini
    assert "Transcribed by AI (openai)" in other and "openai/gpt-4o-mini" in other
    assert "Model summary &mdash; Gemini transcript not available yet" in summary
    assert "fw-normal" in summary, "the stand-in is the visually quieter badge"
    for page in (gemini, other, summary):
        assert "Rule-based explanation" not in page and "Explained by Gemini" not in page
    # Paragraphs are kept apart.
    assert summary.count("<p class=\"small") >= 2 and "Second paragraph." in summary


def test_the_script_is_idempotent_and_never_parses_the_transcript_as_html():
    with open(os.path.join(ROOT, "app", "static", "js", "forecast_transcript.js"), encoding="utf-8") as handle:
        script = handle.read()
    assert "window.__dssForecastTranscript" in script
    assert '"X-CSRFToken"' in script and 'method: "POST"' in script
    assert "element.textContent = paragraph" in script
    assert "for (const hooks of groups.values())" in script and "await requestTranscript" in script, \
        "one request per forecast, one at a time"


# ---------------------------------------------------------------------
# 6. Gemini is the default narrator
# ---------------------------------------------------------------------

def test_gemini_is_the_default_provider(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "")
    import importlib

    from app import config as config_module

    importlib.reload(config_module)
    try:
        assert config_module.Config.LLM_PROVIDER == "gemini"
        assert config_module.Config.GEMINI_MODEL == MODEL, "a declared-but-empty GEMINI_MODEL is the default"
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)


def test_an_sk_key_under_the_default_provider_puts_openai_first(app):
    with app.app_context():
        app.config["LLM_PROVIDER"] = "gemini"
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"
        assert llm_service._provider_order()[0] == "openai"
        assert "warning" in llm_service.llm_status()
        app.config["GEMINI_API_KEY"] = GEMINI_KEY
        assert llm_service._provider_order()[0] == "gemini"


def test_the_deployment_files_name_gemini(app):
    for name in (".env.sample", "render.yaml"):
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            text = handle.read()
        assert "GEMINI_API_KEY" in text and "GEMINI_MODEL" in text, name
    with open(os.path.join(ROOT, "render.yaml"), encoding="utf-8") as handle:
        render = handle.read()
    assert re.search(r"- key: LLM_PROVIDER\s+value: \"gemini\"", render)
    assert re.search(r"- key: GEMINI_API_KEY\s+sync: false", render)
    with open(os.path.join(ROOT, ".env.sample"), encoding="utf-8") as handle:
        assert "LLM_PROVIDER=gemini" in handle.read()


def test_the_admin_switch_says_gemini_writes_the_transcript(app):
    with app.app_context():
        _user("admin@transcript.test", role="Admin")
        page = _client(app, "admin@transcript.test").get("/admin/settings").get_data(as_text=True)
    assert "Gemini writes the forecast transcript" in page
