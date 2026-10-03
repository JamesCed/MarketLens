# Reference/FORECAST_MODEL.md

How a business plan's forecast is computed, and how its output becomes
the numbers and words the owner sees. This covers both trained models,
every input and the formula it goes through, how the models were trained
and how well, how the result is explained, and how Gemini writes the
**forecast transcript** from the models' computation without changing a
figure.

The code is the authority. This file describes `app/ml/plan_model.py`,
`app/ml/constants.py`, `app/ml/train_model.py`,
`app/services/plan_forecast_service.py`,
`app/services/forecasting_service.py`,
`app/services/subcategory_service.py`,
`app/services/recommendation_service.py`,
`app/services/llm_service.py`, the transcript endpoint in
`app/controllers/api_controller.py` and
`app/static/js/forecast_transcript.js`. If this file and the code ever
disagree, the code is what runs and this file is the bug.

**How the numbers here were checked (2 October 2026, after the stage-2
retrain that samples the wage and margin).** The metrics in section 3.7
are copied from `app/ml/model_store/training_report.json`, for the
`plan_model.pkl` whose training-config fingerprint begins `66939a8aa1fc`
(`train_model.plan_model_staleness()` reports it current). Stage 1 was
not retrained: `rf_model.pkl` is byte-identical to the one before, and
so are its predictions. Every number in the worked example (section 5)
was printed by running the real pipeline
(`generate_forecast_for_profile`) against that model, not computed by
hand. The sensitivity figures in 3.2 and the direction checks in 3.12
were re-measured on the same model.

**Contents**

1. The pipeline in one picture
2. Stage 1: Market Saturation Index
3. Stage 2: the Plan Viability Model
4. The forecast payload
5. Worked example, computed by the real code
6. How Gemini writes the forecast transcript
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
 FORECAST TRANSCRIPT (recommendation_service.build_recommendation;
                      stored under the key "explanation")
   a rule-based MODEL SUMMARY is ALWAYS computed from the payload;
   when the AI switch is on: Gemini first, then the configured LLM, then the other
   -> the one-call prompt asks for the transcript alongside the headline,
      reasons and risks; grounding check against the figures it was shown
   -> missing or rejected: the DEDICATED transcript call (llm_service.
      transcribe_forecast) is handed the whole computation; a draft with an
      unverifiable figure is sent back once with feedback, then its offending
      sentences are pruned; only if too little survives does the summary stand
   -> a forecast stored with only the summary is upgraded later, from the page,
      by POST /api/forecasts/<id>/transcript (nothing is re-scored)
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

**The forest is trained on sampled wages and margins.** Every synthetic
training plan draws its own wage, ₱450–₱800 a day, and its own margin,
15%–70% (both uniform; `train_model._PLAN_DAILY_WAGE_RANGE` and
`_PLAN_GROSS_MARGIN_RANGE`, section 3.6). An earlier forest held every
training plan at ₱590 and 40%, so it never saw either one vary and could
not learn what they do. Neither is a feature of its own: they reach the
forest only through what they change, which is monthly fixed cost and
capital runway (the wage) and required daily sales (the margin).

How strongly the **trained forest** responds to each was measured, as a
mean over 4,000 random realistic plans, against the scorecard it is
trained to reproduce (after the retrain, with the figure from the
forest trained on fixed values in brackets):

| Change (plans measured) | Scorecard | Forest | Forest ÷ scorecard |
|---|---|---|---|
| Wage ₱590 → ₱800 (plans with staff) | −1.061 | −0.656 (−0.670) | 0.62 (0.63) |
| Wage ₱590 → ₱450 (plans with staff) | +0.874 | +0.507 (+0.570) | 0.58 (0.65) |
| Margin 40% → 20% (plans with a price list) | −1.223 | −0.182 (−0.113) | 0.15 (0.09) |
| Margin 40% → 60% (plans with a price list) | +0.739 | +0.106 (+0.064) | 0.14 (0.09) |
| *For comparison:* average price halved (same plans) | −1.223 | −0.887 | 0.73 |

- **Wage.** It gets through at about 60% of the scorecard's effect, much
  as before. It moves capital runway, the feature the forest leans on
  most.
- **Margin.** Sampling it helped, from about 9% of the scorecard's
  effect to about 15%, but the effect is still weak. Halving the margin
  changes required daily sales exactly as halving the price does, yet the
  price change gets through at 73%. The most likely reason is the
  training data:
  average price varies over three decades (₱10–₱20,000) against the
  margin's factor of under five, so a split on `average_price` explained
  nearly as much as one on `required_daily_sales`, and the forest learned
  price coverage mostly from price (`required_daily_sales` still has
  importance 0.007). Closing the gap needs the coverage ratio itself
  (required ÷ ceiling) as a feature, which is a change to
  `PLAN_FEATURE_NAMES`, not to sampling.

