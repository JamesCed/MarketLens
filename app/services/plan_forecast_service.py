"""
app/services/plan_forecast_service.py
----------------------------------------
Stage 2 of the forecast, live: turns ONE business plan into its Plan
Viability forecast using the trained Plan Viability Model (RF2,
app/ml/model_store/plan_model.pkl, trained by app/ml/train_model.py).

WHY A SECOND STAGE
Stage 1 (forecasting_service.compute_scores) scores a MARKET -- an
industry in a barangay. Before this existed, a plan's "viability" was
just (100 - saturation) / 10 of that market: two plans for the same
kind of business in the same barangay got the same score whether one
had P5,000,000 behind it and the other P20,000, whether it listed a
sensible price list or none. Every business parameter the owner typed
in was collected, shown back to them, and then ignored by the number.

Stage 2 is where they count. forecast_plan() takes stage 1's output
(MSI*, after the sub-category adjustment) and the plan's own inputs,
derives the quantities a feasibility study would (monthly fixed cost,
capital runway, ramp-up, required daily sales, the demand ceiling --
app/ml/plan_model.py), and asks RF2 for the Plan Viability Index. Which
input contributed what is read straight off the forest's own decision
paths (plan_model.path_contributions) -- an exact decomposition, not a
second model's guess about the first.

WHAT EACH BUSINESS PARAMETER DOES (the full table, with formulas, is in
Reference/FORECAST_MODEL.md)
  industry_type, subcategory, location -> stage 1 -> MSI*, competitor count
  location                -> barangay rent and real PSA population
  business_stage, registration_date -> is_existing, years_in_operation
  capital (required)      -> capital, capital runway, capital adequacy
  employee_count          -> payroll -> fixed cost (runway); staffing
  product_offering        -> has_offering_description
  offering items (prices) -> priced items, average price, required sales
  innovation_idea         -> has_innovation_idea (differentiation)
  business_name           -> NOT used. It is an identifier, not a
                             business parameter; a model that scored a
                             plan by its name would be a bug.

NO TRAINED MODEL ON DISK
The app still works before `python -m app.ml.train_model` has run: the
Plan Viability Index is then the documented scorecard S itself (the
noise-free formula RF2 is trained to reproduce), labelled
"plan_formula_v1" with confidence 50 -- the same convention as stage 1's
"formula_v1" fallback. Its explanation is the scorecard's own component
points with a baseline of 0, which also add up exactly.

Nothing here touches Flask beyond current_app (model directory, logger)
and the two Admin-editable assumptions in system_settings.
"""

import os
import threading

import joblib
import numpy as np

from app.ml import plan_model as pm
from app.ml.constants import (
    DAILY_WAGE_RANGE,
    DEFAULT_DAILY_WAGE_PHP,
    DEFAULT_GROSS_MARGIN,
    GROSS_MARGIN_RANGE,
    OPERATING_DAYS_PER_MONTH,
    PLAN_COMPONENT_WEIGHTS,
    PLAN_FEATURE_NAMES,
)

# Admin-editable assumptions (System Settings > Plan forecast
# assumptions). Defaults and their sources: app/ml/constants.py.
WAGE_SETTING_KEY = "plan_daily_wage_php"
MARGIN_SETTING_KEY = "plan_gross_margin"

# Confidence reported for the formula fallback -- the same 50 stage 1's
# formula fallback reports: "computed, but not by a trained model".
FORMULA_CONFIDENCE = 50.0

# Short codes for the combined model_version stored on forecast_result
# (VARCHAR(20)): "<stage 1>+<stage 2>", e.g. "rf_v1+plan_rf_v1".
_STAGE1_SHORT = {"rf_v1": "rf_v1", "formula_v1": "fx_v1"}
_STAGE2_SHORT = {pm.PLAN_MODEL_VERSION: "plan_rf_v1", pm.PLAN_FORMULA_VERSION: "plan_fx_v1"}
MODEL_VERSION_MAX_LENGTH = 20

_PLAN_MODEL_CACHE = {"model": None, "loaded": False}

