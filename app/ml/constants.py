"""
app/ml/constants.py
---------------------
Shared constants that BOTH the training script (train_model.py) and the
live inference code (services/forecasting_service.py) must agree on.
Keeping them in one file prevents the classic bug where the encoding
used to train a model silently drifts from the encoding used to call it.

Updated to match your real dss_db schema: there is no urban_rural_flag
or household_income column anywhere in the given tables, so those two
features from the original paper draft are gone. In their place we use
the columns that actually exist on `market_data` and `lgu_data`
(foot_traffic_index, average_rent, business_density) plus the SME's own
`years_in_operation` (derived from sme_profile.registration_date) -- a
richer, fully real feature vector.
"""

# ---------------------------------------------------------------------
# INDUSTRY TAXONOMY -- PSIC top-level sections
# ---------------------------------------------------------------------
# These are the industry sections of the Philippine Standard Industrial
# Classification (PSIC), the same sections PSA itself publishes business
# and employment statistics under. Using PSIC sections (rather than an
# invented list) means this system's industry axis lines up with the
# official statistics an LGU or DTI office already reports in, which is
# what makes a claim like "Accommodation and Food Service is saturated
# in this barangay" checkable against a real, published figure.
#
# SCOPE NOTE -- this system targets SMEs, not micro businesses.
# An earlier version of this list carried 22 extra hyper-local
# MICRO-business categories (sari-sari stores, carinderias, food carts,
# piso wifi, market stalls, backyard livestock...). Those were removed:
# a micro retailer operating out of a front window or a market stall is
# not the unit this DSS plans for, and mixing them into competitor
# counts made an SME's real competition look far denser than it is.
# The two mechanisms that enforce that scope:
#   1. app/services/places_service.py's MICRO_BUSINESS_PATTERNS filter,
#      which drops micro establishments out of live Google Places
#      results before they are ever counted; and
#   2. app/services/industry_migration.py, which remapped every
#      pre-existing row onto the sections below and deleted the
#      micro-only rows (one time, on startup, with a backup table).
#
# industry_type in the DB is free text (VARCHAR), so this list is the
# UI's offered options / the ML encoding's known vocabulary -- anything
# typed outside it still works, it just encodes as the generic "other"
# bucket (see BUSINESS_TYPE_ENCODING.get(x, OTHER_INDEX) at call sites).
BUSINESS_TYPES = [
    "Agriculture, Forestry, and Fishing",
    "Mining and Quarrying",
    "Manufacturing",
    "Electricity, Gas, Steam, and Air Conditioning Supply",
    "Water Supply; Sewerage, Waste Management, and Remediation Activities",
    "Construction",
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "Transportation and Storage",
    "Accommodation and Food Service Activities",
    "Food and Beverage",
    "Information and Communication",
    "Financial and Insurance Activities",
    "Real Estate Activities",
    "Professional, Scientific, and Technical Activities",
    "Administrative and Support Service Activities",
    "Education",
    "Human Health and Social Work Activities",
    "Arts, Entertainment, and Recreation",
    "Other Service Activities",
    "Activities of Households as Employers",
]
BUSINESS_TYPE_ENCODING = {name: idx for idx, name in enumerate(BUSINESS_TYPES)}
OTHER_INDUSTRY_ENCODING = len(BUSINESS_TYPES)  # bucket for any industry_type not in the list above

# A small subset "featured" on the SME Home page's live-score cards, so
# that page stays fast (each card computes a real AI score on load) and
# visually matches the original storyboard's card layout. Chosen as the
# sections a Tarlac City SME actually registers under most often. Every
# section above -- not just these -- is selectable everywhere else (the
# "+ New Business Plan" modal, the search bar, the Saturation Map
# filter). Feel free to add more here; each one costs one extra
# real-time score computation on every Home page load.
FEATURED_BUSINESS_TYPES = [
    "Food and Beverage",
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "Accommodation and Food Service Activities",
    "Other Service Activities",
    "Construction",
    "Manufacturing",
    "Information and Communication",
    "Human Health and Social Work Activities",
]

