"""
app/controllers/api_controller.py
------------------------------------
Small JSON API used by the front-end JavaScript (static/js/map.js and
static/js/main.js) so the Google Maps satellite view and the Chart.js
graphs can pull live data without a full page reload. This is also the
"API connections (preparation)" layer mentioned in the README -- every
external-facing JSON shape the front-end depends on lives in this one
file, so it is easy to find and extend.

IMPORTANT (matches app/services/places_service.py's docstring): there
is no barangays table with stored lat/lng in the real schema, so
these endpoints only ever deal in location NAMES. Map coordinates for
those names come from app/services/geocoding_service.py -- REAL,
PhilAtlas-sourced positions for the 76 official Tarlac City barangays,
with live Google geocoding kept only as a fallback for a name outside
that list (see that module's docstring).

All routes require login; the map/report data itself isn't sensitive
per-user, but keeping it behind auth is simplest and matches "no
anonymous access to any part of the DSS" implied by the RBAC section.
"""

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Notification, SystemSetting, SmeProfile
from app.ml.constants import BUSINESS_TYPES, short_industry_label
from app.ml.seed_data import BARANGAY_NAMES
from app.services.response_cache import cached_on_data
from app.services.forecasting_service import compute_scores, compute_scores_batch
from app.services.places_service import search_competitors, search_competitors_detailed

api_bp = Blueprint("api", __name__)


def _known_locations():
    """The locations the map plots: Tarlac City's 76 official barangays
    (app/ml/seed_data.py), and nothing else.

    This used to add every distinct location name found in lgu_data and
    market_data, which is how a stray "Baras" -- the same place as the
    official "Baras-baras", written differently in one data source --
    showed up as a 77th barangay with no shape on the map. Names like
    that are now folded into their official barangay when the data is
    stored (see seed_data.canonical_barangay and
    startup_migrations._merge_barangay_aliases), and a name that is not
    a Tarlac City barangay at all has no boundary to draw, so neither
    belongs in the map's list. An SME plan for another location is
    still scored; it just is not a map cell."""
    return list(BARANGAY_NAMES)


@api_bp.route("/locations")
@login_required
def locations():
    return jsonify(_known_locations())


@api_bp.route("/my-plans")
@login_required
def my_plans():
    """The signed-in SME's business plans as JSON.

    This used to power the "My Plans" popup in the top bar. That popup
    is gone -- the plans are now server-rendered in Settings > Business
    Preferences -- so nothing in the UI calls this any more. It is kept
    because it is the one machine-readable view of a user's own plans,
    it is what test_signup_flow asserts the registration wizard writes,
    and an endpoint that returns a user their own rows costs nothing to
    keep correct.

    Returns [] for non-SME accounts -- only SME users have sme_profile
    rows."""
    if not current_user.is_sme():
        return jsonify([])
    profiles = current_user.sme_profiles.order_by(SmeProfile.sme_id.desc()).all()
    return jsonify([p.to_dict() for p in profiles])


@api_bp.route("/locations-forecast")
@login_required
@cached_on_data("locations-forecast", query_args=("industry_type", "as_of"))
def locations_forecast():
    """Powers the Saturation Map: one ephemeral score (see
    forecasting_service.compute_scores -- no forecast_result row is
    written) per barangay, for the chosen industry_type.

    `as_of` ("YYYY-MM", optional) scores the map for another month --
    history back to January 2020, or the model's prediction up to a year
    ahead. Each row then says which: `period` (history / current /
    future) and `basis` (recorded / back-projected / current /
    predicted). See app/services/saturation_timeline_service.py."""
    from app.ml.seed_data import get_real_population, get_barangay_profile
    from app.services.saturation_timeline_service import saturation_map_at

    industry_type = request.args.get("industry_type", BUSINESS_TYPES[0])
    locations = _known_locations()

    # ONE batched scoring call, not one per barangay (inside
    # saturation_map_at). The loop that used to be here ran
    # compute_scores() 76 times: 228 database round trips, measured at
    # 4.24s for a page that cannot draw until it returns. Batched it is
    # 0.05s -- see forecasting_service.compute_scores_batch.
    scored = saturation_map_at(industry_type, locations, as_of=request.args.get("as_of"))

    rows = []
    for entry in scored:
        location, scores = entry["location"], entry["scores"]
        profile = get_barangay_profile(location) or {}
        rows.append(
            {
                "location": location,
                "industry_type": industry_type,
                "saturation_index": scores["saturation_index"],
                "viability_score": scores["viability_score"],
                "cluster_label": scores["cluster_label"],
                "competitor_count": entry["competitor_count"],
                "confidence_level": scores["confidence_level"],
                "period": entry["period"],
                "as_of": entry["as_of"],
                "basis": entry["basis"],
                # Carried here so the map's hover tooltip can name the
                # industry and show the barangay's real 2024 PSA
                # population without a second round trip per hover.
                "population": get_real_population(location),
                # REAL (73/76 barangays; 3 use a documented dataset-wide
                # estimate -- see app/ml/seed_data.py) people/km2, so the
                # Saturation Map's sociodemographic overlay is grounded in
                # the same PSA figures as everything else, not invented.
                "population_density": profile.get("population_density"),
            }
        )
    return jsonify(rows)


