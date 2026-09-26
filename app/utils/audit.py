"""
app/utils/audit.py
--------------------
One helper used everywhere an action needs to be written to the
Admin Module's audit trail.
"""

from flask import request
from flask_login import current_user

from app.extensions import db
from app.models.audit_log import AuditLog


def log_action(action, details=None, user_id=None):
    """Write one row to audit_logs. Never raises -- a logging failure
    should never break the feature that triggered it."""
    try:
        uid = user_id
        if uid is None and current_user and getattr(current_user, "is_authenticated", False):
            uid = current_user.user_id
        entry = AuditLog(
            user_id=uid,
            action=action,
            details=details,
            ip_address=request.remote_addr if request else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