# Emoji icon + short example subtitle + SHORT display label per section.
# `short` exists because several PSIC section names are long enough to
# break a card or a legend ("Wholesale and Retail Trade; Repair of Motor
# Vehicles and Motorcycles" is 68 characters) -- the full, official name
# is still what's stored in the database and shown wherever there's room;
# `short` is only for tight UI (industry cards, chart legends, pills).
# See short_industry_label() below.
INDUSTRY_DISPLAY = {
    "Agriculture, Forestry, and Fishing": {
        "icon": "🌾", "short": "Agriculture & Fishing",
        "subtitle": "farms, agri-processing, fisheries",
    },
    "Mining and Quarrying": {
        "icon": "⛏️", "short": "Mining & Quarrying",
        "subtitle": "quarries, aggregates, sand & gravel",
    },
    "Manufacturing": {
        "icon": "🏭", "short": "Manufacturing",
        "subtitle": "food processing, furniture, garments, printing",
    },
    "Electricity, Gas, Steam, and Air Conditioning Supply": {
        "icon": "⚡", "short": "Electricity & Gas",
        "subtitle": "power distribution, LPG/gas supply",
    },
    "Water Supply; Sewerage, Waste Management, and Remediation Activities": {
        "icon": "💧", "short": "Water & Waste",
        "subtitle": "water supply, waste collection, septic services",
    },
    "Construction": {
        "icon": "🏗️", "short": "Construction",
        "subtitle": "contractors, builders, specialty trades",
    },
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles": {
        "icon": "🛒", "short": "Wholesale & Retail Trade",
        "subtitle": "groceries, hardware, agri-supply, auto repair",
    },
    "Transportation and Storage": {
        "icon": "🚚", "short": "Transport & Storage",
        "subtitle": "trucking, courier, warehousing, terminals",
    },
    "Accommodation and Food Service Activities": {
        "icon": "🏨", "short": "Accommodation & Food Service",
        "subtitle": "hotels, inns, restaurants, catering",
    },
    "Food and Beverage": {
        "icon": "🍽️", "short": "Food & Beverage",
        "subtitle": "bakeshops, cafes, beverage production",
    },
    "Information and Communication": {
        "icon": "💻", "short": "Information & Communication",
        "subtitle": "IT services, telecoms, publishing, media",
    },
    "Financial and Insurance Activities": {
        "icon": "💰", "short": "Financial & Insurance",
        "subtitle": "lending, insurance, pawnshops, remittance",
    },
    "Real Estate Activities": {
        "icon": "🏠", "short": "Real Estate",
        "subtitle": "brokerage, leasing, property management",
    },
    "Professional, Scientific, and Technical Activities": {
        "icon": "💼", "short": "Professional & Technical",
        "subtitle": "legal, accounting, engineering, consulting",
    },
    "Administrative and Support Service Activities": {
        "icon": "🗂️", "short": "Administrative & Support",
        "subtitle": "manpower, security, travel agencies, cleaning",
    },
    "Education": {
        "icon": "🎓", "short": "Education",
        "subtitle": "schools, review & tutorial centers, training",
    },
    "Human Health and Social Work Activities": {
        "icon": "🏥", "short": "Health & Social Work",
        "subtitle": "clinics, pharmacies, diagnostic labs, care homes",
    },
    "Arts, Entertainment, and Recreation": {
        "icon": "🎭", "short": "Arts & Recreation",
        "subtitle": "gyms, event venues, sports & amusement",
    },
    "Other Service Activities": {
        "icon": "⚙️", "short": "Other Services",
        "subtitle": "salons, laundry, repair shops, personal services",
    },
    "Activities of Households as Employers": {
        "icon": "🏡", "short": "Household Employers",
        "subtitle": "household staffing and domestic services",
    },
}
DEFAULT_INDUSTRY_DISPLAY = {"icon": "📊", "short": "", "subtitle": ""}


# The three sections broken out by name on the Saturation Map's detail
# panel and the SME's personal Trend Report ("Food Industry", "Service
# Industry", "Retail Industry" on the storyboard). Defined once here so
# the two pages can never drift apart, and so renaming a section is a
# one-line change rather than a hunt through two services.
DETAIL_PANEL_SECTIONS = {
    "food": "Food and Beverage",
    "service": "Other Service Activities",
    "retail": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
}


