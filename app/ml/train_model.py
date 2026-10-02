"""
app/ml/train_model.py
-----------------------
Trains the AI Forecasting Engine's Random Forest -- the model
app/services/forecasting_service.py loads to predict saturation_index
(0-100) from the 8-feature vector defined in app/ml/constants.py:

    [competitor_count, population_density, foot_traffic_index,
     average_rent, historical_success_rate, business_density,
     years_in_operation, industry_type_encoded]

This file also runs a K-Means (K=4) clustering pass, per the paper's
stated methodology, on a (competitor pressure, business density)
feature space -- but note the difference from an earlier draft of this
system: the live cluster_label shown to users (see
app/models/forecast_result.py) is derived from fixed threshold cut
points on saturation_index (CLUSTER_THRESHOLDS in constants.py), NOT
from this K-Means model at inference time. The given forecast_result
schema has no place to persist a KMeans cluster id, and predicting
saturation_index with the Random Forest and then thresholding it is
simpler, deterministic, and ties every cluster boundary to the exact
same 0-100 scale used everywhere else in the app. K-Means still runs
here and its cluster_to_label mapping is written to
training_report.json purely as evidence for your methodology chapter
that the clustering step described in the paper was actually
implemented and evaluated.

WHERE DOES THE TRAINING DATA COME FROM?
This is a capstone/demo project with no historical ground-truth
saturation labels yet (real historical SME survival/closure records
per the paper's "Sources of Data" section haven't been collected). So
this script builds a SYNTHETIC labeled dataset: it samples plausible
feature combinations around the reference barangay profiles in
app/ml/seed_data.py, computes a "true" saturation_index using the
paper's own weighted formula (w1*CD + w2*DT + w3*SD, scaled to 0-100)
plus random noise (to imitate real-world unpredictability), and trains
the Random Forest to reconstruct that relationship from raw features
alone.

This gives you TWO honest, defensible things for your evaluation
chapter:
  - A working, reproducible training pipeline you can re-run the
    moment you have real DTI/PSA/LGU historical data (see README
    "Replacing the synthetic data with real data").
  - Real MAE / RMSE / accuracy numbers (see training_report.json)
    computed on a held-out test split -- not made-up numbers.

STAGE 2 -- THE PLAN VIABILITY MODEL (train_plan_model, below)
The Random Forest above answers "how crowded is this market?". A second
forest, RF2, is trained here on top of it to answer "how viable is THIS
PLAN in that market?" -- with the owner's capital, employees, business
stage, price list, offering and idea as inputs alongside stage 1's
output. Its synthetic labels come from a documented scorecard
(app/ml/plan_model.py, Reference/FORECAST_MODEL.md), exactly as stage
1's come from the paper's MSI formula, and the same honesty note
applies: retrain on real outcomes the moment there are any. It is saved
to model_store/plan_model.pkl and reported under "plan_model" in
training_report.json; every stage-1 key in that report is unchanged.

Run it with:
    python -m app.ml.train_model              both stages
    python -m app.ml.train_model --plan-only  stage 2 alone, on the
                                              rf_model.pkl already on disk
(from the flask_webapp_dss folder, inside your activated venv)
"""

import argparse
import json
import math
import os
import random
import sys

import numpy as np
import joblib
from sklearn.cluster import KMeans
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

from app.ml.constants import (
    BUSINESS_TYPES,
    BUSINESS_TYPE_ENCODING,
    OTHER_INDUSTRY_ENCODING,
    FEATURE_NAMES,
    CLUSTER_LABELS_ORDERED,
    N_CLUSTERS,
    RANDOM_STATE,
    N_ESTIMATORS,
    DEFAULT_DAILY_WAGE_PHP,
    DEFAULT_GROSS_MARGIN,
    EXPERIENCE_FULL_YEARS,
    MINIMUM_RAMP_MONTHS,
    MONTHLY_FIXED_COST_FLOOR_PHP,
    N_PLAN_SAMPLES,
    OPERATING_DAYS_PER_MONTH,
    PLAN_COMPONENT_WEIGHTS,
    PLAN_FEATURE_NAMES,
    PLAN_RANDOM_STATE,
    PURCHASES_PER_RESIDENT_PER_DAY,
    STAFF_FULL_CAPACITY,
)
from app.ml.seed_data import BARANGAY_PROFILES

