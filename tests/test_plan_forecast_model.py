"""
tests/test_plan_forecast_model.py
------------------------------------
Stage 2 of the forecast: the trained Plan Viability Model
(app/ml/plan_model.py, app/services/plan_forecast_service.py).

WHAT THESE TESTS ARE FOR
The complaint this model answers was that a plan's forecast ignored
the plan: capital, staff, prices, stage and idea were collected and
then had no effect on the number. So the first block below changes ONE
business parameter on an otherwise identical plan and checks the
forecast moves the way Reference/FORECAST_MODEL.md says it should.
Each of those runs twice -- against the trained forest on disk and
against the documented formula it is trained to reproduce -- because a
direction the formula has and the forest lost would be a training bug,
and one the forest has and the formula lacks would be a documentation
bug.

A NOTE ON "UP OR EQUAL" FOR THE FOREST. A random forest is a step
function fitted to noisy labels (N(0, 3) noise on the scorecard), and
it recovers the noise-free scorecard to within ~1.8 points on average
(training_report.json, plan_model.mae_vs_noise_free_scorecard_points).
So a single change of input can move it the wrong way by about that
much -- occasionally more. Measured, not assumed (100,000 random
realistic plans -- five seeds of 20,000 -- capital doubled on each,
against the model retrained with the wage and margin sampled): the
forest's PVI went DOWN for about one plan in eight, almost always by
under half a point; by more than 1 point for 0.35%, by more than 2
points for 0.11% (about 1 plan in 1,000), worst seen 4.2 points (0.42
on the 0-10 score the owner sees). The over-a-point drops are mostly
thin-capital plans (capital adequacy under 0.2), where the formula
rises steeply but the forest has to read capital, rent, staff and --
now that it varies in training -- the wage together to place the plan;
the very largest were plans the forest had over-scored to begin with,
by 5-8 points, so the drop was its own error unwinding. Averaged over
plans, the forest's gain from doubling capital matches the formula's
to within a tenth of a point (2.26 vs 2.34). Reference/FORECAST_MODEL.md
states the same figures.

So the fixed-plan series below are checked within FOREST_WOBBLE (1
point -- enough for THESE plans, which sit in well-sampled territory),
the population-level behaviour is checked by
test_doubling_capital_helps_on_average_and_never_badly_hurts with its
own, wider, documented tolerance, and every direction is ALSO checked
over a range wide enough that the formula moves by several points,
where a wrong-way answer could not hide in the noise. The formula
itself is checked exactly (no tolerance).

The rest: the explanation adds up exactly, the payload is the
contract the pages and the narrator read (JSON-safe, exact keys), the
formula fallback works without a trained model, training and inference
build the feature row the same way, stage 1 is untouched, and
generate_forecast_for_profile stores the plan's numbers.
"""

import json
import os
import re
from datetime import date, timedelta

import joblib
import numpy as np
import pytest

from app import create_app
from app.extensions import db
from app.ml import plan_model as pm
from app.ml.constants import (
    FEATURE_NAMES,
    OPERATING_DAYS_PER_MONTH,
    PLAN_COMPONENT_WEIGHTS,
    PLAN_FEATURE_NAMES,
)
from app.models import LguData, MarketData, SmeProfile, SystemSetting, User
from app.services import plan_forecast_service as pfs

FOOD = "Food and Beverage"
LOCATION = "Tibag"           # rent P18,000 in the reference profile
POPULATION = 12000
MODEL_STORE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "ml", "model_store")
PLAN_PKL = os.path.join(MODEL_STORE, pm.PLAN_MODEL_FILENAME)

# See the module docstring.
FOREST_WOBBLE = 1.0
# Largest wrong-way move doubling capital may cause in a RANDOM plan
# (worst seen over 100,000: 4.2 points), and the share of plans allowed
# to move the wrong way by more than a point (seen: 0.35%).
CAPITAL_WRONG_WAY_POINTS = 4.5
CAPITAL_WRONG_WAY_SHARE_OVER_1_POINT = 0.02


@pytest.fixture
def app(monkeypatch):
    # Offline and deterministic: no LLM call, no Google Places call.
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
    app = create_app("testing")
    app.config["PLACES_LIVE_FETCH"] = False
    app.config["GOOGLE_PLACES_API_KEY"] = ""
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        pfs.reset_plan_model_cache()
        yield app
        pfs.reset_plan_model_cache()
        db.session.remove()
        db.drop_all()


@pytest.fixture(params=["forest", "formula"])
def mode(request, app):
    """Run a test against the trained forest AND the formula fallback."""
    pfs.reset_plan_model_cache()
    if request.param == "forest":
        if not os.path.exists(PLAN_PKL):
            pytest.skip("no trained plan_model.pkl -- run `python -m app.ml.train_model --plan-only`")
        assert pfs.plan_model_is_trained()
    else:
        pfs._PLAN_MODEL_CACHE.update(model=None, loaded=True)
    yield request.param
    pfs.reset_plan_model_cache()


def _items(*pairs):
    return json.dumps([{"item": name, "price": price} for name, price in pairs])


def _plan(**overrides):
    fields = dict(
        user_id=1, business_name="Pan de Tibag", industry_type=FOOD, location=LOCATION,
        startup_capital=500000, employee_count=2, business_stage="startup",
        product_offering="Pandesal and coffee", innovation_idea=None, offering_details=None,
    )
    fields.update(overrides)
    return SmeProfile(**fields)


def _scores(msi=35.0, competitors=7):
    return {
        "saturation_index": msi, "industry_saturation_index": msi, "competitor_count": competitors,
        "cluster_label": "Moderate", "confidence_level": 88.0, "model_version": "rf_v1",
    }


def _forecast(profile=None, msi=35.0, competitors=7, population=POPULATION):
    return pfs.forecast_plan(profile or _plan(), _scores(msi, competitors), None, population)


def _viability(**plan_overrides):
    msi = plan_overrides.pop("msi", 35.0)
    return _forecast(_plan(**plan_overrides), msi=msi)["plan"]["viability_index"]


def _never_down(series, mode):
    tolerance = FOREST_WOBBLE if mode == "forest" else 1e-9
    for before, after in zip(series, series[1:]):
        assert after >= before - tolerance, f"viability fell {before} -> {after} in {series}"


# ---------------------------------------------------------------------
# 1. Every business parameter moves the forecast, the documented way
# ---------------------------------------------------------------------

def test_more_capital_never_lowers_viability_and_enough_of_it_raises_it(mode):
    series = [_viability(startup_capital=c) for c in (30_000, 150_000, 600_000, 2_000_000, 6_000_000)]
    _never_down(series, mode)
    assert series[-1] - series[0] >= 5.0, series


