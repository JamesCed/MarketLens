"""
app/controllers/sme_controller.py
------------------------------------
Everything an SME (entrepreneur / business owner) account sees: Home,
Saturation Map, Trend Reports, Recommendations. Profile Settings is
shared across roles and lives in profile_controller.py.

Every route here is protected with @role_required("SME") EXCEPT the
saturation map and trend reports, which LGU/Admin accounts may also
view (read-only, aggregate) per the paper's RBAC table -- so those two
use @role_required("SME", "LGU", "Admin") instead.

industry_type and location are both free text in the real schema (no
Barangay table, no business_type dropdown backed by a DB enum) -- the
BUSINESS_TYPES list and BARANGAY_NAMES list below are just the UI's
SUGGESTED options (a <select> and a <datalist>), not a hard constraint;
the AI engine (app/ml/constants.py) has an "other" bucket for anything
typed outside them.

HOME SUMMARISES, PLANNING IS WHERE THE WORK HAPPENS
Home (/home) is an overview of every section -- your plans' scores, the
market around your chosen plan, alerts, and a way into each page. The
planning itself -- adding, editing, comparing and forecasting plans --
lives on the Planning page (/planning).

PLANS ARE MANAGED ON PLANNING, AND TRASH KEEPS THEM FOR 30 DAYS
"Remove" moves a plan to Trash -- an archive, see
app/models/sme_profile.py -- and the Trash dialog restores it with all
its forecasts. A plan left in Trash for TRASH_RETENTION_DAYS (30) is then
deleted for good by purge_expired_trash(), and that purge is audit-
logged. No button deletes a plan outright; the old /delete URL survives
only as an alias that trashes.

Every plan route answers two kinds of caller: a fetch() from the
Planning page's script (asks for JSON with `Accept: application/json`)
and a plain form POST with scripting off (gets a flash and a redirect
back to Planning). See _wants_json().
"""

from datetime import date, datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify, session, make_response,
)
from flask_login import current_user

from app.extensions import db
from app.models import SmeProfile, ForecastResult, PlanSave, MarketData
from app.models.archive import get_including_archived
from app.ml.constants import (
    BUSINESS_TYPES,
    FEATURED_BUSINESS_TYPES,
    INDUSTRY_DISPLAY,
    DEFAULT_INDUSTRY_DISPLAY,
    short_industry_label,
)
from app.ml.seed_data import BARANGAY_NAMES, get_real_population
from app.utils.decorators import role_required
from app.utils.audit import diff_fields, log_action
from app.services.forecasting_service import compute_scores_batch, generate_forecast_for_profile
from app.services.trend_analytics_service import (
    project_quarterly_outlook,
    resolve_as_of_month,
    EARLIEST_TREND_MONTH,
)
from app.services.recommendation_service import (
    parse_recommendation,
    competition_level_label,
)
from app.services.socio_demographic_service import get_demand_summary
from app.services.location_opportunity_service import (
    rank_location_opportunities,
    estimate_roi_timeframe,
    DEFAULT_LIMIT,
)


def _market_meta_for(industry_type):
    """{location: {"is_live": bool, "date_recorded": date}} for one
    industry -- lets the opportunity cards state whether each barangay's
    competitor count is a live Google Places figure or a simulated
    estimate, without re-querying once per card.

    SCOPED TO THE ONE INDUSTRY ASKED FOR. This used to call
    latest_market_data_by_key(), which resolves the freshest row for
    EVERY combo -- 20 industries x 76 barangays -- and then threw
    nineteen twentieths of it away in the comprehension below. Loading
    1,520 ORM objects to read 76 of them is a real cost on a 512 MB
    instance, because ORM instances are the expensive kind of row.
    _latest_market_rows() is the same greatest-n-per-group query with
    the industry list narrowed to one.
    """
    from app.ml.seed_data import BARANGAY_NAMES
    from app.services.forecasting_service import _latest_market_rows

    return {
        loc: {
            "is_live": row.source == "Google Places API",
            "date_recorded": row.date_recorded,
        }
        for (_industry, loc), row in _latest_market_rows([industry_type], BARANGAY_NAMES).items()
    }

sme_bp = Blueprint("sme", __name__)

BUSINESS_STAGES = ["startup", "existing"]


def _forecast_predates_lgu_data(forecast):
    """True when a real LGU upload has landed since this forecast was
    written, so the forecast is quoting figures the city's own records
    have since revised.

    Deliberately compares against REAL uploads only -- the placeholder
    rows the scoring engine creates for itself carry today's date, so
    counting them would mark every forecast stale the moment a new
    barangay was scored, and regenerate forever.
    """
    from app.services.data_import_service import active_lgu_dataset_summary

    dataset = active_lgu_dataset_summary()
    if dataset is None or forecast is None or forecast.forecast_date is None:
        return False
    return forecast.forecast_date < dataset["upload_date"]


HOME_PLAN_SESSION_KEY = "home_plan_id"


def _selected_home_plan(profiles):
    """The plan the Home page is showing -- the "choice bar" selection.

    ?plan=<id> picks one and remembers it in the session, so coming back
    to Home (from the map, from Settings) keeps showing the plan the
    owner chose instead of snapping back to the newest one. An id that
    is not one of THIS user's plans is ignored, never trusted. With no
    choice made, the newest plan is shown, as before.
    """
    if not profiles:
        return None
    by_id = {p.sme_id: p for p in profiles}
    remembered = session.get(HOME_PLAN_SESSION_KEY)

    requested = request.args.get("plan", type=int)
    if requested in by_id:
        if requested != remembered:
            session[HOME_PLAN_SESSION_KEY] = requested
            chosen = by_id[requested]
            log_action("select_plan", details=f"{chosen.industry_type} @ {chosen.location}", target=chosen)
        return by_id[requested]
    if remembered in by_id:
        return by_id[remembered]
    return profiles[0]