MODEL_DIR = os.path.join(os.path.dirname(__file__), "model_store")
RF_MODEL_FILE = "rf_model.pkl"
REPORT_FILE = "training_report.json"

# MSI weights (mirrors app/config.py defaults / system_settings table)
W1_COMPETITOR_DENSITY = 0.45
W2_DEMAND_TREND = 0.35
W3_SOCIODEMOGRAPHIC = 0.20

N_SAMPLES = 4000


def _normalize(value, lo, hi):
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _make_synthetic_dataset(rng):
    profiles = [
        {
            "name": name,
            "population_density": population_density,
            "foot_traffic_index": foot_traffic_index,
            "average_rent": average_rent,
            "business_density": business_density,
            "historical_success_rate": historical_success_rate,
            "urban": urban,
        }
        for name, population_density, foot_traffic_index, average_rent, business_density, historical_success_rate, urban
        in BARANGAY_PROFILES
    ]

    pop_lo = min(p["population_density"] for p in profiles)
    pop_hi = max(p["population_density"] for p in profiles)
    rent_lo = min(p["average_rent"] for p in profiles)
    rent_hi = max(p["average_rent"] for p in profiles)
    density_lo = min(p["business_density"] for p in profiles)
    density_hi = max(p["business_density"] for p in profiles)

    rows = []
    for _ in range(N_SAMPLES):
        brgy = profiles[rng.integers(0, len(profiles))]
        industry_type = BUSINESS_TYPES[rng.integers(0, len(BUSINESS_TYPES))]

        # Competitor count: denser/busier locations realistically support
        # (and attract) more competitors; add noise so it's not a
        # perfect function of foot traffic alone.
        base_competitors = 2 + 25 * _normalize(brgy["foot_traffic_index"], 0, 100)
        competitor_count = max(0, int(rng.normal(base_competitors, 4)))

        years_in_operation = round(max(0.0, rng.uniform(0, 8)), 1)

        population_density = brgy["population_density"] * rng.uniform(0.9, 1.1)
        foot_traffic_index = brgy["foot_traffic_index"] * rng.uniform(0.9, 1.1)
        average_rent = brgy["average_rent"] * rng.uniform(0.9, 1.1)
        historical_success_rate = max(0.0, min(1.0, brgy["historical_success_rate"] + rng.normal(0, 0.03)))
        business_density = brgy["business_density"] * rng.uniform(0.9, 1.1)
        industry_type_encoded = BUSINESS_TYPE_ENCODING.get(industry_type, OTHER_INDUSTRY_ENCODING)

        # ---- component scores (0-1), matching the paper's definitions ----
        cd_score = _normalize(competitor_count, 0, 30)
        dt_score = 1 - historical_success_rate  # low historical success = high demand-decline pressure
        sd_score = (
            0.5 * _normalize(population_density, pop_lo, pop_hi)
            + 0.3 * _normalize(average_rent, rent_lo, rent_hi)
            + 0.2 * (1.0 if brgy["urban"] else 0.0)
        )
        sd_score = max(0.0, min(1.0, sd_score))

        true_saturation = 100 * (
            W1_COMPETITOR_DENSITY * cd_score
            + W2_DEMAND_TREND * dt_score
            + W3_SOCIODEMOGRAPHIC * sd_score
        )
        true_saturation = max(0.0, min(100.0, true_saturation + rng.normal(0, 4.0)))  # real-world noise

        rows.append(
            {
                "competitor_count": competitor_count,
                "population_density": population_density,
                "foot_traffic_index": foot_traffic_index,
                "average_rent": average_rent,
                "historical_success_rate": historical_success_rate,
                "business_density": business_density,
                "years_in_operation": years_in_operation,
                "industry_type_encoded": industry_type_encoded,
                "cd_score": cd_score,
                "business_density_norm": _normalize(business_density, density_lo, density_hi),
                "saturation_index": true_saturation,
            }
        )
    return rows


def _label_clusters(kmeans):
    """Rank cluster centroids by how 'saturated' they look (average of
    the two clustering features) so cluster index 3 isn't arbitrarily
    the 'Saturated' label -- it's whichever centroid actually scores
    highest."""
    centroid_scores = kmeans.cluster_centers_.mean(axis=1)
    order = np.argsort(centroid_scores)  # ascending: lowest saturation first
    cluster_to_label = {}
    for rank, cluster_idx in enumerate(order):
        cluster_to_label[int(cluster_idx)] = CLUSTER_LABELS_ORDERED[rank]
    return cluster_to_label