def _random_plan_inputs(rng):
    return {
        "saturation_index": rng.uniform(5, 65), "competitor_count": int(rng.integers(0, 30)),
        "population": rng.uniform(800, 20000), "monthly_rent": rng.uniform(5000, 35000),
        "employee_count": int(rng.integers(0, 8)), "daily_wage": 590.0, "gross_margin": 0.4,
        "capital": float(np.exp(rng.uniform(np.log(2e4), np.log(5e6)))),
        "is_existing": bool(rng.random() < 0.3), "years_in_operation": float(rng.uniform(0, 12)),
        "priced_item_count": int(rng.integers(0, 10)), "average_price": float(rng.uniform(20, 2000)),
        "has_offering_description": bool(rng.random() < 0.7), "has_innovation_idea": bool(rng.random() < 0.5),
    }


def test_doubling_capital_helps_on_average_and_never_badly_hurts(app):
    """The population-level version of the capital test above, with the
    forest's honest tolerance (module docstring): over 400 random
    realistic plans, doubling capital never lowers the FORMULA, raises
    the forest by as much as the formula on average, and lowers the
    forest by more than a point for at most 2% of plans and by no more
    than CAPITAL_WRONG_WAY_POINTS for any."""
    rng = np.random.default_rng(2026)
    plans = [_random_plan_inputs(rng) for _ in range(400)]
    doubled = [dict(p, capital=2 * p["capital"]) for p in plans]

    formula_gain = np.array([pm.scorecard_index(b) - pm.scorecard_index(a) for a, b in zip(plans, doubled)])
    assert (formula_gain >= -1e-9).all()
    assert formula_gain.mean() >= 1.0

    if not os.path.exists(PLAN_PKL):
        pytest.skip("no trained plan_model.pkl")
    model = pfs._load_plan_model()
    before = model.predict(np.array([pm.plan_feature_vector(p) for p in plans]))
    after = model.predict(np.array([pm.plan_feature_vector(p) for p in doubled]))
    wrong_way = before - after

    assert wrong_way.max() <= CAPITAL_WRONG_WAY_POINTS, wrong_way.max()
    assert (wrong_way > 1.0).mean() <= CAPITAL_WRONG_WAY_SHARE_OVER_1_POINT
    assert (after - before).mean() == pytest.approx(formula_gain.mean(), abs=0.5)


def test_more_employees_on_little_capital_shortens_the_runway(mode):
    lean = _forecast(_plan(startup_capital=60_000, employee_count=0))["financials"]
    staffed = _forecast(_plan(startup_capital=60_000, employee_count=6))["financials"]

    # Payroll is employees x wage x 26 days, exactly -- the wage is the
    # DOLE RBIII-26 default here.
    assert staffed["monthly_payroll"] == pytest.approx(6 * 590.0 * OPERATING_DAYS_PER_MONTH)
    assert staffed["monthly_fixed_cost"] - lean["monthly_fixed_cost"] == pytest.approx(6 * 590.0 * 26)
    assert staffed["capital_runway_months"] < lean["capital_runway_months"]
    assert staffed["capital_adequacy"] <= lean["capital_adequacy"]


def test_an_innovation_idea_raises_viability(mode):
    without = _viability(innovation_idea=None)
    with_idea = _viability(innovation_idea="Ube pandesal delivered warm before 6 a.m.")
    assert with_idea - without >= 1.0, (without, with_idea)


def test_an_existing_business_with_years_behind_it_scores_higher_than_a_startup(mode):
    startup = _viability(business_stage="startup")
    existing = _viability(business_stage="existing", registration_date=date.today() - timedelta(days=5 * 366))
    assert existing - startup >= 2.0, (startup, existing)


def test_a_sane_price_list_is_covered_by_the_market_and_helps(mode):
    no_prices = _forecast(_plan())
    priced = _forecast(_plan(offering_details=_items(("Pandesal (10 pcs)", 50), ("Ensaymada", 85),
                                                     ("Coffee", 90), ("Cake slice", 120), ("Loaf", 80))))

    fin = priced["financials"]
    assert priced["inputs"]["priced_item_count"] == 5
    assert priced["inputs"]["average_price"] == pytest.approx(85.0)
    # Required daily sales = fixed cost / (price x margin x 26), and a
    # sane price list needs fewer of them than the market can supply.
    assert fin["required_daily_sales"] == pytest.approx(
        fin["monthly_fixed_cost"] / (85.0 * 0.40 * 26), abs=0.06)
    assert 0 < fin["required_daily_sales"] < fin["daily_sales_ceiling"]
    coverage = {c["key"]: c["score"] for c in priced["components"]}["price_coverage"]
    assert coverage > 0.5          # better than "unknown" (0.5)

    assert priced["plan"]["viability_index"] > no_prices["plan"]["viability_index"]


def test_an_impossible_price_list_scores_worse_coverage_than_a_sane_one(mode):
    sane = _forecast(_plan(offering_details=_items(("Coffee", 90), ("Cake", 120))))
    absurd = _forecast(_plan(offering_details=_items(("Candy", 1), ("Gum", 2))))
    cov = lambda f: {c["key"]: c["score"] for c in f["components"]}["price_coverage"]
    assert cov(absurd) < cov(sane)
    assert absurd["financials"]["required_daily_sales"] > absurd["financials"]["daily_sales_ceiling"]


def test_a_more_saturated_market_lowers_viability(mode):
    calm = _viability(msi=20.0)
    crowded = _viability(msi=55.0)
    assert calm - crowded >= 5.0, (calm, crowded)


@pytest.mark.parametrize("price", [1e39, float("inf"), float("-inf"), float("nan"), 1e20, 0.001, 0])
def test_a_price_that_is_not_a_price_point_is_left_out_not_crashed_on(mode, price):
    """"1e39" and "inf" get through the browser's number input, and a
    saved plan is forecast on every visit to Home -- so one such price
    used to raise inside the forest's predict() (float32 overflow) and
    take Home down for good. A price outside P0.01-P10,000,000 is now
    simply not a price point: the item still describes the offering."""
    # json.dumps writes inf/nan as Infinity/NaN, which offering_items
    # reads straight back -- exactly what the database would hold.
    items = json.dumps([{"item": "Coffee", "price": price}, {"item": "Cake", "price": 120}])
    payload = _forecast(_plan(offering_details=items))

    assert payload["inputs"]["priced_item_count"] == 1
    assert payload["inputs"]["average_price"] == 120.0
    assert payload["inputs"]["has_offering_description"] is True
    json.dumps(payload, allow_nan=False)        # strict JSON: no Infinity / NaN anywhere

    only_bad = _forecast(_plan(offering_details=json.dumps([{"item": "Coffee", "price": price}])))
    assert only_bad["inputs"]["priced_item_count"] == 0
    assert only_bad["financials"]["required_daily_sales"] == 0.0
    json.dumps(only_bad, allow_nan=False)


