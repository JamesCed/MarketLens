# Reference/FORECAST_MODEL.md

How a business plan's forecast is computed, and how its output becomes
the numbers and words the owner sees. This covers both trained models,
every input and the formula it goes through, how the models were trained
and how well, how the result is explained, and how the AI narrator
(Gemini first) puts it into words without changing a figure.

The code is the authority. This file describes `app/ml/plan_model.py`,
`app/ml/constants.py`, `app/ml/train_model.py`,
`app/services/plan_forecast_service.py`,
`app/services/forecasting_service.py`,
`app/services/subcategory_service.py`,
`app/services/recommendation_service.py` and
`app/services/llm_service.py`. If this file and the code ever disagree,
the code is what runs and this file is the bug.

**How the numbers here were checked (2 October 2026).** The metrics in
section 3.7 are copied from `app/ml/model_store/training_report.json`.
Re-running the full training from scratch reproduced every one of them
exactly, and a bit-identical stage-2 forest. Every number in the worked
example (section 5) was printed by running the real pipeline
(`generate_forecast_for_profile`), not computed by hand. The direction
checks in section 3.12 were re-measured on the same model.

**Contents**

1. The pipeline in one picture
2. Stage 1: Market Saturation Index
3. Stage 2: the Plan Viability Model
4. The forecast payload
5. Worked example, computed by the real code
6. How Gemini transcribes the result
7. Where the forecast appears
8. What this model is not

---

## 1. The pipeline in one picture

```
 THE OWNER'S PLAN  (Home > Add / Edit plan, or sign-up step 2)
   industry, sub-category, location .............. which market the plan enters
   capital, employees, business stage (+ registration date),
   product offering, price list, innovation idea .. the plan itself
   business name .................................. identifies the plan; never a model input
        |
        v
 STAGE 1 -- Market Saturation Model (RF1, model_store/rf_model.pkl)      unchanged
   8 market features from market_data / lgu_data  ->  MSI (0-100)
   forecasting_service.compute_scores()
        |
        v
 SUB-CATEGORY ADJUSTMENT (subcategory_service.adjusted_scores)
   measured direct competitors vs. expected  ->  adjusted competitor count
   ->  RF1 again  ->  MSI*            (MSI* = MSI when nothing was measured)
        |
        |   MSI*, the competitor count it was scored with,
        |   the barangay's rent (market_data) and real PSA 2024 population
        v
 STAGE 2 -- Plan Viability Model (RF2, model_store/plan_model.pkl)       new
   14 features: MSI* + every business parameter + derived money figures
   ->  Plan Viability Index PVI (0-100)
   ->  Plan Viability Score = PVI / 10 (0-10, 1 dp)   <- what the owner sees
   +   confidence (tree spread), exact driver decomposition, break-even window
   plan_forecast_service.forecast_plan()
        |
        v
 FORECAST PAYLOAD  (numbers only, from the models; stored under "forecast"
                    in forecast_result.recommendation)
        |
        +--> Home gauge, "How this forecast was computed", quarterly outlook,
        |    the Recommendations page's plan card
        v
 NARRATION (recommendation_service.build_recommendation)
   a rule-based explanation is ALWAYS computed from the payload;
   when the AI switch is on: Gemini first, then the configured LLM, then the other
   -> grounding check: any number in the LLM's explanation that is not one the
      prompt showed it (model output, plan inputs, market comparison)
      -> the LLM's explanation is discarded and the rule-based one is kept
```

Stage 1 answers "how crowded is this market?" (an industry in a
barangay). Stage 2 answers "how viable is **this plan** in that market?"
Before stage 2 existed, a plan's viability was simply
`(100 - MSI) / 10`. Two plans for the same kind of business in the same
barangay got the same score whether one had ₱5,000,000 behind it and the
other ₱20,000, and whether or not it had a sensible price list. The
owner's parameters were collected, shown back to them, and then ignored
by the number. Stage 2 is where they count.

The Saturation Map, Trend Reports and the Recommendations page's
location cards still use **stage 1 only**. Those pages score markets, not
plans, and a market's viability is still `(100 - MSI) / 10` there.

---

## 2. Stage 1: Market Saturation Index (unchanged by this revision)

### 2.1 Features

**Code:** `forecasting_service.build_feature_vector()`; the order is
`constants.FEATURE_NAMES`:

| # | Feature | Source |
|---|---|---|
| 1 | `competitor_count` | The reconciled count for this industry and barangay: the larger of the freshest Google Places count and the freshest LGU permit-register (DTI) count (`reconciled_competitor_counts`); failing both, the `market_data` row's own figure |
| 2 | `population_density` | `market_data.population_density` |
| 3 | `foot_traffic_index` | `market_data.foot_traffic_index` |
| 4 | `average_rent` | `market_data.average_rent` (₱/month) |
| 5 | `historical_success_rate` | `market_data.historical_success_rate` (0–1) |
| 6 | `business_density` | `lgu_data.business_density` |
| 7 | `years_in_operation` | The plan's own: 0 for a startup; years since `registration_date` (1 dp) for an existing business |
| 8 | `industry_type_encoded` | Index in `BUSINESS_TYPES`; anything else is the "other" bucket |

### 2.2 Training label and sampler

**Code:** `train_model._make_synthetic_dataset()`. 4,000 synthetic rows,
seed 42. For each row a reference barangay profile
(`seed_data.BARANGAY_PROFILES`, all 76) and an industry are drawn
uniformly, then:

```
competitor_count  = max(0, int( N(2 + 25 x foot_traffic/100, 4) ))
years_in_operation ~ U(0, 8)
density, foot traffic, rent, business density  = profile value x U(0.9, 1.1)
historical_success_rate = clamp(profile value + N(0, 0.03), 0, 1)

MSI_label = clip( 100 x (0.45 x CD + 0.35 x DT + 0.20 x SD) + N(0, 4), 0, 100 )
  CD = competitor_count / 30                                     clamped 0-1
  DT = 1 - historical_success_rate
  SD = 0.5 x pop_density_norm + 0.3 x rent_norm + 0.2 x urban     clamped 0-1
       (the _norm values are min-max scaled over the 76 profiles; urban is 0/1)
```

The weights 0.45 / 0.35 / 0.20 are the paper's MSI weights.

### 2.3 Model and metrics

