"""
app/services/upload_templates.py
----------------------------------
The Custom Template on Gov't Data Upload (V3.9).

MarketLens scores every barangay from six figures (the Random Forest's
inputs, which also make up the Market Saturation Index of Equation 10):

    competitor_count         MSI Competitor density   (w1 = 0.45)
    historical_success_rate  MSI Demand trend         (w2 = 0.35)
    population_density       MSI Socio-demographic    (w3 = 0.20)
    average_rent             MSI Socio-demographic    (w3 = 0.20)
    foot_traffic_index       model input
    business_density         model input

Several of them are estimates until an LGU uploads the real figure (see
app/ml/seed_data.py, "WHAT IS REAL vs. ESTIMATED"). The standard
templates ask for every column the importer reads; the Custom Template
lists the same columns as suggestions -- what each one feeds and where
MarketLens gets it today -- and lets the LGU office tick only the data
it can really provide. The required columns are always included, so a
custom template always imports. The choice is saved on the account
(user.upload_template) and the CSV is built from it.

The columns themselves, their importance and examples live in
lgu_controller.UPLOAD_COLUMNS (the same list the column guide and the
importer's documentation use); this module adds what each column feeds
and builds the custom selections.
"""

import csv
import io
import json

# What a template is for: the upload target its file goes to.
TEMPLATE_TARGETS = {"LGU_DATA": "LGU_DATA", "MARKET_DATA": "MARKET_DATA", "MARKET_COUNTS": "MARKET_DATA"}

TEMPLATE_TITLES = {
    "LGU_DATA": "Gov't / Zoning Data",
    "MARKET_DATA": "Market Data: business permit register",
    "MARKET_COUNTS": "Market Data: counts and area figures",
}

TEMPLATE_FILENAMES = {
    "LGU_DATA": "marketlens_zoning_data_template.csv",
    "MARKET_DATA": "marketlens_market_data_template.csv",
    "MARKET_COUNTS": "marketlens_market_counts_template.csv",
}

MODEL_INPUTS = {
    "competitor_count": "Competitor count",
    "historical_success_rate": "Business success rate",
    "population_density": "Population density",
    "average_rent": "Average rent",
    "foot_traffic_index": "Foot traffic",
    "business_density": "Business density",
}

# column: (what it feeds in MarketLens, where MarketLens gets it today,
#          suggested -- it feeds the scores, so a real figure makes them
#          more accurate)
FACTS = {
    "barangay": ("Links every figure to one of the 76 barangays", "", False),
    "zoning_info": ("Dataset records: the zoning of the barangay's business area",
                    "Placeholder until an LGU upload", False),
    "business_density": ("AI saturation model input", "Estimated until an LGU uploads it", True),
    "permit_count": ("Dataset records: permits in force, all trades together",
                     "Counted from the permit register", False),
    "closure_records": ("Dataset records: closures and non-renewals", "Counted from the permit register", False),
    "effective_date": ("Dates the zoning figures", "The upload date", False),
    "industry_type": ("Competitor count per industry: MSI Competitor density (w1 = 0.45) and AI model input",
                      "Google Places when switched on, otherwise an estimate", True),
    "status": ("Decides which permits count as competitors (only permits in force)", "", True),
    "line_of_business": ("Direct competitors: counts per kind of business (bakeries, not all food)", "", True),
    "permit_no": ("A permit listed twice is counted once; the number is not stored", "", True),
    "competitor_count": ("MSI Competitor density (w1 = 0.45) and AI model input",
                         "Google Places when switched on, otherwise an estimate", True),
    "population_density": ("MSI Socio-demographic (w3 = 0.20) and AI model input",
                           "Real for 73 of 76 barangays (PSA 2024 population / land area)", True),
    "average_rent": ("MSI Socio-demographic (w3 = 0.20) and AI model input",
                     "Estimated (no public barangay figure)", True),
    "foot_traffic_index": ("AI model input", "Estimated (no public barangay figure)", True),
    "historical_success_rate": ("MSI Demand trend (w2 = 0.35) and AI model input",
                                "Estimated (no public barangay figure)", True),
    "date_recorded": ("Dates the counts: two uploads 60+ days apart give the barangay its own growth rate "
                      "for the Saturation Map forecast", "The upload date", True),
}

