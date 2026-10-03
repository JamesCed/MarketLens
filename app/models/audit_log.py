"""
app/models/audit_log.py
-------------------------
ADDITIVE table -- the Admin Module's "Audit Trail" requirement. Not one
of your 5 given tables, but references user.user_id only, so it doesn't
touch your existing data. Written by app/utils/audit.py's log_action()
helper.
"""

from datetime import datetime
from app.extensions import db


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)

    # WHO -- the account, plus a snapshot of its name and role AT THE
    # TIME. A person can be renamed or change role later; an audit entry
    # has to say who they were when they acted, not who they are now.
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)
    actor_name = db.Column(db.String(100), nullable=True)
    actor_role = db.Column(db.String(20), nullable=True)

    # WHAT -- the action, and the specific record it acted on.
    action = db.Column(db.String(100), nullable=False)
    target_type = db.Column(db.String(50), nullable=True)
    target_id = db.Column(db.String(50), nullable=True)
    target_label = db.Column(db.String(255), nullable=True)
    details = db.Column(db.String(500))
    # The FULL record of what changed, as JSON -- {"field": [before,
    # after]} for an edit, {"field": value} for something created. The
    # one-line `details` stays the concise summary the table shows; this
    # is what the "Details" button opens, so a plan's every field (or a
    # settings change's old and new values) can be traced without
    # flooding the trail with long lines. NULL for actions with nothing
    # more to say (a theme change, a sign-in).
    changes = db.Column(db.Text, nullable=True)

    # WHEN
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # WHERE -- network origin, the page/endpoint, and the device.
    ip_address = db.Column(db.String(45))
    http_method = db.Column(db.String(10), nullable=True)
    route = db.Column(db.String(255), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)

    # WHY -- the stated reason. Required from an administrator for any
    # archive, restore or access change; derived from the action's
    # purpose otherwise. See app/utils/audit.py.
    reason = db.Column(db.String(255), nullable=True)

    user = db.relationship("User")

    def __repr__(self):
        return f"<AuditLog {self.action} user={self.user_id}>"
