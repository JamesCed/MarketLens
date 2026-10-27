"""
app/models/upload_batch.py
----------------------------
UPLOAD_BATCHES -- the staging area of the Gov't Data Upload.

An LGU Encoder's upload is read and validated, then kept here as one
batch: the rows it would add to lgu_data, market_data and
subcategory_market_data (payload, JSON), how many there are, and where
the batch is in its review:

    pending_review     uploaded (or corrected) by an LGU Encoder, waiting
                       for a Data Steward
    changes_requested  the Data Steward sent it back with a message; the
                       Encoder edits the rows or replaces the file
    approved           approved by an LGU Data Steward, waiting for an Admin
    rejected           refused by the Data Steward or the Admin (with a reason)
    archived           withdrawn by the Encoder before it went live
    published          written to the live tables by a System Admin

`messages` is the conversation on the batch -- the Steward's requests
and the Encoder's replies -- as a JSON list.

Nothing in a batch is read by the scoring engine, the dashboards or the
trend reports until it is published. The payload is cleared once the
batch is published or rejected -- it is then either live or not wanted
-- and the batch record itself follows the uploaded-data retention
period (retention_service).
"""

import json
from datetime import datetime

from sqlalchemy.dialects.mysql import LONGTEXT

from app.extensions import db

STATUS_PENDING = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_CHANGES = "changes_requested"
STATUS_ARCHIVED = "archived"

# What the Encoder may still change, and withdraw.
EDITABLE_STATUSES = (STATUS_PENDING, STATUS_CHANGES)
ARCHIVABLE_STATUSES = (STATUS_PENDING, STATUS_CHANGES, STATUS_APPROVED, STATUS_REJECTED)

STATUS_LABELS = {
    STATUS_PENDING: "Waiting for review",
    STATUS_APPROVED: "Approved, waiting to be published",
    STATUS_REJECTED: "Rejected",
    STATUS_PUBLISHING: "Publishing",
    STATUS_PUBLISHED: "Published (live)",
    STATUS_CHANGES: "Changes requested",
    STATUS_ARCHIVED: "Archived",
}


class UploadBatch(db.Model):
    __tablename__ = "upload_batches"

    id = db.Column(db.Integer, primary_key=True)
    dataset_type = db.Column(db.String(20), nullable=False)
    source = db.Column(db.String(30), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)

    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    lgu_rows = db.Column(db.Integer, nullable=False, default=0)
    market_rows = db.Column(db.Integer, nullable=False, default=0)
    subcategory_rows = db.Column(db.Integer, nullable=False, default=0)
    barangays = db.Column(db.Integer, nullable=False, default=0)
    permits_read = db.Column(db.Integer, nullable=True)
    permits_in_force = db.Column(db.Integer, nullable=True)
    summary = db.Column(db.String(1000), nullable=True)
    payload = db.Column(db.Text().with_variant(LONGTEXT(), "mysql"), nullable=True)

    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_note = db.Column(db.String(500), nullable=True)
    published_by = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)
    messages = db.Column(db.Text, nullable=True)
    scope_barangay = db.Column(db.String(100), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)

    uploader = db.relationship("User", foreign_keys=[uploaded_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    publisher = db.relationship("User", foreign_keys=[published_by])

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def message_list(self):
        try:
            return json.loads(self.messages) if self.messages else []
        except ValueError:
            return []

    @property
    def last_message(self):
        items = self.message_list
        return items[-1] if items else None

    @property
    def is_editable(self):
        return self.status in EDITABLE_STATUSES and bool(self.payload)

    @property
    def is_archivable(self):
        return self.status in ARCHIVABLE_STATUSES

    @property
    def dataset_label(self):
        return "Gov't / Zoning Data" if self.dataset_type == "LGU_DATA" else "Market Data"

    def __repr__(self):
        return f"<UploadBatch #{self.id} {self.dataset_type} {self.status}>"