def short_industry_label(industry_type):
    """The compact label for tight UI (cards, legends, pills). Falls
    back to the full name for anything not in INDUSTRY_DISPLAY -- an
    SME may type a free-text industry outside the PSIC sections above,
    and that text is shown as they typed it."""
    entry = INDUSTRY_DISPLAY.get(industry_type)
    if not entry:
        return industry_type
    return entry.get("short") or industry_type


# Feature order used for the Random Forest Regressor. Every one of these
# comes straight from a real column in market_data / lgu_data /
# sme_profile -- see app/services/forecasting_service.py build_feature_vector().
FEATURE_NAMES = [
    "competitor_count",        # market_data.competitor_count
    "population_density",      # market_data.population_density
    "foot_traffic_index",      # market_data.foot_traffic_index
    "average_rent",            # market_data.average_rent
    "historical_success_rate", # market_data.historical_success_rate (0-1)
    "business_density",        # lgu_data.business_density
    "years_in_operation",      # derived from sme_profile.registration_date
    "industry_type_encoded",   # BUSINESS_TYPE_ENCODING[...] / OTHER_INDUSTRY_ENCODING
]

CLUSTER_LABELS_ORDERED = ["Low", "Moderate", "High", "Saturated"]

# Saturation Index (0-100) cut points used to label a forecast Low/
# Moderate/High/Saturated WITHOUT a stored cluster_label column (the
# given forecast_result table doesn't have one) -- see
# app/models/forecast_result.py's cluster_label property, which is
# what every page actually displays. train_model.py ALSO runs a
# K-Means (K=4) pass (per the paper's stated methodology) and reports
# its own cluster-to-label mapping in training_report.json, but that
# mapping is for your evaluation chapter only -- it is intentionally
# NOT wired into these fixed thresholds, since deriving the label
# straight from the Random Forest's own 0-100 prediction is simpler
# and keeps every cluster boundary on the same scale as
# saturation_alert_threshold in system_settings.
CLUSTER_THRESHOLDS = [25.0, 50.0, 75.0, 100.0]  # <=25 Low, <=50 Moderate, <=75 High, else Saturated

N_CLUSTERS = 4
RANDOM_STATE = 42
N_ESTIMATORS = 100  # "T = 100" per the paper's Random Forest formula


# =====================================================================
# STAGE 2 -- THE PLAN VIABILITY MODEL
# =====================================================================
# Stage 1 (FEATURE_NAMES above) answers "how crowded is this market?"
# and nothing else: two SMEs opening the same kind of business in the
# same barangay get the same Market Saturation Index whether one has
# P5,000,000 behind it and the other P20,000, whether one hires ten
# people and the other runs it alone. That is correct for a market
# measure and wrong for a forecast of a PLAN.
#
# Stage 2 takes stage 1's output as one input among fourteen and adds
# every business parameter the owner actually typed in (capital, staff,
# stage, price list, offering, idea) to predict a Plan Viability Index
# (0-100). The full formula -- every input, every derived quantity,
# every weight below -- is written out in Reference/FORECAST_MODEL.md;
# app/ml/plan_model.py is the code those words describe.
#
# The constants live HERE, next to stage 1's, for the same reason
# FEATURE_NAMES does: the training script and the live service both
# read them, and two copies would drift.

# A Philippine retail or service shop typically trades six days a week:
# 52 weeks x 6 days / 12 months = 26 working days a month. (DOLE's
# equivalent-monthly-rate factor for six-day workers is 313/12 = 26.08;
# 26 is the same figure rounded to whole days.)
OPERATING_DAYS_PER_MONTH = 26

# The daily wage one employee costs. DOLE Wage Order No. RBIII-26 (2nd
# tranche, effective 16 April 2026) sets Tarlac's minimum at P590/day
# for retail and service establishments (P600 for other
# non-agriculture). Payroll here is employees x wage x 26 working days --
# a minimum-wage floor, deliberately: it is the one labour cost every
# plan is legally bound to, and anything above it is the owner's choice.
# An Admin can change it (System Settings > Plan forecast assumptions,
# SystemSetting `plan_daily_wage_php`) when the next wage order lands,
# without retraining -- the model reads wage only through monthly fixed
# cost and capital runway, so a new wage moves the forecast through
# those features exactly as a rent change would.
DEFAULT_DAILY_WAGE_PHP = 590.0