def test_extreme_inputs_stay_finite_and_inside_float32(mode):
    """Whatever reaches stage 2 -- a legacy row, a hand-edited setting --
    the feature row stays something the forest can read, and a forecast
    is made rather than a ValueError raised."""
    inputs = {
        "saturation_index": float("nan"), "competitor_count": float("inf"), "population": float("inf"),
        "monthly_rent": float("nan"), "employee_count": 1e300, "daily_wage": 1e308,
        "gross_margin": float("nan"), "capital": float("inf"), "is_existing": True,
        "years_in_operation": float("inf"), "priced_item_count": float("inf"), "average_price": 1e-300,
        "has_offering_description": True, "has_innovation_idea": False,
    }
    vector = pm.plan_feature_vector(inputs)
    assert pm.tree_safe(vector), vector
    assert not pm.tree_safe([1.0, float("inf")]) and not pm.tree_safe([1e39]) and not pm.tree_safe([float("nan")])

    result = pfs._predict(inputs)
    assert 0.0 <= result["viability_index"] <= 100.0

    # And end to end, with a capital no form would accept.
    payload = _forecast(_plan(startup_capital=float("inf"), employee_count=10**40))
    json.dumps(payload, allow_nan=False)
    assert 0.0 <= payload["plan"]["viability_index"] <= 100.0


def test_inputs_inside_the_ceiling_are_read_unchanged():
    """The guard must not move a real figure -- or the model trained on
    those figures would be answering a different question."""
    for value in (0, 1, 590.0, 12_345.67, 9_999_999_999.99, 1e12):
        assert pm.finite_input(value) == float(value)
    assert pm.finite_input(None, 7.0) == 7.0
    assert pm.finite_input("abc", 7.0) == 7.0
    assert pm.finite_input(float("nan"), 7.0) == 7.0
    assert pm.finite_input(float("inf")) == 1e12 and pm.finite_input(float("-inf")) == -1e12


def test_the_business_name_is_not_a_business_parameter(mode):
    """Same plan, different name: same forecast. A model that scored a
    plan by what it is called would be a bug."""
    a = _forecast(_plan(business_name="A"))
    b = _forecast(_plan(business_name="Something completely different"))
    assert a["plan"] == b["plan"] and a["drivers"] == b["drivers"]


# ---------------------------------------------------------------------
# 2. The explanation adds up -- exactly
# ---------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(PLAN_PKL), reason="no trained plan_model.pkl")
def test_path_decomposition_sums_exactly_to_the_forest_prediction(app):
    model = pfs._load_plan_model()
    assert model is not None
    rng = np.random.default_rng(7)
    for _ in range(25):
        inputs = {
            "saturation_index": rng.uniform(5, 75), "competitor_count": int(rng.integers(0, 40)),
            "population": rng.uniform(300, 25000), "monthly_rent": rng.uniform(5000, 35000),
            "employee_count": int(rng.integers(0, 15)), "daily_wage": 590.0, "gross_margin": 0.4,
            "capital": float(np.exp(rng.uniform(np.log(1e4), np.log(1e7)))),
            "is_existing": bool(rng.random() < 0.3), "years_in_operation": float(rng.uniform(0, 12)),
            "priced_item_count": int(rng.integers(0, 12)), "average_price": float(rng.uniform(10, 5000)),
            "has_offering_description": bool(rng.random() < 0.7), "has_innovation_idea": bool(rng.random() < 0.5),
        }
        vector = pm.plan_feature_vector(inputs)
        baseline, contributions, prediction, trees = pm.path_contributions(model, vector)
        assert abs(baseline + contributions.sum() - prediction) < 1e-6
        assert prediction == pytest.approx(float(model.predict(np.array([vector]))[0]), abs=1e-9)
        assert len(trees) == len(model.estimators_)

        # The fast path the quarterly outlook uses gives the forest's
        # own answer, to the last bit, from the same leaves.
        leaves = pm.tree_leaf_values(model, vector)
        assert np.array_equal(leaves, [t.predict(np.array([vector]))[0] for t in model.estimators_])
        assert np.array_equal(leaves, trees)
        assert pm.forest_mean(leaves) == float(model.predict(np.array([vector]))[0])


def test_the_payload_breakdown_adds_up_to_the_reported_viability(mode):
    for profile in (_plan(), _plan(startup_capital=40_000, employee_count=5),
                    _plan(innovation_idea="x", offering_details=_items(("Coffee", 90)))):
        payload = _forecast(profile)
        total = payload["baseline"] + sum(d["points"] for d in payload["drivers"])
        assert round(total, 1) == payload["plan"]["viability_index"]
        # Biggest effect first.
        sizes = [abs(d["points"]) for d in payload["drivers"]]
        assert sizes == sorted(sizes, reverse=True)
        assert {d["key"] for d in payload["drivers"]} == set(pm.DRIVER_LABELS)


def test_a_decomposition_that_does_not_add_up_is_refused():
    """The self-check is real: a forest whose prediction does not equal
    its own decision paths (here, one that adds a point) raises rather
    than reporting a wrong explanation."""
    from sklearn.tree import DecisionTreeRegressor

    X = np.random.default_rng(0).uniform(0, 1, (50, len(PLAN_FEATURE_NAMES)))
    tree = DecisionTreeRegressor(max_depth=3, random_state=0).fit(X, X[:, 0] * 10)

    class Shifted:
        estimators_ = [tree]

        def predict(self, rows):
            return tree.predict(rows) + 1.0

    with pytest.raises(pm.DecompositionError):
        pm.path_contributions(Shifted(), X[0])


def test_round_preserving_sum_keeps_the_total():
    parts = [52.04, 3.33, -1.26, 0.04, 0.04, 0.04]
    rounded = pm.round_preserving_sum(parts, sum(parts), 1)
    assert round(sum(rounded), 1) == round(sum(parts), 1)
    assert all(abs(r - p) < 0.1 + 1e-9 for r, p in zip(rounded, parts))


# ---------------------------------------------------------------------
# 3. The payload contract
# ---------------------------------------------------------------------

CONTRACT = {
    "top": {"version", "market", "plan", "inputs", "financials", "components", "baseline", "drivers"},
    "market": {"saturation_index", "industry_saturation_index", "cluster_label", "competitor_count",
               "confidence", "model_version"},
    "plan": {"viability_index", "viability_score", "confidence", "model_version", "scorecard_index"},
    "inputs": {"capital", "employee_count", "business_stage", "years_in_operation", "priced_item_count",
               "average_price", "has_offering_description", "has_innovation_idea", "industry_type",
               "subcategory_label", "location", "population", "residents_per_business"},
    "financials": {"monthly_rent", "daily_wage", "monthly_payroll", "monthly_fixed_cost",
                   "capital_runway_months", "ramp_up_months", "capital_adequacy", "gross_margin",
                   "operating_days", "required_daily_sales", "daily_sales_ceiling", "break_even"},
    "break_even": {"low_months", "high_months", "label"},
    "component": {"key", "label", "aspect", "score", "weight", "points", "inputs"},
    "driver": {"key", "label", "points"},
}


