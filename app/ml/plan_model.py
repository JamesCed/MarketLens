"""
app/ml/plan_model.py
----------------------
Stage 2 of the forecast: the PLAN VIABILITY MODEL.

Pure numpy / scikit-learn -- no Flask, no database, no settings lookups.
That is deliberate: the training script (app/ml/train_model.py) and the
live service (app/services/plan_forecast_service.py) both build the
model's input through the functions in this file, so the arithmetic a
model was trained on and the arithmetic it is later asked about cannot
drift apart. Anything that needs the database (the plan row, the market
row, the Admin's wage/margin overrides) is resolved by the service and
handed in as plain numbers.

THE TWO STAGES, IN ONE PARAGRAPH
Stage 1 (unchanged, forecasting_service.compute_scores) predicts how
saturated a MARKET is -- industry x barangay -- from eight market
features, then the sub-category adjustment re-scores it with the direct
competitor count: MSI* (0-100). Stage 2 (this file) predicts how viable
a PLAN is in that market. It takes MSI* as one input among fourteen and
adds every business parameter the owner typed in -- capital, employees,
business stage, the price list, the offering, the idea -- plus the
barangay's real population and rent. Its output is the Plan Viability
Index (PVI, 0-100), shown as a Plan Viability Score = PVI / 10.

WHAT IS IN HERE
  1. ramp_up_months()        -- the months-to-pay-for-itself formula,
                                shared with the ROI window
                                (location_opportunity_service).
  2. derive_quantities()     -- capital runway, fixed cost, required
                                daily sales, the demand ceiling...
  3. plan_feature_vector()   -- RF2's input, in PLAN_FEATURE_NAMES order.
  4. plan_components()       -- the seven-part scorecard whose weighted
                                sum is the TRAINING LABEL (plus noise).
  5. path_contributions()    -- the exact explanation of one prediction:
                                how many points each input moved it.

Reference/FORECAST_MODEL.md is the prose version of this file, with a
worked example. If the two ever disagree, this file is what runs.
"""

import math
import statistics
from functools import lru_cache

import numpy as np

from app.ml.constants import (
    DEFAULT_DAILY_WAGE_PHP,
    DEFAULT_GROSS_MARGIN,
    EXPERIENCE_FULL_YEARS,
    GROSS_MARGIN_RANGE,
    ITEM_PRICE_RANGE_PHP,
    MINIMUM_RAMP_MONTHS,
    MONTHLY_FIXED_COST_FLOOR_PHP,
    OPERATING_DAYS_PER_MONTH,
    PLAN_COMPONENT_WEIGHTS,
    PLAN_FEATURE_NAMES,
    PLAN_INPUT_CEILING,
    PURCHASES_PER_RESIDENT_PER_DAY,
    STAFF_FULL_CAPACITY,
)

# File name inside MODEL_DIR (next to stage 1's rf_model.pkl).
PLAN_MODEL_FILENAME = "plan_model.pkl"

# Version strings. The trained forest and the formula fallback are
# labelled differently everywhere they appear, so a forecast made
# without the trained model can never pass for one made with it.
PLAN_MODEL_VERSION = "plan_rf_v1"
PLAN_FORMULA_VERSION = "plan_formula_v1"
PAYLOAD_VERSION = "plan_v1"


# =====================================================================
# 1. RAMP-UP -- one formula, two users
# =====================================================================
# The ROI window on the Recommendations page and the ramp-up period in
# the plan forecast are the same question ("how long until this business
# pays for itself?"), so they are answered by the same arithmetic. It
# used to live only inside location_opportunity_service; it lives here
# now so the training script can use it without importing a Flask
# service, and estimate_roi_timeframe() calls it -- with results
# unchanged to the last bit (tests/test_trends_and_roi.py).

def clamp(value, low, high):
    return max(low, min(high, value))


def market_viability(saturation_index):
    """Vm = (100 - MSI) / 10 -- stage 1's market-only viability on the
    familiar 0-10 scale. An input to the ramp formula, not the score a
    plan is shown (that is the PVI)."""
    return (100.0 - float(saturation_index or 0)) / 10.0