# The range an Admin's daily wage is accepted in, both when it is saved
# (admin_controller._save_plan_assumptions) and when it is read back
# (plan_forecast_service.plan_assumptions -- a value outside it falls
# back to the default above). "Greater than zero" alone was not a bound:
# a wage of 1e37 passed it, overflowed the forest's float32 input on
# every plan with staff, and took the Home page down for all of them.
# P1-P100,000 a day is far wider than any Philippine wage order and
# still a finite, plausible number of pesos.
DAILY_WAGE_RANGE = (1.0, 100_000.0)

# Share of each sale left after paying for the goods sold. 40% is an
# ASSUMPTION -- a middle-of-the-road figure for food service and retail
# -- not a measurement of any plan: the app does not collect a cost of
# goods. It is used only to turn the owner's own prices into "sales a
# day needed to cover fixed costs". Admin-overridable (SystemSetting
# `plan_gross_margin`, accepted range 0.05-0.95).
DEFAULT_GROSS_MARGIN = 0.40
GROSS_MARGIN_RANGE = (0.05, 0.95)

# The demand ceiling a price list is checked against: every resident a
# business can expect to serve buys from it at most ONCE A WEEK. A
# generous ceiling on purpose -- if a plan needs more sales a day than
# even this allows, no realistic share of the market will carry it.
PURCHASES_PER_RESIDENT_PER_DAY = 1.0 / 7.0

# Operating experience reaches full marks after this many years in
# business; an existing business starts at half marks on day one (it
# already has premises, suppliers and customers a plan does not).
EXPERIENCE_FULL_YEARS = 5

# Staffing capacity reaches full marks at this many employees (the
# owner alone scores 1/(3+1) = 0.25). The COST of staff is already in
# capital adequacy through payroll -- the trade-off between the two is
# intended: hiring helps capacity and costs runway.
STAFF_FULL_CAPACITY = 3

# No new business earns its steady-state revenue in month one: there is
# fit-out, permits, and the weeks it takes for customers to find you.
# The ramp-up period (and the ROI window built from it, see
# location_opportunity_service.estimate_roi_timeframe) never reports
# faster than this, however favourable the model's scores look. Moved
# here from location_opportunity_service so stage 2 and the ROI window
# share one value.
MINIMUM_RAMP_MONTHS = 3

# Monthly fixed cost is never treated as less than this. Rent comes
# from market_data and is always positive in practice, but a zero there
# (a hand-edited row) would make capital runway infinite, which is not
# a forecast, it is a division by zero.
MONTHLY_FIXED_COST_FLOOR_PHP = 1000.0

# A price-list entry counts as a PRICE POINT only inside this range
# (plan_model.price_list_summary). Below a centavo is not a price
# anything can be sold at -- and it would make required daily sales
# overflow -- and above P10,000,000 is not an SME menu price, it is a
# typo (or "1e39", which the browser's number input happily accepts).
# An entry outside the range still tells the model the offering is
# described; it just does not enter the average price.
ITEM_PRICE_RANGE_PHP = (0.01, 10_000_000.0)

# The largest value any single stage-2 input is read as (capital, rent,
# employees, population...). A guard, not an assumption: it sits five
# orders of magnitude above anything a plan or a barangay can carry and
# far above the training range, so it never changes a real forecast. It
# exists because the forest reads its input as float32 (max ~3.4e38):
# one unbounded value -- a legacy row, a hand-edited setting -- used to
# raise inside predict() and turn a forecast into a 500 error. Non-finite
# values (NaN, infinity) are read as missing or as this ceiling. With
# every input at most 1e12, the largest derived quantity -- required
# daily sales, (1e12 + 1e12 x 1e12 x 26) / (0.01 x 0.05 x 26), about
# 2e27 -- is still eleven orders of magnitude inside float32.
PLAN_INPUT_CEILING = 1e12