def train_and_save(verbose=True):
    os.makedirs(MODEL_DIR, exist_ok=True)
    rng = np.random.default_rng(RANDOM_STATE)
    random.seed(RANDOM_STATE)

    data = _make_synthetic_dataset(rng)

    X = np.array([[r[f] for f in FEATURE_NAMES] for r in data])
    y = np.array([r["saturation_index"] for r in data])

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=RANDOM_STATE)

    rf_model = RandomForestRegressor(
        n_estimators=N_ESTIMATORS,
        max_depth=12,
        min_samples_leaf=2,
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )
    rf_model.fit(X_train, y_train)

    y_pred = rf_model.predict(X_test)
    mae = float(mean_absolute_error(y_test, y_pred))          # in saturation-index POINTS (0-100 scale)
    rmse = float(mean_squared_error(y_test, y_pred) ** 0.5)    # same units
    accuracy_percent = round(max(0.0, 100.0 - mae), 2)

    feature_importances = dict(zip(FEATURE_NAMES, [float(v) for v in rf_model.feature_importances_]))

    # ---- K-Means on (competitor pressure, business density) pairs ----
    # See module docstring: this satisfies the paper's stated clustering
    # methodology and is reported below for your evaluation chapter, but
    # is NOT what forecasting_service.py uses to label a live forecast
    # (that uses fixed thresholds on the RF's own saturation_index
    # prediction instead -- simpler and schema-compatible).
    cluster_features = np.array([[r["cd_score"], r["business_density_norm"]] for r in data])
    kmeans_model = KMeans(n_clusters=N_CLUSTERS, random_state=RANDOM_STATE, n_init=10)
    kmeans_model.fit(cluster_features)
    cluster_to_label = _label_clusters(kmeans_model)

    joblib.dump(rf_model, os.path.join(MODEL_DIR, "rf_model.pkl"))

    report = {
        "trained_on_samples": len(data),
        "test_samples": len(y_test),
        "mae_points": round(mae, 4),
        "rmse_points": round(rmse, 4),
        "accuracy_percent": accuracy_percent,
        "target_accuracy_percent": 85.0,
        "meets_target": accuracy_percent >= 85.0,
        "feature_importances": feature_importances,
        "kmeans_cluster_to_label": {str(k): v for k, v in cluster_to_label.items()},
        "msi_weights": {
            "w1_competitor_density": W1_COMPETITOR_DENSITY,
            "w2_demand_trend": W2_DEMAND_TREND,
            "w3_sociodemographic": W3_SOCIODEMOGRAPHIC,
        },
        "note": (
            "Trained on SYNTHETIC data derived from the paper's own MSI formula "
            "(see docstring at the top of this file). Re-train on real DTI/PSA/LGU "
            "historical data before citing these numbers as real-world accuracy. "
            "saturation_index/mae/rmse are all on a 0-100 scale, matching "
            "forecast_result.saturation_index in the real database."
        ),
    }

    # Stage 2 is trained on THIS run's stage-1 forest, after everything
    # above is finished, with its own freshly seeded generator -- so it
    # draws nothing from `rng` and cannot change a single stage-1
    # number. Its section is added under one new key; every key above
    # is exactly what it was before stage 2 existed.
    report["plan_model"] = train_plan_model(
        rf_model, np.random.default_rng(PLAN_RANDOM_STATE), verbose=verbose
    )

    with open(os.path.join(MODEL_DIR, "training_report.json"), "w") as f:
        json.dump(report, f, indent=2)

    if verbose:
        print("=" * 70)
        print("AI Forecasting Engine -- training complete")
        print("=" * 70)
        print(f"  Samples trained on : {report['trained_on_samples']}")
        print(f"  MAE (points)       : {report['mae_points']}")
        print(f"  RMSE (points)      : {report['rmse_points']}")
        print(f"  Accuracy (100-MAE) : {report['accuracy_percent']}%  (target >= 85%)")
        print(f"  K-Means cluster map: {report['kmeans_cluster_to_label']}")
        print(f"  Model saved to     : {MODEL_DIR}")
        print("=" * 70)

    return report


