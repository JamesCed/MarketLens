"""
app/models/upload_batch.py
----------------------------
UPLOAD_BATCHES -- the staging area of the Gov't Data Upload.

An LGU Encoder's upload is read and validated, then kept here as one
batch: the rows it would add to lgu_data, market_data and
subcategory_market_data (payload, JSON), how many there are, and where
the batch is in its review:

    pending_review  uploaded by an LGU Encoder, waiting for a Data Steward
    approved        approved by an LGU Data Steward, waiting for an Admin
    rejected        refused by the Data Steward or the Admin (with a reason)
    published       written to the live tables by a System Admin

Nothing in a batch is read by the scoring engine, the dashboards or the
trend reports until it is published. The payload is cleared once the
batch is published or rejected -- it is then either live or not wanted
-- and the batch record itself follows the uploaded-data retention
period (retention_service).
"""

from datetime import datetime

from sqlalchemy.dialects.mysql import LONGTEXT

from app.extensions import db

STATUS_PENDING = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"

STATUS_LABELS = {
    STATUS_PENDING: "Waiting for review",
    STATUS_APPROVED: "Approved, waiting to be published",
    STATUS_REJECTED: "Rejected",
    STATUS_PUBLISHING: "Publishing",
    STATUS_PUBLISHED: "Published (live)",
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

    uploader = db.relationship("User", foreign_keys=[uploaded_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    publisher = db.relationship("User", foreign_keys=[published_by])

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def dataset_label(self):
        return "Gov't / Zoning Data" if self.dataset_type == "LGU_DATA" else "Market Data"

    def __repr__(self):
        return f"<UploadBatch #{self.id} {self.dataset_type} {self.status}>"
