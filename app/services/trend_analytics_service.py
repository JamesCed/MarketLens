"""
app/services/trend_analytics_service.py
-------------------------------------------
Builds every number the Trend Reports & Analytics page shows, from
EVERY seeded and real data source this project has -- not just
whatever ForecastResult rows happen to already exist from SMEs
manually running a forecast on the Home page (that WAS the old
/api/trend-data behaviour: a brand-new install, or one with only a
handful of plans, showed an almost-empty page, even though the app
already has a full 76-barangay reference dataset to draw on).

WHAT "RELY ON HISTORICAL / SEEDED DATA" MEANS HERE, CONCRETELY:

1. Market Saturation / Avg Viability KPI cards, and the "current"
   point of every chart, are computed across a full sweep of the
   76-barangay reference dataset (app/ml/seed_data.py) -- the exact
   same ephemeral compute_scores() call the Saturation Map already
   uses for its pins, run for every barangay x a representative set
   of industries (FEATURED_BUSINESS_TYPES, to keep a page load
   bounded -- see _sweep_baseline()). That reference dataset IS this
   project's historical baseline: it's what the AI model is trained
   on, and what the AI engine itself falls back to for any barangay
   without a real market_data/lgu_data snapshot yet. Real, persisted
   ForecastResult rows (actual SME plans someone ran) are blended in
   on top of that baseline, so the numbers move as real usage
   accumulates instead of staying frozen at a city-wide average
   forever -- but the page is never empty on a fresh install.

2. "Total Businesses" and "Industry Distribution" are REAL counts:
   every SmeProfile (a real registered business plan) plus whatever
   REAL competitor_count is already sitting in `market_data` for a
   given (industry, barangay) combo -- fetched live, straight from the
   Google Places API (New), the moment that combo is first needed by
   any page (see app/services/places_service.py and
   forecasting_service.find_or_create_market_data()). No estimate
   involved for any combo that's already been looked up.

3. The KPI cards, "Monthly Industry Trends" and "Quarterly Performance
   & Growth Rate" need a TIME SERIES, and the "Select Period" calendar
   reaches back to January 2020 -- years before this app first ran.
   Wherever real, dated market_data / ForecastResult rows exist for a
   month or quarter, those are used and nothing is estimated. Every
   earlier month is BACK-PROJECTED by
   app/services/historical_baseline_service.py: this app's own real
   current counts, scaled along the Philippines' published national MSME
   establishment series (DTI / PSA), so the history carries the real 2020
   contraction and 2021 rebound rather than a smooth invented curve.

   Google Places cannot supply those months -- it answers "what is there
   now" and has no historical endpoint of any kind -- so they are
   modelled, and they say so: `measured: False` on the card,
   `projected: True` on the chart point, "(estimated)" on screen, and a
   note naming the national figure behind that month. NOTHING is written
   to the database; the projection is computed on the fly, and real
   readings replace it automatically as they accumulate.

ONE report, shared by every role. SME, LGU and Admin accounts all
see the same city-wide analytics dashboard (build_trend_report()).
An earlier version gave SMEs a separate per-plan "My Trend Report"
instead; it was removed, because a chart drawn from one user's one
or two saved plans is not a market trend -- it is a restatement of
their own inputs. The paper's Figure 4 / 4.1 describes this page as
market indicators, quarterly performance and industry growth
distribution, all city-wide, which is what every role now gets. An
SME's own plan-specific numbers live on Home and Recommendations.
"""

import calendar
import hashlib
import json
import os
import time
from datetime import date

from app.extensions import db
from app.models import SmeProfile, ForecastResult, MarketData
from app.ml.constants import BUSINESS_TYPES, FEATURED_BUSINESS_TYPES, DETAIL_PANEL_SECTIONS
from app.ml.seed_data import BARANGAY_NAMES, get_barangay_profile, get_real_population
from app.services.forecasting_service import compute_scores, compute_scores_batch
from app.services.recommendation_service import parse_recommendation
from app.services.historical_baseline_service import (
    EARLIEST_HISTORY,
    POST_RECOVERY_GROWTH,
    growth_index,
    national_context,
    project_businesses,
    project_saturation,
)

# Bounds a full-city sweep to FEATURED_BUSINESS_TYPES (8) x 76 barangays
# = 608 ephemeral AI scores -- the same compute_scores() the Saturation
# Map already calls per-barangay. find_or_create_market_data/
# find_or_create_lgu_data cache their snapshots for 30 days, so repeat
# page loads reuse those rows; only the trained model's predict() step
# re-runs every time (fast: a single-row Random Forest inference).
_MONTHS_BACK = 6
_QUARTERS_BACK = 5


# The city-wide sweep now covers EVERY industry (24), not just the 8
# featured ones, because the Trend Reports page charts them all. To keep
# the page load roughly where it was, it samples barangays instead of
# visiting all 76: 24 industries x 25 barangays = 600 ephemeral scores,
# versus the 8 x 76 = 608 it used to run. Same cost, full industry
# coverage.
_SWEEP_BARANGAY_SAMPLE = 25


# ---------------------------------------------------------------------
# CACHING -- why changing the Select Period month is fast
# ---------------------------------------------------------------------
# Picking a different month re-asks for every card and chart. Almost
# none of that work actually depends on the month: the city-wide AI
# sweep (600 compute_scores() calls) and the market_data scan produce
# exactly the same thing in March as in August -- only the arithmetic
# laid over them moves. Re-running them per date change was the reason
# the page took seconds to answer a click.
#
# So both are memoised, keyed on a FINGERPRINT of the data they were
# built from, not on a timer alone. If a single market_data row is
# added, changed or re-fetched, the fingerprint changes and the cache
# misses -- there is no window in which the page can show numbers that
# no longer match the database. The TTL is a second safety net for
# anything the fingerprint cannot see (a retrained model, an edited
# reference dataset), not the primary invalidation.
_CACHE_TTL_SECONDS = 300
_SWEEP_CACHE = {}
_SNAPSHOT_CACHE = {}
_CACHE_MAX_ENTRIES = 32

# ...AND WHY THE SAME SWEEP IS ALSO KEPT ON DISK.
#
# The memo above lives in one Python process. Restart the server, or let
# a second worker pick up the request, and the first visit to Trend
# Reports pays for the whole city-wide sweep again -- roughly 14 CPU
# seconds of Random Forest inference before a single pixel is drawn.
# That is the "Building city-wide trend report..." wait.
#
# So the finished sweep is also written to instance/, keyed on the SAME
# market_data fingerprint as the in-memory copy. A new worker reads the
# file instead of recomputing, which turns those 14 seconds into
# essentially zero. If any market_data row moves, the fingerprint moves
# with it, the file no longer matches and the sweep is recomputed --
# there is no window in which the page can show numbers the database
# disagrees with.
#
# Every number in the file was produced by the trained model; this
# caches the model's OUTPUT, it does not replace the model with a lookup
# table. Delete the file and the next request rebuilds it.
_SWEEP_DISK_CACHE_NAME = "trend_sweep_cache"
_SWEEP_DISK_CACHE_VERSION = 1


def _data_fingerprint():
    """Identifies the exact state of `market_data` in one cheap query:
    how many rows, the newest row id, the newest recorded date and the
    total competitor count. Any insert, delete or edit moves at least
    one of those.

    The engine's identity is part of the key as well, because each test
    builds its own throw-away database and two of them can easily hold
    the same row COUNT -- without this, one test could read another
    test's cached sweep.
    """
    try:
        engine_token = id(db.engine)
    except Exception:  # pragma: no cover -- no app context / no engine
        engine_token = 0
    count, max_id, max_date, total = db.session.query(
        db.func.count(MarketData.market_id),
        db.func.max(MarketData.market_id),
        db.func.max(MarketData.date_recorded),
        db.func.sum(MarketData.competitor_count),
    ).one()
    return (
        engine_token,
        int(count or 0),
        int(max_id or 0),
        max_date.isoformat() if max_date else None,
        int(total or 0),
    )


