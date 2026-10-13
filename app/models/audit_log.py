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

    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)
    actor_name = db.Column(db.String(100), nullable=True)
    actor_role = db.Column(db.String(20), nullable=True)

    action = db.Column(db.String(100), nullable=False)
    target_type = db.Column(db.String(50), nullable=True)
    target_id = db.Column(db.String(50), nullable=True)
    target_label = db.Column(db.String(255), nullable=True)
    details = db.Column(db.String(500))
    changes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    ip_address = db.Column(db.String(45))
    http_method = db.Column(db.String(10), nullable=True)
    route = db.Column(db.String(255), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)

    reason = db.Column(db.String(255), nullable=True)

    user = db.relationship("User")

    def __repr__(self):
        return f"<AuditLog {self.action} user={self.user_id}>"