@api_bp.route("/lgu-recommendations")
@login_required
def lgu_recommendations():
    """The Home page's "LGU Recommendations" panel: the city's best and
    worst barangays for one industry, ranked server-side.

    A SEPARATE ENDPOINT FROM /locations-forecast, on purpose.

    /locations-forecast also draws the Saturation Map, and the map has
    to keep working with or without an uploaded permit register -- it
    answers "what does the data we have say", which is a fair question
    either way. This endpoint answers "which barangays should the city
    steer investment to", which is a claim about the city's own
    records, and it declines to answer before those records exist.
    Gating the shared endpoint would have blanked the map in order to
    gate the panel.

    Ranking happens HERE rather than in the browser because "top
    opportunity" and "top saturated" are findings, not presentation:
    the page must not be able to disagree with the API about which
    barangay is best, and a second consumer should not have to
    re-implement the sort to get the same answer.
    """
    from app.services.data_import_service import has_active_lgu_data

    if not has_active_lgu_data():
        return jsonify({"has_lgu_data": False, "top_opportunity": [], "top_saturated": []})

    industry_type = request.args.get("industry_type", BUSINESS_TYPES[0])
    locations = _known_locations()
    scored = compute_scores_batch([(industry_type, location) for location in locations])

    rows = [
        {
            "location": location,
            "saturation_index": scores["saturation_index"],
            "viability_score": scores["viability_score"],
            "cluster_label": scores["cluster_label"],
            "competitor_count": scores["competitor_count"],
        }
        for location, scores in zip(locations, scored)
    ]
    # Least saturated first. Ties broken by viability, then by name, so
    # the list is stable between refreshes -- a "top 3" that reshuffles
    # on reload with unchanged data reads as noise, not a finding.
    rows.sort(key=lambda row: (row["saturation_index"], -row["viability_score"], row["location"]))

    return jsonify({
        "has_lgu_data": True,
        "industry_type": industry_type,
        "barangays_ranked": len(rows),
        "top_opportunity": rows[:3],
        "top_saturated": list(reversed(rows[-3:])),
    })


@api_bp.route("/forecast")
@login_required
def forecast():
    """Ephemeral single-location lookup -- e.g. a map pin's info window,
    or a 'quick check' before an SME commits to a full business plan."""
    industry_type = request.args.get("industry_type", BUSINESS_TYPES[0])
    location = request.args.get("location", "").strip()
    years_in_operation = request.args.get("years_in_operation", type=float) or 0
    if not location:
        return jsonify({"error": "location is required"}), 400
    return jsonify(compute_scores(industry_type, location, years_in_operation))


@api_bp.route("/places/nearby")
@login_required
def places_nearby():
    location = request.args.get("location", "").strip()
    industry_type = request.args.get("industry_type", BUSINESS_TYPES[0])
    api_key = current_app.config.get("GOOGLE_PLACES_API_KEY", "")
    max_results = int(SystemSetting.get_float("places_max_results", 0))  # 0 = unlimited

    # The detailed call also hands back the micro businesses that were
    # filtered OUT of the count (this system's scope is SMEs -- see
    # places_service.MICRO_BUSINESS_PATTERNS). They're returned so the UI
    # can show what was excluded rather than just asserting a number.
    detail = search_competitors_detailed(
        location, industry_type, api_key=api_key, max_results=max_results
    )
    return jsonify(
        {
            "location": location,
            "industry_type": industry_type,
            "simulated": detail["simulated"],
            "places": detail["results"],
            "excluded_micro": detail["excluded_micro"],
            "excluded_micro_count": len(detail["excluded_micro"]),
        }
    )