def _cached(store, name, build):
    """Memoise `build()` under (`name`, current data fingerprint)."""
    version = _data_fingerprint()
    key = (name, version)
    hit = store.get(key)
    if hit is not None and (time.monotonic() - hit[0]) <= _CACHE_TTL_SECONDS:
        return hit[1]

    value = build()
    now = time.monotonic()
    store[key] = (now, value)

    # Building may itself have written rows: compute_scores() persists
    # the Places lookups it makes into market_data. That moves the
    # fingerprint, so file the result under the NEW one too -- otherwise
    # the very next request misses and rebuilds something that has not
    # actually changed, and the cache never warms up on a fresh install.
    after = _data_fingerprint()
    if after != version:
        store[(name, after)] = (now, value)

    if len(store) > _CACHE_MAX_ENTRIES:
        for stale in sorted(store, key=lambda k: store[k][0])[: _CACHE_MAX_ENTRIES // 2]:
            store.pop(stale, None)
    return value


def clear_trend_caches(drop_disk=True):
    """Drop everything memoised above, including the on-disk sweep.
    Called after a bulk Google Places refresh, and available to any code
    that rewrites market_data in a way the fingerprint might not notice.

    The disk copy goes too: leaving a stale file behind after an
    explicit "forget everything" would be the one case the fingerprint
    cannot cover, because the caller is telling us the data changed in a
    way it cannot see."""
    _SWEEP_CACHE.clear()
    _SNAPSHOT_CACHE.clear()
    if not drop_disk:
        return
    path = _sweep_disk_cache_path()
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _baseline_barangays():
    """A deterministic, STRATIFIED sample of barangays for the city-wide
    sweep: sort all 76 by real population, then take evenly-spaced
    entries across that ordering.

    Evenly spaced rather than "the biggest N" on purpose -- taking only
    the most populous barangays would sample just the built-up core and
    bias every city-wide average upward. Spreading the picks across the
    population range keeps dense and sparse barangays both represented.
    Deterministic, so the same numbers come back on every refresh.
    """
    ordered = sorted(BARANGAY_NAMES, key=lambda b: (-(get_real_population(b) or 0), b))
    if len(ordered) <= _SWEEP_BARANGAY_SAMPLE:
        return ordered
    step = len(ordered) / float(_SWEEP_BARANGAY_SAMPLE)
    return [ordered[int(i * step)] for i in range(_SWEEP_BARANGAY_SAMPLE)]


def _sweep_disk_cache_path():
    """Where the on-disk sweep lives: inside instance/, which is already
    gitignored and writable wherever the app can run.

    The filename carries a short hash of the DATABASE URI, so two
    databases served from the same checkout (dev vs. a copy, MySQL vs. a
    SQLite fallback) can never read each other's sweep. The in-memory
    fingerprint includes the engine's identity for exactly that reason,
    but a memory address cannot survive a restart, so it is dropped from
    the file and the database's name takes its place.

    Returns None -- meaning "don't use a disk cache at all" -- when
    there is no app context, or when the database lives in memory. An
    in-memory database dies with the process, so a file outliving it
    could only ever describe data that no longer exists; the tests run
    that way, which is also why they never write into instance/.
    """
    try:
        from flask import current_app, has_app_context

        if not has_app_context():
            return None
        uri = str(current_app.config.get("SQLALCHEMY_DATABASE_URI") or "")
        if not uri or ":memory:" in uri or uri.endswith("sqlite://"):
            return None
        token = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:12]
        folder = current_app.instance_path
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{_SWEEP_DISK_CACHE_NAME}.{token}.json")
    except (ImportError, RuntimeError, OSError):  # pragma: no cover
        return None


def _load_sweep_from_disk(cache_key, version):
    """The previously-computed sweep for this exact fingerprint, or None.

    Never raises: a missing, truncated, unreadable or stale file just
    means "recompute", which is always the correct answer.
    """
    path = _sweep_disk_cache_path()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None

    if payload.get("format") != _SWEEP_DISK_CACHE_VERSION:
        return None
    # The engine id is part of the in-memory fingerprint and is a memory
    # address, so it differs between processes and must NOT be compared
    # across a restart -- it is dropped here. Everything else in the
    # fingerprint is real data and is compared in full.
    if payload.get("fingerprint") != list(version[1:]):
        return None
    if payload.get("key") != list(cache_key):
        return None
    rows = payload.get("rows")
    return rows if isinstance(rows, list) and rows else None


def _plain(value):
    """A JSON-safe copy of one score value, with its TYPE preserved.

    compute_scores() runs numbers through the Random Forest, and numpy
    hands back np.float32/np.int64 rather than Python floats. json
    cannot write those, and the obvious escape hatch -- default=str --
    would quietly turn 73.4 into "73.4", so the cache would reload
    strings where the page expects numbers and every average built on
    them would break. Converting properly here is the difference
    between a cache and a corruption.

    Raises TypeError for anything that is not a number, string, bool or
    None, which _save_sweep_to_disk turns into "don't cache this" --
    recomputing is always safe, writing something unreadable is not.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # numpy scalars, Decimal, and anything else exposing the numeric
    # protocol: keep it a number, do not stringify it.
    if hasattr(value, "item"):  # numpy scalar
        return _plain(value.item())
    if isinstance(value, (date,)):
        return value.isoformat()
    raise TypeError(f"not cacheable: {type(value).__name__}")


def _save_sweep_to_disk(cache_key, version, rows):
    """Best-effort. A read-only or full disk must not turn a caching
    optimisation into a 500, so every failure is swallowed -- the app
    simply recomputes next time."""
    path = _sweep_disk_cache_path()
    if not path or not rows:
        return
    try:
        clean = [{k: _plain(v) for k, v in row.items()} for row in rows]
    except (TypeError, AttributeError):
        return
    payload = {
        "format": _SWEEP_DISK_CACHE_VERSION,
        "key": list(cache_key),
        "fingerprint": list(version[1:]),
        "written_at": date.today().isoformat(),
        "rows": clean,
    }
    try:
        # Write-then-rename, so a worker reading the file never catches
        # it half-written.
        temp = f"{path}.{os.getpid()}.tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(temp, path)
    except (OSError, TypeError, ValueError):
        pass


def _sweep_baseline(industry_type=None):
    """Ephemeral AI score for every (industry, sampled barangay) pair --
    the "historical / seeded" baseline this module's docstring
    describes. Returns a flat list of compute_scores() dicts, computed
    ONCE per request and reused by every stat below.

    Filtering to one industry sweeps ALL 76 barangays for it (that is
    cheap: 76 calls); the unfiltered city-wide view sweeps every
    industry across the sampled barangays -- see _baseline_barangays().

    CACHED IN TWO PLACES, both keyed on the market_data fingerprint:
    in-process (see _cached) and on disk (see _load_sweep_from_disk).
    This sweep is what the Select Period control used to re-run on every
    date change, which it never needed to -- the sweep describes the
    market as it stands NOW, and the chosen month only changes the
    arithmetic applied to it afterwards. The disk layer exists because
    the in-process memo dies with the worker, and recomputing is ~14 CPU
    seconds the visitor spends watching a progress bar.
    """
    cache_key = ("sweep", industry_type)

    def build():
        version = _data_fingerprint()
        from_disk = _load_sweep_from_disk(cache_key, version)
        if from_disk is not None:
            return from_disk

        # One batched call, not 500 separate ones. See
        # forecasting_service.compute_scores_batch: it resolves every
        # market/lgu row in two GROUP BY queries instead of ~1,000
        # round trips, and runs the forest once over the whole matrix
        # instead of once per pair. Same numbers, same order.
        if industry_type:
            pairs = [(industry_type, barangay) for barangay in BARANGAY_NAMES]
        else:
            barangays = _baseline_barangays()
            pairs = [(industry, barangay)
                     for industry in BUSINESS_TYPES
                     for barangay in barangays]
        rows = compute_scores_batch(pairs)

        # compute_scores() can itself write market_data rows, which moves
        # the fingerprint -- so file the result under the fingerprint as
        # it stands AFTER the sweep, which is what the next request will
        # be holding.
        _save_sweep_to_disk(cache_key, _data_fingerprint(), rows)
        return rows

    return _cached(_SWEEP_CACHE, cache_key, build)


def _market_viability_of(forecast):
    """A stored forecast's MARKET viability, (100 - saturation) / 10.

    Trend Reports describe markets -- the baseline sweep they blend
    forecasts with is stage 1 only -- so a forecast enters their
    averages through its market figure. forecast_result.viability_score
    used to be exactly this number; since the Plan Viability Model it is
    the PLAN's viability (capital, staff, prices... weighed in), which
    belongs on the plan's own pages, not averaged into a city's market
    trend next to market-only baseline figures. Recomputing it from the
    stored saturation_index gives every row -- old or new -- the same
    value the Trend page always used."""
    return round(max(0.0, min(10.0, (100.0 - float(forecast.saturation_index or 0)) / 10.0)), 1)


def live_plan_forecasts():
    """ForecastResult query limited to plans that are NOT in Trash.

    A plan moved to Trash keeps its forecasts (restore brings them back),
    and forecast_result has no archive stamp of its own, so the global
    archive filter (app/models/archive.py) can only drop them where
    SmeProfile is part of the query. Joining it here is what makes every
    count and average built on this a count of LIVE plans' forecasts:
    without it, a trashed plan went on feeding the LGU dashboard's
    tallies and the trend averages. sme_id is NOT NULL with a foreign
    key, so when nothing is in Trash the join drops no row and every
    figure is exactly what it was.

    Filter it with filter(ForecastResult.<column> == ...), not
    filter_by(): after a join, filter_by() reads its keywords off the
    JOINED entity (SmeProfile), which has no forecast columns."""
    return ForecastResult.query.join(SmeProfile, ForecastResult.sme_id == SmeProfile.sme_id)


def _real_forecasts(industry_type=None):
    query = live_plan_forecasts()
    if industry_type:
        query = query.filter(ForecastResult.input_industry_type == industry_type)
    return query.order_by(ForecastResult.forecast_date.asc()).all()


def month_end(anchor):
    """Last day of `anchor`'s month -- the cut-off every "as of this
    month" query below compares against."""
    last_day = calendar.monthrange(anchor.year, anchor.month)[1]
    return date(anchor.year, anchor.month, last_day)


def previous_month(anchor):
    first = anchor.replace(day=1)
    prev_month = first.month - 1 or 12
    prev_year = first.year - 1 if first.month == 1 else first.year
    return date(prev_year, prev_month, 1)


def _last_n_month_dates(n, as_of=None):
    """The `n` months ending AT `as_of` (default: this month), oldest
    first, as real dates -- so picking March 2026 charts the months up to
    March 2026, not the months up to today."""
    months = []
    cursor = (as_of or date.today()).replace(day=1)
    for _ in range(n):
        months.append(cursor)
        cursor = previous_month(cursor)
    months.reverse()
    return months


