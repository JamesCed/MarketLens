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
from app.models import LguData, MarketData, Notification, SystemSetting, SmeProfile
from app.ml.constants import BUSINESS_TYPES, DETAIL_PANEL_SECTIONS
from app.ml.seed_data import BARANGAY_NAMES
from app.services.response_cache import cached_on_data
from app.services.forecasting_service import compute_scores, compute_scores_batch
from app.services.places_service import search_competitors, search_competitors_detailed

api_bp = Blueprint("api", __name__)


def _known_locations():
    """Every location name the app currently has ANY data for, so the
    map always has something to plot: the 76 official Tarlac City
    barangays (see app/ml/seed_data.py) plus any real barangay/location
    a real LGU upload or a real SME plan has already introduced."""
    names = set(BARANGAY_NAMES)
    names.update(row[0] for row in db.session.query(LguData.barangay).distinct().all())
    names.update(row[0] for row in db.session.query(MarketData.location).distinct().all())
    return sorted(names)


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
@cached_on_data("locations-forecast", query_args=("industry_type",))
def locations_forecast():
    """Powers the Saturation Map's pins: one ephemeral score (see
    forecasting_service.compute_scores -- no forecast_result row is
    written) per known location, for the chosen industry_type."""
    from app.ml.seed_data import get_real_population, get_barangay_profile

    industry_type = request.args.get("industry_type", BUSINESS_TYPES[0])
    locations = _known_locations()

    # ONE batched scoring call, not one per barangay. The loop that was
    # here ran compute_scores() 76 times: 228 database round trips and
    # 76 separate passes over the Random Forest, measured at 4.24s for
    # a page that cannot draw until it returns. Batched it is 0.05s and
    # 3 queries, with byte-identical output -- see
    # forecasting_service.compute_scores_batch.
    scored = compute_scores_batch([(industry_type, location) for location in locations])

    rows = []
    for location, scores in zip(locations, scored):
        profile = get_barangay_profile(location) or {}
        rows.append(
            {
                "location": location,
                "industry_type": industry_type,
                "saturation_index": scores["saturation_index"],
                "viability_score": scores["viability_score"],
                "cluster_label": scores["cluster_label"],
                "competitor_count": scores["competitor_count"],
                "confidence_level": scores["confidence_level"],
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
@cached_on_data("barangay-choropleth")
def barangay_choropleth():
    """Polygon cells for the Saturation Map's choropleth -- see
    app/services/choropleth_service.py for exactly what these polygons
    are (each barangay's real coordinate's nearest-neighbor region,
    clipped to Tarlac City's REAL administrative boundary -- not a
    bounding rectangle) and are NOT (a surveyed per-barangay boundary
    -- no public dataset publishes one for Tarlac City's 76 barangays;
    see that module's docstring). Geometry only depends on the
    barangay coordinate set, not on industry_type/overlay mode, so the
    front end fetches this once per page load and re-styles the same
    cells as the industry/overlay selectors change."""
    from app.services.choropleth_service import compute_choropleth_geojson
    from app.services.geocoding_service import merged_coords

    api_key = (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()
    geocoding_key = (current_app.config.get("GOOGLE_GEOCODING_API_KEY") or "").strip()

    app_obj = current_app._get_current_object()
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


@api_bp.route("/barangay-detail")
@login_required
def barangay_detail():
    """Powers the Saturation Map's detail panel: Total Businesses /
    Food / Service / Retail Industry counts (real market_data.
    competitor_count per industry, fetched live via the Google Places
    API (New) -- see app/services/places_service.py), real 2024 PSA
    Population, this industry's own AI Density/Saturation Score, and
    short Recommended Actions text -- all for ONE barangay, matching
    the redesigned map's reference screenshot layout."""
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
    def _section_count(key):
        """Competitor count for one of the three named PSIC sections the
        detail panel breaks out -- see constants.DETAIL_PANEL_SECTIONS."""
        section = DETAIL_PANEL_SECTIONS[key]
        row = location_rows.get(section)
        return int(row.competitor_count or 0) if row is not None else 0

    food_count = _section_count("food")
    service_count = _section_count("service")
    retail_count = _section_count("retail")

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
            "food_count": food_count,
            "service_count": service_count,
            "retail_count": retail_count,
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
