"""
app/services/forecasting_service.py
--------------------------------------
This is "the AI" of the Decision Support System: it turns an industry
type + free-text location (+ an SME's own years_in_operation, when
tied to a real plan) into a Market Saturation Index, a viability
score, a saturation cluster label, and a confidence level, using a
trained RandomForestRegressor (app/ml/train_model.py).

Two entry points, because forecast_result.sme_id/market_id/lgu_id are
ALL NOT NULL in the real schema (see app/models/forecast_result.py
docstring) -- there is no "anonymous"/aggregate forecast row:

  - compute_scores(industry_type, location, years_in_operation=0)
        Ephemeral -- makes NO forecast_result write. Used by the
        Saturation Map and Trend Reports, which show scores for a
        industry+location combination that isn't tied to one SME's
        plan. It DOES reuse/create MarketData & LguData snapshot rows
        (that's normal caching of "what does this area look like right
        now", not a forecast).

  - generate_forecast_for_profile(sme_profile)
        Calls compute_scores() for that profile's own
        industry_type/location, then persists a ForecastResult row and
        fires an early-warning Notification when appropriate. Used by
        the Home page / "Generate Forecast" action, which IS tied to a
        specific SME's business plan.

Pipeline (mirrors the paper's Technical Background / Analytical Model
sections):
  1. competitor_count      <- Google Places Text Search (or simulated)
  2. MarketData snapshot    <- population_density, foot_traffic_index,
                                average_rent, historical_success_rate
  3. LguData snapshot        <- business_density (from a real LGU
                                upload, or an auto-generated placeholder
                                -- see find_or_create_lgu_data)
  4. Market Saturation Index (0-100) <- trained RandomForestRegressor
  5. Saturation cluster (Low/Moderate/High/Saturated) <- threshold cut
     points discovered by K-Means during training
  6. Confidence level (0-100) <- agreement between the Random Forest's
     individual trees (low spread across estimators = high confidence)
  7. Viability score (0-10, shown to users) <- derived from saturation
"""

import os
from datetime import date

import joblib
import numpy as np
from sqlalchemy import func

from app.extensions import db
from app.models import MarketData, LguData, ForecastResult, Notification, SystemSetting, User
from app.ml.constants import BUSINESS_TYPE_ENCODING, OTHER_INDUSTRY_ENCODING, CLUSTER_THRESHOLDS, CLUSTER_LABELS_ORDERED
from app.ml.seed_data import get_barangay_profile, get_real_population

# Dedicated system account (created by seed.py) that owns auto-generated
# placeholder LguData rows until a real LGU account uploads actual
# government data for that barangay -- see find_or_create_lgu_data().
SYSTEM_USER_EMAIL = "system@dss.local"

# The exact zoning_info text on a placeholder lgu_data row -- the rows
# find_or_create_lgu_data() writes for itself so the model always has a
# business_density to compute with.
#
# IT IS A CONSTANT BECAUSE TWO PLACES DEPEND ON IT MATCHING.
# data_import_service.has_active_lgu_data() has to tell a row the app
# invented from a row a person uploaded, and authorship alone does not
# settle it: _get_system_user_id() falls back to "the first Admin, then
# any user" on a database with no system account, so a placeholder can
# end up attributed to a real person. This sentence is written in one
# place and read in the other, so re-wording it cannot silently turn
# every placeholder into an "official dataset".
PLACEHOLDER_ZONING_NOTE = (
    "Auto-generated placeholder -- no LGU upload on file yet for this location."
)

_MODEL_CACHE = {"rf": None, "loaded": False}

# How many SIMULATED market_data rows a single web request is allowed to
# re-try against the live Google Places API (see
# find_or_create_market_data). Pages like Trend Reports sweep 8
# industries x 76 barangays = 608 combos in one go; without a budget,
# the first load after adding an API key would sit there making 608
# sequential HTTP calls. With it, each page load upgrades a handful of
# rows to real Google data and stays responsive -- the database
# converges on real data over a few visits instead of stalling once.
# Rows that already hold real Google data are never re-fetched here.
_MAX_LIVE_REFRESH_PER_REQUEST = 8


def _places_recap_watermark():
    """market_id at/below which a Google Places row was fetched under the
    old 20-result cap -- see app/services/startup_migrations.py."""
    try:
        from app.services.startup_migrations import get_places_recap_watermark

        return get_places_recap_watermark()
    except Exception:  # noqa: BLE001 -- a missing settings row must not break scoring
        return 0


def set_places_refresh_budget(limit):
    """Override this request's live-Places budget. `None` means
    unlimited. Used by the explicit bulk-refresh endpoint
    (app/services/market_refresh_service.py), which is the one place
    that is SUPPOSED to make a long run of Google calls -- ordinary page
    loads keep the small default so they stay responsive."""
    try:
        from flask import g, has_request_context

        if has_request_context():
            g._dss_places_budget = limit
    except (ImportError, RuntimeError):
        pass