def _last_n_month_labels(n, as_of=None):
    """Labels for those months, WITH the year.

    The year is not decoration: the period picker now reaches back to
    2020, so a six-month window can straddle a year boundary and a bare
    "Jan" would be genuinely ambiguous about which January it is."""
    return [m.strftime("%b %Y") for m in _last_n_month_dates(n, as_of=as_of)]


def _industry_baseline_average(baseline, industry_type, field):
    values = [row[field] for row in baseline if row["industry_type"] == industry_type]
    return sum(values) / len(values) if values else 0.0


def latest_market_data_by_key():
    """Returns {(industry_type, location): MarketData row} using only
    the FRESHEST snapshot for each combo. market_data keeps one row per
    refresh (a full history over time), so naively summing every row in
    the table would double/triple-count the same barangay+industry
    every time it's re-seeded -- this collapses it back down to "one
    number per (industry, location), right now".

    RESOLVED IN SQL, NOT IN PYTHON. This used to load every row in
    market_data as an ORM object and throw most of them away. That
    table grows by one row per (industry, location) per refresh, so
    its size tracks how often the app has been used, while the answer
    is always at most 1,520 rows -- one per combo. On a 512 MB
    instance, materialising an unbounded history to produce a bounded
    result is the wrong end of the trade, and it got worse every week.

    The GROUP BY below asks the database for the winning market_id per
    combo (newest date; highest id among same-date ties, which is the
    ordering the old Python loop implemented) and then loads only
    those rows.

    COMPETITOR COUNTS COME BACK RECONCILED, not raw.

    Every figure on Trend Reports and the LGU dashboard is read from
    here, and they have to agree with what the model scored. Without
    this they would not: uploading a permit register makes the DTI row
    the freshest one for that combo, so "newest wins" would show the
    permit count on the trend page while the engine scored the larger
    reconciled figure -- one system quoting two different competitor
    counts for the same barangay on two different pages.

    The row is WRAPPED rather than edited (see _ReconciledRow): these
    are live ORM objects, and assigning to one would mark the session
    dirty and persist a derived number as though it had been measured.
    """
    from app.services.forecasting_service import (
        _latest_market_rows, reconciled_competitor_counts,
    )

    industries = [row[0] for row in db.session.query(MarketData.industry_type).distinct().all()]
    locations = [row[0] for row in db.session.query(MarketData.location).distinct().all()]
    if not industries or not locations:
        return {}

    rows = _latest_market_rows(industries, locations)
    merged = reconciled_competitor_counts(industries, locations, with_source=True)
    return {
        key: _ReconciledRow(row, *merged[key]) if key in merged else row
        for key, row in rows.items()
    }


class _ReconciledRow:
    """A read-only view of a MarketData row whose competitor_count is
    the cross-source reconciled figure.

    Everything else passes straight through to the real row, so a
    caller reading .location, .source or .date_recorded sees exactly
    what it saw before. Two attributes are added: `raw_competitor_count`
    (what this particular row measured) and `competitor_source` (which
    source supplied the winning number), because the LGU dashboard
    reports how much of a barangay's count is confirmed by a live
    Google lookup and must not credit Places for a figure the permit
    register supplied.

    Deliberately not a subclass and deliberately not writable: it must
    be impossible to add one of these to a session by accident.
    """

    __slots__ = ("_row", "competitor_count", "competitor_source")

    def __init__(self, row, competitor_count, competitor_source):
        object.__setattr__(self, "_row", row)
        object.__setattr__(self, "competitor_count", competitor_count)
        object.__setattr__(self, "competitor_source", competitor_source)

    def __getattr__(self, name):
        return getattr(self._row, name)

    def __setattr__(self, name, value):
        raise AttributeError(
            "reconciled market rows are read-only -- write to the underlying "
            "MarketData row if a measured figure really needs to change"
        )

    @property
    def raw_competitor_count(self):
        return self._row.competitor_count


def get_overview_stats(baseline, real_forecasts):
    """The 4 KPI cards. Total Businesses / New Startups are REAL counts;
    Market Saturation / Avg Viability blend the full-city baseline
    sweep with any real forecasts already on file.

    TOTAL BUSINESSES counts EVERY business this system has on file
    across Tarlac City -- that is, the freshest competitor_count for
    each (industry, barangay) combo in `market_data`, WHATEVER its
    source, plus each registered SME business plan.

    It used to count only rows whose source was exactly "Google Places
    API", which meant a database holding thousands of market_data rows
    still displayed "1" until a live Places lookup happened to run.
    That was the wrong denominator: market_data IS the city's business
    data, and a row sourced from a PSA/DTI upload or from the reference
    dataset is still a real count of businesses on file. The Places
    rows are still tracked separately (`from_places_api` below) so the
    UI can always show how much of the total is live Google data.
    """
    latest_market = latest_market_data_by_key()

    places_total = 0
    other_market_total = 0
    for row in latest_market.values():
        count = int(row.competitor_count or 0)
        if row.source == "Google Places API":
            places_total += count
        else:
            other_market_total += count

    registered_plans = SmeProfile.query.count()
    total_businesses = places_total + other_market_total + registered_plans
    new_startups = SmeProfile.query.filter_by(business_stage="startup").count()

    blended_saturation = [s["saturation_index"] for s in baseline] + [
        float(r.saturation_index or 0) for r in real_forecasts
    ]
    blended_viability = [s["viability_score"] for s in baseline] + [
        _market_viability_of(r) for r in real_forecasts
    ]

    return {
        "total_businesses": total_businesses,
        # Provenance breakdown of the number above -- lets the page state
        # plainly how much of the total is live Google Places data vs.
        # market_data already on file vs. registered SME plans.
        "businesses_from_places_api": places_total,
        "businesses_from_market_data": other_market_total,
        "businesses_registered_plans": registered_plans,
        "market_combos_total": len(latest_market),
        "market_combos_from_places_api": len(
            [r for r in latest_market.values() if r.source == "Google Places API"]
        ),
        "new_startups": new_startups,
        "avg_saturation_percent": round(sum(blended_saturation) / len(blended_saturation), 1)
        if blended_saturation
        else 0,
        "avg_viability": round(sum(blended_viability) / len(blended_viability), 1) if blended_viability else 0,
        "forecasts_run": len(real_forecasts),
        "saturated_zones": len([s for s in baseline if s["cluster_label"] == "Saturated"])
        + len([r for r in real_forecasts if r.cluster_label == "Saturated"]),
    }


def _snapshot_rows(industry_type=None):
    """Every market_data row as a plain (date, industry, location, count)
    tuple, oldest first -- read ONCE per fingerprint and reused by the
    KPI cards, the quarterly chart and the "as of" series.

    Those three each used to run their own full-table scan, and the KPI
    cards ran two (this month and last month). Plain tuples rather than
    ORM objects so a cached list holds no session state.
    """

    def build():
        query = MarketData.query
        if industry_type:
            query = query.filter_by(industry_type=industry_type)
        rows = query.order_by(MarketData.date_recorded.asc(), MarketData.market_id.asc()).all()
        return [
            (r.date_recorded, r.industry_type, r.location, int(r.competitor_count or 0))
            for r in rows
        ]

    return _cached(_SNAPSHOT_CACHE, ("snapshot", industry_type), build)


def _snapshot_total_as_of(rows, cut_off):
    """(total, combos) from the freshest row per (industry, location)
    dated on or before `cut_off`. combos == 0 means this month has no
    measurement behind it at all."""
    snapshot = {}
    for recorded, industry, location, count in rows:
        if recorded and recorded <= cut_off:
            snapshot[(industry, location)] = count
    return sum(snapshot.values()), len(snapshot)


def _current_market_total(rows):
    """Today's REAL total: the freshest row per (industry, location),
    whatever its date, plus the date that total effectively speaks for."""
    snapshot = {}
    newest = None
    for recorded, industry, location, count in rows:
        snapshot[(industry, location)] = count
        if recorded and (newest is None or recorded > newest):
            newest = recorded
    return sum(snapshot.values()), len(snapshot), (newest or date.today())


def businesses_as_of(anchor, industry_type=None, rows=None):
    """How many businesses this system says were operating at the end of
    `anchor`'s month.

    MEASURED where it can be. `market_data` keeps one row per refresh,
    so any month at or after this app's first data collection is answered
    from real dated snapshots and nothing is projected.

    BACK-PROJECTED before that, because the alternative was returning
    zero and printing "no comparison available" on every card for every
    month of 2020-2025 -- which is what the page was doing. The
    projection is not an invented curve: it takes this app's OWN REAL
    current count and scales it along the Philippines' published national
    MSME establishment series to where that month sat on it. See
    app/services/historical_baseline_service.py for the figures, the
    sourcing, and a plain statement of what is assumed. Nothing is
    written to the database; `measured` is False on every projected
    month so the UI can say so.

    Returns {"value", "measured", "projected", "combos"}.
    """
    rows = _snapshot_rows(industry_type) if rows is None else rows
    cut_off = month_end(anchor)

    total, combos = _snapshot_total_as_of(rows, cut_off)
    if combos:
        return {"value": total, "measured": True, "projected": False, "combos": combos}

    current_total, current_combos, reference = _current_market_total(rows)
    if not current_total:
        # Nothing real to scale. An honest zero beats a projection with
        # nothing behind it.
        return {"value": 0, "measured": False, "projected": False, "combos": 0}

    return {
        "value": project_businesses(current_total, anchor, reference=reference, series_key=industry_type),
        "measured": False,
        "projected": True,
        "combos": current_combos,
    }