`RandomForestRegressor(n_estimators=100, max_depth=12,
min_samples_leaf=2, random_state=42)`, 80/20 split (3,200 train, 800
test; the report's `trained_on_samples` field holds the 4,000 total).
From `training_report.json`:

| Metric | Stage 1 (RF1) |
|---|---|
| Test rows | 800 |
| MAE (points, 0–100) | 3.371 |
| RMSE | 4.2188 |
| "Accuracy" (100 − MAE) | 96.63% (target ≥ 85%) |
| Largest feature importances | competitor count 0.518, business density 0.170, foot traffic 0.169, rent 0.100 |

A K-Means (K = 4) pass also runs during training, as the paper's
methodology describes, and its cluster-to-label map is written to the
report. Nothing reads it at inference (see 2.6).

### 2.4 Confidence

From how much the 100 trees agree on this one market:

```
confidence = max(40, min(100, 100 - sd(tree predictions) / 25 x 60))
```

A spread of 0 gives 100%. The value falls linearly to a floor of 40% at
a spread of 25 points. Stage 2 uses the same mapping (section 3.8).

### 2.5 No trained model on disk

The weighted formula stands in (`_predict_saturation`), labelled
`formula_v1`, with confidence 50:

```
MSI = clip( 100 x ( w1 x min(1, competitor_count/30)
                  + w2 x (1 - historical_success_rate)
                  + w3 x min(1, business_density/10) ), 0, 100 )
```

`w1`, `w2` and `w3` are the `msi_weight_*` system settings (defaults
0.45 / 0.35 / 0.20). Note that the fallback's third term is business
density, not the training label's socio-demographic SD.

### 2.6 Tier

`cluster_label` uses fixed cut points on the saturation index: ≤ 25 Low,
≤ 50 Moderate, ≤ 75 High, otherwise Saturated.

### 2.7 Sub-category adjustment: MSI to MSI\*

**Code:** `subcategory_service.direct_competition()` and
`adjusted_scores()`.

Stage 1 scores an industry section. A bakery inside "Food and Beverage"
does not compete with every eatery. Feeding the forest the bakery count
instead of the food count would be wrong, because the forest learned
competitor counts at the industry scale. So the industry count is
**scaled** by how dense the sub-category is compared with what would be
expected:

```
expected share  = measured share across barangays (needs >= 3 barangays with a
                  measured count), else 1 / (number of countable sub-categories
                  in the industry)                    "even split"
expected direct = industry count x expected share
ratio           = measured direct count / expected direct      clamped 0.25-2.5
                  (expected direct = 0: ratio 1 if the measured count is 0, else 2.5)
adjusted count  = round(industry count x ratio)
```

The measured direct count comes from a stored Google Places search or
the LGU permit register's line-of-business column (the larger of the
two), or a live Places search when live fetching is allowed. The
adjusted count goes back through RF1 **with years in operation 0**
(`saturation_for_counts`), giving MSI\*, and the tier is recomputed from
MSI\*. The confidence stays the industry run's: the adjustment changes
one input, not how much the trees agree about the market.

When nothing has been measured, the direct count is only an estimate,
the ratio is exactly 1, and `MSI* = MSI`. A guess is never allowed to
change the score; it is labelled "estimated" wherever it appears.

---

## 3. Stage 2: the Plan Viability Model

### 3.1 Where every business parameter goes

| Plan field (form) | How it enters the forecast |
|---|---|
| `industry_type` | Stage 1 (industry code, market rows), giving MSI\* |
| `subcategory` | The direct-competition adjustment (2.7), giving MSI\* and the competitor count stage 2 uses |
| `location` | Stage 1 market rows. The barangay's **rent** feeds monthly fixed cost. Its **real PSA 2024 population** feeds market depth and the demand ceiling |
| `business_stage`, `registration_date` | `is_existing`, `years_in_operation` |
| **`capital`** (required) | `capital`, capital runway, capital adequacy |
| `employee_count` | Payroll, then fixed cost and runway; also staffing capacity |
| `product_offering` | `has_offering_description` |
| offering items (the price list) | `priced_item_count`, `average_price`, required daily sales; any item at all also sets `has_offering_description` |
| `innovation_idea` | `has_innovation_idea` (differentiation) |
| `business_name` | **Not used.** It identifies the plan and is not a business parameter. A model that scored a plan by its name would be a bug, and `test_the_business_name_is_not_a_business_parameter` checks that renaming a plan changes nothing. The narrator's prompt shows the name only as a label |

Capital is required (and must be above zero) on every plan form. The
form field is `capital`, and the database column keeps its old name,
`sme_profile.startup_capital`. A legacy plan saved before that rule
existed still gets a forecast, with capital 0, so its runway and capital
adequacy are 0. Home shows such a plan a "Capital missing" warning and
"Add your capital to get an accurate forecast", with the edit action.

### 3.2 Assumptions and constants

All of these live in `app/ml/constants.py`, where each is documented
next to its value. The first four are the **assumptions** the forecast
rests on.

| Assumption | Value | Why this value | Who can change it |
|---|---|---|---|
| Daily wage per employee | **₱590/day** (`DEFAULT_DAILY_WAGE_PHP`) | DOLE Wage Order No. RBIII-26, 2nd tranche, effective 16 Apr 2026: Tarlac retail and service establishments (₱600 for other non-agriculture). A minimum-wage floor: the one labour cost every plan is legally bound to | **Admin**, in **System Settings → Plan forecast assumptions** (`plan_daily_wage_php`, accepted ₱1–₱100,000) |
| Gross margin | **40%** (`DEFAULT_GROSS_MARGIN`) | The **assumed** share of each sale left after the cost of goods. It is not measured: no plan form collects costs | **Admin**, same panel (`plan_gross_margin`, accepted 0.05–0.95) |
| Operating days per month | **26** (`OPERATING_DAYS_PER_MONTH`) | 52 weeks × 6 days ÷ 12. DOLE's six-day equivalent-monthly factor is 313/12 = 26.08 | Code constant; retrain after changing it |
| Purchase ceiling | **once a week per resident** (`PURCHASES_PER_RESIDENT_PER_DAY` = 1/7) | Each resident a business can expect to serve buys from it at most once a week. Generous on purpose: a plan that needs more sales than this is out of reach | Code constant; retrain after changing it |

The remaining constants:

| Name | Value | What it is |
|---|---|---|
| `EXPERIENCE_FULL_YEARS` | 5 | Years after which operating experience scores full marks |
| `STAFF_FULL_CAPACITY` | 3 | Employees at which staffing capacity scores full marks |
| `MINIMUM_RAMP_MONTHS` | 3 | Ramp-up is never shorter than this. Shared with the ROI window |
| `MONTHLY_FIXED_COST_FLOOR_PHP` | ₱1,000 | Fixed cost is never treated as less than this, so runway can never divide by zero |
| `ITEM_PRICE_RANGE_PHP` | ₱0.01–₱10,000,000 | A price-list entry counts as a price point only inside this range. A free sample, a typo or "1e39" still marks the offering as described, but is left out of the average price |
| `PLAN_INPUT_CEILING` | 10¹² | A guard, not an assumption. Every stage-2 input is read as a finite number no larger than this, so the forest's float32 input can never overflow |

**Changing the wage or the margin.** An Admin's change applies to every
forecast made after it, with no retraining needed for the forecast to
run. A stored forecast keeps the values it was computed with (they are
recorded in its payload) until the plan is forecast again, and the
quarterly outlook drawn next to it holds those recorded values too, so
its Q1 bar still equals the gauge. Out-of-range or missing values fall
back to the defaults above.

How strongly the **trained forest** responds to each one is a different
matter, and it was measured over 5,000 random plans with price lists:

- **Wage.** It reaches the forest through monthly fixed cost and capital
  runway, two of its most-used features. Raising the wage from ₱590 to
  ₱700 lowered the scorecard by 0.58 points on average and the forest by
  0.33.
- **Margin.** It reaches the forest only through required daily sales,
  and every training plan used the same 40% margin. So the forest learned
  the effect of prices through `average_price` and almost never splits on
  `required_daily_sales` (importance 0.006). Halving the margin to 20%
  lowered the scorecard by 1.29 points on average and the forest by only
  0.11. A new margin is still reported correctly: required daily sales,
  the explanation and the scorecard all use it. But the Plan Viability
  Score barely moves. For the forest to weigh the margin, retrain it with
  margins sampled across the Admin range (`train_model._make_plan_dataset`
  holds the margin at the default today).

### 3.3 Derived quantities

These are computed in `plan_model.derive_quantities()`, the one function
both training and inference call.