# The scorecard -- seven feasibility-study aspects (Market, Financial,
# Technical/Operational, Product), each scored 0-1, weighted to 1.00.
# The weights are a STATED EDITORIAL CHOICE, documented in
# Reference/FORECAST_MODEL.md: the market keeps the largest share
# (stage 1 is still the best-evidenced signal the system has), capital
# is next (running out of money before customers arrive is the most
# common way a small business closes), and the rest share what is left.
PLAN_COMPONENT_WEIGHTS = {
    "market_opportunity": 0.40,
    "capital_adequacy": 0.20,
    "price_coverage": 0.10,
    "operating_experience": 0.08,
    "staffing": 0.07,
    "offering_definition": 0.07,
    "differentiation": 0.08,
}
assert abs(sum(PLAN_COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9

# RF2's input order -- the single source of truth for training
# (train_model.train_plan_model) and inference
# (services/plan_forecast_service.py). Both build the vector through
# app/ml/plan_model.plan_feature_vector(), and the trained bundle on
# disk records this list so a model trained on a different order is
# refused rather than silently misread.
PLAN_FEATURE_NAMES = [
    "market_saturation",         # MSI* from stage 1 (after the sub-category adjustment), 0-100
    "residents_per_business",    # population / (competitor_count + 1)
    "monthly_fixed_cost",        # rent + employees x daily wage x 26, PHP/month
    "capital",                   # PHP, the owner's own figure
    "capital_runway_months",     # capital / monthly_fixed_cost
    "ramp_up_months",            # months before the business pays for itself (plan_model.ramp_up_months)
    "employee_count",
    "is_existing",               # 0/1 from business_stage
    "years_in_operation",        # from registration_date, 0 for a startup
    "priced_item_count",         # price-list items with a price above zero, 0-30
    "average_price",             # PHP, mean of the listed prices; 0 when none
    "required_daily_sales",      # sales/day to cover fixed costs; 0 when no prices
    "has_offering_description",  # 0/1: product_offering text OR any price-list item
    "has_innovation_idea",       # 0/1
]

# Synthetic training set size for stage 2 and its own seed. A SEPARATE
# seed (rather than continuing stage 1's generator) is what lets
# `python -m app.ml.train_model --plan-only` reproduce exactly the model
# a full run would have trained.
N_PLAN_SAMPLES = 6000
PLAN_RANDOM_STATE = 4242


# =====================================================================
# READING AN LGU PERMIT REGISTER'S "WHAT TRADE IS THIS" COLUMN
# =====================================================================
# BUSINESS_TYPES above is the PSIC section list (minus Public
# Administration and Extraterritorial Organizations, which no SME
# registers under, plus "Food and Beverage", which this project carries
# separately because it is the most common SME category in the city).
# A real business-permit register does NOT contain those exact strings.
# Depending on which office exported it, the trade column holds a PSIC
# section LETTER ("G"), a numeric PSIC code ("47211"), or whatever the
# clerk typed ("Sari-sari Store", "coffee shop/cafe").
#
# All three have to land on one of the sections above, or the count is
# filed under an industry nothing else in the app scores. Anything that
# cannot be mapped confidently is deliberately counted NOWHERE: an
# uncounted permit understates one barangay, a MIScounted one corrupts
# the comparison between barangays, and comparing barangays is the
# whole basis of the recommendation engine.

# PSIC section letters. O and U are intentionally absent.
_PSIC_SECTION_LETTERS = {
    "A": "Agriculture, Forestry, and Fishing",
    "B": "Mining and Quarrying",
    "C": "Manufacturing",
    "D": "Electricity, Gas, Steam, and Air Conditioning Supply",
    "E": "Water Supply; Sewerage, Waste Management, and Remediation Activities",
    "F": "Construction",
    "G": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "H": "Transportation and Storage",
    "I": "Accommodation and Food Service Activities",
    "J": "Information and Communication",
    "K": "Financial and Insurance Activities",
    "L": "Real Estate Activities",
    "M": "Professional, Scientific, and Technical Activities",
    "N": "Administrative and Support Service Activities",
    "P": "Education",
    "Q": "Human Health and Social Work Activities",
    "R": "Arts, Entertainment, and Recreation",
    "S": "Other Service Activities",
    "T": "Activities of Households as Employers",
}

# PSIC division (the first TWO digits of any numeric code) -> section.
# A five-digit code like 47211 is division 47, which is section G.
# Ranges are inclusive and follow PSIC 2009's own section boundaries.
_PSIC_DIVISION_RANGES = [
    ((1, 3), "A"), ((5, 9), "B"), ((10, 33), "C"), ((35, 35), "D"),
    ((36, 39), "E"), ((41, 43), "F"), ((45, 47), "G"), ((49, 53), "H"),
    ((55, 56), "I"), ((58, 63), "J"), ((64, 66), "K"), ((68, 68), "L"),
    ((69, 75), "M"), ((77, 82), "N"), ((85, 85), "P"), ((86, 88), "Q"),
    ((90, 93), "R"), ((94, 96), "S"), ((97, 98), "T"),
]

# Free text a Tarlac City permit clerk actually writes. Matched against
# the lowercased cell, longest phrase first, so "internet cafe" is not
# caught by the bare "cafe" rule and filed under Food and Beverage.
_TRADE_PHRASES = [
    ("carinderia", "Food and Beverage"),
    ("eatery", "Food and Beverage"),
    ("restaurant", "Food and Beverage"),
    ("coffee", "Food and Beverage"),
    ("bakery", "Food and Beverage"),
    ("bakeshop", "Food and Beverage"),
    ("catering", "Food and Beverage"),
    ("canteen", "Food and Beverage"),
    ("food", "Food and Beverage"),
    ("cafe", "Food and Beverage"),
    ("lodging", "Accommodation and Food Service Activities"),
    ("hotel", "Accommodation and Food Service Activities"),
    ("resort", "Accommodation and Food Service Activities"),
    ("sari-sari", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("sari sari", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("grocery", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("wholesale", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("hardware", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("pharmacy", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("drugstore", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("trading", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("retail", "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"),
    ("water refilling", "Water Supply; Sewerage, Waste Management, and Remediation Activities"),
    ("internet cafe", "Information and Communication"),
    ("computer shop", "Information and Communication"),
    ("salon", "Other Service Activities"),
    ("barber", "Other Service Activities"),
    ("laundry", "Other Service Activities"),
    ("repair", "Other Service Activities"),
    ("printing", "Manufacturing"),
    ("construction", "Construction"),
    ("hauling", "Transportation and Storage"),
    ("trucking", "Transportation and Storage"),
    ("tricycle", "Transportation and Storage"),
    ("pawnshop", "Financial and Insurance Activities"),
    ("remittance", "Financial and Insurance Activities"),
    ("lending", "Financial and Insurance Activities"),
    ("apartment", "Real Estate Activities"),
    ("rental", "Real Estate Activities"),
    ("clinic", "Human Health and Social Work Activities"),
    ("dental", "Human Health and Social Work Activities"),
    ("tutorial", "Education"),
    ("school", "Education"),
    ("videoke", "Arts, Entertainment, and Recreation"),
    ("billiard", "Arts, Entertainment, and Recreation"),
]
# Longest first, so a phrase is never shadowed by a shorter one it
# contains. Sorted once at import rather than on every lookup.
_TRADE_PHRASES.sort(key=lambda pair: -len(pair[0]))

_SECTION_BY_NAME = {name.casefold(): name for name in BUSINESS_TYPES}


def canonical_industry_for(raw_value):
    """Map one permit register's trade cell onto a BUSINESS_TYPES
    entry, or None when it cannot be mapped confidently.

    None is a real answer here, not a failure -- see the note above on
    why a miscounted permit is worse than an uncounted one.
    """
    text = str(raw_value or "").strip()
    if not text:
        return None

    # 1. The exact section name, however it is cased.
    exact = _SECTION_BY_NAME.get(text.casefold())
    if exact:
        return exact

    # 2. A bare section letter. Guarded to a single character, so a
    #    one-letter cell reads as a section while "Agriculture" does
    #    not read as the letter A.
    if len(text) == 1 and text.upper() in _PSIC_SECTION_LETTERS:
        return _PSIC_SECTION_LETTERS[text.upper()]

    # 3. A numeric PSIC code: its first two digits are the division,
    #    and the division determines the section.
    digits = "".join(character for character in text if character.isdigit())
    if digits:
        division = int(digits[:2])
        for (low, high), letter in _PSIC_DIVISION_RANGES:
            if low <= division <= high:
                return _PSIC_SECTION_LETTERS[letter]
        return None

    # 4. Free text. Longest phrase first (see _TRADE_PHRASES).
    lowered = text.casefold()
    for phrase, section in _TRADE_PHRASES:
        if phrase in lowered:
            return section

    return None