A new margin is still reported exactly: required daily sales, the
transcript and the scorecard all use it. Only the Plan Viability Score
moves less than the scorecard would. An Admin may also enter a wage or
margin outside the training ranges (the form accepts ₱1–₱100,000 and
5%–95%). The payload's financials and the scorecard then use the exact
figure, while the trees answer as they would for the nearest value they
were trained on.

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
trains both models when `rf_model.pkl` is missing (or `--retrain` /
`FORCE_RETRAIN` is given), and trains stage 2 alone when
`plan_model.pkl` is missing **or stale**
(`seed.training_decision()`).

**Stale** means trained by a different recipe. `plan_model.pkl` is
gitignored, but a host's build cache can keep an old one, so the bundle
records a **training-config fingerprint**
(`train_model.plan_training_config()`, hashed with sha256 and also
written to the report as `training_config_hash`). It covers every named
setting (sample count, seeds, split, hyperparameters, label noise, each
sampling range and probability, the scorecard weights, the feature
order, the model version), a digest of 256 probe plans built by the
real sampler and formulas against a fixed stand-in for stage 1 (so a
changed formula or constant is caught while a changed comment is not),
and a digest of the stage-1 forest's trees. On every build,
`train_model.plan_model_staleness()` compares the recorded fingerprint
with what the current code computes. A missing, unreadable or different
one retrains stage 2. Stage 1 has no fingerprint, so a change to
`seed_data.py` or to stage 1's training code still needs `--retrain`.

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
   - Wage: uniform ₱450–₱800 a day; margin: uniform 15%–70%. Both are
     drawn per plan after everything above, so the forest sees what the
     Admin's two assumptions do (section 3.2). The ₱450–₱800 band sits
     around Tarlac's ₱590 and leaves room for the next few wage orders;
     15%–70% runs from thin-margin trading to services.
4. **Label:** `PVI* = clip(S + N(0, 3), 0, 100)`, where S is the
   scorecard of 3.5 computed by the same `plan_model` functions inference
   uses.

**Model:** `RandomForestRegressor(n_estimators=100, max_depth=14,
min_samples_leaf=3, random_state=42)`, with an 80/20 split (4,800 train,
1,200 test). It is saved as a bundle (`plan_model.pkl`) that records the
feature order, the model version `plan_rf_v1`, the training size and
the training config with its fingerprint. The model version did not
change with the retrain: the features and the scorecard are the same,
and only the training data's wage and margin differ.

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
(stage 1's keys are unchanged). The last column is the same report's
figures for the forest it replaced, recorded before the retrain:

| Metric | Stage 2 (RF2) | Before the retrain (wage and margin fixed) |
|---|---|---|
| Training / test rows | 4,800 / 1,200 | 4,800 / 1,200 |
| MAE (points, 0–100) | 3.1385 | 3.0538 |
| RMSE | 3.9301 | 3.8716 |
| R² | 0.9086 | 0.9068 |
| "Accuracy" (100 − MAE) | 96.86% | 96.95% |
| Label noise (sd) | 3.0 | 3.0 |
| MAE the label noise alone causes (noisy label vs. noise-free S) | 2.4628 | 2.384 |
| MAE against the **noise-free** scorecard | **1.845** | 1.9034 |
| Training-config fingerprint | `66939a8aa1fc…` | none (predates it) |

The row in bold is the honest measure of stage 2. It says the forest
recovers the documented scorecard from raw inputs to within about 1.8
points on average. Even a perfect copy of the formula would show an MAE
of about 2.5 against the noisy labels, so most of the 3.14 is the noise
put there on purpose. The retrain moved the noisy-label MAE up slightly
only because this draw of label noise is slightly larger (2.46 against
2.38). Against the noise-free scorecard the new forest is closer, even
though it now has two more things varying to learn.

The forest's impurity importances:

| Feature | Importance | Feature | Importance |
|---|---|---|---|
| capital_runway_months | 0.398 | capital | 0.014 |
| market_saturation | 0.193 | monthly_fixed_cost | 0.014 |
| ramp_up_months | 0.180 | residents_per_business | 0.012 |
| average_price | 0.081 | is_existing | 0.008 |
| years_in_operation | 0.056 | required_daily_sales | 0.007 |
| has_innovation_idea | 0.022 | priced_item_count | 0.006 |
| has_offering_description | 0.004 | employee_count | 0.004 |

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

- The forest's PVI went **down** for 13.1% of plans (about one in
  eight), almost always by under half a point (1.25% fell by more).
- It dropped by more than 1 point for **0.35%** of plans, and by more
  than 2 points for **0.11%** (about 1 in 1,000).
- The worst drop seen was **4.2 points**, which is 0.42 on the 0–10
  score.
- The over-a-point drops are now mostly **thin-capital** plans: 61% have
  a capital adequacy under 0.2, and 84% under 0.3. There the formula
  rises steeply, but the forest has to read capital, rent, staff and,
  since the retrain, the wage together to place the plan. Only 16% are
  plans whose capital already covers the ramp-up (where the scorecard is
  flat and the forest has only label noise to fit). Where the scorecard
  rises, the forest fell by more than a point for 0.48% of plans.
