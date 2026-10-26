"""
app/controllers/lgu_controller.py
------------------------------------
LGU-only pages: the LGU dashboard and, most importantly, the Gov't
Data Upload page.

LGU-ONLY -- the dashboard, the Diversification Plan and the Gov't Data
Upload. Enforced two ways:
  1. @role_required("LGU") on every route below -- an SME or an Admin
     account hitting these URLs directly gets HTTP 403, not just a
     hidden link. Admin used to share these pages; it no longer does.
     The Admin module's purpose is to run the SYSTEM (accounts, audit
     trail, datasets, settings, activity), not to plan the city or to
     upload the city's records -- an administrator reviews, archives
     and restores uploaded rows from Admin > Datasets instead.
  2. The sidebar nav (app/templates/shared/_sidebar.html) only renders
     these links for an LGU account.

The uploaded file itself is NOT tracked anywhere (the real lgu_data
table has no filename/size/status columns -- see
app/models/lgu_data.py) -- only the PARSED DATA ROWS it produces are
kept, straight in lgu_data or market_data depending on which target
table the LGU user picks. The temporary file on disk is deleted right
after processing.
"""

import os

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import current_user

from app.models import LguData, MarketData, ForecastResult
from app.ml.constants import CLUSTER_THRESHOLDS
from app.models.upload_batch import STATUS_APPROVED, STATUS_PENDING, STATUS_PUBLISHED, STATUS_REJECTED
from app.services import staging_service
from app.utils.decorators import lgu_role_required, role_required
from app.utils.helpers import allowed_file, unique_upload_path
from app.utils.audit import log_action
from app.services.diversification_service import build_diversification_plan
from app.services.data_import_service import (
    active_lgu_dataset_summary,
    has_active_lgu_data,
    process_upload,
)

lgu_bp = Blueprint("lgu", __name__)

LGU_SOURCES = ["DTI", "CLUP", "Other"]
MARKET_SOURCES = ["PSA", "DTI", "Manual"]