def _plan_first_seen():
    """{sme_id: earliest forecast date} -- run ONCE and shared by every
    "as of this month" plan count below, instead of the same GROUP BY
    four times per page."""
    query = db.session.query(ForecastResult.sme_id, db.func.min(ForecastResult.forecast_date))
    return {
        sme_id: first
        for sme_id, first in query.group_by(ForecastResult.sme_id).all()
        if first is not None
    }


def _plans_as_of(cut_off, startup_only=False, first_seen=None):
    """How many SME business plans existed by `cut_off`.

    sme_profile has no created_at column, so a plan is dated by the
    EARLIEST forecast run against it (forecast_result.forecast_date is
    real and dated). A plan that has never been forecast can only be
    counted in the current month, since nothing in the schema says when
    it was created -- guessing would be inventing history."""
    dated = _plan_first_seen() if first_seen is None else first_seen
    dated_ids = {sme_id for sme_id, first in dated.items() if first <= cut_off}

    if cut_off >= date.today():
        # Current month: include plans with no forecast history too.
        profiles = SmeProfile.query
        if startup_only:
            profiles = profiles.filter_by(business_stage="startup")
        return profiles.count()

    if not dated_ids:
        return 0
    profiles = SmeProfile.query.filter(SmeProfile.sme_id.in_(tuple(dated_ids)))
    if startup_only:
        profiles = profiles.filter_by(business_stage="startup")
    return profiles.count()


def _plans_registered_in(anchor, first_seen=None):
    """SME plans whose FIRST forecast falls inside `anchor`'s month --
    i.e. business plans this app itself saw start up that month."""
    dated = _plan_first_seen() if first_seen is None else first_seen
    start, end = anchor.replace(day=1), month_end(anchor)
    ids = {sme_id for sme_id, first in dated.items() if start <= first <= end}
    if not ids:
        return 0
    return SmeProfile.query.filter(SmeProfile.sme_id.in_(tuple(ids))).count()


def _forecast_averages_in_month(anchor, industry_type=None):
    """(avg saturation %, avg viability, n) from REAL forecast_result
    rows dated inside `anchor`'s month. Returns n=0 when that month has
    no forecasts -- the caller then falls back to the model baseline
    and says so, rather than drawing a line through empty months."""
    start = anchor.replace(day=1)
    end = month_end(anchor)
    # Live plans only -- see live_plan_forecasts().
    query = live_plan_forecasts().filter(
        ForecastResult.forecast_date >= start, ForecastResult.forecast_date <= end
    )
    if industry_type:
        query = query.filter(ForecastResult.input_industry_type == industry_type)
    rows = query.all()
    if not rows:
        return 0.0, 0.0, 0
    saturation = [float(r.saturation_index or 0) for r in rows]
    viability = [_market_viability_of(r) for r in rows]
    return (
        round(sum(saturation) / len(saturation), 1),
        round(sum(viability) / len(viability), 1),
        len(rows),
    )


def _delta(current, previous, as_percent=True, ndigits=1):
    """Month-over-month change. None when there is no previous figure to
    compare against -- the card then shows no badge instead of a
    fabricated "+0%".

    The two modes differ in a way that matters more than it looks. A
    PERCENT change needs a non-zero base, so `previous == 0` genuinely
    has no answer and returns None. An ABSOLUTE change does not: zero
    last month and zero this month is a real, known "no change", and
    returning None for it would print "no comparison available" on a card
    whose two figures are both perfectly well known. Conflating "zero"
    with "missing" is exactly what blanked the New Startups card in the
    months when the market was contracting.
    """
    if previous is None or current is None:
        return None
    if as_percent:
        if not previous:
            return None
        return round(((current - previous) / float(previous)) * 100.0, 1)
    return round(current - previous, ndigits)


def get_period_overview(as_of, baseline=None, real_forecasts=None, industry_type=None):
    """The four KPI cards of the paper's Figure 4 -- Total Businesses,
    New Startups, Market Saturation, Avg. Viability -- computed AS OF
    the end of a chosen month, each with its month-over-month change.

    This is the part the "Select Period" calendar drives.

    WHY THE CARDS USED TO SAY "NO COMPARISON AVAILABLE"
    Every figure here came from a dated row, and this app only has rows
    from the day it was first run. So the current month had a value, the
    month before it had nothing, `_delta()` returned None, and all four
    cards printed "no comparison available" -- for every month anyone
    could pick.

    WHAT THEY DO NOW
    A month with real dated rows is still measured from those rows and
    nothing else; that has not changed, and `measured: True` still means
    exactly what it did. A month from before this app existed is
    BACK-PROJECTED from the published national MSME establishment series
    (see historical_baseline_service.py), so 2020-2026 has a real,
    grounded shape to compare against -- including the pandemic dip --
    and the card carries `measured: False` so the page says "(estimated)"
    rather than passing a projection off as a reading.

    Where a month has neither a measurement nor anything real to project
    from (a genuinely empty database), the delta is still None and the
    card still says so. The fix removes the false blanks, not the honest
    ones.

    NEW STARTUPS is market entry, not this app's sign-up log. The count
    is how many businesses the city gained that month -- the increase in
    businesses on file -- plus any SME plan first forecast that month.
    Counting only registered plans gave a number with no history behind
    it at all (sme_profile has no created_at), so that card could never
    show a change for any month, ever.
    """
    cut_off = month_end(as_of)
    prev_anchor = previous_month(as_of)
    before_prev = previous_month(prev_anchor)
    prev_cut_off = month_end(prev_anchor)

    rows = _snapshot_rows(industry_type)
    first_seen = _plan_first_seen()

    market_now = businesses_as_of(as_of, industry_type, rows=rows)
    market_prev = businesses_as_of(prev_anchor, industry_type, rows=rows)
    market_before = businesses_as_of(before_prev, industry_type, rows=rows)

    plans_now = _plans_as_of(cut_off, first_seen=first_seen)
    plans_prev = _plans_as_of(prev_cut_off, first_seen=first_seen)

    businesses_now = market_now["value"] + plans_now
    businesses_prev = market_prev["value"] + plans_prev

    # New entrants = the month's net gain in businesses on file, plus the
    # SME plans this app itself saw start that month.
    entrants_now = max(0, market_now["value"] - market_prev["value"]) + _plans_registered_in(
        as_of, first_seen=first_seen
    )
    entrants_prev = max(0, market_prev["value"] - market_before["value"]) + _plans_registered_in(
        prev_anchor, first_seen=first_seen
    )
    # ...but only where the months behind it are known. On an empty
    # database every month is zero for lack of data, not because nothing
    # opened, and "0, unchanged from last month" would assert a
    # measurement that was never taken. That case still gets the blank.
    entrants_known = market_now["measured"] or market_now["projected"]

    sat_now, via_now, n_now = _forecast_averages_in_month(as_of, industry_type)
    sat_prev, via_prev, n_prev = _forecast_averages_in_month(prev_anchor, industry_type)

    # Saturation / viability for a month with no forecasts of its own.
    # The model's CURRENT city-wide average is the level; the national
    # establishment curve supplies the movement, because a month when
    # materially fewer businesses were trading was, all else equal, a
    # less crowded market. Both months are projected from the SAME
    # reference so the delta compares like with like instead of setting
    # a measured month against a modelled one.
    saturation_measured = n_now > 0
    viability_measured = n_now > 0
    baseline_sat = baseline_via = None
    if baseline:
        baseline_sat = round(sum(s["saturation_index"] for s in baseline) / len(baseline), 1)
        baseline_via = round(sum(s["viability_score"] for s in baseline) / len(baseline), 1)

    reference_month = date.today().replace(day=1)
    if not saturation_measured and baseline_sat is not None:
        sat_now = project_saturation(baseline_sat, as_of, reference=reference_month, series_key=industry_type)
        via_now = _viability_from_saturation_shift(baseline_via, baseline_sat, sat_now)

    if n_prev == 0:
        anchor_sat = sat_now if sat_now else baseline_sat
        anchor_via = via_now if via_now else baseline_via
        if anchor_sat:
            sat_prev = project_saturation(
                anchor_sat, prev_anchor, reference=as_of, series_key=industry_type
            )
            via_prev = _viability_from_saturation_shift(anchor_via, anchor_sat, sat_prev)
        else:
            sat_prev = via_prev = None

    return {
        "as_of": cut_off.isoformat(),
        "as_of_label": as_of.strftime("%B %Y"),
        "previous_label": prev_anchor.strftime("%B %Y"),
        "cards": {
            "total_businesses": {
                "value": businesses_now,
                "delta_percent": _delta(businesses_now, businesses_prev or None),
                "measured": market_now["measured"],
                "projected": market_now["projected"],
                "higher_is_better": True,
            },
            "new_startups": {
                # An ABSOLUTE change, not a percentage. New entries in a
                # month is a small count that legitimately hits zero --
                # in the first half of 2020 it hit zero for six months
                # straight, because the national series says the country
                # was losing establishments. A percentage change has no
                # answer from a base of zero, so the card would have gone
                # back to "no comparison available" for exactly the
                # months worth looking at. "3 fewer than last month" has
                # an answer whatever the two figures are.
                "value": entrants_now,
                "delta_points": _delta(entrants_now, entrants_prev, as_percent=False, ndigits=0)
                if entrants_known
                else None,
                "measured": market_now["measured"] and market_prev["measured"],
                "projected": market_now["projected"] or market_prev["projected"],
                "higher_is_better": True,
            },
            "market_saturation": {
                # Displayed to one decimal; the delta is computed from
                # the unrounded pair, for the reason given on viability.
                "value": round(sat_now, 1) if sat_now is not None else sat_now,
                "delta_points": _delta(sat_now, sat_prev, as_percent=False),
                "measured": saturation_measured,
                "projected": not saturation_measured and baseline_sat is not None,
                "higher_is_better": False,
            },
            "avg_viability": {
                # Displayed to one decimal (it is an index out of 10);
                # the delta below is computed from the unrounded pair.
                "value": round(via_now, 1) if via_now is not None else via_now,
                # Two decimals, because viability is a 0-10 index and
                # saturation is a 0-100 one: a month that moves
                # saturation by 0.2 points moves viability by 0.02, which
                # rounds to a flat "+0.0" at one decimal. The movement is
                # small, but it is real, and reporting it as zero would
                # hide a genuine change rather than an absent one.
                "delta_points": _delta(via_now, via_prev, as_percent=False, ndigits=2),
                "measured": viability_measured,
                "projected": not viability_measured and baseline_via is not None,
                "higher_is_better": True,
            },
        },
        "forecasts_in_month": n_now,
        # What the projected months are grounded in, so the page can name
        # its source on screen instead of asking anyone to take it on
        # trust. See historical_baseline_service.national_context().
        "basis": national_context(as_of),
    }