# Guards the one-time load. gunicorn runs this app with --threads 4, and
# reading plan_model.pkl (~12 MB) takes long enough for a second request
# to arrive mid-load. See _load_plan_model for what that used to cost.
_PLAN_MODEL_LOCK = threading.Lock()


# ---------------------------------------------------------------------
# Loading -- once per process, like stage 1
# ---------------------------------------------------------------------

def _model_dir():
    from flask import current_app

    return current_app.config["MODEL_DIR"]


def _load_plan_model():
    """RF2, lazily, once per process -- or None when there is no usable
    trained model (not trained yet, unreadable, or trained on a
    different feature order than this code builds).

    Pinned to one thread for prediction, for the same three reasons
    forecasting_service._pin_to_one_thread() gives for stage 1 -- the
    last of which decides it here too: with n_jobs=-1 the same plan can
    come back with a different last digit on every refresh, and a
    forecast whose explanation must add up exactly cannot wobble.

    ONE LOADER, AND "loaded" IS SET LAST. This used to mark the cache
    loaded BEFORE joblib.load() had read the file, so a second request
    thread arriving during the read saw loaded=True with no model yet,
    took the formula fallback, and stored that plan's forecast as
    "plan_fx_v1" with confidence 50 -- where it stayed, because Home
    only regenerates a forecast that lacks a payload. Now the first
    thread loads under a lock while any other waits for it (double-
    checked, so once loaded no request ever touches the lock), and
    "loaded" flips only after the model -- or the decision to use the
    formula -- has been stored.
    """
    if _PLAN_MODEL_CACHE["loaded"]:
        return _PLAN_MODEL_CACHE["model"]
    with _PLAN_MODEL_LOCK:
        if not _PLAN_MODEL_CACHE["loaded"]:
            _PLAN_MODEL_CACHE["model"] = _read_plan_model()
            _PLAN_MODEL_CACHE["loaded"] = True
    return _PLAN_MODEL_CACHE["model"]


