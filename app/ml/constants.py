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
OTHER_INDUSTRY_ENCODING = len(BUSINESS_TYPES)

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

INDUSTRY_DISPLAY = {
    "Agriculture, Forestry, and Fishing": {
        "icon": "🌾", "short": "Agriculture & Fishing",
        "subtitle": "farms, agri-processing, fisheries",
        "bi": "bi-tree-fill", "hue": "green",
    },
    "Mining and Quarrying": {
        "icon": "⛏️", "short": "Mining & Quarrying",
        "subtitle": "quarries, aggregates, sand & gravel",
        "bi": "bi-minecart-loaded", "hue": "stone",
    },
    "Manufacturing": {
        "icon": "🏭", "short": "Manufacturing",
        "subtitle": "food processing, furniture, garments, printing",
        "bi": "bi-gear-wide-connected", "hue": "slate",
    },
    "Electricity, Gas, Steam, and Air Conditioning Supply": {
        "icon": "⚡", "short": "Electricity & Gas",
        "subtitle": "power distribution, LPG/gas supply",
        "bi": "bi-lightning-charge-fill", "hue": "yellow",
    },
    "Water Supply; Sewerage, Waste Management, and Remediation Activities": {
        "icon": "💧", "short": "Water & Waste",
        "subtitle": "water supply, waste collection, septic services",
        "bi": "bi-droplet-fill", "hue": "sky",
    },
    "Construction": {
        "icon": "🏗️", "short": "Construction",
        "subtitle": "contractors, builders, specialty trades",
        "bi": "bi-cone-striped", "hue": "orange",
    },
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles": {
        "icon": "🛒", "short": "Wholesale & Retail Trade",
        "subtitle": "groceries, hardware, agri-supply, auto repair",
        "bi": "bi-shop", "hue": "blue",
    },
    "Transportation and Storage": {
        "icon": "🚚", "short": "Transport & Storage",
        "subtitle": "trucking, courier, warehousing, terminals",
        "bi": "bi-truck", "hue": "indigo",
    },
    "Accommodation and Food Service Activities": {
        "icon": "🏨", "short": "Accommodation & Food Service",
        "subtitle": "hotels, inns, restaurants, catering",
        "bi": "bi-building", "hue": "violet",
    },
    "Food and Beverage": {
        "icon": "🍽️", "short": "Food & Beverage",
        "subtitle": "bakeshops, cafes, beverage production",
        "bi": "bi-cup-hot-fill", "hue": "rose",
    },
    "Information and Communication": {
        "icon": "💻", "short": "Information & Communication",
        "subtitle": "IT services, telecoms, publishing, media",
        "bi": "bi-pc-display", "hue": "cyan",
    },
    "Financial and Insurance Activities": {
        "icon": "💰", "short": "Financial & Insurance",
        "subtitle": "lending, insurance, pawnshops, remittance",
        "bi": "bi-bank", "hue": "emerald",
    },
    "Real Estate Activities": {
        "icon": "🏠", "short": "Real Estate",
        "subtitle": "brokerage, leasing, property management",
        "bi": "bi-house-door-fill", "hue": "teal",
    },
    "Professional, Scientific, and Technical Activities": {
        "icon": "💼", "short": "Professional & Technical",
        "subtitle": "legal, accounting, engineering, consulting",
        "bi": "bi-briefcase-fill", "hue": "brown",
    },
    "Administrative and Support Service Activities": {
        "icon": "🗂️", "short": "Administrative & Support",
        "subtitle": "manpower, security, travel agencies, cleaning",
        "bi": "bi-clipboard-check-fill", "hue": "lime",
    },
    "Education": {
        "icon": "🎓", "short": "Education",
        "subtitle": "schools, review & tutorial centers, training",
        "bi": "bi-mortarboard-fill", "hue": "purple",
    },
    "Human Health and Social Work Activities": {
        "icon": "🏥", "short": "Health & Social Work",
        "subtitle": "clinics, pharmacies, diagnostic labs, care homes",
        "bi": "bi-heart-pulse-fill", "hue": "red",
    },
    "Arts, Entertainment, and Recreation": {
        "icon": "🎭", "short": "Arts & Recreation",
        "subtitle": "gyms, event venues, sports & amusement",
        "bi": "bi-palette-fill", "hue": "fuchsia",
    },
    "Other Service Activities": {
        "icon": "⚙️", "short": "Other Services",
        "subtitle": "salons, laundry, repair shops, personal services",
        "bi": "bi-scissors", "hue": "pink",
    },
    "Activities of Households as Employers": {
        "icon": "🏡", "short": "Household Employers",
        "subtitle": "household staffing and domestic services",
        "bi": "bi-house-heart-fill", "hue": "amber",
    },
}
DEFAULT_INDUSTRY_DISPLAY = {"icon": "📊", "short": "", "subtitle": "", "bi": "bi-bar-chart-fill", "hue": "neutral"}


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