def _viability_from_saturation_shift(current_viability, current_saturation, projected_saturation):
    """Move a viability score by exactly the saturation points the
    projection moved it, on the forecasting engine's own scale
    (viability = (100 - saturation) / 10). Keeps the two cards
    consistent with each other and with app/ml/train_model.py, instead
    of projecting them independently and letting them drift apart."""
    if current_viability is None or current_saturation is None or projected_saturation is None:
        return current_viability
    shifted = current_viability + (current_saturation - projected_saturation) / 10.0
    # Two decimals, not one: the card DISPLAYS one, but the delta between
    # two consecutive months is a hundredths-scale number, and rounding
    # here first would flatten every month-over-month change to zero
    # before it ever reached the card.
    return round(max(0.0, min(10.0, shifted)), 2)


def get_industry_distribution(latest_market=None):
    """Distribution, across industries, of EVERY business on file
    city-wide: the freshest competitor_count for each
    (industry, barangay) combo in `market_data` -- live Google Places
    rows AND rows already on file from any other source -- plus each
    registered SME business plan.

    This previously counted ONLY rows whose source was exactly
    "Google Places API". On a database where no live lookup had run
    yet, that collapsed the entire pie to whichever single industry an
    SME had registered a plan in -- one slice at 100%, which is what
    the chart was showing. Every source is counted now, and each slice
    still carries its own `from_places_api` / `from_market_data`
    split so nothing about where a number came from is hidden.
    """
    counts = {}

    def _bump(industry, key, amount):
        if amount <= 0:
            return
        entry = counts.setdefault(
            industry, {"count": 0, "from_places_api": 0, "from_market_data": 0, "registered_plans": 0}
        )
        entry["count"] += amount
        entry[key] += amount

    for row in SmeProfile.query.with_entities(SmeProfile.industry_type).all():
        _bump(row[0], "registered_plans", 1)

    latest_market = latest_market if latest_market is not None else latest_market_data_by_key()
    for (industry_type, _location), row in latest_market.items():
        bucket = "from_places_api" if row.source == "Google Places API" else "from_market_data"
        _bump(industry_type, bucket, int(row.competitor_count or 0))

    total = sum(entry["count"] for entry in counts.values())
    if not total:
        return []
    return sorted(
        [
            {
                "name": name,
                "count": entry["count"],
                "percent": round(entry["count"] / total * 100, 1),
                "from_places_api": entry["from_places_api"],
                "from_market_data": entry["from_market_data"],
                "registered_plans": entry["registered_plans"],
            }
            for name, entry in counts.items()
        ],
        key=lambda r: r["count"],
        reverse=True,
    )


def get_places_api_rows(latest_market=None, limit=500):
    """TEMPORARY VERIFICATION DATA -- every (industry, barangay) combo
    whose freshest market_data row actually came back from the Google
    Places API (New), i.e. `source == "Google Places API"`.

    This exists purely so you can SEE, on screen, that real Google data
    is arriving rather than the simulated fallback. It is surfaced by
    the "Google Places API -- Fetched Data" panel on the LGU Trend
    Reports page, which is marked as temporary and is safe to delete
    wholesale once you've confirmed the integration: remove this
    function, the /api/places-fetched route, and that one card.
    """
    latest_market = latest_market if latest_market is not None else latest_market_data_by_key()
    rows = []
    for row in latest_market.values():
        if row.source != "Google Places API":
            continue
        population = get_real_population(row.location)
        # Skip locations with no real PSA population on file. Those are
        # names that entered via an upload but don't match any of the 76
        # real barangays (e.g. a stray "Baras" alongside the actual
        # "Baras-baras"), so there is nothing verifiable behind them --
        # see _has_real_population().
        if population is None:
            continue
        rows.append(
            {
                "industry_type": row.industry_type,
                "location": row.location,
                "competitor_count": int(row.competitor_count or 0),
                "population": population,
                "source": row.source,
                "date_recorded": row.date_recorded.strftime("%Y-%m-%d") if row.date_recorded else None,
            }
        )
    rows.sort(key=lambda r: (-r["competitor_count"], r["location"], r["industry_type"]))
    return rows[:limit]


def _has_real_population(location):
    """True only for locations that map to one of Tarlac City's 76 real
    barangays, which is exactly the set that has a real 2024 PSA
    population figure behind it (app/ml/seed_data.py).

    Location is free text everywhere in this schema, so an LGU upload or
    a hand-typed business plan can introduce a name that isn't a real
    barangay -- a stray "Baras" next to the actual "Baras-baras", a
    misspelling, a subdivision name. Those rows have no population, no
    land area and no reference profile, so they can't be reported on
    honestly; the dashboard tables leave them out rather than showing a
    row of blanks. They are NOT deleted -- the underlying market_data is
    untouched and still feeds the AI engine's own averages.
    """
    return get_real_population(location) is not None


def get_barangay_business_table(latest_market=None):
    """Per-barangay roll-up of every business on file, used by the LGU
    Dashboard's "All Barangays" panel.

    For each barangay: how many businesses are on file in total, how
    many of those came from a live Google Places lookup, how many
    distinct industries have been surveyed there, and that barangay's
    real 2024 PSA population. Pure database reads (no AI sweep, no HTTP)
    so it stays cheap enough to render on a dashboard.

    Locations with NO real PSA population are excluded -- see
    _has_real_population().
    """
    latest_market = latest_market if latest_market is not None else latest_market_data_by_key()

    by_location = {}
    for (industry_type, location), row in latest_market.items():
        if not _has_real_population(location):
            continue
        entry = by_location.setdefault(
            location,
            {"location": location, "total_businesses": 0, "from_places_api": 0, "industries": 0, "top_industry": None,
             "_top_count": -1},
        )
        count = int(row.competitor_count or 0)
        entry["total_businesses"] += count
        entry["industries"] += 1
        # Credit Places only when Places actually supplied the winning
        # figure. After reconciliation the freshest row can be the
        # permit register while the larger, reported count came from
        # Google -- or the reverse -- so `source` alone would mis-state
        # how much of this barangay is confirmed by a live lookup.
        winning_source = getattr(row, "competitor_source", row.source)
        if winning_source == "Google Places API":
            entry["from_places_api"] += count
        if count > entry["_top_count"]:
            entry["_top_count"] = count
            entry["top_industry"] = industry_type

    # Include reference barangays that have no market_data yet, so the
    # LGU sees all 76 rather than only the ones already looked up.
    for name in BARANGAY_NAMES:
        by_location.setdefault(
            name,
            {"location": name, "total_businesses": 0, "from_places_api": 0, "industries": 0, "top_industry": None,
             "_top_count": -1},
        )

    rows = []
    for entry in by_location.values():
        entry.pop("_top_count", None)
        entry["population"] = get_real_population(entry["location"])
        rows.append(entry)

    rows.sort(key=lambda r: (-r["total_businesses"], r["location"]))
    return rows


def get_monthly_industry_trends(baseline, real_forecasts, industries, months=_MONTHS_BACK, as_of=None):
    """Saturation % per industry over the months ending at `as_of`.

    A month with real ForecastResult rows uses their real average. A
    month with none is BACK-PROJECTED: that industry's current baseline
    average, moved along the national establishment curve to where that
    month sat on it (historical_baseline_service.project_saturation).

    That replaces a flat line plus a small sine wiggle. The wiggle kept
    the chart from looking dead, but it carried no information -- every
    month of 2020 drew at the same height as every month of 2026, which
    is exactly the "made-up" shape this page is supposed to avoid. The
    projection has a real 2020 contraction and a real 2021 rebound in it
    and each industry keeps its own deterministic, seeded variation, so
    the lines still separate without being random.

    Real months are matched on (year, month), not on the month NAME. The
    old comparison was `forecast_date.strftime("%b") == label`, which
    matched March 2021 against March 2026 -- now that the picker reaches
    back to 2020, that would have pulled the wrong year's forecasts into
    the chart.
    """
    month_dates = _last_n_month_dates(months, as_of=as_of)
    labels = [m.strftime("%b %Y") for m in month_dates]
    series = {industry: [] for industry in industries}
    reference_month = date.today().replace(day=1)

    for industry in industries:
        baseline_avg = _industry_baseline_average(baseline, industry, "saturation_index")
        for anchor in month_dates:
            month_rows = [
                r
                for r in real_forecasts
                if r.input_industry_type == industry
                and r.forecast_date
                and (r.forecast_date.year, r.forecast_date.month) == (anchor.year, anchor.month)
            ]
            if month_rows:
                value = sum(float(r.saturation_index or 0) for r in month_rows) / len(month_rows)
            elif baseline_avg:
                value = project_saturation(
                    baseline_avg, anchor, reference=reference_month, series_key=industry
                )
            else:
                value = 0.0
            series[industry].append(round(value, 1))

    return {"labels": labels, "series": series}


