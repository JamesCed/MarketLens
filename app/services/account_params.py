"""
app/services/account_params.py
--------------------------------
The rules every account form shares -- sign-up, Settings, the password
reset, Admin > Create Account -- so a field is validated the same way
wherever it is typed.

PASSWORDS (IT expert review): at least 8 characters with at least one
uppercase letter, one lowercase letter, one number and one special
character. Checked whenever a password is SET; an account created under
the old 6-character rule still signs in with its existing password and
meets the new rule the next time it changes it.

PERSONAL INFORMATION (IT expert review): the full name in parts (first,
middle, last, suffix), gender, birthday (age is computed, never typed),
contact number and address. The account owner is a real person whose
details the city may rely on, so the sign-up form asks for them in full.
Gender, birthday, contact number and address are stored encrypted (see
app/utils/privacy.py).
"""

import re
from datetime import date

from app.ml.seed_data import BARANGAY_NAMES

PASSWORD_MIN_LENGTH = 8
PASSWORD_RULES = (
    ("length", f"At least {PASSWORD_MIN_LENGTH} characters", lambda p: len(p) >= PASSWORD_MIN_LENGTH),
    ("upper", "One uppercase letter (A-Z)", lambda p: re.search(r"[A-Z]", p) is not None),
    ("lower", "One lowercase letter (a-z)", lambda p: re.search(r"[a-z]", p) is not None),
    ("number", "One number (0-9)", lambda p: re.search(r"\d", p) is not None),
    ("special", "One special character (e.g. ! @ # $ %)", lambda p: re.search(r"[^A-Za-z0-9\s]", p) is not None),
)
PASSWORD_HINT = (
    f"At least {PASSWORD_MIN_LENGTH} characters, with an uppercase letter, a lowercase letter, "
    "a number and a special character."
)


def password_problems(password):
    """The rules `password` breaks, as sentences. Empty means strong."""
    password = password or ""
    missing = [label for _key, label, test in PASSWORD_RULES if not test(password)]
    if not missing:
        return []
    return [f"Password needs: {', '.join(m[0].lower() + m[1:] for m in missing)}."]


# ---------------------------------------------------------------------
# Personal information
# ---------------------------------------------------------------------
SUFFIXES = ("", "Jr.", "Sr.", "II", "III", "IV", "V")
# The details stored encrypted, and never written to the audit trail.
PII_FIELDS = ("gender", "birthdate", "contact_number", "address_line", "address_barangay")
GENDERS = ("Male", "Female", "Prefer not to say")
MIN_AGE = 18
MAX_AGE = 100
CITY = "Tarlac City"

_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿÑñ][A-Za-zÀ-ÖØ-öø-ÿÑñ .'\-]*$")
_PH_MOBILE_RE = re.compile(r"^(?:\+?63|0)?(9\d{9})$")


def normalise_ph_mobile(value):
    """'0917 123 4567', '+639171234567', '9171234567' -> '09171234567'.
    None when it is not a Philippine mobile number."""
    digits = re.sub(r"[\s\-()]", "", value or "")
    match = _PH_MOBILE_RE.match(digits)
    return f"0{match.group(1)}" if match else None


def compose_full_name(first, middle, last, suffix):
    """'Juan', 'Santos', 'Dela Cruz', 'Jr.' -> 'Juan Santos Dela Cruz Jr.'
    -- the single display name every page already shows (user.name)."""
    parts = [p.strip() for p in (first, middle, last) if p and p.strip()]
    name = " ".join(parts)
    if suffix:
        name = f"{name} {suffix}"
    return name[:100]


def age_on(birthdate, today=None):
    if not birthdate:
        return None
    today = today or date.today()
    return today.year - birthdate.year - ((today.month, today.day) < (birthdate.month, birthdate.day))


def _clean(form, key, limit):
    return (form.get(key) or "").strip()[:limit]


def parse_personal_info(form, today=None):
    """Read and check the personal-information fields of `form`.

    Returns (data, errors). `data` is JSON-safe (the birthday as an ISO
    string) because sign-up parks it in the session until the emailed
    code is confirmed."""
    errors = []
    first = _clean(form, "first_name", 60)
    middle = _clean(form, "middle_name", 60)
    last = _clean(form, "last_name", 60)
    suffix = _clean(form, "suffix", 10)

    if not first:
        errors.append("First name is required.")
    if not last:
        errors.append("Last name is required.")
    for label, value in (("First name", first), ("Middle name", middle), ("Last name", last)):
        if value and not _NAME_RE.match(value):
            errors.append(f"{label} can only contain letters, spaces, hyphens, apostrophes and periods.")
    if suffix not in SUFFIXES:
        errors.append("Choose a suffix from the list.")
        suffix = ""

    gender = _clean(form, "gender", 30)
    if gender not in GENDERS:
        errors.append("Choose a gender.")

    birthdate = None
    raw_birthdate = _clean(form, "birthdate", 10)
    if not raw_birthdate:
        errors.append("Birthday is required.")
    else:
        try:
            birthdate = date.fromisoformat(raw_birthdate)
        except ValueError:
            errors.append("Enter your birthday as a valid date.")
    if birthdate is not None:
        age = age_on(birthdate, today)
        if age is None or age < 0 or birthdate > (today or date.today()):
            errors.append("Your birthday cannot be in the future.")
        elif age < MIN_AGE:
            errors.append(f"You must be at least {MIN_AGE} years old to create an account.")
        elif age > MAX_AGE:
            errors.append("Please check your birthday -- that would make you over 100.")

    raw_contact = _clean(form, "contact_number", 20)
    contact = normalise_ph_mobile(raw_contact)
    if not raw_contact:
        errors.append("Contact number is required.")
    elif contact is None:
        errors.append("Enter a Philippine mobile number, e.g. 09171234567.")

    address_line = _clean(form, "address_line", 150)
    address_barangay = _clean(form, "address_barangay", 100)
    if not address_line:
        errors.append("House number and street are required.")
    if address_barangay not in BARANGAY_NAMES:
        errors.append(f"Choose your barangay from the list ({CITY}).")

    data = {
        "first_name": first,
        "middle_name": middle or None,
        "last_name": last,
        "suffix": suffix or None,
        "gender": gender,
        "birthdate": birthdate.isoformat() if birthdate else None,
        "contact_number": contact,
        "address_line": address_line,
        "address_barangay": address_barangay,
    }
    return data, errors


def apply_personal_info(user, data):
    """Write parsed personal information onto a User (and its display
    name)."""
    user.first_name = data["first_name"]
    user.middle_name = data.get("middle_name")
    user.last_name = data["last_name"]
    user.suffix = data.get("suffix")
    user.gender = data.get("gender")
    user.birthdate = date.fromisoformat(data["birthdate"]) if data.get("birthdate") else None
    user.contact_number = data.get("contact_number")
    user.address_line = data.get("address_line")
    user.address_barangay = data.get("address_barangay")
    user.name = compose_full_name(data["first_name"], data.get("middle_name"), data["last_name"], data.get("suffix"))
    return user