@lgu_bp.route("/lgu/dashboard")
@role_required("LGU")
def dashboard():
    """The LGU's city-wide counterpart to the SME Home page.

    Same shape as sme/home.html -- hero + city-wide search, a row of
    live industry cards, then the analysis panels -- but every number is
    CITY-WIDE rather than scoped to one business plan, and it keeps all
    the original LGU planning stats (barangays on file, forecasts
    generated, saturated vs. opportunity zones, diversification
    guidance) that this page already had.

    Below that, every barangay is listed with how many businesses are on
    file for it, and how many of those came back from a live Google
    Places API lookup -- see
    trend_analytics_service.get_barangay_business_table().
    """
    from app.extensions import db
    from app.ml.constants import BUSINESS_TYPES, FEATURED_BUSINESS_TYPES, INDUSTRY_DISPLAY, DEFAULT_INDUSTRY_DISPLAY
    from app.ml.seed_data import BARANGAY_NAMES, get_real_population
    from app.services.forecasting_service import _scoring_inputs_fingerprint, compute_scores_batch
    from app.services.trend_analytics_service import (
        get_barangay_business_table,
        latest_market_data_by_key,
        live_plan_forecasts,
    )

    from sqlalchemy import case, func

    total_forecasts, saturated_count, opportunity_count = live_plan_forecasts().with_entities(
        func.count(ForecastResult.forecast_id),
        func.sum(case((ForecastResult.saturation_index > CLUSTER_THRESHOLDS[2], 1), else_=0)),
        func.sum(case((ForecastResult.saturation_index <= CLUSTER_THRESHOLDS[0], 1), else_=0)),
    ).one()
    total_forecasts = int(total_forecasts or 0)
    saturated_count = int(saturated_count or 0)
    opportunity_count = int(opportunity_count or 0)

    from app.ml.seed_data import canonical_barangay

    from app.services.data_import_service import _system_user_id_or_none
    from app.services.forecasting_service import PLACEHOLDER_ZONING_NOTE

    system_user_id = _system_user_id_or_none()
    on_file, unrecognised = set(), set()
    names = db.session.query(LguData.barangay, LguData.uploaded_by, LguData.zoning_info).distinct()
    for raw_name, uploaded_by, zoning_info in names.all():
        official = canonical_barangay(raw_name)
        if official:
            on_file.add(official)
        elif (raw_name or "").strip() and uploaded_by != system_user_id and zoning_info != PLACEHOLDER_ZONING_NOTE:
            unrecognised.add(raw_name.strip())
    barangays_on_file = sorted(on_file)
    unrecognised_barangays = sorted(unrecognised)

    latest_market = latest_market_data_by_key()
    barangay_rows = get_barangay_business_table(latest_market)

    sample_barangays = sorted(BARANGAY_NAMES, key=lambda b: -(get_real_population(b) or 0))[:6]

    businesses_by_industry = {}
    for (industry_type, _location), row in latest_market.items():
        businesses_by_industry[industry_type] = businesses_by_industry.get(industry_type, 0) + int(
            row.competitor_count or 0
        )

    pairs = [(industry_type, barangay)
             for industry_type in BUSINESS_TYPES
             for barangay in sample_barangays]
    scores_by_industry = {}
    for (industry_type, _barangay), score in zip(pairs, compute_scores_batch(pairs)):
        scores_by_industry.setdefault(industry_type, []).append(score)

    industry_cards = []
    for industry_type in BUSINESS_TYPES:
        scores = scores_by_industry.get(industry_type, [])
        avg_viability = round(sum(s["viability_score"] for s in scores) / len(scores), 1) if scores else 0.0
        avg_saturation = round(sum(s["saturation_index"] for s in scores) / len(scores), 1) if scores else 0.0
        display = INDUSTRY_DISPLAY.get(industry_type, DEFAULT_INDUSTRY_DISPLAY)
        band = "good" if avg_viability >= 6.5 else ("fair" if avg_viability >= 4 else "low")
        industry_cards.append(
            {
                "name": industry_type,
                "score": avg_viability,
                "saturation": avg_saturation,
                "businesses": businesses_by_industry.get(industry_type, 0),
                "band": band,
                "trend": {"good": "up", "fair": "neutral", "low": "down"}[band],
                "icon": display["icon"],
                "bi": display.get("bi", "bi-briefcase"),
                "hue": display.get("hue", "neutral"),
                "short": display.get("short") or industry_type,
                "subtitle": display["subtitle"],
                "is_current": False,
            }
        )

    totals = {
        "businesses": sum(int(r.competitor_count or 0) for r in latest_market.values()),
        "from_places_api": sum(
            int(r.competitor_count or 0) for r in latest_market.values() if r.source == "Google Places API"
        ),
        "barangays_with_businesses": len([r for r in barangay_rows if r["total_businesses"] > 0]),
    }

    return render_template(
        "lgu/dashboard.html",
        total_forecasts=total_forecasts,
        saturated_count=saturated_count,
        opportunity_count=opportunity_count,
        barangays_on_file=barangays_on_file,
        unrecognised_barangays=unrecognised_barangays,
        barangay_total=len(BARANGAY_NAMES),
        lgu_data_count=_scoring_inputs_fingerprint()[1][0],
        market_data_count=_scoring_inputs_fingerprint()[0][1],
        industry_cards=industry_cards,
        barangay_rows=barangay_rows,
        totals=totals,
        business_types=BUSINESS_TYPES,
        locations=BARANGAY_NAMES,
        sample_barangays=sample_barangays,
        saturated_threshold=CLUSTER_THRESHOLDS[2],
        has_lgu_data=has_active_lgu_data(),
        lgu_dataset=active_lgu_dataset_summary(),
        plan=build_diversification_plan(),
        default_industry=FEATURED_BUSINESS_TYPES[0],
    )