```
Vm  = (100 - MSI*) / 10                       market-only viability, 0-10
SF  = clamp(1 + (MSI* - 50)/100, 0.6, 1.8)    saturation factor

residents_per_business  RPB = population / (competitor_count + 1)
monthly_payroll             = employees x daily_wage x 26
monthly_fixed_cost      MFC = max(1,000, rent + monthly_payroll)
capital_runway_months   RW  = capital / MFC
ramp_up_months          R   = max(3, (24 - 1.8 x Vm) x SF)
capital_adequacy            = min(1, RW / R)
required_daily_sales    RDS = MFC / (average_price x gross_margin x 26)   (0 when no prices)
daily_sales_ceiling     DSC = RPB x 1/7
```

- `competitor_count` is the direct-adjusted count when a sub-category
  adjusted the score. Otherwise it is the reconciled industry count. It
  is always the same count MSI\* was scored with, so the two figures
  describe the same market.
- `population` is the barangay's real PSA 2024 population. All 76
  Tarlac City barangays have one. Only a location outside that list (a
  free-typed one) falls back to the **median of the 76** (3,881 people).
  The median is used rather than the mean so that the four barangays
  above 17,000 do not inflate what counts as typical.
- `rent` is the `average_rent` of the market row the plan was scored on
  (the same figure stage 1 used). Failing that, the barangay's reference
  profile is used, then a neutral ₱15,000.
- `average_price` is the mean of the price-list entries inside
  ₱0.01–₱10,000,000, and `priced_item_count` is how many there are (the
  form accepts at most 30 items).
- **Ramp-up uses the same formula as the ROI window.**
  `ramp_midpoint_months()` lives in `plan_model.py`, and
  `location_opportunity_service.estimate_roi_timeframe()` calls it,
  with results identical to the last bit. Stage 2 does not apply the ROI
  card's market-depth nudge, because market depth is already its own
  feature here and would otherwise be counted twice.

### 3.4 The fourteen features (`constants.PLAN_FEATURE_NAMES`, in order)

| # | Feature | Source |
|---|---|---|
| 1 | `market_saturation` | MSI\* (0–100) |
| 2 | `residents_per_business` | RPB |
| 3 | `monthly_fixed_cost` | MFC (₱/month) |
| 4 | `capital` | ₱, as entered |
| 5 | `capital_runway_months` | RW |
| 6 | `ramp_up_months` | R |
| 7 | `employee_count` | as entered |
| 8 | `is_existing` | 0/1 from business stage |
| 9 | `years_in_operation` | from the registration date; 0 for a startup |
| 10 | `priced_item_count` | price-list items with a valid price (0–30) |
| 11 | `average_price` | ₱, mean of the valid prices; 0 when none |
| 12 | `required_daily_sales` | RDS; 0 when no prices |
| 13 | `has_offering_description` | 0/1: offering text **or** any price-list item |
| 14 | `has_innovation_idea` | 0/1 |

The trained bundle on disk records this list. A model trained on a
different order is **refused** and the formula stands in, because feeding
a forest its columns in the wrong order raises no error and simply gives
wrong answers.

### 3.5 The scorecard: the formula the model is trained on

The scorecard has seven components, each clamped to 0–1, grouped by the
four aspects a feasibility study covers. The weights are a **stated
editorial choice**. The market keeps the largest share because stage 1
is still the best-evidenced signal the system has. Capital comes next,
because running out of money before customers arrive is the most common
way a small business closes. The remaining components share the rest.

| Component | Aspect | Weight | Formula |
|---|---|---|---|
| C1 market opportunity | Market | 0.40 | `1 - MSI*/100` |
| C2 capital adequacy | Financial | 0.20 | `min(1, RW / R)` |
| C3 price coverage | Financial | 0.10 | 0.5 with no prices (unknown, so neutral); else `clamp(1 - RDS/DSC, 0, 1)` (0 if DSC ≤ 0) |
| C4 operating experience | Technical/Operational | 0.08 | 0 for a startup; existing: `min(1, 0.5 + 0.5 x years/5)` |
| C5 staffing capacity | Technical/Operational | 0.07 | `min(1, (employees + 1) / 4)`: the owner alone scores 0.25 |
| C6 offering defined | Product | 0.07 | 0 if nothing described; else `0.5 + 0.5 x min(1, priced_items/5)` |
| C7 differentiation | Product | 0.08 | 0 without an idea; else `0.4 + 0.6 x MSI*/100`, since an idea matters more the more crowded the market |

```
Scorecard index  S = 100 x sum( w_k x C_k )        0-100        (weights sum to 1.00)
```

Each component's **points** are `100 x w_k x C_k`, and the seven points
add up to S. Two trade-offs are intended. Staff raise C5 (capacity) and
also raise payroll, which lowers C2 (runway). Saturation lowers C1 and
raises the value of an idea in C7.

### 3.6 Training

**Code:** `train_model.train_plan_model()`, called by `train_and_save()`
after stage 1 finishes. It can also be run alone with
`python -m app.ml.train_model --plan-only`, which builds on the
`rf_model.pkl` already on disk. `seed.py` (and so the Render build)
trains whenever **either** model file is missing, and trains stage 2
alone when only `plan_model.pkl` is missing.

The training set is 6,000 synthetic plans (`N_PLAN_SAMPLES`), drawn with
their own seed (`PLAN_RANDOM_STATE = 4242`) so that a stage-2-only run
reproduces exactly what a full run trains. Each plan is built in four
steps:

1. **Market.** A barangay profile, an industry and a competitor count
   are drawn exactly as stage 1's sampler draws them, with the same
   ±10% jitter. The market is then scored by the **trained** stage-1
   forest (`rf.predict`, not the formula), so stage 2 learns from the
   saturation figures stage 1 actually produces. Stage 1 sees the plan's
   own years in operation, as it does for a real plan. 30% of plans also
   get a sub-category-style adjustment: competitor count × U(0.3, 1.6),
   rounded, and re-scored by the forest with years 0, as
   `saturation_for_counts` does at inference.
2. **Population** is the barangay's real PSA 2024 figure.
3. **Plan parameters:**
   - Capital: log-uniform ₱10,000–₱10,000,000.
   - Employees: 0–20, each count 20% less likely than the one below.
   - Stage: 30% existing businesses, with U(0, 15) years in operation.
   - Price list: none 40% of the time. Otherwise 1–30 priced items (each
     count 10% rarer than the one below) at a log-uniform ₱10–₱20,000
     average price.
   - Offering described: 70% of plans (always, when there is a price
     list).
   - Innovation idea: 50% of plans.
   - Wage and margin: the defaults, ₱590 and 40%.
4. **Label:** `PVI* = clip(S + N(0, 3), 0, 100)`, where S is the
   scorecard of 3.5 computed by the same `plan_model` functions inference
   uses.

**Model:** `RandomForestRegressor(n_estimators=100, max_depth=14,
min_samples_leaf=3, random_state=42)`, with an 80/20 split (4,800 train,
1,200 test). It is saved as a bundle (`plan_model.pkl`) that records the
feature order, the model version `plan_rf_v1` and the training size.

> **Honesty note: synthetic labels.** Like stage 1, stage 2 is trained
> on **synthetic** plans labelled by a **documented formula plus noise**,
> because no recorded outcomes of Tarlac City business plans exist yet.
> Its accuracy figures measure how well it recovers that formula from
> raw inputs. They do not measure how well it predicts which real
> businesses survive. The pipeline exists as code so that it can be
> re-run the moment real outcomes are collected (replace the label in
> `_make_plan_dataset` with the recorded outcome), at which point the
> numbers become real ones. Until then, quote them as what they are.

### 3.7 Metrics

