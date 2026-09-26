"""
app/controllers/admin_controller.py
--------------------------------------
Admin Module: manage user accounts, audit trail, dataset management,
system configuration -- the bullet points listed under "Admin Module"
in the paper's Specific Objectives.

Two things worth calling out because the real schema shaped this file:

1. There is no stored cluster_label column, so "how many forecasts are
   Saturated" is computed from saturation_index using the same cut
   points the training run discovered (app/ml/constants.py
   CLUSTER_THRESHOLDS), not a simple equality filter.

2. lgu_data.uploaded_by is a NOT NULL, ondelete="RESTRICT" foreign key
   -- the database itself refuses to delete a user who has any lgu_data
   row (real OR auto-generated placeholder, see
   forecasting_service.find_or_create_lgu_data) attributed to them.
   delete_user() below catches that and tells the admin to deactivate
   the account instead, rather than crashing with a raw 500 error.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import User, AuditLog, LguData, MarketData, SystemSetting, ForecastResult, Notification
from app.ml.constants import CLUSTER_THRESHOLDS
from app.utils.decorators import role_required
from app.utils.audit import log_action
from app.services.forecasting_service import SYSTEM_USER_EMAIL

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/dashboard")
@role_required("Admin")
def dashboard():
    stats = {
        "total_users": User.query.count(),
        "sme_users": User.query.filter_by(role="SME").count(),
        "lgu_users": User.query.filter_by(role="LGU").count(),
        "total_forecasts": ForecastResult.query.count(),
        "total_lgu_data": LguData.query.count(),
        "total_market_data": MarketData.query.count(),
        "total_notifications": Notification.query.count(),
    }
    recent_activity = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(15).all()
    return render_template("admin/dashboard.html", stats=stats, recent_activity=recent_activity)


# ------------------------------------------------------------------ users
@admin_bp.route("/users")
@role_required("Admin")
def users():
    all_users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin/users.html", users=all_users)


@admin_bp.route("/users/create", methods=["POST"])
@role_required("Admin")
def create_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "SME")
    contact_number = request.form.get("contact_number", "").strip()

    if not name or not email or not password or role not in ("Admin", "SME", "LGU"):
        flash("Name, email, password are required and role must be Admin, SME or LGU.", "danger")
        return redirect(url_for("admin.users"))

    if User.query.filter_by(email=email).first():
        flash("A user with that email already exists.", "danger")
        return redirect(url_for("admin.users"))

    user = User(name=name, email=email, role=role, contact_number=contact_number or None)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    log_action("admin_create_user", details=f"{role}: {email}")
    flash(f"Account created for {email}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@role_required("Admin")
def toggle_active(user_id):
    user = User.query.get_or_404(user_id)
    if user.user_id == current_user.user_id:
        flash("You cannot deactivate your own account.", "warning")
        return redirect(url_for("admin.users"))

    user.status = "inactive" if user.status == "active" else "active"
    db.session.commit()
    log_action("admin_toggle_active", details=f"user={user.email} status={user.status}")
    flash(f"{user.email} is now {user.status}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@role_required("Admin")
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.user_id == current_user.user_id:
        flash("You cannot delete your own account.", "warning")
        return redirect(url_for("admin.users"))
    if user.email == SYSTEM_USER_EMAIL:
        flash("The system account can't be deleted -- it owns auto-generated placeholder LGU data records.", "warning")
        return redirect(url_for("admin.users"))

    email = user.email
    try:
        db.session.delete(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            f"Cannot delete {email}: this account has government data on file "
            "(lgu_data.uploaded_by keeps a record and the database won't let it be deleted). "
            "Deactivate the account instead.",
            "danger",
        )
        return redirect(url_for("admin.users"))

    log_action("admin_delete_user", details=f"user={email}")
    flash(f"Deleted account {email}.", "success")
    return redirect(url_for("admin.users"))


# ------------------------------------------------------------- audit trail
@admin_bp.route("/audit-log")
@role_required("Admin")
def audit_log():
    page = request.args.get("page", 1, type=int)
    pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=40, error_out=False)
    return render_template("admin/audit_log.html", pagination=pagination)


# --------------------------------------------------------------- datasets
@admin_bp.route("/datasets")
@role_required("Admin")
def datasets():
    lgu_rows = LguData.query.order_by(LguData.upload_date.desc(), LguData.lgu_id.desc()).limit(100).all()
    market_rows = MarketData.query.order_by(MarketData.date_recorded.desc(), MarketData.market_id.desc()).limit(100).all()
    market_total = MarketData.query.count()
    market_real_count = MarketData.query.filter_by(source="Google Places API").count()
    return render_template(
        "admin/datasets.html",
        lgu_rows=lgu_rows,
        market_rows=market_rows,
        market_total=market_total,
        market_real_count=market_real_count,
        places_api_key_configured=bool(current_app.config.get("GOOGLE_PLACES_API_KEY")),
    )


@admin_bp.route("/datasets/export-barangay-seed-csv")
@role_required("Admin")
def export_barangay_seed_csv():
    """Downloads the full 76-barangay reference/seed dataset (see
    app/ml/seed_data.py) as a CSV file -- every sourced/generated field
    for every barangay, not just the columns the ML model consumes.
    Same export as running `python export_seed_data.py` -- this route
    just streams it as a download instead of writing a local file."""
    import io

    from flask import Response

    from app.ml.seed_data import write_seed_data_csv

    buffer = io.StringIO()
    write_seed_data_csv(buffer)
    log_action("export_barangay_seed_csv")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=barangay_seed_data.csv"},
    )


@admin_bp.route("/datasets/lgu/<int:lgu_id>/delete", methods=["POST"])
@role_required("Admin")
def delete_lgu_data(lgu_id):
    row = LguData.query.get_or_404(lgu_id)
    barangay = row.barangay
    # NOTE: forecast_result.lgu_id is ondelete="CASCADE" -- deleting this
    # row also deletes every ForecastResult that used it as its LGU
    # snapshot. That's real behavior from the schema you supplied, not
    # something added here; the flash below warns about it every time.
    db.session.delete(row)
    db.session.commit()
    log_action("admin_delete_lgu_data", details=f"barangay={barangay}")
    flash(f"Deleted LGU data record for {barangay}. Any forecasts built on it were removed too (CASCADE).", "warning")
    return redirect(url_for("admin.datasets"))


@admin_bp.route("/datasets/market/<int:market_id>/delete", methods=["POST"])
@role_required("Admin")
def delete_market_data(market_id):
    row = MarketData.query.get_or_404(market_id)
    label = f"{row.industry_type} @ {row.location}"
    db.session.delete(row)
    db.session.commit()
    log_action("admin_delete_market_data", details=label)
    flash(f"Deleted market data record for {label}. Any forecasts built on it were removed too (CASCADE).", "warning")
    return redirect(url_for("admin.datasets"))


# ------------------------------------------------------------ settings
@admin_bp.route("/settings", methods=["GET", "POST"])
@role_required("Admin")
def settings():
    if request.method == "POST":
        for key in (
            "kmeans_n_clusters",
            "msi_weight_competitor_density",
            "msi_weight_demand_trend",
            "msi_weight_sociodemographic",
            "saturation_alert_threshold",
            "places_max_results",
        ):
            value = request.form.get(key)
            if value not in (None, ""):
                SystemSetting.set(key, value)

        SystemSetting.set("use_llm_recommendations", "true" if request.form.get("use_llm_recommendations") else "false")
        log_action("admin_update_settings")
        flash("System settings updated.", "success")
        return redirect(url_for("admin.settings"))

    SystemSetting.ensure_defaults()
    all_settings = {row.setting_key: row for row in SystemSetting.query.all()}
    return render_template("admin/settings.html", settings=all_settings, cluster_thresholds=CLUSTER_THRESHOLDS)


# --------------------------------------------------------- LLM status
@admin_bp.route("/llm-status", methods=["GET"])
@admin_bp.route("/llm-status/probe", methods=["GET", "POST"], endpoint="llm_probe")
@role_required("Admin")
def llm_status():
    """Answers "why is the page still showing rule-based text?" in one
    place, as JSON.

    This exists because that question had no answer from outside the
    process. Every failure in llm_service was caught and discarded, so
    a wrong API key, a model name the endpoint does not recognise, a
    spent quota and a malformed response all produced the same
    thing -- rule-based wording, no log line, no clue. On a hosted
    deployment the only way to tell them apart was to add print
    statements and redeploy.

    /admin/llm-status reports the configuration and the most recent
    failure. /admin/llm-status/probe additionally makes a real
    one-sentence call and reports the provider's own answer, or the
    provider's own error.

    NO SECRET IS RETURNED. See llm_service.llm_status(): the key is
    reported as set/not-set, its length, and its first four
    characters, which is enough to tell an AI Studio key ("AIza") from
    an OpenRouter key ("sk-o") from an OAuth access token ("AQ.",
    "ya29") -- the mistake that actually happens -- and useless to
    anyone else. Admin-only on top of that.
    """
    from flask import jsonify

    from app.services import llm_service

    payload = {"status": llm_service.llm_status()}
    if request.path.endswith("/probe"):
        log_action("admin_llm_probe")
        payload["probe"] = llm_service.probe()
        # The status is re-read AFTER the probe so last_failure
        # reflects this attempt rather than whatever failed before it.
        payload["status"] = llm_service.llm_status()
    else:
        payload["hint"] = "Open /admin/llm-status/probe to make a real test call."
    return jsonify(payload)