@lgu_bp.route("/lgu/diversification")
@role_required("LGU")
def diversification():
    """The Diversification Plan: how concentrated each barangay's economy
    is, which industries to encourage or regulate city-wide, and a
    prioritised, per-barangay action list -- see
    app/services/diversification_service.py for every measure."""
    log_action("view_diversification_plan")
    return render_template("lgu/diversification.html", plan=build_diversification_plan())


@lgu_bp.route("/lgu/diversification.csv")
@role_required("LGU")
def diversification_csv():
    """The per-barangay plan as a spreadsheet, for council briefings."""
    import csv
    import io

    from flask import Response

    plan = build_diversification_plan()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["barangay", "population", "businesses", "businesses_per_1000", "industries_present",
                     "diversity_score", "dominant_industry", "dominant_share_percent", "need_score",
                     "why_priority", "encourage", "review_permits", "actions"])
    for b in plan["barangays"]:
        writer.writerow([
            b["location"], b["population"], b["businesses"], b["per_thousand"], b["industries_present"],
            b["diversity"], b["dominant_full"], b["dominant_share"], b["need_score"],
            "; ".join(b["reasons"]), "; ".join(x["industry"] for x in b["promote"]),
            "; ".join(x["industry"] for x in b["limit"]), "; ".join(b["actions"]),
        ])
    log_action("export_diversification_plan")
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=diversification_plan.csv"})


IMPORTANCE = {
    "required": ("Required", "The upload is refused without it. Fill it on every row."),
    "important": ("Important", "Used by the analysis. A blank cell is allowed and handled as described."),
    "record": ("For records", "Saved and shown in the dataset records; not part of the calculations."),
}

UPLOAD_COLUMNS = {
    "LGU_DATA": [
        ("barangay", "required",
         "Official Tarlac City barangay name, one row per barangay. Case is forgiven; a "
         "misspelt name is rejected with a suggestion rather than imported.",
         "San Roque"),
        ("zoning_info", "required",
         "Zoning classification of the barangay's business area: Commercial, Residential, "
         "Mixed-use, Industrial, Institutional or Agricultural.",
         "Commercial"),
        ("business_density", "required",
         "Businesses per hectare. The AI saturation model reads this figure for every "
         "forecast and ranking in the barangay.",
         "1.4"),
        ("permit_count", "record",
         "Business permits in force in the barangay, all trades together.",
         "240"),
        ("closure_records", "record",
         "Businesses that closed or did not renew in the period.",
         "12"),
        ("effective_date", "record",
         "The date these figures describe (YYYY-MM-DD). Blank means the upload date.",
         "2026-09-30"),
    ],
    "MARKET_DATA": [
        ("barangay", "required",
         "Official Tarlac City barangay name where the business operates, matched as above.",
         "San Roque"),
        ("industry_type", "required",
         "The business's industry: a PSIC section name (e.g. Food and Beverage), section "
         "letter (I) or PSIC code (56101). Permits are counted per industry per barangay -- "
         "the competitor counts every recommendation is scored from.",
         "Food and Beverage"),
        ("status", "important",
         "Permit status. active, renewed, approved, released, issued or valid permits are "
         "counted as competitors; expired, cancelled or closed are not. Blank counts as active.",
         "active"),
        ("line_of_business", "important",
         "What the business sells or does, in a few words. Used to count DIRECT competitors "
         "(bakeries, not all food businesses).",
         "Bakery"),
        ("permit_no", "important",
         "Business permit number. Used only while the file is processed, so a permit listed "
         "twice is counted once; it is not stored.",
         "TC-2026-00001"),
    ],
}