def saturation_factor(saturation_index):
    """SF = 1 + (MSI - 50)/100, clamped to 0.6x-1.8x so no single
    extreme input runs away with the estimate. A market scored at 20%
    ramps faster than one at 80%."""
    return clamp(1.0 + (float(saturation_index or 0) - 50.0) / 100.0, 0.6, 1.8)


def ramp_midpoint_months(viability_score, saturation_index, depth_multiplier=1.0):
    """(24 - 1.8 x viability) x SF x depth_multiplier: 10/10 starts from
    ~6 months, 0/10 from ~24, then saturation stretches or compresses
    it. `depth_multiplier` is the ROI window's market-depth nudge (0.9 /
    1.1); stage 2 does not use it, because market depth is its own
    feature there (residents_per_business) and counting it twice would
    double its weight.

    The multiplication order matches the original
    estimate_roi_timeframe() exactly -- (24 - v*1.8) * (SF * nudge) --
    so moving the formula here changed no ROI figure, not even in the
    last floating-point bit."""
    viability = float(viability_score or 0)
    factor = saturation_factor(saturation_index) * depth_multiplier
    return (24.0 - (viability * 1.8)) * factor


def ramp_up_months(viability_score, saturation_index):
    """R = max(MINIMUM_RAMP_MONTHS, (24 - 1.8 x Vm) x SF) -- the months
    a new business needs before it covers its own running costs. This is
    the yardstick capital runway is measured against."""
    return max(float(MINIMUM_RAMP_MONTHS), ramp_midpoint_months(viability_score, saturation_index))


# =====================================================================
# 2. INPUTS -> DERIVED QUANTITIES
# =====================================================================

@lru_cache(maxsize=1)
def fallback_population():
    """The median 2024 PSA population of Tarlac City's 76 barangays.

    Used ONLY when a plan's location is not one of the 76 (a free-typed
    location, or a test barangay) and so has no published population.
    The median rather than the mean, because three barangays above
    17,000 would otherwise drag a 'typical' barangay upward. Every one
    of the 76 has a real figure, so a plan placed in Tarlac City never
    takes this path."""
    from app.ml.seed_data import BARANGAY_NAMES, _parse_source_comments

    notes = _parse_source_comments()
    figures = []
    for name in BARANGAY_NAMES:
        raw = (notes.get(name) or {}).get("population_2024_psa")
        try:
            figures.append(int(raw))
        except (TypeError, ValueError):
            continue
    return float(statistics.median(figures)) if figures else 4000.0


