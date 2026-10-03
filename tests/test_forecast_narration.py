"""
tests/test_forecast_narration.py
-----------------------------------
The trained model forecasts; Gemini transcribes. These tests pin the
half of that sentence that lives in recommendation_service.py and
llm_service.py:

  * the forecast payload in a recommendation is ALWAYS the model's --
    the LLM writes about it and can never supply or edit it;
  * a deterministic rule-based explanation is built from the payload
    alone, quoting only the payload's own figures;
  * the LLM's explanation is kept only when every number in it can be
    found in the payload/context (the grounding check), and is swapped
    for the rule-based one the moment it invents a figure;
  * Gemini is asked FIRST for this call whatever LLM_PROVIDER says,
    except when the key it would be sent is plainly an OpenAI/OpenRouter
    `sk-` key;
  * "Capital" is the word used, never "Startup capital".

No network: every LLM call is a monkeypatched generator or a fake
requests.post. The payload is a hand-made fixture in the shape of the
contract plan_forecast_service.forecast_plan() produces (see the spec /
Reference/FORECAST_MODEL.md), so these tests do not depend on a trained
plan_model.pkl being on disk.

Run with:
    pytest tests/test_forecast_narration.py -v
"""

import copy
import json
import re

import pytest

from app import create_app
from app.extensions import db
from app.models import SystemSetting
from app.services import llm_service
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


@pytest.fixture(autouse=True)
def _clean_llm_state():
    """The service caches its clients and its last failure per process."""
    llm_service.reset_client_cache()
    yield
    llm_service.reset_client_cache()


# ---------------------------------------------------------------------
# Fixtures: a payload in the contract's shape, and a context around it
# ---------------------------------------------------------------------

def _payload(**overrides):
    payload = {
        "version": "plan_v1",
        "market": {"saturation_index": 42.1, "industry_saturation_index": 48.0, "cluster_label": "Moderate",
                   "competitor_count": 7, "confidence": 88.2, "model_version": "rf_v1"},
        "plan": {"viability_index": 61.3, "viability_score": 6.1, "confidence": 84.0,
                 "model_version": "plan_rf_v1", "scorecard_index": 60.4},
        "inputs": {"capital": 500000.0, "employee_count": 3, "business_stage": "startup",
                   "years_in_operation": 0.0, "priced_item_count": 5, "average_price": 85.0,
                   "has_offering_description": True, "has_innovation_idea": True,
                   "industry_type": "Food and Beverage", "subcategory_label": "Bakery / Pastries",
                   "location": "Tibag", "population": 12450, "residents_per_business": 1556.3},
        "financials": {"monthly_rent": 15000.0, "daily_wage": 590.0, "monthly_payroll": 46020.0,
                       "monthly_fixed_cost": 61020.0, "capital_runway_months": 8.2, "ramp_up_months": 11.9,
                       "capital_adequacy": 0.69, "gross_margin": 0.4, "operating_days": 26,
                       "required_daily_sales": 69.1, "daily_sales_ceiling": 222.3,
                       "break_even": {"low_months": 10, "high_months": 15, "label": "10-15 months"}},
        "components": [
            {"key": "market_opportunity", "label": "Market opportunity", "aspect": "Market",
             "score": 0.58, "weight": 0.40, "points": 23.2, "inputs": "industry, sub-category, location"},
        ],
        "baseline": 52.0,
        "drivers": [
            {"key": "market", "label": "Market saturation (industry · sub-category · location)", "points": 8.3},
            {"key": "capital", "label": "Capital vs. running costs", "points": -5.1},
            {"key": "pricing", "label": "Pricing (price list)", "points": 3.4},
            {"key": "staffing", "label": "Staffing", "points": 0.2},
        ],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key] = {**payload[key], **value}
        else:
            payload[key] = value
    return payload


def _context(forecast="default", **overrides):
    context = {
        "business_name": "Pan de Tibag", "industry_type": "Food and Beverage",
        "subcategory": "bakery", "subcategory_label": "Bakery / Pastries",
        "product_offering": "Pandesal", "innovation_idea": "", "offering_items": [], "price_summary": None,
        "location": "Tibag", "business_stage": "startup", "years_in_operation": 0.0,
        "capital": 500000.0, "startup_capital": 500000.0, "employee_count": 3,
        "saturation_index": 42.1, "industry_saturation_index": 48.0, "cluster_label": "Moderate",
        "viability_score": 6.1, "confidence_level": 84.0, "population": 12450, "competitor_count": 7,
        "competitor_simulated": False, "competitor_sample": [], "subcategory_analysis": None,
        "forecast": _payload() if forecast == "default" else forecast,
    }
    context.update(overrides)
    return context


# The kind of paragraph Gemini is asked for: every figure in it is one
# the payload holds (6.1, 61.3, 84, 42.1, 8.3, 5.1, 500,000, 8.2,
# 61,020, 11.9, 10-15).
GROUNDED = (
    "The trained model gives this plan a viability of 6.1/10 (index 61.3) with 84% confidence. "
    "Tibag is 42.1% saturated, a Moderate market. Market saturation added 8.3 points while capital "
    "vs. running costs took off 5.1. Your ₱500,000 covers 8.2 months of fixed costs of ₱61,020 a "
    "month, shorter than the 11.9-month ramp-up, and break-even is expected in 10-15 months."
)

