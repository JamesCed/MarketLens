"""
app/services/saturation_timeline_service.py
---------------------------------------------
The Saturation Map at any month: HISTORY, CURRENT, or the model's
PREDICTION for a month ahead.

The map used to show one moment -- now. The timeline under it picks a
month from January 2020 to a year ahead, and every barangay is re-scored
for that month by the same Random Forest that scores the present. What
changes between months is the input the engine weights most heavily --
the number of competing businesses -- and where that number comes from
is recorded on every row as its `basis`, because "we have a count on
file for March 2024" and "we projected one" are different claims:

  recorded        A market_data snapshot on file at the end of that
                  month (the latest per source, the larger across
                  sources -- the same reconciliation the present uses).
  back-projected  No snapshot that old: today's count scaled back along
                  the PSA/DTI national MSME establishment series -- the
                  same series the Trend Reports page back-projects with
                  (historical_baseline_service), so the map and the
                  charts cannot disagree about the past.
  current         This month: exactly what the map has always shown.
  predicted       A month ahead: today's count moved forward along the
                  barangay's OWN recorded trend where it has one
                  (trend_analytics_service.observed_competitor_growth),
                  the national series otherwise -- the same rule the
                  Home page's quarterly outlook uses. Confidence falls
                  with the horizon (HORIZON_CONFIDENCE_PENALTY a
                  quarter).

Everything else in the feature vector -- population density, rent,
foot traffic, success rate -- is held at today's values, because no
published series exists for any of them by barangay and by month.
Holding them is an assumption, and the page says so.
"""

from datetime import date

from app.extensions import db
from app.services.historical_baseline_service import EARLIEST_HISTORY, growth_index

# How far ahead the timeline reaches. A year is what the quarterly
# outlook already forecasts; past that the horizon penalty leaves little
# confidence worth drawing.
FUTURE_MONTHS = 12

PERIOD_HISTORY = "history"
PERIOD_CURRENT = "current"
PERIOD_FUTURE = "future"


def _first_of_month(anchor):
    return anchor.replace(day=1)


def _month_end(anchor):
    import calendar

    return anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])