def finite_input(value, default=0.0):
    """`value` as a number the forest can read. None, NaN or anything
    unparseable -> `default`; +/-infinity and anything beyond
    PLAN_INPUT_CEILING -> the ceiling, with its sign.

    Every number derive_quantities() reads goes through here. Inside the
    ceiling it is exactly float(value) -- the same bits, so a training
    row and the model trained on it are unchanged -- and outside it the
    derived quantities stay finite and inside float32, which is what
    RF2's predict() demands (constants.PLAN_INPUT_CEILING says why that
    used to matter)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if math.isnan(number):
        return float(default)
    return clamp(number, -PLAN_INPUT_CEILING, PLAN_INPUT_CEILING)


def resolve_population(population):
    """The population the model uses: the real one when known, the
    fallback above when not (None, 0, negative or not a number)."""
    value = finite_input(population, 0.0)
    return value if value > 0 else fallback_population()


def price_list_summary(items):
    """(priced_item_count, average_price) from the owner's price list.

    Only real PRICE POINTS count: finite prices inside
    ITEM_PRICE_RANGE_PHP (P0.01-P10,000,000). A listed item with no
    price tells the model the offering is described, which
    has_offering_description already captures; a price of 0 (a freebie,
    a sample) is not a price point anything can be sold at, and
    averaging it in would make required daily sales infinite. The upper
    end is the same idea from the other side: the form's number input
    accepts "1e39" and "inf", and one such entry averaged in used to
    overflow the forest's float32 input and crash the forecast -- on
    every later visit to Home, since the plan was already saved."""
    low, high = ITEM_PRICE_RANGE_PHP
    prices = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("item"):
            continue
        try:
            price = float(item.get("price")) if item.get("price") is not None else None
        except (TypeError, ValueError, OverflowError):
            price = None
        # NaN fails both comparisons, so it is skipped with the rest.
        if price is not None and low <= price <= high:
            prices.append(price)
    if not prices:
        return 0, 0.0
    return len(prices), sum(prices) / len(prices)


def derive_quantities(inputs):
    """Every intermediate number the forecast uses, from the raw inputs.

    `inputs` keys (all plain numbers / bools -- see
    plan_forecast_service.build_plan_inputs for where each comes from):
        saturation_index, competitor_count, population, monthly_rent,
        employee_count, daily_wage, gross_margin, capital, is_existing,
        years_in_operation, priced_item_count, average_price,
        has_offering_description, has_innovation_idea

    Formulas (PHP and months throughout):
        residents_per_business = population / (competitor_count + 1)
        monthly_payroll        = employees x daily_wage x 26
        monthly_fixed_cost     = max(1,000, rent + monthly_payroll)
        capital_runway_months  = capital / monthly_fixed_cost
        ramp_up_months         = ramp_up_months(Vm, MSI*)
        capital_adequacy       = min(1, runway / ramp_up)
        required_daily_sales   = monthly_fixed_cost
                                 / (average_price x gross_margin x 26)
                                 (0 when the plan lists no prices)
        daily_sales_ceiling    = residents_per_business x 1/7

    Every input is read through finite_input(), so whatever arrives --
    a legacy row, a hand-edited setting -- every quantity below is a
    finite number inside float32 (the bound is worked through in
    constants.PLAN_INPUT_CEILING's note and Reference/FORECAST_MODEL.md).
    """
    saturation = clamp(finite_input(inputs.get("saturation_index")), 0.0, 100.0)
    competitors = max(0.0, finite_input(inputs.get("competitor_count")))
    population = resolve_population(inputs.get("population"))
    rent = max(0.0, finite_input(inputs.get("monthly_rent")))
    employees = max(0.0, finite_input(inputs.get("employee_count")))
    wage = max(0.0, finite_input(inputs.get("daily_wage") or DEFAULT_DAILY_WAGE_PHP, DEFAULT_DAILY_WAGE_PHP))
    margin = clamp(finite_input(inputs.get("gross_margin") or DEFAULT_GROSS_MARGIN, DEFAULT_GROSS_MARGIN),
                   *GROSS_MARGIN_RANGE)
    capital = max(0.0, finite_input(inputs.get("capital")))
    priced = max(0, int(finite_input(inputs.get("priced_item_count"))))
    average_price = max(0.0, finite_input(inputs.get("average_price")))
    if priced <= 0 or average_price < ITEM_PRICE_RANGE_PHP[0]:
        # A count without a price, or a price without a count, is not a
        # price list the arithmetic below can use. (Nor is an average
        # below a centavo: see ITEM_PRICE_RANGE_PHP.)
        priced, average_price = 0, 0.0

    residents_per_business = population / (competitors + 1.0)
    monthly_payroll = employees * wage * OPERATING_DAYS_PER_MONTH
    monthly_fixed_cost = max(MONTHLY_FIXED_COST_FLOOR_PHP, rent + monthly_payroll)
    runway = capital / monthly_fixed_cost
    viability = market_viability(saturation)
    ramp = ramp_up_months(viability, saturation)
    adequacy = clamp(runway / ramp, 0.0, 1.0)

    if priced:
        required_daily_sales = monthly_fixed_cost / (average_price * margin * OPERATING_DAYS_PER_MONTH)
    else:
        required_daily_sales = 0.0
    daily_sales_ceiling = residents_per_business * PURCHASES_PER_RESIDENT_PER_DAY

    return {
        "saturation_index": saturation,
        "market_saturation": saturation,   # the feature's name for the same number
        "competitor_count": competitors,
        "population": population,
        "residents_per_business": residents_per_business,
        "monthly_rent": rent,
        "daily_wage": wage,
        "gross_margin": margin,
        "monthly_payroll": monthly_payroll,
        "monthly_fixed_cost": monthly_fixed_cost,
        "capital": capital,
        "capital_runway_months": runway,
        "market_viability": viability,
        "ramp_up_months": ramp,
        "capital_adequacy": adequacy,
        "employee_count": employees,
        "is_existing": 1.0 if inputs.get("is_existing") else 0.0,
        "years_in_operation": max(0.0, finite_input(inputs.get("years_in_operation"))),
        "priced_item_count": float(priced),
        "average_price": average_price,
        "required_daily_sales": required_daily_sales,
        "daily_sales_ceiling": daily_sales_ceiling,
        # Any price-list item (priced or not) counts as a described
        # offering, as does the free-text description.
        "has_offering_description": 1.0 if (inputs.get("has_offering_description") or priced > 0) else 0.0,
        "has_innovation_idea": 1.0 if inputs.get("has_innovation_idea") else 0.0,
    }


def plan_feature_vector(inputs, derived=None):
    """RF2's input row, in PLAN_FEATURE_NAMES order. The ONE function
    both training and inference call to build it."""
    derived = derived or derive_quantities(inputs)
    return [float(derived[name]) for name in PLAN_FEATURE_NAMES]


# =====================================================================
# 3. THE SCORECARD -- the formula the training labels come from
# =====================================================================
# Seven components, each 0-1, grouped by the four aspects a feasibility
# study covers. (label, aspect, which inputs feed it, which explanation
# driver it is reported under when the formula fallback is used.)
COMPONENT_META = {
    "market_opportunity": ("Market opportunity", "Market",
                           "industry, sub-category, location", "market"),
    "capital_adequacy": ("Capital adequacy", "Financial",
                         "capital, employees, location (rent)", "capital"),
    "price_coverage": ("Price coverage", "Financial",
                       "price list, location (population, competitors)", "pricing"),
    "operating_experience": ("Operating experience", "Technical/Operational",
                             "business stage, registration date", "experience"),
    "staffing": ("Staffing capacity", "Technical/Operational", "employees", "staffing"),
    "offering_definition": ("Offering defined", "Product",
                            "product offering, price list", "offering"),
    "differentiation": ("Differentiation", "Product",
                        "innovation idea, market saturation", "differentiation"),
}


def component_scores(inputs, derived=None):
    """{component_key: score 0-1}.

        C1 market_opportunity   = 1 - MSI*/100
        C2 capital_adequacy     = min(1, runway / ramp_up)
        C3 price_coverage       = 0.5 with no prices (unknown -> neutral),
                                  else clamp(1 - RDS/DSC, 0, 1)
        C4 operating_experience = 0 for a startup; existing:
                                  min(1, 0.5 + 0.5 x years / 5)
        C5 staffing             = min(1, (employees + 1) / 4)
        C6 offering_definition  = 0 if nothing described, else
                                  0.5 + 0.5 x min(1, priced_items / 5)
        C7 differentiation      = 0 without an idea, else
                                  0.4 + 0.6 x MSI*/100
    """
    d = derived or derive_quantities(inputs)

    if d["priced_item_count"] <= 0:
        price_coverage = 0.5
    elif d["daily_sales_ceiling"] <= 0:
        price_coverage = 0.0
    else:
        price_coverage = clamp(1.0 - d["required_daily_sales"] / d["daily_sales_ceiling"], 0.0, 1.0)

    if d["is_existing"]:
        experience = min(1.0, 0.5 + 0.5 * d["years_in_operation"] / EXPERIENCE_FULL_YEARS)
    else:
        experience = 0.0

    if d["has_offering_description"]:
        offering = 0.5 + 0.5 * min(1.0, d["priced_item_count"] / 5.0)
    else:
        offering = 0.0

    differentiation = (0.4 + 0.6 * d["saturation_index"] / 100.0) if d["has_innovation_idea"] else 0.0

    return {
        "market_opportunity": clamp(1.0 - d["saturation_index"] / 100.0, 0.0, 1.0),
        "capital_adequacy": d["capital_adequacy"],
        "price_coverage": price_coverage,
        "operating_experience": experience,
        "staffing": min(1.0, (d["employee_count"] + 1.0) / (STAFF_FULL_CAPACITY + 1.0)),
        "offering_definition": offering,
        "differentiation": clamp(differentiation, 0.0, 1.0),
    }


def scorecard_index(inputs, derived=None):
    """S = 100 x sum(w_k x C_k), 0-100. The noise-free formula RF2 is
    trained to reproduce (see train_model.train_plan_model)."""
    scores = component_scores(inputs, derived)
    return 100.0 * sum(PLAN_COMPONENT_WEIGHTS[key] * scores[key] for key in PLAN_COMPONENT_WEIGHTS)


def plan_components(inputs, derived=None):
    """The scorecard as the payload reports it: one dict per component,
    heaviest weight first (ties keep the PLAN_COMPONENT_WEIGHTS order).
    `points` = 100 x weight x score, so the points add up to S."""
    scores = component_scores(inputs, derived)
    ordered = sorted(PLAN_COMPONENT_WEIGHTS.items(), key=lambda pair: -pair[1])
    rows = []
    for key, weight in ordered:
        label, aspect, fed_by, _driver = COMPONENT_META[key]
        rows.append({
            "key": key,
            "label": label,
            "aspect": aspect,
            "score": scores[key],
            "weight": weight,
            "points": 100.0 * weight * scores[key],
            "inputs": fed_by,
        })
    return rows


# =====================================================================
# 4. EXPLAINING ONE PREDICTION -- exact path decomposition
# =====================================================================
# A random forest's prediction for x is the average, over its trees, of
# the value stored at the leaf x lands in. Walk any one tree from the
# root to that leaf: the root's value is the training-set mean, and
# every split along the way moves the running value from the parent
# node's mean to the child's. Crediting each such move to the feature
# the split tested gives, for that tree,
#
#     leaf value = root value + sum over splits (child - parent)
#
# exactly -- it is a telescoping sum. Averaging over the trees:
#
#     PVI = baseline + sum over features of contribution_f
#
# where baseline is the mean root value. This is the Saabas method (the
# "treeinterpreter" decomposition). It is not an approximation, so the
# self-check below holds to floating-point precision, and it describes
# THIS model's actual decision paths -- not a separate story about them.
#
# The fourteen feature contributions are then summed into eight
# plain-language drivers. FEATURE_GROUPS is the one place that mapping
# is defined.

FEATURE_GROUPS = {
    "market_saturation": "market",
    "residents_per_business": "depth",
    "monthly_fixed_cost": "capital",
    "capital": "capital",
    "capital_runway_months": "capital",
    "ramp_up_months": "capital",
    "employee_count": "staffing",
    "is_existing": "experience",
    "years_in_operation": "experience",
    "priced_item_count": "pricing",
    "average_price": "pricing",
    "required_daily_sales": "pricing",
    "has_offering_description": "offering",
    "has_innovation_idea": "differentiation",
}
assert set(FEATURE_GROUPS) == set(PLAN_FEATURE_NAMES)

DRIVER_LABELS = {
    "market": "Market saturation (industry · sub-category · location)",
    "depth": "Market depth (residents per business)",
    "capital": "Capital vs. running costs",
    "pricing": "Pricing (price list)",
    "experience": "Business stage & experience",
    "staffing": "Staffing",
    "offering": "Offering described",
    "differentiation": "Differentiation (your idea)",
}

# Largest |baseline + sum - prediction| the decomposition may show
# before it is treated as broken rather than as float rounding.
DECOMPOSITION_TOLERANCE = 1e-6


class DecompositionError(RuntimeError):
    """The path decomposition did not add back up to the prediction --
    which would mean the forest is not the plain averaging regressor
    this code assumes. Raised rather than reported, so a wrong
    explanation is never shown."""


def _tree_input(x):
    """The row exactly as scikit-learn's trees see it: float32,
    C-contiguous. Each estimator's public predict()/decision_path()
    re-validates its input on every call, and over a hundred trees that
    validation -- not the arithmetic -- was most of a forecast's time
    (~140 ms). Calling the compiled `tree_` methods with the row already
    in the trees' own dtype does the same lookups once validated, and
    gives the same nodes: this is the conversion sklearn itself applies
    before descending a tree."""
    return np.ascontiguousarray(x, dtype=np.float32)


_FLOAT32_MAX = float(np.finfo(np.float32).max)


def tree_safe(feature_vector):
    """True when every value is finite and fits in float32 -- the input
    RF2 can be asked about without predict() raising. derive_quantities
    already guarantees this (finite_input); plan_forecast_service checks
    it again at the boundary so that a future feature added without
    that guard degrades to the labelled formula instead of a 500."""
    values = np.asarray(feature_vector, dtype=np.float64)
    return bool(np.all(np.isfinite(values)) and np.all(np.abs(values) <= _FLOAT32_MAX))


def tree_leaf_values(forest, feature_vector):
    """Every tree's own prediction for one row (the value of the leaf it
    lands in) -- the spread the confidence is computed from, without
    walking each path."""
    x_tree = _tree_input(np.asarray(feature_vector, dtype=np.float64).reshape(1, -1))
    return np.array([
        float(tree.tree_.value[tree.tree_.apply(x_tree)[0], 0, 0]) for tree in forest.estimators_
    ])


def forest_mean(leaf_values):
    """The forest's prediction from its trees' leaf values: added one
    tree at a time, in estimator order, then divided by the number of
    trees -- the same float64 operations, in the same order, that
    RandomForestRegressor.predict() performs on one thread, so the
    result is bit-identical to it (tests/test_plan_forecast_model.py
    checks that) at a fraction of the cost: predict() goes through
    joblib's dispatch even with n_jobs=1, ~30 ms a call here, which the
    four-quarter outlook would otherwise pay four times per page."""
    total = 0.0
    for value in leaf_values:
        total += float(value)
    return total / len(leaf_values)


def path_contributions(forest, feature_vector):
    """(baseline, contributions, prediction, tree_predictions) for one row.

    `contributions` is a numpy array aligned with PLAN_FEATURE_NAMES;
    `tree_predictions` is every tree's own answer (its leaf value), from
    which the confidence is computed -- the same tree-spread measure
    stage 1 uses -- without walking the forest a second time.
    """
    x = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
    x_tree = _tree_input(x)
    contributions = np.zeros(x.shape[1], dtype=np.float64)
    baseline = 0.0
    tree_predictions = []

    for tree in forest.estimators_:
        structure = tree.tree_
        values = structure.value[:, 0, 0]
        # Node ids on x's root-to-leaf path. sklearn numbers a child
        # after its parent, so ascending id order IS path order.
        path = np.sort(structure.decision_path(x_tree).indices)
        baseline += float(values[path[0]])
        tree_predictions.append(float(values[path[-1]]))
        if len(path) > 1:
            np.add.at(contributions, structure.feature[path[:-1]],
                      values[path[1:]] - values[path[:-1]])

    n_trees = len(forest.estimators_)
    baseline /= n_trees
    contributions /= n_trees
    prediction = float(forest.predict(x)[0])

    error = abs(baseline + float(contributions.sum()) - prediction)
    if error > DECOMPOSITION_TOLERANCE:
        raise DecompositionError(
            f"path decomposition is off by {error:.3g} points; refusing to report it"
        )
    return baseline, contributions, prediction, np.array(tree_predictions)


def grouped_drivers(contributions):
    """{driver_key: points} -- the fourteen per-feature contributions
    summed into the eight drivers of FEATURE_GROUPS. Every driver key is
    present, even at 0, so a reader can see an input that did not move
    the forecast as clearly as one that did."""
    totals = {key: 0.0 for key in DRIVER_LABELS}
    for name, value in zip(PLAN_FEATURE_NAMES, contributions):
        totals[FEATURE_GROUPS[name]] += float(value)
    return totals


def round_preserving_sum(values, total, ndigits=1):
    """Round `values` to `ndigits` so that they add up to `total` rounded
    to the same precision -- the largest-remainder method.

    The payload shows "baseline + drivers = viability". Rounding each
    term on its own lets that sum miss by a few tenths, and a breakdown
    that does not add up reads as a breakdown that is wrong. Each value
    moves by at most one unit in the last place, which is the rounding
    error it had anyway."""
    scale = 10 ** ndigits
    target = int(round(total * scale))
    scaled = [v * scale for v in values]
    floors = [math.floor(v) for v in scaled]
    remainder = target - sum(floors)
    order = sorted(range(len(values)), key=lambda i: -(scaled[i] - floors[i]))
    if remainder >= 0:
        for i in order[:remainder]:
            floors[i] += 1
    else:
        for i in list(reversed(order))[:(-remainder)]:
            floors[i] -= 1
    return [f / scale for f in floors]
