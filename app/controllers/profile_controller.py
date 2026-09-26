"""
app/controllers/profile_controller.py
----------------------------------------
SETTINGS -- one page, shared by every role, split into sections:

    Profile        name, contact number, profile picture, and a summary
                   of this account's activity
    Business       (SME only) the business plans themselves, with the
    Preferences    inline edit/delete that used to be the "My Plans"
                   popup in the top bar -- see the note below
    Notifications  the notification preference switches, plus the inbox
                   itself: mark everything read, clear what has been read
    Appearance     light or dark theme
    Security       change password, and what the account actually is

WHY "MY PLANS" MOVED HERE. It was a Bootstrap modal living in
shared/_topbar.html, which base.html includes on every authenticated
page. That put its markup and ~130 lines of script into every response
for every role -- including LGU and Admin accounts, which have no
sme_profile rows and could never see anything in it. As a Settings pane
it is rendered once, by the server, only for the accounts it applies to,
and it still uses the same /home/plans/<id>/update and /delete endpoints
the popup did.

The blueprint and endpoint are still named `profile` / `profile.settings`
even though the page is now "Settings". Renaming them would mean touching
every url_for() in the templates for no behavioural gain; the URL moved to
/settings and /profile-settings redirects there, so old links and
bookmarks keep working.

WHERE THE PREFERENCES LIVE. `theme` and `notify_early_warning` are
columns on the `user` row (see app/models/user.py), not rows in a
preferences table. Each account already has exactly one user row, so
storing two preferences on it costs no rows at all, where a key/value
table would cost one per user per setting.

The section list is deliberately short. Every control here changes
something real -- the theme is applied on the next page render, the
alert switch is checked before an early-warning notification is
created. A settings page whose switches do nothing is worse than no
settings page, so there is no "email me digests" toggle: nothing in
this app sends digests.

Also handles the (additive, see app/models/user.py) profile picture
upload/removal for ALL THREE roles (SME, LGU, Admin) -- stored as a
base64 data: URI directly on the user row, no upload folder needed.
"""

import base64

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import SmeProfile, PlanSave, ForecastResult, Notification, SystemSetting
from app.models.user import User
from app.utils.audit import log_action

profile_bp = Blueprint("profile", __name__)

# Kept deliberately small -- this is stored inline in the database (a
# LONGTEXT column), not as a file, so there's no reason to allow a huge
# upload. 2 MB is plenty for a profile photo.
MAX_AVATAR_BYTES = 2 * 1024 * 1024

# The panes the Settings page offers, in order. ?section= picks which one
# opens; anything else falls back to the first.
#
# "plans" is Business Preferences, and it is SME-ONLY: the template
# renders neither the tab nor the pane for an LGU or Admin account,
# which have no sme_profile rows. SME_ONLY_SECTIONS is what stops a
# stale ?section=plans link from leaving one of those accounts looking
# at a Settings page with every pane hidden and no tab selected.
SETTINGS_SECTIONS = ("profile", "plans", "notifications", "appearance", "security")
SME_ONLY_SECTIONS = ("plans",)

# The Notification Preferences switches, in the order they are shown.
#
#   (column, label, wired)
#
# `wired` records whether anything in this project ACTS on the
# preference. Exactly one does today: saturation-change alerts are
# checked before an early-warning notification is created. The other
# three are saved faithfully and will be honoured the moment a sender
# exists, but email_service.py can currently only send a registration
# code and there is no scheduled job here -- so the page says so instead
# of implying mail is going out. The flag is what drives that label, so
# it cannot drift out of date silently.
NOTIFICATION_PREFS = (
    ("notify_recommendations", "Email notifications for new recommendations",
     "When the AI surfaces new locations worth entering.", False),
    ("notify_weekly_trends", "Weekly market trend reports",
     "A digest of how saturation moved across the city.", False),
    ("notify_saturation_change", "Alert me when saturation levels change",
     "Fires when a forecast crosses the saturation threshold.", True),
    ("notify_newsletter", "Monthly newsletter",
     "Product news and what changed in the data.", False),
)
NOTIFICATION_FIELDS = tuple(field for field, _label, _blurb, _wired in NOTIFICATION_PREFS)

# (file signature bytes, mime type) -- checked against the file's own
# content rather than trusting its extension/browser-supplied
# Content-Type, which are both trivially spoofable.
_IMAGE_SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _detect_image_mime(head_bytes):
    """Sniffs PNG/JPEG/GIF/WEBP from the file's own magic bytes -- no
    Pillow/python-magic dependency needed, and no reliance on the
    filename extension or the browser-supplied Content-Type."""
    for signature, mime in _IMAGE_SIGNATURES:
        if head_bytes.startswith(signature):
            return mime
    if head_bytes[:4] == b"RIFF" and head_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


@profile_bp.route("/profile-settings")
@login_required
def legacy_settings_redirect():
    """The page lived here before it became Settings. Kept so bookmarks,
    the browser history and anything linking to the old path still
    land somewhere useful."""
    return redirect(url_for("profile.settings"), code=301)