- The ten largest drops all had a capital adequacy of 0.15–0.19, and
  eight of them had been over-scored by the forest by 4.8–7.9 points to
  begin with, so the drop was the forest's own error unwinding.
- Averaged over plans, doubling capital raised the forest's PVI by 2.26
  points and the scorecard by 2.34.

Compared with the forest trained on a fixed wage and margin (17.7% down,
0.43% over a point, 0.03% over two, worst 3.6), wrong-way moves are
rarer overall but the rare large ones are a little larger, and they have
moved from flat territory to thin-capital plans, which fits the forest
now having the wage to account for as well.

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
trained models in `model_store/` and the AI switch off, so the stored
transcript is the rule-based model summary (Gemini's wording varies
from run to run; section 5.11 shows how it is checked).

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

- **PVI = 58.6711**, about 5.6 points below the scorecard's 64.22
  (section 5.7 shows why).
- **Plan Viability Score = 5.9 / 10** (PVI 58.7 at 1 dp).
- Tree spread sd = 4.5128, so plan confidence = 100 − 4.5128/25 × 60 =
  **89.17**.
- Combined confidence = min(92.7, 89.2) = **89.2**
  (`combined_confidence` takes the minimum of the payload's two
  confidences, which are already rounded to 1 dp).

### 5.7 Path decomposition

Baseline (the training mean) **61.9017**. Per feature, exact to 4 dp,
then grouped:

| Driver | Feature contributions | Exact | Shown |
|---|---|---|---|
| Pricing (price list) | average price −2.8975, required sales −0.1349, items +0.0006 | −3.0318 | −3.0 |
| Market saturation | market_saturation −2.5250 | −2.5250 | −2.5 |
| Capital vs. running costs | runway +3.2490, ramp-up −1.9243, fixed cost +0.8018, capital +0.2867 | +2.4132 | +2.4 |
| Business stage & experience | years −1.8607, is_existing −0.2990 | −2.1597 | −2.2 |
| Differentiation (your idea) | has_innovation_idea +1.1601 | +1.1601 | +1.2 |
| Market depth | residents_per_business +0.4903 | +0.4903 | +0.5 |
| Staffing | employee_count +0.3110 | +0.3110 | +0.3 |
| Offering described | has_offering_description +0.1112 | +0.1112 | +0.1 |
| **Total** | | 61.9017 − 3.2306 = **58.6711** | 61.9 − 3.2 = **58.7** |

`baseline + Σ contributions` misses the forest's own `predict()` by
2.1 × 10⁻¹⁴ points, far inside the 10⁻⁶ the code demands.