# The same paragraph with one invented figure in it.
INVENTED = GROUNDED + " Expect about ₱2,400,000 in sales in your first year."


def _llm_reply(explanation=GROUNDED, **extra):
    body = {
        "headline": "MODERATE OPPORTUNITY -- viable with a solid differentiation strategy.",
        "opportunity_type": "Moderate Opportunity",
        "summary": "A bakery in Tibag can work if the capital stretches.",
        "reasons": ["Only 7 food businesses on file"],
        "risks": ["Capital runs short of the ramp-up"],
    }
    if explanation is not None:
        body["explanation"] = explanation
    body.update(extra)
    return json.dumps(body)


def _recording_generators(monkeypatch, replies):
    """Replace every generator with one that records being asked and
    answers with replies[name] (None = no answer)."""
    attempted = []

    def make(name):
        def generate(prompt):
            attempted.append(name)
            return replies.get(name)
        return generate

    for name in ("openai", "anthropic", "gemini"):
        monkeypatch.setitem(llm_service._GENERATORS, name, make(name))
    return attempted


# ---------------------------------------------------------------------
# 1. The rule-based explanation
# ---------------------------------------------------------------------

def test_rule_based_explanation_quotes_only_payload_numbers():
    explanation = rec_service._rule_based_explanation(_context())
    assert explanation["generated_by"] == "rule-based"
    text = explanation["text"]

    # It passes the same check the LLM's wording has to pass.
    assert rec_service.ungrounded_numbers(text, _context()) == []
    # ...and that check is not vacuous: it does find the figures.
    assert len(rec_service._numbers_in(text)) >= 10


def test_rule_based_explanation_covers_runway_ramp_up_drivers_and_break_even():
    text = rec_service._rule_based_explanation(_context())["text"]

    assert "6.1/10" in text and "84% confidence" in text
    assert "42.1% saturated" in text and "'Moderate' tier" in text
    # The three largest drivers, with their signs, from the baseline.
    assert "baseline of 52 points" in text
    assert "market saturation (industry · sub-category · location) added 8.3 points" in text
    assert "capital vs. running costs took off 5.1 points" in text
    assert "pricing (price list) added 3.4 points" in text
    assert "staffing" not in text.lower(), "only the three largest drivers are named"
    # Fixed cost broken into rent + payroll, then runway vs ramp-up.
    assert "₱61,020/month (rent ₱15,000 plus 3 employees × ₱590/day × 26 days)" in text
    assert "covers about 8.2 months" in text
    assert "shorter than the ~11.9-month ramp-up" in text
    # Required daily sales vs the ceiling, then the break-even window.
    assert "69.1 sales a day" in text and "222.3" in text
    assert "10-15 months" in text

    # Split on a full stop followed by a capital, so "vs. running" stays whole.
    sentences = re.split(r"(?<=\.)\s+(?=[A-Z])", text)
    assert 4 <= len(sentences) <= 6, sentences


def test_rule_based_explanation_is_deterministic():
    assert rec_service._rule_based_explanation(_context()) == rec_service._rule_based_explanation(_context())


def test_the_formula_fallback_is_not_called_a_trained_model():
    payload = _payload(plan={"model_version": "plan_formula_v1", "confidence": 50.0}, baseline=0.0)
    text = rec_service._rule_based_explanation(_context(forecast=payload))["text"]
    assert "scorecard formula" in text
    assert not text.startswith("The trained plan model")
    assert rec_service.ungrounded_numbers(text, _context(forecast=payload)) == []


def test_each_confidence_is_credited_to_the_stage_that_produced_it():
    """When the market stage is the less sure one, the forecast's overall
    confidence (the lower of the two) is the market model's -- it must
    not be quoted as the plan model's own."""
    context = _context(forecast=_payload(market={"confidence": 55.0}, plan={"confidence": 92.0}))
    text = rec_service._rule_based_explanation(context)["text"]

    assert "rates this plan's viability 6.1/10 with 92% confidence" in text
    assert "the forecast's overall confidence is 55%, the market model's" in text
    assert "with 55% confidence" not in text
    assert rec_service.ungrounded_numbers(text, context) == []

    # The usual case -- the plan stage is the less sure -- names one figure.
    text = rec_service._rule_based_explanation(_context())["text"]
    assert "with 84% confidence." in text and "overall confidence" not in text


def test_no_payload_means_no_explanation():
    """Nothing from the trained model to transcribe -- the explanation is
    absent rather than invented from the older market-only figures."""
    recommendation = rec_service._rule_based_recommendation(_context(forecast=None))
    assert recommendation["forecast"] is None
    assert recommendation["explanation"] is None


# ---------------------------------------------------------------------
# 2. Capital and pricing in the reasons, risks and summary
# ---------------------------------------------------------------------

def test_runway_short_of_the_ramp_up_is_a_risk_with_levers():
    risks = " ".join(rec_service._risks_for(_context()))
    assert "your capital of ₱500,000 covers only about 8.2 months of fixed costs (₱61,020/month)" in risks
    assert "~11.9-month ramp-up" in risks
    assert "more capital" in risks and "fewer staff" in risks and "cheaper site" in risks