@api_bp.route("/barangay-coords")
@login_required
def barangay_coords():
    """Map coordinates for every known barangay -- see
    app/services/geocoding_service.py.

    For all 76 official Tarlac City barangays this is the REAL,
    PhilAtlas-sourced coordinate shipped in
    app/static/data/barangay_coords.json -- answered instantly, with no
    network call and nothing that can be "still resolving". A location
    name outside that list (a typo, or a barangay a future LGU upload
    introduces) falls back to live Google geocoding, bounds-checked and
    cached in the instance folder; `limit` bounds how many such fallback
    names one request will attempt, and the response reports how many
    are still `pending` for that (normally empty) set.
    """
    from app.services.geocoding_service import merged_coords

    api_key = (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()
    geocoding_key = (current_app.config.get("GOOGLE_GEOCODING_API_KEY") or "").strip()
    limit = request.args.get("limit", default=12, type=int)
    limit = max(0, min(limit, 40))

    names = _known_locations()
    coords, resolved_now, pending = merged_coords(
        current_app._get_current_object(), names, api_key=api_key, limit=limit, geocoding_key=geocoding_key
    )

    return jsonify(
        {
            "barangays": coords,
            "resolved_now": resolved_now,
            "pending": pending,
            "real_count": len([c for c in coords.values() if c.get("source") == "real"]),
            "unresolved_count": pending,
        }
    )


@api_bp.route("/barangay-choropleth")
@login_required
# "-official": a new key, so a deployment does not keep serving the old
# computed cells from its on-disk cache after this change.
@cached_on_data("barangay-choropleth-official")
def barangay_choropleth():
    """The barangay shapes for the Saturation Map's choropleth.

    The OFFICIAL PSA/NAMRIA barangay boundaries (2023) when the
    boundaries file is present -- which it is in every normal deployment
    -- and the older computed cells only as a fallback. See
    app/services/choropleth_service.py. Geometry does not depend on
    industry_type or the overlay, so the front end fetches this once per
    page load and re-styles the same shapes as the selectors change."""
    from app.services.choropleth_service import barangay_boundaries_geojson, compute_choropleth_geojson
    from app.services.geocoding_service import merged_coords

    app_obj = current_app._get_current_object()
    official = barangay_boundaries_geojson(app_obj, names=_known_locations())
    if official is not None and official["features"]:
        return jsonify(official)

    api_key = (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()
    geocoding_key = (current_app.config.get("GOOGLE_GEOCODING_API_KEY") or "").strip()

    names = _known_locations()
    coords, _resolved_now, _pending = merged_coords(
        app_obj, names, api_key=api_key, limit=12, geocoding_key=geocoding_key
    )
    return jsonify(compute_choropleth_geojson(coords, app=app_obj))


@api_bp.route("/tarlac-city-boundary")
@login_required
def tarlac_city_boundary():
    """Tarlac City's real outline (see choropleth_service.py's module
    docstring for provenance) as a single-feature GeoJSON
    FeatureCollection, for map.js to draw as its own bold outline
    overlay -- distinct from the barangay choropleth cells, so the
    user can see the actual city border regardless of which barangay
    cells are shown/isolated/filtered."""
    from app.services.choropleth_service import load_city_boundary_ring

    ring = load_city_boundary_ring(current_app._get_current_object())
    return jsonify(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Tarlac City"},
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                }
            ],
        }
    )