# =====================================================================
# STAGE 2 -- the Plan Viability Model (RF2)
# =====================================================================
# WHERE ITS TRAINING DATA COMES FROM. Same situation as stage 1: there
# are no recorded outcomes of Tarlac City business plans to learn from
# yet. So each synthetic plan is
#
#   1. placed in a market drawn EXACTLY the way stage 1's sampler draws
#      one (a barangay profile, an industry, a competitor count around
#      that barangay's foot traffic, the same +/-10% jitter);
#   2. scored by the TRAINED stage-1 forest -- rf.predict, not the MSI
#      formula -- so stage 2 learns on the saturation figures stage 1
#      actually produces, quirks included, rather than on an idealised
#      version of them. 30% of plans get a sub-category style
#      adjustment (competitor count x U(0.3, 1.6), re-scored by the
#      forest), as a bakery inside "Food and Beverage" would;
#   3. given business parameters drawn from deliberately wide ranges
#      (capital P10k-P10M log-uniform, 0-20 staff skewed small, 30%
#      existing businesses, a price list 60% of the time...);
#   4. labelled with the scorecard S of app/ml/plan_model.py plus
#      N(0, 3) noise -- the same "documented formula + real-world
#      unpredictability" construction as stage 1's labels.
#
# So RF2's accuracy figures measure how well it RECOVERS THE DOCUMENTED
# SCORECARD from raw inputs, not how well it predicts real business
# survival. That is said in the report itself, and it is the reason the
# pipeline exists as code: re-run it on real outcomes and the numbers
# become real ones.

# Plan-parameter sampling distributions, named so the report can quote
# them and a reader can argue with them.
_PLAN_CAPITAL_RANGE = (10_000.0, 10_000_000.0)          # PHP, log-uniform
_PLAN_PRICE_RANGE = (10.0, 20_000.0)                    # PHP, log-uniform
_PLAN_EMPLOYEE_CHOICES = np.arange(0, 21)               # 0-20 employees...
_PLAN_EMPLOYEE_WEIGHTS = 0.8 ** _PLAN_EMPLOYEE_CHOICES  # ...each 20% rarer than the last
_PLAN_EMPLOYEE_WEIGHTS = _PLAN_EMPLOYEE_WEIGHTS / _PLAN_EMPLOYEE_WEIGHTS.sum()
_PLAN_ITEM_CHOICES = np.arange(1, 31)                   # 1-30 priced items...
_PLAN_ITEM_WEIGHTS = 0.9 ** (_PLAN_ITEM_CHOICES - 1)    # ...each 10% rarer than the last
_PLAN_ITEM_WEIGHTS = _PLAN_ITEM_WEIGHTS / _PLAN_ITEM_WEIGHTS.sum()
_PLAN_LABEL_NOISE_SD = 3.0
_PLAN_PROBABILITIES = {
    "subcategory_adjusted": 0.3,
    "existing_business": 0.3,
    "no_price_list": 0.4,
    "offering_described": 0.7,
    "has_innovation_idea": 0.5,
}


def _log_uniform(rng, low, high):
    return float(math.exp(rng.uniform(math.log(low), math.log(high))))


def _barangay_populations():
    """{barangay: population the model uses} -- the real 2024 PSA
    figure, through the same resolve_population() fallback inference
    applies (all 76 have a real figure, so the fallback is never hit
    here)."""
    from app.ml.plan_model import resolve_population
    from app.ml.seed_data import _parse_source_comments

    notes = _parse_source_comments()
    populations = {}
    for row in BARANGAY_PROFILES:
        raw = (notes.get(row[0]) or {}).get("population_2024_psa")
        try:
            populations[row[0]] = resolve_population(int(raw))
        except (TypeError, ValueError):
            populations[row[0]] = resolve_population(None)
    return populations