def test_runway_longer_than_the_ramp_up_is_a_reason():
    payload = _payload(inputs={"capital": 900000.0}, financials={"capital_runway_months": 14.7})
    reasons = " ".join(rec_service._reasons_for(_context(forecast=payload)))
    assert ("your capital of ₱900,000 covers about 14.7 months of fixed costs (₱61,020/month), longer "
            "than the ~11.9-month ramp-up the model expects") in reasons


def test_missing_capital_is_flagged_not_hidden():
    payload = _payload(inputs={"capital": 0.0}, financials={"capital_runway_months": 0.0})
    context = _context(forecast=payload, capital=0.0, startup_capital=0.0)
    assert "no capital is on file" in " ".join(rec_service._risks_for(context))
    assert "No capital is on file" in rec_service._rule_based_explanation(context)["text"]


def test_prices_the_barangay_cannot_support_are_a_risk():
    payload = _payload(financials={"required_daily_sales": 310.4, "daily_sales_ceiling": 222.3})
    context = _context(forecast=payload)
    risks = " ".join(rec_service._risks_for(context))
    assert "about 310.4 sales a day" in risks and "more than the ~222.3 a day" in risks
    assert "310.4" not in " ".join(rec_service._reasons_for(context))


def test_affordable_prices_are_a_reason():
    reasons = " ".join(rec_service._reasons_for(_context()))
    assert "at an average price of ₱85 you need about 69.1 sales a day" in reasons
    assert "well within the ~222.3 a day" in reasons


def test_summary_factors_in_the_plan():
    summary = rec_service._summary_for(_context())
    assert "once your capital, staffing, pricing and offering are factored in" in summary
    assert "6.1/10" in summary


def test_without_a_payload_the_summary_reads_as_before():
    summary = rec_service._summary_for(_context(forecast=None))
    assert "for the parameters you entered" in summary


# ---------------------------------------------------------------------
# 3. Capital wording, and the context
# ---------------------------------------------------------------------

class _Profile:
    """Just enough of an SmeProfile for build_recommendation_context."""
    business_name = "Pan de Tibag"
    industry_type = "Food and Beverage"
    subcategory = "bakery"
    subcategory_label = "Bakery / Pastries"
    product_offering = "Pandesal"
    innovation_idea = ""
    offering_items = []
    location = "Tibag"
    business_stage = "startup"
    startup_capital = 500000
    employee_count = 3

    def years_in_operation(self):
        return 0.0


def _scores():
    return {"saturation_index": 42.1, "industry_saturation_index": 48.0, "cluster_label": "Moderate",
            "viability_score": 6.1, "confidence_level": 84.0, "competitor_count": 7}


def test_context_carries_capital_and_the_payload():
    payload = _payload()
    context = rec_service.build_recommendation_context(_Profile(), _scores(), population=12450,
                                                       plan_forecast=payload)
    assert context["capital"] == 500000.0
    assert context["startup_capital"] == 500000.0, "the alias stays for older readers"
    assert context["forecast"] is payload


def test_context_without_a_plan_forecast_has_none():
    context = rec_service.build_recommendation_context(_Profile(), _scores())
    assert context["forecast"] is None


def test_the_prompt_says_capital_not_startup_capital():
    prompt = llm_service._prompt_for(_context())
    assert "Capital: PHP 500,000" in prompt
    assert "startup capital" not in prompt.lower()


def test_a_context_with_only_the_old_key_still_prompts():
    """Hand-built contexts elsewhere only carry "startup_capital"."""
    context = _context(forecast=None)
    del context["capital"]
    assert "Capital: PHP 500,000" in llm_service._prompt_for(context)


def test_the_capital_reason_without_a_payload_says_capital():
    reasons = " ".join(rec_service._reasons_for(_context(forecast=None)))
    assert "your capital of PHP 500,000 is on file" in reasons
    assert "planned capital" not in reasons


# ---------------------------------------------------------------------
# 4. The prompt hands the LLM the trained model's output
# ---------------------------------------------------------------------

def test_the_prompt_carries_the_trained_model_output_block():
    prompt = llm_service._prompt_for(_context())
    assert "TRAINED MODEL OUTPUT" in prompt
    assert '"explanation"' in prompt
    assert "Market Saturation Index 42.1%" in prompt
    assert "Plan Viability Index 61.3 out of 100" in prompt and "6.1/10" in prompt
    assert "baseline of 52" in prompt
    assert "Capital vs. running costs: -5.1 points" in prompt
    assert "Market saturation (industry · sub-category · location): +8.3 points" in prompt
    assert "Monthly fixed cost: PHP 61,020 = rent PHP 15,000 + payroll PHP 46,020" in prompt
    assert "Capital runway: 8.2 months" in prompt and "11.9 months" in prompt
    assert "Break-even window: 10-15 months" in prompt
    assert "never change, recompute, combine or invent" in prompt


def test_no_payload_means_no_block_and_no_explanation_asked_for():
    prompt = llm_service._prompt_for(_context(forecast=None))
    assert "TRAINED MODEL OUTPUT" not in prompt
    assert '"explanation"' not in prompt


