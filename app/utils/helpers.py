"""
app/utils/helpers.py
----------------------
Small shared helper functions used by more than one controller/template.

Business-type vocabulary lives in ONE place -- app/ml/constants.py --
not here, so the AI encoding and the UI dropdowns can never drift apart;
import BUSINESS_TYPES from there instead. There is also no
INVESTMENT_RANGES list anymore: the real sme_profile.startup_capital
column is a plain DECIMAL, not a dropdown-of-ranges like an earlier
draft of this UI had.
"""

import os

from werkzeug.utils import secure_filename


def allowed_file(filename, allowed_extensions):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def unique_upload_path(upload_folder, filename):
    """Return a (safe_filename, absolute_path) pair that doesn't already exist on disk."""
    safe_name = secure_filename(filename)
    base, ext = os.path.splitext(safe_name)
    candidate = safe_name
    counter = 1
    while os.path.exists(os.path.join(upload_folder, candidate)):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate, os.path.join(upload_folder, candidate)


def format_currency(value):
    try:
        return f"₱{float(value):,.0f}"
    except (TypeError, ValueError):
        return "₱0"
