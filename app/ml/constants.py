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