def _assert_plain_json(value, path="payload"):
    """No numpy scalar, no Decimal, nothing json.dumps would choke on or
    silently stringify."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), path
            _assert_plain_json(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_plain_json(item, f"{path}[{index}]")
    else:
        assert value is None or type(value) in (str, int, float, bool), f"{path} is {type(value)!r}"


def test_the_payload_has_exactly_the_contract_keys_and_is_plain_json(mode):
    payload = _forecast(_plan(innovation_idea="x", offering_details=_items(("Coffee", 90), ("Cake", 120))))

    assert set(payload) == CONTRACT["top"]
    for section in ("market", "plan", "inputs", "financials"):
        assert set(payload[section]) == CONTRACT[section], section
    assert set(payload["financials"]["break_even"]) == CONTRACT["break_even"]
    assert all(set(c) == CONTRACT["component"] for c in payload["components"])
    assert all(set(d) == CONTRACT["driver"] for d in payload["drivers"])

    _assert_plain_json(payload)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["version"] == pm.PAYLOAD_VERSION


def test_the_payload_reports_the_scorecard_it_is_trained_on(mode):
    payload = _forecast()
    components = payload["components"]
    assert [c["key"] for c in components] == [
        k for k, _w in sorted(PLAN_COMPONENT_WEIGHTS.items(), key=lambda kv: -kv[1])
    ]
    assert sum(c["weight"] for c in components) == pytest.approx(1.0)
    assert sum(c["points"] for c in components) == pytest.approx(payload["plan"]["scorecard_index"], abs=0.4)
    assert payload["plan"]["viability_score"] == round(payload["plan"]["viability_index"] / 10, 1)


def test_capital_and_location_reach_the_financials(mode):
    payload = _forecast(_plan(startup_capital=480_000, employee_count=2))
    fin = payload["financials"]
    assert payload["inputs"]["capital"] == 480000.0
    assert fin["monthly_rent"] == 18000.0                      # Tibag's reference rent
    assert fin["monthly_fixed_cost"] == pytest.approx(18000 + 2 * 590 * 26)
    assert fin["capital_runway_months"] == pytest.approx(480000 / fin["monthly_fixed_cost"], abs=0.05)
    assert payload["inputs"]["residents_per_business"] == pytest.approx(POPULATION / 8, abs=0.05)
    assert fin["break_even"]["low_months"] < fin["break_even"]["high_months"]


def test_admin_assumptions_feed_the_forecast(mode):
    SystemSetting.set(pfs.WAGE_SETTING_KEY, "700")
    SystemSetting.set(pfs.MARGIN_SETTING_KEY, "0.5")
    fin = _forecast(_plan(employee_count=1, offering_details=_items(("Coffee", 100))))["financials"]
    assert fin["daily_wage"] == 700.0 and fin["gross_margin"] == 0.5
    assert fin["monthly_payroll"] == pytest.approx(700 * 26)

    # A broken setting is not an assumption: the documented default stands.
    SystemSetting.set(pfs.WAGE_SETTING_KEY, "0")
    SystemSetting.set(pfs.MARGIN_SETTING_KEY, "1.5")
    assert pfs.plan_assumptions() == (590.0, 0.40)
    # Nor is a wage too large to be a wage (it used to overflow the
    # forest's input for every plan with staff).
    for wage in ("1e37", "1e308", "inf", "nan", "100001"):
        SystemSetting.set(pfs.WAGE_SETTING_KEY, wage)
        assert pfs.plan_assumptions()[0] == 590.0, wage


def test_a_stored_payload_hands_back_the_assumptions_it_used(mode):
    """What the quarterly outlook relies on to keep Q1 equal to the
    gauge after an Admin changes the wage or margin."""
    SystemSetting.set(pfs.WAGE_SETTING_KEY, "612.345")
    SystemSetting.set(pfs.MARGIN_SETTING_KEY, "0.37256")
    payload = _forecast(_plan(employee_count=2, offering_details=_items(("Coffee", 90))))

    used = pfs.plan_assumptions()
    assert used == (612.35, 0.3726) or used == (612.34, 0.3726)   # float rounding of .345
    assert pfs.assumptions_from_payload(payload) == used
    assert pfs.assumptions_from_payload(None) is None
    assert pfs.assumptions_from_payload({"financials": {}}) is None
    assert pfs.assumptions_from_payload({"financials": {"daily_wage": 1e37, "gross_margin": 0.4}}) is None

    # Holding them reproduces the stored viability exactly, whatever the
    # settings say now.
    SystemSetting.set(pfs.WAGE_SETTING_KEY, "900")
    SystemSetting.set(pfs.MARGIN_SETTING_KEY, "0.15")
    profile = _plan(employee_count=2, offering_details=_items(("Coffee", 90)))
    held = pfs.build_plan_inputs(profile, _scores(), None, POPULATION,
                                 assumptions=pfs.assumptions_from_payload(payload))
    again = pfs.plan_viability_for(held, 35.0, 7)
    assert again["viability_index"] == payload["plan"]["viability_index"]


def test_the_break_even_window_is_fed_the_unrounded_viability(mode):
    """estimate_roi_timeframe(PVI / 10, MSI*, None, None), as
    Reference/FORECAST_MODEL.md states -- not the twice-rounded 1-dp
    score, which moved one end of the window by a month for ~8% of
    plans."""
    from app.services.location_opportunity_service import estimate_roi_timeframe

    for profile in (_plan(), _plan(startup_capital=40_000, employee_count=5),
                    _plan(innovation_idea="x", offering_details=_items(("Coffee", 90)))):
        payload = _forecast(profile)
        pvi = pfs._predict(pfs.build_plan_inputs(profile, _scores(), None, POPULATION))["viability_index"]
        expected = estimate_roi_timeframe(pvi / 10.0, 35.0, None, None)
        assert payload["financials"]["break_even"] == {
            "low_months": expected["low_months"], "high_months": expected["high_months"],
            "label": expected["label"],
        }


def test_an_unknown_location_uses_the_documented_population_fallback(mode):
    payload = pfs.forecast_plan(_plan(location="Nowhere In Particular"), _scores(), None, None)
    assert payload["inputs"]["population"] == int(pm.fallback_population())
    assert payload["financials"]["monthly_rent"] == 15000.0


def test_a_legacy_plan_with_no_capital_still_forecasts(mode):
    payload = _forecast(_plan(startup_capital=None))
    assert payload["inputs"]["capital"] == 0.0
    assert payload["financials"]["capital_runway_months"] == 0.0
    assert payload["financials"]["capital_adequacy"] == 0.0
    assert 0.0 <= payload["plan"]["viability_index"] <= 100.0


def _admin_client(app):
    admin = User(name="Admin", email="admin@planmodel.test", role="Admin")
    admin.set_password("password123")
    db.session.add(admin)
    db.session.commit()
    client = app.test_client()
    client.post("/login", data={"email": "admin@planmodel.test", "password": "password123"})
    return client


def test_the_admin_settings_page_edits_the_plan_assumptions(app):
    client = _admin_client(app)
    page = client.get("/admin/settings").get_data(as_text=True)
    assert "Plan forecast assumptions" in page
    assert 'name="plan_daily_wage_php"' in page and 'name="plan_gross_margin"' in page
    assert "RBIII-26" in page

    client.post("/admin/settings", data={"plan_daily_wage_php": "610", "plan_gross_margin": "0.35"})
    assert SystemSetting.get(pfs.WAGE_SETTING_KEY) == "610"
    assert SystemSetting.get(pfs.MARGIN_SETTING_KEY) == "0.35"
    # Centavos are kept in full (":g" used to cut this to 12345.7).
    client.post("/admin/settings", data={"plan_daily_wage_php": "12,345.67"})
    assert SystemSetting.get(pfs.WAGE_SETTING_KEY) == "12345.67"


def test_the_admin_settings_page_says_gemini_narrates_the_forecast(app):
    """The AI-text switch describes what it actually does now: Gemini
    first for the forecast narration, figures from the trained models."""
    page = _admin_client(app).get("/admin/settings").get_data(as_text=True)
    assert "GPT or Claude" not in page
    assert "Gemini" in page and "GEMINI_API_KEY" in page and "LLM_PROVIDER" in page
    assert 'max="100000"' in page          # the wage input carries its upper bound too

    from app.models.system_setting import DEFAULT_SETTINGS

    description = DEFAULT_SETTINGS["use_llm_recommendations"][1]
    assert "Gemini" in description and "GPT-4o-mini via OpenRouter" not in description
    assert all(len(text) <= 255 for _value, text in DEFAULT_SETTINGS.values())


def test_an_existing_install_gets_the_current_description_but_keeps_its_value(app):
    """A row created before the narration went Gemini first still held
    the old "GPT-4o-mini via OpenRouter, or Claude" note. ensure_defaults
    refreshes the note -- the code's own documentation -- and leaves the
    Admin's chosen value alone."""
    from app.models.system_setting import DEFAULT_SETTINGS

    stale = "true = use an LLM (GPT-4o-mini via OpenRouter, or Claude ...)"
    llm = SystemSetting.query.get("use_llm_recommendations")
    llm.description = stale
    wage = SystemSetting.query.get(pfs.WAGE_SETTING_KEY)
    wage.setting_value, wage.description = "610", "an older note"
    db.session.commit()

    SystemSetting.ensure_defaults()

    assert SystemSetting.query.get("use_llm_recommendations").description == \
        DEFAULT_SETTINGS["use_llm_recommendations"][1]
    wage = SystemSetting.query.get(pfs.WAGE_SETTING_KEY)
    assert wage.description == DEFAULT_SETTINGS[pfs.WAGE_SETTING_KEY][1]
    assert wage.setting_value == "610"