def _make_plan_dataset(rf_market_model, rng):
    """N_PLAN_SAMPLES synthetic plans: (inputs dicts, feature matrix,
    noise-free scorecard, noisy labels). See the block comment above."""
    from app.ml.plan_model import derive_quantities, plan_feature_vector, scorecard_index

    profiles = [
        {
            "name": name,
            "population_density": population_density,
            "foot_traffic_index": foot_traffic_index,
            "average_rent": average_rent,
            "business_density": business_density,
            "historical_success_rate": historical_success_rate,
        }
        for name, population_density, foot_traffic_index, average_rent, business_density, historical_success_rate, _urban
        in BARANGAY_PROFILES
    ]
    populations = _barangay_populations()
    p = _PLAN_PROBABILITIES

    drafts, industry_vectors, direct_vectors = [], [], []
    for _ in range(N_PLAN_SAMPLES):
        # ---- the market: drawn exactly as _make_synthetic_dataset does ----
        brgy = profiles[rng.integers(0, len(profiles))]
        industry_type = BUSINESS_TYPES[rng.integers(0, len(BUSINESS_TYPES))]
        base_competitors = 2 + 25 * _normalize(brgy["foot_traffic_index"], 0, 100)
        competitor_count = max(0, int(rng.normal(base_competitors, 4)))
        population_density = brgy["population_density"] * rng.uniform(0.9, 1.1)
        foot_traffic_index = brgy["foot_traffic_index"] * rng.uniform(0.9, 1.1)
        average_rent = brgy["average_rent"] * rng.uniform(0.9, 1.1)
        historical_success_rate = max(0.0, min(1.0, brgy["historical_success_rate"] + rng.normal(0, 0.03)))
        business_density = brgy["business_density"] * rng.uniform(0.9, 1.1)
        industry_type_encoded = BUSINESS_TYPE_ENCODING.get(industry_type, OTHER_INDUSTRY_ENCODING)

        # ---- the plan ----
        is_existing = bool(rng.random() < p["existing_business"])
        years_in_operation = round(float(rng.uniform(0, 15)), 1) if is_existing else 0.0
        adjusted = bool(rng.random() < p["subcategory_adjusted"])
        adjustment = float(rng.uniform(0.3, 1.6))
        direct_count = max(0, int(round(competitor_count * adjustment))) if adjusted else competitor_count
        capital = _log_uniform(rng, *_PLAN_CAPITAL_RANGE)
        employees = int(rng.choice(_PLAN_EMPLOYEE_CHOICES, p=_PLAN_EMPLOYEE_WEIGHTS))
        if rng.random() < p["no_price_list"]:
            priced_items, average_price = 0, 0.0
        else:
            priced_items = int(rng.choice(_PLAN_ITEM_CHOICES, p=_PLAN_ITEM_WEIGHTS))
            average_price = _log_uniform(rng, *_PLAN_PRICE_RANGE)
        described = bool(rng.random() < p["offering_described"]) or priced_items > 0
        has_idea = bool(rng.random() < p["has_innovation_idea"])

        # Stage 1 sees the plan's own years in business, as
        # compute_scores(years_in_operation=...) does for a real plan...
        industry_vectors.append([
            competitor_count, population_density, foot_traffic_index, average_rent,
            historical_success_rate, business_density, years_in_operation, industry_type_encoded,
        ])
        # ...and the sub-category re-score uses years 0, exactly as
        # forecasting_service.saturation_for_counts does at inference.
        direct_vectors.append([
            direct_count, population_density, foot_traffic_index, average_rent,
            historical_success_rate, business_density, 0.0, industry_type_encoded,
        ])
        drafts.append({
            "adjusted": adjusted,
            "competitor_count": direct_count,
            "population": populations[brgy["name"]],
            "monthly_rent": average_rent,
            "employee_count": employees,
            "daily_wage": DEFAULT_DAILY_WAGE_PHP,
            "gross_margin": DEFAULT_GROSS_MARGIN,
            "capital": capital,
            "is_existing": is_existing,
            "years_in_operation": years_in_operation,
            "priced_item_count": priced_items,
            "average_price": average_price,
            "has_offering_description": described,
            "has_innovation_idea": has_idea,
        })

    # Stage 1's TRAINED forest scores every market, in one batch each.
    # Pinned to one thread for the duration: with n_jobs=-1 the
    # summation order of the trees varies run to run (see
    # forecasting_service._pin_to_one_thread), and the training set
    # should not differ in the last bit between two runs of this script.
    original_jobs = rf_market_model.n_jobs
    rf_market_model.n_jobs = 1
    try:
        industry_msi = np.clip(rf_market_model.predict(np.array(industry_vectors, dtype=float)), 0.0, 100.0)
        direct_msi = np.clip(rf_market_model.predict(np.array(direct_vectors, dtype=float)), 0.0, 100.0)
    finally:
        rf_market_model.n_jobs = original_jobs

    inputs_list, features, scorecard = [], [], []
    for index, draft in enumerate(drafts):
        adjusted = draft.pop("adjusted")
        inputs = dict(draft)
        inputs["saturation_index"] = float(direct_msi[index] if adjusted else industry_msi[index])
        derived = derive_quantities(inputs)
        inputs_list.append(inputs)
        features.append(plan_feature_vector(inputs, derived))
        scorecard.append(scorecard_index(inputs, derived))

    scorecard = np.array(scorecard)
    labels = np.clip(scorecard + rng.normal(0, _PLAN_LABEL_NOISE_SD, size=len(scorecard)), 0.0, 100.0)
    return inputs_list, np.array(features, dtype=float), scorecard, labels


