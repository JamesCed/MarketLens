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
  5. Saturation cluster (Low/Moderate/High/Saturated) <- FIXED threshold
     cut points on the same 0-100 scale (app/ml/constants.py
     CLUSTER_THRESHOLDS). Deliberately not the K-Means centroids -- see
     the note there and in app/ml/train_model.py
  6. Confidence level (0-100) <- agreement between the Random Forest's
     individual trees (low spread across estimators = high confidence)
  7. Viability score (0-10, shown to users):
       - for a MARKET (compute_scores: map, trend reports, location
         cards) <- (100 - saturation) / 10, as it always was;
       - for a PLAN (generate_forecast_for_profile) <- stage 2, the
         trained Plan Viability Model, which adds the plan's capital,
         staff, stage, price list, offering and idea to the market
         figures above. See app/services/plan_forecast_service.py and
         Reference/FORECAST_MODEL.md.
"""

import os
import threading
from datetime import date

import joblib
import numpy as np
from sqlalchemy import func

from app.extensions import db
from app.models import MarketData, LguData, ForecastResult, Notification, SystemSetting, User
from app.ml.constants import BUSINESS_TYPE_ENCODING, OTHER_INDUSTRY_ENCODING, CLUSTER_THRESHOLDS, CLUSTER_LABELS_ORDERED
from app.ml.seed_data import get_barangay_profile, get_real_population

SYSTEM_USER_EMAIL = "system@dss.local"

PLACEHOLDER_ZONING_NOTE = (
    "Auto-generated placeholder -- no LGU upload on file yet for this location."
)

_MODEL_CACHE = {"rf": None, "loaded": False}
_MODEL_LOCK = threading.Lock()

_MAX_LIVE_REFRESH_PER_REQUEST = 8


def _places_recap_watermark():
    """market_id at/below which a Google Places row was fetched under the
    old 20-result cap -- see app/services/startup_migrations.py."""
    try:
        from app.services.startup_migrations import get_places_recap_watermark

        return get_places_recap_watermark()
    except Exception:
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


PLACES_DAILY_BUDGET_KEY = "places_daily_call_budget"
PLACES_DAY_COUNTER_KEY = "places_calls_today"
PLACES_DAY_STAMP_KEY = "places_calls_day"
DEFAULT_PLACES_DAILY_BUDGET = 500


def places_calls_today():
    """(calls used today, budget) for the Admin page. Budget 0 means
    unlimited. Read-only -- it never advances the counter."""
    today = date.today().isoformat()
    if _DAY_SPEND["day"] == today:
        used = _DAY_SPEND["used"]
    else:
        stamp = SystemSetting.get(PLACES_DAY_STAMP_KEY, "")
        used = int(SystemSetting.get_float(PLACES_DAY_COUNTER_KEY, 0) or 0)
        if stamp != today:
            used = 0
    budget = int(SystemSetting.get_float(PLACES_DAILY_BUDGET_KEY, DEFAULT_PLACES_DAILY_BUDGET) or 0)
    return used, budget


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

        caller_has_pending = bool(db.session.new or db.session.dirty or db.session.deleted)

        with db.session.no_autoflush:
            if _DAY_SPEND["day"] != today:
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
    except Exception:
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

        return _spend_day_budget()
    except (ImportError, RuntimeError):
        return True


def _model_dir():
    from flask import current_app

    return current_app.config["MODEL_DIR"]


def _load_models():
    """Lazily load the trained Random Forest into memory (once per
    process).

    "loaded" is set only AFTER the model is in the cache, under a lock.
    It used to be set first, before joblib.load() had read the file, so
    under gunicorn's --threads 4 a second request arriving mid-load saw
    "loaded" with no model yet and silently scored its market with the
    formula fallback ("formula_v1") -- and a plan forecast made that way
    is stored. Double-checked: once loaded, no request touches the lock.
    A file that fails to load still marks the cache loaded (in the
    finally), so -- as before -- the failing call raises and later ones
    use the formula rather than retrying a broken file on every request.
    """
    if _MODEL_CACHE["loaded"]:
        return
    with _MODEL_LOCK:
        if _MODEL_CACHE["loaded"]:
            return
        try:
            rf_path = os.path.join(_model_dir(), "rf_model.pkl")
            if os.path.exists(rf_path):
                model = joblib.load(rf_path)
                _pin_to_one_thread(model)
                _MODEL_CACHE["rf"] = model
        finally:
            _MODEL_CACHE["loaded"] = True


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
    except Exception:
        current_app.logger.warning("could not pin the forest to one thread", exc_info=True)


def models_are_trained():
    _load_models()
    return _MODEL_CACHE["rf"] is not None


def _cluster_label_for(saturation_index):
    """Low / Moderate / High / Saturated from a 0-100 saturation_index,
    using the FIXED cut points in app/ml/constants.py CLUSTER_THRESHOLDS.

    These are NOT the K-Means centroids. K-Means runs during training and
    its cluster-to-label map is reported in training_report.json as
    evidence the clustering step was implemented and evaluated, but
    nothing reads it at inference: thresholding the regressor's own
    output is deterministic and keeps every tier boundary on the same
    0-100 scale as saturation_alert_threshold. See the note at the top
    of app/ml/train_model.py."""
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

    from app.services.places_service import live_fetch_enabled

    if existing and not live_fetch_enabled():
        return existing

    if existing and (date.today() - existing.date_recorded).days <= max_age_days:
        is_simulated = existing.source != "Google Places API"
        was_capped = (
            not is_simulated
            and existing.market_id is not None
            and existing.market_id <= _places_recap_watermark()
        )
        needs_refetch = (is_simulated or was_capped) and api_key
        if not (needs_refetch and _can_spend_live_refresh()):
            return existing

    max_results = int(SystemSetting.get_float("places_max_results", 0))
    competitor_count, simulated = get_competitor_count(location, industry_type, api_key=api_key, max_results=max_results)

    if (
        simulated
        and existing is not None
        and existing.source != "Google Places API"
        and (date.today() - existing.date_recorded).days <= max_age_days
    ):
        return existing

    profile = get_barangay_profile(location)
    if profile:
        population_density = profile["population_density"]
        foot_traffic_index = profile["foot_traffic_index"]
        average_rent = profile["average_rent"]
        historical_success_rate = profile["historical_success_rate"]
    else:
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
    newest = _newest_per_group(
        (MarketData.industry_type, MarketData.location, MarketData.source,
         MarketData.competitor_count),
        partition_by=(MarketData.industry_type, MarketData.location, MarketData.source),
        order_by=(MarketData.date_recorded.desc(), MarketData.market_id.desc()),
        filters=(
            MarketData.industry_type.in_(industries),
            MarketData.location.in_(locations),
            MarketData.source.in_(_COMPETITOR_SOURCES),
            MarketData.competitor_count.isnot(None),
            MarketData.archived_at.is_(None),
        ),
    )
    source_order = {source: position for position, source in enumerate(_COMPETITOR_SOURCES)}
    freshest = {
        (industry, location, source): (None, int(count))
        for industry, location, source, count in sorted(
            db.session.execute(newest).all(), key=lambda row: source_order.get(row[2], len(source_order))
        )
    }

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


def forest_predict(rf_model, X):
    """(mean, per_tree) for every row of X: the forest's prediction and
    each tree's own answer, shape (n_rows,) and (n_trees, n_rows).

    Reads each tree's compiled structure directly (tree_.predict on a
    float32 matrix) instead of going through RandomForestRegressor.
    predict() and DecisionTreeRegressor.predict(). Those public calls
    re-validate the input and dispatch through joblib on every call,
    which cost ~10 ms per forest call and ~12 ms per pass over the trees
    on a laptop, many times that on the deployment's fraction of a CPU.

    The arithmetic is the same as sklearn's, in the same order: the same
    float32 input, each tree's leaf value, added tree by tree in
    estimator order, then divided by the number of trees. So the mean is
    bit-identical to rf_model.predict(X) (tests/test_fast_forest.py
    checks it), and the per-tree answers -- what confidence_level is
    computed from -- come out of the same single pass."""
    X32 = np.ascontiguousarray(np.asarray(X, dtype=np.float64), dtype=np.float32)
    n_rows = X32.shape[0]
    estimators = rf_model.estimators_
    per_tree = np.empty((len(estimators), n_rows), dtype=np.float64)
    total = np.zeros(n_rows, dtype=np.float64)
    for position, tree in enumerate(estimators):
        values = tree.tree_.predict(X32).reshape(n_rows, -1)[:, 0]
        per_tree[position] = values
        total += values
    return total / len(estimators), per_tree


def _tree_predictions(rf_model, X):
    """Every tree's prediction for every row: shape (n_trees, n_rows).
    See forest_predict()."""
    return forest_predict(rf_model, X)[1]


def _predict_saturation(feature_vector):
    """Returns (saturation_index 0-100, confidence_level 0-100,
    model_version). confidence_level comes from how much the Random
    Forest's individual trees agree with each other -- a real
    uncertainty signal (low std-dev across rf_model.estimators_'
    predictions = high confidence), not a placeholder number."""
    _load_models()
    rf_model = _MODEL_CACHE["rf"]

    if rf_model is None:
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
    means, per_tree = forest_predict(rf_model, X)
    prediction = float(means[0])
    saturation_index = max(0.0, min(100.0, prediction))
    spread = float(np.std(per_tree[:, 0]))
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


_BATCH_CACHE = {}
_BATCH_CACHE_TTL_SECONDS = 1800
_BATCH_CACHE_MAX_ENTRIES = 64
_BATCH_CACHE_LOCK = threading.Lock()


def _scoring_inputs_fingerprint():
    """market_data's fingerprint (the one Trend Reports already keys on)
    plus lgu_data's -- everything compute_scores_batch() reads from the
    database. Both are memoised per request."""
    from app.services.trend_analytics_service import _data_fingerprint

    return _data_fingerprint(), lgu_data_fingerprint()


def lgu_data_fingerprint():
    """lgu_data's state in one cheap query -- row count, newest id and
    newest upload date -- memoised per request. Any insert or archive
    moves it."""
    from app.utils.request_memo import request_memo

    def query():
        count, max_id, newest = db.session.query(
            func.count(LguData.lgu_id), func.max(LguData.lgu_id), func.max(LguData.upload_date),
        ).one()
        return int(count or 0), int(max_id or 0), str(newest) if newest else None

    return request_memo("lgu_fingerprint", query)


def compute_scores_batch(pairs, years_in_operation=0):
    """compute_scores() for many (industry_type, location) pairs, with
    the result remembered for up to half an hour.

    The same batches recur on every visit -- the Planning slider scores
    all twenty sections in the plan's barangay, the LGU dashboard 120
    industry x barangay cells -- and each one costs four queries plus a
    pass of every tree in the forest. The answer can only change when
    market_data or lgu_data does, so it is keyed on both tables'
    fingerprints (any insert, edit or archive moves them) and, as a
    second net, expires after _BATCH_CACHE_TTL_SECONDS. A batch that had
    to CREATE rows is never cached. Callers get their own copies."""
    import copy
    import time as _time

    pairs = list(pairs)
    if not pairs:
        return []
    try:
        fingerprint = _scoring_inputs_fingerprint()
    except Exception:
        return _compute_scores_batch(pairs, years_in_operation)

    _load_models()
    if _MODEL_CACHE.get("rf") is None:
        return _compute_scores_batch(pairs, years_in_operation)
    key = (tuple(pairs), years_in_operation, id(_MODEL_CACHE["rf"]))
    hit = _BATCH_CACHE.get(key)
    if hit is not None and hit[0] == fingerprint and (_time.monotonic() - hit[1]) <= _BATCH_CACHE_TTL_SECONDS:
        return copy.deepcopy(hit[2])

    results = _compute_scores_batch(pairs, years_in_operation)
    try:
        if _scoring_inputs_fingerprint() == fingerprint:
            with _BATCH_CACHE_LOCK:
                if len(_BATCH_CACHE) >= _BATCH_CACHE_MAX_ENTRIES:
                    _BATCH_CACHE.clear()
                _BATCH_CACHE[key] = (fingerprint, _time.monotonic(), copy.deepcopy(results))
    except Exception:
        pass
    return results


def clear_batch_cache():
    _BATCH_CACHE.clear()


def _compute_scores_batch(pairs, years_in_operation=0):
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
    industries = {industry for industry, _location in pairs}
    locations = {location for _industry, location in pairs}

    market_by_key = _latest_market_rows(industries, locations)
    lgu_by_location = _lgu_rows(locations)
    merged_counts = reconciled_competitor_counts(industries, locations)

    resolved, missing = [], []
    for index, (industry, location) in enumerate(pairs):
        market = market_by_key.get((industry, location))
        lgu = lgu_by_location.get(location)
        if market is None or lgu is None:
            missing.append(index)
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
            for position, (index, industry, location, market, lgu) in enumerate(resolved):
                saturation, confidence, version = _predict_saturation(list(matrix[position]))
                results[index] = _score_dict(
                    industry, location, market, lgu, saturation, confidence, version,
                    competitor_count=merged_counts.get((industry, location), market.competitor_count),
                )
        else:
            means, per_tree = forest_predict(rf_model, matrix)
            predictions = np.clip(means, 0.0, 100.0)
            spreads = per_tree.std(axis=0)
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


def saturation_for_counts(requests_):
    """Saturation for given competitor counts, WITHOUT touching the
    database.

    `requests_` is an iterable of (industry_type, location,
    competitor_count). Returns a list of the same length holding a
    float for each request that could be scored, and None for any
    whose market_data or lgu_data row does not exist yet.

    WHY THIS IS SEPARATE FROM compute_scores_batch()

    Two differences, and both matter to the caller it was written for
    -- market_alert_service, which asks "what WAS the saturation when
    this barangay had 18 competitors instead of 30?"

      1. IT NEVER CREATES A ROW. compute_scores_batch() falls back to
         the per-pair path for anything missing, and that path creates
         a market_data snapshot -- inventing a simulated competitor
         count when no Places key is configured. That is right when
         somebody asked for a forecast. It is wrong for a detector
         whose job is to compare recorded states: it would write a new
         state in the middle of measuring the old one, and then
         measure what it had just written.

      2. THE COUNT IS AN INPUT rather than something looked up. Every
         other feature is held at the row's recorded values, so the
         difference between two results is attributable to the
         competitor count and nothing else -- which is exactly the
         claim an alert makes.

    This replaced inferring the previous saturation by ratio
    (`now_saturation * was_count / now_count`). That inference assumed
    the model responds linearly to the competitor count AND that the
    scored count is the recorded one. Neither holds: a random forest is
    a step function, not a line, and the reconciled cross-source count
    can differ from market.competitor_count. Scoring both states
    properly costs one extra row in a matrix that was being built
    anyway.
    """
    requests_ = list(requests_)
    if not requests_:
        return []

    industries = {industry for industry, _location, _count in requests_}
    locations = {location for _industry, location, _count in requests_}

    market_by_key = _latest_market_rows(industries, locations)
    lgu_by_location = _lgu_rows(locations)

    results = [None] * len(requests_)
    resolved = []
    for index, (industry, location, count) in enumerate(requests_):
        market = market_by_key.get((industry, location))
        lgu = lgu_by_location.get(location)
        if market is not None and lgu is not None:
            resolved.append((index, industry, market, lgu, count))

    if not resolved:
        return results

    matrix = np.array([
        build_feature_vector(market, lgu, 0, industry, competitor_count=count)
        for _index, industry, market, lgu, count in resolved
    ])

    _load_models()
    rf_model = _MODEL_CACHE["rf"]

    if rf_model is None:
        for position, (index, _industry, _market, _lgu, _count) in enumerate(resolved):
            saturation, _confidence, _version = _predict_saturation(list(matrix[position]))
            results[index] = round(max(0.0, min(100.0, saturation)), 2)
        return results

    predictions = np.clip(forest_predict(rf_model, matrix)[0], 0.0, 100.0)
    for position, (index, _industry, _market, _lgu, _count) in enumerate(resolved):
        results[index] = round(float(predictions[position]), 2)
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
    given combos, in one query.

    market_data keeps one row per refresh -- a full history -- so
    "freshest" is a greatest-n-per-group problem. It is solved in SQL
    (newest date, then the highest market_id among same-date ties --
    see _newest_per_group) so the database returns one row per combo
    instead of the app pulling the whole table and discarding most of
    it.
    """
    if not industries or not locations:
        return {}
    newest = _newest_per_group(
        (MarketData,),
        partition_by=(MarketData.industry_type, MarketData.location),
        order_by=(MarketData.date_recorded.desc(), MarketData.market_id.desc()),
        filters=(
            MarketData.industry_type.in_(industries),
            MarketData.location.in_(locations),
            MarketData.archived_at.is_(None),
        ),
        entity=MarketData,
    )
    rows = newest.all()
    return {(row.industry_type, row.location): row for row in rows}