DATASET_GUIDES = {
    "LGU_DATA": {
        "title": "Gov't / Zoning Data template",
        "rows": "One row per barangay.",
        "note": "",
    },
    "MARKET_DATA": {
        "title": "Market Data template",
        "rows": "One row per business (the business permit register).",
        "note": ("Only these counts are saved: in-force permits per industry, per kind of business "
                 "and per barangay. No business name, permit number or row of the file is stored. "
                 "Already have counts instead of a permit list? A file with industry_type, "
                 "barangay and competitor_count (one row per industry per barangay, plus "
                 "population_density, foot_traffic_index, average_rent and "
                 "historical_success_rate if you have them) is accepted too."),
    },
}


@lgu_bp.route("/lgu/dataset-template/<dataset_type>")
@role_required("LGU")
def dataset_template(dataset_type):
    """A ready-made CSV with the right headers and one example row.

    Generated from UPLOAD_COLUMNS, the same list the guidance table on
    the page renders from, so the documentation and the file cannot
    drift apart -- which is the usual way a "download the template"
    link starts handing out a header row the importer no longer
    accepts.
    """
    import csv
    import io

    from flask import Response

    columns = UPLOAD_COLUMNS.get(dataset_type)
    if columns is None:
        flash("Unknown dataset type.", "danger")
        return redirect(url_for("lgu.government_upload"))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([name for name, _level, _meaning, _example in columns])
    writer.writerow([example for _name, _level, _meaning, example in columns])

    filename = {"LGU_DATA": "marketlens_zoning_data_template.csv",
                "MARKET_DATA": "marketlens_market_data_template.csv"}[dataset_type]
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@lgu_bp.route("/lgu/government-data-upload", methods=["GET", "POST"])
@role_required("LGU")
def government_upload():
    """Upload page. The POST saves the file and hands it to a background
    job (app/services/upload_jobs.py), then waits up to
    UPLOAD_WAIT_SECONDS for the result: a normal file is reported on the
    next page exactly as before, a long one gets a progress card instead
    of a page that hangs until the server gives up on it."""
    from app.services import upload_jobs

    if request.method == "POST":
        if not current_user.is_lgu_encoder():
            flash("Only an LGU Encoder uploads files. As a Data Steward you review them in Data Review.", "warning")
            return redirect(url_for("lgu.government_upload"))
        dataset_type = request.form.get("dataset_type", "LGU_DATA")
        source = request.form.get("source", "Other")
        uploaded_file = request.files.get("dataset_file")

        if dataset_type not in ("LGU_DATA", "MARKET_DATA"):
            flash("Please choose a target table (Gov't / Zoning Data or Market Data).", "danger")
            return redirect(url_for("lgu.government_upload"))

        allowed_sources = LGU_SOURCES if dataset_type == "LGU_DATA" else MARKET_SOURCES
        if source not in allowed_sources:
            source = allowed_sources[-1]

        if uploaded_file is None or uploaded_file.filename == "":
            flash("Please choose a file to upload.", "danger")
            return redirect(url_for("lgu.government_upload"))

        allowed_ext = current_app.config["ALLOWED_UPLOAD_EXTENSIONS"]
        if not allowed_file(uploaded_file.filename, allowed_ext):
            flash("Only Excel (.xlsx, .xls) or CSV (.csv) files are accepted.", "danger")
            return redirect(url_for("lgu.government_upload"))

        if upload_jobs.running_job_for(current_user.user_id) is not None:
            flash("Your previous upload is still being processed. Wait for it to finish, then "
                  "upload the next file.", "warning")
            return redirect(url_for("lgu.government_upload"))

        upload_folder = current_app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_folder, exist_ok=True)
        _safe_name, absolute_path = unique_upload_path(upload_folder, uploaded_file.filename)
        uploaded_file.save(absolute_path)

        job = upload_jobs.start_upload(
            current_app._get_current_object(), current_user.user_id, absolute_path,
            uploaded_file.filename, dataset_type, source,
        )
        job.done.wait(timeout=float(current_app.config.get("UPLOAD_WAIT_SECONDS", 15)))

        if upload_jobs.claim_result_logging(job):
            staged = f", staged as batch #{job.batch_id}" if job.batch_id else ""
            log_action("dataset_upload",
                       details=f"{dataset_type}/{source}: {uploaded_file.filename} ({job.status}{staged})")
            job.shown = True
            _flash_job_result(job)
            return redirect(url_for("lgu.government_upload"))

        return redirect(url_for("lgu.government_upload", job=job.id))

    for finished in upload_jobs.unshown_results_for(current_user.user_id):
        _flash_job_result(finished)
    running = upload_jobs.get_job(request.args.get("job"), current_user.user_id)
    if running is None or running.done.is_set():
        running = upload_jobs.running_job_for(current_user.user_id)

    recent_lgu_data = LguData.query.order_by(LguData.upload_date.desc(), LguData.lgu_id.desc()).limit(20).all()
    recent_market_data = (
        MarketData.query.order_by(MarketData.date_recorded.desc(), MarketData.market_id.desc()).limit(20).all()
    )
    return render_template(
        "lgu/government_upload.html",
        recent_lgu_data=recent_lgu_data,
        recent_market_data=recent_market_data,
        lgu_sources=LGU_SOURCES,
        market_sources=MARKET_SOURCES,
        upload_columns=UPLOAD_COLUMNS,
        dataset_guides=DATASET_GUIDES,
        importance=IMPORTANCE,
        upload_job=running.as_dict() if running is not None else None,
        my_batches=staging_service.batches(uploaded_by=current_user.user_id, limit=15),
        pending_review=staging_service.count_waiting(STATUS_PENDING) if current_user.is_data_steward() else 0,
    )