# ---------------------------------------------------------------------
# THE DAY BUDGET -- the one that stands between a public URL and a bill
# ---------------------------------------------------------------------
# The per-request budget above bounds ONE page load. It does nothing
# about a thousand page loads, and Places API (New) is billed per call.
# With PLACES_LIVE_FETCH=true on a public deployment, every visitor who
# can sign in can trigger lookups, so the per-request cap alone bounds
# the wrong quantity entirely.
#
# This is a second, day-scoped cap, counted in system_settings so it
# survives a worker restart and can be changed from the Admin page
# without a redeploy. Reaching it does not break anything: lookups stop
# and the app serves the counts already on file, which is exactly what
# PLACES_LIVE_FETCH=false does all the time.
#
# IT IS NOT A SUBSTITUTE FOR GOOGLE'S OWN QUOTA. This bounds what the
# APP spends. A quota set in the Cloud Console bounds what the KEY can
# spend, including anything that gets hold of it outside this app.
# Both, or neither is worth much.
PLACES_DAILY_BUDGET_KEY = "places_daily_call_budget"
PLACES_DAY_COUNTER_KEY = "places_calls_today"
PLACES_DAY_STAMP_KEY = "places_calls_day"
DEFAULT_PLACES_DAILY_BUDGET = 500


def places_calls_today():
    """(calls used today, budget) for the Admin page. Budget 0 means
    unlimited. Read-only -- it never advances the counter."""
    today = date.today().isoformat()
    if _DAY_SPEND["day"] == today:
        used = _DAY_SPEND["used"]          # the live tally beats the snapshot
    else:
        stamp = SystemSetting.get(PLACES_DAY_STAMP_KEY, "")
        used = int(SystemSetting.get_float(PLACES_DAY_COUNTER_KEY, 0) or 0)
        if stamp != today:
            used = 0
    budget = int(SystemSetting.get_float(PLACES_DAILY_BUDGET_KEY, DEFAULT_PLACES_DAILY_BUDGET) or 0)
    return used, budget


# Counted in memory, persisted for visibility. See _spend_day_budget.
_DAY_SPEND = {"day": None, "used": 0}


def _spend_day_budget():
    """Count one live lookup against today's allowance. False when the
    allowance is gone.

    ENFORCED IN MEMORY, PERSISTED ONLY WHEN IT IS SAFE TO.

    SystemSetting.set() commits. Calling it from here would commit
    whatever else the caller happens to have staged -- and this runs
    deep inside scoring, which the LGU upload pipeline's single
    all-or-nothing transaction must be able to survive. A budget
    counter that can half-commit somebody else's file import is a
    far worse bug than an imprecise counter.

    So the in-memory tally is what enforces the cap, and the row is
    written only when the session has nothing pending. That is exact
    for this deployment (gunicorn --workers 1, so one process holds
    the whole tally) and slightly under-counts across a restart, which
    errs toward letting a lookup through rather than blocking one --
    the right direction for a safety net whose hard backstop is
    Google's own quota.
    """
    try:
        today = date.today().isoformat()

        # Whether the CALLER has work in flight, captured before this
        # function touches the session at all.
        #
        # Checking it later does not work, and a test caught that:
        # every SystemSetting read below runs a query, a query
        # AUTOFLUSHES pending objects into the transaction, and the
        # commit that follows would then persist them. The guard has to
        # be read first, and the reads themselves have to happen with
        # autoflush off, or simply asking what the budget is would
        # flush somebody else's half-built import into the database.
        caller_has_pending = bool(db.session.new or db.session.dirty or db.session.deleted)

        with db.session.no_autoflush:
            if _DAY_SPEND["day"] != today:
                # New day in this process. A stored count is trusted
                # only if it is for today, so a mid-day restart resumes
                # the allowance rather than starting it over.
                stored_day = SystemSetting.get(PLACES_DAY_STAMP_KEY, "")
                stored_used = int(SystemSetting.get_float(PLACES_DAY_COUNTER_KEY, 0) or 0)
                _DAY_SPEND["day"] = today
                _DAY_SPEND["used"] = stored_used if stored_day == today else 0

            budget = int(SystemSetting.get_float(PLACES_DAILY_BUDGET_KEY,
                                                 DEFAULT_PLACES_DAILY_BUDGET) or 0)

        if budget and _DAY_SPEND["used"] >= budget:
            return False

        _DAY_SPEND["used"] += 1

        if not caller_has_pending:
            SystemSetting.set(PLACES_DAY_STAMP_KEY, today)
            SystemSetting.set(PLACES_DAY_COUNTER_KEY, str(_DAY_SPEND["used"]))
        return True
    except Exception:  # pragma: no cover - a broken counter must not block the page
        from flask import current_app

        current_app.logger.warning("Places day budget check failed", exc_info=True)
        return True