def _quarter_end_dates(n, as_of=None):
    """[(label, end_date)] for the `n` quarters ending at `as_of`
    (default: this quarter), oldest first."""
    today = as_of or date.today()
    q = (today.month - 1) // 3 + 1
    year = today.year
    out = []
    for _ in range(n):
        end_month = q * 3
        last_day = calendar.monthrange(year, end_month)[1]
        out.append((f"Q{q} {year}", date(year, end_month, last_day)))
        q -= 1
        if q == 0:
            q = 4
            year -= 1
    out.reverse()
    return out


def get_market_quarterly_performance(industry_type=None, quarters=_QUARTERS_BACK, as_of=None):
    """How the MARKET performed, quarter by quarter -- optionally for one
    industry.

    This replaced a revenue chart that summed the (since removed)
    monthly revenue estimate across registered business plans. On a real deployment that is one
    or two plans, so the line was flat at a fraction of a million pesos
    and said nothing about the market. What an LGU actually wants to see
    is how the *market* moved, so each quarter now reports:

      businesses        -- the real number of businesses on file at the
                           END of that quarter, i.e. the freshest
                           market_data snapshot dated on or before that
                           quarter's last day, summed across barangays.
                           market_data keeps one row per refresh, so
                           this is genuine history, not a re-statement
                           of today's number.
      saturation        -- average AI saturation % from real
                           ForecastResult rows dated inside that
                           quarter, where any exist.
      growth_rate       -- quarter-over-quarter % change in `businesses`.

    Quarters older than the first snapshot on file cannot be measured.
    Rather than inventing a number and presenting it as data, those are
    back-projected along the Philippines' published national MSME
    establishment series and flagged `projected: true`, so the chart can
    draw them differently and the page can say so. As real history
    accumulates the projected points are replaced by measured ones.

    That projection used to be a flat 8% per quarter, which is where
    this chart's "made-up" feel came from: 8% a quarter is 36% a year,
    compounding, in every quarter of every year -- no pandemic, no
    rebound, just a smooth exponential nobody measured. It now follows
    the real series (see historical_baseline_service.py), so the growth
    rate the chart reports for, say, mid-2020 is negative, because that
    is what actually happened to Philippine establishments.
    """
    labels_and_ends = _quarter_end_dates(quarters, as_of=as_of)
    rows = _snapshot_rows(industry_type)

    # Live plans only -- see live_plan_forecasts().
    forecast_query = live_plan_forecasts()
    if industry_type:
        forecast_query = forecast_query.filter(ForecastResult.input_industry_type == industry_type)
    forecasts = forecast_query.all()

    businesses = []
    saturations = []
    measured = []
    for label, end_date in labels_and_ends:
        total, combos = _snapshot_total_as_of(rows, end_date)
        businesses.append(total)
        measured.append(bool(combos))

        quarter_start_month = ((int(label[1]) - 1) * 3) + 1
        year = int(label.split()[1])
        in_quarter = [
            float(f.saturation_index or 0)
            for f in forecasts
            if f.forecast_date
            and f.forecast_date.year == year
            and quarter_start_month <= f.forecast_date.month <= quarter_start_month + 2
        ]
        saturations.append(round(sum(in_quarter) / len(in_quarter), 1) if in_quarter else None)

    # Back-project the leading run of quarters that have no data, from
    # the earliest quarter that does.
    first_measured = next((i for i, m in enumerate(measured) if m), None)
    projected = [not m for m in measured]
    if first_measured is not None:
        anchor_total = businesses[first_measured]
        anchor_end = labels_and_ends[first_measured][1]
        for i in range(first_measured - 1, -1, -1):
            businesses[i] = project_businesses(
                anchor_total, labels_and_ends[i][1], reference=anchor_end, series_key=industry_type
            )
    else:
        # EVERY quarter in the window predates the first snapshot -- which
        # is what happens the moment anyone picks 2020-2025 in the period
        # calendar. This used to return a row of zeros, so the chart drew
        # a flat line on the floor and the growth rate read 0% for every
        # quarter. Project the whole window from today's REAL total
        # instead, and flag all of it.
        current_total, _combos, reference = _current_market_total(rows)
        if current_total:
            businesses = [
                project_businesses(
                    current_total, end_date, reference=reference, series_key=industry_type
                )
                for _label, end_date in labels_and_ends
            ]
            projected = [True] * len(businesses)
        else:
            projected = [False] * len(businesses)  # nothing on file at all -- honest zeros

    growth = [0.0]
    for i in range(1, len(businesses)):
        prev = businesses[i - 1]
        growth.append(round(((businesses[i] - prev) / prev) * 100, 1) if prev else 0.0)

    # Carry the last known saturation forward so the series has no holes,
    # but never invent one before the first real reading.
    last_seen = None
    saturation_filled = []
    for value in saturations:
        if value is not None:
            last_seen = value
        saturation_filled.append(last_seen)

    return {
        "industry_type": industry_type,
        "labels": [label for label, _ in labels_and_ends],
        "businesses": businesses,
        "saturation_percent": saturation_filled,
        "growth_rate_percent": growth,
        "projected": projected,
        "has_real_history": any(measured),
        "measured_quarters": sum(1 for m in measured if m),
    }


def get_top_industries(baseline, monthly_trends):
    """Ranks industries by blended (baseline + real) viability. The "%"
    change shown is the real first-vs-last delta of that industry's own
    monthly trend series computed above -- not a separate invented
    number."""
    rows = []
    for industry in monthly_trends["series"]:
        avg_viability = _industry_baseline_average(baseline, industry, "viability_score")
        series = monthly_trends["series"][industry]
        change_percent = round(((series[-1] - series[0]) / series[0]) * 100, 1) if series and series[0] else 0.0
        # Saturation trending up = viability trending down, so flip the
        # sign for a "market score" change indicator.
        rows.append({"name": industry, "market_score": round(avg_viability, 1), "change_percent": -change_percent})
    return sorted(rows, key=lambda r: r["market_score"], reverse=True)


def _trend_delta(monthly_trends, invert=False):
    """Average, across every charted industry, of that industry's own
    first-vs-last value in monthly_trends -- a real computed delta (not
    a separate invented number), used for the Market Saturation / Avg
    Viability KPI cards' "vs 6 months ago" indicator. invert=True flips
    the sign (used for viability, where a saturation-driven series
    moving down is actually an improvement)."""
    series_list = list(monthly_trends["series"].values())
    if not series_list:
        return 0.0
    deltas = [s[-1] - s[0] for s in series_list if s]
    if not deltas:
        return 0.0
    avg_delta = sum(deltas) / len(deltas)
    return round(-avg_delta if invert else avg_delta, 1)


# How much of the model's own confidence a quarter of forecast horizon
# costs. The Random Forest's confidence describes how much its trees
# agree about ONE feature vector; it says nothing about whether that
# vector will still describe the market in nine months. Projecting the
# inputs forward adds uncertainty the model cannot see, so it is
# subtracted here rather than left for a reader to guess at. 8 points
# per quarter puts a Q4 figure roughly 24 points below its Q1 sibling,
# which is the right order for "this is the model's real answer to a
# question about next year".
HORIZON_CONFIDENCE_PENALTY = 8.0