def _lgu_rows(locations):
    """{location: freshest LguData row} for the given barangays, in one
    query that returns only that row per barangay (see
    _newest_per_group), however many uploads lgu_data holds."""
    locations = list(locations)
    if not locations:
        return {}
    newest = _newest_per_group(
        (LguData,),
        partition_by=(LguData.barangay,),
        order_by=(LguData.upload_date.desc(), LguData.lgu_id.desc()),
        filters=(LguData.barangay.in_(locations), LguData.archived_at.is_(None)),
        entity=LguData,
    )
    return {row.barangay: row for row in newest.all()}


def _newest_per_group(columns, partition_by, order_by, filters, entity=None):
    """The newest row of each group, in ONE query: ROW_NUMBER() over the
    group, keep number 1. Supported by MySQL 8, MariaDB 10.2+ and SQLite
    3.25+.

    market_data and lgu_data keep every upload and every refresh -- a
    full history -- while each caller only wants the current row per
    combination. Selecting the whole history and discarding most of it
    in Python made every scoring call slower after each upload, and
    a GROUP BY + join took two round trips; this returns exactly one
    row per group in one.

    With `entity`, returns a Query of ORM objects of that model;
    otherwise a Core select of `columns`.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    rank = func.row_number().over(partition_by=partition_by, order_by=order_by).label("dss_rank")
    if entity is not None:
        inner = select(entity, rank).where(*filters).subquery()
        alias = aliased(entity, inner)
        return db.session.query(alias).filter(inner.c.dss_rank == 1)
    inner = select(*columns, rank).where(*filters).subquery()
    return select(*(inner.c[column.key] for column in columns)).where(inner.c.dss_rank == 1)


def generate_forecast_for_profile(sme_profile, defer_ai=False):
    """Persisting entry point -- see module docstring. Writes a
    ForecastResult row tied to this SmeProfile's OWN
    industry_type/location/years_in_operation, and fires an
    early-warning Notification when the result crosses the
    saturation_alert_threshold system setting.

    SUB-CATEGORY. When the plan names a sub-category (a bakery rather
    than "Food and Beverage"), the stored saturation/viability are the
    direct-competition-adjusted figures from subcategory_service; the
    industry-wide figures travel in the recommendation JSON's
    subcategory_analysis for the page to compare against. With no
    measured sub-category count the adjustment is exactly nothing.

    THE PLAN VIABILITY MODEL (stage 2). The market figures above are
    then handed, with every business parameter on the plan (capital,
    employees, stage, price list, offering, idea) and the barangay's
    real population and rent, to the trained Plan Viability Model --
    plan_forecast_service.forecast_plan(). What is STORED on the row
    is therefore:

        viability_score  -- the PLAN's viability (PVI / 10), not
                            (100 - saturation) / 10 any more. Two plans
                            in the same market now differ when their
                            capital, staffing or pricing do.
        saturation_index -- still MSI*, the market's (the early warning
                            and the cluster tier keep reading it).
        confidence_level -- the lower of the two stages' confidences.
        model_version    -- both stages, e.g. "rf_v1+plan_rf_v1".

    The full payload (inputs, financials, scorecard, the exact
    per-driver explanation) travels in the recommendation JSON under
    "forecast", where the narrator -- Gemini first, see llm_service --
    transcribes it into words without changing a figure.

    defer_ai=True (the plan pages: add, edit, re-forecast on view) stores
    the rule-based text and leaves Gemini's to the page's background
    upgrade, so the redirect after saving a plan never waits on an LLM.

    The stored recommendation is generated from the SME's OWN input
    business parameters (sub-category, offering, idea, price list,
    capital, employees, stage)
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
    from app.services.subcategory_service import adjusted_scores, direct_competition

    subcategory_analysis = direct_competition(
        sme_profile.industry_type,
        getattr(sme_profile, "subcategory", None),
        sme_profile.location,
        scores["competitor_count"],
    )
    scores = adjusted_scores(sme_profile.industry_type, sme_profile.location, scores, subcategory_analysis)
    if subcategory_analysis is not None:
        subcategory_analysis = {
            **subcategory_analysis,
            "industry_saturation_index": scores["industry_saturation_index"],
            "industry_viability_score": scores["industry_viability_score"],
            "saturation_index": scores["saturation_index"],
        }

    market_row = MarketData.query.get(scores["market_id"])
    competitor_simulated = not market_row or market_row.source != "Google Places API"
    competitor_sample = []
    if not competitor_simulated:
        api_key = (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()
        sample_results, _ = search_competitors(
            sme_profile.location, sme_profile.industry_type, api_key=api_key, max_results=5
        )
        competitor_sample = [r["name"] for r in sample_results[:5] if r.get("name")]

    from app.services.plan_forecast_service import (
        combined_confidence,
        combined_model_version,
        forecast_plan,
    )

    population = get_real_population(sme_profile.location) or 0
    plan_forecast = forecast_plan(
        sme_profile, scores, market_row, population, subcategory_analysis=subcategory_analysis,
    )
    scores = {
        **scores,
        "viability_score": plan_forecast["plan"]["viability_score"],
        "confidence_level": combined_confidence(plan_forecast),
        "model_version": combined_model_version(scores["model_version"], plan_forecast["plan"]["model_version"]),
    }

    context = build_recommendation_context(
        sme_profile,
        scores,
        competitor_sample=competitor_sample,
        competitor_simulated=competitor_simulated,
        population=population,
        subcategory_analysis=subcategory_analysis,
        plan_forecast=plan_forecast,
    )
    recommendation = build_recommendation(context, defer_ai=defer_ai)
    if not isinstance(recommendation.get("forecast"), dict):
        recommendation = {**recommendation, "forecast": plan_forecast}
    recommendation_text = serialize_recommendation(recommendation)

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

    if owner is None:
        owner = db.session.get(User, sme_profile.user_id)
    _email_early_warning(owner, forecast, sme_profile, message, notification, threshold)


def _early_warning_emailed_recently(user_id, sme_id, notification_id, hours):
    """True when this plan already raised an early warning inside the
    cooldown window -- it was e-mailed then, so it is not e-mailed again.

    Keyed on the plan, not the wording: a plan edited three times in an
    afternoon, each forecast still over the threshold, is one thing to
    tell the owner about, not three.
    """
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return db.session.query(Notification.id).join(
        ForecastResult, Notification.forecast_result_id == ForecastResult.forecast_id
    ).filter(
        Notification.user_id == user_id,
        Notification.type == "early_warning",
        Notification.id != notification_id,
        Notification.created_at >= cutoff,
        ForecastResult.sme_id == sme_id,
    ).first() is not None


def _email_early_warning(owner, forecast, sme_profile, message, notification, threshold):
    """E-mail the plan's owner the same early warning the bell shows, so
    it reaches them with MarketLens closed.

    Sent only when the owner's "Early warning alerts" switch is on (the
    caller has checked), mail is configured, the account is active and
    this plan has not already been e-mailed about inside the cooldown.
    Handed to the background sender, so the Planning page does not wait
    on the mail server. Never raises: the forecast is already saved and
    the in-app alert written, and those are what matter.
    """
    try:
        from flask import current_app
        from app.services import email_service

        if owner is None or not owner.email or not owner.is_active:
            return False
        if not email_service.is_configured():
            return False

        hours = float(current_app.config.get("EARLY_WARNING_EMAIL_COOLDOWN_HOURS", 24) or 0)
        if hours > 0 and _early_warning_emailed_recently(
                owner.user_id, sme_profile.sme_id, notification.id, hours):
            return False

        industry, location = forecast.input_industry_type, forecast.input_location
        subject = (f"MarketLens early warning: {industry} in {location} is "
                   f"{forecast.saturation_percent}% saturated")
        body = (
            f"{message}\n\n"
            f"Your plan's latest forecast came back above the {threshold:.0f}% saturation "
            f"alert threshold set for Tarlac City. Open the plan to see the full forecast, "
            f"the direct competition and the barangays with more room for {industry}."
        )
        link = email_service.app_link("sme.planning", plan=sme_profile.sme_id)
        return email_service.send_in_background(
            email_service.send_early_warning, owner.email, owner.name, subject, body, link=link)
    except Exception:
        try:
            from flask import current_app

            current_app.logger.warning("could not e-mail an early warning", exc_info=True)
        except Exception:
            pass
        return False