def _can_spend_live_refresh():
    """True if this request still has budget to attempt a live Places
    lookup. Outside a request context (CLI scripts, seeding, tests)
    the per-request budget does not apply -- but the DAY budget still
    does, because a script left in a loop spends the same money a web
    request does."""
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return _spend_day_budget()

        budget = getattr(g, "_dss_places_budget", _MAX_LIVE_REFRESH_PER_REQUEST)
        if budget is not None:
            used = getattr(g, "_dss_places_refreshes", 0)
            if used >= budget:
                return False
            g._dss_places_refreshes = used + 1

        # Per-request budget passed; the day's allowance is the last
        # word. Checked second so an already-exhausted request does not
        # consume a day slot it will not use.
        return _spend_day_budget()
    except (ImportError, RuntimeError):
        return True


def _model_dir():
    from flask import current_app

    return current_app.config["MODEL_DIR"]


def _load_models():
    """Lazily load the trained Random Forest into memory (once per
    process)."""
    if _MODEL_CACHE["loaded"]:
        return
    _MODEL_CACHE["loaded"] = True

    rf_path = os.path.join(_model_dir(), "rf_model.pkl")
    if os.path.exists(rf_path):
        model = joblib.load(rf_path)
        _pin_to_one_thread(model)
        _MODEL_CACHE["rf"] = model


def _pin_to_one_thread(rf_model):
    """Predict on one thread, not one per core.

    The forest is trained with n_jobs=-1, which is right for TRAINING
    -- it is a one-off batch job on a developer machine with cores to
    spare. It is wrong for serving, here, for three separate reasons,
    and the third one is not about speed at all.

    1. IT IS SLOWER ON THIS WORKLOAD. n_jobs=-1 makes every predict()
       hand its trees to a joblib worker pool. For a 76-row city sweep
       there is not enough arithmetic to pay for the handshake:
       measured over 20 sweeps, 42.4 ms with the pool against 15.4 ms
       without it. Before the sweep was batched it was far worse --
       profiling the Recommendations page found 2.0 of its 4.0 seconds
       inside time.sleep() in joblib's worker handshake.

    2. THE DEPLOYMENT HAS NO CORES TO GIVE. A free Render instance is
       roughly a tenth of a CPU, and gunicorn there runs --threads 4.
       Four request threads each spawning a pool of os.cpu_count()
       workers (which in a container reports the HOST's cores, not the
       share this instance actually gets) is a thread pile-up
       competing for one fractional core.

    3. IT MAKES THE MODEL NON-REPRODUCIBLE. This is the one that
       decided it. With n_jobs=-1, sklearn accumulates each tree's
       contribution into a shared array as the workers finish, so the
       summation ORDER varies run to run and so does the last bit of
       the float. Measured on this model: 40 out of 40 repeat
       predict() calls on identical input returned a different answer
       (worst 2.1e-14). With n_jobs=1, 0 out of 40 differed.

       2e-14 changes no decision on its own -- but a saturation index
       sitting exactly on one of the CLUSTER_THRESHOLDS boundaries can
       land either side of it, so the same plan could come back
       "Moderate" on one refresh and "High" on the next with nothing
       having changed. For a decision-support tool whose defence is
       "here is exactly why the system said this", an answer that
       will not sit still is worse than a slow one.

    Arithmetic is otherwise untouched: same trees, same thresholds,
    same feature vector. n_jobs controls dispatch, not the model.
    """
    from flask import current_app

    try:
        rf_model.n_jobs = 1
    except Exception:  # pragma: no cover - a model that refuses is still usable
        current_app.logger.warning("could not pin the forest to one thread", exc_info=True)


def models_are_trained():
    _load_models()
    return _MODEL_CACHE["rf"] is not None


def _cluster_label_for(saturation_index):
    """Low / Moderate / High / Saturated from a 0-100 saturation_index,
    using the cut points app/ml/train_model.py's K-Means run discovered
    (see app/ml/constants.py CLUSTER_THRESHOLDS)."""
    value = float(saturation_index or 0)
    for threshold, label in zip(CLUSTER_THRESHOLDS, CLUSTER_LABELS_ORDERED):
        if value <= threshold:
            return label
    return CLUSTER_LABELS_ORDERED[-1]


def _industry_encoding(industry_type):
    return BUSINESS_TYPE_ENCODING.get(industry_type, OTHER_INDUSTRY_ENCODING)


def _average_or(column, default):
    """Dataset-wide average for a MarketData/LguData numeric column, or
    a neutral default when the table is still empty (brand-new DB,
    before any Gov't Data Upload or Places lookup has ever run)."""
    value = db.session.query(func.avg(column)).scalar()
    return float(value) if value is not None else default


