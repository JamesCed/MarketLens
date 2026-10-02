"""
app/services/plan_params.py
-----------------------------
ONE parser for a business plan's input parameters.

A plan is entered in three places -- the sign-up wizard, the Home
page's "Add New Plan" dialog, and the per-plan Edit dialog on the Home
page (it used to live in Settings > Business Preferences) -- and each
used to parse the form itself. Three parsers drift: one accepted a
free-typed industry the others refused, one defaulted the capital to 0
and another to None, and removing a field meant finding all three. This
module is the only place that decides what a valid plan looks like; the
controllers just call it.

WHAT IS COLLECTED, AND WHAT IS NOT

  required   business_name, industry_type, location, capital (> 0)
  optional   subcategory, product_offering, innovation_idea,
             offering items (the optional "sub-plan": menu / price list),
             business_stage (+ registration_date), employee_count

CAPITAL IS REQUIRED, AND IT IS CALLED "CAPITAL"
Capital used to be optional and was only ever displayed. It is now an
input to the plan viability model (app/ml/plan_model.py): how many
months of rent and payroll it carries, against how long the model
expects the business to take to ramp up. A model input that can be
silently blank is a model input that is silently zero, so the form
refuses a plan without one. The form field is `capital`; the legacy
name `startup_capital` is still accepted, so an old cached form or a
bookmarked POST keeps working. The parsed data carries both keys:
`startup_capital` is the column apply_plan_data() writes (renaming a
live MySQL column would be a destructive migration), `capital` is the
name everything else reads.

Monthly revenue is gone. Someone planning a business does not have a
revenue figure, and asking for one invited a guess that then drove the
ROI estimate as though it had been measured. The break-even window is
derived from the models' own output alone (see
location_opportunity_service.estimate_roi_timeframe and
app/services/plan_forecast_service.py).

Everything returned is JSON-safe (dates as ISO strings, the offering list
as a list), because the sign-up wizard has to hold a validated plan in
the session until the email code is confirmed. apply_plan_data() turns it
into model attributes.
"""

import json
from datetime import date, datetime

from app.ml.constants import BUSINESS_TYPES
from app.ml.subcategories import is_valid_subcategory

BUSINESS_STAGES = ("startup", "existing")

MAX_OFFERING_ITEMS = 30
MAX_ITEM_NAME = 80
MAX_PRODUCT_OFFERING = 500
MAX_INNOVATION_IDEA = 1000
# sme_profile.startup_capital is DECIMAL(12,2): ten digits before the
# point. Anything larger would be refused by MySQL at commit time, as a
# 500 rather than a message, so it is refused here instead.
MAX_CAPITAL = 9_999_999_999.99
# The other numbers on a plan get the same treatment, for the same two
# reasons. sme_profile.employee_count is an INT, so a typed
# 99999999999 would be the same commit-time 500 MAX_CAPITAL prevents;
# and both numbers are now inputs to the plan viability model, which
# must never be handed NaN or infinity (float("nan") parses happily,
# and a NaN price would then be stored, re-forecast, and written into
# a JSON reply the browser refuses to parse). The caps are generous on
# purpose -- far above any micro, small or medium enterprise (DTI puts
# "large" at 200 staff) and any single menu price -- so they only ever
# stop a typo or a crafted POST, never a real plan.
MAX_EMPLOYEE_COUNT = 10_000
MAX_ITEM_PRICE = 10_000_000.0


def _is_finite(value):
    """False for NaN and +/-infinity -- both of which float() accepts
    from text ("nan", "inf", "1e400") and both of which pass a `< 0`
    check, since every comparison with NaN is False."""
    return value == value and value not in (float("inf"), float("-inf"))


def _text(form, field, limit):
    return (form.get(field) or "").strip()[:limit]


def _getlist(form, field):
    getter = getattr(form, "getlist", None)
    if getter is not None:
        return getter(field)
    value = form.get(field)
    return value if isinstance(value, list) else ([] if value is None else [value])


def parse_offering_items(form, errors):
    """Pairs offering_item[] with offering_price[]. Rows with no item name
    are skipped rather than rejected -- an empty trailing row is how a
    "add another item" form naturally ends. A price is optional; a price
    that is not a non-negative number is an error, because a menu price
    of "abc" is a typo the owner wants to know about. "nan", "inf" and
    an absurdly large figure are refused as well: the prices feed the
    plan viability model's required-daily-sales check (see
    MAX_ITEM_PRICE)."""
    names = _getlist(form, "offering_item")
    prices = _getlist(form, "offering_price")
    items = []
    for index, raw_name in enumerate(names):
        name = (raw_name or "").strip()[:MAX_ITEM_NAME]
        if not name:
            continue
        raw_price = (prices[index] if index < len(prices) else "") or ""
        raw_price = str(raw_price).strip().replace(",", "")
        price = None
        if raw_price:
            try:
                price = float(raw_price)
            except ValueError:
                errors.append(f"The price for \"{name}\" must be a number.")
                continue
            if not _is_finite(price):
                errors.append(f"The price for \"{name}\" must be a number.")
                continue
            price = round(price, 2)
            if price < 0:
                errors.append(f"The price for \"{name}\" cannot be negative.")
                continue
            if price > MAX_ITEM_PRICE:
                errors.append(f"The price for \"{name}\" must be ₱{MAX_ITEM_PRICE:,.0f} or less.")
                continue
        items.append({"item": name, "price": price})
        if len(items) >= MAX_OFFERING_ITEMS:
            break
    return items