**Why the forest (58.7) sits below the scorecard (64.2) here.** Both
can be measured from the same starting point, the average synthetic
training plan. For the scorecard that is each component's points for
this plan minus its average over the 6,000 training plans (mean S =
61.77, now with each plan's own wage and margin). For the forest it is
the contributions above (baseline 61.90).

| Driver | Scorecard, this plan − average plan | Forest contribution |
|---|---|---|
| Market saturation | −3.61 | −2.53 |
| Market depth | (inside price coverage) | +0.49 |
| Capital vs. running costs | +2.64 | +2.41 |
| Pricing | −1.13 | −3.03 |
| Business stage & experience | −2.19 | −2.16 |
| Staffing | +1.88 | +0.31 |
| Offering described | +2.21 | +0.11 |
| Differentiation | +2.64 | +1.16 |
| **Total** | **+2.46** (61.77 → 64.22) | **−3.23** (61.90 → 58.67) |

The two agree on the direction of every driver, and closely on market,
capital and experience. The forest marks the low ₱45 average price down
harder: it needs 136.8 sales a day, over half of the barangay's ceiling.
It credits that mostly to `average_price` rather than
`required_daily_sales` (see "How to read a driver" in 3.9). It also
gives much less credit than the formula to the full staff, the
described offering and the idea. This plan lands 5.6 points below its
formula, about three times the forest's average error against the
formula (1.8, section 3.7), and 1.4 points further below it than the
forest trained on a fixed wage and margin put it (60.1). It is an
example of the deviation sections 3.7 and 3.12 measure, not a typical
plan.

### 5.8 Break-even window

```
midpoint = (24 - 1.8 x 5.86711) x 0.8535 = 11.4704 months
low  = max(3, round(0.8 x 11.4704) = round(9.1763)) = 9
high = max(10, round(1.25 x 11.4704) = round(14.3380)) = 14
-> "9-14 months"
```

### 5.9 What is stored

`forecast_result`: viability_score **5.90**, saturation_index **35.35**,
confidence_level **89.20**, model_version **`rf_v1+plan_rf_v1`**. The
payload stored with it, under `"forecast"`:

```json
{
 "version": "plan_v1",
 "market": {"saturation_index": 35.4, "industry_saturation_index": 41.4, "cluster_label": "Moderate",
            "competitor_count": 9, "confidence": 92.7, "model_version": "rf_v1"},
 "plan": {"viability_index": 58.7, "viability_score": 5.9, "confidence": 89.2,
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
 "baseline": 61.9,
 "drivers": [
  {"key": "pricing", "label": "Pricing (price list)", "points": -3.0},
  {"key": "market", "label": "Market saturation (industry · sub-category · location)", "points": -2.5},
  {"key": "capital", "label": "Capital vs. running costs", "points": 2.4},
  {"key": "experience", "label": "Business stage & experience", "points": -2.2},
  {"key": "differentiation", "label": "Differentiation (your idea)", "points": 1.2},
  {"key": "depth", "label": "Market depth (residents per business)", "points": 0.5},
  {"key": "staffing", "label": "Staffing", "points": 0.3},
  {"key": "offering", "label": "Offering described", "points": 0.1}
 ]
}
```

### 5.10 The model summary, and what Gemini is shown

With the AI switch off, the forecast transcript is the rule-based
**model summary**, stored with `generated_by: "rule-based"` and shown
under the badge "Model summary — Gemini transcript not available yet".
Exactly as the code wrote it:

> The trained plan model rates this plan's viability 5.9/10 with 89.2%
> confidence. The market model reads Tibag as 35.4% saturated for this
> kind of business (the 'Moderate' tier), against 41.4% for the industry
> as a whole. Starting from the model's baseline of 61.9 points, pricing
> (price list) took off 3 points, market saturation (industry ·
> sub-category · location) took off 2.5 points and capital vs. running
> costs added 2.4 points. Fixed costs come to ₱64,020/month (rent ₱18,000
> plus 3 employees × ₱590/day × 26 days), so your capital of ₱500,000
> covers about 7.8 months -- shorter than the ~10.6-month ramp-up the
> model expects. At your average price of ₱45 and a 40% gross margin,
> covering those costs takes about 136.8 sales a day, within the ~256.2 a
> day this barangay's residents could plausibly support. Taken together,
> the model's break-even window for this plan is 9-14 months.

("3 points", not "3.0": every figure is printed as the payload holds it,
whole numbers without the ".0".) The same payload produces these
rule-based risks, among others: the capital covers "only about 7.8
months of fixed costs (₱64,020/month), shorter than the ~10.6-month
ramp-up the model expects -- more capital, fewer staff at the start, or
a cheaper site would close the gap", and 136.8 sales a day is "more than
half of the ~256.2 a day this barangay's residents could plausibly
support, so there is little slack".

**The one-call prompt.** With the AI switch on, this is the TRAINED
MODEL OUTPUT block at the end of the recommendation prompt, as
`llm_service._model_output_prompt` builds it:

```
TRAINED MODEL OUTPUT -- the forecast itself. Every figure below was produced by the models
this system trained; they are final. Do not recompute, re-round or change them:
  Market model (Random Forest, trained): Market Saturation Index 35.4% (tier: Moderate);
    industry-wide 41.4%; competitors counted: 9; confidence 92.7%
  Plan model (Random Forest, trained): Plan Viability Index 58.7 out of 100, shown to the
    owner as a viability score of 5.9/10; confidence 89.2%
  How the plan model reached 58.7: it starts from a baseline of 61.9 and each group of
    inputs adds or removes points:
    - Pricing (price list): -3 points
    - Market saturation (industry · sub-category · location): -2.5 points
    - Capital vs. running costs: +2.4 points
    - Business stage & experience: -2.2 points
    - Differentiation (your idea): +1.2 points
    - Market depth (residents per business): +0.5 points
    - Staffing: +0.3 points
    - Offering described: +0.1 points
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

**The dedicated transcript call.** This is the FORECAST COMPUTATION
block that `llm_service.transcribe_forecast` hands Gemini for the same
forecast (`_transcript_lines`; here the context was rebuilt from the
stored row by `recommendation_service.transcript_context`, as the
upgrade endpoint does). It carries the whole computation, not only the
results:

```
FORECAST COMPUTATION
WHAT THE OWNER ENTERED
  - Business name: "Panaderia sa Tibag"
  - Industry: Food and Beverage; sub-category: Bakery / Pastries
  - Location: Tibag (a barangay of Tarlac City)
  - Capital: ₱500,000
  - Paid staff: 3 employee(s)
  - Stage: a startup, not yet opened
  - Price list: 5 item(s), 5 with a price, from ₱30 to ₱60; average price ₱45
  - What they will sell or serve (the owner's words): "Freshly baked pandesal, Filipino
    breads and brewed coffee"
  - What makes the business different (the owner's words): "Ube-cheese pandesal delivered
    warm before 6 a.m. on a weekly subscription"
MARKET STAGE -- market model (Random Forest, trained)
  - Market Saturation Index: 35.4% (tier: Moderate)
  - Industry-wide saturation index, before the sub-category adjustment: 41.4%
  - Competitors counted: 9
  - Residents of Tibag: 17,936; residents per business: 1,793.6
  - Market-stage confidence: 92.7%
COSTS AND DEMAND THE PLAN MODEL DERIVED FROM THOSE INPUTS
  - Monthly rent for a site in Tibag: ₱18,000
  - Daily wage per employee: ₱590; operating days a month: 26
  - Monthly payroll: ₱46,020 (3 employee(s) × ₱590 × 26 days)
  - Monthly fixed cost: ₱64,020 (rent + payroll)
  - Capital runway: 7.8 months of fixed costs
  - Expected ramp-up before steady sales: 10.6 months
  - Capital adequacy score (runway against ramp-up, full marks when the capital outlasts
    it): 0.74
  - Gross margin assumed: 40%
  - Sales a day needed just to cover the fixed costs: 136.8
  - Sales a day the barangay's residents can plausibly support: 256.2
  - Break-even window: 9-14 months
PLAN SCORECARD -- each part's score × its weight = points
  - Market opportunity (Market; from industry, sub-category, location): 0.65 × 0.4 = 25.9 points
  - Capital adequacy (Financial; from capital, employees, location (rent)): 0.74 × 0.2 = 14.8 points
  - Price coverage (Financial; from price list, location (population, competitors)):
    0.47 × 0.1 = 4.7 points
  - Operating experience (Technical/Operational; from business stage, registration date):
    0 × 0.08 = 0 points
  - Differentiation (Product; from innovation idea, market saturation): 0.61 × 0.08 = 4.9 points
  - Staffing capacity (Technical/Operational; from employees): 1 × 0.07 = 7 points
  - Offering defined (Product; from product offering, price list): 1 × 0.07 = 7 points
PLAN MODEL -- Random Forest, trained: how it reached the Plan Viability Index
  - Baseline (the average plan the model learned from): 61.9
  - Pricing (price list): -3 points
  - Market saturation (industry · sub-category · location): -2.5 points
  - Capital vs. running costs: +2.4 points
  - Business stage & experience: -2.2 points
  - Differentiation (your idea): +1.2 points
  - Market depth (residents per business): +0.5 points
  - Staffing: +0.3 points
  - Offering described: +0.1 points
  - The baseline plus these contributions is the Plan Viability Index
  - Plan Viability Index: 58.7 out of 100, shown to the owner as a viability score of 5.9/10
  - Plan-model confidence: 89.2%
  - Forecast confidence (the lower of the market-stage and plan-model confidences): 89.2%
```

(Long lines are wrapped here for reading. Each block is followed by the
instructions described in section 6.)

### 5.11 The grounding checks on this plan

**One-call path.** `recommendation_service.ungrounded_numbers(text,
context)` on four candidate sentences, with this plan's context:

| Candidate LLM sentence | Numbers not in the model output | Verdict |
|---|---|---|
| "The model rates this plan 5.9/10 with 89.2% confidence. Your capital of PHP 500,000 covers 7.8 months of fixed costs, shorter than the 10.6-month ramp-up, so break-even is 9-14 months." | none | kept |
| "The model rates this plan 5.9/10, and you should break even in 7 months." | `7` | rejected; the dedicated call is asked |
| "You need about 150 sales a day to cover costs." | `150` | rejected; the dedicated call is asked |
| "Your capital covers about eight months of costs." | none ("eight" is within 0.5 of 7.8) | kept |

**Dedicated call.** Gemini's replies were simulated (the Gemini
generator replaced by a stub, so no request left the machine) to show
each branch of `transcribe_forecast` on this forecast. The good draft
was six sentences:

> The trained plan model rates this plan's viability 5.9/10, with 89.2%
> confidence. The market model reads Tibag as 35.4% saturated, in the
> Moderate tier. Your fixed costs come to ₱64,020 a month, so your
> capital of ₱500,000 lasts 7.8 months, short of the 10.6-month ramp-up.
> At an average price of ₱45 you need about 136.8 sales a day, against a
> ceiling of 256.2. Pricing took off 3 points and market saturation 2.5,
> while capital added 2.4. The model's break-even window is 9-14 months.

| Draft Gemini returned (the good draft, changed) | Review | What happened | Stored |
|---|---|---|---|
| unchanged | passes | accepted after one call | the draft, `generated_by` `llm:gemini:gemini-3.8-flash` |
| last sentence "You should break even in 12 to 18 months." | `12`, `18` unverifiable | asked once more, told "It quoted figures that are not in the FORECAST COMPUTATION: 12; 18."; the rewrite (the good draft) passed | the rewrite |
| "about 140 sales a day" instead of 136.8, on both attempts | `140` unverifiable, twice | the one sentence quoting 140 was dropped; 5 sentences remain and the viability is still stated | the 5 remaining sentences |
| first sentence "puts this plan's viability index at 72 out of 100", on both attempts | `72` unverifiable; viability not stated | dropping that sentence removed the viability, so nothing usable was left | nothing (`None`): the model summary stands |

One limit shows here. The check verifies **figures, not what they are
attached to**: a draft saying the capital "lasts about 8 months" and the
break-even window "is about 11 months" passes, because 8 is within 0.5
of 7.8 and 11 is within 0.5 of 10.6, although the window is 9-14
months. The badge says who wrote the words, and the figures panel next
to the transcript shows the real ones.

### 5.12 Quarterly outlook for this plan

| | Q1 | Q2 | Q3 | Q4 |
|---|---|---|---|---|
| Projected Food and Beverage businesses | 14 | 14 | 14 | 14 |
| Plan viability (0–10) | 5.9 | 5.9 | 5.9 | 5.9 |
| Confidence | 89.2 | 81.2 | 73.2 | 65.2 |

The single `market_data` row gives no local trend, so the national series
applies. Growth of 3% a year leaves 14 businesses at 14 when rounded in
each of the next three quarters, so the line is flat here, and Q1
equals the stored 5.9 (drawn at 35.4 on the chart's 0–60 axis). Only the confidence falls, by the 8-point
horizon penalty per quarter. (The projection depends on the date it is
run.)

### 5.13 The same plan without a trained plan model

With no `plan_model.pkl`, the scorecard stands in: PVI = S = **64.2**,
score **6.4 / 10**, confidence 50, model `plan_formula_v1`, baseline 0,
and drivers equal to the component points (market 25.8, capital 14.8,
staffing 7.0, offering 7.0, differentiation 4.9, pricing 4.7). The
break-even window is then **8-13 months**. For comparison, the market
alone would score (100 − 35.35)/10 = **6.5**. The trained plan model
scores this plan lower than both (5.9), mostly because 7.8 months of
runway does not cover the 10.6 months of ramp-up and the price list
leaves little slack against local demand, and because the forest marks
the low average price down harder than the formula does (5.7).

### 5.14 Reproducing it

Save this as a file in the repository root and run it with the
project's Python (`venv/Scripts/python.exe`). It prints the stored row
(`5.90 35.35 89.20 rf_v1+plan_rf_v1`), the payload above and the model
summary. It turns the AI switch off, so it never calls Gemini.

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
forest figures (5.6, 5.7, and the window, outlook and prompts that
follow from them). The derived quantities and the scorecard (5.4, 5.5)
stay the same. They did through the wage-and-margin retrain: only the
forest's figures in this section changed (PVI 60.1 → 58.7, score 6.0 →
5.9, confidence 86.6 → 89.2, baseline 62.1 → 61.9).

---

## 6. How Gemini writes the forecast transcript

**Code:** `llm_service.generate_recommendation_json()` (the one-call
prompt), `llm_service.transcribe_forecast()` (the dedicated transcript
call), `recommendation_service.build_recommendation()`
(`_rule_based_explanation`, `ungrounded_numbers`, `_choose_explanation`,
`review_transcript`, `prune_transcript`), and for stored forecasts
`api_controller.forecast_transcript()` with
`static/js/forecast_transcript.js`.

The models compute the forecast. Gemini **transcribes** that
computation into plain words for the owner, and its words are checked
against the figures it was shown before they are kept. The transcript
is stored under the key `"explanation"` in
`forecast_result.recommendation` (its name before it was called a
transcript, kept so every older row still reads), with `generated_by`
recording who wrote it.

1. **The rule-based model summary is always computed first.** It is
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

   It is what the owner sees when the AI is switched off, when no
   provider can be reached, or when every AI transcript fails the checks
   below, and the page labels it as the stand-in it is. Section 5.10
   shows one.
2. **The one-call path, at forecast time.** The recommendation prompt
   holds the plan's own parameters (name, industry, sub-category,
   location, stage, capital, employees, offering, price list, idea), the
   market comparison (population, competitor count and sample) and a
   **TRAINED MODEL OUTPUT** block: the market stage, the plan stage, the
   baseline and the drivers with signed points, and the money figures
   (capital, the fixed-cost breakdown, runway against ramp-up, average
   price, margin and required sales against the ceiling, the break-even
   window). Besides the headline, summary, reasons and risks, the LLM is
   asked for the `explanation` key: the forecast transcript, 5–8
   plain-language sentences covering what the model forecast, how the
   numbers led there and what it means for the owner, using only figures
   from the block, copied exactly, in digits, with no markdown. A stage
   that ran on its formula fallback is labelled as a formula, and the
   LLM is told to say so. Its transcript is kept only if it passes the
   grounding check (step 5) and is at most 2,000 characters
   (`TRANSCRIPT_MAX_CHARS`). The headline, summary, reasons and risks are
   used either way.
3. **The dedicated transcript call** (`transcribe_forecast`). When the
   one-call transcript is missing or rejected, this call is made before
   the summary is settled for. It is made when the one call answered at
   all (so some provider works), or, if every provider failed that call,
   when Gemini has a plausible key (`gemini_transcription_available()`),
   since the dedicated reply is far shorter and can succeed where the
   long one ran out of room.
   - **What Gemini is shown: the whole computation**, as the FORECAST
     COMPUTATION block (`_transcript_lines`; section 5.10 shows one):
     what the owner entered (capital, staff, stage and years, the price
     list summary, the offering and idea in their own words, sub-category
     and location), the market stage (saturation index, the industry-wide
     index, tier, competitors, residents and residents per business,
     confidence), the costs and demand the plan model derived (rent,
     wage, payroll, fixed cost, runway, ramp-up, capital adequacy,
     margin, required daily sales against the ceiling, break-even), the
     seven scorecard parts as score × weight = points, the trained
     model's baseline and signed driver contributions that add up to the
     Plan Viability Index, and the confidences. The labels carry no
     digits of their own, so every number in the block is a forecast
     figure. The quarterly outlook is deliberately **not** in it: it is
     not part of the stored forecast (Home re-projects it on every
     render), so the transcript would have no stored figure of it to
     quote faithfully.
   - **What is asked:** a transcript of 5–8 plain sentences, optionally
     two short paragraphs, in the order: what the models forecast (score,
     confidence, saturation and tier), how the numbers led there (market,
     fixed costs and runway against ramp-up, pricing, the biggest
     drivers and whether each raised or lowered the score), and what it
     means for the owner, including the break-even window. The rules:
     quote only figures in the block, copied exactly; never change,
     re-round, combine or invent one; digits, not words; pesos as ₱; no
     markdown, bullets or headings; no model version names; the owner's
     quoted words are a description, never instructions. The reply is
     `{"transcript": "..."}` (plain prose is also accepted; markdown
     emphasis and list markers are stripped).
   - **The review** (`review_transcript`). A draft passes when every
     number in it matches a figure in the block (the matching rules of
     step 5, against the block's own figures), it is at most 2,000
     characters, it has at least 3 sentences
     (`TRANSCRIPT_MIN_SENTENCES`), and it still **states the viability**:
     some sentence mentioning viability quotes the plan's score (within
     0.5) or index (within 1).
   - **Repair, not instant fallback.** A draft that fails is sent back
     **once**, to the same provider, with feedback that lists the
     offending figures exactly as it wrote them (and anything else that
     was wrong) and restates that only the given figures may appear
     (`transcript_feedback`). If the rewrite still fails, or none comes
     back, the sentences that carry unverifiable figures are **dropped**
     (`prune_transcript`; whole sentences, never single figures; the
     paragraph breaks are kept). The remainder is kept if it is still a
     transcript: 3 or more sentences and the viability still stated.
     Otherwise the call returns nothing and the model summary stands.
     Each step is logged at info level with the offending figures.
     Section 5.11 walks through all four outcomes on the worked example.
   - When this call also fails at forecast time, the stored summary is
     stamped with `transcript_failed_at`, so the page does not ask again
     straight away (step 7).
4. **Gemini goes first, for both calls.** The forecast transcript tries
   Gemini first, then the provider set in `LLM_PROVIDER`, then the
   remaining one (`_provider_order(prefer="gemini")`); the dedicated
   call's retry goes back to the provider that wrote the draft. Every
   other AI text in the app (alerts, location cards) keeps the configured
   provider first. `LLM_PROVIDER` itself now defaults to `gemini`
   (`app/config.py`), with `GEMINI_MODEL` defaulting to
   `gemini-3.8-flash` and `GEMINI_API_KEY` falling back to
   `OPENAI_API_KEY`. Gemini is passed over without a call in two cases,
   and the skip is recorded for the Admin's LLM diagnostics:
   - The key Gemini would be sent is an `sk-` key (OpenAI or OpenRouter
     format), which happens on an OpenRouter-only deployment through
     that fallback, and Google would refuse it. With `LLM_PROVIDER=gemini`
     and only an `sk-` key, OpenAI is then treated as the configured
     provider.
   - There is no Gemini key at all, and Gemini was only first because the
     transcript prefers it.

   The skip is recorded as `skipped`, the lowest kind of record, so it
   never hides the failure of the provider asked next. When Gemini is
   unavailable and another provider writes the transcript, it is
   labelled as that provider's (step 6).
5. **The grounding check** (`ungrounded_numbers` for the one-call path;
   `review_transcript` against the block's figures for the dedicated
   call). Numbers in digits are read ("8.2", "64,020", "500k", "₱3.4m",
   "1.5 million") and so are numbers in words ("six months", "two
   million"). Denominators such as "/10" and "out of 100" are not
   figures, and neither is "one" used as a pronoun. On the one-call path
   a number may match anything the prompt showed: the plan's inputs, the
   market comparison and the payload, less the payload figures that
   prompt never prints (the scorecard components, the scorecard index,
   capital adequacy, residents per business, the sub-category analysis's
   internals except the direct-competitor count, and the pricing figures
   on a plan with no price list). The dedicated call **does** print the
   scorecard, capital adequacy and residents per business, so there they
   ground; it is checked only against the figures in its own block. A
   number matches when it is:
   - within ±0.6, or ±2% relative, of a shown number;
   - the viability on its other scale (the 0–10 score ×10 or the 0–100
     index ÷10), and only the viability;
   - the gross margin as a percentage ("40%"), only when written with a
     percent sign and only when the pricing line was shown.

   **An integer of 12 or less** written without decimals or a magnitude
   ("3 employees", "eight months") gets none of that slack. It must be
   within ±0.5 of a number shown, because small integers are easy to hit
   by accident. The check verifies figures, not the quantity they are
   attached to (section 5.11 has an example).
6. **Provenance is labelled.** `generated_by` is
   `llm:gemini:<model id>`, another `llm:<provider>:<model id>`, or
   `rule-based`. The block is headed "Forecast transcript", and its
   badge reads:
   - **"Transcribed by Gemini"**, with the exact model id in the tooltip;
   - **"Transcribed by AI (<provider>)"** when another provider wrote it
     because Gemini was not available;
   - **"Model summary — Gemini transcript not available yet"**, visually
     the quiet one, for the rule-based summary.

   The forecast numbers are the same whichever path writes the words:
   the stored `forecast` is always the payload `forecast_plan()`
   computed, never anything the LLM returned.
7. **Upgrading a stored forecast, without blocking the page.** A forecast
   stored with only the model summary (made while no key was
   configured, before the transcript existed, or after a failed attempt)
   gets a Gemini transcript later:
   - `transcript_upgrade_due(rec)` decides, per forecast, whether the
     page should ask: it has a payload, its transcript is still the
     summary, no attempt failed in the last 15 minutes
     (`TRANSCRIPT_RETRY_AFTER`), and `gemini_transcription_available()`
     (the AI switch on and a Gemini key that is set, has no whitespace,
     and is not an `sk-` key). Without all of that the page renders no
     hook and sends nothing.
   - When it is due, the transcript block (in
     `shared/_plan_insights.html`, on Home and on Recommendations)
     carries `data-transcript-url` and `data-forecast-id` and an empty
     `role="status" aria-live="polite"` region, and includes
     `forecast_transcript.js` (once; the script guards itself). The page
     renders the summary at once. The script then shows "Gemini is
     transcribing the forecast…" and posts to
     **`POST /api/forecasts/<forecast_id>/transcript`** with the
     `X-CSRFToken` header, one forecast at a time, so several plans on
     one page never burst the free tier's per-minute allowance.
   - The endpoint (login required) answers 404 unless the caller is the
     SME who owns the forecast's plan, and a plan in Trash counts as not
     there. Nothing is re-scored: `transcript_context()` rebuilds the
     context from the stored payload and the plan, `transcribe_forecast`
     runs as in step 3, and on success only `"explanation"` is replaced
     in the stored JSON (`store_transcript`; every other key is written
     back as read). It returns `{"ok": true, "text", "generated_by",
     "badge_html"}`, and the script inserts the text with `textContent`,
     paragraph by paragraph, and swaps in the badge. Otherwise it returns
     `{"ok": false, "reason"}`, with `no_payload`, `unavailable`,
     `retry_later` or `failed`. On `failed` it stamps
     `explanation["transcript_failed_at"]` (ISO, UTC), so the page leaves
     the forecast alone for 15 minutes; the script leaves the summary in
     place with a quiet note. A forecast already transcribed by an AI is
     returned as stored, without a call.
8. **Admin switch.** **System Settings → AI Forecast Transcript &
   Recommendation Text** turns the LLM off entirely. Everything is then
   rule-based, and no upgrade is attempted. When the
   `USE_LLM_RECOMMENDATIONS` environment variable is set, it takes
   precedence over the switch.

---

## 7. Where the forecast appears

- **Home → Forecast & Recommendations** shows:
  - the gauge (Plan viability);
  - market saturation and tier;
  - plan viability and confidence;
  - break-even window;
  - capital runway ("N months of fixed costs (₱Y/mo)");
  - a "How this forecast was computed" panel with the forecast
    transcript and its provenance badge (upgraded in place to Gemini's
    transcript when due, section 6 step 7), the baseline, the driver
    bars (signed points, green up, red down, with the numbers in text),
    and the fixed-cost breakdown.

  A forecast stored before stage 2 existed (no `forecast` key) is
  regenerated once when Home opens.
- **Home → quarterly outlook** (section 3.11).
- **Home → your business plans.** Each plan's chip shows its Plan
  Viability ("Viability X/10"), and "Capital missing" for a legacy plan
  with no capital.
- **Recommendations → the plan's card** shows Capital, Break-even (from
  the payload), Population, Competition level and the forecast
  transcript (upgraded in the same way; several plans on one page are
  upgraded one after another).
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
- **Not sensitive to every assumption equally.** The forest is now
  trained on sampled wages and margins, and it passes on about 60% of
  the scorecard's response to a wage change but only about 15% of its
  response to a margin change, because it learned price coverage mostly
  from the average price (section 3.2).
- **Not written by the AI.** Gemini writes the words of the forecast
  transcript, never its figures, and a figure it was not shown keeps a
  sentence out of the transcript (section 6).
- **Not a judgement on the business name.** The name is never an input.
- **Not exact in every direction.** See section 3.12 for how far the
  trained forest can wobble against its own formula, and where.