def project_quarterly_outlook(saturation_index, viability_score, location,
                              industry_type=None, quarters=4, sme_profile=None):
    """The Home page's "Current vs. Projected Demand & Viability" chart:
    FOUR REAL MODEL RUNS, one per quarter.

    WHAT THIS USED TO BE, AND WHY IT CHANGED

    Q1 was real and Q2-Q4 were decoration: the Q1 bars multiplied by
    1.08, 1.04 and 1.06 per quarter. Those constants came from nowhere.
    The chart therefore always sloped the same way -- demand up,
    saturation up, viability up -- for every barangay, every industry
    and every plan, because the shape was in the multipliers rather
    than in the data. It could not tell an SME anything they had not
    already been told by the Q1 bar, and it could never disagree with
    itself.

    Now each quarter RECALIBRATES THE FEATURE VECTOR and re-runs the
    Random Forest on it. The saturation bar for Q3 is the model's own
    prediction for Q3's inputs, not Q1's answer scaled by a constant,
    so the line can flatten, fall or steepen depending on the barangay
    -- and when it rises, it rises because the model says so.

    WHAT MOVES BETWEEN QUARTERS, AND WHAT DOES NOT

    Only the competitor count is projected forward, and it moves along
    the PSA/DTI national MSME establishment series that
    historical_baseline_service already uses to project BACKWARDS for
    the Trend Reports page (published 2019-2023, continued at
    POST_RECOVERY_GROWTH after that). Using one series in both
    directions matters: a back-projection and a forward projection that
    disagreed about the same market would be two different claims from
    one system.

    Everything else -- population density, rent, foot traffic,
    historical success rate, business density -- is HELD AT TODAY'S
    VALUE, because this project has no published forward series for any
    of them and inventing drift rates is exactly the thing being
    removed. Holding them is an assumption too, and it is stated in
    `assumptions` below so the UI can show it rather than imply the
    whole vector was forecast.

    Competitor count is also the feature the engine weights most
    heavily (msi_weight_competitor_density, 0.45), so it is the one
    worth projecting if only one can be.

    Returns the three 0-60 series the chart draws, plus per-quarter
    `confidence`, the projected `competitors`, and `basis` -- "model"
    for a real run, "scaled" if the model was unavailable and the old
    arithmetic had to stand in.

    WITH A PLAN (`sme_profile`): THE VIABILITY LINE IS THE PLAN'S
    A plan's stored viability is no longer (100 - saturation) / 10 --
    it is the Plan Viability Model's (stage 2,
    plan_forecast_service), which also weighs the plan's capital,
    staff, stage, price list, offering and idea. A chart that drew the
    market formula next to a plan score would show two different
    numbers called "viability" on one page. So with a plan, each
    quarter runs BOTH stages:

      1. stage 1 on that quarter's projected competitor count, with the
         plan's own years in business (as its stored forecast was), and
         -- when the plan's sub-category adjusted its competition --
         the same direct-competition ratio applied to the projected
         count and re-scored the way subcategory_service does;
      2. stage 2 on that quarter's MSI* and competitor count, with
         every plan input HELD as the owner entered it
         (plan_forecast_service.plan_viability_for).

    Q1 is today's market, so it reproduces the stored forecast's
    viability -- which is why the plan inputs are held at the daily wage
    and gross margin that forecast used, even after an Admin has changed
    them (see _quarterly_plan_context). Confidence is the lower of the
    two stages', less the horizon penalty. Without a plan the function
    behaves exactly as it always did, and `viability_model` says which
    of the two this is.
    """
    from app.services.forecasting_service import (
        _lgu_rows, _latest_market_rows, _predict_saturation, build_feature_vector,
        reconciled_competitor_counts,
    )

    profile = get_barangay_profile(location)
    foot_traffic = profile["foot_traffic_index"] if profile else 45.0
    today = date.today()

    market = lgu = None
    current_competitors = None
    if industry_type:
        market = _latest_market_rows([industry_type], [location]).get((industry_type, location))
        lgu = _lgu_rows([location]).get(location)
        current_competitors = reconciled_competitor_counts(
            [industry_type], [location]
        ).get((industry_type, location))
        if current_competitors is None and market is not None:
            current_competitors = market.competitor_count

    # This combo's own observed rate where the history supports one,
    # the national series otherwise. Which was used is reported in
    # `assumptions` -- "projected from this barangay's own trend" and
    # "projected from the national series" are different claims and the
    # UI should not present them as the same one.
    observed = observed_competitor_growth(industry_type, location) if industry_type else None

    plan = _quarterly_plan_context(sme_profile, industry_type, location, market, lgu,
                                   current_competitors, saturation_index)

    labels, demand, saturation, viability = [], [], [], []
    confidence, competitors, basis = [], [], []

    for index in range(quarters):
        # Quarter 1 is now, so its growth is 1.0 and its model run
        # reproduces the stored forecast. Later quarters are anchored
        # three months apart.
        if observed is not None:
            growth = (1.0 + observed) ** index
        else:
            # series_key is deliberately NOT passed. _series_variation
            # adds a small deterministic wobble so that dozens of
            # back-projected history lines do not sit on top of each
            # other -- useful there, wrong here: across four forward
            # points it made demand fall in Q2 and rise again in Q3,
            # which reads as a finding and is an artefact.
            growth = growth_index(_add_months(today, 3 * index), reference=today)

        quarter_saturation = None
        quarter_confidence = None
        quarter_viability = None
        projected_competitors = None

        if market is not None and lgu is not None and current_competitors is not None:
            projected_competitors = max(0, int(round(float(current_competitors) * growth)))
            if plan is None:
                vector = build_feature_vector(
                    market, lgu, 0, industry_type, competitor_count=projected_competitors
                )
                predicted, model_confidence, _version = _predict_saturation(vector)
                quarter_saturation = predicted
            else:
                quarter_saturation, model_confidence, quarter_viability = _quarterly_plan_run(
                    plan, market, lgu, industry_type, projected_competitors,
                )
            quarter_confidence = round(
                max(0.0, model_confidence - HORIZON_CONFIDENCE_PENALTY * index), 1
            )

        if quarter_saturation is None:
            # No rows for this combo yet (a brand-new database, or a
            # location the engine has never scored). Fall back to the
            # stored figure rather than refusing to draw the chart, and
            # mark the bar so nothing claims it was a model run.
            quarter_saturation = float(saturation_index or 0)
            quarter_confidence = None
            if sme_profile is not None and viability_score is not None:
                # The stored figure for a plan IS the plan's viability.
                quarter_viability = round(max(0.0, min(10.0, float(viability_score))), 1)
            basis.append("scaled")
        else:
            basis.append("model")

        if quarter_viability is None:
            quarter_viability = round(max(0.0, min(10.0, (100.0 - quarter_saturation) / 10.0)), 1)

        labels.append(f"Q{index + 1}")
        competitors.append(projected_competitors)
        confidence.append(quarter_confidence)
        # Demand uses the same establishment index: more businesses
        # trading in a market is this project's only published proxy
        # for commercial activity in it. Named in `assumptions` as a
        # proxy, because it is one.
        demand.append(round(min(60.0, foot_traffic * 0.6 * growth), 1))
        saturation.append(round(min(60.0, quarter_saturation * 0.6), 1))
        viability.append(round(min(60.0, quarter_viability * 6.0), 1))

    return {
        "labels": labels,
        "demand": demand,
        "saturation": saturation,
        "viability": viability,
        "confidence": confidence,
        "competitors": competitors,
        "basis": basis,
        "recalibrated": all(entry == "model" for entry in basis),
        # "plan": the viability line is the Plan Viability Model's (stage
        # 2, every plan input held); "market": (100 - saturation) / 10.
        "viability_model": "plan" if sme_profile is not None else "market",
        "assumptions": {
            "projected": "competitor count",
            "source": "observed" if observed is not None else "national",
            "series": (
                f"this barangay's own recorded trend ({observed:+.1%} per quarter)"
                if observed is not None else
                "the PSA/DTI national MSME establishment series (published "
                f"2019-2023, continued at {POST_RECOVERY_GROWTH:.0%}/year)"
            ),
            # The MARKET inputs held. The plan's own inputs, held too in
            # plan mode, are named under "viability" below, so a caption
            # quoting both never says the same thing twice.
            "held": "population density, rent, foot traffic, historical success "
                    "rate and business density are held at today's values",
            "demand_proxy": "foot traffic scaled by the same growth rate",
            "viability": (
                "the Plan Viability Model re-run on each quarter's projected market, with the "
                "plan's capital, employees, stage, price list, offering and idea held as entered"
                if sme_profile is not None else
                "(100 - saturation) / 10 of each quarter's projected market"
            ),
        },
    }


def _quarterly_plan_context(sme_profile, industry_type, location, market, lgu,
                            current_competitors, saturation_index):
    """What a plan-aware quarterly run needs, resolved once rather than
    per quarter -- or None when there is no plan, or no market on file
    to run the models on (the caller then falls back exactly as it
    always has).

    `direct_ratio` carries the plan's sub-category adjustment forward:
    when the stored forecast scored a bakery against 0.4x the industry's
    competitor count, every projected quarter is scored against 0.4x
    that quarter's projected count too. It is the ratio of the counts
    actually used (adjusted / industry) rather than the 2-decimal
    density_ratio, so Q1 lands on exactly the stored count.

    The plan inputs are held at the wage and gross margin the STORED
    forecast was computed with (_stored_plan_assumptions), not today's
    Admin settings. A stored forecast keeps its assumptions until the
    plan is re-forecast; a chart that picked up a new wage at once would
    put a Q1 bar next to the gauge that no longer equals it."""
    if sme_profile is None or market is None or lgu is None or current_competitors is None:
        return None

    from app.services.plan_forecast_service import build_plan_inputs
    from app.services.subcategory_service import direct_competition

    analysis = None
    subcategory = getattr(sme_profile, "subcategory", None)
    if subcategory:
        # allow_live=False: a chart must never spend a Places call. The
        # stored forecast already fetched (and saved) any live count.
        analysis = direct_competition(industry_type, subcategory, location, current_competitors,
                                      allow_live=False)
    direct_ratio = None
    if analysis and analysis.get("adjusts_score") and current_competitors:
        direct_ratio = float(analysis["adjusted_competitor_count"]) / float(current_competitors)

    inputs = build_plan_inputs(
        sme_profile,
        {"saturation_index": float(saturation_index or 0), "competitor_count": current_competitors},
        market,
        get_real_population(location) or 0,
        analysis,
        # None (no stored payload) -> build_plan_inputs uses today's.
        assumptions=_stored_plan_assumptions(sme_profile),
    )
    return {
        "inputs": inputs,
        "years": float(sme_profile.years_in_operation() or 0),
        "direct_ratio": direct_ratio,
    }