def test_the_admin_can_turn_the_llm_switch_off_and_it_stays_off(app):
    """Unticking "Use an LLM..." used to be undone by the redirect after
    the save: the settings page's ensure_defaults() "upgraded" the
    stored "false" back to "true" as if it were an untouched old default.
    Off must stay off, through the page reload and a restart."""
    from app.services.startup_migrations import run_startup_migrations

    client = _admin_client(app)
    client.post("/admin/settings", data={})          # the checkbox unticked
    assert SystemSetting.get("use_llm_recommendations") == "false"

    page = client.get("/admin/settings").get_data(as_text=True)
    assert SystemSetting.get("use_llm_recommendations") == "false"
    toggle = re.search(r'<input[^>]*id="llmToggle"[^>]*>', page).group(0)
    assert "checked" not in toggle                    # the page shows it off, too
    run_startup_migrations(app)
    assert SystemSetting.get("use_llm_recommendations") == "false"

    client.post("/admin/settings", data={"use_llm_recommendations": "on"})
    assert SystemSetting.get("use_llm_recommendations") == "true"


@pytest.mark.parametrize("field,value", [
    ("plan_daily_wage_php", "0"), ("plan_daily_wage_php", "-5"), ("plan_daily_wage_php", "abc"),
    ("plan_daily_wage_php", "0.5"), ("plan_daily_wage_php", "100001"), ("plan_daily_wage_php", "1e37"),
    ("plan_daily_wage_php", "inf"), ("plan_daily_wage_php", "nan"),
    ("plan_gross_margin", "0.01"), ("plan_gross_margin", "0.99"), ("plan_gross_margin", "40"),
])
def test_the_admin_settings_page_refuses_a_broken_assumption(app, field, value):
    client = _admin_client(app)
    before = SystemSetting.get(field)
    page = client.post("/admin/settings", data={field: value}, follow_redirects=True).get_data(as_text=True)
    assert SystemSetting.get(field) == before
    assert "Settings saved, except the" in page


# ---------------------------------------------------------------------
# 4. No trained model: the documented formula, labelled as such
# ---------------------------------------------------------------------

def test_without_a_model_file_the_formula_stands_in(app, tmp_path):
    app.config["MODEL_DIR"] = str(tmp_path)
    pfs.reset_plan_model_cache()
    assert not pfs.plan_model_is_trained()

    profile = _plan(innovation_idea="x")
    payload = _forecast(profile)
    plan = payload["plan"]
    assert plan["model_version"] == pm.PLAN_FORMULA_VERSION
    assert plan["confidence"] == pfs.FORMULA_CONFIDENCE
    assert plan["viability_index"] == plan["scorecard_index"]
    assert payload["baseline"] == 0.0
    assert round(sum(d["points"] for d in payload["drivers"]), 1) == plan["viability_index"]
    assert pfs.combined_model_version("rf_v1", plan["model_version"]) == "rf_v1+plan_fx_v1"


def test_a_model_trained_on_a_different_feature_order_is_refused(app, tmp_path):
    if not os.path.exists(PLAN_PKL):
        pytest.skip("no trained plan_model.pkl")
    bundle = joblib.load(PLAN_PKL)
    bundle["feature_names"] = list(reversed(PLAN_FEATURE_NAMES))
    joblib.dump(bundle, tmp_path / pm.PLAN_MODEL_FILENAME)
    app.config["MODEL_DIR"] = str(tmp_path)
    pfs.reset_plan_model_cache()

    assert not pfs.plan_model_is_trained()
    assert _forecast()["plan"]["model_version"] == pm.PLAN_FORMULA_VERSION


def _first_loads_race(app, load, read_attr_owner, read_attr, reset, threads=4):
    """Start `threads` first-time loads at once while the file read is
    slowed down, the way a burst of requests meets a cold gunicorn
    worker (--threads 4). Returns what each thread got, and how many
    times the file was actually read."""
    import threading
    import time

    original = getattr(read_attr_owner, read_attr)
    reads = []

    def slow_read(*args, **kwargs):
        reads.append(1)
        time.sleep(0.3)
        return original(*args, **kwargs)

    reset()
    results, barrier = [], threading.Barrier(threads)

    def worker():
        with app.app_context():
            barrier.wait()
            results.append(load())

    setattr(read_attr_owner, read_attr, slow_read)
    try:
        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(timeout=30)
    finally:
        setattr(read_attr_owner, read_attr, original)
    return results, len(reads)