def _latest_forecasts_by_plan(profiles):
    """{sme_id: newest ForecastResult} for every plan, in ONE query --
    the choice bar shows each plan's market score, and a latest_forecast()
    call per plan would be one round trip each."""
    ids = [p.sme_id for p in profiles]
    if not ids:
        return {}
    latest = {}
    rows = (
        ForecastResult.query.filter(ForecastResult.sme_id.in_(ids))
        .order_by(ForecastResult.forecast_date.desc(), ForecastResult.forecast_id.desc())
        .all()
    )
    for row in rows:
        latest.setdefault(row.sme_id, row)
    return latest


def _forecast_predates_plan_model(forecast):
    """True when this forecast was written before the plan viability
    model existed, so its stored recommendation has no "forecast"
    payload -- no capital runway, no break-even for the plan, no
    explanation of the model's output -- and the Home panel would have
    nothing to show for them. Treated like _forecast_predates_lgu_data:
    regenerated once, after which the new row carries the payload."""
    if forecast is None:
        return False
    return parse_recommendation(forecast.recommendation).get("forecast") is None


def _score_summary(forecast):
    """What a plan's stored score IS, for a chip or a Trash row.

    A row written by the plan viability model stores the PLAN's viability
    (capital, staff, prices and offering weighed in). A row written
    before that model stores the old market-only figure, (100 -
    saturation) / 10. Both sit in the same viability_score column, so
    the column alone cannot say which one it holds; the payload can.
    Only the plan on screen is regenerated (see home()) -- re-running
    every plan's forecast, AI narration included, on every Home visit
    would be the slow page this app has worked to avoid -- so the others
    are LABELLED for what they are rather than shown side by side under
    one name. Opening a plan re-runs it, and its chip then reads
    "Viability".

    `band` colours the score pill by the number on it -- the same cuts
    as the forecast panel's HIGH / MODERATE / LOW viability word -- not
    by the market's saturation tier, which can disagree with a plan
    score: a plan with almost no capital in a quiet market is a low
    plan viability in a green "Low saturation" market."""
    if forecast is None:
        return {"viability_score": None, "cluster_label": None, "is_plan_score": False, "band": None}
    score = forecast.viability_score
    band = None
    if score is not None:
        value = float(score)
        band = "good" if value >= 6.5 else ("moderate" if value >= 4 else "low")
    return {
        "viability_score": score,
        "cluster_label": forecast.cluster_label,
        "is_plan_score": not _forecast_predates_plan_model(forecast),
        "band": band,
    }


# The Trash dialog's "Moved to Trash on <date>" is shown in Philippine
# time, like the audit trail and the forum (admin_controller /
# forum_controller define the same offset): archived_at is stored in UTC
# (HiddenWhenArchived.archive uses datetime.utcnow), so a plan trashed
# at 07:00 in Tarlac would otherwise read as trashed the day before. A
# fixed +8 is exact -- the Philippines has no daylight saving time.
DISPLAY_UTC_OFFSET = timedelta(hours=8)


def _trashed_plans_for(user_id):
    """This user's plans in Trash, newest-trashed first, each with its
    last viability score -- for the Trash dialog on Home.

    include_archived is the explicit escape hatch from the global
    archive filter (app/models/archive.py); without it these rows are
    invisible to every query, which is exactly what keeps a trashed plan
    off every other page."""
    profiles = (
        SmeProfile.query.execution_options(include_archived=True)
        .filter(SmeProfile.user_id == user_id, SmeProfile.archived_at.isnot(None))
        .order_by(SmeProfile.archived_at.desc(), SmeProfile.sme_id.desc())
        .all()
    )
    latest = _latest_forecasts_by_plan(profiles)
    return [
        {
            "profile": profile,
            # Labelled "last viability" or "last market score" by what
            # the stored row actually holds -- see _score_summary().
            **_score_summary(latest.get(profile.sme_id)),
            "archived_local": profile.archived_at + DISPLAY_UTC_OFFSET if profile.archived_at else None,
            # Whole days before purge_expired_trash() deletes it for good
            # (at least 0; a plan due today still shows as "today").
            "days_left": (
                max(0, TRASH_RETENTION_DAYS - (datetime.utcnow() - profile.archived_at).days)
                if profile.archived_at else TRASH_RETENTION_DAYS
            ),
        }
        for profile in profiles
    ]


def _plan_snapshot(profile):
    """Every business parameter of a plan as plain values -- what the
    audit trail's Details shows for a created plan, and what an edit is
    diffed against."""
    return {
        "business_name": profile.business_name,
        "industry_type": profile.industry_type,
        "subcategory": profile.subcategory_label or profile.subcategory,
        "location": profile.location,
        "business_stage": profile.business_stage,
        "registration_date": profile.registration_date.isoformat() if profile.registration_date else None,
        "capital": float(profile.startup_capital) if profile.startup_capital is not None else None,
        "employee_count": profile.employee_count,
        "product_offering": profile.product_offering,
        "innovation_idea": profile.innovation_idea,
        "price_list": [f"{i.get('item')}: {i.get('price')}" for i in profile.offering_items] or None,
    }