def _stored_plan_assumptions(sme_profile):
    """(daily_wage, gross_margin) the plan's latest stored forecast was
    computed with -- the forecast the Home gauge shows -- or None when
    there is no such forecast or it predates the plan model (the caller
    then uses the current settings, which is all a forecast made now
    would use too). One indexed query; never raises."""
    import json

    from app.services.plan_forecast_service import assumptions_from_payload

    try:
        latest = sme_profile.latest_forecast()
        stored = json.loads(latest.recommendation) if latest is not None and latest.recommendation else None
    except Exception:  # noqa: BLE001 -- a chart must not fail over a stored row
        return None
    if not isinstance(stored, dict):
        return None
    return assumptions_from_payload(stored.get("forecast"))


def _quarterly_plan_run(plan, market, lgu, industry_type, projected_competitors):
    """(MSI*, combined confidence, plan viability 0-10) for one quarter
    -- both stages, as described in project_quarterly_outlook()."""
    from app.services.forecasting_service import _predict_saturation, build_feature_vector
    from app.services.plan_forecast_service import plan_viability_for

    vector = build_feature_vector(
        market, lgu, plan["years"], industry_type, competitor_count=projected_competitors
    )
    msi, stage1_confidence, _version = _predict_saturation(vector)
    plan_competitors = projected_competitors

    if plan["direct_ratio"] is not None:
        plan_competitors = max(0, int(round(projected_competitors * plan["direct_ratio"])))
        # Re-scored with years 0, exactly as saturation_for_counts() --
        # and so the stored forecast's MSI* -- does.
        direct_vector = build_feature_vector(
            market, lgu, 0, industry_type, competitor_count=plan_competitors
        )
        msi, _direct_confidence, _version = _predict_saturation(direct_vector)

    result = plan_viability_for(plan["inputs"], msi, plan_competitors)
    return msi, min(stage1_confidence, result["confidence"]), result["viability_score"]


# Widest per-quarter competitor growth this will believe from a
# barangay's own history, either way. Two refreshes weeks apart can
# differ for reasons that are not a trend -- a Places lookup that hit
# the 60-result ceiling, a permit register covering half the city --
# and an unclamped rate compounded over four quarters turns one such
# row into a hockey stick. +/-15% a quarter is still a market changing
# fast; beyond that the evidence is more likely about the measurement
# than the market.
MAX_OBSERVED_QUARTERLY_GROWTH = 0.15

# Below this the history is too short to read a rate from: two counts a
# fortnight apart say almost nothing about a year.
MIN_HISTORY_DAYS = 60


def observed_competitor_growth(industry_type, location):
    """This combo's OWN observed competitor growth, per quarter, or
    None when its history is too thin to read one from.

    WHY THIS BEATS THE NATIONAL SERIES WHEN IT EXISTS. The national
    MSME series continues at 3%/year, which over four quarters moves a
    count of 2 to a count of 2 and a count of 34 to 35 -- a
    recalibration that recalibrates nothing, and a chart that is flat
    for every barangay in the city. That is the same failure as the
    hardcoded multipliers it replaced, just with a more respectable
    source: the shape is in the constant rather than in the barangay.

    market_data is a history table -- one row per refresh -- so a
    barangay that has been scored a few times over a few months has
    already recorded its own trend. Reading the rate from those rows
    is what makes Q3 differ between a barangay filling up fast and one
    standing still, which is the entire point of drawing the chart.

    The national series stays as the fallback, so a combo with no
    history still gets a defensible number rather than a flat line.
    """
    rows = (
        db.session.query(MarketData.date_recorded, MarketData.competitor_count)
        .filter(
            MarketData.industry_type == industry_type,
            MarketData.location == location,
            MarketData.competitor_count.isnot(None),
        )
        .order_by(MarketData.date_recorded.asc(), MarketData.market_id.asc())
        .all()
    )
    if len(rows) < 2:
        return None

    (first_date, first_count), (last_date, last_count) = rows[0], rows[-1]
    span_days = (last_date - first_date).days
    if span_days < MIN_HISTORY_DAYS or not first_count or first_count <= 0:
        return None

    quarters = span_days / 91.3125
    try:
        per_quarter = (float(last_count) / float(first_count)) ** (1.0 / quarters) - 1.0
    except (ValueError, ZeroDivisionError, OverflowError):
        return None

    return max(-MAX_OBSERVED_QUARTERLY_GROWTH,
               min(MAX_OBSERVED_QUARTERLY_GROWTH, per_quarter))


def _add_months(anchor, months):
    """`anchor` moved forward by whole months, clamped to the last valid
    day (so a 31st never rolls into the next month)."""
    import calendar

    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


# How many months/quarters of history the charts draw, ending at the
# month the user picked in the "Select Period" calendar. These are
# window SIZES, not a menu of dates -- the date itself comes from a real
# month picker, so any month of any year can be reviewed.
TREND_WINDOW_MONTHS = 6
TREND_WINDOW_QUARTERS = 5

# The earliest month the picker will accept: January 2020, so the
# control offers 2020 through the current month, which is the range the
# published national establishment series (and therefore the
# back-projection) actually covers. Going further back would extrapolate
# below the real data; going less far would hide the pandemic years,
# which are the most interesting part of any 2020-2026 trend.
EARLIEST_TREND_MONTH = EARLIEST_HISTORY


def resolve_as_of_month(value):
    """Parse the month picker's value ("YYYY-MM") into the last day of
    that month. Anything unparseable, in the future, or before
    EARLIEST_TREND_MONTH falls back to the current month -- a trend
    report for a month that hasn't happened yet would be pure
    projection presented as history."""
    today = date.today()
    current = today.replace(day=1)
    if not value:
        return current
    try:
        year_str, month_str = str(value).split("-")[:2]
        anchor = date(int(year_str), int(month_str), 1)
    except (ValueError, TypeError):
        return current
    if anchor > current:
        return current
    if anchor < EARLIEST_TREND_MONTH:
        return EARLIEST_TREND_MONTH
    return anchor


def build_trend_period(industry_type=None, as_of=None,
                       months=TREND_WINDOW_MONTHS, quarters=TREND_WINDOW_QUARTERS):
    """ONLY the parts of the report that move when the Select Period
    month changes: the four KPI cards, the monthly trend lines, the
    quarterly chart and the industry ranking derived from them.

    This exists so picking a month is fast. It used to reload the whole
    page, which re-ran the full city-wide AI sweep, re-scanned
    market_data for the pie chart and the Places verification table, and
    re-downloaded every asset -- to change four numbers and three
    charts. The Industry Distribution pie and the Places table do not
    depend on the chosen month at all, so they are not rebuilt here, and
    the sweep behind the rest is served from cache (see _cached above).
    """
    as_of = as_of or date.today().replace(day=1)
    industries = [industry_type] if industry_type else list(BUSINESS_TYPES)
    baseline = _sweep_baseline(industry_type)
    real_forecasts = _real_forecasts(industry_type)

    monthly_trends = get_monthly_industry_trends(
        baseline, real_forecasts, industries, months=months, as_of=as_of
    )

    return {
        "period": {
            "as_of": as_of.strftime("%Y-%m"),
            "label": as_of.strftime("%B %Y"),
            "months": months,
            "quarters": quarters,
            "earliest": EARLIEST_TREND_MONTH.strftime("%Y-%m"),
        },
        "period_overview": get_period_overview(as_of, baseline, real_forecasts, industry_type),
        "monthly_trends": monthly_trends,
        "market_quarterly": get_market_quarterly_performance(
            industry_type, quarters=quarters, as_of=as_of
        ),
        "top_industries": get_top_industries(baseline, monthly_trends),
        "_baseline": baseline,
        "_real_forecasts": real_forecasts,
    }


def build_trend_report(industry_type=None, as_of=None,
                       months=TREND_WINDOW_MONTHS, quarters=TREND_WINDOW_QUARTERS):
    """Single entry point used by GET /api/trend-data -- runs the
    full-city baseline sweep ONCE and derives every card/chart from it,
    so the (relatively) expensive AI sweep only happens one time per
    request no matter how many stats are built from it.

    `as_of` is the month chosen in the "Select Period" calendar (the
    first day of that month); every series ENDS there rather than at
    today, so picking March 2026 reviews the market as it stood in March
    2026. Reviewing a past month does not invent history: months and
    quarters with no real snapshot behind them stay flagged as
    back-projections (see get_market_quarterly_performance), and the KPI
    cards report `measured: False` rather than a fabricated delta."""
    as_of = as_of or date.today().replace(day=1)

    # Everything that moves with the chosen month, built once.
    period = build_trend_period(industry_type, as_of=as_of, months=months, quarters=quarters)
    baseline = period.pop("_baseline")
    real_forecasts = period.pop("_real_forecasts")
    monthly_trends = period["monthly_trends"]

    overview = get_overview_stats(baseline, real_forecasts)
    overview["saturation_trend_pts"] = _trend_delta(monthly_trends)
    overview["viability_trend"] = round(_trend_delta(monthly_trends, invert=True) / 10, 1)

    # Collapse market_data to one row per (industry, location) ONCE and
    # share it -- the distribution and the Places verification table
    # would otherwise each re-run the same full-table scan.
    latest_market = latest_market_data_by_key()

    return {
        "overview": overview,
        "industry_distribution": get_industry_distribution(latest_market),
        # period / period_overview / monthly_trends / market_quarterly /
        # top_industries -- the month-dependent half, identical to what
        # /api/trend-period returns on its own.
        **period,
    }