def test_the_parser_keeps_the_explanation_text():
    payload = llm_service._coerce_llm_payload(_llm_reply())
    assert payload["explanation"] == GROUNDED
    assert "explanation" not in llm_service._coerce_llm_payload(_llm_reply(explanation=None))
    assert "explanation" not in llm_service._coerce_llm_payload(_llm_reply(explanation=["not", "text"]))


# ---------------------------------------------------------------------
# 5. The grounding check
# ---------------------------------------------------------------------

def test_grounded_text_passes_the_check():
    assert rec_service.ungrounded_numbers(GROUNDED, _context()) == []


def test_an_invented_figure_is_caught():
    assert rec_service.ungrounded_numbers(INVENTED, _context()) == ["2,400,000"]


@pytest.mark.parametrize("text", [
    "a viability of 61 out of 100",          # 61.3 rounded
    "a 6/10 plan",                            # 6.1 rounded; "/10" is not a figure
    "about 42% saturated",                    # 42.1 rounded
    "a 40% gross margin",                     # 0.4 as a percentage
    "3 employees",                            # present exactly
    "₱61,020 a month",                        # thousands separator
    "PHP500k of capital",                     # currency prefix and k suffix
    "Php 500,000 of capital",                 # the prefix in any case, spaced
    "php500,000 of capital",                  # ...or glued
    "the rf_v1 and plan_rf_v1 models",       # identifiers glued to digits are codes, not figures
    "from Q1 to Q4",                          # quarters
    "B2B catering orders",                    # digits between letters
    "a site 5 km from the market",            # 5 is held; "km" is not thousands
])
def test_tolerated_forms(text):
    assert rec_service.ungrounded_numbers(text, _context()) == [], text


@pytest.mark.parametrize("text, invented", [
    ("break even in 4 months", "4"),          # a small integer the payload never held
    ("30 months to break even", "30"),        # not 3 employees x 10
    ("a viability index of 75.5", "75.5"),
    ("₱987,654 of capital", "987,654"),
    ("1.5 million in sales", "1.5 million"),
    ("1.5 Million in sales", "1.5 Million"),  # the scale word in any case
    ("40 months to break even", "40"),        # the 40% margin grounds "40%" only, never "40 months"
    # How money is commonly written. Each was read as nothing at all, or
    # as a small figure the payload happens to hold, before.
    ("Expect about Php2,400,000 in sales in your first year.", "2,400,000"),
    ("Expect about php 2,400,000 in sales.", "2,400,000"),
    ("Expect about Php9,999,999 in sales in your first year.", "9,999,999"),
    ("Expect about ₱3.4m in sales in your first year.", "3.4m"),     # not the 3.4-point driver
    ("Expect about ₱8.3 M in revenue.", "8.3 M"),                     # not the 8.3-point driver
    ("Expect ₱2.4B over five years.", "2.4B"),
    ("Expect 2.4 mn in sales.", "2.4 mn"),
    ("Expect ₱2 mil in sales.", "2 mil"),
    ("You can expect a x25 return on capital.", "25"),
    ("About USD500 a day.", "500"),
])
def test_invented_forms(text, invented):
    assert rec_service.ungrounded_numbers(text, _context()) == [invented]


# The scorecard's seven components, weights as the spec sets them. Every
# real payload carries all seven -- and none of these figures is ever
# shown to the LLM, so none of them may ground anything.
SEVEN_COMPONENTS = [
    {"key": "market_opportunity", "label": "Market opportunity", "aspect": "Market",
     "score": 0.58, "weight": 0.40, "points": 23.2, "inputs": "industry, sub-category, location"},
    {"key": "capital_adequacy", "label": "Capital adequacy", "aspect": "Financial",
     "score": 0.69, "weight": 0.20, "points": 13.8, "inputs": "capital, employees, rent"},
    {"key": "price_coverage", "label": "Price coverage", "aspect": "Financial",
     "score": 0.65, "weight": 0.10, "points": 6.5, "inputs": "price list"},
    {"key": "operating_experience", "label": "Operating experience", "aspect": "Technical",
     "score": 0.0, "weight": 0.08, "points": 0.0, "inputs": "business stage"},
    {"key": "differentiation", "label": "Differentiation", "aspect": "Product",
     "score": 0.0, "weight": 0.08, "points": 0.0, "inputs": "innovation idea"},
    {"key": "staffing", "label": "Staffing", "aspect": "Technical",
     "score": 1.0, "weight": 0.07, "points": 7.0, "inputs": "employees"},
    {"key": "offering_definition", "label": "Offering defined", "aspect": "Product",
     "score": 0.75, "weight": 0.07, "points": 5.3, "inputs": "offering, price list"},
]


def _full_context():
    """The fixture with all seven components, and with the payload's own
    figures moved off 7, 8, 10, 20 and 40 (4 competitors, a 5.6-month
    runway, an 11-16 month window, a 35% margin, a 9.4-point market
    driver) so that only the components could ground those numbers."""
    payload = _payload(
        market={"competitor_count": 4},
        financials={"capital_runway_months": 5.6, "gross_margin": 0.35,
                    "break_even": {"low_months": 11, "high_months": 16, "label": "11-16 months"}},
        components=copy.deepcopy(SEVEN_COMPONENTS),
        drivers=[
            {"key": "market", "label": "Market saturation (industry · sub-category · location)", "points": 9.4},
            {"key": "capital", "label": "Capital vs. running costs", "points": -5.1},
            {"key": "pricing", "label": "Pricing (price list)", "points": 3.4},
            {"key": "staffing", "label": "Staffing", "points": 0.2},
        ],
    )
    return _context(forecast=payload, competitor_count=4)


