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
