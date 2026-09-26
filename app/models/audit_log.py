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
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.String(500))
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

    def __repr__(self):
        return f"<AuditLog {self.action} user={self.user_id}>"
