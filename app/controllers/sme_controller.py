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
"""

from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify, session
from flask_login import current_user

from app.extensions import db
from app.models import SmeProfile, ForecastResult, PlanSave, MarketData
from app.ml.constants import (
    BUSINESS_TYPES,
    FEATURED_BUSINESS_TYPES,
    INDUSTRY_DISPLAY,
    DEFAULT_INDUSTRY_DISPLAY,
    short_industry_label,
)
from app.ml.seed_data import BARANGAY_NAMES, get_real_population
from app.utils.decorators import role_required
from app.utils.audit import log_action
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


@sme_bp.route("/home")
@role_required("SME")
def home():
    profiles = current_user.sme_profiles.order_by(SmeProfile.sme_id.desc()).all()
    locations = BARANGAY_NAMES
    selected_plan = _selected_home_plan(profiles)
    default_location = selected_plan.location if selected_plan else (locations[0] if locations else "Poblacion")

    # One quick, EPHEMERAL score per industry type (no forecast_result
    # write -- see compute_scores docstring) for the SME's own/default
    # location, so the "Industry Cards" on the Home page have live
    # numbers, matching the storyboard's card layout. Only a curated
    # FEATURED_BUSINESS_TYPES subset is scored here (not the full
    # expanded BUSINESS_TYPES list) so this page stays fast -- every
    # industry is still fully selectable in the "+ New Business Plan"
    # modal and the search bar below.
    # Scored in ONE batch, not one call per card. Same reasoning as the
    # Recommendations page: a loop of compute_scores() is two SELECTs
    # and a separate forest dispatch per industry, and this is the first
    # page an SME lands on after signing in.
    industry_cards = []
    card_scores = compute_scores_batch(
        [(industry_type, default_location) for industry_type in FEATURED_BUSINESS_TYPES]
    )
    for industry_type, scores in zip(FEATURED_BUSINESS_TYPES, card_scores):
        trend = "up" if scores["viability_score"] >= 6.5 else ("down" if scores["viability_score"] < 5 else "neutral")
        display = INDUSTRY_DISPLAY.get(industry_type, DEFAULT_INDUSTRY_DISPLAY)
        industry_cards.append({
            "name": industry_type,
            "score": scores["viability_score"],
            "trend": trend,
            "icon": display["icon"],
            "subtitle": display["subtitle"],
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
        elif _forecast_predates_lgu_data(featured_forecast):
            # An LGU upload has landed since this forecast was written,
            # so its numbers -- and the recommendation text quoting
            # them -- describe a city that no longer matches the
            # records. Re-running it here is what makes "upload the
            # permits and the output changes" true on the page the SME
            # actually lands on, rather than only after they happen to
            # edit their plan.
            featured_forecast = generate_forecast_for_profile(selected_plan)
            latest_by_plan[selected_plan.sme_id] = featured_forecast

    featured_industry_type = (
        featured_forecast.input_industry_type if featured_forecast else FEATURED_BUSINESS_TYPES[0]
    )

    quarterly_outlook = None
    featured_recommendation = None
    if featured_forecast:
        quarterly_outlook = project_quarterly_outlook(
            featured_forecast.saturation_index,
            featured_forecast.viability_score,
            featured_forecast.input_location,
            industry_type=featured_forecast.input_industry_type,
        )
        # forecast_result.recommendation is JSON-serialized structured
        # data (see recommendation_service.py) -- parse it back into
        # {headline, opportunity_type, summary, reasons, risks} so this
        # panel shows readable text instead of a raw JSON string.
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

    # The choice bar: every plan with its own market score, so the owner
    # can compare plans at a glance and switch without re-entering any.
    plan_choices = []
    for profile in profiles:
        latest = latest_by_plan.get(profile.sme_id)
        plan_choices.append({
            "profile": profile,
            "viability_score": latest.viability_score if latest else None,
            "cluster_label": latest.cluster_label if latest else None,
            "selected": selected_plan is not None and profile.sme_id == selected_plan.sme_id,
        })

    return render_template(
        "sme/home.html",
        profiles=profiles,
        selected_plan=selected_plan,
        plan_choices=plan_choices,
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
    )


@sme_bp.route("/home/analyze", methods=["POST"])
@role_required("SME")
def analyze():
    """The "Add New Plan" dialog: create a new SmeProfile and run the AI
    forecasting engine on it immediately (see
    generate_forecast_for_profile).

    Parsing is plan_params.parse_plan_form() -- the same parser the
    sign-up wizard and the Settings Edit form use, so the three cannot
    disagree about what a plan is (monthly revenue included: none of
    them collects it any more)."""
    from app.services.plan_params import apply_plan_data, parse_plan_form

    data, errors = parse_plan_form(request.form)
    if errors:
        for message in errors:
            flash(message, "danger")
        return redirect(url_for("sme.home"))

    profile = apply_plan_data(SmeProfile(user_id=current_user.user_id), data)
    db.session.add(profile)
    db.session.commit()

    generate_forecast_for_profile(profile)
    log_action("run_forecast", details=f"{profile.industry_type} @ {profile.location}", target=profile)
    # Select it now, so landing on it is not also logged as a switch.
    session[HOME_PLAN_SESSION_KEY] = profile.sme_id
    flash("Forecast generated -- see your results below.", "success")
    # Land on the plan just made, so the choice bar, the map and the
    # forecast panel all show it rather than whichever plan was selected.
    return redirect(url_for("sme.home", plan=profile.sme_id))


@sme_bp.route("/home/plans/<int:sme_id>/update", methods=["POST"])
@role_required("SME")
def update_plan(sme_id):
    """Powers the inline Edit form in Settings > Business Preferences
    (shared/settings.html) -- called via fetch(), so this returns JSON
    and never redirects, which is what lets the pane update the edited
    row in place instead of reloading the whole Settings page.

    The form also has a real action= pointing here, so with scripting
    off the submit still saves; the visitor just sees the JSON."""
    from app.services.plan_params import apply_plan_data, parse_plan_form

    profile = SmeProfile.query.get_or_404(sme_id)
    if profile.user_id != current_user.user_id:
        abort(403)

    form = request.form.copy()  # a mutable MultiDict; getlist() still works
    # A missing stage picker means "unchanged", not "reset to startup".
    if not form.get("business_stage"):
        form["business_stage"] = profile.business_stage or "startup"

    data, errors = parse_plan_form(form, keep_industry=profile.industry_type)
    if errors:
        return jsonify({"success": False, "error": " ".join(errors)}), 400

    # Keep a registration date the form did not send, rather than
    # re-dating an existing business to today on every edit.
    if not form.get("registration_date") and profile.registration_date and data["business_stage"] == "existing":
        data["registration_date"] = profile.registration_date.isoformat()

    apply_plan_data(profile, data)
    db.session.commit()
    # Re-run the AI engine so the Home page's featured forecast reflects
    # the edited industry/location/sub-category immediately.
    generate_forecast_for_profile(profile)
    log_action("update_plan", details=f"-> {profile.industry_type} @ {profile.location}", target=profile)

    return jsonify({"success": True, "plan": profile.to_dict()})


@sme_bp.route("/home/plans/<int:sme_id>/delete", methods=["POST"])
@role_required("SME")
def delete_plan(sme_id):
    """Powers the Delete button in Settings > Business Preferences.
    Deleting a SmeProfile
    cascades (ORM-level cascade="all, delete-orphan") to its
    ForecastResult rows, which in turn cascades to their PlanSave rows
    -- see app/models/sme_profile.py and app/models/forecast_result.py."""
    profile = SmeProfile.query.get_or_404(sme_id)
    if profile.user_id != current_user.user_id:
        abort(403)

    details = f"sme_id={profile.sme_id} {profile.industry_type}@{profile.location}"
    db.session.delete(profile)
    db.session.commit()
    log_action("delete_plan", details=details)

    return jsonify({"success": True})


@sme_bp.route("/saturation-map")
@role_required("SME", "LGU", "Admin")
def saturation_map():
    return render_template(
        "sme/saturation_map.html",
        business_types=BUSINESS_TYPES,
        locations=BARANGAY_NAMES,
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