From `app/ml/model_store/training_report.json`, under `"plan_model"`
(stage 1's keys are unchanged):

| Metric | Stage 2 (RF2) |
|---|---|
| Training / test rows | 4,800 / 1,200 |
| MAE (points, 0–100) | 3.0538 |
| RMSE | 3.8716 |
| R² | 0.9068 |
| "Accuracy" (100 − MAE) | 96.95% |
| Label noise (sd) | 3.0 |
| MAE the label noise alone causes (noisy label vs. noise-free S) | 2.384 |
| MAE against the **noise-free** scorecard | **1.9034** |

The last row is the honest measure of stage 2. It says the forest
recovers the documented scorecard from raw inputs to within about 1.9
points on average. Even a perfect copy of the formula would show an MAE
of about 2.4 against the noisy labels, so most of the 3.05 is the noise
put there on purpose.

The forest's impurity importances:

| Feature | Importance | Feature | Importance |
|---|---|---|---|
| capital_runway_months | 0.397 | has_innovation_idea | 0.020 |
| market_saturation | 0.191 | capital | 0.016 |
| ramp_up_months | 0.186 | monthly_fixed_cost | 0.012 |
| average_price | 0.083 | residents_per_business | 0.012 |
| years_in_operation | 0.053 | is_existing | 0.011 |
| priced_item_count | 0.007 | required_daily_sales | 0.006 |
| has_offering_description | 0.004 | employee_count | 0.002 |

Correlated features share credit in these figures, so a low one does not
mean an input is ignored. Employees, for example, mostly reach the model
through fixed cost and runway.

### 3.8 Inference: one plan's forecast

**Code:** `plan_forecast_service.forecast_plan(sme_profile, scores,
market_row, population, subcategory_analysis)`, called from
`forecasting_service.generate_forecast_for_profile()` right after stage 1
and the sub-category adjustment.

1. Build the raw inputs (`build_plan_inputs`), derive the quantities
   above, and build the feature row (`plan_feature_vector`).
2. **PVI** = RF2's prediction, clipped to 0–100. RF2 is loaded once per
   process under a lock and pinned to one thread, so the same plan
   always gets the same number to the last digit. The **Plan Viability
   Score** is PVI / 10, rounded to 1 dp. It is what
   `forecast_result.viability_score` stores.
3. **Plan confidence** uses stage 1's tree-spread mapping (2.4) on RF2's
   100 trees.
4. **Combined confidence** = `min(stage-1 confidence, stage-2
   confidence)`: a chained forecast is only as sure as its weaker half.
   This is what `forecast_result.confidence_level` stores.
5. **Stored model version:** `rf_v1+plan_rf_v1`. When either stage ran on
   its formula fallback, that is named instead (`fx_v1` / `plan_fx_v1`).
   `forecast_result.saturation_index` still stores MSI\*, so the early
   warning and the tier keep describing the market.

**No `plan_model.pkl` on disk:** PVI is the scorecard S itself, labelled
`plan_formula_v1`, with confidence 50 (the same convention as stage 1's
fallback). The explanation is then the scorecard's component points
(`100 x w_k x C_k`) from a baseline of 0, which also add up exactly.

**Guards against bad input.** Every input is read through
`plan_model.finite_input()`: NaN or unparseable values count as missing,
and ±infinity or anything beyond 10¹² is capped. Prices outside
₱0.01–₱10,000,000 are not price points. An Admin wage outside ₱1–₱100,000
is refused when it is saved and ignored when it is read. Before
`predict()`, the service also checks that the feature row fits in
float32. If it does not, the plan gets the labelled formula and a log
line, never a 500 error.

### 3.9 Explaining one forecast: exact path decomposition

A random forest's prediction is the average, over its trees, of the leaf
value each tree lands in. Walk one tree from root to leaf. The root holds
the training mean, and each split moves the running value from the
parent node's mean to the child's. Crediting each move to the feature
that split tested gives, per tree:

```
leaf value = root value + sum over splits (child value - parent value)
```

This is a telescoping sum, so it is **exact**. Averaging over the trees:

```
PVI = baseline + sum over the 14 features of contribution_f
      baseline = mean of the trees' root values (the training-set mean)
```

This is the Saabas ("treeinterpreter") method, implemented in
`plan_model.path_contributions()` with numpy over each tree's
`decision_path`, `tree_.value` and `tree_.feature`. It is not an
approximation, and it describes **this** forest's actual decision paths,
not a separate story about them. If the sum ever misses the prediction
by more than 10⁻⁶ points, the explanation is refused
(`DecompositionError`) rather than shown.

The 14 contributions are summed into 8 plain-language **drivers**
(`plan_model.FEATURE_GROUPS`, the one place the mapping is defined):

| Driver | Features |
|---|---|
| Market saturation (industry · sub-category · location) | `market_saturation` |
| Market depth (residents per business) | `residents_per_business` |
| Capital vs. running costs | `capital`, `monthly_fixed_cost`, `capital_runway_months`, `ramp_up_months` |
| Pricing (price list) | `average_price`, `required_daily_sales`, `priced_item_count` |
| Business stage & experience | `is_existing`, `years_in_operation` |
| Staffing | `employee_count` |
| Offering described | `has_offering_description` |
| Differentiation (your idea) | `has_innovation_idea` |

The baseline is rounded on its own to 1 dp. The drivers are rounded to
1 dp by the largest-remainder method (`round_preserving_sum`), so that
`baseline + drivers` equals the displayed PVI exactly. Each driver moves
by at most 0.1 from its own rounding. Drivers are listed largest
|points| first.

**How to read a driver.** A driver's points are measured against the
**baseline**: the average synthetic plan the forest learned from, not
against zero or against "good". A startup therefore shows a negative
"Business stage & experience" (the average training plan includes 30%
existing businesses), and a plan with more capital than the average
training plan shows a positive "Capital vs. running costs" even when its
runway is shorter than its ramp-up. Credit also goes to the feature the
forest **split on**, which among correlated features may not be the one
the formula names. In the worked example (section 5), the price list's
effect lands almost entirely on `average_price`, not on
`required_daily_sales`.

### 3.10 Break-even window

```
break_even = estimate_roi_timeframe(PVI / 10, MSI*, None, None)

midpoint = (24 - 1.8 x PVI/10) x SF          SF as in 3.3, from MSI*
low      = max(3, round(0.8 x midpoint))
high     = max(low + 1, round(1.25 x midpoint))
label    = "<low>-<high> months"
```

This is the ROI window's formula from the Recommendations page, driven
by the **plan's** viability instead of the market's. That is how
capital, staffing and pricing reach it. It is fed the model's
**unrounded** PVI / 10 and unrounded MSI\*. The window's ends are whole
months, so feeding it the twice-rounded display score used to move one
end by a month for about 8% of plans. The window therefore cannot always
be recomputed exactly from the displayed 1-dp figures.

### 3.11 Quarterly outlook

**Code:** `trend_analytics_service.project_quarterly_outlook(...,
sme_profile=plan)`, the Home page's "Current vs. Projected Demand &
Viability" chart. Each of the four quarters is a real run of **both**
models:

1. **Only the competitor count is projected.** It grows along this
   barangay's own recorded trend when its `market_data` history spans at
   least 60 days (clamped to ±15% a quarter), or else along the PSA/DTI
   national MSME establishment series (published 2019–2023, continued at
   3% a year): `projected = round(today's count x growth)`. Every other
   market input is held at today's value, and the chart's caption says
   so.
2. **Stage 1** runs on that quarter's projected count, with the plan's
   own years in operation. When the plan's sub-category adjusted its
   competition, the same ratio (adjusted count / industry count) is
   applied to the projected count and re-scored with years 0, as in 2.7.
3. **Stage 2** runs on that quarter's MSI\* and competitor count with
   every plan input held as the owner entered it
   (`plan_forecast_service.plan_viability_for`). It also holds the wage
   and margin the stored forecast used, not today's settings.
4. **Confidence** = min(stage 1, stage 2) − 8 points per quarter ahead
   (`HORIZON_CONFIDENCE_PENALTY`), because projecting the inputs adds
   uncertainty the trees cannot see.

Q1 is today's market, so it reproduces the stored viability. The chart
draws saturation × 0.6 and viability × 6 on its 0–60 axis.

### 3.12 How far to trust one direction of change

The scorecard is monotone where it should be: more capital never lowers
it, and a more saturated market never raises it. The forest only
approximates the scorecard (3.7), so a single change can move it
slightly the wrong way. This was **measured**, not assumed, over 100,000
random realistic plans (five seeds of 20,000), doubling the capital on
each:

- The forest's PVI went **down** for 17.7% of plans (about one in six),
  almost always by under half a point.
- It dropped by more than 1 point for **0.43%** of plans, and by more
  than 2 points for **0.03%** (3 in 10,000).
- The worst drop seen was **3.6 points**, which is 0.36 on the 0–10
  score.
- 90% of the over-a-point drops are plans whose capital **already covers
  the ramp-up**. There the scorecard is flat, and the forest has only
  label noise to fit. Where the scorecard rises, the forest fell by more
  than a point for 0.07% of plans.
- The largest drops were all thin-capital plans (2–4 months of runway
  against a ~14-month ramp-up); the two largest had been over-scored by
  the forest by 6–8 points to begin with.
- Averaged over plans, doubling capital raised the forest's PVI by 2.29
  points and the scorecard by 2.33.

`tests/test_plan_forecast_model.py` checks these directions twice: on
fixed plans, with a 1-point tolerance for the forest and none for the
formula, and on a random population, with the tolerance above.

---

## 4. The forecast payload

The payload is stored inside the recommendation JSON under `"forecast"`,
and the Home page, the Recommendations page and the narrator all read
it. **Every number in it comes from the models and the plan's own
inputs. None comes from the LLM.** All values are plain JSON numbers,
strings and booleans.

```
version     "plan_v1"
market      saturation_index (MSI*), industry_saturation_index (MSI), cluster_label,
            competitor_count (the count MSI* was scored with), confidence, model_version
plan        viability_index (PVI), viability_score (PVI/10), confidence (stage 2),
            model_version, scorecard_index (S)
inputs      capital, employee_count, business_stage, years_in_operation,
            priced_item_count, average_price, has_offering_description,
            has_innovation_idea, industry_type, subcategory_label, location,
            population, residents_per_business
financials  monthly_rent, daily_wage, monthly_payroll, monthly_fixed_cost,
            capital_runway_months, ramp_up_months, capital_adequacy,
            gross_margin, operating_days, required_daily_sales,
            daily_sales_ceiling, break_even{low_months, high_months, label}
components  the seven scorecard rows {key, label, aspect, score, weight, points,
            inputs}, heaviest weight first
baseline    the forest's baseline (0 on the formula path)
drivers     the eight drivers {key, label, points}, largest |points| first
```

Section 5 shows a complete payload with real values.

---

## 5. Worked example (computed by the real code)

A bakery plan in **Tibag**, run end to end through
`generate_forecast_for_profile()` on a fresh in-memory database, with the
trained models in `model_store/` and the AI switch off (so the rule-based
narration is shown; Gemini's wording varies from run to run).

### 5.1 The plan, and the market figures fixed for the example

| Input | Value |
|---|---|
| Business name | Panaderia sa Tibag (not an input) |
| Industry / sub-category | Food and Beverage / Bakery / Pastries |
| Location | Tibag (real PSA 2024 population **17,936**) |
| Capital | **₱500,000** |
| Employees | **3** |
| Stage | startup (years in operation 0) |
| Product offering | "Freshly baked pandesal, Filipino breads and brewed coffee" |
| Price list | Pandesal (10 pcs) ₱30, Spanish bread (5 pcs) ₱40, Ensaymada ₱35, Ube-cheese pandesal (6 pcs) ₱60, Brewed coffee ₱60: 5 priced items, average **₱45** |
| Innovation idea | "Ube-cheese pandesal delivered warm before 6 a.m. on a weekly subscription" |
| Wage / margin | ₱590 / 40% (the defaults) |

Live competitor counts depend on Google Places and the LGU permit
register at the time of running, so the example fixes them: a
`market_data` row with **14** Food and Beverage businesses in Tibag, and
**1** bakery measured in the permit register. The rest of the market row
is Tibag's reference profile: population density 2,320, foot traffic 31,
rent ₱18,000 a month, historical success rate 0.71, and business density
2.9 (the LGU placeholder row).

### 5.2 Stage 1

```
feature vector = [14, 2320.0, 31, 18000, 0.71, 2.9, 0, 9]
                  (competitors, density, foot traffic, rent, success rate,
                   business density, years, industry code for Food and Beverage)
RF1 prediction  MSI = 41.38            tier Moderate
tree spread     sd = 3.0463   ->  confidence = 100 - 3.0463/25 x 60 = 92.69
```

### 5.3 Sub-category adjustment

```
expected share  = 1/9 = 0.111       (even split: no barangay has 3+ measured bakery counts;
                                     Food and Beverage has 9 countable sub-categories)
expected direct = 14 x 1/9 = 1.556 bakeries
ratio           = 1 / 1.556 = 0.643  (inside 0.25-2.5; shown as 0.64)
adjusted count  = round(14 x 0.643) = 9
RF1 on [9, 2320.0, 31, 18000, 0.71, 2.9, 0, 9]  ->  MSI* = 35.35   tier Moderate
```

One bakery where about 1.6 would be expected makes the direct
competition lighter than the industry figure, so saturation falls from
41.4% to 35.4%. The market confidence stays 92.69.

### 5.4 Stage 2: derived quantities

```
Vm  = (100 - 35.35)/10                       = 6.465
SF  = 1 + (35.35 - 50)/100                   = 0.8535
RPB = 17,936 / (9 + 1)                       = 1,793.6 residents per business
payroll = 3 x 590 x 26                       = ₱46,020 / month
MFC = 18,000 + 46,020                        = ₱64,020 / month
RW  = 500,000 / 64,020                       = 7.810 months
R   = max(3, (24 - 1.8 x 6.465) x 0.8535)    = 10.552 months
capital adequacy = min(1, 7.810 / 10.552)    = 0.7402
RDS = 64,020 / (45 x 0.40 x 26)              = 136.79 sales a day
DSC = 1,793.6 x 1/7                          = 256.23 sales a day (ceiling)
```

The capital carries the fixed costs for 7.8 months, shorter than the
10.6 months the business is expected to need before it pays for itself.

### 5.5 The scorecard

| Component | Formula with this plan's numbers | Score | × weight | Points |
|---|---|---|---|---|
| C1 Market opportunity | 1 − 0.3535 | 0.6465 | 0.40 | 25.86 |
| C2 Capital adequacy | min(1, 7.810 / 10.552) | 0.7402 | 0.20 | 14.80 |
| C3 Price coverage | 1 − 136.79 / 256.23 | 0.4661 | 0.10 | 4.66 |
| C4 Operating experience | startup | 0 | 0.08 | 0.00 |
| C5 Staffing | min(1, (3 + 1)/4) | 1 | 0.07 | 7.00 |
| C6 Offering defined | 0.5 + 0.5 × min(1, 5/5) | 1 | 0.07 | 7.00 |
| C7 Differentiation | 0.4 + 0.6 × 0.3535 | 0.6121 | 0.08 | 4.90 |
| **S** | | | | **64.22** |

### 5.6 The trained forest's prediction

The fourteen-feature row RF2 receives:

```
[35.35, 1793.6, 64020, 500000, 7.810059, 10.55182, 3, 0, 0, 5, 45.0, 136.794872, 1, 1]
```

- **PVI = 60.0804**, about 4 points below the scorecard's 64.22 (section
  5.7 shows why).
- **Plan Viability Score = 6.0 / 10** (PVI 60.1 at 1 dp).
- Tree spread sd = 5.5826, so plan confidence = 100 − 5.5826/25 × 60 =
  **86.6**.
- Combined confidence = min(92.69, 86.60) = **86.6**.

### 5.7 Path decomposition

Baseline (the training mean) **62.1043**. Per feature, exact to 4 dp,
then grouped:

| Driver | Feature contributions | Exact | Shown |
|---|---|---|---|
| Capital vs. running costs | runway +5.0365, ramp-up −2.0136, fixed cost +0.6732, capital −0.2227 | +3.4735 | +3.5 |
| Pricing (price list) | average price −3.1954, required sales −0.0316, items −0.0125 | −3.2394 | −3.2 |
| Market saturation | market_saturation −2.4839 | −2.4839 | −2.5 |
| Business stage & experience | years −1.9387, is_existing −0.1295 | −2.0682 | −2.1 |
| Differentiation (your idea) | has_innovation_idea +1.4663 | +1.4663 | +1.5 |
| Market depth | residents_per_business +0.4201 | +0.4201 | +0.4 |
| Offering described | has_offering_description +0.2775 | +0.2775 | +0.3 |
| Staffing | employee_count +0.1302 | +0.1302 | +0.1 |
| **Total** | | 62.1043 − 2.0241 = **60.0804** | 62.1 − 2.0 = **60.1** |

`baseline + Σ contributions` misses the forest's own `predict()` by
2.1 × 10⁻¹⁴ points, far inside the 10⁻⁶ the code demands.

**Why the forest (60.1) sits below the scorecard (64.2) here.** Both
can be measured from the same starting point, the average synthetic
training plan. For the scorecard that is each component's points for
this plan minus its average over the 6,000 training plans (mean S =
61.89). For the forest it is the contributions above (baseline 62.10).

| Driver | Scorecard, this plan − average plan | Forest contribution |
|---|---|---|
| Market saturation | −3.61 | −2.48 |
| Market depth | (inside price coverage) | +0.42 |
| Capital vs. running costs | +2.56 | +3.47 |
| Pricing | −1.17 | −3.24 |
| Business stage & experience | −2.19 | −2.07 |
| Staffing | +1.88 | +0.13 |
| Offering described | +2.21 | +0.28 |
| Differentiation | +2.64 | +1.47 |
| **Total** | **+2.32** (61.89 → 64.22) | **−2.02** (62.10 → 60.08) |

The two agree on the direction of every driver, and closely on market,
capital and experience. The forest marks the low ₱45 average price down
harder: it needs 136.8 sales a day, over half of the barangay's ceiling.
It credits that to `average_price` rather than `required_daily_sales`
(see "How to read a driver" in 3.9). It also gives less credit than the
formula to the full staff, the described offering and the idea. This
plan lands 4.1 points below its formula, about twice the forest's
average error against the formula (1.9, section 3.7). That is the kind
of deviation section 3.12 measures.

### 5.8 Break-even window

```
midpoint = (24 - 1.8 x 6.00804) x 0.8535 = 11.2538 months
low  = max(3, round(0.8 x 11.2538) = round(9.0031)) = 9
high = max(10, round(1.25 x 11.2538) = round(14.0673)) = 14
-> "9-14 months"
```

### 5.9 What is stored

`forecast_result`: viability_score **6.00**, saturation_index **35.35**,
confidence_level **86.60**, model_version **`rf_v1+plan_rf_v1`**. The
payload stored with it, under `"forecast"`:

```json
{
 "version": "plan_v1",
 "market": {"saturation_index": 35.4, "industry_saturation_index": 41.4, "cluster_label": "Moderate",
            "competitor_count": 9, "confidence": 92.7, "model_version": "rf_v1"},
 "plan": {"viability_index": 60.1, "viability_score": 6.0, "confidence": 86.6,
          "model_version": "plan_rf_v1", "scorecard_index": 64.2},
 "inputs": {"capital": 500000.0, "employee_count": 3, "business_stage": "startup",
            "years_in_operation": 0.0, "priced_item_count": 5, "average_price": 45.0,
            "has_offering_description": true, "has_innovation_idea": true,
            "industry_type": "Food and Beverage", "subcategory_label": "Bakery / Pastries",
            "location": "Tibag", "population": 17936, "residents_per_business": 1793.6},
 "financials": {"monthly_rent": 18000.0, "daily_wage": 590.0, "monthly_payroll": 46020.0,
                "monthly_fixed_cost": 64020.0, "capital_runway_months": 7.8, "ramp_up_months": 10.6,
                "capital_adequacy": 0.74, "gross_margin": 0.4, "operating_days": 26,
                "required_daily_sales": 136.8, "daily_sales_ceiling": 256.2,
                "break_even": {"low_months": 9, "high_months": 14, "label": "9-14 months"}},
 "components": [
  {"key": "market_opportunity", "label": "Market opportunity", "aspect": "Market", "score": 0.65, "weight": 0.4, "points": 25.9, "inputs": "industry, sub-category, location"},
  {"key": "capital_adequacy", "label": "Capital adequacy", "aspect": "Financial", "score": 0.74, "weight": 0.2, "points": 14.8, "inputs": "capital, employees, location (rent)"},
  {"key": "price_coverage", "label": "Price coverage", "aspect": "Financial", "score": 0.47, "weight": 0.1, "points": 4.7, "inputs": "price list, location (population, competitors)"},
  {"key": "operating_experience", "label": "Operating experience", "aspect": "Technical/Operational", "score": 0.0, "weight": 0.08, "points": 0.0, "inputs": "business stage, registration date"},
  {"key": "differentiation", "label": "Differentiation", "aspect": "Product", "score": 0.61, "weight": 0.08, "points": 4.9, "inputs": "innovation idea, market saturation"},
  {"key": "staffing", "label": "Staffing capacity", "aspect": "Technical/Operational", "score": 1.0, "weight": 0.07, "points": 7.0, "inputs": "employees"},
  {"key": "offering_definition", "label": "Offering defined", "aspect": "Product", "score": 1.0, "weight": 0.07, "points": 7.0, "inputs": "product offering, price list"}
 ],
 "baseline": 62.1,
 "drivers": [
  {"key": "capital", "label": "Capital vs. running costs", "points": 3.5},
  {"key": "pricing", "label": "Pricing (price list)", "points": -3.2},
  {"key": "market", "label": "Market saturation (industry · sub-category · location)", "points": -2.5},
  {"key": "experience", "label": "Business stage & experience", "points": -2.1},
  {"key": "differentiation", "label": "Differentiation (your idea)", "points": 1.5},
  {"key": "depth", "label": "Market depth (residents per business)", "points": 0.4},
  {"key": "offering", "label": "Offering described", "points": 0.3},
  {"key": "staffing", "label": "Staffing", "points": 0.1}
 ]
}
```

### 5.10 The explanation

The rule-based explanation stored with it (`generated_by: "rule-based"`),
exactly as the code wrote it:

> The trained plan model rates this plan's viability 6/10 with 86.6%
> confidence. The market model reads Tibag as 35.4% saturated for this
> kind of business (the 'Moderate' tier), against 41.4% for the industry
> as a whole. Starting from the model's baseline of 62.1 points, capital
> vs. running costs added 3.5 points, pricing (price list) took off 3.2
> points and market saturation (industry · sub-category · location) took
> off 2.5 points. Fixed costs come to ₱64,020/month (rent ₱18,000 plus 3
> employees × ₱590/day × 26 days), so your capital of ₱500,000 covers
> about 7.8 months -- shorter than the ~10.6-month ramp-up the model
> expects. At your average price of ₱45 and a 40% gross margin, covering
> those costs takes about 136.8 sales a day, within the ~256.2 a day this
> barangay's residents could plausibly support. Taken together, the
> model's break-even window for this plan is 9-14 months.

The same payload produces these rule-based risks, among others: the
capital covers "only about 7.8 months of fixed costs (₱64,020/month),
shorter than the ~10.6-month ramp-up the model expects -- more capital,
fewer staff at the start, or a cheaper site would close the gap", and
136.8 sales a day is "more than half of the ~256.2 a day this barangay's
residents could plausibly support, so there is little slack".

With the AI switch on, this is the TRAINED MODEL OUTPUT block Gemini
receives for the same plan, as `llm_service._model_output_prompt` builds
it:

```
TRAINED MODEL OUTPUT -- the forecast itself. Every figure below was produced by the models
this system trained; they are final. Do not recompute, re-round or change them:
  Market model (Random Forest, trained): Market Saturation Index 35.4% (tier: Moderate);
    industry-wide 41.4%; competitors counted: 9; confidence 92.7%
  Plan model (Random Forest, trained): Plan Viability Index 60.1 out of 100, shown to the
    owner as a viability score of 6/10; confidence 86.6%
  How the plan model reached 60.1: it starts from a baseline of 62.1 and each group of
    inputs adds or removes points:
    - Capital vs. running costs: +3.5 points
    - Pricing (price list): -3.2 points
    - Market saturation (industry · sub-category · location): -2.5 points
    - Business stage & experience: -2.1 points
    - Differentiation (your idea): +1.5 points
    - Market depth (residents per business): +0.4 points
    - Offering described: +0.3 points
    - Staffing: +0.1 points
  Money and demand figures the plan model used:
    - Capital: PHP 500,000
    - Monthly fixed cost: PHP 64,020 = rent PHP 18,000 + payroll PHP 46,020
      (3 employee(s) x PHP 590/day x 26 days)
    - Capital runway: 7.8 months of fixed costs; expected ramp-up before steady sales: 10.6 months
    - Average price PHP 45 with a 40% gross margin: about 136.8 sales a day are needed to
      cover fixed costs, against a ceiling of about 256.2 sales a day the barangay's
      residents can plausibly support
    - Break-even window: 9-14 months
```

(Long lines are wrapped here for reading. The prompt then asks for the
`explanation` key; see section 6.)

### 5.11 The grounding check on this plan

`recommendation_service.ungrounded_numbers(text, context)` on four
candidate sentences, with this plan's context:

| Candidate LLM sentence | Numbers not in the model output | Verdict |
|---|---|---|
| "The model rates this plan 6/10 with 86.6% confidence. Your capital of PHP 500,000 covers 7.8 months of fixed costs, shorter than the 10.6-month ramp-up, so break-even is 9-14 months." | none | kept |
| "The model rates this plan 6/10, and you should break even in 7 months." | `7` | whole explanation discarded |
| "You need about 150 sales a day to cover costs." | `150` | whole explanation discarded |
| "Your capital covers about eight months of costs." | none ("eight" is 7.8 rounded) | kept |

### 5.12 Quarterly outlook for this plan

| | Q1 | Q2 | Q3 | Q4 |
|---|---|---|---|---|
| Projected Food and Beverage businesses | 14 | 14 | 14 | 14 |
| Plan viability (0–10) | 6.0 | 6.0 | 6.0 | 6.0 |
| Confidence | 86.6 | 78.6 | 70.6 | 62.6 |

The single `market_data` row gives no local trend, so the national series
applies. Growth of 3% a year leaves 14 businesses at 14 when rounded in
each of the next three quarters, so the line is flat here, and Q1
equals the stored 6.0. Only the confidence falls, by the 8-point
horizon penalty per quarter. (The projection depends on the date it is
run.)

### 5.13 The same plan without a trained plan model

With no `plan_model.pkl`, the scorecard stands in: PVI = S = **64.2**,
score **6.4 / 10**, confidence 50, model `plan_formula_v1`, baseline 0,
and drivers equal to the component points (market 25.8, capital 14.8,
staffing 7.0, offering 7.0, differentiation 4.9, pricing 4.7). The
break-even window is then **8-13 months**. For comparison, the market
alone would score (100 − 35.35)/10 = **6.5**. The plan model scores this
plan lower, mostly because 7.8 months of runway does not cover the 10.6
months of ramp-up, and the price list leaves little slack against local
demand.

### 5.14 Reproducing it

Save this as a file in the repository root and run it with the
project's Python (`venv/Scripts/python.exe`). It prints the stored row,
the payload above and the explanation.

```python
import json, os
from datetime import date
os.environ["USE_LLM_RECOMMENDATIONS"] = "false"          # rule-based narration; no API call

from app import create_app
from app.extensions import db
from app.ml.seed_data import get_barangay_profile
from app.models import MarketData, SmeProfile, SubcategoryMarketData, SystemSetting, User
from app.services.forecasting_service import generate_forecast_for_profile
from app.services.recommendation_service import parse_recommendation

app = create_app("testing")                              # in-memory SQLite
app.config.update(PLACES_LIVE_FETCH=False, GOOGLE_PLACES_API_KEY="")
with app.app_context():
    db.create_all(); SystemSetting.ensure_defaults()
    owner = User(name="Example", email="example@example.test", role="SME")
    owner.set_password("example-only"); db.session.add(owner)
    tibag = get_barangay_profile("Tibag")
    db.session.add(MarketData(industry_type="Food and Beverage", location="Tibag", competitor_count=14,
                              population_density=tibag["population_density"],
                              foot_traffic_index=tibag["foot_traffic_index"],
                              average_rent=tibag["average_rent"],
                              historical_success_rate=tibag["historical_success_rate"],
                              source="DTI", date_recorded=date.today()))
    db.session.add(SubcategoryMarketData(industry_type="Food and Beverage", subcategory="bakery",
                                         location="Tibag", competitor_count=1, source="DTI",
                                         date_recorded=date.today()))
    db.session.commit()
    plan = SmeProfile(user_id=owner.user_id, business_name="Panaderia sa Tibag",
                      industry_type="Food and Beverage", subcategory="bakery", location="Tibag",
                      startup_capital=500000, employee_count=3, business_stage="startup",
                      product_offering="Freshly baked pandesal, Filipino breads and brewed coffee",
                      innovation_idea="Ube-cheese pandesal delivered warm before 6 a.m. on a weekly subscription",
                      offering_details=json.dumps([{"item": "Pandesal (10 pcs)", "price": 30},
                                                   {"item": "Spanish bread (5 pcs)", "price": 40},
                                                   {"item": "Ensaymada", "price": 35},
                                                   {"item": "Ube-cheese pandesal (6 pcs)", "price": 60},
                                                   {"item": "Brewed coffee", "price": 60}]))
    db.session.add(plan); db.session.commit()
    row = generate_forecast_for_profile(plan)
    stored = parse_recommendation(row.recommendation)
    print(row.viability_score, row.saturation_index, row.confidence_level, row.model_version)
    print(json.dumps(stored["forecast"], indent=1, ensure_ascii=False))
    print(stored["explanation"]["text"])
```

A retrained forest built from changed code or data gives different
forest figures (5.6, 5.7, and the window and outlook that follow from
them). The derived quantities and the scorecard (5.4, 5.5) stay the same.

---

## 6. How Gemini transcribes the result

**Code:** `llm_service.generate_recommendation_json()` (the prompt and
the provider order) and `recommendation_service.build_recommendation()`
(`_rule_based_explanation`, `ungrounded_numbers`, `_choose_explanation`).

The model computes the forecast. The LLM only puts it into words, and
its words are checked before they are kept.

1. **The rule-based explanation is always computed first.** It is
   deterministic, quotes only payload numbers, and runs to 4–6 sentences:
   - the plan's viability and its confidence (and the overall, lower,
     confidence when the market stage is the less certain one);
   - the market saturation, its tier, and the industry-wide figure when
     a sub-category moved it;
   - the three largest drivers, with their signs;
   - fixed cost split into rent and payroll, and the capital runway
     against the ramp-up;
   - required daily sales against the ceiling, when there is a price
     list;
   - the break-even window.

   It is what the owner sees whenever the LLM is switched off, fails, or
   fails the check in step 5. Section 5.10 shows one.