def _get_system_user_id():
    """Real user_id needed to satisfy lgu_data.uploaded_by NOT NULL when
    this engine auto-creates a placeholder LguData row. Prefers the
    dedicated system@dss.local account seed.py creates for exactly this
    purpose; falls back to the first Admin, then to any user.

    If the database has NO users at all, the account is created here
    rather than returning None. Returning None used to produce a raw
    `IntegrityError: NOT NULL constraint failed: lgu_data.uploaded_by`
    the first time anything asked for a score on an unseeded database --
    scoring a location is a read-shaped operation and should not explode
    because seed.py hasn't been run. The account matches the one seed.py
    makes: Admin role, and a random password nobody holds, since it
    exists only to own placeholder rows and is never meant to be logged
    into.
    """
    user = User.query.filter_by(email=SYSTEM_USER_EMAIL).first()
    if user:
        return user.user_id
    admin = User.query.filter_by(role="Admin").order_by(User.user_id.asc()).first()
    if admin:
        return admin.user_id
    any_user = User.query.order_by(User.user_id.asc()).first()
    if any_user:
        return any_user.user_id

    import secrets

    system_user = User(name="System (placeholder data)", email=SYSTEM_USER_EMAIL, role="Admin")
    system_user.set_password(secrets.token_urlsafe(32))
    db.session.add(system_user)
    db.session.commit()
    return system_user.user_id


