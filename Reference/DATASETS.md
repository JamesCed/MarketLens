# Reference/DATASETS.md

Where the numbers in this project come from today, what's genuinely real
vs. estimated, and where to get the rest before your defense / before any
real deployment. This file was rebuilt after a second, deeper research
pass specifically requested to (a) resolve a barangay-name ambiguity,
(b) get real land area for a true population density, and (c) find out
whether real DTI/PSA/LGU business data exists at the barangay level. All
three are answered below, honestly, including what could NOT be found.

## 1. The "San Juan de Bautista" mystery is solved -- it's two different real barangays

An earlier version of this file flagged an unresolved ambiguity: a CLUP
source named a 19th urban barangay "San Juan de Bautista," which doesn't
exist by that exact name in the official 76-barangay list, and guessed
that "San Juan de Mata" was probably a mistranscription of it.

**That guess was wrong, and you were right to flag it.** Tarlac City's
own government website (tarlaccity.gov.ph) confirms these are two
separate, real barangays:

- **San Juan de Mata** -- its own barangay, PSGC code 0306916064,
  population 4,727 (PSA 2024), land area 992.54 hectares.
- **"San Juan Bautista"** (no "de") -- this is the *official/original
  name* for the barangay everyone calls **Matadero** in the PSGC list.
  Its own government page is literally titled "Barangay San Juan
  Bautista (Matadero)": <https://tarlaccity.gov.ph/san-juan-bautista/>.
  PSGC code 0306916096, population 2,099 (PSA 2024), land area 38.91
  hectares.

Both were already present in `app/ml/seed_data.py` as separate rows
(`"San Juan de Mata"` and `"Matadero"`) even before this fix, so no
barangay was missing from the dataset -- the only thing that was wrong
was which of the two I'd guessed the CLUP's "19th urban barangay" might
refer to. See section 2 below for why that guess no longer matters much:
neither one is confirmed urban by any single fully-trusted source.

## 2. Urban / rural classification -- genuinely contested, not fully resolved

**File:** `app/ml/seed_data.py` (`BARANGAY_PROFILES`, last column)

Three different attempts at classifying these 76 barangays as urban or
rural exist, and they do **not** agree with each other. This is reported
straight rather than papered over, because it's a real methodological
choice that affects your model's input features:

| Source | Urban count | Rural count | Coverage | Notes |
|---|---|---|---|---|
| **PSA/PSGC official classification** | 36 | 40 | Complete (76/76) | Single coherent source, census density/activity criteria. **This is what's shipped in `seed_data.py` right now.** |
| **Tarlac City's own barangay web pages** (tarlaccity.gov.ph), taken at face value | 16 confirmed, 2 unconfirmed | 58 | 74/76 confirmed | See caveat below -- this source's own text is internally inconsistent. |
| **CLUP figure you originally gave me** | 19 | 57 | -- | I could not locate an actual CLUP document hosted on tarlaccity.gov.ph or any other `.gov.ph` domain to verify this against a primary source. It may exist only as a physical/PDF document your City Planning & Development Office holds. |

PSA and the city's own website **disagree on 23 of the 76 barangays**
(e.g. Armenia, Balete, Balingcanaway, Carangian, Central, and 18 others
are called "Urban" by PSA but the barangay's own city-government page
says "Rural Barangay"; conversely, Suizo and "Matadero/San Juan
Bautista" are called "Rural" by PSA but the city's own page for Suizo
says "Urban Barangay").

**Why the city's own website isn't a clean answer either:** every
barangay page opens with a boilerplate sentence, "Barangay X classified
as Rural Barangay, it has a total population of...". Most pages just
leave that sentence as-is. But some pages have a *second*, later
sentence that overrides it -- e.g. Maligaya's and Paraiso's pages
literally say, verbatim: *"Barangay [X] is one of the 19 barangays on
Tarlac City classified as an urban barangay."* That sentence directly
corroborates your "19" figure as a real, named classification the city
uses -- but going through every page and specifically checking for that
override sentence (not just the opening boilerplate), only **16
barangays** carry it or an unambiguous direct "Urban Barangay" label:
Cut-cut I, Ligtasan, Mabini, Maligaya, Maliwalo, Matatalaib, Paraiso,
Poblacion, San Isidro, San Nicolas, San Pablo, San Rafael, San Roque,
San Sebastian, San Vicente, Suizo. Two more barangays couldn't be
confirmed either way: **San Miguel** (its page returns a persistent
server error and could not be fetched at all) and **San Pascual** (its
page has no classification sentence of any kind, just a description of
farming activity).