@pytest.mark.parametrize("text, invented", [
    ("You will break even in 20 months.", "20"),          # weight 0.20
    ("Capital covers 40% of the ramp-up.", "40"),         # weight 0.40 -- not the 35% margin
    ("Break even in 7 months.", "7"),                     # weight 0.07
    ("Break even in 8 months.", "8"),                     # weight 0.08
    ("Break even in 10 months.", "10"),                   # weight 0.10
    ("This plan scores 8/10.", "8"),
    ("Expect 65 months.", "65"),                          # a component score as a percentage
    ("Market opportunity earned 23.2 points.", "23.2"),   # a component's points
])
def test_the_scorecard_components_ground_nothing(text, invented):
    assert rec_service.ungrounded_numbers(text, _full_context()) == [invented]


def test_the_full_payload_still_grounds_the_rule_based_explanation():
    context = _full_context()
    text = rec_service._rule_based_explanation(context)["text"]
    assert rec_service.ungrounded_numbers(text, context) == []
    assert "35% gross margin" in text
    assert rec_service.ungrounded_numbers("a 35% gross margin", context) == []


@pytest.mark.parametrize("text, invented", [
    ("You will break even in four months.", ["four"]),
    ("Expect twenty months before break-even.", ["twenty"]),
    ("Expect two million pesos in sales.", ["two million"]),
    ("You need nine times the ceiling.", ["nine"]),
    ("A ramp-up of eighteen months.", ["eighteen"]),
    ("Plan for twenty-five thousand pesos a month.", ["twenty-five thousand"]),
])
def test_spelled_out_numbers_are_checked(text, invented):
    assert rec_service.ungrounded_numbers(text, _context()) == invented


@pytest.mark.parametrize("text", [
    "With three employees on the payroll",    # 3, as the payload holds it
    "seven competitors already",              # 7
    "break-even in ten to fifteen months",    # the window, in words
    "one of the biggest drivers",             # "one" as a pronoun is not a figure
    "no one else sells it",
    "a viability of 61 out of one hundred",
])
def test_spelled_out_numbers_that_are_in_the_payload_pass(text):
    assert rec_service.ungrounded_numbers(text, _context()) == [], text


def test_spelled_out_numbers_are_read_whole():
    """A compound is one figure, and two numbers joined by "and" stay two."""
    assert [f.value for f in rec_service._numbers_in("two hundred and fifty thousand pesos")] == [250000.0]
    assert [f.value for f in rec_service._numbers_in("between six and nine months")] == [6.0, 9.0]
    assert [f.value for f in rec_service._numbers_in("1.5 million in sales")] == [1500000.0]


# ---------------------------------------------------------------------
# 5b. A plan with no price list, and a market stage with no forest
# ---------------------------------------------------------------------

def _no_price_payload(**plan):
    return _payload(
        plan=plan or {},
        inputs={"priced_item_count": 0, "average_price": 0.0},
        financials={"required_daily_sales": 0.0, "daily_sales_ceiling": 222.3},
        drivers=[
            {"key": "capital", "label": "Capital vs. running costs", "points": -5.1},
            {"key": "pricing", "label": "Pricing (price list)", "points": -2.9},
            {"key": "market", "label": "Market saturation (industry · sub-category · location)", "points": 1.2},
        ],
    )


def test_no_price_list_under_the_trained_model_is_not_called_neutral():
    """The forest's decomposition says the missing price list took points
    off; the same paragraph must not then call pricing neutral."""
    context = _context(forecast=_no_price_payload())
    text = rec_service._rule_based_explanation(context)["text"]
    assert "pricing (price list) took off 2.9 points" in text
    assert "neutral" not in text
    assert "could not be checked" in text
    assert rec_service.ungrounded_numbers(text, context) == []

    prompt = llm_service._prompt_for(context)
    assert "treated pricing as neutral" not in prompt
    assert "No price list yet: prices could not be checked" in prompt


def test_no_price_list_under_the_scorecard_is_neutral():
    """The formula fallback really does score unknown pricing at half."""
    context = _context(forecast=_no_price_payload(model_version="plan_formula_v1", confidence=50.0))
    assert "scorecard treated pricing as neutral" in rec_service._rule_based_explanation(context)["text"]
    assert "the scorecard treated pricing as neutral" in llm_service._prompt_for(context)


def test_pricing_figures_the_prompt_never_printed_ground_nothing():
    """With no price list the prompt prints no ceiling and no margin, so
    neither can ground the LLM's text."""
    context = _context(forecast=_no_price_payload())
    assert rec_service.ungrounded_numbers("about 222.3 sales a day", context) == ["222.3"]
    assert rec_service.ungrounded_numbers("a 40% gross margin", context) == ["40"]


