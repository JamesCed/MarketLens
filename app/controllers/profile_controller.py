"""
app/controllers/profile_controller.py
----------------------------------------
SETTINGS -- one page, shared by every role, split into sections:

    Profile        name, contact number, profile picture, and a summary
                   of this account's activity
    Notifications  the notification preference switches, plus the inbox
                   itself: mark everything read, clear what has been read
    Appearance     light or dark theme
    Security       change password, and what the account actually is
    Tutorial       replay the guided tour from step 1, and what this
                   account's walkthrough state is, in words

The link to this page sits in the sidebar FOOTER, directly above
Support (shared/_sidebar.html), not in the list of pages: it looks
after the account, not the market.

WHERE THE PLANS WENT. Settings used to have a sixth, SME-only pane,
"Business Preferences", holding the business plans and their inline
Edit/Delete. The plans are managed on the Home page now, next to the
plan they act on: a pencil edits a plan, a bin moves it to Trash (an
archive -- nothing is deleted for good), and the Trash button restores
it (see sme_controller's update_plan / trash_plan / restore_plan). Two
places to edit the same plan would only invite the question of which
one was authoritative, so the pane is gone rather than kept alongside.
A stale ?section=plans link -- a bookmark, the old top-bar shortcut in
someone's history -- still lands somewhere useful: an SME is redirected
to Home, where the plans now are; any other account falls back to
Profile, exactly as before.

WHY "TAKE THE TOUR" MOVED HERE. It was a button in the sidebar footer.
It now lives in the Tutorial pane, as a [data-tour-replay] button that
static/js/tour.js binds on every page -- so the replay itself is
unchanged, only where it is found. What did NOT change is the first
visit: an account whose onboarding_state is NULL is still asked "Is
this your first time here?" on its first signed-in page
(shared/_onboarding.html + tour.js boot()), Settings or not.

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
something real -- the theme is applied on the next page render, and the
notification switches are each checked by market_alert_service before
anything is written or sent. A settings page whose switches do nothing
is worse than no settings page, so the one switch that still has no
sender behind it (the monthly newsletter, which would need a human to
write it) says so on its own label rather than pretending.

Also handles the (additive, see app/models/user.py) profile picture
upload/removal for ALL THREE roles (SME, LGU, Admin) -- stored as a
base64 data: URI directly on the user row, no upload folder needed.
"""

import base64

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.controllers.onboarding_controller import describe_walkthrough
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
# Every pane is now offered to every role -- Tutorial included, since
# all three roles have a tour (static/js/tour_steps.js) -- so
# SME_ONLY_SECTIONS is empty. It is kept, rather than deleted, because
# the check below that reads it is what stops a role-specific pane from
# ever leaving another role looking at a Settings page with every pane
# hidden and no tab selected; the next SME-only pane only has to be
# listed here.
SETTINGS_SECTIONS = ("profile", "notifications", "appearance", "security", "tutorial")
SME_ONLY_SECTIONS = ()

# Sections that no longer exist, and where a link to one should go
# instead. "plans" was Business Preferences: the plans are managed on
# Home now, so for an SME an old ?section=plans link is redirected there
# (302 -- a bookmark that keeps working, not a permanent rename of
# Settings). Any other role never had that pane and simply gets Profile,
# as it always did.
SME_SECTION_REDIRECTS = {"plans": "sme.home"}