# Model inputs a template supplies besides the columns named after them:
# a permit register is counted into competitor counts.
_DERIVED_INPUTS = {"MARKET_DATA": {"industry_type": "competitor_count"}}


def _columns():
    from app.controllers.lgu_controller import UPLOAD_COLUMNS

    return UPLOAD_COLUMNS


def template_columns(key):
    """[(name, level, meaning, example)] for one template, in order."""
    return list(_columns().get(key, ()))


def required_columns(key):
    return [name for name, level, _m, _e in template_columns(key) if level == "required"]


def standard_selection(key):
    """Every column of the template (the standard download)."""
    return [name for name, *_rest in template_columns(key)]


def suggested_selection(key):
    """The required columns plus every suggested one -- what a new custom
    template starts with."""
    return [name for name, level, _m, _e in template_columns(key)
            if level == "required" or FACTS.get(name, ("", "", False))[2]]


def clean_selection(key, names):
    """`names` reduced to real columns of the template, in template order,
    with the required columns always in."""
    wanted = set(names or ()) | set(required_columns(key))
    return [name for name in standard_selection(key) if name in wanted]


def saved_selections(user):
    """{template key: [columns]} from the account, falling back to the
    suggested selection for a template never saved."""
    try:
        stored = json.loads(getattr(user, "upload_template", None) or "{}")
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {key: clean_selection(key, stored[key]) if isinstance(stored.get(key), list) else suggested_selection(key)
            for key in TEMPLATE_TARGETS}


def has_saved_template(user):
    return bool(getattr(user, "upload_template", None))


def parse_selection_form(form):
    """({key: [columns]}, errors) from the Custom Template form: one
    checkbox group per template, named col-<KEY>. Nothing to refuse
    today -- the required columns are added back whatever is ticked, and
    competitor_count (required in the counts template) is what makes
    the importer read a counts file as counts -- but the errors list
    keeps the form's contract if a rule is added."""
    selections, errors = {}, []
    for key in TEMPLATE_TARGETS:
        selections[key] = clean_selection(key, form.getlist(f"col-{key}"))
    return selections, errors


def save_selections(user, selections):
    user.upload_template = json.dumps({key: list(columns) for key, columns in selections.items()})


def model_inputs_covered(key, columns):
    """[label] of the AI model inputs a file with these columns supplies."""
    derived = _DERIVED_INPUTS.get(key, {})
    covered = []
    for name in columns:
        target = name if name in MODEL_INPUTS else derived.get(name)
        if target and MODEL_INPUTS[target] not in covered:
            covered.append(MODEL_INPUTS[target])
    return covered


def template_csv(key, columns):
    """The CSV text for a template with these columns: the header row and
    one example row."""
    examples = {name: example for name, _level, _meaning, example in template_columns(key)}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerow([examples.get(name, "") for name in columns])
    return buffer.getvalue()


def builder_context(user):
    """What lgu/_custom_template.html needs."""
    saved = saved_selections(user)
    templates = []
    for key in TEMPLATE_TARGETS:
        rows = []
        for name, level, meaning, example in template_columns(key):
            feeds, today, suggested = FACTS.get(name, ("", "", False))
            model_input = name if name in MODEL_INPUTS else _DERIVED_INPUTS.get(key, {}).get(name)
            rows.append({"name": name, "level": level, "meaning": meaning, "example": example, "feeds": feeds,
                         "today": today, "suggested": suggested, "checked": name in saved[key],
                         "locked": level == "required",
                         "model_label": MODEL_INPUTS[model_input] if model_input else ""})
        templates.append({"key": key, "title": TEMPLATE_TITLES[key], "target": TEMPLATE_TARGETS[key],
                          "rows": rows, "covered": model_inputs_covered(key, saved[key]),
                          "selected": saved[key]})
    return {"custom_templates": templates, "model_input_total": len(MODEL_INPUTS),
            "custom_template_saved": has_saved_template(user)}