def _wants_json():
    """True when the caller is the Home page's script rather than a plain
    form post.

    fetch() callers say so with `Accept: application/json` (or the
    conventional X-Requested-With header). A browser submitting the form
    with scripting off sends an Accept that prefers text/html, and gets
    a flash and a redirect back to Home instead of a page of raw JSON --
    which is what the old Settings form showed anyone without JS.
    """
    if (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest":
        return True
    accept = request.accept_mimetypes
    if not accept.provided:
        return False
    return accept.best_match(("text/html", "application/json")) == "application/json"


def _plan_refusal(status, message):
    """abort() with the right body for the caller: JSON for the page's
    script (so it can show the reason inside the dialog), the ordinary
    error page otherwise."""
    if _wants_json():
        abort(make_response(jsonify({"success": False, "error": message}), status))
    abort(status)


def _owned_plan(sme_id, *, allow_trashed=False):
    """The plan `sme_id` IF it belongs to the signed-in user, or a
    refusal.

    Looked up with get_including_archived() so a plan in Trash is FOUND
    -- and therefore ownership-checked -- rather than reported missing by
    the archive filter. That ordering is the IDOR guard: someone else's
    plan is refused (403) whether it is live or in their Trash, and the
    existence of a trashed plan is never a way around the check.

    A plan in Trash can be restored (allow_trashed=True) but not edited:
    editing it would re-forecast a plan the owner removed. Restore first.
    """
    profile = get_including_archived(SmeProfile, sme_id)
    if profile is None:
        _plan_refusal(404, "That plan does not exist.")
    if profile.user_id != current_user.user_id:
        _plan_refusal(403, "That plan is not yours.")
    if profile.is_archived and not allow_trashed:
        _plan_refusal(404, "That plan is in Trash. Restore it first.")
    return profile


def _industry_card_order(current_industry=None):
    """The order of Home's industry slider: the chosen plan's industry
    first, then FEATURED_BUSINESS_TYPES (the sections Tarlac City SMEs
    register under most), then every other section in BUSINESS_TYPES
    order. Each section appears exactly once.

    The plan's own industry leads because it is the card the owner came
    to compare against; the featured ones follow so the first screenful
    still looks like the Home page they know.

    An industry outside BUSINESS_TYPES (a legacy name a plan kept, see
    plan_params) gets no card: the slider is the twenty PSIC sections,
    and a twenty-first card scored in the engine's generic "other"
    bucket would be a number about no section in particular.
    """
    order = []
    lead = [current_industry] if current_industry in BUSINESS_TYPES else []
    for industry_type in lead + list(FEATURED_BUSINESS_TYPES) + list(BUSINESS_TYPES):
        if industry_type not in order:
            order.append(industry_type)
    return order


def _industry_card_band(score):
    """good / fair / low for an industry card's Market Score -- the same
    cuts as the plan chips and the forecast panel's viability word
    (>= 6.5, >= 4), so one number never wears two different colours on
    the same page."""
    value = float(score or 0)
    return "good" if value >= 6.5 else ("fair" if value >= 4 else "low")


# ---------------------------------------------------------------------
# TRASH RETENTION
# ---------------------------------------------------------------------
# A plan moved to Trash is archived, not deleted -- and it stays
# restorable for TRASH_RETENTION_DAYS. After that it is deleted for good,
# with its forecasts and bookmarks (ORM cascade on SmeProfile and
# ForecastResult; a notification that pointed at one of those forecasts
# keeps its text and loses only the link, ondelete=SET NULL).
#
# Purged LAZILY, when the owner opens Planning or Home, rather than by a
# scheduler: this deployment has no cron, and the only person who could
# notice a plan lingering a day past its 30 is the owner, who triggers
# the purge simply by looking. The purge itself is audit-logged, so the
# trail records what was removed and why even though nobody clicked.
TRASH_RETENTION_DAYS = 30


def purge_expired_trash(user_id):
    """Delete this user's plans that have sat in Trash longer than
    TRASH_RETENTION_DAYS. Returns how many were removed."""
    cutoff = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
    expired = (
        SmeProfile.query.execution_options(include_archived=True)
        .filter(
            SmeProfile.user_id == user_id,
            SmeProfile.archived_at.isnot(None),
            SmeProfile.archived_at < cutoff,
        )
        .all()
    )
    for profile in expired:
        details = (f"{profile.business_name}: {profile.industry_type} @ {profile.location} "
                   f"(in Trash since {profile.archived_at:%Y-%m-%d}, over {TRASH_RETENTION_DAYS} days)")
        log_action("purge_plan", details=details, target=profile)
        db.session.delete(profile)
    if expired:
        db.session.commit()
    return len(expired)


@sme_bp.route("/home")
@role_required("SME")
def home():
    """THE OVERVIEW. Home is a summary of every section, not a workspace:
    each card states the one thing worth knowing about its section and
    links to it, and the actual planning -- adding, editing, comparing
    and forecasting plans -- happens on the Planning page.

    Built from what is already stored (the newest forecast per plan, the
    notifications) plus ONE batched market sweep for the chosen plan's
    barangay, so it stays the fast landing page it has to be. It never
    generates a forecast; a plan that has none yet says so and points to
    Planning, where opening it runs one."""
    from app.models import Notification
    from app.services.data_import_service import active_lgu_dataset_summary, has_active_lgu_data

    purge_expired_trash(current_user.user_id)

    profiles = current_user.sme_profiles.order_by(SmeProfile.sme_id.desc()).all()
    selected_plan = _selected_home_plan(profiles)
    latest_by_plan = _latest_forecasts_by_plan(profiles)

    plan_rows = []
    for profile in profiles:
        latest = latest_by_plan.get(profile.sme_id)
        plan_rows.append({
            "profile": profile,
            "forecast": latest,
            **_score_summary(latest),
            "selected": selected_plan is not None and profile.sme_id == selected_plan.sme_id,
        })
    scored = [row for row in plan_rows if row["viability_score"] is not None]
    best_plan = max(scored, key=lambda row: float(row["viability_score"])) if scored else None

    featured = latest_by_plan.get(selected_plan.sme_id) if selected_plan else None
    featured_rec = parse_recommendation(featured.recommendation) if featured else None

    # The market around the chosen plan: every industry scored in its
    # barangay in ONE batch (the same sweep Planning's slider shows), so
    # Home can name the strongest and the most crowded sections there.
    location = selected_plan.location if selected_plan else None
    top_industries, crowded_industries = [], []
    if location:
        scores = compute_scores_batch([(industry, location) for industry in BUSINESS_TYPES])
        ranked = sorted(
            ({"name": industry, "short": short_industry_label(industry), "score": s.get("viability_score"),
              "saturation": s.get("saturation_index") or 0, "competitors": s.get("competitor_count")}
             for industry, s in zip(BUSINESS_TYPES, scores)),
            key=lambda row: -float(row["score"] or 0),
        )
        top_industries, crowded_industries = ranked[:3], ranked[-3:][::-1]

    unread = (
        Notification.query.filter_by(user_id=current_user.user_id, is_read=False)
        .order_by(Notification.created_at.desc()).limit(3).all()
    )
    unread_count = Notification.query.filter_by(user_id=current_user.user_id, is_read=False).count()
    has_lgu_data = has_active_lgu_data()

    return render_template(
        "sme/home.html",
        profiles=profiles,
        plan_rows=plan_rows,
        best_plan=best_plan,
        selected_plan=selected_plan,
        featured=featured,
        featured_rec=featured_rec,
        top_industries=top_industries,
        crowded_industries=crowded_industries,
        trash_count=len(_trashed_plans_for(current_user.user_id)),
        unread=unread,
        unread_count=unread_count,
        has_lgu_data=has_lgu_data,
        lgu_dataset=active_lgu_dataset_summary() if has_lgu_data else None,
        retention_days=TRASH_RETENTION_DAYS,
    )


@sme_bp.route("/planning")
@role_required("SME")
def planning():
    """THE PLANNING PAGE -- the main workspace: every plan with its score,
    add / edit / move to Trash / restore, the industry slider for the
    chosen plan's barangay, the map focused on it, the full forecast with
    its transcript, the direct-competition insight and the quarterly
    outlook. Home only summarises this page and links here."""
    purge_expired_trash(current_user.user_id)
    profiles = current_user.sme_profiles.order_by(SmeProfile.sme_id.desc()).all()
    locations = BARANGAY_NAMES
    selected_plan = _selected_home_plan(profiles)
    default_location = selected_plan.location if selected_plan else (locations[0] if locations else "Poblacion")

    # One quick, EPHEMERAL score per industry type (no forecast_result
    # write -- see compute_scores docstring) for the SME's own/default
    # location, so the industry slider on the Home page has live
    # numbers.
    #
    # ALL TWENTY sections, not the eight featured ones the old card grid
    # showed. The slider shows four at a time and scrolls to the rest, so
    # an owner can see how every section fares in their barangay without
    # retyping it in the search box -- which used to be the only way to
    # see the other twelve.
    #
    # Still ONE batch, not one call per card, which is what makes twenty
    # affordable: a loop of compute_scores() is two SELECTs and a separate
    # forest dispatch per industry, while compute_scores_batch resolves
    # every pair's rows in two queries and predicts them in one pass --
    # more rows in the same matrix, not more round trips. This is the
    # first page an SME lands on after signing in.
    current_industry = selected_plan.industry_type if selected_plan else None
    card_order = _industry_card_order(current_industry)
    card_scores = compute_scores_batch(
        [(industry_type, default_location) for industry_type in card_order]
    )
    industry_cards = []
    for industry_type, scores in zip(card_order, card_scores):
        score = scores["viability_score"]
        band = _industry_card_band(score)
        # The arrow is the score's LEVEL drawn as a direction (green up =
        # a good chance, red down = hard to compete) -- not a change over
        # time, so the page's screen-reader text says what it means; see
        # sme/home.html. It is read off the SAME band as the pill beside
        # it: the old cut (down below 5) put a red down arrow next to a
        # yellow "fair" pill for any score from 4 to 4.9, a score the
        # forecast panel further down calls MODERATE.
        trend = {"good": "up", "fair": "neutral", "low": "down"}[band]
        display = INDUSTRY_DISPLAY.get(industry_type, DEFAULT_INDUSTRY_DISPLAY)
        industry_cards.append({
            "name": industry_type,
            "score": score,
            "trend": trend,
            "band": band,
            "icon": display["icon"],
            "bi": display["bi"],
            "hue": display["hue"],
            "short": display.get("short") or industry_type,
            "subtitle": display["subtitle"],
            "is_current": industry_type == current_industry,
        })

    # Featured forecast (the SELECTED plan) for the "Forecast &
    # Recommendations" panel -- generated on first visit if that plan
    # has never actually been forecast yet.
    latest_by_plan = _latest_forecasts_by_plan(profiles)
    featured_forecast = None
    if selected_plan is not None:
        featured_forecast = latest_by_plan.get(selected_plan.sme_id)
        if featured_forecast is None:
            featured_forecast = generate_forecast_for_profile(selected_plan)
            latest_by_plan[selected_plan.sme_id] = featured_forecast
        elif _forecast_predates_lgu_data(featured_forecast) or _forecast_predates_plan_model(featured_forecast):
            # An LGU upload has landed since this forecast was written,
            # so its numbers -- and the recommendation text quoting
            # them -- describe a city that no longer matches the
            # records. Re-running it here is what makes "upload the
            # permits and the output changes" true on the page the SME
            # actually lands on, rather than only after they happen to
            # edit their plan.
            #
            # Same for a forecast written before the plan viability
            # model: it has no capital runway, break-even or explanation
            # to show, so it is re-run once and the new row carries them.
            featured_forecast = generate_forecast_for_profile(selected_plan)
            latest_by_plan[selected_plan.sme_id] = featured_forecast

    featured_industry_type = (
        featured_forecast.input_industry_type if featured_forecast else FEATURED_BUSINESS_TYPES[0]
    )

    quarterly_outlook = None
    featured_recommendation = None
    if featured_forecast:
        # sme_profile: each quarter's viability is the PLAN model re-run
        # with that quarter's projected saturation and competitor count,
        # every plan input (capital, staff, prices...) held -- so Q1
        # agrees with the plan viability on the gauge above it.
        quarterly_outlook = project_quarterly_outlook(
            featured_forecast.saturation_index,
            featured_forecast.viability_score,
            featured_forecast.input_location,
            industry_type=featured_forecast.input_industry_type,
            sme_profile=selected_plan,
        )
        # forecast_result.recommendation is JSON-serialized structured
        # data (see recommendation_service.py) -- parse it back into
        # {headline, opportunity_type, summary, reasons, risks,
        # forecast, explanation} so this panel shows readable text
        # instead of a raw JSON string. "forecast" is the trained
        # model's payload (capital runway, break-even, drivers); it is
        # None on a row older than the plan model.
        featured_recommendation = parse_recommendation(featured_forecast.recommendation)

    # COLD START. The "LGU Recommendations" panel ranks barangays
    # city-wide -- which barangays the city should steer investment to.
    # That is a claim about the city's own records, so until an LGU
    # account has actually uploaded some, the panel says so instead of
    # ranking barangays off auto-generated placeholder rows. Everything
    # else on this page answers "what does the data we have say", which
    # is a fair question either way, so none of it is gated.
    from app.services.data_import_service import active_lgu_dataset_summary, has_active_lgu_data

    has_lgu_data = has_active_lgu_data()

    # The choice bar: every plan with its own score, so the owner can
    # compare plans at a glance and switch without re-entering any. A
    # plan whose newest forecast predates the plan viability model shows
    # it as a market score, honestly labelled, until it is opened (which
    # re-runs it) -- see _score_summary().
    plan_choices = []
    for profile in profiles:
        plan_choices.append({
            "profile": profile,
            **_score_summary(latest_by_plan.get(profile.sme_id)),
            "selected": selected_plan is not None and profile.sme_id == selected_plan.sme_id,
        })

    return render_template(
        "sme/planning.html",
        profiles=profiles,
        selected_plan=selected_plan,
        plan_choices=plan_choices,
        # The Trash dialog: plans moved to Trash, restorable. Read with
        # include_archived -- nothing else on this page can see them.
        trashed_plans=_trashed_plans_for(current_user.user_id),
        business_types=BUSINESS_TYPES,
        business_stages=BUSINESS_STAGES,
        locations=locations,
        default_location=default_location,
        industry_cards=industry_cards,
        featured_forecast=featured_forecast,
        featured_industry_type=featured_industry_type,
        featured_recommendation=featured_recommendation,
        quarterly_outlook=quarterly_outlook,
        demand_summary=get_demand_summary(),
        has_lgu_data=has_lgu_data,
        lgu_dataset=active_lgu_dataset_summary() if has_lgu_data else None,
        retention_days=TRASH_RETENTION_DAYS,
    )


@sme_bp.route("/home/analyze", methods=["POST"])
@role_required("SME")
def analyze():
    """The "Add New Plan" dialog: create a new SmeProfile and run the AI
    forecasting engine on it immediately (see
    generate_forecast_for_profile).

    Parsing is plan_params.parse_plan_form() -- the same parser the
    sign-up wizard and the Home page's Edit dialog use, so the three
    cannot disagree about what a plan is (capital required, monthly
    revenue not collected by any of them)."""
    from app.services.plan_params import apply_plan_data, parse_plan_form

    data, errors = parse_plan_form(request.form)
    if errors:
        for message in errors:
            flash(message, "danger")
        return redirect(url_for("sme.planning"))

    profile = apply_plan_data(SmeProfile(user_id=current_user.user_id), data)
    db.session.add(profile)
    db.session.commit()

    generate_forecast_for_profile(profile)
    # The trail keeps EVERY field the plan was created with (Details
    # button), not just the one-line summary.
    log_action("create_plan", details=f"{profile.business_name}: {profile.industry_type} @ {profile.location}",
               target=profile, changes=_plan_snapshot(profile))
    # Select it now, so landing on it is not also logged as a switch.
    session[HOME_PLAN_SESSION_KEY] = profile.sme_id
    flash(f"“{profile.business_name}” added to your plans — its forecast is ready below.", "success")
    # Land on the plan just made, so the choice bar, the map and the
    # forecast panel all show it rather than whichever plan was selected.
    return redirect(url_for("sme.planning", plan=profile.sme_id))


@sme_bp.route("/home/plans/<int:sme_id>/update", methods=["POST"])
@role_required("SME")
def update_plan(sme_id):
    """The Edit dialog on a Home plan chip (the pencil icon).

    With scripting on, the dialog posts here over fetch() asking for
    JSON: a validation error comes back as 400 {"success": false,
    "error": ...} and is shown INSIDE the dialog without losing what was
    typed; success returns {"success": true, "plan": ...} and the page
    reloads onto the edited plan, because every panel on Home (score,
    map, forecast, quarterly chart) depends on it.

    With scripting off it is an ordinary form post: errors are flashed
    and success is flashed, and either way the browser is sent back to
    Home on that plan -- never left looking at raw JSON, which is what
    the old Settings form showed.

    A plan in Trash cannot be edited (404 -- restore it first), and a
    plan that is not yours is refused -- see _owned_plan()."""
    from app.services.plan_params import apply_plan_data, parse_plan_form

    profile = _owned_plan(sme_id)
    wants_json = _wants_json()

    form = request.form.copy()  # a mutable MultiDict; getlist() still works
    # A missing stage picker means "unchanged", not "reset to startup".
    if not form.get("business_stage"):
        form["business_stage"] = profile.business_stage or "startup"

    data, errors = parse_plan_form(form, keep_industry=profile.industry_type)
    if errors:
        if wants_json:
            return jsonify({"success": False, "error": " ".join(errors), "errors": errors}), 400
        for message in errors:
            flash(message, "danger")
        return redirect(url_for("sme.planning", plan=profile.sme_id))

    # Keep a registration date the form did not send, rather than
    # re-dating an existing business to today on every edit.
    if not form.get("registration_date") and profile.registration_date and data["business_stage"] == "existing":
        data["registration_date"] = profile.registration_date.isoformat()

    before = _plan_snapshot(profile)
    apply_plan_data(profile, data)
    db.session.commit()
    # Re-run the AI engine so the Home page's featured forecast reflects
    # the edited plan -- every input feeds the plan viability model, so
    # changing the capital or the staff count changes the forecast too.
    generate_forecast_for_profile(profile)
    changed = diff_fields(before, _plan_snapshot(profile))
    log_action("update_plan",
               details=(f"{profile.business_name}: changed " + ", ".join(changed)) if changed
               else f"{profile.business_name}: saved with no changes",
               target=profile, changes=changed)
    # Land on the edited plan without that landing being logged as a
    # separate "switched plan" -- same as a newly added plan.
    session[HOME_PLAN_SESSION_KEY] = profile.sme_id

    # Flashed on BOTH paths: the page's script reloads Home onto the
    # plan after a JSON success, so the confirmation is shown there.
    flash(f"\u201c{profile.business_name}\u201d updated \u2014 its forecast has been re-run.", "success")
    if wants_json:
        return jsonify({
            "success": True,
            "plan": profile.to_dict(),
            "redirect": url_for("sme.planning", plan=profile.sme_id),
        })
    return redirect(url_for("sme.planning", plan=profile.sme_id))


def _move_plan_to_trash(sme_id):
    """Shared by trash_plan and the legacy delete_plan alias.

    ARCHIVES the plan (archived_at/by/reason -- see
    app/models/sme_profile.py); nothing is deleted. Its forecasts and
    bookmarks are not touched at all, so restore brings them back as
    they were. Note what that does and does not hide: every page that
    lists plans (SmeProfile queries) drops the plan, but forecast_result
    and plan_save rows carry no archive stamp of their own, so a count
    that reads those tables WITHOUT joining to the plan still includes
    them -- see the model's docstring. Trashing a plan that is already in
    Trash changes nothing and is not logged twice (a double-clicked
    button)."""
    profile = _owned_plan(sme_id, allow_trashed=True)
    name = profile.business_name

    if not profile.is_archived:
        profile.archive(current_user.user_id, "Moved to Trash from the Home page")
        db.session.commit()
        log_action("trash_plan", details=f"{profile.business_name}: {profile.industry_type} @ {profile.location}",
                   target=profile, changes={"status": ["active", "in Trash"],
                                            "deleted_permanently_after_days": TRASH_RETENTION_DAYS})

    # The Home page remembers the plan being viewed; if that is the one
    # just trashed, forget it, so Home falls back to the newest live
    # plan instead of a plan it can no longer show.
    if session.get(HOME_PLAN_SESSION_KEY) == profile.sme_id:
        session.pop(HOME_PLAN_SESSION_KEY, None)

    message = f"\u201c{name}\u201d moved to Trash."
    if _wants_json():
        return jsonify({"success": True, "message": message, "plan": profile.to_dict(),
                        "redirect": url_for("sme.planning")})
    flash(message, "success")
    return redirect(url_for("sme.planning"))


@sme_bp.route("/home/plans/<int:sme_id>/trash", methods=["POST"])
@role_required("SME")
def trash_plan(sme_id):
    """The bin icon on a Home plan chip (after its confirm dialog).
    Moves the plan to Trash -- see _move_plan_to_trash()."""
    return _move_plan_to_trash(sme_id)


@sme_bp.route("/home/plans/<int:sme_id>/delete", methods=["POST"])
@role_required("SME")
def delete_plan(sme_id):
    """LEGACY ALIAS. This URL used to hard-delete the plan, cascading
    through its forecasts and every bookmark on them. It is kept so an
    old cached page or script still works, but it now does exactly what
    the Trash button does: archive, never delete."""
    return _move_plan_to_trash(sme_id)


@sme_bp.route("/home/plans/<int:sme_id>/restore", methods=["POST"])
@role_required("SME")
def restore_plan(sme_id):
    """The restore icon in the Trash dialog. Clears the archive stamp,
    which brings the plan -- and, because they were never touched, all
    its forecasts and bookmarks -- back onto every page. Lands on the
    restored plan."""
    profile = _owned_plan(sme_id, allow_trashed=True)
    name = profile.business_name

    if profile.is_archived:
        profile.restore()
        db.session.commit()
        log_action("restore_plan", details=f"{profile.business_name}: {profile.industry_type} @ {profile.location}",
                   target=profile, changes={"status": ["in Trash", "active"]})

    session[HOME_PLAN_SESSION_KEY] = profile.sme_id
    message = f"\u201c{name}\u201d restored."
    if _wants_json():
        return jsonify({"success": True, "message": message, "plan": profile.to_dict(),
                        "redirect": url_for("sme.planning", plan=profile.sme_id)})
    flash(message, "success")
    return redirect(url_for("sme.planning", plan=profile.sme_id))


@sme_bp.route("/saturation-map")
@role_required("SME", "LGU", "Admin")
def saturation_map():
    from app.services.saturation_timeline_service import timeline_bounds

    bounds = timeline_bounds()
    return render_template(
        "sme/saturation_map.html",
        business_types=BUSINESS_TYPES,
        locations=BARANGAY_NAMES,
        # The month timeline under the map: history from January 2020,
        # this month, and up to a year of prediction.
        timeline={key: value.strftime("%Y-%m") for key, value in bounds.items()},
        # Real PSA FIES (2023) household spending-category figures --
        # the SAME for every barangay (no barangay-level breakdown
        # exists publicly), so this is computed once here and exposed
        # as a page-level constant rather than re-fetched per barangay.
        # See app/services/socio_demographic_service.py for sourcing.
        demand_summary=get_demand_summary(),
    )


@sme_bp.route("/trend-reports")
@role_required("SME", "LGU", "Admin")
def trend_reports():
    """ONE city-wide Trend Reports & Analytics dashboard, shared by SME,
    LGU and Admin accounts.

    An earlier version gave SME accounts a separate per-plan "My Trend
    Report" built from their own saved business plans. That is gone: a
    trend report is about how the MARKET is moving, and a chart drawn
    from a single user's one or two saved plans is not a market trend,
    it is a restatement of their own inputs. The paper's own Figure 4 /
    4.1 describes this page as market indicators, quarterly performance
    and industry growth distribution -- all city-wide -- so that is what
    every role now sees. An SME's own plan-specific numbers still live
    on Home and Recommendations, where they belong.
    """
    return render_template(
        "sme/trend_reports.html",
        business_types=BUSINESS_TYPES,
        # The line chart plots exactly these 8, so the template can give
        # each one its own fixed palette slot instead of cycling colours.
        featured_business_types=FEATURED_BUSINESS_TYPES,
        # The month picker is a real <input type="month"> -- month and
        # year only, no days, which is what "as of this month" means.
        # ?as_of= survives the reload it triggers, so the control still
        # shows the month the charts are actually drawing.
        selected_month=resolve_as_of_month(request.args.get("as_of")).strftime("%Y-%m"),
        earliest_month=EARLIEST_TREND_MONTH.strftime("%Y-%m"),
        latest_month=date.today().strftime("%Y-%m"),
    )


@sme_bp.route("/recommendations")
@role_required("SME")
def recommendations():
    """Matches the capstone paper's Figma storyboard: 3 summary stat
    cards, then one card per plan with real Key Metrics (your own
    capital, a computed break-even estimate, this barangay's real PSA
    population, and a competition level derived from the actual
    competitor count) and the structured "Why This Works" /
    "Considerations" text generated by recommendation_service.py. Every
    number here is scoped to THIS SME's own saved plans, matching the
    page subtitle -- not a city-wide figure (see the LGU Dashboard for
    that)."""
    profiles = current_user.sme_profiles.order_by(SmeProfile.sme_id.desc()).all()
    forecasts = []

    # One query for every plan's market row instead of one per plan.
    # This loop used to run MarketData.query.get() per profile, so an
    # SME with a dozen saved plans paid a dozen round trips to fetch a
    # dozen rows by primary key -- the textbook N+1, and each round trip
    # crosses a data centre in production.
    latest_by_profile = [(profile, profile.latest_forecast()) for profile in profiles]
    market_ids = {
        latest.market_id for _profile, latest in latest_by_profile
        if latest is not None and latest.market_id is not None
    }
    market_by_id = {}
    if market_ids:
        market_by_id = {
            row.market_id: row
            for row in MarketData.query.filter(MarketData.market_id.in_(market_ids)).all()
        }

    for profile, latest in latest_by_profile:
        if not latest:
            continue

        rec = parse_recommendation(latest.recommendation)
        market_row = market_by_id.get(latest.market_id)
        competitor_count = market_row.competitor_count if market_row else 0

        forecasts.append({
            "profile": profile,
            "forecast": latest,
            "rec": rec,
            "population": get_real_population(profile.location),
            "competition_level": competition_level_label(competitor_count),
            # Same AI-derived ROI window the location cards use -- built
            # from this forecast's own saturation/viability output. See
            # estimate_roi_timeframe().
            "roi": estimate_roi_timeframe(
                latest.viability_score, latest.saturation_index, None, None,
            ),
        })


    saved_ids = {ps.forecast_result_id for ps in PlanSave.query.filter_by(user_id=current_user.user_id).all()}

    # --- "Where else could I open this?" -----------------------------
    # The storyboard's page promises 15+ further opportunities. This is
    # that list, and it is real: for the industry on the SME's own plan,
    # every barangay in the city is scored and the best ones come back
    # as cards, each one's "Why This Works"/"Considerations" written
    # from that barangay's own Places API competitor count, real 2024
    # PSA population/density and the model's own saturation output --
    # compared against the city median for the same industry. See
    # app/services/location_opportunity_service.py.
    plan_for_opportunities = None
    if request.args.get("sme_id", type=int):
        plan_for_opportunities = next(
            (p for p in profiles if p.sme_id == request.args.get("sme_id", type=int)), None
        )
    if plan_for_opportunities is None:
        plan_for_opportunities = profiles[0] if profiles else None

    # "Explore more recommendations" drops the 5-card display cap and
    # shows everything the AI recommended (still only the enterable
    # tiers -- a saturated barangay is never recommended).
    show_all = request.args.get("all") == "1"

    opportunity_result = None
    if plan_for_opportunities is not None:
        opportunity_result = rank_location_opportunities(
            plan_for_opportunities.industry_type,
            BARANGAY_NAMES,
            sme_profile=plan_for_opportunities,
            market_meta_by_location=_market_meta_for(plan_for_opportunities.industry_type),
            limit=None if show_all else DEFAULT_LIMIT,
        )

    result = opportunity_result or {}
    return render_template(
        "sme/recommendations.html",
        forecasts=forecasts,
        saved_ids=saved_ids,
        has_profiles=bool(profiles),
        profiles=profiles,
        opportunity_plan=plan_for_opportunities,
        opportunities=result.get("opportunities", []),
        opportunity_city=result.get("city", {}),
        opportunity_industry_label=result.get("industry_label", ""),
        # The three summary cards count every barangay the AI scored
        # city-wide -- NOT the SME's own saved plans, which is what they
        # used to show (and why they read "1 / 1 / 2").
        high_count=result.get("high_opportunity_count", 0),
        moderate_count=result.get("moderate_opportunity_count", 0),
        # "AI-Recommended Locations" must mean locations the AI actually
        # recommends -- High + Moderate -- not every barangay it scored.
        locations_count=result.get("recommended_count", 0),
        not_recommended_count=result.get("not_recommended_count", 0),
        total_scored=result.get("total_scored", 0),
        showing_count=result.get("showing_count", 0),
        has_more=result.get("has_more", False),
        show_all=show_all,
        # Which (industry, location) pairs this user has already saved,
        # so a card can show "Saved" instead of offering to save twice.
        saved_locations={
            (p.industry_type, p.location) for p in profiles
        },
    )


@sme_bp.route("/recommendations/save-location", methods=["POST"])
@role_required("SME")
def save_recommended_location():
    """Turn an AI location recommendation into a real saved plan.

    A recommendation card is an ephemeral score for an (industry,
    barangay) pair -- there is no forecast_result row behind it, so it
    cannot be bookmarked the way an existing plan can. Saving one
    therefore CREATES the plan it describes: a new SmeProfile for that
    industry in that barangay, carrying over the capital / revenue /
    stage from the plan the recommendation was scored against, then runs
    the real forecasting engine on it and bookmarks the result. After
    this the recommendation is an ordinary plan -- it shows up in My
    Plans, on Home, and in its own per-plan card here.

    Nothing is invented: the only new information is the industry and
    location the user just chose off a card.
    """
    industry_type = (request.form.get("industry_type") or "").strip()
    location = (request.form.get("location") or "").strip()
    source_id = request.form.get("source_sme_id", type=int)
    if not industry_type or not location:
        flash("Could not save that recommendation -- it was missing an industry or location.", "danger")
        return redirect(url_for("sme.recommendations"))

    # This endpoint creates a real business plan, so it validates its
    # inputs rather than trusting whatever was posted: only an industry
    # the system actually scores, and only a barangay it actually knows.
    # Without this a malformed or hand-crafted POST would happily create
    # a plan for a location that does not exist, which then can never be
    # scored or mapped.
    if industry_type not in BUSINESS_TYPES:
        flash("That industry is not one this system scores.", "danger")
        return redirect(url_for("sme.recommendations"))
    if location not in BARANGAY_NAMES:
        flash(f"'{location}' is not a Tarlac City barangay this system knows.", "danger")
        return redirect(url_for("sme.recommendations"))

    source = None
    if source_id:
        source = SmeProfile.query.filter_by(sme_id=source_id, user_id=current_user.user_id).first()
    if source is None:
        source = current_user.sme_profiles.order_by(SmeProfile.sme_id.desc()).first()

    # Already saved this exact combination? Don't create a duplicate
    # plan -- just say so and go back.
    existing = SmeProfile.query.filter_by(
        user_id=current_user.user_id, industry_type=industry_type, location=location
    ).first()
    if existing is not None:
        flash(f"You already have a plan for {industry_type} in {location}.", "info")
        return redirect(url_for("sme.recommendations", sme_id=source.sme_id if source else None))

    profile = SmeProfile(
        user_id=current_user.user_id,
        business_name=f"{short_industry_label(industry_type)} in {location}",
        industry_type=industry_type,
        location=location,
        # Carried over from the plan this was scored against, so the new
        # plan is forecast on the same parameters the card showed.
        startup_capital=source.startup_capital if source else 0,
        employee_count=source.employee_count if source else None,
        business_stage="startup",
    )
    if source is not None and source.industry_type == industry_type:
        # Same business, different barangay: what they sell, how they
        # differ and their menu travel with it. (A different industry
        # would make the old sub-category meaningless, so it is not.)
        profile.subcategory = source.subcategory
        profile.product_offering = source.product_offering
        profile.innovation_idea = source.innovation_idea
        profile.offering_details = source.offering_details
    db.session.add(profile)
    db.session.commit()

    forecast = generate_forecast_for_profile(profile)
    if forecast is not None:
        db.session.add(PlanSave(user_id=current_user.user_id, forecast_result_id=forecast.forecast_id))
        db.session.commit()

    log_action("save_recommended_location", f"{industry_type} in {location}", target=profile)
    flash(f"Saved to My Plans: {profile.business_name}.", "success")
    return redirect(url_for("sme.recommendations", sme_id=source.sme_id if source else None))


@sme_bp.route("/recommendations/<int:forecast_id>/save", methods=["POST"])
@role_required("SME")
def save_recommendation(forecast_id):
    forecast = ForecastResult.query.get_or_404(forecast_id)
    if forecast.sme_profile.user_id != current_user.user_id:
        abort(403)

    existing = PlanSave.query.filter_by(user_id=current_user.user_id, forecast_result_id=forecast.forecast_id).first()
    if existing is None:
        db.session.add(PlanSave(user_id=current_user.user_id, forecast_result_id=forecast.forecast_id))
        db.session.commit()
        flash("Saved to My Plans.", "success")
    return redirect(url_for("sme.recommendations"))
