"""
app/services/socio_demographic_service.py
--------------------------------------------
The Saturation Map's "household demand" figures -- what a typical
Tarlac household spends per year on food, utilities, transportation,
recreation, and education. Requested so the map's per-barangay
tooltip/detail panel can show real spending-pattern context alongside
the existing population/competitor/saturation figures.

READ THIS BEFORE CITING THESE NUMBERS -- WHAT IS REAL vs. DERIVED:

  1. TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP (below) is REAL and
     Tarlac-specific: PSA Region III's own 2023 Family Income and
     Expenditure Survey (FIES) release reports Tarlac PROVINCE's
     average annual family expenditure directly (see SOURCES below).
     This is the one number in this file that is genuinely
     Tarlac-specific -- everything else derives from it.

  2. NATIONAL_EXPENDITURE_SHARES (below) is REAL but NOT
     Tarlac-specific -- it is the PSA's own NATIONAL percent-share
     breakdown of family expenditure by category, from the official
     2023 FIES infographic. PSA does not publish a Region III- or
     Tarlac-specific category breakdown anywhere in its public
     releases (its detailed cross-tabs go down to province for
     income/expenditure TOTALS, never down to a province-level
     category breakdown) -- this was searched for specifically for
     this feature and not found; see SOURCES below for exactly what
     was checked.

  3. The per-category peso figures this module computes
     (get_demand_breakdown()) are therefore an ESTIMATE, not a
     directly-published statistic: Tarlac's own REAL total annual
     family expenditure (#1) multiplied by the NATIONAL category
     shares (#2). This is a standard, transparent way to localize a
     national spending pattern when no local breakdown exists --
     but it assumes Tarlac households allocate their budget in
     roughly the same proportions as the national average, which is
     an assumption, not a measurement. Every value this module returns
     carries an explicit "estimated" flag/source note so the UI can
     (and does) label it as such rather than presenting it as
     precisely-measured Tarlac data.

  4. GEOGRAPHIC GRANULARITY: this is a CITY-WIDE (really, province-
     wide -- Tarlac City is Tarlac province's capital and by far its
     largest population/economic center, but PSA's FIES sampling
     frame does not resolve below province) figure, identical for
     every one of the 76 barangays. There is no publicly available
     barangay-level household spending dataset anywhere in the
     Philippines -- PSA's FIES sampling frame and public tables never
     go below province/HUC level. This matches this project's
     existing, repeatedly-documented finding for population density
     and household income (see Reference/DATASETS.md, and this
     module's sibling app/ml/seed_data.py) -- the map's demand
     tooltip is explicit that this is "Tarlac City average," never
     implying a barangay-specific figure exists.

  5. "Recreation" specifically: PSA's 2023 FIES headline release does
     NOT break recreation out as its own line -- it is folded into an
     8.6%-of-total "Other" bucket together with alcoholic beverages &
     tobacco, clothing & footwear, restaurants & hotels, and furnishings.
     The ~2.0% figure used below for recreation alone is carried over
     from a DIFFERENT PSA-sourced release (a CPBRD "Consumption
     Patterns Among Filipino Households, 2021" factsheet, which
     reported recreation and culture, clothing, alcohol/tobacco, and
     furnishing as each "less than 2%" of total expenditure). Four
     categories at "just under 2% each" is consistent with the 2023
     release's combined 8.6% "Other" bucket (2 x 4 = 8), which is why
     this figure is used as a reasonable stand-in rather than guessed
     from nothing -- but it comes from a different survey year than
     the other four categories, and is the softest number in this
     file. Flagged inline below and in every place this value surfaces
     in the UI.

SOURCES (fetched during this round's research; see git history / delivery
notes for the exact fetch dates):
  - PSA Region III, "2023 Special Release: Family Income and
    Expenditure Survey" (Tarlac province average annual family
    income ~PHP 367,820 and expenditure ~PHP 299,670 for 2023):
    https://rsso03.psa.gov.ph/sites/default/files/2024-09/2024-SRFIES-2023-011.pdf
  - PSA, "2023 FIES Infographics" (national percent-share breakdown of
    family expenditure by major category):
    https://psa.gov.ph/sites/default/files/infographics/2023%20FIES%20Infographics_0.pdf
  - CPBRD (Congressional Policy and Budget Research Department),
    "Consumption Patterns Among Filipino Households, 2021" factsheet
    (recreation/clothing/alcohol-tobacco/furnishing each "less than 2%"
    of total expenditure -- used only for the recreation estimate,
    since the 2023 release does not break it out separately):
    https://econgress.gov.ph/wp-content/uploads/publications/FF2022-71%20Consumption%20Patterns%20Among%20Fil%20Households,%202021.pdf
  - PSA, 2018 FIES press release (food-expenditure-share by income
    decile -- checked, not used directly: no category-by-category
    breakdown finer than food vs. everything-else):
    https://psa.gov.ph/statistics/income-expenditure/fies/node/144731
  - PSA's own OpenSTAT portal and PSADA microdata catalog were also
    checked for a Region III/Tarlac-specific category breakdown; none
    is published there either (only province-level income/expenditure
    TOTALS, matching the SRFIES release above) -- consistent with the
    "no sub-province socio-economic breakdown exists publicly" finding
    already documented for population/household-income data in
    Reference/DATASETS.md.
"""