2. **The prompt carries the model output.** The prompt holds the plan's
   own parameters (name, industry, sub-category, location, stage, capital,
   employees, offering, price list, idea), the market comparison
   (population, competitor count and sample) and a **TRAINED MODEL
   OUTPUT** block. That block lists the market stage, the plan stage, the
   baseline and the drivers with signed points, and the money figures:
   capital, the fixed-cost breakdown, runway against ramp-up, average
   price, margin and required sales against the ceiling, and the
   break-even window. Section 5.10 shows the block for the worked
   example. A stage that ran on its formula fallback is labelled as a
   formula, and the LLM is told to say so. The LLM is asked to return,
   besides the usual headline, summary, reasons and risks, an
   `explanation` of 3–5 plain sentences that transcribe what the trained
   model forecast and why. It must use only figures from the block,
   copied exactly as written, and never change, recompute, combine or
   invent one. The prompt also says that an explanation with any other
   figure is discarded.
3. **Gemini goes first for this call.** Forecast narration tries Gemini
   first, then the provider set in `LLM_PROVIDER`, then the remaining one
   (`_provider_order(prefer="gemini")`). Every other AI text in the app
   (alerts, location cards) keeps the configured provider first. Gemini
   is passed over without a call in two cases, and the skip is recorded
   for the Admin's LLM diagnostics:
   - The key Gemini would be sent is an `sk-` key (OpenAI or OpenRouter
     format). `GEMINI_API_KEY` falls back to `OPENAI_API_KEY` in
     `app/config.py`, so this happens on an OpenRouter-only deployment,
     and Google would refuse the key.
   - There is no Gemini key at all, and Gemini was only first because the
     narration prefers it.

   The skip is recorded as `skipped` when Gemini was moved ahead of the
   configured provider, so that it never hides that provider's own
   failure. It is recorded as `no_client` when Gemini is the configured
   provider.