@pytest.mark.skipif(not os.path.exists(PLAN_PKL), reason="no trained plan_model.pkl")
def test_requests_arriving_during_the_first_load_wait_for_the_model(app):
    """The cache used to be marked loaded BEFORE the 12 MB file was read,
    so a second thread arriving mid-read got None, forecast with the
    formula and stored "plan_fx_v1" for that plan. Every thread must now
    get the trained model, from one read of the file."""
    results, reads = _first_loads_race(app, pfs._load_plan_model, pfs, "_read_plan_model",
                                       pfs.reset_plan_model_cache)
    assert len(results) == 4 and all(model is not None for model in results)
    assert all(model is results[0] for model in results)
    assert reads == 1


def test_stage_one_requests_arriving_during_its_first_load_wait_too(app):
    from app.services import forecasting_service as fs

    if not os.path.exists(os.path.join(MODEL_STORE, "rf_model.pkl")):
        pytest.skip("no trained rf_model.pkl")

    def reset():
        fs._MODEL_CACHE.update(rf=None, loaded=False)

    def load():
        fs._load_models()
        return fs._MODEL_CACHE["rf"]

    results, reads = _first_loads_race(app, load, fs.joblib, "load", reset)
    assert len(results) == 4 and all(model is not None for model in results)
    assert reads == 1


# ---------------------------------------------------------------------
# 5. Training and inference build the same row
# ---------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(PLAN_PKL), reason="no trained plan_model.pkl")
def test_the_trained_bundle_records_the_feature_order_the_code_uses():
    bundle = joblib.load(PLAN_PKL)
    assert bundle["feature_names"] == PLAN_FEATURE_NAMES
    assert bundle["model"].n_features_in_ == len(PLAN_FEATURE_NAMES)
    assert bundle["model_version"] == pm.PLAN_MODEL_VERSION


def test_training_rows_are_built_by_the_inference_function(monkeypatch):
    """train_model builds every row through plan_feature_vector(), and
    labels it with scorecard_index() -- the functions inference uses."""
    from sklearn.ensemble import RandomForestRegressor

    from app.ml import train_model as tm

    monkeypatch.setattr(tm, "N_PLAN_SAMPLES", 40)
    market = RandomForestRegressor(n_estimators=3, random_state=0).fit(
        np.random.default_rng(0).uniform(0, 50, (60, len(FEATURE_NAMES))),
        np.random.default_rng(1).uniform(0, 100, 60),
    )
    inputs, X, scorecard, labels = tm._make_plan_dataset(market, np.random.default_rng(3))

    assert X.shape == (40, len(PLAN_FEATURE_NAMES))
    for row, plan_inputs, s in zip(X, inputs, scorecard):
        assert list(row) == pm.plan_feature_vector(plan_inputs)
        assert s == pytest.approx(pm.scorecard_index(plan_inputs))
    assert ((labels >= 0) & (labels <= 100)).all()


def test_plan_feature_vector_follows_plan_feature_names():
    inputs = {"saturation_index": 40, "competitor_count": 9, "population": 10000, "monthly_rent": 12000,
              "employee_count": 3, "daily_wage": 590, "gross_margin": 0.4, "capital": 300000,
              "is_existing": True, "years_in_operation": 4, "priced_item_count": 3, "average_price": 60,
              "has_offering_description": True, "has_innovation_idea": False}
    derived = pm.derive_quantities(inputs)
    vector = pm.plan_feature_vector(inputs)
    assert vector == [float(derived[name]) for name in PLAN_FEATURE_NAMES]
    assert vector[PLAN_FEATURE_NAMES.index("market_saturation")] == 40
    assert vector[PLAN_FEATURE_NAMES.index("residents_per_business")] == pytest.approx(1000)
    assert vector[PLAN_FEATURE_NAMES.index("monthly_fixed_cost")] == pytest.approx(12000 + 3 * 590 * 26)


# ---------------------------------------------------------------------
# 6. Stage 1 is untouched; the ROI window shares stage 2's ramp formula
# ---------------------------------------------------------------------

def test_stage_one_scores_are_unchanged(app):
    from app.services.forecasting_service import compute_scores

    result = compute_scores(FOOD, LOCATION)
    assert set(result) == {"industry_type", "location", "market_id", "lgu_id", "competitor_count",
                           "saturation_index", "viability_score", "confidence_level",
                           "cluster_label", "model_version"}
    # A MARKET's viability is still the market formula.
    assert result["viability_score"] == round(max(0.0, min(10.0, (100 - result["saturation_index"]) / 10)), 1)
    assert result["model_version"] in ("rf_v1", "formula_v1")


def test_the_training_report_keeps_stage_one_and_adds_plan_model():
    path = os.path.join(MODEL_STORE, "training_report.json")
    if not os.path.exists(path):
        pytest.skip("no training_report.json")
    with open(path) as f:
        report = json.load(f)
    for key in ("trained_on_samples", "test_samples", "mae_points", "rmse_points", "accuracy_percent",
                "target_accuracy_percent", "meets_target", "feature_importances",
                "kmeans_cluster_to_label", "msi_weights", "note"):
        assert key in report, key
    assert list(report["feature_importances"]) == FEATURE_NAMES
    if os.path.exists(PLAN_PKL):
        plan = report["plan_model"]
        assert plan["feature_names"] == PLAN_FEATURE_NAMES
        for key in ("mae_points", "rmse_points", "r2", "accuracy_percent", "feature_importances",
                    "component_weights", "assumptions"):
            assert key in plan, key


def test_the_roi_window_uses_the_shared_ramp_formula():
    from app.services.location_opportunity_service import MINIMUM_RAMP_MONTHS, estimate_roi_timeframe

    for viability, saturation in ((8.0, 50), (2.5, 80), (9.9, 5), (5.0, 35)):
        midpoint = pm.ramp_midpoint_months(viability, saturation)
        roi = estimate_roi_timeframe(viability, saturation, None, None)
        assert roi["low_months"] == max(MINIMUM_RAMP_MONTHS, int(round(midpoint * 0.8)))
        assert pm.ramp_up_months(viability, saturation) == max(MINIMUM_RAMP_MONTHS, midpoint)


def test_location_cards_say_their_score_is_the_markets_not_the_plans(app):
    """The Recommendations page's location cards are one city-wide
    stage-1 sweep -- capital never enters them -- so a card must not
    claim it was "scored against your own plan parameters", next to a
    plan forecast whose Plan Viability for the same barangay does weigh
    capital and differs."""
    from app.services.location_opportunity_service import _reasons_for

    row = {"competitor_count": 3, "residents_per_business": 900, "population": 9000,
           "population_density": None, "saturation_index": 30.0, "confidence_level": 85.0}
    city = {"median_competitors": 5, "median_residents_per_business": 800, "median_density": 0,
            "barangays_scored": 76}
    for capital, quoted in ((20_000, "PHP 20,000"), (5_000_000, "PHP 5,000,000"), (None, "your capital")):
        reasons = " ".join(_reasons_for(row, city, {}, _plan(startup_capital=capital), "Food"))
        assert "plan parameters" not in reasons
        assert "market-only score" in reasons and "Plan Viability" in reasons
        assert quoted in reasons
    # Without a plan, nothing is said about one.
    assert "Plan Viability" not in " ".join(_reasons_for(row, city, {}, None, "Food"))