# A measured sub-category analysis, in subcategory_service's shape plus
# the three figures forecasting_service adds to it.
MEASURED_ANALYSIS = {
    "subcategory": "bakery", "label": "Bakery / Pastries", "direct_count": 2, "is_estimated": False,
    "source": "Google Places API", "industry_count": 7, "expected_share": 0.18,
    "share_basis": "assumed", "density_ratio": 0.57, "adjusted_competitor_count": 4,
    "adjusts_score": True, "industry_saturation_index": 48.0, "industry_viability_score": 3.6,
    "saturation_index": 42.1,
}


def test_the_sub_category_internals_ground_nothing():
    """The prompt prints the analysis's direct-competitor count and
    nothing else of it -- not the adjusted count, the ratios or the
    industry-level viability (3.6, i.e. "36" once scaled) -- so only the
    direct count may ground a figure."""
    without = _context()
    with_analysis = _context(subcategory_analysis=dict(MEASURED_ANALYSIS))
    prompt = llm_service._prompt_for(with_analysis)
    assert "Direct Bakery / Pastries competitors in Tibag: 2" in prompt
    assert "3.6" not in prompt and "0.57" not in prompt

    for text, invented in (("You will break even in 4 months.", "4"),
                           ("The plan's viability index is 36 out of 100.", "36")):
        assert rec_service.ungrounded_numbers(text, without) == [invented]
        assert rec_service.ungrounded_numbers(text, with_analysis) == [invented]

    assert rec_service.ungrounded_numbers("Only 2 direct bakeries nearby.", without) == ["2"]
    assert rec_service.ungrounded_numbers("Only 2 direct bakeries nearby.", with_analysis) == []


def test_a_formula_market_reading_is_not_called_a_trained_model():
    payload = _payload(market={"model_version": "formula_v1", "confidence": 50.0})
    context = _context(forecast=payload)

    prompt = llm_service._prompt_for(context)
    assert "Market model (Random Forest, trained)" not in prompt
    assert "Market formula (weighted formula -- no trained market model is available yet, say so)" in prompt
    assert "produced by the models this system trained" not in prompt

    text = rec_service._rule_based_explanation(context)["text"]
    assert "The market formula (no trained market model is available yet) reads Tibag" in text
    assert rec_service.ungrounded_numbers(text, context) == []


def test_both_stages_trained_keep_the_trained_labels():
    prompt = llm_service._prompt_for(_context())
    assert "Market model (Random Forest, trained)" in prompt
    assert "produced by the models this system trained" in prompt
    assert "The market model reads Tibag" in rec_service._rule_based_explanation(_context())["text"]


# ---------------------------------------------------------------------
# 6. build_recommendation: which explanation is kept
# ---------------------------------------------------------------------

def test_a_grounded_llm_explanation_is_kept(app, monkeypatch):
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
    with app.app_context():
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: {
            **llm_service._coerce_llm_payload(_llm_reply()),
            "generated_by": "llm:gemini", "model": "gemini-3.8-flash",
        })
        recommendation = rec_service.build_recommendation(_context())

    assert recommendation["explanation"] == {"text": GROUNDED, "generated_by": "llm:gemini:gemini-3.8-flash"}
    assert recommendation["generated_by"] == "llm:gemini"
    assert recommendation["summary"] == "A bakery in Tibag can work if the capital stretches."


def test_an_llm_explanation_that_invents_a_number_is_replaced(app, monkeypatch):
    """The one call's transcript invents a figure, and the dedicated
    transcription call (asked next) cannot do better either: the
    rule-based model summary stands."""
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
    asked = []
    with app.app_context():
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: {
            **llm_service._coerce_llm_payload(_llm_reply(explanation=INVENTED)),
            "generated_by": "llm:gemini", "model": "gemini-3.8-flash",
        })
        monkeypatch.setattr(llm_service, "transcribe_forecast", lambda c: asked.append(c) or None)
        recommendation = rec_service.build_recommendation(_context())

    assert len(asked) == 1, "the dedicated transcript was asked for before settling for the summary"
    explanation = dict(recommendation["explanation"])
    # The failed Gemini attempt is stamped, so the page does not retry at once.
    assert explanation.pop("transcript_failed_at")
    assert explanation.pop("transcript_failed_version") == rec_service.TRANSCRIPT_REQUEST_VERSION
    assert explanation == rec_service._rule_based_explanation(_context())
    assert recommendation["explanation"]["generated_by"] == "rule-based"
    assert "2,400,000" not in json.dumps(recommendation, ensure_ascii=False)
    # Only the explanation is swapped: the LLM's other wording stays.
    assert recommendation["generated_by"] == "llm:gemini"
    assert recommendation["headline"].startswith("MODERATE OPPORTUNITY")


def test_an_llm_reply_without_an_explanation_keeps_the_rule_based_one(app, monkeypatch):
    """...when the dedicated transcription call fails too."""
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
    with app.app_context():
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: {
            **llm_service._coerce_llm_payload(_llm_reply(explanation=None)), "generated_by": "llm:openai",
        })
        monkeypatch.setattr(llm_service, "transcribe_forecast", lambda c: None)
        recommendation = rec_service.build_recommendation(_context())

    assert recommendation["explanation"]["generated_by"] == "rule-based"