def train_plan_model(rf_market_model, rng=None, verbose=True):
    """Train RF2 on top of `rf_market_model` (stage 1's trained forest),
    save model_store/plan_model.pkl, and return the "plan_model" section
    of training_report.json.

    The pickle is a small bundle, not the bare forest: it records the
    PLAN_FEATURE_NAMES order the forest was trained on, and
    plan_forecast_service refuses a bundle whose order differs from the
    code's (falling back to the documented formula, loudly) instead of
    feeding it a row in the wrong order -- which would not error, it
    would just be wrong.
    """
    from app.ml.plan_model import PLAN_MODEL_FILENAME, PLAN_MODEL_VERSION

    os.makedirs(MODEL_DIR, exist_ok=True)
    if rng is None:
        rng = np.random.default_rng(PLAN_RANDOM_STATE)

    _inputs, X, scorecard, y = _make_plan_dataset(rf_market_model, rng)

    indices = np.arange(len(y))
    train_idx, test_idx = train_test_split(indices, test_size=0.2, random_state=RANDOM_STATE)

    plan_model = RandomForestRegressor(
        n_estimators=N_ESTIMATORS,
        max_depth=14,
        min_samples_leaf=3,
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )
    plan_model.fit(X[train_idx], y[train_idx])

    # Pinned for evaluation, for the same reproducibility reason as above.
    plan_model.n_jobs = 1
    y_pred = plan_model.predict(X[test_idx])
    plan_model.n_jobs = -1

    y_test = y[test_idx]
    mae = float(mean_absolute_error(y_test, y_pred))
    rmse = float(mean_squared_error(y_test, y_pred) ** 0.5)
    r2 = float(r2_score(y_test, y_pred))
    # How far the noisy labels themselves sit from the noise-free
    # formula: the error even a perfect model of the formula would
    # show. Reported so MAE can be read against what is achievable.
    noise_floor = float(mean_absolute_error(y_test, scorecard[test_idx]))
    # How closely the forest reproduces the formula itself.
    mae_vs_formula = float(mean_absolute_error(scorecard[test_idx], y_pred))
    accuracy_percent = round(max(0.0, 100.0 - mae), 2)

    joblib.dump(
        {
            "model": plan_model,
            "feature_names": list(PLAN_FEATURE_NAMES),
            "model_version": PLAN_MODEL_VERSION,
            "trained_on_samples": int(len(train_idx)),
        },
        os.path.join(MODEL_DIR, PLAN_MODEL_FILENAME),
    )

    section = {
        "model_file": PLAN_MODEL_FILENAME,
        "model_version": PLAN_MODEL_VERSION,
        "target": "Plan Viability Index (0-100); shown to users as a 0-10 Plan Viability Score",
        "trained_on_samples": int(len(train_idx)),
        "test_samples": int(len(test_idx)),
        "total_samples": int(len(y)),
        "mae_points": round(mae, 4),
        "rmse_points": round(rmse, 4),
        "r2": round(r2, 4),
        "accuracy_percent": accuracy_percent,
        "label_noise_sd_points": _PLAN_LABEL_NOISE_SD,
        "label_noise_floor_mae_points": round(noise_floor, 4),
        "mae_vs_noise_free_scorecard_points": round(mae_vs_formula, 4),
        "feature_names": list(PLAN_FEATURE_NAMES),
        "feature_importances": dict(zip(PLAN_FEATURE_NAMES, [float(v) for v in plan_model.feature_importances_])),
        "component_weights": dict(PLAN_COMPONENT_WEIGHTS),
        "hyperparameters": {
            "n_estimators": N_ESTIMATORS, "max_depth": 14, "min_samples_leaf": 3,
            "random_state": RANDOM_STATE, "data_seed": PLAN_RANDOM_STATE,
        },
        "assumptions": {
            "operating_days_per_month": OPERATING_DAYS_PER_MONTH,
            "daily_wage_php": DEFAULT_DAILY_WAGE_PHP,
            "daily_wage_source": "DOLE Wage Order No. RBIII-26 (2nd tranche, effective 16 Apr 2026): "
                                 "Tarlac retail & service establishments",
            "gross_margin": DEFAULT_GROSS_MARGIN,
            "purchases_per_resident_per_day": round(PURCHASES_PER_RESIDENT_PER_DAY, 6),
            "experience_full_years": EXPERIENCE_FULL_YEARS,
            "staff_full_capacity": STAFF_FULL_CAPACITY,
            "minimum_ramp_months": MINIMUM_RAMP_MONTHS,
            "monthly_fixed_cost_floor_php": MONTHLY_FIXED_COST_FLOOR_PHP,
        },
        "sampling": {
            "market": "barangay profile + industry + competitor count drawn as stage 1's sampler does; "
                      "saturation from the TRAINED stage-1 forest",
            "capital_php_log_uniform": list(_PLAN_CAPITAL_RANGE),
            "average_price_php_log_uniform": list(_PLAN_PRICE_RANGE),
            "employees": "0-20, each count 20% less likely than the one below",
            "priced_items": "1-30 when a price list exists, each count 10% less likely than the one below",
            "subcategory_adjustment": "competitor count x U(0.3, 1.6), re-scored by the stage-1 forest",
            "years_in_operation_existing": [0, 15],
            "probabilities": dict(_PLAN_PROBABILITIES),
        },
        "note": (
            "Trained on SYNTHETIC plans labelled with the documented scorecard in app/ml/plan_model.py "
            "(see Reference/FORECAST_MODEL.md) plus N(0, 3) noise. These metrics measure how well the "
            "forest recovers that scorecard from raw inputs, not real-world business survival. Re-train "
            "on recorded plan outcomes before citing them as real-world accuracy."
        ),
    }

    if verbose:
        print("-" * 70)
        print("Plan Viability Model (stage 2) -- training complete")
        print(f"  Samples trained on : {section['trained_on_samples']}")
        print(f"  MAE (points)       : {section['mae_points']}  (label-noise floor {section['label_noise_floor_mae_points']})")
        print(f"  RMSE (points)      : {section['rmse_points']}")
        print(f"  R^2                : {section['r2']}")
        print(f"  Accuracy (100-MAE) : {section['accuracy_percent']}%")
        print(f"  Model saved to     : {os.path.join(MODEL_DIR, PLAN_MODEL_FILENAME)}")
        print("-" * 70)

    return section