def _add_months(anchor, months):
    total = anchor.year * 12 + (anchor.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _months_between(earlier, later):
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def timeline_bounds(today=None):
    """The months the timeline offers, as first-of-month dates."""
    today = today or date.today()
    current = _first_of_month(today)
    return {
        "start": EARLIEST_HISTORY,
        "current": current,
        "end": _add_months(current, FUTURE_MONTHS),
    }


def resolve_period(value, today=None):
    """("YYYY-MM" or empty) -> (first-of-month date, period). Anything
    unparseable is the current month; anything outside the timeline is
    clamped to its nearest end rather than refused."""
    bounds = timeline_bounds(today)
    anchor = bounds["current"]
    if value:
        try:
            year, month = str(value).split("-")[:2]
            anchor = date(int(year), int(month), 1)
        except (TypeError, ValueError):
            anchor = bounds["current"]
    anchor = max(bounds["start"], min(bounds["end"], anchor))
    if anchor < bounds["current"]:
        period = PERIOD_HISTORY
    elif anchor > bounds["current"]:
        period = PERIOD_FUTURE
    else:
        period = PERIOD_CURRENT
    return anchor, period


def _snapshots(industry_type, locations):
    """{location: [(date_recorded, source, count), ...]} oldest first --
    every market_data snapshot on file for this industry (archived
    datasets are excluded by the global archive filter)."""
    from app.models import MarketData

    rows = (
        db.session.query(MarketData.location, MarketData.date_recorded, MarketData.source,
                         MarketData.competitor_count)
        .filter(
            MarketData.industry_type == industry_type,
            MarketData.location.in_(list(locations)),
            MarketData.competitor_count.isnot(None),
        )
        .order_by(MarketData.date_recorded.asc(), MarketData.market_id.asc())
        .all()
    )
    history = {}
    for location, recorded, source, count in rows:
        history.setdefault(location, []).append((recorded, source, int(count)))
    return history


def _recorded_as_of(snapshots, cutoff):
    """The count on file at `cutoff`: freshest row per source, larger
    across sources. None if nothing was on file yet."""
    freshest = {}
    for recorded, source, count in snapshots:
        if recorded <= cutoff:
            freshest[source] = count          # oldest first, so the last one wins
    return max(freshest.values()) if freshest else None


def _first_recorded_after(snapshots, cutoff):
    """(date, count) of the earliest snapshot after `cutoff`, or None --
    the anchor a back-projection starts from, so the projected past
    joins up with the first real count instead of jumping to it."""
    later = [(recorded, count) for recorded, _source, count in snapshots if recorded > cutoff]
    if not later:
        return None
    first_date = later[0][0]
    return first_date, max(count for recorded, count in later if recorded == first_date)


def _observed_growth(industry_type, locations):
    """{location: per-quarter growth} for the barangays whose own history
    supports a rate -- see trend_analytics_service.observed_competitor_growth."""
    from app.services.trend_analytics_service import observed_competitor_growth

    rates = {}
    for location in locations:
        rate = observed_competitor_growth(industry_type, location)
        if rate is not None:
            rates[location] = rate
    return rates


def saturation_map_at(industry_type, locations, as_of=None, today=None):
    """One row per location for the map, scored for the month `as_of`
    ("YYYY-MM"; empty means now). Each row carries the usual map fields
    plus `period`, `as_of` and `basis` (see the module docstring)."""
    from app.services.forecasting_service import (
        _cluster_label_for,
        compute_scores_batch,
        saturation_for_counts,
    )
    from app.services.trend_analytics_service import HORIZON_CONFIDENCE_PENALTY

    today = today or date.today()
    anchor, period = resolve_period(as_of, today)
    locations = list(locations)

    # Today's scores first: they are the "current" answer, and scoring
    # also creates any market/lgu row a barangay is still missing, which
    # the what-if scoring below needs.
    current = compute_scores_batch([(industry_type, location) for location in locations])
    label = anchor.strftime("%Y-%m")

    if period == PERIOD_CURRENT:
        return [
            {"location": location, "scores": scores, "competitor_count": scores["competitor_count"],
             "basis": "current", "period": period, "as_of": label}
            for location, scores in zip(locations, current)
        ]

    counts, bases, confidences = [], [], []
    if period == PERIOD_HISTORY:
        cutoff = _month_end(anchor)
        history = _snapshots(industry_type, locations)
        for location, scores in zip(locations, current):
            snapshots = history.get(location, [])
            on_file = _recorded_as_of(snapshots, cutoff)
            if on_file is not None:
                counts.append(on_file)
                bases.append("recorded")
            else:
                # Back-projected from the EARLIEST real count after this
                # month (today's, if there is no older one), along the
                # national series -- so March 2021 leads into the first
                # count on file rather than contradicting it.
                anchor_point = _first_recorded_after(snapshots, cutoff)
                if anchor_point is not None:
                    reference, base = anchor_point
                else:
                    reference, base = today, float(scores["competitor_count"] or 0)
                factor = growth_index(cutoff, reference=reference)
                counts.append(max(0, int(round(float(base) * factor))))
                bases.append("back-projected")
            confidences.append(scores["confidence_level"])
    else:
        months_ahead = _months_between(_first_of_month(today), anchor)
        quarters_ahead = months_ahead / 3.0
        observed = _observed_growth(industry_type, locations)
        national = growth_index(_month_end(anchor), reference=today)
        for location, scores in zip(locations, current):
            now = float(scores["competitor_count"] or 0)
            rate = observed.get(location)
            growth = (1.0 + rate) ** quarters_ahead if rate is not None else national
            counts.append(max(0, int(round(now * growth))))
            bases.append("predicted")
            confidences.append(round(max(0.0, float(scores["confidence_level"] or 0)
                                         - HORIZON_CONFIDENCE_PENALTY * quarters_ahead), 1))

    predicted = saturation_for_counts(
        [(industry_type, location, count) for location, count in zip(locations, counts)]
    )

    rows = []
    for index, (location, scores) in enumerate(zip(locations, current)):
        saturation = predicted[index]
        if saturation is None:
            # No rows to score this barangay against (should not happen
            # after compute_scores_batch) -- show today's figure, and say so.
            saturation = scores["saturation_index"]
            basis = "current"
            count = scores["competitor_count"]
        else:
            saturation = round(float(saturation), 2)
            basis = bases[index]
            count = counts[index]
        viability = round(max(0.0, min(10.0, (100.0 - saturation) / 10.0)), 1)
        rows.append({
            "location": location,
            "scores": {
                **scores,
                "saturation_index": saturation,
                "viability_score": viability,
                "cluster_label": _cluster_label_for(saturation),
                "confidence_level": confidences[index],
            },
            "competitor_count": count,
            "basis": basis,
            "period": period,
            "as_of": label,
        })
    return rows