def _read_plan_model():
    """The usable RF2 on disk, pinned to one thread -- or None."""
    from flask import current_app

    path = os.path.join(_model_dir(), pm.PLAN_MODEL_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        bundle = joblib.load(path)
    except Exception:  # noqa: BLE001 -- a corrupt file must not take the forecast down
        current_app.logger.warning("plan_model.pkl could not be loaded; using the formula", exc_info=True)
        return None

    if isinstance(bundle, dict):
        model, names = bundle.get("model"), bundle.get("feature_names")
    else:
        model, names = bundle, None

    # A forest fed a row in the wrong order does not raise -- it answers
    # a different question. So a mismatch is refused, loudly, and the
    # documented formula stands in until the model is retrained.
    if model is None or (names is not None and list(names) != list(PLAN_FEATURE_NAMES)) \
            or getattr(model, "n_features_in_", len(PLAN_FEATURE_NAMES)) != len(PLAN_FEATURE_NAMES):
        current_app.logger.warning(
            "plan_model.pkl was trained on a different feature list than PLAN_FEATURE_NAMES; "
            "using the formula until `python -m app.ml.train_model --plan-only` is re-run"
        )
        return None

    try:
        model.n_jobs = 1
    except Exception:  # pragma: no cover - a model that refuses is still usable
        current_app.logger.warning("could not pin the plan forest to one thread", exc_info=True)
    return model


def reset_plan_model_cache():
    """Forget the loaded model, so the next forecast re-reads the file.
    For tests, and for a process that has just retrained."""
    with _PLAN_MODEL_LOCK:
        _PLAN_MODEL_CACHE["loaded"] = False
        _PLAN_MODEL_CACHE["model"] = None


def plan_model_is_trained():
    return _load_plan_model() is not None


# ---------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------

# The precision the two assumptions are used AND stored at (the payload's
# financials.daily_wage / gross_margin). Rounding them before use, not
# just for display, is what lets a reader -- or the quarterly outlook,
# which re-runs this plan later -- take them back out of a stored
# forecast and get the very numbers it was computed with.
WAGE_DECIMALS = 2      # centavos
MARGIN_DECIMALS = 4


def _valid_wage(value):
    low, high = DAILY_WAGE_RANGE
    return value is not None and low <= value <= high      # NaN fails both


def _valid_margin(value):
    low, high = GROSS_MARGIN_RANGE
    return value is not None and low <= value <= high


def plan_assumptions():
    """(daily_wage, gross_margin) -- the Admin's values when they are
    valid, the documented defaults when they are missing or not. A
    wage of 0 or a margin of 100% would not be an assumption, it would
    be a broken setting, and a broken setting must not quietly become a
    forecast. Nor would a wage of 1e37 a day: "valid" means inside
    DAILY_WAGE_RANGE (P1-P100,000), the range the Admin form enforces
    too, since a wage that large used to overflow the forest's input
    and fail every plan forecast with staff."""
    from app.models import SystemSetting

    wage = SystemSetting.get_float(WAGE_SETTING_KEY, DEFAULT_DAILY_WAGE_PHP)
    if not _valid_wage(wage):
        wage = DEFAULT_DAILY_WAGE_PHP
    margin = SystemSetting.get_float(MARGIN_SETTING_KEY, DEFAULT_GROSS_MARGIN)
    if not _valid_margin(margin):
        margin = DEFAULT_GROSS_MARGIN
    return round(float(wage), WAGE_DECIMALS), round(float(margin), MARGIN_DECIMALS)


def assumptions_from_payload(payload):
    """(daily_wage, gross_margin) a STORED forecast payload was computed
    with, or None when the payload does not carry usable ones (a row
    older than the plan model, or a damaged one).

    Why this exists: an Admin can change the wage or margin at any time,
    and a stored forecast keeps the values it was made with until the
    plan is re-forecast. Anything that re-runs stage 2 to sit NEXT TO a
    stored forecast -- the quarterly outlook, whose Q1 bar must equal
    the gauge -- has to hold the stored values, not today's settings."""
    try:
        financials = (payload or {}).get("financials") or {}
        wage = float(financials["daily_wage"])
        margin = float(financials["gross_margin"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if not (_valid_wage(wage) and _valid_margin(margin)):
        return None
    return wage, margin


def _profile_capital(sme_profile):
    """The plan's capital. `SmeProfile.capital` when the model has it;
    the column it reads (`startup_capital` -- the name the live table
    keeps) otherwise."""
    capital = getattr(sme_profile, "capital", None)
    if capital is None:
        capital = getattr(sme_profile, "startup_capital", None)
    try:
        return max(0.0, float(capital or 0))
    except (TypeError, ValueError):
        return 0.0


def _monthly_rent(market_row, location):
    """Rent from the market_data row the forecast was scored on (the
    same figure stage 1 used), falling back to the barangay's reference
    profile and then to the same neutral P15,000 find_or_create_market_data
    uses for an unknown location."""
    rent = getattr(market_row, "average_rent", None) if market_row is not None else None
    if rent:
        return float(rent)
    from app.ml.seed_data import get_barangay_profile

    profile = get_barangay_profile(location)
    return float(profile["average_rent"]) if profile else 15000.0


def plan_competitor_count(scores, subcategory_analysis=None):
    """The competitor count the plan is judged against: the
    direct-competition-adjusted count when a sub-category actually
    adjusted the score, else the reconciled industry count. The same
    count MSI* was scored with, so the two never describe different
    markets."""
    if subcategory_analysis and subcategory_analysis.get("adjusts_score"):
        return int(subcategory_analysis.get("adjusted_competitor_count") or 0)
    return int(scores.get("competitor_count") or 0)


def build_plan_inputs(sme_profile, scores, market_row, population, subcategory_analysis=None,
                      assumptions=None):
    """The raw inputs stage 2 reads, as plain numbers -- see
    plan_model.derive_quantities() for what is computed from them.
    Also carries the plan's descriptive fields (industry, location...)
    for the payload; derive_quantities ignores those."""
    wage, margin = assumptions or plan_assumptions()
    items = list(getattr(sme_profile, "offering_items", None) or [])
    priced, average_price = pm.price_list_summary(items)
    offering_text = (getattr(sme_profile, "product_offering", None) or "").strip()
    idea_text = (getattr(sme_profile, "innovation_idea", None) or "").strip()
    stage = getattr(sme_profile, "business_stage", None) or "startup"

    return {
        # stage 1
        "saturation_index": float(scores["saturation_index"]),
        "competitor_count": plan_competitor_count(scores, subcategory_analysis),
        # location
        "population": pm.resolve_population(population),
        "monthly_rent": _monthly_rent(market_row, sme_profile.location),
        # the owner's own parameters
        "capital": _profile_capital(sme_profile),
        "employee_count": max(0, int(getattr(sme_profile, "employee_count", None) or 0)),
        "is_existing": stage == "existing",
        "years_in_operation": float(sme_profile.years_in_operation() or 0),
        "priced_item_count": priced,
        "average_price": average_price,
        "has_offering_description": bool(offering_text) or bool(items),
        "has_innovation_idea": bool(idea_text),
        # assumptions
        "daily_wage": wage,
        "gross_margin": margin,
        # descriptive, for the payload only
        "business_stage": stage,
        "industry_type": sme_profile.industry_type,
        "subcategory_label": getattr(sme_profile, "subcategory_label", None),
        "location": sme_profile.location,
    }


# ---------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------

def _confidence_from_spread(tree_predictions):
    """Exactly stage 1's mapping: 0 spread across the trees = 100%,
    falling linearly to a floor of 40% at a spread of 25 points."""
    spread = float(np.std(tree_predictions))
    return max(40.0, min(100.0, 100.0 - (spread / 25.0) * 60.0))


def _predict(inputs, explain=True):
    """{viability_index, confidence, model_version, scorecard_index,
    baseline, feature_contributions, derived} for one set of inputs."""
    derived = pm.derive_quantities(inputs)
    scorecard = pm.scorecard_index(inputs, derived)
    model = _load_plan_model()

    vector = pm.plan_feature_vector(inputs, derived)
    if model is not None and not pm.tree_safe(vector):
        # derive_quantities() keeps every feature finite and inside
        # float32, so this should never happen; if a future feature
        # breaks that, the plan gets the labelled formula and a log
        # line, not a ValueError out of predict() and a 500 on Home.
        from flask import current_app

        current_app.logger.warning(
            "plan feature vector is not float32-safe (%r); using the formula for this plan", vector
        )
        model = None

    if model is None:
        return {
            "viability_index": pm.clamp(scorecard, 0.0, 100.0),
            "confidence": FORMULA_CONFIDENCE,
            "model_version": pm.PLAN_FORMULA_VERSION,
            "scorecard_index": scorecard,
            "baseline": None,
            "feature_contributions": None,
            "derived": derived,
        }

    if explain:
        baseline, contributions, prediction, tree_predictions = pm.path_contributions(model, vector)
    else:
        # No explanation wanted (the quarterly outlook): the trees' leaf
        # values give both the prediction (bit-identical to
        # model.predict, see plan_model.forest_mean) and the spread.
        tree_predictions = pm.tree_leaf_values(model, vector)
        prediction = pm.forest_mean(tree_predictions)
        baseline, contributions = None, None

    return {
        "viability_index": pm.clamp(prediction, 0.0, 100.0),
        "confidence": _confidence_from_spread(tree_predictions),
        "model_version": pm.PLAN_MODEL_VERSION,
        "scorecard_index": scorecard,
        "baseline": baseline,
        "feature_contributions": contributions,
        "derived": derived,
    }


def plan_viability_for(inputs, saturation_index, competitor_count):
    """Re-run stage 2 with a different market -- a quarter's projected
    MSI and competitor count -- holding every plan input fixed. Used by
    trend_analytics_service.project_quarterly_outlook().

    Returns {viability_index, viability_score, confidence,
    model_version}; no explanation is computed (four of these per page
    load do not need one)."""
    shifted = dict(inputs)
    shifted["saturation_index"] = float(saturation_index or 0)
    shifted["competitor_count"] = max(0, int(competitor_count or 0))
    result = _predict(shifted, explain=False)
    index = round(float(result["viability_index"]), 1)
    return {
        "viability_index": index,
        "viability_score": round(index / 10.0, 1),
        "confidence": round(float(result["confidence"]), 1),
        "model_version": result["model_version"],
    }


# ---------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------

def _r(value, ndigits=1):
    """A plain, JSON-safe Python float. Every number in the payload goes
    through here or int(), so no numpy scalar ever reaches json.dumps."""
    return round(float(value), ndigits)


def _drivers(prediction):
    """(baseline, [{key, label, points}]) -- the explanation, rounded so
    baseline + the drivers' points equals the reported viability index
    exactly at one decimal (plan_model.round_preserving_sum), sorted by
    size of effect."""
    index = float(prediction["viability_index"])
    if prediction["feature_contributions"] is not None:
        baseline = float(prediction["baseline"])
        grouped = pm.grouped_drivers(prediction["feature_contributions"])
    else:
        # Formula fallback: the scorecard's own component points, from a
        # baseline of 0. They add up to S, which IS the index here.
        baseline = 0.0
        grouped = {key: 0.0 for key in pm.DRIVER_LABELS}
        components = pm.component_scores(None, prediction["derived"])
        for key, weight in PLAN_COMPONENT_WEIGHTS.items():
            grouped[pm.COMPONENT_META[key][3]] += 100.0 * weight * components[key]

    # The baseline is the same for every plan (the forest's training
    # mean), so it is rounded on its own and stays put; any rounding
    # slack is absorbed by the drivers, never by the baseline.
    baseline = _r(baseline)
    keys = list(pm.DRIVER_LABELS)
    rounded = pm.round_preserving_sum([grouped[k] for k in keys], index - baseline, 1)
    rows = [
        {"key": key, "label": pm.DRIVER_LABELS[key], "points": _r(points)}
        for key, points in zip(keys, rounded)
    ]
    rows.sort(key=lambda row: -abs(row["points"]))
    return baseline, rows


def combined_model_version(stage1_version, plan_version):
    """"rf_v1+plan_rf_v1" -- both stages named in the one VARCHAR(20)
    model_version column, so a stored forecast says which models made
    it (and whether either was the formula fallback)."""
    first = _STAGE1_SHORT.get(stage1_version, str(stage1_version or "?"))
    second = _STAGE2_SHORT.get(plan_version, str(plan_version or "?"))
    return f"{first}+{second}"[:MODEL_VERSION_MAX_LENGTH]


def combined_confidence(payload):
    """The forecast's overall confidence: the LOWER of the two stages'.
    A plan forecast is only as certain as the less certain of the two
    models it chains together."""
    return round(min(float(payload["market"]["confidence"]), float(payload["plan"]["confidence"])), 2)


def forecast_plan(sme_profile, scores, market_row, population, subcategory_analysis=None):
    """The plan's forecast payload -- stored inside the recommendation
    JSON under "forecast", read by the Home and Recommendations pages and
    handed to the LLM to narrate. Every number in it comes from the
    models and the plan's own inputs; nothing in it is written by the
    LLM.

    `scores` is compute_scores() after the sub-category adjustment
    (subcategory_service.adjusted_scores); `market_row` the MarketData
    row it was scored on; `population` the barangay's real PSA
    population (0/None -> the documented fallback);
    `subcategory_analysis` subcategory_service.direct_competition()'s
    result, which says whether the competitor count was adjusted.

    Shape (all floats JSON-safe):
      version, market{saturation_index, industry_saturation_index,
      cluster_label, competitor_count, confidence, model_version},
      plan{viability_index, viability_score, confidence, model_version,
      scorecard_index}, inputs{...}, financials{..., break_even},
      components[...], baseline, drivers[...]
    """
    from app.services.location_opportunity_service import estimate_roi_timeframe

    inputs = build_plan_inputs(sme_profile, scores, market_row, population, subcategory_analysis)
    prediction = _predict(inputs, explain=True)
    derived = prediction["derived"]

    viability_index = _r(prediction["viability_index"])
    viability_score = _r(viability_index / 10.0)
    saturation = float(scores["saturation_index"])

    # The break-even window is the ROI formula the Recommendations page
    # already uses, driven by the PLAN's viability rather than the
    # market's -- which is how capital, staffing and pricing reach it.
    # It is fed the model's own PVI / 10, unrounded, as the formula
    # (Reference/FORECAST_MODEL.md) states -- not the 1-dp score shown.
    # The window's ends are whole months, so feeding it a twice-rounded
    # score moved one end by a month for about 8% of plans. (MSI* goes
    # in unrounded too, so the window was never meant to be recomputed
    # from the displayed figures.)
    roi = estimate_roi_timeframe(float(prediction["viability_index"]) / 10.0, saturation, None, None)
    baseline, drivers = _drivers({**prediction, "viability_index": viability_index})

    return {
        "version": pm.PAYLOAD_VERSION,
        "market": {
            "saturation_index": _r(saturation),
            "industry_saturation_index": _r(scores.get("industry_saturation_index", saturation)),
            "cluster_label": scores.get("cluster_label"),
            "competitor_count": int(inputs["competitor_count"]),
            "confidence": _r(scores.get("confidence_level") or 0),
            "model_version": scores.get("model_version"),
        },
        "plan": {
            "viability_index": viability_index,
            "viability_score": viability_score,
            "confidence": _r(prediction["confidence"]),
            "model_version": prediction["model_version"],
            "scorecard_index": _r(prediction["scorecard_index"]),
        },
        "inputs": {
            # derived[...] is the figure the model read
            # (plan_model.finite_input) -- the owner's own number for
            # any capital or headcount a plan can hold.
            "capital": _r(derived["capital"], 2),
            "employee_count": int(derived["employee_count"]),
            "business_stage": inputs["business_stage"],
            "years_in_operation": _r(inputs["years_in_operation"]),
            "priced_item_count": int(derived["priced_item_count"]),
            "average_price": _r(derived["average_price"], 2),
            "has_offering_description": bool(derived["has_offering_description"]),
            "has_innovation_idea": bool(derived["has_innovation_idea"]),
            "industry_type": inputs["industry_type"],
            "subcategory_label": inputs["subcategory_label"],
            "location": inputs["location"],
            "population": int(round(derived["population"])),
            "residents_per_business": _r(derived["residents_per_business"]),
        },
        "financials": {
            "monthly_rent": _r(derived["monthly_rent"], 2),
            "daily_wage": _r(derived["daily_wage"], WAGE_DECIMALS),
            "monthly_payroll": _r(derived["monthly_payroll"], 2),
            "monthly_fixed_cost": _r(derived["monthly_fixed_cost"], 2),
            "capital_runway_months": _r(derived["capital_runway_months"]),
            "ramp_up_months": _r(derived["ramp_up_months"]),
            "capital_adequacy": _r(derived["capital_adequacy"], 2),
            # Both assumptions at the precision they were USED at
            # (plan_assumptions), so assumptions_from_payload() can hand
            # them back exactly -- see the quarterly outlook.
            "gross_margin": _r(derived["gross_margin"], MARGIN_DECIMALS),
            "operating_days": int(OPERATING_DAYS_PER_MONTH),
            # 0 when the plan lists no prices (inputs.priced_item_count
            # is then 0 too) -- "not computable", not "zero sales needed".
            "required_daily_sales": _r(derived["required_daily_sales"]),
            "daily_sales_ceiling": _r(derived["daily_sales_ceiling"]),
            "break_even": {
                "low_months": int(roi["low_months"]),
                "high_months": int(roi["high_months"]),
                "label": roi["label"],
            },
        },
        "components": [
            {
                "key": row["key"],
                "label": row["label"],
                "aspect": row["aspect"],
                "score": _r(row["score"], 2),
                "weight": _r(row["weight"], 2),
                "points": _r(row["points"]),
                "inputs": row["inputs"],
            }
            for row in pm.plan_components(None, derived)
        ],
        "baseline": baseline,
        "drivers": drivers,
    }