# ---------------------------------------------------------------------
# 7. generate_forecast_for_profile stores the plan's forecast
# ---------------------------------------------------------------------

def _market_on_file(user_id, competitors=12):
    db.session.add(MarketData(
        industry_type=FOOD, location=LOCATION, competitor_count=competitors,
        population_density=2320.0, historical_success_rate=0.71, foot_traffic_index=31,
        average_rent=18000, source="Google Places API", date_recorded=date.today(),
    ))
    db.session.add(LguData(
        source="DTI", barangay=LOCATION, permit_count=5, business_density=2.9,
        upload_date=date.today(), uploaded_by=user_id,
    ))
    db.session.commit()


def _owner():
    user = User(name="Owner", email="planowner@example.com", role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def test_generate_forecast_for_profile_stores_the_plan_viability_and_payload(app):
    from app.services.forecasting_service import generate_forecast_for_profile

    user = _owner()
    _market_on_file(user.user_id)
    profile = _plan(user_id=user.user_id, startup_capital=750_000, employee_count=3,
                    innovation_idea="Ube pandesal", offering_details=_items(("Pandesal", 50), ("Coffee", 90)))
    db.session.add(profile)
    db.session.commit()

    forecast = generate_forecast_for_profile(profile)
    stored = json.loads(forecast.recommendation)
    payload = stored["forecast"]

    assert set(payload) == CONTRACT["top"]
    assert float(forecast.viability_score) == pytest.approx(payload["plan"]["viability_score"])
    assert float(forecast.saturation_index) == pytest.approx(payload["market"]["saturation_index"], abs=0.051)
    assert float(forecast.confidence_level) == pytest.approx(
        min(payload["market"]["confidence"], payload["plan"]["confidence"]), abs=0.051)

    expected_version = pfs.combined_model_version(payload["market"]["model_version"],
                                                  payload["plan"]["model_version"])
    assert forecast.model_version == expected_version
    assert len(forecast.model_version) <= 20
    if os.path.exists(PLAN_PKL) and payload["market"]["model_version"] == "rf_v1":
        assert forecast.model_version == "rf_v1+plan_rf_v1"

    assert payload["inputs"]["capital"] == 750000.0
    assert payload["inputs"]["employee_count"] == 3
    assert payload["inputs"]["priced_item_count"] == 2


def test_trend_reports_keep_averaging_the_market_viability(app):
    """Trend Reports are about markets (stage 1 only). A plan forecast
    enters their averages through its saturation, exactly as before --
    not through the plan's viability, which weighs the owner's capital."""
    from app.services.forecasting_service import generate_forecast_for_profile
    from app.services.trend_analytics_service import get_overview_stats

    user = _owner()
    _market_on_file(user.user_id)
    profile = _plan(user_id=user.user_id, startup_capital=15_000, employee_count=6)
    db.session.add(profile)
    db.session.commit()
    forecast = generate_forecast_for_profile(profile)

    market_viability = round((100 - float(forecast.saturation_index)) / 10, 1)
    assert float(forecast.viability_score) != market_viability   # the plan's own figure differs
    assert get_overview_stats([], [forecast])["avg_viability"] == market_viability


def test_two_plans_in_one_market_now_differ_by_their_parameters(app):
    """The point of stage 2, end to end: same industry, same barangay,
    same day -- different capital and staffing, different forecast."""
    from app.services.forecasting_service import generate_forecast_for_profile

    user = _owner()
    _market_on_file(user.user_id)
    thin = _plan(user_id=user.user_id, business_name="Thin", startup_capital=25_000, employee_count=4,
                 product_offering=None)
    solid = _plan(user_id=user.user_id, business_name="Solid", startup_capital=2_500_000, employee_count=3,
                  innovation_idea="Delivery before 6 a.m.", offering_details=_items(("Pandesal", 50), ("Coffee", 90)))
    db.session.add_all([thin, solid])
    db.session.commit()

    thin_forecast = generate_forecast_for_profile(thin)
    solid_forecast = generate_forecast_for_profile(solid)

    assert float(thin_forecast.saturation_index) == pytest.approx(float(solid_forecast.saturation_index))
    assert float(solid_forecast.viability_score) > float(thin_forecast.viability_score)


# ---------------------------------------------------------------------
# 8. The Admin's wage and margin are learnt; a stale model is retrained
# ---------------------------------------------------------------------
# The wage and gross margin used to be held at P590 / 40% for every
# training plan, so the forest never saw them vary: an Admin who halved
# the margin moved the scorecard by -1.2 points on an average priced
# plan and the forest by -0.1. Training now draws both per plan
# (train_model._PLAN_DAILY_WAGE_RANGE). And because plan_model.pkl is
# gitignored and a host's build cache can keep an old one, the bundle
# records a fingerprint of its training recipe that seed.py checks.

def test_training_draws_the_wage_and_margin_per_plan():
    """The forest can only learn an input that varies in its training
    data: every synthetic plan carries its own wage (P450-P800) and
    margin (15%-70%), and its features are built from them."""
    from app.ml import train_model as tm

    assert tm._PLAN_DAILY_WAGE_RANGE == (450.0, 800.0)
    assert tm._PLAN_GROSS_MARGIN_RANGE == (0.15, 0.70)

    inputs, X, _scorecard, _labels = tm._make_plan_dataset(
        tm._StandInMarketModel(), np.random.default_rng(3), n_samples=300)
    wages = np.array([p["daily_wage"] for p in inputs])
    margins = np.array([p["gross_margin"] for p in inputs])
    assert ((wages >= 450.0) & (wages <= 800.0)).all() and wages.std() > 50
    assert ((margins >= 0.15) & (margins <= 0.70)).all() and margins.std() > 0.1

    fixed_cost = PLAN_FEATURE_NAMES.index("monthly_fixed_cost")
    required = PLAN_FEATURE_NAMES.index("required_daily_sales")
    for row, plan in zip(X, inputs):
        assert row[fixed_cost] == pytest.approx(
            max(1000.0, plan["monthly_rent"] + plan["employee_count"] * plan["daily_wage"] * OPERATING_DAYS_PER_MONTH))
        if plan["priced_item_count"]:
            assert row[required] == pytest.approx(
                row[fixed_cost] / (plan["average_price"] * plan["gross_margin"] * OPERATING_DAYS_PER_MONTH))


def _assumption_plans(rng, n, priced):
    """`n` random realistic plans: all with a price list (priced=True,
    for the margin) or all with staff (priced=False, for the wage)."""
    plans = []
    while len(plans) < n:
        plan = _random_plan_inputs(rng)
        if priced:
            plan["priced_item_count"] = max(1, plan["priced_item_count"])
            plan["average_price"] = float(np.exp(rng.uniform(np.log(20), np.log(2000))))
        elif plan["employee_count"] == 0:
            continue
        plans.append(plan)
    return plans


@pytest.mark.skipif(not os.path.exists(PLAN_PKL), reason="no trained plan_model.pkl")
def test_the_forest_follows_the_admin_wage_and_margin_the_scorecard_way(app):
    """Over 2,000 random plans each: a wage of P800 instead of P590
    (plans with staff) and a margin of 20% instead of 40% (plans with a
    price list) lower the forest's PVI on average, as they lower the
    scorecard.

    HOW MUCH, honestly (Reference/FORECAST_MODEL.md has the figures).
    The wage gets through at about 60% of the scorecard's effect, as it
    did before the retrain: it moves capital runway, which the forest
    leans on most. The margin gets through at about 15% -- up from about
    9% when the margin was not sampled, and still small, because the
    margin reaches only required daily sales, and the forest learnt
    price coverage mostly from AVERAGE PRICE: price varies over three
    decades in training against the margin's factor of five, so a split
    on price was nearly as good as one on required daily sales. (Halving
    the PRICE, which changes required daily sales exactly as halving the
    margin does, gets through at about 70%.) The thresholds pin the
    direction always, and about two-thirds of each measured share, so a
    retrain that lost either effect would fail here."""
    model = pfs._load_plan_model()

    def mean_effects(plans, **change):
        changed = [dict(p, **change) for p in plans]
        forest = model.predict(np.array([pm.plan_feature_vector(p) for p in changed])) - \
            model.predict(np.array([pm.plan_feature_vector(p) for p in plans]))
        formula = np.array([pm.scorecard_index(c) - pm.scorecard_index(p) for p, c in zip(plans, changed)])
        return float(forest.mean()), float(formula.mean())

    forest, formula = mean_effects(_assumption_plans(np.random.default_rng(590), 2000, priced=False),
                                   daily_wage=800.0)
    assert formula < -0.5
    assert forest <= 0.4 * formula, (forest, formula)

    forest, formula = mean_effects(_assumption_plans(np.random.default_rng(40), 2000, priced=True),
                                   gross_margin=0.20)
    assert formula < -0.5
    assert forest <= 0.1 * formula, (forest, formula)


@pytest.mark.skipif(not os.path.exists(PLAN_PKL), reason="no trained plan_model.pkl")
def test_the_model_on_disk_was_trained_by_the_current_recipe():
    """The fingerprint seed.py checks. If this fails, plan_model.pkl is
    stale: run `python -m app.ml.train_model --plan-only` (seed.py would
    do it on its own)."""
    from app.ml import train_model as tm

    assert tm.plan_model_staleness() is None
    bundle = joblib.load(PLAN_PKL)
    sampling = bundle["training_config"]["sampling"]
    assert sampling["daily_wage_php_uniform"] == [450.0, 800.0]
    assert sampling["gross_margin_uniform"] == [0.15, 0.70]
    with open(os.path.join(MODEL_STORE, "training_report.json")) as f:
        report = json.load(f)["plan_model"]
    assert report["training_config_hash"] == bundle["training_config_hash"]
    assert report["sampling"]["daily_wage_php_uniform"] == [450.0, 800.0]


def _tiny_market_forest(seed=0):
    from sklearn.ensemble import RandomForestRegressor

    rng = np.random.default_rng(seed)
    return RandomForestRegressor(n_estimators=2, random_state=seed).fit(
        rng.uniform(0, 50, (40, len(FEATURE_NAMES))), rng.uniform(0, 100, 40))


def test_the_fingerprint_changes_with_the_recipe_and_only_with_it(monkeypatch):
    """Same code, same market model: the same fingerprint, every time.
    A changed named setting changes it; so does a changed FORMULA that no
    setting names (the recipe probe runs the real sampler and scorecard);
    so does a different market model."""
    from app.ml import train_model as tm

    market = _tiny_market_forest()

    def fingerprint(market_model=market):
        return tm.plan_training_config_hash(tm.plan_training_config(market_model))

    base = fingerprint()
    assert fingerprint() == base and len(base) == 64

    monkeypatch.setattr(tm, "_PLAN_GROSS_MARGIN_RANGE", (0.40, 0.40))
    assert fingerprint() != base
    monkeypatch.undo()
    assert fingerprint() == base

    monkeypatch.setattr(pm, "STAFF_FULL_CAPACITY", 4)       # a scorecard formula change
    assert fingerprint() != base
    monkeypatch.undo()

    assert fingerprint(_tiny_market_forest(seed=1)) != base


def test_plan_model_staleness_reads_the_fingerprint(tmp_path):
    from app.ml import train_model as tm

    market = _tiny_market_forest()
    joblib.dump(market, tmp_path / tm.RF_MODEL_FILE)
    plan_file = tmp_path / pm.PLAN_MODEL_FILENAME
    current = tm.plan_training_config_hash(tm.plan_training_config(market))

    def staleness_of(**extra):
        joblib.dump(dict({"model": None, "feature_names": list(PLAN_FEATURE_NAMES)}, **extra), plan_file)
        return tm.plan_model_staleness(str(tmp_path))

    assert tm.plan_model_staleness(str(tmp_path)) == "no plan viability model on disk"
    assert staleness_of(training_config_hash=current) is None
    # Every model trained before the fingerprint existed -- what an old
    # build cache would hold.
    assert "predates" in staleness_of()
    assert "different recipe" in staleness_of(training_config_hash="0" * 64)
    plan_file.write_bytes(b"not a pickle")
    assert "could not be read" in tm.plan_model_staleness(str(tmp_path))

    # The right recipe on a different market model is still stale.
    staleness_of(training_config_hash=current)
    joblib.dump(_tiny_market_forest(seed=1), tmp_path / tm.RF_MODEL_FILE)
    assert "different recipe" in tm.plan_model_staleness(str(tmp_path))


def test_seed_retrains_a_stale_plan_model_and_leaves_stage_one_alone(tmp_path):
    """seed.py's decision, on a scratch model directory: a stale or
    pre-fingerprint plan model retrains stage 2 ONLY (plan_only=True);
    a current one retrains nothing."""
    import seed
    from app.ml import train_model as tm

    rf_file, plan_file = tmp_path / tm.RF_MODEL_FILE, tmp_path / pm.PLAN_MODEL_FILENAME

    def decide(force=False):
        return seed.training_decision(force=force, model_file=str(rf_file), plan_model_file=str(plan_file))

    assert decide() == ("no model on disk", False)
    market = _tiny_market_forest()
    joblib.dump(market, rf_file)
    assert decide() == ("no plan viability model on disk", True)

    joblib.dump({"model": None, "feature_names": list(PLAN_FEATURE_NAMES)}, plan_file)
    reason, plan_only = decide()
    assert "predates" in reason and plan_only is True

    current = tm.plan_training_config_hash(tm.plan_training_config(market))
    joblib.dump({"model": None, "feature_names": list(PLAN_FEATURE_NAMES), "training_config_hash": current},
                plan_file)
    assert decide() == (None, False)
    assert decide(force=True) == ("forced", False)