FEATURE_NAMES = [
    "competitor_count",
    "population_density",
    "foot_traffic_index",
    "average_rent",
    "historical_success_rate",
    "business_density",
    "years_in_operation",
    "industry_type_encoded",
]

CLUSTER_LABELS_ORDERED = ["Low", "Moderate", "High", "Saturated"]

CLUSTER_THRESHOLDS = [25.0, 50.0, 75.0, 100.0]

N_CLUSTERS = 4
RANDOM_STATE = 42
N_ESTIMATORS = 100


OPERATING_DAYS_PER_MONTH = 26

DEFAULT_DAILY_WAGE_PHP = 590.0

DAILY_WAGE_RANGE = (1.0, 100_000.0)

DEFAULT_GROSS_MARGIN = 0.40
GROSS_MARGIN_RANGE = (0.05, 0.95)

PURCHASES_PER_RESIDENT_PER_DAY = 1.0 / 7.0

EXPERIENCE_FULL_YEARS = 5

STAFF_FULL_CAPACITY = 3

MINIMUM_RAMP_MONTHS = 3

MONTHLY_FIXED_COST_FLOOR_PHP = 1000.0

ITEM_PRICE_RANGE_PHP = (0.01, 10_000_000.0)

PLAN_INPUT_CEILING = 1e12

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

PLAN_FEATURE_NAMES = [
    "market_saturation",
    "residents_per_business",
    "monthly_fixed_cost",
    "capital",
    "capital_runway_months",
    "ramp_up_months",
    "employee_count",
    "is_existing",
    "years_in_operation",
    "priced_item_count",
    "average_price",
    "required_daily_sales",
    "has_offering_description",
    "has_innovation_idea",
]

N_PLAN_SAMPLES = 6000
PLAN_RANDOM_STATE = 4242


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

_PSIC_DIVISION_RANGES = [
    ((1, 3), "A"), ((5, 9), "B"), ((10, 33), "C"), ((35, 35), "D"),
    ((36, 39), "E"), ((41, 43), "F"), ((45, 47), "G"), ((49, 53), "H"),
    ((55, 56), "I"), ((58, 63), "J"), ((64, 66), "K"), ((68, 68), "L"),
    ((69, 75), "M"), ((77, 82), "N"), ((85, 85), "P"), ((86, 88), "Q"),
    ((90, 93), "R"), ((94, 96), "S"), ((97, 98), "T"),
]

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

    exact = _SECTION_BY_NAME.get(text.casefold())
    if exact:
        return exact

    if len(text) == 1 and text.upper() in _PSIC_SECTION_LETTERS:
        return _PSIC_SECTION_LETTERS[text.upper()]

    digits = "".join(character for character in text if character.isdigit())
    if digits:
        division = int(digits[:2])
        for (low, high), letter in _PSIC_DIVISION_RANGES:
            if low <= division <= high:
                return _PSIC_SECTION_LETTERS[letter]
        return None

    lowered = text.casefold()
    for phrase, section in _TRADE_PHRASES:
        if phrase in lowered:
            return section

    return None
