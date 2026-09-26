"""
app/models/user.py
-------------------
Matches the real `user` table exactly as given in dss_db_user.sql:

    user_id (PK), name, email (unique), password, role ENUM('Admin','SME','LGU'),
    contact_number, status ENUM('active','inactive') DEFAULT 'active', created_at

Note the table name is singular "user" (not "users"), the role values are
capitalized ('Admin'/'SME'/'LGU'), and there is no separate boolean
is_active column -- account state is the `status` enum instead. There is
also no location/business_type/organization_name/notification-preference
columns on this table (that used to be here in an earlier draft) -- those
concepts now live on SmeProfile (industry_type/location) where the given
schema actually puts them; account-level notification toggles simply
aren't part of the given schema, so that UI section was removed (see
README "What changed to match your real schema").

ADDITIVE COLUMN -- profile_picture: NOT part of the original given
schema, added the same way Notification/PlanSave/AuditLog/SystemSetting
are "additive" (they don't touch your 5 given tables' meaning, they just
add capability on top). Stored as a data: URI string (base64-encoded
image, e.g. "data:image/png;base64,....") directly in the row -- NOT as
a file path -- on purpose: no new upload folder to create/serve/back up,
works identically on any host, and needs zero extra route. Capped at a
small file size (see profile_controller.py) so the LONGTEXT column
stays reasonable.

ADDITIVE COLUMNS -- theme and the four notify_* switches: the
per-account preferences the Settings page saves.

They are COLUMNS ON THIS ROW rather than a separate preferences table,
deliberately. A key/value preferences table costs a row per user per
setting -- hundreds of rows to remember two small values -- where a
column costs none: every account already has exactly one `user` row and
these simply travel on it. It is also the same call already made for
profile_picture above.

If your MySQL database was created BEFORE these columns existed, run
this once:

    ALTER TABLE user ADD COLUMN profile_picture LONGTEXT NULL;
    ALTER TABLE user ADD COLUMN theme VARCHAR(10) NOT NULL DEFAULT 'light';
    ALTER TABLE user ADD COLUMN notify_recommendations TINYINT(1) NOT NULL DEFAULT 0;
    ALTER TABLE user ADD COLUMN notify_weekly_trends TINYINT(1) NOT NULL DEFAULT 0;
    ALTER TABLE user ADD COLUMN notify_saturation_change TINYINT(1) NOT NULL DEFAULT 1;
    ALTER TABLE user ADD COLUMN notify_newsletter TINYINT(1) NOT NULL DEFAULT 0;

(a brand-new database created via db.create_all() gets them
automatically, and app/services/startup_migrations.py adds any that are
missing on boot.)
"""

from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.dialects.mysql import LONGTEXT

from app.extensions import db, login_manager


class User(UserMixin, db.Model):
    __tablename__ = "user"

    user_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum("Admin", "SME", "LGU", name="user_role"), nullable=False)
    contact_number = db.Column(db.String(20))
    status = db.Column(db.Enum("active", "inactive", name="user_status"), nullable=False, default="active")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Additive -- see docstring above. A data: URI (base64 image), or
    # NULL if the user never uploaded one (falls back to initials()).
    profile_picture = db.Column(db.Text().with_variant(LONGTEXT(),"mysql"), nullable=True)

    # Additive preferences, saved by the Settings page.
    #
    # theme: 'light' | 'dark'. A plain string, not an Enum, so adding a
    # value later needs no migration.
    #
    # There is deliberately no "match system" option: the choice should
    # be one the person made and can see, not one that changes under
    # them when their laptop switches at sunset. Accounts created before
    # this was decided may still hold 'system' in the database --
    # resolved_theme() below maps that to light, and
    # startup_migrations.py rewrites the rows.
    theme = db.Column(db.String(10), nullable=False, default="light", server_default="light")

    # ---------------- Notification preferences ----------------
    # Four switches, stored as four columns on this row. Still no new
    # rows anywhere: a per-user/per-setting preferences table would cost
    # four rows per account to hold four booleans.
    #
    # ONE OF THESE IS WIRED TO A LIVE FEATURE AND THREE ARE NOT, and
    # that distinction is deliberate rather than an oversight:
    #
    #   notify_saturation_change -> REAL. Checked by
    #       _maybe_fire_early_warning() in forecasting_service.py before
    #       an early-warning notification is created. Defaults ON, since
    #       the warnings are one of the SME module's stated features.
    #
    #   notify_recommendations / notify_weekly_trends / notify_newsletter
    #       -> saved and honoured the moment something sends them, but
    #       NOTHING SENDS THEM YET. email_service.py can only send a
    #       registration verification code, and there is no scheduled
    #       job in this project. They default OFF for that reason: an
    #       account should not be silently opted in to mail that starts
    #       arriving the day a digest job is written. The Settings page
    #       labels them accordingly rather than implying they work.
    notify_recommendations = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    notify_weekly_trends = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    notify_saturation_change = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    notify_newsletter = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    # Kept as an alias so older code and databases that used the
    # original name keep working after the rename.
    @property
    def notify_early_warning(self):
        return self.notify_saturation_change

    @notify_early_warning.setter
    def notify_early_warning(self, value):
        self.notify_saturation_change = bool(value)

    # Only the values the UI offers. Anything else -- a legacy 'system'
    # row, or a hand-edited one -- resolves to light, so nothing
    # unexpected can reach the <html data-bs-theme> attribute.
    THEMES = ("light", "dark")

    @property
    def resolved_theme(self):
        """The theme value safe to render into the page."""
        return self.theme if self.theme in self.THEMES else "light"

    sme_profiles = db.relationship("SmeProfile", backref="owner", lazy="dynamic", cascade="all, delete-orphan")
    notifications = db.relationship("Notification", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    uploaded_lgu_data = db.relationship("LguData", backref="uploader", lazy="dynamic")

    # Flask-Login's UserMixin.get_id() returns str(self.id) by default,
    # but this table's primary key column is named user_id, not id --
    # override it so login sessions actually work.
    def get_id(self):
        return str(self.user_id)

    @property
    def is_active(self):
        return self.status == "active"

    def set_password(self, raw_password):
        self.password = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password, raw_password)

    def initials(self):
        parts = (self.name or "").split()
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][0].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def is_admin(self):
        return self.role == "Admin"

    def is_sme(self):
        return self.role == "SME"

    def is_lgu(self):
        return self.role == "LGU"

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
