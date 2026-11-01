"""
app/services/lgu_profile.py
-----------------------------
The "Tarlac City LGU context" part of the LGU registration form
(/register/lgu): the jurisdiction scope (city-wide or one barangay), the
LGU department / office and the official job title. They are saved on
the account (user.lgu_scope, lgu_barangay, lgu_office, lgu_position) and
shown next to the LGU badge in Admin > Manage Users.

They describe the person; they grant nothing. An LGU account is active
as soon as it is created and has every LGU page, Gov't Data Upload
included -- there is no separate access request or LGU sub-role.
"""

import re

from app.models import User

OFFICES = (
    "City Planning and Development Office (CPDO)",
    "Business Permits and Licensing Office (BPLO)",
    "City Assessor's Office",
    "City Treasurer's Office",
    "Office of the City Mayor",
    "Barangay Executive Office",
)
SCOPES = User.LGU_SCOPES

_TITLE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿÑñ0-9][A-Za-zÀ-ÖØ-öø-ÿÑñ0-9 .,'&()/\-]{1,99}$")


def form_context():
    """What the LGU registration form needs to draw its dropdowns."""
    from app.ml.seed_data import BARANGAY_NAMES

    return {"offices": OFFICES, "lgu_scopes": SCOPES, "lgu_barangays": sorted(BARANGAY_NAMES)}


def parse_lgu_context(form):
    """Returns (data, errors). `data` is JSON-safe so it can wait in the
    session while the e-mail code is confirmed."""
    from app.ml.seed_data import BARANGAY_NAMES

    errors = []
    scope = (form.get("lgu_scope") or "").strip()
    barangay = (form.get("lgu_barangay") or "").strip()
    if scope not in SCOPES:
        errors.append("Choose your jurisdiction scope: City-wide or a specific barangay.")
    if scope == "barangay":
        if barangay not in BARANGAY_NAMES:
            errors.append("Choose the barangay you work for (one of Tarlac City's 76 barangays).")
    else:
        barangay = ""

    office = (form.get("lgu_office") or "").strip()
    if office not in OFFICES:
        errors.append("Choose your LGU department or office.")

    position = " ".join((form.get("lgu_position") or "").split())[:100]
    if not _TITLE.match(position):
        errors.append("Enter your official job title or designation (e.g. Planning Officer).")

    data = {
        "scope": scope if scope in SCOPES else "city",
        "barangay": barangay or None,
        "office": office,
        "position": position,
    }
    return data, errors


def apply_lgu_context(user, data):
    """Save the LGU context on the account."""
    if not data or not data.get("office"):
        return
    user.lgu_office = data["office"]
    user.lgu_position = data.get("position") or None
    user.lgu_scope = "barangay" if data.get("scope") == "barangay" and data.get("barangay") else "city"
    user.lgu_barangay = data.get("barangay") if user.lgu_scope == "barangay" else None