4. **The forecast is never the LLM's.** Whatever the LLM returns, the
   stored `forecast` is the payload `forecast_plan()` computed. The LLM
   supplies words only.
5. **Grounding check** (`ungrounded_numbers`). Every number in the LLM's
   explanation is matched against the numbers the prompt actually showed
   it: the plan's inputs, the market comparison and the payload. Numbers
   in digits are read ("8.2", "64,020", "500k", "₱3.4m", "1.5 million")
   and so are numbers in words ("six months", "two million").
   Denominators such as "/10" and "out of 100" are not figures, and
   neither is "one" used as a pronoun. A payload figure the prompt never
   prints does not count as grounding: the scorecard components, the
   scorecard index, capital adequacy, residents per business, the
   sub-category analysis's internals (except the direct-competitor count),
   and the pricing figures on a plan with no price list. A number matches
   when it is:
   - within ±0.6, or ±2% relative, of a shown number;
   - the viability on its other scale (the 0–10 score ×10 or the 0–100
     index ÷10), and only the viability;
   - the gross margin as a percentage ("40%"), only when written with a
     percent sign and only when the pricing line was shown.

   **An integer of 12 or less** written without decimals or a magnitude
   ("3 employees", "eight months") gets none of that slack. It must be
   within ±0.5 of a number the context holds, because small integers are
   easy to hit by accident.

   **One unmatched number discards the whole LLM explanation** in favour
   of the rule-based one, and the discard is logged at info level. An
   explanation longer than 1,500 characters is discarded the same way.
   The LLM's headline, summary, reasons and risks are still used.
   Section 5.11 shows the check on real sentences.