def train_plan_only(verbose=True):
    """Stage 2 alone, on the stage-1 forest already on disk -- what
    seed.py runs when rf_model.pkl exists but plan_model.pkl does not,
    so adding stage 2 to an existing install does not retrain (and
    possibly shift) stage 1. Updates only the "plan_model" key of
    training_report.json. Falls back to a full run when there is no
    stage-1 model to build on."""
    rf_path = os.path.join(MODEL_DIR, RF_MODEL_FILE)
    if not os.path.exists(rf_path):
        if verbose:
            print("No stage-1 model on disk (rf_model.pkl) -- training both stages.")
        return train_and_save(verbose=verbose)

    rf_model = joblib.load(rf_path)
    section = train_plan_model(rf_model, np.random.default_rng(PLAN_RANDOM_STATE), verbose=verbose)

    report_path = os.path.join(MODEL_DIR, REPORT_FILE)
    report = {}
    if os.path.exists(report_path):
        try:
            with open(report_path) as f:
                report = json.load(f)
        except (OSError, ValueError):
            report = {}
    report["plan_model"] = section
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train the AI Forecasting Engine's models.")
    parser.add_argument("--plan-only", action="store_true",
                        help="train only stage 2 (the Plan Viability Model) on the rf_model.pkl already on disk")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.plan_only:
        train_plan_only()
    else:
        train_and_save()


if __name__ == "__main__":
    main()
