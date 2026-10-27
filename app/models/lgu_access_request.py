"""
app/models/lgu_access_request.py
----------------------------------
LGU_ACCESS_REQUESTS -- how an LGU staff member or official gets an LGU
role in MarketLens (app/services/lgu_access_service.py).

Anyone can register as LGU staff, so registering grants no LGU role.
The account stays "pending_verification" until the System Admin approves
or rejects this request, which records what the admin needs to decide:

    office, position        the LGU department / office and official job title
    jurisdiction_scope      "city" (city-wide) or "barangay"
    target_barangay         the one barangay, when the scope is a barangay
    requested_role          LGU Encoder (uploads) or LGU Data Steward (reviews)
    reason                  what datasets their office intends to manage
    reference_code          shown to the applicant, for any follow-up

endorser_name, endorser_email and discord_username belong to the V3.5
request form (head-of-office endorsement, Discord) and are no longer
collected; they stay as columns so older rows still load, and are
deleted with those rows by the retention job.
"""

from datetime import datetime

from app.extensions import db
from app.utils.privacy import EncryptedText

REQUEST_PENDING = "pending_verification"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_CANCELLED = "cancelled"
REQUEST_EXPIRED = "expired"

REQUEST_LABELS = {
    REQUEST_PENDING: "Pending verification",
    REQUEST_APPROVED: "Approved",
    REQUEST_REJECTED: "Rejected",
    REQUEST_CANCELLED: "Replaced by a newer request",
    REQUEST_EXPIRED: "Expired",
}


class LguAccessRequest(db.Model):
    __tablename__ = "lgu_access_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False, index=True)
    reference_code = db.Column(db.String(16), nullable=False, unique=True, index=True)
    status = db.Column(db.String(20), nullable=False, default=REQUEST_PENDING, index=True)

    requested_role = db.Column(db.String(20), nullable=False)
    office = db.Column(db.String(120), nullable=False)
    position = db.Column(db.String(100), nullable=False)
    jurisdiction_scope = db.Column(db.String(20), nullable=True)
    target_barangay = db.Column(db.String(100), nullable=True)
    reason = db.Column(db.String(300), nullable=True)

    # V3.5 only -- no longer collected (see the module docstring).
    endorser_name = db.Column(EncryptedText, nullable=True)
    endorser_email = db.Column(EncryptedText, nullable=True)
    discord_username = db.Column(EncryptedText, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    decided_by = db.Column(db.Integer, nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    granted_role = db.Column(db.String(20), nullable=True)
    decision_note = db.Column(db.String(255), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])

    @property
    def status_label(self):
        return REQUEST_LABELS.get(self.status, self.status)

    @property
    def is_pending(self):
        return self.status in (REQUEST_PENDING, "pending")

    @property
    def requested_role_label(self):
        from app.models.user import User

        return User.LGU_ROLES.get(self.requested_role, self.requested_role)

    @property
    def scope_label(self):
        if self.jurisdiction_scope == "barangay" and self.target_barangay:
            return f"Barangay {self.target_barangay}"
        return "City-wide"

    def __repr__(self):
        return f"<LguAccessRequest {self.reference_code} {self.status}>"