def test_the_forecast_always_comes_from_the_model_never_the_llm(app, monkeypatch):
    """Even an LLM reply that carries a "forecast" of its own cannot
    replace the payload."""
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
    context = _context()
    original = copy.deepcopy(context["forecast"])
    with app.app_context():
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: {
            **llm_service._coerce_llm_payload(_llm_reply()), "generated_by": "llm:gemini",
            "forecast": {"plan": {"viability_score": 10.0}},
        })
        recommendation = rec_service.build_recommendation(context)

    assert recommendation["forecast"] == original
    assert recommendation["forecast"]["plan"]["viability_score"] == 6.1


def test_an_llm_explanation_without_a_payload_is_ignored(app, monkeypatch):
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
    with app.app_context():
        monkeypatch.setattr(llm_service, "generate_recommendation_json", lambda c: {
            **llm_service._coerce_llm_payload(_llm_reply()), "generated_by": "llm:gemini",
        })
        recommendation = rec_service.build_recommendation(_context(forecast=None))

    assert recommendation["forecast"] is None
    assert recommendation["explanation"] is None


def test_switch_off_means_everything_rule_based(app, monkeypatch):
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
    with app.app_context():
        def _must_not_be_called(_context):
            raise AssertionError("the LLM was asked although the switch is off")

        monkeypatch.setattr(llm_service, "generate_recommendation_json", _must_not_be_called)
        recommendation = rec_service.build_recommendation(_context())

    assert recommendation["generated_by"] == "rule_based"
    assert recommendation["explanation"]["generated_by"] == "rule-based"
    assert recommendation["forecast"] == _payload()


def test_the_payload_and_explanation_survive_storage(app, monkeypatch):
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
    with app.app_context():
        recommendation = rec_service.build_recommendation(_context())
    parsed = rec_service.parse_recommendation(rec_service.serialize_recommendation(recommendation))
    assert parsed["forecast"] == _payload()
    assert parsed["explanation"] == recommendation["explanation"]


def test_old_rows_parse_with_forecast_and_explanation_none():
    old_json_row = json.dumps({
        "headline": "HIGH OPPORTUNITY", "opportunity_type": "High Opportunity", "summary": "s",
        "reasons": [], "risks": [], "generated_by": "rule_based",
        "subcategory_analysis": None, "innovation": None,
    })
    legacy_text_row = "HIGH OPPORTUNITY\n\nSummary.\n\nWhy: a; b.\n\nConsiderations: c."
    for row in (old_json_row, legacy_text_row, None, ""):
        parsed = rec_service.parse_recommendation(row)
        assert parsed["forecast"] is None
        assert parsed["explanation"] is None


def test_a_malformed_stored_explanation_is_dropped():
    row = json.dumps({"headline": "H", "explanation": {"text": "  "}, "forecast": "not a dict"})
    parsed = rec_service.parse_recommendation(row)
    assert parsed["explanation"] is None
    assert parsed["forecast"] is None


# ---------------------------------------------------------------------
# 7. Gemini first -- and not with someone else's key
# ---------------------------------------------------------------------

def test_gemini_is_tried_first_even_when_the_provider_is_openai(app, monkeypatch):
    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "sk-proj-0000000000000000"
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test_key"
        app.config["GEMINI_MODEL"] = "gemini-3.8-flash"
        attempted = _recording_generators(monkeypatch, {"gemini": _llm_reply(), "openai": _llm_reply()})

        payload = llm_service.generate_recommendation_json(_context())

    assert attempted == ["gemini"]
    assert payload["generated_by"] == "llm:gemini"
    assert payload["model"] == "gemini-3.8-flash"


def test_the_configured_provider_is_next_when_gemini_fails(app, monkeypatch):
    with app.app_context():
        app.config["LLM_PROVIDER"] = "anthropic"
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test_key"
        app.config["ANTHROPIC_MODEL"] = "claude-haiku-4-5"
        attempted = _recording_generators(monkeypatch, {"gemini": None, "anthropic": _llm_reply()})

        payload = llm_service.generate_recommendation_json(_context())

    assert attempted == ["gemini", "anthropic"]
    assert payload["generated_by"] == "llm:anthropic"
    assert payload["model"] == "claude-haiku-4-5"


def test_other_callers_keep_the_configured_provider_first(app):
    """Only the forecast narration prefers Gemini."""
    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "sk-proj-0000000000000000"
        assert llm_service._provider_order()[0] == "openai"
        assert llm_service._provider_order(prefer="gemini") == ["gemini", "openai", "anthropic"]


def test_an_sk_key_skips_gemini(app, monkeypatch):
    """GEMINI_API_KEY inherits OPENAI_API_KEY when empty; an OpenRouter
    key sent to Google can only ever fail, so it is not sent."""
    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "sk-or-v1-0000000000000000"
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"
        attempted = _recording_generators(monkeypatch, {"gemini": _llm_reply(), "openai": _llm_reply()})

        payload = llm_service.generate_recommendation_json(_context())
        failure = llm_service.last_failure()

    assert "gemini" not in attempted
    assert attempted[0] == "openai"
    assert payload["generated_by"] == "llm:openai"
    # The skip is recorded, so the diagnostics say why Gemini did not
    # write it -- as a "skipped", the kind any later failure replaces.
    assert failure["provider"] == "gemini"
    assert failure["stage"] == "skipped"
    assert "sk-" in failure["detail"]


