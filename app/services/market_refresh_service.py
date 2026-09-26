"""
app/services/market_refresh_service.py
-----------------------------------------
Bulk-fetches REAL business counts from Google Places for every
(industry x barangay) combination, in explicit, bounded batches with a
progress readout.

WHY A BULK PATH EXISTS AT ALL
Ordinary page loads only upgrade a handful of rows each
(`_MAX_LIVE_REFRESH_PER_REQUEST` in forecasting_service), so the
database creeps towards real data over many visits. That is the right
behaviour for a page load -- nobody wants a dashboard that blocks on
hundreds of HTTP calls -- but it is a poor way to get a demo-ready
database. This module is the deliberate "go and fetch it all now"
button behind /api/places-refresh.

*** READ THIS BEFORE RUNNING IT ACROSS THE WHOLE CITY ***
The full scope is every industry x every barangay:
    20 industries x 76 barangays = 1,520 combinations
and with result paging switched on (unlimited), ONE combination can
cost up to 3 Text Search requests. A complete sweep is therefore up to
~4,560 billable Places API calls. That is well past Google's monthly
free allowance for Text Search, and it WILL appear on the billing
account attached to your API key.

So this module never runs on its own. It only ever moves when someone
presses the button, it moves `limit` combinations at a time, and it
reports exactly how many remain so the cost stays visible and
interruptible. If you only need a convincing demo, refreshing a few
hundred combinations is plenty -- prioritise_combos() deliberately
front-loads the biggest barangays and the most common industries so the
first batches are the ones anyone will actually look at.
"""

from app.extensions import db
from app.models import MarketData
from app.ml.constants import BUSINESS_TYPES, FEATURED_BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES, get_real_population

# Ceiling on one HTTP request's worth of work, so a batch can't run long
# enough to hit a proxy/gateway timeout. The UI loops instead.
MAX_BATCH = 25


def _real_combo_keys():
    """{(industry, location)} that already hold a real, UNCAPPED Google
    Places count -- i.e. nothing left to do for them."""
    from app.services.startup_migrations import get_places_recap_watermark

    watermark = get_places_recap_watermark()
    rows = (
        MarketData.query.filter_by(source="Google Places API")
        .with_entities(MarketData.industry_type, MarketData.location, MarketData.market_id)
        .all()
    )
    done = set()
    for industry, location, market_id in rows:
        # Rows at/below the watermark were fetched under the old
        # 20-result cap, so they are NOT done -- they need re-fetching.
        if market_id is not None and market_id <= watermark:
            continue
        done.add((industry, location))
    return done


def prioritise_combos():
    """Every (industry, barangay) pair, ordered so the most useful ones
    are fetched first: the featured industries before the long tail, and
    the most populous barangays before the smallest. A partial sweep
    then still produces a map and a set of charts that look right where
    anyone would actually look."""
    industries = list(FEATURED_BUSINESS_TYPES) + [b for b in BUSINESS_TYPES if b not in FEATURED_BUSINESS_TYPES]
    barangays = sorted(BARANGAY_NAMES, key=lambda b: (-(get_real_population(b) or 0), b))
    return [(industry, barangay) for barangay in barangays for industry in industries]


def get_progress():
    """{total, done, pending} across the full industry x barangay grid."""
    all_combos = prioritise_combos()
    done_keys = _real_combo_keys()
    done = sum(1 for combo in all_combos if combo in done_keys)
    return {"total": len(all_combos), "done": done, "pending": len(all_combos) - done}


def refresh_batch(limit=MAX_BATCH):
    """Fetch real Places data for up to `limit` combinations that don't
    have it yet. Returns {requested, refreshed, failed, total, done,
    pending}.

    `refreshed` counts combinations that came back as REAL Google data.
    `failed` counts ones that fell back to a simulated count (no key,
    quota exhausted, network error, or genuinely no matches) -- those
    stay pending so a later batch can try again rather than being
    silently recorded as complete.
    """
    from app.services.forecasting_service import find_or_create_market_data, set_places_refresh_budget

    limit = max(1, min(int(limit or MAX_BATCH), MAX_BATCH))

    # This endpoint is the one place allowed to spend an unbounded
    # number of live lookups -- that is its entire purpose.
    set_places_refresh_budget(None)

    done_keys = _real_combo_keys()
    todo = [combo for combo in prioritise_combos() if combo not in done_keys][:limit]

    refreshed = failed = 0
    for industry, location in todo:
        try:
            row = find_or_create_market_data(industry, location)
        except Exception:  # noqa: BLE001 -- one bad combo must not abort the batch
            db.session.rollback()
            failed += 1
            continue
        if row is not None and row.source == "Google Places API":
            refreshed += 1
        else:
            failed += 1

    progress = get_progress()
    return {
        "requested": len(todo),
        "refreshed": refreshed,
        "failed": failed,
        **progress,
    }