So the honest state of things: 16 confirmed + up to 2 unconfirmed = at
most 18, not quite 19, and the site's own inconsistency (leftover
boilerplate on pages that were only partially updated, at least one
duplicated land-area figure between Dolores and Lourdes, a typo reading
"Urbanl Barangay" on San Sebastian's page) means this source can't be
fully trusted as a complete, current register either.

**What I'd recommend:** the PSA classification shipped in the code is
the only complete, single-source, internally-consistent option, so
that's the default. But if your capstone specifically needs to match
"19 urban / 57 rural," the only way to get a confirmed, defensible
answer is to ask Tarlac City's **City Planning & Development Office
(CPDO)** directly for their current CLUP-approved urban/rural barangay
list -- that's the actual source of authority here, and neither PSA's
statistical classification nor the city website's inconsistent
descriptive text can substitute for it. If you obtain that list, replace
the `urban` column in `BARANGAY_PROFILES` (in `app/ml/seed_data.py`)
with it directly -- the assertion at the bottom of that file
(`assert _urban_count == 36`) will need its expected number updated to
match.

Full per-barangay detail (PSA population, PSA classification, land area,
and the city website's classification) is inline as a comment on every
row of `BARANGAY_PROFILES`, so you can audit any single barangay without
re-deriving anything.

## 3. Population density is now REAL for 73 of 76 barangays

**File:** `app/ml/seed_data.py` (`BARANGAY_PROFILES`, `population_density` column)

Previously this column held a raw population headcount as a stand-in for
density (flagged as such). That gap is now closed for almost the whole
dataset:

- **Land area**, in hectares, was pulled directly from each barangay's
  own profile page on Tarlac City's official government website
  (tarlaccity.gov.ph) -- e.g. Poblacion: 19.64 ha, Central: 713.19 ha,
  San Vicente: ~270 ha. This is real, sourced, per-barangay data.
- **`population_density` = 2024 PSA population ÷ land area in km²** --
  a true people/km² figure, computed and stored directly in the table.
- **Three barangays are still estimated**, using the dataset's average
  land area (367.61 ha) because their real figure wasn't available:
  - **Matatalaib** -- its page only gives an agricultural sub-area
    (56.5 ha), not a total.
  - **San Isidro** -- its page describes land use but gives no hectare
    total at all.
  - **San Miguel** -- its page could not be fetched (persistent server
    error across many retries, not a transient issue).
  Each is flagged inline in `seed_data.py` with `land=~367.61 ha
  ESTIMATED`. If you can get these three barangays' real land area
  (the CPDO or the barangay hall itself would have it), update those
  three rows and the numbers will be fully real.

## 4. Business data (rent, foot traffic, business density, SME success rate) -- still estimated, and here's exactly why

This was researched specifically to find out whether a real substitute
exists anywhere publicly. Short answer: **no barangay-level public
dataset exists for any of these four columns**, and here's what was
actually checked:

- **PSA's Census of Philippine Business and Industry (CPBI)** --
  the closest thing to a national business census. Its public tables
  are aggregated at the **regional** level only ("by region, industry
  group and sub-class"). Anything more granular (province/city/barangay)
  would require microdata access through the **PSA Data Enclave**,
  which is a formal, in-person/controlled-access research facility, not
  a public download.
- **PSA's Annual Survey of Philippine Business and Industry (ASPBI)** --
  same granularity limitation as CPBI.
- **DTI's Cities and Municipalities Competitiveness Index (CMCI)** --
  has a real Tarlac provincial profile
  (<https://cmci.dti.gov.ph/prov-profile.php?prov=Tarlac>) with
  city-level population and revenue figures (Tarlac City: population
  385,398, revenue ≈₱2.67B, competitiveness score 40.9622 -- the
  highest in the province), but **no business-establishment counts and
  nothing below city level**.
- **PSA Region III's local office** (rsso03.psa.gov.ph/tarlac) --
  publishes demographic, agricultural, and vital-statistics releases for
  Tarlac, but nothing on business establishments or employment.
- **BIR zonal valuation schedules** -- these DO exist per barangay/street
  (Tarlac City falls under Revenue District Office 17A) and would be a
  legitimate real proxy for commercial land value / rent, but the BIR's
  public zonal-value lookup is a JavaScript-driven tool that couldn't be
  read directly in this pass. Worth pursuing yourself: search
  bir.gov.ph's "Zonal Values" section for **RDO No. 17A -- Tarlac City**.

**The real, concrete next step for these four columns:** this data
genuinely only exists inside Tarlac City's own **Business Permits and
Licensing Office (BPLO)** and **City Treasurer's Office** records --
active business permits per barangay (→ `business_density`), permit
renewal vs. non-renewal rates over time (→ a real proxy for
`historical_success_rate`), and, if they track it, commercial lease/rent
data. This is exactly what this system's **Gov't Data Upload** page (LGU
role) is built to receive -- so the actual path to closing this gap is
requesting that data from those two city offices (or having your LGU
contact upload it directly), not finding it in a public dataset, because
no such public dataset exists at this granularity.

Until then, `foot_traffic_index`, `average_rent`, `business_density`,
and `historical_success_rate` remain a deterministic formula over the
real `population_density` + `urban` columns -- internally consistent for
model training, but not real figures.

## 5. How to replace any of this with real data

Two options, from quickest to most correct:

1. **Edit `app/ml/seed_data.py` directly** with real figures, then
   re-run `python seed.py` (or just `python -m app.ml.train_model`) to
   retrain the model on the corrected data.
2. **(Recommended for the real system)** Log in as an LGU account and
   use the Gov't Data Upload page to upload real Excel/CSV files. This
   writes directly into the live `market_data` / `lgu_data` tables via
   `app/services/data_import_service.py`, which is what the forecasting
   engine actually reads from at request time -- `seed_data.py` is only
   a training-time / demo fallback.

## 6. Other placeholder / synthetic components in this codebase

- `app/ml/train_model.py` bootstraps its training set from
  `BARANGAY_PROFILES` above combined with `BUSINESS_TYPES`
  (`app/controllers/sme_controller.py`) -- synthetic combinations, not
  real historical SME outcomes. Re-run training after real
  `market_data`/`lgu_data`/`forecast_result` rows accumulate.
- `app/services/places_service.py` falls back to a simulated
  competitor list when no Google Places API key is configured.
- Demo login accounts created by `seed.py`
  (`admin@dss.local` / `sme@dss.local` / `lgu@dss.local`) -- change or
  delete these before any real deployment.
- `app/services/historical_baseline_service.py` back-projects the Trend
  Reports page's pre-2026 history. See section 6b below -- it is the one
  synthetic component in this codebase whose SHAPE is taken from real
  published national figures rather than chosen.

## 6b. Trend Reports history (2020-2026) -- modelled, and grounded in real national figures

**The Google Places API has no historical data.** It answers "what is
there now". There is no parameter, endpoint or field in it that returns
the businesses that existed in a past year, and no commercial API does --
that is not what Places is. So the Trend Reports page's "Select Period"
calendar cannot be served from an API for any month before this app
started collecting its own `market_data` snapshots. Those months are
modelled, and this is exactly how.

**The level is real.** The starting point is this app's OWN measured
current count for each (industry, barangay) combo -- the freshest
`market_data.competitor_count` on file, which for any combo already
looked up is a real Google Places result.

**The trajectory is real.** That count is scaled back along the
Philippines' published MSME establishment series, from DTI's *Philippine
MSME Statistics* (built on PSA's List of Establishments):

| Year | Establishments | Change |
|------|---------------:|-------:|
| 2019 | 995,741        |        |
| 2020 | 952,969        | -4.3%  |
| 2021 | 1,076,279      | +12.9% |
| 2022 | 1,105,143      | +2.7%  |
| 2023 | 1,241,733      | +12.4% |

Annual figures are anchored at **mid-year**, not January, because an
annual establishment count is a whole-year figure. That places January
2020 between the 2019 and 2020 counts, so the monthly series falls
through the first half of 2020 and rebounds through 2021 -- the pandemic
contraction is visible rather than hidden at a year boundary.

**What is modelled:** only the attribution of that national trajectory
to one city and one barangay.

**What is assumed, plainly:**

1. Tarlac City moved with the national MSME trend. A city-level
   establishment series for Tarlac is not published (section 4 above
   documents the search that established this), so the national series
   is the closest real proxy available.
2. 2024 onward is not yet in the published series. Those years continue
   at `POST_RECOVERY_GROWTH = 0.03` -- deliberately conservative, roughly
   the 2022 step rather than the 12%+ rebound years.
3. Each (industry, barangay) line carries a small deterministic
   variation (`SERIES_VARIATION = 0.04`), seeded from the series name, so
   the chart is not N identically-shaped lines. It is never random and
   never re-rolled between page loads.

**Nothing is written to the database.** The projection is computed on
the fly on every request. Writing invented rows into `market_data` would
corrupt the one table in this system that holds real measurements: it
would inflate Total Businesses, be indistinguishable from a real PSA/DTI
upload afterwards, and be very hard to undo.

**It replaces itself.** A month with real dated rows is measured from
those rows and nothing else; only months with none are projected, and
every projected figure is flagged `projected` / `measured: false` and
labelled "(estimated)" on screen. As real snapshots accumulate, projected
points become measured ones with no code change.

**To replace it with real data:** if a city-level or provincial
establishment series for Tarlac is ever published, put its annual counts
into `NATIONAL_MSME_ESTABLISHMENTS` in
`app/services/historical_baseline_service.py`. Everything downstream --
KPI cards, monthly lines, quarterly chart -- follows automatically.

## 7. Household spending / "demand" data -- PSA FIES, city-level not barangay-level

Added for the Saturation Map's "demand" tooltip/detail panel figures
(food, utilities, transportation, recreation, education spending). Full
detail and every source URL is in
`app/services/socio_demographic_service.py`'s own module docstring --
short version:

- **Tarlac's average annual family expenditure (~PHP 299,670, 2023) is
  REAL and Tarlac-specific** -- PSA Region III's own 2023 Family Income
  and Expenditure Survey (FIES) special release reports Tarlac
  PROVINCE's total average annual family expenditure directly.
- **The percent-share breakdown by category (food 40.9%, housing/
  utilities 22.9%, transportation 10.7%, education 3.0%, etc.) is REAL
  but NATIONAL, not Tarlac-specific** -- from PSA's own 2023 FIES
  Infographics release. PSA does not publish a province- or city-level
  category breakdown anywhere (checked: PSA's OpenSTAT portal, the
  PSADA FIES 2023 microdata catalog, and PSA's own FIES press releases
  -- all stop at province-level TOTALS, never a province-level category
  split).
- **The peso amount per category shown on the map is therefore an
  ESTIMATE**: Tarlac's own real total (above) multiplied by the
  national category share. This assumes Tarlac households allocate
  their budget in roughly the same proportions as the national
  average -- a transparent, standard way to localize a national
  pattern when no local breakdown exists, but an assumption, not a
  measurement, and the UI/code both say so.
- **Recreation specifically** is the softest figure here: PSA's 2023
  release doesn't break it out on its own (it's folded into a combined
  8.6% "Other" bucket with clothing, alcohol/tobacco, restaurants/
  hotels, and furnishings), so the ~2.0% used for recreation alone is
  carried over from a separate CPBRD factsheet using 2021 FIES data.
  Flagged with a `*` and its own tooltip wherever it appears in the UI.
- **Geographic granularity, same conclusion as sections 2-4 above**:
  this is one figure for the whole city, identical across all 76
  barangays -- there is no barangay-level household spending dataset
  published anywhere in the Philippines. The map is explicit about this
  ("Tarlac City avg., not barangay-specific") rather than implying
  household spending is measured or varies per barangay.

## 7b. Competitor counts are SME-scale only (micro businesses excluded)

Worth knowing when you read any competitor figure in this system: it
counts **SME-scale establishments only**. Under the Philippine MSME
definition (RA 9501/DTI, by asset size) a micro enterprise holds up to
PHP 3,000,000 in assets — the sari-sari store in a front window, the
carinderia with four tables, the market stall, the food cart. Those are
real businesses, but they are not the competition an SME planning a PHP
500k–5M entry is sizing itself against, and including them made every
barangay look saturated for reasons unrelated to SME-scale competition.

How the exclusion works, and its honest limits:

- Google Places has **no "micro enterprise" flag**, and no reliable
  place TYPE for it either — a sari-sari store and a 7-Eleven can both
  come back as `convenience_store`. So the filter matches on the
  place's own NAME, using the vocabulary these establishments actually
  label themselves with (`MICRO_BUSINESS_PATTERNS` in
  `app/services/places_service.py`).
- That makes it **deliberately conservative**: a pattern is only listed
  when a match is near-certain to be micro. The cost is that a micro
  business with a generic name ("JM Store") is still counted; the
  benefit is that a genuine SME is essentially never dropped by
  mistake. If you need the figure both ways for your defense, pass
  `exclude_micro=False` to `search_competitors()`.
- **Every exclusion is recorded**, not silently dropped —
  `search_competitors_detailed()` returns them and `/api/places/nearby`
  exposes them, so you can show exactly which establishments were left
  out of a barangay's count.
- The rows already in the database were cleaned once by
  `app/services/industry_migration.py` — micro-only rows deleted,
  everything else remapped onto the PSIC sections — with every change
  copied into `industry_migration_log` first and an `undo()` that
  restores them.

## 8. Sources referenced in this file

- PSA Philippine Standard Geographic Code (PSGC), Barangays of Tarlac
  City (names, 2024 population, PSA's own urban/rural classification):
  <https://psa.gov.ph/classification/psgc/barangays/0306916000>
- Tarlac City official government website, individual barangay profile
  pages (land area, each barangay's own classification text):
  tarlaccity.gov.ph/<barangay-slug>/ (76 individual pages; slugs mostly
  follow the lowercased barangay name, with some exceptions found during
  this research: Alvindia Segundo → `/alvindia/`, Dela Paz →
  `/de-lapaz/`, Matadero → `/san-juan-bautista/`, and the five
  Santa/Santo barangays use `sta-`/`sto-` abbreviated slugs, e.g.
  `/sta-cruz/`, `/sto-nino/`)
- DTI Cities and Municipalities Competitiveness Index, Tarlac provincial
  profile: <https://cmci.dti.gov.ph/prov-profile.php?prov=Tarlac>
- PSA Census/Annual Survey of Philippine Business and Industry (public
  granularity limitations): <https://psa.gov.ph/statistics/census/business-and-industry/index>,
  <https://psada.psa.gov.ph/catalog/CPBI/about>
- PSA Region III local office: <https://rsso03.psa.gov.ph/tarlac>
- BIR Zonal Values (starting point for RDO 17A -- Tarlac City):
  <https://www.bir.gov.ph/zonal-values>
- PSA Region III, 2023 Special Release: Family Income and Expenditure
  Survey (Tarlac province's real average annual family income/
  expenditure):
  <https://rsso03.psa.gov.ph/sites/default/files/2024-09/2024-SRFIES-2023-011.pdf>
- PSA, 2023 FIES Infographics (national percent-share expenditure
  breakdown by category):
  <https://psa.gov.ph/sites/default/files/infographics/2023%20FIES%20Infographics_0.pdf>
- CPBRD, "Consumption Patterns Among Filipino Households, 2021"
  factsheet (recreation-specific share estimate, used only because the
  2023 release doesn't break recreation out separately):
  <https://econgress.gov.ph/wp-content/uploads/publications/FF2022-71%20Consumption%20Patterns%20Among%20Fil%20Households,%202021.pdf>
- PSA, 2018 FIES press release (checked for a category breakdown; only
  food-share-by-income-decile was found there):
  <https://psa.gov.ph/statistics/income-expenditure/fies/node/144731>
- DTI, "2023 Philippine MSME Statistics in Brief" (the national
  establishment counts behind the Trend Reports back-projection --
  section 6b above): <https://www.dti.gov.ph/resources/msme-statistics/>
- PSA, List of Establishments (the frame DTI's MSME counts are built
  on): <https://psa.gov.ph/statistics/list-establishments>