def test_a_real_error_after_the_skip_is_the_one_reported(app, monkeypatch):
    """The skip is the dull failure; OpenRouter's own 404 is the one an
    operator needs to read."""
    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"

        def openai_404(_prompt):
            llm_service._record_failure("openai", "api_call", "model=openai/gpt-4o-mini: Error code: 404")
            return None

        monkeypatch.setitem(llm_service._GENERATORS, "openai", openai_404)
        monkeypatch.setitem(llm_service._GENERATORS, "anthropic", lambda _p: None)

        assert llm_service.generate_recommendation_json(_context()) is None
        failure = llm_service.last_failure()

    assert failure["provider"] == "openai"
    assert failure["stage"] == "api_call"
    assert "404" in failure["detail"]


def test_an_openai_client_that_cannot_be_built_is_not_hidden_by_the_skip(app, monkeypatch):
    """OpenRouter-only, and the openai/httpx pairing the repo pins
    against is broken, so the OpenAI client cannot even be built -- a
    no_client failure, through the real _generate_with_openai. That is
    the cause, not "Gemini skipped"."""
    import sys
    import types

    def broken_client(**_kwargs):
        raise TypeError("Client.__init__() got an unexpected keyword argument 'proxies'")

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "sk-or-v1-0000000000000000"
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"
        app.config["ANTHROPIC_API_KEY"] = ""
        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=broken_client))

        assert llm_service.generate_recommendation_json(_context()) is None
        failure = llm_service.last_failure()

    assert failure["provider"] == "openai"
    assert failure["stage"] == "no_client"
    assert "could not be built" in failure["detail"] and "proxies" in failure["detail"]


def test_an_anthropic_only_deployment_is_not_told_about_a_gemini_key(app, monkeypatch):
    """No Gemini key and no OpenAI key: Gemini, asked first only because
    the narration prefers it, is passed over without a call, and the
    configured Anthropic client's own failure is the one reported."""
    import sys
    import types

    def broken_client(**_kwargs):
        raise RuntimeError("bad base url")

    attempted = []

    def gemini(_prompt):
        attempted.append("gemini")
        return None

    with app.app_context():
        app.config["LLM_PROVIDER"] = "anthropic"
        app.config["ANTHROPIC_API_KEY"] = "sk-ant-0000000000000000"
        app.config["OPENAI_API_KEY"] = ""
        app.config["GEMINI_API_KEY"] = ""
        monkeypatch.setitem(llm_service._GENERATORS, "gemini", gemini)
        monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=broken_client))

        assert llm_service.generate_recommendation_json(_context()) is None
        failure = llm_service.last_failure()

    assert attempted == []
    assert failure["provider"] == "anthropic"
    assert failure["stage"] == "no_client"
    assert "could not be built" in failure["detail"]


def test_an_sk_key_under_provider_gemini_sends_the_call_to_openai(app, monkeypatch):
    """Gemini is now the DEFAULT provider, so LLM_PROVIDER=gemini no longer
    means the operator chose it: an OpenRouter-only deployment that never
    set the variable lands here, with its sk- key inherited into the
    Gemini slot. Google can only refuse that key, so it is never sent:
    OpenAI is treated as the configured provider, Gemini's skip is the
    low "skipped" record, and OpenAI's own error is the one reported."""
    attempted = []

    def gemini(_prompt):
        attempted.append("gemini")
        return None

    with app.app_context():
        app.config["LLM_PROVIDER"] = "gemini"
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"

        def openai_404(_prompt):
            attempted.append("openai")
            llm_service._record_failure("openai", "api_call", "model=gpt-4o-mini: Error code: 404")
            return None

        monkeypatch.setitem(llm_service._GENERATORS, "gemini", gemini)
        monkeypatch.setitem(llm_service._GENERATORS, "openai", openai_404)
        monkeypatch.setitem(llm_service._GENERATORS, "anthropic", lambda _p: None)

        assert llm_service._provider_order()[0] == "openai"
        assert llm_service.generate_recommendation_json(_context()) is None
        failure = llm_service.last_failure()

    assert attempted == ["openai"], "the sk- key never reached Google"
    assert failure["provider"] == "openai"
    assert failure["stage"] == "api_call"
    assert "404" in failure["detail"]


def test_end_to_end_gemini_narrates_over_its_native_endpoint(app, monkeypatch):
    """Through the real _generate_with_gemini with requests.post faked:
    the prompt Google receives carries the model output, and what comes
    back is stored as Gemini's explanation, naming the model."""
    import requests

    seen = {}

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": _llm_reply()}]}, "finishReason": "STOP"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, prompt=json["contents"][0]["parts"][0]["text"])
        return _Response()

    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = ""
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test_key"
        app.config["GEMINI_MODEL"] = "gemini-3.8-flash"
        monkeypatch.setattr(requests, "post", fake_post)

        recommendation = rec_service.build_recommendation(_context())

    assert seen["url"].endswith("/models/gemini-3.8-flash:generateContent")
    assert "TRAINED MODEL OUTPUT" in seen["prompt"]
    assert recommendation["explanation"] == {"text": GROUNDED, "generated_by": "llm:gemini:gemini-3.8-flash"}
    assert recommendation["forecast"] == _payload()
