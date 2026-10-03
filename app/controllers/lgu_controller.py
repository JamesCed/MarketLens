"""
app/controllers/lgu_controller.py
------------------------------------
LGU-only pages: the LGU dashboard and, most importantly, the Gov't
Data Upload page.

*** This is the page that must be LGU-only per your instructions. ***
Enforced two ways:
  1. @role_required("LGU", "Admin") on every route below -- an SME
     account hitting these URLs directly gets HTTP 403, not just a
     hidden link.
  2. The sidebar nav (app/templates/shared/_sidebar.html) only renders
     the "Gov't Data Upload" link when current_user.is_lgu() or
     current_user.is_admin() is true.

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
from app.utils.decorators import role_required
from app.utils.helpers import allowed_file, unique_upload_path
from app.utils.audit import log_action
from app.services.data_import_service import (
    active_lgu_dataset_summary,
    has_active_lgu_data,
    process_upload,
)

lgu_bp = Blueprint("lgu", __name__)

LGU_SOURCES = ["DTI", "CLUP", "Other"]
MARKET_SOURCES = ["PSA", "DTI", "Manual"]


@lgu_bp.route("/lgu/dashboard")
@role_required("LGU", "Admin")
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
    from app.services.forecasting_service import compute_scores_batch
    from app.services.trend_analytics_service import (
        get_barangay_business_table,
        latest_market_data_by_key,
        live_plan_forecasts,
    )

    # Forecasts of LIVE plans only: a plan an SME moved to Trash keeps its
    # forecast rows (so it can be restored), and without the join to
    # sme_profile inside live_plan_forecasts() it went on counting here
    # -- an SME's "removed" plan still tallied as a saturated zone.
    total_forecasts = live_plan_forecasts().count()
    # No stored cluster_label column -- Low/Saturated cut points come
    # straight from app/ml/constants.py CLUSTER_THRESHOLDS ([25, 50, 75, 100]).
    saturated_count = live_plan_forecasts().filter(ForecastResult.saturation_index > CLUSTER_THRESHOLDS[2]).count()
    opportunity_count = live_plan_forecasts().filter(ForecastResult.saturation_index <= CLUSTER_THRESHOLDS[0]).count()

    barangays_on_file = [
        row[0] for row in db.session.query(LguData.barangay).distinct().order_by(LguData.barangay).all()
    ]

    latest_market = latest_market_data_by_key()
    barangay_rows = get_barangay_business_table(latest_market)

    # ---- City-wide industry cards -------------------------------------
    # Mirrors the SME Home page's card row, but scored city-wide. Running
    # the AI on all 8 featured industries x 76 barangays would be 608
    # model calls on a dashboard, so each industry is instead SAMPLED
    # across the city's most populous barangays -- a small, fixed,
    # deterministic sample that still reflects the built-up core where
    # competition actually concentrates. The full sweep still happens on
    # Trend Reports, which is the page built for it.
    sample_barangays = sorted(BARANGAY_NAMES, key=lambda b: -(get_real_population(b) or 0))[:6]

    businesses_by_industry = {}
    for (industry_type, _location), row in latest_market.items():
        businesses_by_industry[industry_type] = businesses_by_industry.get(industry_type, 0) + int(
            row.competitor_count or 0
        )

    # Every industry x sample-barangay combination in ONE batch. This
    # was a nested loop calling compute_scores() per cell: 8 featured
    # industries x 6 barangays is 48 calls, ~96 SELECTs and 48 separate
    # trips through the forest to fill eight cards on the page an LGU
    # officer lands on. Same fix as SME Home and Recommendations.
    pairs = [(industry_type, barangay)
             for industry_type in FEATURED_BUSINESS_TYPES
             for barangay in sample_barangays]
    scores_by_industry = {}
    for (industry_type, _barangay), score in zip(pairs, compute_scores_batch(pairs)):
        scores_by_industry.setdefault(industry_type, []).append(score)

    industry_cards = []
    for industry_type in FEATURED_BUSINESS_TYPES:
        scores = scores_by_industry.get(industry_type, [])
        avg_viability = round(sum(s["viability_score"] for s in scores) / len(scores), 1) if scores else 0.0
        avg_saturation = round(sum(s["saturation_index"] for s in scores) / len(scores), 1) if scores else 0.0
        display = INDUSTRY_DISPLAY.get(industry_type, DEFAULT_INDUSTRY_DISPLAY)
        industry_cards.append(
            {
                "name": industry_type,
                "score": avg_viability,
                "saturation": avg_saturation,
                "businesses": businesses_by_industry.get(industry_type, 0),
                "trend": "up" if avg_viability >= 6.5 else ("down" if avg_viability < 5 else "neutral"),
                "icon": display["icon"],
                "subtitle": display["subtitle"],
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
        lgu_data_count=LguData.query.count(),
        market_data_count=MarketData.query.count(),
        industry_cards=industry_cards,
        barangay_rows=barangay_rows,
        totals=totals,
        business_types=BUSINESS_TYPES,
        locations=BARANGAY_NAMES,
        sample_barangays=sample_barangays,
        saturated_threshold=CLUSTER_THRESHOLDS[2],
        # Same cold start as the SME Home panel: the city-wide
        # recommendation is a claim about the city's own records, so it
        # is not made until an LGU account has uploaded some. This is
        # the page that account actually lands on, so it is also where
        # the "Upload Dataset" way out belongs.
        has_lgu_data=has_active_lgu_data(),
        lgu_dataset=active_lgu_dataset_summary(),
    )


# The column reference shown on the upload page AND used to build the
# downloadable templates, so the guidance and the file can never
# disagree about what a column is called. Each entry is
# (column, required, what it means, example).
UPLOAD_COLUMNS = {
    "LGU_DATA": [
        ("barangay", True,
         "Official Tarlac City barangay name. Must match exactly (case is forgiven); "
         "an unrecognised name is rejected with a suggestion rather than imported.",
         "San Roque"),
        ("psic_code", False,
         "PSIC code, section letter, or the line of business. Include this and each row "
         "is treated as ONE business permit, so the system can count competitors per "
         "industry per barangay -- this is what makes an upload change the "
         "recommendations rather than only the totals.",
         "56101"),
        ("business_name", False, "The registered business name. Kept for your reference.",
         "Aling Nena's Carinderia"),
        ("status", False,
         "Permit status. Only active/approved/released/issued/valid/renewed are counted "
         "as competitors; anything else (expired, cancelled) is ignored.",
         "active"),
        ("permit_count", False,
         "Total permits in this barangay across all trades. Use this for a barangay "
         "SUMMARY row instead of one row per business.", "240"),
        ("business_density", False, "Businesses per hectare, if your office computes it.", "1.4"),
        ("closure_records", False, "Businesses that closed in the period.", "12"),
        ("zoning_info", False, "Zoning classification or notes.", "Commercial"),
        ("effective_date", False, "The date these figures describe. Defaults to today.",
         "2026-09-01"),
    ],
    "MARKET_DATA": [
        ("industry_type", True, "PSIC section name, code, or trade description.",
         "Food and Beverage"),
        ("location", True, "Official Tarlac City barangay name, matched as above.", "San Roque"),
        ("competitor_count", False, "Businesses of this industry operating in this barangay.",
         "14"),
        ("population_density", False, "People per square kilometre.", "2500"),
        ("historical_success_rate", False,
         "A FRACTION between 0 and 1, not a percentage. 0.85 means 85%; writing 85 is "
         "rejected, because it would move the figure by two orders of magnitude.",
         "0.85"),
        ("foot_traffic_index", False, "Relative foot traffic, 0-100.", "48"),
        ("average_rent", False, "Typical monthly commercial rent in pesos.", "19000"),
        ("date_recorded", False, "The date these figures describe. Defaults to today.",
         "2026-09-01"),
    ],
}


@lgu_bp.route("/lgu/dataset-template/<dataset_type>")
@role_required("LGU", "Admin")
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
    writer.writerow([name for name, _required, _meaning, _example in columns])
    writer.writerow([example for _name, _required, _meaning, example in columns])

    filename = f"marketlens_{dataset_type.lower()}_template.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@lgu_bp.route("/lgu/government-data-upload", methods=["GET", "POST"])
@role_required("LGU", "Admin")
def government_upload():
    if request.method == "POST":
        dataset_type = request.form.get("dataset_type", "LGU_DATA")
        source = request.form.get("source", "Other")
        uploaded_file = request.files.get("dataset_file")

        if dataset_type not in ("LGU_DATA", "MARKET_DATA"):
            flash("Please choose a target table (Gov't/Zoning Data or Market Data).", "danger")
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

        upload_folder = current_app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_folder, exist_ok=True)
        _safe_name, absolute_path = unique_upload_path(upload_folder, uploaded_file.filename)
        uploaded_file.save(absolute_path)

        try:
            records_count, status, error_message = process_upload(
                dataset_type, absolute_path, source, current_user.user_id
            )
        finally:
            # No file-metadata table to keep a reference to this path in
            # -- only the parsed rows survive, so the temp file goes now.
            if os.path.exists(absolute_path):
                os.remove(absolute_path)

        log_action("dataset_upload", details=f"{dataset_type}/{source}: {uploaded_file.filename} ({status})")

        if status == "success":
            flash(
                f"'{uploaded_file.filename}' processed successfully -- "
                f"{records_count} row(s) saved to {dataset_type.replace('_', ' ').title()}.",
                "success",
            )
        else:
            flash(f"'{uploaded_file.filename}' could not be processed: {error_message}", "danger")

        return redirect(url_for("lgu.government_upload"))

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
    )
