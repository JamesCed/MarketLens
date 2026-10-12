"""
tests/account_fixtures.py
---------------------------
What a valid sign-up now carries, shared by every test that registers an
account (IT expert review): the personal information in full, a password
that meets the strength rule, and acceptance of the Terms and Conditions.
"""

STRONG_PASSWORD = "Secret#2026"

PERSONAL = {
    "first_name": "Juan",
    "middle_name": "Santos",
    "last_name": "Dela Cruz",
    "suffix": "",
    "gender": "Male",
    "birthdate": "1990-05-14",
    "contact_number": "09171234567",
    "address_line": "123 Rizal St.",
    "address_barangay": "Poblacion",
    "accept_terms": "1",
}


def account_fields(email, password=STRONG_PASSWORD, **overrides):
    """The account half of a sign-up form (everything but the plan)."""
    data = dict(PERSONAL)
    data.update({"email": email, "password": password, "confirm_password": password})
    data.update(overrides)
    return data