@api_bp.route("/places-refresh", methods=["POST"])
@login_required
def places_refresh():
    """Fetch real Google Places counts for a BATCH of (industry x
    barangay) combinations that don't have them yet, and report
    progress. Called repeatedly by the "Fetch all from Google" control
    on the Trend Reports page.

    Deliberately batched and user-initiated: a full sweep of the grid is
    ~1,800 combinations and, with result paging on, up to ~5,500
    billable Places calls -- see market_refresh_service's module
    docstring. Batching keeps that visible and interruptible.
    """
    from app.services.market_refresh_service import MAX_BATCH, refresh_batch
    from app.services.response_cache import clear_response_cache
    from app.services.trend_analytics_service import clear_trend_caches

    # READ-ONLY DEPLOYMENTS. places_service.live_fetch_enabled() already
    # stops the outbound calls themselves, so nothing can be spent even
    # without this check -- but a button that appears to work and
    # silently achieves nothing is its own kind of broken. Refusing here
    # is what lets the page say WHY.
    if not current_app.config.get("PLACES_LIVE_FETCH", True):
        return (
            jsonify(
                {
                    "error": "Live Google Places fetching is switched off on this deployment. "
                    "It serves the competitor data already collected. To fetch new data, "
                    "run the app locally with PLACES_LIVE_FETCH=true and a Places API key.",
                    "live_fetch_disabled": True,
                }
            ),
            403,
        )

    if not (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip():
        return jsonify({"error": "No GOOGLE_PLACES_API_KEY configured in .env."}), 400

    limit = request.args.get("limit", default=MAX_BATCH, type=int)
    result = refresh_batch(limit)
    # A batch rewrites market_data wholesale, so drop the memoised trend
    # sweep and snapshot rather than waiting for the fingerprint to be
    # re-read on the next request.
    clear_trend_caches()
    clear_response_cache()
    return jsonify(result)


@api_bp.route("/places-refresh/progress")
@login_required
def places_refresh_progress():
    """How much of the industry x barangay grid already holds real,
    uncapped Google data. Cheap -- a single indexed read, no HTTP."""
    from app.services.market_refresh_service import get_progress

    progress = get_progress()
    progress["configured"] = bool((current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip())
    # The page uses this to hide the fetch control and to stop calling
    # imported rows "simulated" -- on a read-only deployment the data IS
    # real Google data, it was just collected somewhere else.
    progress["live_fetch_disabled"] = not current_app.config.get("PLACES_LIVE_FETCH", True)
    return jsonify(progress)


@api_bp.route("/trend-data")
@login_required
@cached_on_data("trend-data", query_args=("industry_type", "as_of"))
def trend_data():
    """Aggregate, CITY-WIDE data for the LGU/Admin Trend Reports page --
    see app/services/trend_analytics_service.py for exactly how this
    blends the full 76-barangay reference dataset (the AI engine's own
    historical/seeded baseline) with any real, persisted ForecastResult/
    SmeProfile rows and real market_data on file, so the page is never
    empty on a fresh install and gets more "real" as real usage grows.
    This is the SHARED view: SME, LGU and Admin accounts all see this
    same city-wide report. (An earlier version gave SMEs a separate
    per-plan "My Trend Report" instead; that is gone -- a trend report
    is about the market, not about one user's saved plans.)

    `as_of` is the month chosen in the "Select Period" calendar, as
    "YYYY-MM" -- every series ends at that month instead of today."""
    from app.services.trend_analytics_service import build_trend_report, resolve_as_of_month

    industry_type = request.args.get("industry_type")
    as_of = resolve_as_of_month(request.args.get("as_of"))
    return jsonify(build_trend_report(industry_type, as_of=as_of))


@api_bp.route("/trend-period")
@login_required
@cached_on_data("trend-period", query_args=("industry_type", "as_of"))
def trend_period():
    """JUST the half of the Trend Reports page that moves when an SME or
    LGU user picks a different month in "Select Period": the four Figure
    4 KPI cards, the monthly trend lines, the quarterly chart and the
    industry ranking.

    Changing the month used to reload the whole page, which re-ran the
    city-wide AI sweep, rebuilt the Industry Distribution pie and the
    Google Places verification table, and re-downloaded every asset --
    none of which depend on the chosen month at all. This endpoint skips
    all of that, and the sweep behind what's left is served from cache
    (see _cached in trend_analytics_service.py), so a date change is one
    small JSON response instead of a full page rebuild.
    """
    from app.services.trend_analytics_service import build_trend_period, resolve_as_of_month

    industry_type = request.args.get("industry_type") or None
    if industry_type and industry_type not in BUSINESS_TYPES:
        return jsonify({"error": "Unknown industry_type"}), 400
    as_of = resolve_as_of_month(request.args.get("as_of"))
    payload = build_trend_period(industry_type, as_of=as_of)
    # Internal handles shared with build_trend_report() -- never sent.
    payload.pop("_baseline", None)
    payload.pop("_real_forecasts", None)
    return jsonify(payload)


@api_bp.route("/quarterly-performance")
@login_required
def quarterly_performance():
    """Market-based quarterly performance, optionally for ONE industry.

    Separate from /api/trend-data on purpose: the industry dropdown on
    the Trend Reports page re-requests this alone, and it is pure
    database reads (no AI sweep, no HTTP), so switching industry is
    instant instead of re-running the whole city-wide sweep.
    """
    from app.services.trend_analytics_service import (
        get_market_quarterly_performance,
        resolve_as_of_month,
        TREND_WINDOW_QUARTERS,
    )

    industry_type = request.args.get("industry_type") or None
    if industry_type and industry_type not in BUSINESS_TYPES:
        return jsonify({"error": "Unknown industry_type"}), 400
    as_of = resolve_as_of_month(request.args.get("as_of"))
    return jsonify(
        get_market_quarterly_performance(
            industry_type, quarters=TREND_WINDOW_QUARTERS, as_of=as_of
        )
    )


# How many industries the barangay detail panel ranks. Three is what the
# client asked for: enough to show what a barangay is known for, short
# enough to read at a glance on the Home page's small map popup.
TOP_INDUSTRIES_LIMIT = 3


def _top_industries(location_rows, limit=TOP_INDUSTRIES_LIMIT):
    """The `limit` industries with the most businesses in one barangay,
    most first, as [{industry, label, count, is_estimated}].

    `location_rows` is {industry_type: row} taken from
    trend_analytics_service.latest_market_data_by_key(), so each count is
    the SAME reconciled figure (max of Google Places and the LGU/DTI
    permit register, see forecasting_service.reconciled_competitor_counts)
    that the scoring engine, Trend Reports and the LGU dashboard use --
    the panel can never quote a different number for the same barangay.

    WHY THIS REPLACED THE FIXED FOOD / SERVICE / RETAIL ROWS. Those three
    sections were hard-coded from the storyboard, so a barangay whose
    biggest sector is, say, Manufacturing showed three small numbers and
    hid the one that mattered. Ranking by count shows what is actually
    there.

    `is_estimated` is True when the winning count did not come from a
    real source (a live Google Places lookup or an LGU/DTI upload) --
    i.e. it is a generated placeholder, which the page must say. The
    list of real sources is the reconciliation's own, imported rather
    than retyped, so a source added there is automatically trusted here.

    Zero counts are left out (an industry with no businesses is not a
    "top" industry), and ties are broken by name so the order is stable
    between refreshes.
    """
    from app.services.forecasting_service import _COMPETITOR_SOURCES

    ranked = []
    for industry, row in location_rows.items():
        count = int(row.competitor_count or 0)
        if count <= 0:
            continue
        # A reconciled row names the source that supplied the winning
        # figure; a plain row (no Places/DTI figure at all) only has its
        # own source, which is by definition not a real one.
        source = getattr(row, "competitor_source", None) or row.source
        ranked.append(
            {
                "industry": industry,
                "label": short_industry_label(industry),
                "count": count,
                "is_estimated": source not in _COMPETITOR_SOURCES,
            }
        )
    ranked.sort(key=lambda item: (-item["count"], item["industry"]))
    return ranked[:limit]


@api_bp.route("/barangay-detail")
@login_required
def barangay_detail():
    """Powers the barangay detail panel (Saturation Map) and the info
    popup on the Home page's mini map: Total Businesses, the barangay's
    TOP industries by business count (see _top_industries), real 2024
    PSA Population, this industry's own AI Saturation Score, and short
    Recommended Actions text -- all for ONE barangay."""
    from app.services.trend_analytics_service import latest_market_data_by_key
    from app.ml.seed_data import get_real_population, get_barangay_profile

    location = request.args.get("location", "").strip()
    industry_type = request.args.get("industry_type", BUSINESS_TYPES[0])
    if not location:
        return jsonify({"error": "location is required"}), 400

    scores = compute_scores(industry_type, location)

    location_rows = {
        industry: row for (industry, loc), row in latest_market_data_by_key().items() if loc == location
    }
    total_businesses = sum(int(r.competitor_count or 0) for r in location_rows.values())

    if scores["cluster_label"] in ("Low", "Moderate"):
        actions = [
            "Favorable conditions for a new business in this industry",
            "Consider entering this market while demand is undersupplied",
        ]
    else:
        actions = [
            f"Market is {scores['cluster_label'].lower()} for {industry_type}",
            "Consider a niche/differentiated offering, or a nearby barangay",
        ]

    return jsonify(
        {
            "location": location,
            "industry_type": industry_type,
            "cluster_label": scores["cluster_label"],
            "saturation_index": scores["saturation_index"],
            "viability_score": scores["viability_score"],
            "competitor_count": scores["competitor_count"],
            "confidence_level": scores["confidence_level"],
            "total_businesses": total_businesses,
            "top_industries": _top_industries(location_rows),
            "population": get_real_population(location),
            # A FIXED, real per-barangay figure (people/km2) -- unlike
            # every other number in this response, it must never change
            # when industry_type changes. Kept as its own field, separate
            # from saturation_index, after the map's detail panel was
            # found mislabeling saturation_index as "Density Score",
            # which made it look like population density was shifting
            # with the industry dropdown when it never actually did.
            "population_density": (get_barangay_profile(location) or {}).get("population_density"),
            "recommended_actions": actions,
        }
    )


@api_bp.route("/forecasts/<int:forecast_id>/transcript", methods=["POST"])
@login_required
def forecast_transcript(forecast_id):
    """Upgrade ONE stored forecast's transcript from the rule-based model
    summary to Gemini's transcript of the forecast -- called by
    static/js/forecast_transcript.js after the Home or Recommendations
    page has rendered, so the page never waits on an LLM.

    NOTHING IS RE-SCORED. The context is rebuilt from the forecast's
    stored payload and its plan (recommendation_service.
    transcript_context), Gemini is asked for the transcript
    (llm_service.transcribe_forecast -- the whole computation, Gemini
    first, one corrective retry, sentence pruning), and only the stored
    "explanation" changes: every other key of forecast_result.
    recommendation is written back as it was read.

    Only the owner of the forecast's plan may ask: anyone else -- another
    SME, an LGU or Admin account, a plan in Trash -- gets the same 404 as
    a forecast that does not exist, so the endpoint does not reveal which
    ids exist. CSRF: a POST, so Flask-WTF checks the X-CSRFToken header
    the script sends, like every other fetch POST in this app.

    Responses (all 200 unless the forecast is not the caller's):
      {"ok": true, "text", "generated_by", "badge_html"}  -- the new
          transcript (or the AI one already stored); the script inserts
          the text with textContent and swaps in the badge;
      {"ok": false, "reason"}  -- "no_payload" (a forecast from before
          the plan model), "unavailable" (LLM off or no usable Gemini
          key), "retry_later" (an upgrade failed under
          TRANSCRIPT_RETRY_AFTER ago), or "failed" (this attempt failed;
          explanation["transcript_failed_at"] is stamped so the page
          leaves it alone for 15 minutes)."""
    from datetime import datetime, timezone

    from flask import abort, get_template_attribute

    from app.models import ForecastResult
    from app.services import llm_service
    from app.services import recommendation_service as rec_service

    forecast = db.session.get(ForecastResult, forecast_id)
    if forecast is None or not current_user.is_sme():
        abort(404)
    # The ordinary query applies the Trash filter, so a trashed plan's
    # forecast is treated as not there -- the same rule every plan route
    # follows (restore first).
    plan = SmeProfile.query.filter_by(sme_id=forecast.sme_id, user_id=current_user.user_id).first()
    if plan is None:
        abort(404)

    badge = get_template_attribute("shared/_plan_insights.html", "explanation_badge")
    stored = rec_service.stored_recommendation(forecast.recommendation)
    rec = rec_service.parse_recommendation(forecast.recommendation)
    explanation = rec.get("explanation")
    if stored is None or rec.get("forecast") is None or explanation is None:
        # A forecast from before the trained plan model has no computation
        # to transcribe. If it is still the plan's LATEST forecast, re-run
        # it: the new forecast is scored by both models and Gemini writes
        # its transcript as part of the run. (An older, superseded row is
        # left alone -- the plan's current forecast is the one that counts.)
        latest = plan.latest_forecast()
        if latest is None or latest.forecast_id != forecast.forecast_id \
                or not llm_service.gemini_transcription_available():
            return jsonify({"ok": False, "reason": "no_payload"})
        from app.services.forecasting_service import generate_forecast_for_profile

        fresh = generate_forecast_for_profile(plan)
        fresh_rec = rec_service.parse_recommendation(fresh.recommendation)
        fresh_explanation = fresh_rec.get("explanation")
        if fresh_explanation and fresh_explanation["generated_by"].startswith("llm:"):
            return jsonify({"ok": True, "text": fresh_explanation["text"],
                            "generated_by": fresh_explanation["generated_by"],
                            "badge_html": str(badge(fresh_explanation)), "refreshed": True})
        return jsonify({"ok": False, "reason": "failed"})
    if explanation["generated_by"].startswith("llm:"):
        # Already transcribed -- by an earlier request, another tab, or
        # the forecast run itself. Nothing to spend a call on.
        return jsonify({"ok": True, "text": explanation["text"], "generated_by": explanation["generated_by"],
                        "badge_html": str(badge(explanation))})
    if rec_service.transcript_failed_recently(explanation):
        return jsonify({"ok": False, "reason": "retry_later"})
    if not llm_service.gemini_transcription_available():
        return jsonify({"ok": False, "reason": "unavailable"})

    context = rec_service.transcript_context(forecast, plan)
    transcript = llm_service.transcribe_forecast(context) if context else None
    if not transcript:
        forecast.recommendation = rec_service.mark_transcript_failed(
            forecast.recommendation, now=datetime.now(timezone.utc)
        )
        db.session.commit()
        return jsonify({"ok": False, "reason": "failed"})

    forecast.recommendation = rec_service.store_transcript(forecast.recommendation, transcript)
    db.session.commit()
    return jsonify({"ok": True, "text": transcript["text"], "generated_by": transcript["generated_by"],
                    "badge_html": str(badge(transcript))})


# The Home and Recommendations templates ask this (through
# shared/_plan_insights.html) whether to render the transcript upgrade
# hook for a forecast. Registered from here because this blueprint owns
# the endpoint the hook calls; see
# recommendation_service.transcript_upgrade_due.
def _forecast_transcript_due(rec):
    from app.services.recommendation_service import transcript_upgrade_due

    try:
        return transcript_upgrade_due(rec)
    except Exception:  # noqa: BLE001 -- a page render must never fail on this
        return False


api_bp.add_app_template_global(_forecast_transcript_due, "forecast_transcript_due")


def _transcript_queue(forecasts):
    """[(forecast_id, url)] for every forecast in `forecasts` (each plan's
    latest) that should get a Gemini transcript in the background -- see
    recommendation_service.transcript_queue_due. The Planning and Home
    pages render these as hidden hooks for forecast_transcript.js, so
    EVERY plan is transcribed, not only the one on screen."""
    from flask import url_for

    from app.services.recommendation_service import parse_recommendation, transcript_queue_due

    out = []
    try:
        for forecast in forecasts or []:
            if forecast is None:
                continue
            if transcript_queue_due(parse_recommendation(forecast.recommendation)):
                out.append((forecast.forecast_id,
                            url_for("api.forecast_transcript", forecast_id=forecast.forecast_id)))
    except Exception:  # noqa: BLE001 -- a page render must never fail on this
        return []
    return out


api_bp.add_app_template_global(_transcript_queue, "transcript_queue")


@api_bp.route("/notifications")
@login_required
def notifications():
    rows = (
        Notification.query.filter_by(user_id=current_user.user_id)
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    return jsonify(
        [
            {
                "id": n.id,
                "type": n.type,
                "message": n.message,
                "is_read": n.is_read,
                "created_at": n.created_at.strftime("%b %d, %Y %I:%M %p"),
            }
            for n in rows
        ]
    )


@api_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notification_id):
    notification = Notification.query.filter_by(id=notification_id, user_id=current_user.user_id).first()
    if notification:
        notification.is_read = True
        db.session.commit()
    return jsonify({"ok": True})