@profile_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        form_type = request.form.get("form_type", "profile")

        if form_type == "avatar":
            file = request.files.get("profile_picture")
            if not file or not file.filename:
                flash("Choose an image file first.", "danger")
                return redirect(url_for("profile.settings"))

            raw = file.read(MAX_AVATAR_BYTES + 1)
            if len(raw) > MAX_AVATAR_BYTES:
                flash("That image is too large -- please use one under 2 MB.", "danger")
                return redirect(url_for("profile.settings"))

            mime = _detect_image_mime(raw)
            if mime is None:
                flash("Unsupported file -- please upload a PNG, JPG, GIF, or WEBP image.", "danger")
                return redirect(url_for("profile.settings"))

            current_user.profile_picture = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
            db.session.commit()
            log_action("update_profile_picture")
            flash("Profile picture updated.", "success")
            return redirect(url_for("profile.settings"))

        if form_type == "remove_avatar":
            current_user.profile_picture = None
            db.session.commit()
            log_action("remove_profile_picture")
            flash("Profile picture removed.", "success")
            return redirect(url_for("profile.settings"))

        if form_type == "appearance":
            theme = (request.form.get("theme") or "").strip().lower()
            if theme not in User.THEMES:
                flash("Unknown theme.", "danger")
            else:
                current_user.theme = theme
                db.session.commit()
                log_action("update_theme", details=f"theme={theme}")
                flash("Appearance updated.", "success")
            return redirect(url_for("profile.settings", section="appearance"))

        if form_type == "notifications":
            # An unchecked checkbox submits NOTHING at all, so absence is
            # the "off" signal. Reading it as a value ("off"/"false")
            # would leave every switch permanently on.
            saved = {}
            for field in NOTIFICATION_FIELDS:
                value = bool(request.form.get(field))
                setattr(current_user, field, value)
                saved[field] = value
            db.session.commit()
            log_action("update_notification_prefs",
                       details=", ".join(f"{k}={v}" for k, v in saved.items()))
            flash("Notification preferences saved.", "success")
            return redirect(url_for("profile.settings", section="notifications"))

        if form_type == "mark_all_read":
            updated = Notification.query.filter_by(user_id=current_user.user_id, is_read=False).update(
                {"is_read": True}, synchronize_session=False
            )
            db.session.commit()
            flash(f"Marked {updated} notification(s) as read." if updated else "Nothing unread.", "success")
            return redirect(url_for("profile.settings", section="notifications"))

        if form_type == "clear_read":
            # Deletes only what has already been read. Clearing an unread
            # early warning the visitor has never seen would defeat the
            # entire point of raising it.
            removed = Notification.query.filter_by(user_id=current_user.user_id, is_read=True).delete(
                synchronize_session=False
            )
            db.session.commit()
            log_action("clear_notifications", details=f"removed={removed}")
            flash(f"Cleared {removed} read notification(s)." if removed else "Nothing to clear.", "success")
            return redirect(url_for("profile.settings", section="notifications"))

        if form_type == "password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not current_user.check_password(current_password):
                flash("Current password is incorrect.", "danger")
            elif len(new_password) < 6:
                flash("New password must be at least 6 characters.", "danger")
            elif new_password != confirm_password:
                flash("New passwords do not match.", "danger")
            else:
                current_user.set_password(new_password)
                db.session.commit()
                log_action("change_password")
                flash("Password updated.", "success")
            return redirect(url_for("profile.settings"))

        name = request.form.get("name", "").strip()
        if name:
            current_user.name = name
        current_user.contact_number = request.form.get("contact_number", "").strip() or None
        db.session.commit()
        log_action("update_profile")
        flash("Profile updated.", "success")
        return redirect(url_for("profile.settings"))

    saved_plans_count = PlanSave.query.filter_by(user_id=current_user.user_id).count()
    reports_viewed_count = (
        ForecastResult.query.join(SmeProfile, ForecastResult.sme_id == SmeProfile.sme_id)
        .filter(SmeProfile.user_id == current_user.user_id)
        .count()
        if current_user.is_sme()
        else 0
    )
    # The plans shown in Business Preferences, newest first. Rendered
    # server-side so the pane is correct before any script runs; the
    # Edit and Delete buttons on each row then POST to sme_controller's
    # JSON routes via fetch(), which is why this page itself never
    # redirects for those actions.
    sme_profiles = (
        current_user.sme_profiles.order_by(SmeProfile.sme_id.desc()).all() if current_user.is_sme() else []
    )

    notifications = (
        Notification.query.filter_by(user_id=current_user.user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(20)
        .all()
    )
    unread_count = Notification.query.filter_by(user_id=current_user.user_id, is_read=False).count()
    read_count = Notification.query.filter_by(user_id=current_user.user_id, is_read=True).count()

    # `section` only picks which pane opens first; an unknown value is
    # ignored rather than 404'd, because it arrives from a redirect or a
    # bookmark, not from anything security-relevant.
    section = (request.args.get("section") or "profile").strip().lower()
    if section not in SETTINGS_SECTIONS:
        section = "profile"
    if section in SME_ONLY_SECTIONS and not current_user.is_sme():
        section = "profile"

    return render_template(
        "shared/settings.html",
        saved_plans_count=saved_plans_count,
        reports_viewed_count=reports_viewed_count,
        sme_profiles=sme_profiles,
        notifications=notifications,
        unread_count=unread_count,
        read_count=read_count,
        active_section=section,
        notification_prefs=NOTIFICATION_PREFS,
        # Shown read-only in Notifications: the threshold is a single
        # system-wide value an Admin controls, not a per-account one, so
        # displaying it as an editable field here would be a lie.
        alert_threshold=SystemSetting.get_float("saturation_alert_threshold", 75.0),
    )