# The Notification Preferences switches, in the order they are shown.
#
#   (column, label, blurb, wired)
#
# `wired` records whether anything in this project ACTS on the
# preference -- it is what draws the "not sending yet" badge, so a
# switch that does nothing cannot quietly look like one that works.
#
# THREE OF THESE JUST BECAME TRUE. app/services/market_alert_service.py
# now detects real movement in the market and delivers it, in-app and by
# email, through email_service:
#
#   notify_saturation_change -> run_market_alert_sweep(), warning half.
#   notify_recommendations   -> run_market_alert_sweep(), opportunity
#                               half: barangays that became LESS
#                               saturated in an industry the account is
#                               planning in.
#   notify_weekly_trends     -> run_weekly_trend_digest(), at most once
#                               a week and only when something moved.
#
# THE FOURTH IS STILL FALSE, AND DELIBERATELY STAYS FALSE. A monthly
# newsletter is product news written by a person; there is no copy to
# send and no author, and no amount of code changes that. Wiring it to
# send an auto-generated "newsletter" would be worse than the badge --
# it would make the honest label into a false one. So it keeps saying
# "not sending yet", which is exactly what is true.
NOTIFICATION_PREFS = (
    ("notify_recommendations", "Email notifications for new recommendations",
     "When a barangay in your industry becomes less saturated and worth a look.", True),
    ("notify_weekly_trends", "Weekly market trend reports",
     "A digest of how saturation moved -- at most weekly, and only when it moved.", True),
    ("notify_saturation_change", "Alert me when saturation levels change",
     "When a market you follow moves, or a forecast crosses the saturation threshold.", True),
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

    # `section` only picks which pane opens first; an unknown value is
    # ignored rather than 404'd, because it arrives from a redirect or a
    # bookmark, not from anything security-relevant. Read before any
    # query so a retired section's redirect costs nothing.
    section = (request.args.get("section") or "profile").strip().lower()
    if section in SME_SECTION_REDIRECTS and current_user.is_sme():
        return redirect(url_for(SME_SECTION_REDIRECTS[section]))
    if section not in SETTINGS_SECTIONS:
        section = "profile"
    if section in SME_ONLY_SECTIONS and not current_user.is_sme():
        section = "profile"

    # Both counts JOIN through to the plan, and that join is what makes
    # them counts of live plans. A plan moved to Trash keeps its
    # forecast_result and plan_saves rows -- that is what lets restore
    # bring everything back -- and neither table carries an archive
    # stamp of its own, so the global archive filter (app/models/
    # archive.py) can only drop a trashed plan's rows where SmeProfile
    # is in the query. Counting plan_saves on its own kept a trashed
    # plan in "Saved Plans" while "Forecasts Run", right beside it,
    # dropped it: two numbers on one card disagreeing about the same
    # plan. Before Trash this never showed, because deleting a plan
    # cascaded its bookmarks away.
    saved_plans_count = (
        PlanSave.query.join(ForecastResult, PlanSave.forecast_result_id == ForecastResult.forecast_id)
        .join(SmeProfile, ForecastResult.sme_id == SmeProfile.sme_id)
        .filter(PlanSave.user_id == current_user.user_id)
        .count()
    )
    reports_viewed_count = (
        ForecastResult.query.join(SmeProfile, ForecastResult.sme_id == SmeProfile.sme_id)
        .filter(SmeProfile.user_id == current_user.user_id)
        .count()
        if current_user.is_sme()
        else 0
    )

    notifications = (
        Notification.query.filter_by(user_id=current_user.user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(20)
        .all()
    )
    unread_count = Notification.query.filter_by(user_id=current_user.user_id, is_read=False).count()
    read_count = Notification.query.filter_by(user_id=current_user.user_id, is_read=True).count()

    return render_template(
        "shared/settings.html",
        saved_plans_count=saved_plans_count,
        reports_viewed_count=reports_viewed_count,
        notifications=notifications,
        unread_count=unread_count,
        read_count=read_count,
        active_section=section,
        notification_prefs=NOTIFICATION_PREFS,
        # The Tutorial pane says where this account's walkthrough stands
        # -- completed, skipped, in progress, never started -- in words
        # rather than as the raw onboarding_state value. Worded by
        # onboarding_controller, which owns what those states mean.
        walkthrough=describe_walkthrough(current_user.onboarding_state),
        # Shown read-only in Notifications: the threshold is a single
        # system-wide value an Admin controls, not a per-account one, so
        # displaying it as an editable field here would be a lie.
        alert_threshold=SystemSetting.get_float("saturation_alert_threshold", 75.0),
        # Which address the alert emails arrive from, and whether email
        # is configured on this deployment at all.
        #
        # "Why does it say nothing is sending -- is there no sender?" was
        # a fair question to have to ask, and the page could not answer
        # it. Now it can: the sending address is named, and an account
        # whose switches are on but whose deployment has no mail
        # transport is told that in-app notifications still arrive,
        # rather than being left to wonder where the email went.
        **_alert_sender_context(),
    )


def _alert_sender_context():
    from app.services import email_service

    try:
        configured = email_service.is_configured()
        sender = email_service._from_address()[1]
    except Exception:  # pragma: no cover - defensive; Settings must render
        configured, sender = False, ""
    return {"email_configured": bool(configured), "alert_sender": sender}