def _flash_job_result(job):
    label = "Gov't / Zoning Data" if job.dataset_type == "LGU_DATA" else "Market Data"
    if job.status == "success":
        flash(f"'{job.filename}' ({label}): {job.message}", "success")
    else:
        flash(f"'{job.filename}' could not be processed: {job.message}", "danger")


@lgu_bp.route("/lgu/data-review")
@lgu_role_required("steward")
def data_review():
    """The LGU Data Steward's queue: batches waiting for review, and the
    latest decisions."""
    return render_template(
        "lgu/data_review.html",
        pending=staging_service.batches([STATUS_PENDING], limit=50),
        decided=staging_service.batches([STATUS_APPROVED, STATUS_REJECTED, STATUS_PUBLISHED], limit=20),
    )


@lgu_bp.route("/lgu/data-review/<int:batch_id>", methods=["GET", "POST"])
@lgu_role_required("steward")
def review_batch(batch_id):
    """One batch: what it would change, and the approve / reject decision."""
    from app.models import UploadBatch

    batch = UploadBatch.query.get_or_404(batch_id)
    if request.method == "POST":
        decision = request.form.get("decision")
        note = request.form.get("note", "")
        ok, message = staging_service.review(batch_id, current_user, decision == "approve", note)
        if ok:
            log_action("staging_review_approve" if decision == "approve" else "staging_review_reject",
                       details=f"batch #{batch_id}: {batch.filename}", target_type="UploadBatch",
                       target_id=batch_id, target_label=batch.filename, reason=note or None)
            flash(message, "success")
            return redirect(url_for("lgu.data_review"))
        flash(message, "danger")
        return redirect(url_for("lgu.review_batch", batch_id=batch_id))
    return render_template("lgu/review_batch.html", batch=batch, preview=staging_service.preview(batch))


@lgu_bp.route("/lgu/upload-status/<job_id>")
@role_required("LGU")
def upload_status(job_id):
    """Where a background upload has got to, for the progress card."""
    from flask import jsonify

    from app.services import upload_jobs

    job = upload_jobs.get_job(job_id, current_user.user_id)
    if job is None:
        return jsonify({"state": "unknown"}), 404
    return jsonify(job.as_dict())
