"""
app/models/lgu_access_request.py
----------------------------------
LGU_ACCESS_REQUESTS -- how an LGU staff member or official gets an LGU
role in MarketLens (app/services/lgu_access_service.py).

Anyone can pick "LGU Official" at sign-up, so picking it grants nothing.
The account starts "pending" and this request records what the System
Admin needs to verify the person before any LGU page opens:

    office, position     where they work and what they do there
    requested_role       LGU Encoder (uploads) or LGU Data Steward (reviews)
    endorser_*           their head of office, who must confirm the request
                         from an official LGU e-mail address
    discord_username     optional: lets the admin give the matching role
                         on the MarketLens Discord after approval
    reference_code       quoted in the endorsement e-mail and in a Discord
                         ticket, so the request can be matched without
                         the applicant posting personal details anywhere

The endorser's name and e-mail and the Discord username are stored
encrypted. A decided request is deleted after the audit-trail retention
period (retention_service); a pending one nobody acts on expires.
"""

from datetime import datetime

from app.extensions import db
from app.utils.privacy import EncryptedText

REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_CANCELLED = "cancelled"
REQUEST_EXPIRED = "expired"

REQUEST_LABELS = {
    REQUEST_PENDING: "Waiting for verification",
    REQUEST_APPROVED: "Approved",
    REQUEST_REJECTED: "Not approved",
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
    def requested_role_label(self):
        from app.models.user import User

        return User.LGU_ROLES.get(self.requested_role, self.requested_role)

    def __repr__(self):
        return f"<LguAccessRequest {self.reference_code} {self.status}>"