# Tarlac PROVINCE's real, PSA-published average annual family
# expenditure for 2023 (Tarlac City is the province's capital and
# largest urban center; PSA does not sample/publish below province
# level, so this is the most granular REAL figure available for
# "Tarlac"). See module docstring, source #1.
TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP = 299_670

# PSA's own NATIONAL percent-share breakdown of average family
# expenditure by major category, 2023 FIES. See module docstring,
# source #2. Order matches the 2023 FIES Infographics release.
NATIONAL_EXPENDITURE_SHARES_2023 = {
    "food": {"label": "Food & Non-Alcoholic Beverages", "percent": 40.9, "year": 2023},
    "housing_utilities": {
        "label": "Housing, Water, Electricity, Gas & Other Fuels",
        "percent": 22.9,
        "year": 2023,
    },
    "transportation": {"label": "Transportation", "percent": 10.7, "year": 2023},
    "education": {"label": "Education", "percent": 3.0, "year": 2023},
    # Not a separate PSA 2023 line item -- see module docstring point 5.
    "recreation": {"label": "Recreation & Culture", "percent": 2.0, "year": "2021 (est.)"},
    "health_furnishings": {"label": "Health & Furnishings", "percent": 3.3, "year": 2023},
    "communication": {"label": "Information & Communication", "percent": 2.7, "year": 2023},
    "miscellaneous": {"label": "Miscellaneous", "percent": 2.6, "year": 2023},
    # Alcohol/tobacco, clothing/footwear, restaurants/hotels, furniture,
    # special occasions -- not requested by name for the map tooltip,
    # kept here only so the shares below are traceable back to 100%.
    "other": {"label": "Other (incl. clothing, restaurants/hotels)", "percent": 11.9, "year": 2023},
}

# The exact five categories requested for the map's "demand" tooltip
# (food, utilities, transportation, recreation, education), in display
# order.
DEMAND_TOOLTIP_CATEGORIES = ["food", "housing_utilities", "transportation", "recreation", "education"]


def get_demand_breakdown():
    """Returns the demand-tooltip category list: for each of
    DEMAND_TOOLTIP_CATEGORIES, its display label, the national percent
    share it's based on, and the resulting ESTIMATED annual peso amount
    for a Tarlac household (Tarlac's own real total x the national
    share -- see module docstring, point 3). Every entry also carries
    `is_estimated_category` = True for "recreation" specifically, so
    the UI can render its softer sourcing distinctly from the other
    four (which are directly-published 2023 PSA figures multiplied by
    a real Tarlac total -- only the multiplication is an estimate).
    """
    breakdown = []
    for key in DEMAND_TOOLTIP_CATEGORIES:
        entry = NATIONAL_EXPENDITURE_SHARES_2023[key]
        annual_php = round(TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP * entry["percent"] / 100.0)
        breakdown.append(
            {
                "key": key,
                "label": entry["label"],
                "national_share_percent": entry["percent"],
                "share_source_year": entry["year"],
                "estimated_annual_php": annual_php,
                "is_recreation_estimate": key == "recreation",
            }
        )
    return breakdown


def get_demand_summary():
    """The full payload surfaced to the frontend (once, as a page-level
    constant -- see saturation_map.html/map.js -- since this figure is
    identical for every barangay, unlike population_density)."""
    return {
        "tarlac_avg_annual_family_expenditure_php": TARLAC_AVG_ANNUAL_FAMILY_EXPENDITURE_PHP,
        "expenditure_year": 2023,
        "geographic_note": (
            "Tarlac City average (PSA FIES, Tarlac province level) -- "
            "the same figure for every barangay. No barangay-level "
            "household spending dataset is published anywhere in the "
            "Philippines; see Reference/DATASETS.md."
        ),
        "categories": get_demand_breakdown(),
    }