6. **Provenance is labelled.** The stored explanation carries
   `generated_by`: `llm:gemini:<model id>`, another `llm:<provider>:<model
   id>`, or `rule-based`. The pages show it as "Explained by Gemini",
   "AI-written" or "Rule-based explanation".
7. **Admin switch.** **System Settings → AI-Generated Recommendation
   Text** turns the LLM off entirely. Everything, including the
   explanation, is then rule-based. When the `USE_LLM_RECOMMENDATIONS`
   environment variable is set, it takes precedence over the switch.

The forecast numbers are the same whichever path writes the words.

---

## 7. Where the forecast appears

- **Home → Forecast & Recommendations** shows:
  - the gauge (Plan viability);
  - market saturation and tier;
  - plan viability and confidence;
  - break-even window;
  - capital runway ("N months of fixed costs (₱Y/mo)");
  - a "How this forecast was computed" panel with the explanation and
    its provenance badge, the baseline, the driver bars (signed points,
    green up, red down, with the numbers in text), and the fixed-cost
    breakdown.

  A forecast stored before stage 2 existed (no `forecast` key) is
  regenerated once when Home opens.
- **Home → quarterly outlook** (section 3.11).
- **Home → your business plans.** Each plan's chip shows its Plan
  Viability ("Viability X/10"), and "Capital missing" for a legacy plan
  with no capital.
- **Recommendations → the plan's card** shows Capital, Break-even (from
  the payload), Population, Competition level and the explanation.
- **Recommendations → location cards** are **stage 1 only**: a
  market-only score, the same for every plan in that industry and
  barangay. Each card says so and points to the plan's own forecast for
  the figure that weighs capital, staff and prices.

---

## 8. What this model is not

- **Not a measurement of real survival.** Both stages are trained on
  documented formulas plus noise (sections 2.2 and 3.6). Retrain them on
  recorded outcomes before quoting their accuracy as real-world accuracy.
- **Not a cash-flow statement.** Payroll is a minimum-wage floor, the
  gross margin is an assumption, and revenue is not modelled month by
  month. Runway and required daily sales are yardsticks for comparing
  plans, not a budget.
- **Not sensitive to every assumption equally.** The trained forest
  barely responds to the Admin's gross margin (section 3.2), because it
  was trained on one margin.
- **Not a judgement on the business name.** The name is never an input.
- **Not exact in every direction.** See section 3.12 for how far the
  trained forest can wobble against its own formula, and where.
