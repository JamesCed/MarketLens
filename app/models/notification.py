"""
app/models/notification.py
----------------------------
ADDITIVE table (not part of the 5-table dss_db ERD you supplied) that
powers the AI Early Warning system ("AI early warning notifications for
market decline and oversaturation" in the SME Module objectives) plus
generic info/system messages. Safe to add alongside your existing
tables -- it only references user.user_id and forecast_result.forecast_id,
it doesn't change either of those tables.
"""

from datetime import datetime
from app.extensions import db


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False)
    forecast_result_id = db.Column(
        db.Integer, db.ForeignKey("forecast_result.forecast_id", ondelete="SET NULL"), nullable=True
    )
    type = db.Column(db.Enum("early_warning", "info", "system", name="notification_type"), default="info")
    message = db.Column(db.String(500), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Notification {self.type} user={self.user_id}>"

    @staticmethod
    def unread_count_for(user_id):
        """How many unread notifications `user_id` has -- counted once
        per request. The topbar bell (context processor, every page) and
        Home's alerts card both need it."""
        from app.utils.request_memo import request_memo

        return request_memo(
            ("unread_notifications", user_id),
            lambda: Notification.query.filter_by(user_id=user_id, is_read=False).count(),
        )