def find_or_create_market_data(industry_type, location, max_age_days=30):
    """Returns the freshest MarketData row for this industry+location,
    creating a new snapshot (via Google Places, or a clearly-flagged
    estimate) when the newest one on file is missing or stale.

    A cached row is also refreshed EARLY when it is a simulated one
    (source != "Google Places API") and a real Places API key is now
    configured. Without that rule, adding your API key to .env would
    appear to do nothing for up to `max_age_days`: every page would
    keep serving the simulated row it cached before the key existed,
    so no live Google lookup would ever be attempted and the app would
    look like the key was wrong. A row that already holds real Google
    data is still cached normally for the full window.
    """
    from flask import current_app

    from app.services.places_service import get_competitor_count

    api_key = (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()

    existing = (
        MarketData.query.filter_by(industry_type=industry_type, location=location)
        .order_by(MarketData.date_recorded.desc(), MarketData.market_id.desc())
        .first()
    )

    # READ-ONLY DEPLOYMENT: serve whatever is on file, however old, and
    # never replace it.
    #
    # This guard matters more than it looks. Without it, a deployment with
    # live fetching switched off would still fall through below once a row
    # passed `max_age_days`, call get_competitor_count() -- which with
    # fetching off returns a SIMULATED number -- and write that as the new
    # freshest snapshot. The effect would be a live site that looked
    # correct for its first month and then quietly degraded, barangay by
    # barangay, replacing real Google counts with estimates. An old real
    # measurement beats a fresh invented one, so stale-by-age is simply
    # not a reason to refetch when refetching is not an option.
    from app.services.places_service import live_fetch_enabled

    if existing and not live_fetch_enabled():
        return existing

    if existing and (date.today() - existing.date_recorded).days <= max_age_days:
        is_simulated = existing.source != "Google Places API"
        # A real Google row can ALSO be stale-by-content rather than
        # stale-by-age: everything fetched before the result cap was
        # lifted stopped at the first page of 20, so those rows
        # understate every busy barangay. They sit at or below the
        # watermark recorded at upgrade time and get one re-fetch each;
        # the replacement row lands above the watermark and is then
        # cached normally.
        was_capped = (
            not is_simulated
            and existing.market_id is not None
            and existing.market_id <= _places_recap_watermark()
        )
        needs_refetch = (is_simulated or was_capped) and api_key
        if not (needs_refetch and _can_spend_live_refresh()):
            return existing

    max_results = int(SystemSetting.get_float("places_max_results", 0))  # 0 = unlimited
    competitor_count, simulated = get_competitor_count(location, industry_type, api_key=api_key, max_results=max_results)

    if (
        simulated
        and existing is not None
        and existing.source != "Google Places API"
        and (date.today() - existing.date_recorded).days <= max_age_days
    ):
        # We only re-tried because a key is configured and the cached row
        # was simulated -- but the live call still didn't yield real data
        # (quota, network, API not enabled, or genuinely no matches).
        # Reuse the cached row rather than writing a second identical
        # simulated snapshot: otherwise every page load would append one
        # more row per (industry, barangay) forever.
        return existing

    profile = get_barangay_profile(location)
    if profile:
        population_density = profile["population_density"]
        foot_traffic_index = profile["foot_traffic_index"]
        average_rent = profile["average_rent"]
        historical_success_rate = profile["historical_success_rate"]
    else:
        # No seed profile matches this exact location string -- fall
        # back to the dataset's own running averages (or neutral
        # midpoints on a brand-new DB) rather than inventing numbers.
        population_density = _average_or(MarketData.population_density, 8000)
        foot_traffic_index = _average_or(MarketData.foot_traffic_index, 45)
        average_rent = _average_or(MarketData.average_rent, 15000)
        historical_success_rate = _average_or(MarketData.historical_success_rate, 0.55)

    row = MarketData(
        industry_type=industry_type,
        location=location,
        competitor_count=competitor_count,
        population_density=population_density,
        historical_success_rate=historical_success_rate,
        foot_traffic_index=foot_traffic_index,
        average_rent=average_rent,
        source="Manual" if simulated else "Google Places API",
        date_recorded=date.today(),
    )
    db.session.add(row)
    db.session.commit()
    return row


def find_or_create_lgu_data(location):
    """Returns the most recent LguData row for this barangay. If an LGU
    account hasn't uploaded real government data for it yet, this
    auto-creates a clearly-labeled placeholder row (attributed to the
    system account, see _get_system_user_id) from the reference
    barangay profile in app/ml/seed_data.py, so the AI pipeline always
    has SOME business_density figure to compute with. The moment a real
    LGU upload happens for that barangay (data_import_service.py), that
    real row becomes the "most recent" one and takes over."""
    existing = (
        LguData.query.filter_by(barangay=location)
        .order_by(LguData.upload_date.desc(), LguData.lgu_id.desc())
        .first()
    )
    if existing:
        return existing

    profile = get_barangay_profile(location)
    business_density = profile["business_density"] if profile else _average_or(LguData.business_density, 3.0)

    row = LguData(
        source="Other",
        zoning_info=PLACEHOLDER_ZONING_NOTE,
        closure_records=0,
        barangay=location,
        permit_count=0,
        business_density=business_density,
        effective_date=date.today(),
        upload_date=date.today(),
        uploaded_by=_get_system_user_id(),
    )
    db.session.add(row)
    db.session.commit()
    return row


# =====================================================================
# COMPETITOR DENSITY: GOOGLE PLACES RECONCILED WITH LGU PERMITS
# =====================================================================
# Two sources now write a competitor count for the same (industry,
# barangay) into market_data:
#
#   source = "Google Places API"  -- what Google can see operating.
#   source = "DTI"                -- what the city has issued permits
#                                    for, grouped per trade by the LGU
#                                    upload pipeline (see
#                                    data_import_service.derive_permit_counts).
#
# NEWEST-ROW-WINS IS THE WRONG MERGE. It is what the app did by
# accident, and it means whichever source was written last silently
# erases the other: upload a permit register on Tuesday and a Places
# count of 12 becomes a permit count of 3, not because the market
# changed but because a file was uploaded.
#
# WHY max() IS THE RIGHT ONE. Both sources UNDERCOUNT, in different
# directions, and neither contains the other:
#
#   * Google lists businesses that chose to be listed. Unlisted
#     micro-enterprises -- a large share of Philippine SMEs -- are
#     invisible to it, and a Text Search stops issuing page tokens
#     after 60 results.
#   * A permit register holds businesses that registered. Informal
#     operators, and anyone trading on a lapsed permit, are missing
#     from it.
#
# A business seen by EITHER source exists. So the count supported by
# the evidence is the larger of the two, never their sum (which would
# double-count every business that is both listed and licensed, i.e.
# most established ones) and never the newest (which discards
# evidence). max() is the conservative floor: "at least this many
# competitors are really there".
#
# Each source is taken at its own freshest date, so an old permit
# register does not hold back a fresh Google count, and vice versa.

_COMPETITOR_SOURCES = ("Google Places API", "DTI")


def reconciled_competitor_counts(industries, locations, with_source=False):
    """{(industry, location): count} merged across sources, in one
    query. Pairs with no competitor figure at all are simply absent --
    callers keep whatever the chosen market_data row already held.

    `with_source=True` returns {(industry, location): (count, source)}
    instead, naming which source supplied the winning number. The LGU
    dashboard needs that: it reports how much of a barangay's business
    count is confirmed by a live Google lookup, and after
    reconciliation the winning figure may have come from the permit
    register instead -- crediting it to Places would be a false claim
    about where the number came from.
    """
    rows = (
        db.session.query(
            MarketData.industry_type,
            MarketData.location,
            MarketData.source,
            MarketData.competitor_count,
            MarketData.date_recorded,
            MarketData.market_id,
        )
        .filter(
            MarketData.industry_type.in_(industries),
            MarketData.location.in_(locations),
            MarketData.source.in_(_COMPETITOR_SOURCES),
            MarketData.competitor_count.isnot(None),
        )
        .all()
    )

    # Freshest row per (industry, location, source) first...
    freshest = {}
    for industry, location, source, count, recorded, market_id in rows:
        key = (industry, location, source)
        rank = (recorded, market_id)
        if key not in freshest or rank > freshest[key][0]:
            freshest[key] = (rank, int(count))

    # ...then the larger of whatever sources that barangay has.
    merged = {}
    for (industry, location, source), (_rank, count) in freshest.items():
        pair = (industry, location)
        if pair not in merged or count > merged[pair][0]:
            merged[pair] = (count, source)

    if with_source:
        return merged
    return {pair: count for pair, (count, _source) in merged.items()}


def build_feature_vector(market, lgu, years_in_operation, industry_type, competitor_count=None):
    """Order MUST match app/ml/constants.py FEATURE_NAMES exactly -- this
    is the single source of truth train_model.py and this file both use.

    `competitor_count` overrides the chosen row's own figure with the
    reconciled cross-source one (see reconciled_competitor_counts). It
    is passed in rather than written onto the row because the row is a
    live ORM object: assigning to it would mark the session dirty and
    quietly persist a derived number as though it had been measured.
    """
    if competitor_count is None:
        competitor_count = market.competitor_count
    return [
        float(competitor_count or 0),
        float(market.population_density or 0),
        float(market.foot_traffic_index or 0),
        float(market.average_rent or 0),
        float(market.historical_success_rate or 0),
        float(lgu.business_density or 0),
        float(years_in_operation or 0),
        float(_industry_encoding(industry_type)),
    ]


def _tree_predictions(rf_model, X):
    """Every tree's prediction for every row: shape (n_trees, n_rows).

    The forest's own per-tree spread is what confidence_level is
    derived from, and there is no scikit-learn API that returns it --
    so the trees have to be asked individually. The thing that matters
    is asking each tree ONCE for the whole batch instead of once per
    row.

    That distinction dominated the Trend Reports page. The city-wide
    sweep scores 500 (industry, barangay) pairs, and the old code
    called tree.predict() on a single row inside a loop over the
    forest: 500 x n_trees separate calls. Each one goes through
    joblib's dispatch machinery, and profiling the page showed 13 of
    its 28 seconds inside time.sleep() in joblib's worker handshake --
    pure overhead, not arithmetic. Batched, it is n_trees calls in
    total for the whole sweep.
    """
    return np.array([tree.predict(X) for tree in rf_model.estimators_])


def _predict_saturation(feature_vector):
    """Returns (saturation_index 0-100, confidence_level 0-100,
    model_version). confidence_level comes from how much the Random
    Forest's individual trees agree with each other -- a real
    uncertainty signal (low std-dev across rf_model.estimators_'
    predictions = high confidence), not a placeholder number."""
    _load_models()
    rf_model = _MODEL_CACHE["rf"]

    if rf_model is None:
        # Fallback so the app still works before `python -m
        # app.ml.train_model` has ever been run: the same weighted
        # formula the paper describes (competitor density / demand
        # trend proxy / business density), just without the trained
        # model's learned nonlinearity.
        competitor_count, _pop, _foot, _rent, historical_success_rate, business_density, _years, _ind = feature_vector
        w1 = SystemSetting.get_float("msi_weight_competitor_density", 0.45)
        w2 = SystemSetting.get_float("msi_weight_demand_trend", 0.35)
        w3 = SystemSetting.get_float("msi_weight_sociodemographic", 0.20)
        formulaic = (
            w1 * min(1.0, competitor_count / 30.0)
            + w2 * (1 - historical_success_rate)
            + w3 * min(1.0, business_density / 10.0)
        )
        return round(max(0.0, min(100.0, formulaic * 100)), 2), 50.0, "formula_v1"

    X = np.array([feature_vector])
    prediction = float(rf_model.predict(X)[0])
    saturation_index = max(0.0, min(100.0, prediction))

    # One pass per tree over X, rather than a scalar predict() per
    # tree. Same arithmetic, and it is the shape the batch path needs,
    # so there is one implementation instead of two.
    tree_predictions = _tree_predictions(rf_model, X)[:, 0]
    spread = float(np.std(tree_predictions))
    # Map tree-disagreement spread -> 0-100 confidence: 0 spread = 100%
    # confidence, decaying linearly to a floor of 40% once spread
    # reaches ~25 saturation points (an empirically "the trees strongly
    # disagree" spread on this dataset's 0-100 scale).
    confidence_level = max(40.0, min(100.0, 100.0 - (spread / 25.0) * 60.0))
    return round(saturation_index, 2), round(confidence_level, 2), "rf_v1"


def compute_scores(industry_type, location, years_in_operation=0):
    """Ephemeral entry point -- see module docstring. Returns a plain
    dict, no forecast_result row is written."""
    market = find_or_create_market_data(industry_type, location)
    lgu = find_or_create_lgu_data(location)

    competitor_count = reconciled_competitor_counts([industry_type], [location]).get(
        (industry_type, location), market.competitor_count
    )
    feature_vector = build_feature_vector(market, lgu, years_in_operation, industry_type,
                                          competitor_count=competitor_count)
    saturation_index, confidence_level, model_version = _predict_saturation(feature_vector)
    viability_score = round(max(0.0, min(10.0, (100.0 - saturation_index) / 10.0)), 1)
    cluster_label = _cluster_label_for(saturation_index)

    return {
        "industry_type": industry_type,
        "location": location,
        "market_id": market.market_id,
        "lgu_id": lgu.lgu_id,
        "competitor_count": competitor_count,
        "saturation_index": saturation_index,
        "viability_score": viability_score,
        "confidence_level": confidence_level,
        "cluster_label": cluster_label,
        "model_version": model_version,
    }


def compute_scores_batch(pairs, years_in_operation=0):
    """compute_scores() for many (industry_type, location) pairs at
    once, returning the same dicts in the same order.

    WHY THIS EXISTS. The Trend Reports page scores 500 pairs to build
    its city-wide baseline. Calling compute_scores() in a loop does
    that correctly and slowly, in two separate ways:

      QUERIES. find_or_create_market_data() and find_or_create_lgu_data()
      each run a SELECT per pair -- about 1,000 round trips for one
      page. Against a managed database in another data centre (Aiven,
      from Render) that is 1,000 network round trips. Here the freshest
      market_data row per (industry, location) is resolved in ONE
      GROUP BY, and every lgu_data row in one more.

      PREDICTION. See _tree_predictions() above. One predict() over a
      500-row matrix replaces 500 one-row predicts, and n_trees passes
      replace 500 x n_trees.

    Pairs whose market/lgu rows do not exist yet fall back to the
    ordinary per-pair path, which can create them. So a cold database
    behaves exactly as before and a warm one -- which is every request
    after the first -- takes the fast route.
    """
    pairs = list(pairs)
    if not pairs:
        return []

    industries = {industry for industry, _location in pairs}
    locations = {location for _industry, location in pairs}

    market_by_key = _latest_market_rows(industries, locations)
    lgu_by_location = _lgu_rows(locations)
    # One extra query for the whole batch, not one per pair.
    merged_counts = reconciled_competitor_counts(industries, locations)

    resolved, missing = [], []
    for index, (industry, location) in enumerate(pairs):
        market = market_by_key.get((industry, location))
        lgu = lgu_by_location.get(location)
        if market is None or lgu is None:
            missing.append(index)          # needs the row-creating path
        else:
            resolved.append((index, industry, location, market, lgu))

    results = [None] * len(pairs)

    if resolved:
        matrix = np.array([
            build_feature_vector(
                market, lgu, years_in_operation, industry,
                competitor_count=merged_counts.get((industry, location), market.competitor_count),
            )
            for _i, industry, location, market, lgu in resolved
        ])
        _load_models()
        rf_model = _MODEL_CACHE["rf"]

        if rf_model is None:
            # No trained model on disk yet (before the first
            # train_and_save). There is nothing to batch, and the
            # weighted-formula fallback lives in _predict_saturation --
            # so defer to it rather than restating the formula here.
            for position, (index, industry, location, market, lgu) in enumerate(resolved):
                saturation, confidence, version = _predict_saturation(list(matrix[position]))
                results[index] = _score_dict(
                    industry, location, market, lgu, saturation, confidence, version,
                    competitor_count=merged_counts.get((industry, location), market.competitor_count),
                )
        else:
            predictions = np.clip(rf_model.predict(matrix), 0.0, 100.0)
            spreads = _tree_predictions(rf_model, matrix).std(axis=0)
            confidences = np.clip(100.0 - (spreads / 25.0) * 60.0, 40.0, 100.0)
            for position, (index, industry, location, market, lgu) in enumerate(resolved):
                results[index] = _score_dict(
                    industry, location, market, lgu,
                    float(predictions[position]), float(confidences[position]), "rf_v1",
                    competitor_count=merged_counts.get((industry, location), market.competitor_count),
                )

    for index in missing:
        industry, location = pairs[index]
        results[index] = compute_scores(industry, location, years_in_operation)

    return results


def _score_dict(industry_type, location, market, lgu, saturation_index, confidence_level,
                model_version, competitor_count=None):
    """The one place the compute_scores() result shape is defined, so
    the batch path cannot drift away from the single-row path.

    `competitor_count` is the reconciled cross-source figure. It is
    reported as well as scored: a card that says "12 competitors" and
    a model that scored 3 would be two different answers on one page.
    """
    saturation_index = round(max(0.0, min(100.0, saturation_index)), 2)
    if competitor_count is None:
        competitor_count = market.competitor_count
    return {
        "industry_type": industry_type,
        "location": location,
        "market_id": market.market_id,
        "lgu_id": lgu.lgu_id,
        "competitor_count": competitor_count,
        "saturation_index": saturation_index,
        "viability_score": round(max(0.0, min(10.0, (100.0 - saturation_index) / 10.0)), 1),
        "confidence_level": round(confidence_level, 2),
        "cluster_label": _cluster_label_for(saturation_index),
        "model_version": model_version,
    }


def _latest_market_rows(industries, locations):
    """{(industry_type, location): freshest MarketData row} for the
    given combos, in two queries.

    market_data keeps one row per refresh -- a full history -- so
    "freshest" is a greatest-n-per-group problem. It is solved here in
    SQL (GROUP BY the pair, take the newest date, then the highest
    market_id among any same-date ties) so the database returns one
    row per combo instead of the app pulling the whole table and
    discarding most of it.
    """
    newest = (
        db.session.query(
            MarketData.industry_type.label("industry_type"),
            MarketData.location.label("location"),
            db.func.max(MarketData.date_recorded).label("newest_date"),
        )
        .filter(MarketData.industry_type.in_(industries), MarketData.location.in_(locations))
        .group_by(MarketData.industry_type, MarketData.location)
        .subquery()
    )
    winning_ids = (
        db.session.query(db.func.max(MarketData.market_id))
        .join(
            newest,
            db.and_(
                MarketData.industry_type == newest.c.industry_type,
                MarketData.location == newest.c.location,
                MarketData.date_recorded == newest.c.newest_date,
            ),
        )
        .group_by(MarketData.industry_type, MarketData.location)
        .all()
    )
    ids = [row[0] for row in winning_ids if row[0] is not None]
    if not ids:
        return {}
    rows = MarketData.query.filter(MarketData.market_id.in_(ids)).all()
    return {(row.industry_type, row.location): row for row in rows}


def _lgu_rows(locations):
    """{location: freshest LguData row} for the given barangays, in one
    query. Same greatest-n-per-group reasoning as above, resolved on
    the (much smaller) result rather than with a second round trip."""
    rows = (
        LguData.query.filter(LguData.barangay.in_(locations))
        .order_by(LguData.barangay, LguData.upload_date.desc(), LguData.lgu_id.desc())
        .all()
    )
    latest = {}
    for row in rows:
        if row.barangay not in latest:
            latest[row.barangay] = row
    return latest


def generate_forecast_for_profile(sme_profile):
    """Persisting entry point -- see module docstring. Writes a
    ForecastResult row tied to this SmeProfile's OWN
    industry_type/location/years_in_operation, and fires an
    early-warning Notification when the result crosses the
    saturation_alert_threshold system setting.

    The stored recommendation is generated from the SME's OWN input
    business parameters (capital, employees, stage, revenue estimate)
    compared against the real businesses already on file for this
    industry+location (a market_data snapshot from find_or_create_market_data
    above, and -- when that snapshot is real Google data, not a
    simulated fallback -- a short sample of real competitor names) plus
    this barangay's real PSA population. See
    app/services/recommendation_service.py for how that comparison
    turns into the "AI-Powered Recommendation" (rule-based by default,
    or LLM-written when use_llm_recommendations is on)."""
    scores = compute_scores(
        industry_type=sme_profile.industry_type,
        location=sme_profile.location,
        years_in_operation=sme_profile.years_in_operation(),
    )

    from flask import current_app

    from app.services.places_service import search_competitors
    from app.services.recommendation_service import (
        build_recommendation,
        build_recommendation_context,
        serialize_recommendation,
    )

    # A short, real sample of the businesses this plan would actually
    # be competing with -- only fetched when the market_data snapshot
    # backing this forecast is itself real Google data (not a
    # simulated fallback), so we never hand the LLM/rule-based writer a
    # mix of a real competitor_count and fabricated-looking names, and
    # never spend an extra live API call when we already know the
    # result would just be simulated placeholders.
    market_row = MarketData.query.get(scores["market_id"])
    competitor_simulated = not market_row or market_row.source != "Google Places API"
    competitor_sample = []
    if not competitor_simulated:
        api_key = (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()
        sample_results, _ = search_competitors(
            sme_profile.location, sme_profile.industry_type, api_key=api_key, max_results=5
        )
        competitor_sample = [r["name"] for r in sample_results[:5] if r.get("name")]

    context = build_recommendation_context(
        sme_profile,
        scores,
        competitor_sample=competitor_sample,
        competitor_simulated=competitor_simulated,
        population=get_real_population(sme_profile.location) or 0,
    )
    recommendation_text = serialize_recommendation(build_recommendation(context))

    forecast = ForecastResult(
        sme_id=sme_profile.sme_id,
        market_id=scores["market_id"],
        lgu_id=scores["lgu_id"],
        viability_score=scores["viability_score"],
        input_industry_type=sme_profile.industry_type,
        input_location=sme_profile.location,
        saturation_index=scores["saturation_index"],
        recommendation=recommendation_text,
        confidence_level=scores["confidence_level"],
        model_version=scores["model_version"],
        forecast_date=date.today(),
    )
    db.session.add(forecast)
    db.session.commit()

    _maybe_fire_early_warning(forecast, sme_profile)

    return forecast


def _maybe_fire_early_warning(forecast, sme_profile):
    threshold = SystemSetting.get_float("saturation_alert_threshold", 75.0)
    if float(forecast.saturation_index or 0) <= threshold:
        return

    # Respect the account's own switch (Settings -> Notifications). The
    # check is HERE rather than at display time on purpose: someone who
    # turned these off should not accumulate a hidden backlog that all
    # appears the moment they turn them back on.
    #
    # getattr, because a database created before the column existed (and
    # not yet migrated by startup_migrations) would otherwise raise --
    # and an early warning failing to send must never break the forecast
    # that produced it. Absent means on, which matches the column default.
    owner = getattr(sme_profile, "owner", None)
    if owner is not None and not getattr(owner, "notify_early_warning", True):
        return

    message = (
        f"Early warning: {forecast.input_industry_type} in {forecast.input_location} is now "
        f"{forecast.saturation_percent}% saturated (cluster: {forecast.cluster_label}). "
        "Consider an alternative location or a niche offering."
    )
    notification = Notification(
        user_id=sme_profile.user_id,
        forecast_result_id=forecast.forecast_id,
        type="early_warning",
        message=message,
    )
    db.session.add(notification)
    db.session.commit()