def _capital(form, errors):
    """The plan's capital in pesos, REQUIRED and > 0.

    Read from `capital`, falling back to the legacy field name
    `startup_capital` (an old cached form, a bookmarked POST). Commas are
    tolerated because "500,000" is how people write pesos. Returns None
    and appends one of three plain messages when it is not usable.

    Rounded to centavos BEFORE the "> 0" check, not after: the column
    holds two decimals, so "0.001" passes a check on the raw float and
    is then stored as 0.00 -- a brand-new plan saved with no capital and
    flagged "Capital missing" the moment it lands. What is checked is
    what is stored."""
    raw = form.get("capital")
    if raw is None or str(raw).strip() == "":
        raw = form.get("startup_capital")
    raw = "" if raw is None else str(raw).strip().replace(",", "")
    if raw.startswith("₱"):  # a pasted "₱500000"
        raw = raw[1:].strip()
    if raw == "":
        errors.append("Capital is required.")
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        errors.append("Capital must be a number.")
        return None
    if not _is_finite(value):  # "nan", "inf", "1e400"
        errors.append("Capital must be a number.")
        return None
    value = round(value, 2)
    if value <= 0:
        errors.append("Capital must be greater than zero.")
        return None
    if value > MAX_CAPITAL:
        errors.append("Capital is larger than this system can store.")
        return None
    return value


def parse_plan_form(form, keep_industry=None):
    """(data, errors). `data` holds every plan field, JSON-safe.

    `keep_industry` is the plan's CURRENT industry when editing: a plan
    saved under a name the taxonomy has since dropped may keep it (the
    Home page's Edit dialog offers it as "(current)"), so opening Edit
    and saving never forces a reclassification. A new plan must pick
    from the list."""
    errors = []

    business_name = _text(form, "business_name", 150)
    industry_type = _text(form, "industry_type", 100)
    location = _text(form, "location", 150)
    business_stage = _text(form, "business_stage", 20).lower() or "startup"
    subcategory = _text(form, "subcategory", 100) or None

    if not business_name:
        errors.append("Business name is required.")
    if not industry_type:
        errors.append("Please choose an industry type.")
    elif industry_type not in BUSINESS_TYPES and industry_type != keep_industry:
        errors.append("That industry type is not one of the options.")
    if not location:
        errors.append("Please give the barangay or location you are planning for.")
    if business_stage not in BUSINESS_STAGES:
        business_stage = "startup"
    if subcategory and industry_type in BUSINESS_TYPES and not is_valid_subcategory(industry_type, subcategory):
        # Most likely the industry was changed after a sub-category was
        # picked and the page's script did not refresh the list. Dropping
        # it scores the plan at the industry level, which is correct for
        # an unknown sub-category; refusing the whole form over it would
        # not be.
        subcategory = None

    def _number(field, label, cast, upper):
        raw = (form.get(field) or "").strip().replace(",", "")
        if raw == "":
            return None
        try:
            number = float(raw)
            # "nan" and "inf" parse as floats; int() of them raises
            # ValueError / OverflowError, and a float field would keep
            # them -- so they are refused before the cast either way.
            if not _is_finite(number):
                raise ValueError(raw)
            value = cast(number)
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{label} must be a number.")
            return None
        if value < 0:
            errors.append(f"{label} cannot be negative.")
            return None
        if value > upper:
            errors.append(f"{label} must be {upper:,} or fewer.")
            return None
        return value

    capital = _capital(form, errors)
    employee_count = _number("employee_count", "Employee count", int, MAX_EMPLOYEE_COUNT)

    registration_date = None
    raw_date = (form.get("registration_date") or "").strip()
    if raw_date:
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            errors.append("Registration date must be a real date.")
            parsed = None
        else:
            if parsed > date.today():
                errors.append("Registration date cannot be in the future.")
                parsed = None
        registration_date = parsed.isoformat() if parsed else None
    if business_stage == "existing" and registration_date is None and not errors:
        # An existing business with no date is dated today, so
        # years_in_operation() reads 0 rather than failing on a null.
        registration_date = date.today().isoformat()

    offering_items = parse_offering_items(form, errors)

    data = {
        "business_name": business_name,
        "industry_type": industry_type,
        "subcategory": subcategory,
        "location": location,
        "business_stage": business_stage,
        "registration_date": registration_date,
        # Same figure under both names -- see the module docstring.
        "startup_capital": capital,
        "capital": capital,
        "employee_count": employee_count,
        "product_offering": _text(form, "product_offering", MAX_PRODUCT_OFFERING) or None,
        "innovation_idea": _text(form, "innovation_idea", MAX_INNOVATION_IDEA) or None,
        "offering_items": offering_items,
    }
    return data, errors


def apply_plan_data(profile, data):
    """Write parsed plan data onto an SmeProfile. monthly_revenue_est is
    cleared rather than left holding a stale figure nothing can edit."""
    profile.business_name = data["business_name"]
    profile.industry_type = data["industry_type"]
    profile.subcategory = data.get("subcategory")
    profile.location = data["location"]
    profile.business_stage = data.get("business_stage") or "startup"
    raw_date = data.get("registration_date")
    if raw_date:
        profile.registration_date = date.fromisoformat(raw_date)
    elif profile.business_stage == "startup":
        profile.registration_date = None
    # The column is still called startup_capital -- see the docstring.
    capital = data.get("capital")
    if capital is None:
        capital = data.get("startup_capital")
    profile.startup_capital = capital or 0
    profile.employee_count = data.get("employee_count")
    profile.product_offering = data.get("product_offering")
    profile.innovation_idea = data.get("innovation_idea")
    items = data.get("offering_items") or []
    profile.offering_details = json.dumps(items, ensure_ascii=False) if items else None
    profile.monthly_revenue_est = None
    return profile
