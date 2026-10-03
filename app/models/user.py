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

from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.dialects.mysql import LONGTEXT

from app.extensions import db, login_manager
from app.models.archive import ArchivableMixin


class User(ArchivableMixin, UserMixin, db.Model):
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
    #   notify_recommendations / notify_weekly_trends
    #       -> REAL. app/services/market_alert_service.py detects
    #       material movement in market_data and delivers both an
    #       in-app Notification and an email through email_service.
    #
    #   notify_newsletter
    #       -> saved, and nothing sends it. A monthly newsletter is
    #       product news written by a person; there is no copy and no
    #       author, and wiring it to send something auto-generated
    #       would make an honest label into a false one. The Settings
    #       page still marks this one as not sending.
    #
    #   All three default OFF: an account must not be silently opted in
    #   to mail it never asked for. Only notify_saturation_change, a
    #   stated SME-module feature, defaults ON.
    notify_recommendations = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    notify_weekly_trends = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    notify_saturation_change = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    notify_newsletter = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    # First-time walkthrough (see static/js/tour.js). NULL means the
    # account has never been asked "is this your first time?" -- which is
    # every account that existed before the walkthrough did, so each of
    # them is asked exactly once. 'touring' while a walkthrough is in
    # progress, then 'completed' or 'skipped'. A plain string so adding a
    # state later needs no migration.
    onboarding_state = db.Column(db.String(20), nullable=True)

    # ---------------- Monitoring (Admin > Manage Users) ----------------
    # last_seen_at: the last time this account used the system, written
    # at sign-in and then at most once every LAST_SEEN_RESOLUTION by the
    # before_request hook in app/__init__.py -- a write per page view
    # would be the most expensive thing on every page. That resolution is
    # plenty for "is this account active or dormant?".
    #
    # suspended_until: a suspension's end. NULL with status 'inactive'
    # means suspended until an administrator lifts it; a date means the
    # suspension ends on its own then (lift_expired_suspension).
    last_seen_at = db.Column(db.DateTime, nullable=True)
    suspended_until = db.Column(db.DateTime, nullable=True)

    LAST_SEEN_RESOLUTION = timedelta(minutes=5)
    # An account not seen for this long is "inactive" on the Manage Users
    # page -- dormant, not suspended.
    INACTIVE_AFTER = timedelta(days=30)

    def touch_last_seen(self, now=None):
        """Record activity; True when it wrote (i.e. the stored value was
        older than LAST_SEEN_RESOLUTION)."""
        now = now or datetime.utcnow()
        if self.last_seen_at is None or now - self.last_seen_at >= self.LAST_SEEN_RESOLUTION:
            self.last_seen_at = now
            return True
        return False

    @property
    def is_suspended(self):
        return self.status == "inactive"

    def lift_expired_suspension(self, now=None):
        """End a timed suspension whose time is up. True when it did."""
        now = now or datetime.utcnow()
        if self.status == "inactive" and self.suspended_until is not None and now >= self.suspended_until:
            self.status = "active"
            self.suspended_until = None
            return True
        return False

    @property
    def activity_state(self):
        """'archived' | 'suspended' | 'online' (seen in the last 15
        minutes) | 'active' (seen within INACTIVE_AFTER) | 'inactive'
        (dormant, or never seen)."""
        if self.archived_at is not None:
            return "archived"
        if self.status == "inactive":
            return "suspended"
        if self.last_seen_at is None:
            return "inactive"
        idle = datetime.utcnow() - self.last_seen_at
        if idle <= timedelta(minutes=15):
            return "online"
        return "active" if idle <= self.INACTIVE_AFTER else "inactive"

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
        # An archived account cannot sign in whatever its status says:
        # archiving is the admin's replacement for deletion, so it has to
        # be at least as final as deletion was for access.
        return self.status == "active" and self.archived_at is None

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
