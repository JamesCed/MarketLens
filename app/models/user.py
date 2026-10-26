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
from app.utils.privacy import EncryptedText


class User(ArchivableMixin, UserMixin, db.Model):
    __tablename__ = "user"

    user_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum("Admin", "SME", "LGU", name="user_role"), nullable=False)
    lgu_role = db.Column(db.String(20), nullable=True)

    first_name = db.Column(db.String(60), nullable=True)
    middle_name = db.Column(db.String(60), nullable=True)
    last_name = db.Column(db.String(60), nullable=True)
    suffix = db.Column(db.String(10), nullable=True)

    contact_number = db.Column("contact_number_enc", EncryptedText, nullable=True)
    legacy_contact_number = db.Column("contact_number", db.String(20), nullable=True)
    gender = db.Column("gender_enc", EncryptedText, nullable=True)
    birthdate_iso = db.Column("birthdate_enc", EncryptedText, nullable=True)
    address_line = db.Column("address_line_enc", EncryptedText, nullable=True)
    address_barangay = db.Column("address_barangay_enc", EncryptedText, nullable=True)
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.Enum("active", "inactive", name="user_status"), nullable=False, default="active")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    profile_picture = db.Column(db.Text().with_variant(LONGTEXT(),"mysql"), nullable=True)

    theme = db.Column(db.String(10), nullable=False, default="light", server_default="light")

    notify_recommendations = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    notify_weekly_trends = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    notify_saturation_change = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    notify_newsletter = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    onboarding_state = db.Column(db.String(20), nullable=True)

    last_seen_at = db.Column(db.DateTime, nullable=True)
    suspended_until = db.Column(db.DateTime, nullable=True)

    LAST_SEEN_RESOLUTION = timedelta(minutes=5)
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

    @property
    def notify_early_warning(self):
        return self.notify_saturation_change

    @notify_early_warning.setter
    def notify_early_warning(self, value):
        self.notify_saturation_change = bool(value)

    THEMES = ("light", "dark")

    @property
    def resolved_theme(self):
        """The theme value safe to render into the page."""
        return self.theme if self.theme in self.THEMES else "light"

    sme_profiles = db.relationship("SmeProfile", backref="owner", lazy="dynamic", cascade="all, delete-orphan")
    notifications = db.relationship("Notification", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    uploaded_lgu_data = db.relationship("LguData", backref="uploader", lazy="dynamic")

    def get_id(self):
        return str(self.user_id)

    @property
    def is_active(self):
        return self.status == "active" and self.archived_at is None

    @property
    def birthdate(self):
        from datetime import date as _date

        try:
            return _date.fromisoformat(self.birthdate_iso) if self.birthdate_iso else None
        except ValueError:
            return None

    @birthdate.setter
    def birthdate(self, value):
        self.birthdate_iso = value.isoformat() if value else None

    @property
    def age(self):
        from app.services.account_params import age_on

        return age_on(self.birthdate)

    @property
    def address_display(self):
        from app.services.account_params import CITY

        parts = [p for p in (self.address_line, self.address_barangay) if p]
        return ", ".join(parts + [CITY]) if parts else None

    @property
    def name_parts(self):
        """(first, middle, last) for the Settings form. An account from
        before the name was asked in parts gets a best guess from its
        display name -- the first word and the last -- so the owner only
        has to check it, not retype it."""
        if self.first_name or self.last_name:
            return self.first_name or "", self.middle_name or "", self.last_name or ""
        words = (self.name or "").split()
        if len(words) >= 2:
            return " ".join(words[:-1]), "", words[-1]
        return (words[0] if words else ""), "", ""

    @property
    def personal_info_complete(self):
        """True once every detail sign-up now asks for is on file -- an
        account from before that stays usable, but Settings asks its
        owner to fill the gaps."""
        return all((self.first_name, self.last_name, self.gender, self.birthdate_iso,
                    self.contact_number, self.address_line, self.address_barangay))

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

    LGU_ROLES = {"encoder": "LGU Encoder", "steward": "LGU Data Steward"}

    @property
    def lgu_role_key(self):
        """'encoder' or 'steward' for an LGU account (a blank value counts
        as encoder), None for any other role. See staging_service."""
        if self.role != "LGU":
            return None
        return self.lgu_role if self.lgu_role in self.LGU_ROLES else "encoder"

    @property
    def lgu_role_label(self):
        key = self.lgu_role_key
        return self.LGU_ROLES[key] if key else None

    def is_lgu_encoder(self):
        return self.lgu_role_key == "encoder"

    def is_data_steward(self):
        return self.lgu_role_key == "steward"

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
