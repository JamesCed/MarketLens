"""
app/services/subcategory_service.py
-------------------------------------
Direct competition: how many businesses of the SAME KIND as this plan
are in the barangay, and what that does to its score.

THE PROBLEM
The model scores an industry section. A pandesal bakery in Tibag is
scored against every food business in Tibag. If Tibag has forty
eateries and three bakeries, the industry figure says "crowded" and the
owner is right to ask whether that crowding is really their problem.

THE APPROACH, AND WHY THIS ONE
The obvious move -- feed the model the bakery count instead of the food
count -- is wrong in a way that is easy to miss. The model learned
competitor counts at the INDUSTRY scale. Three bakeries looks like an
empty market to it, because three food businesses in a barangay WOULD be
an empty market; it has no idea the demand for bread is also a fraction
of the demand for food. Every sub-category would come back looking
wonderful.

So instead the industry count is SCALED by how dense this sub-category
is compared with what you would expect:

    expected direct competitors = industry count x expected share
    density ratio               = actual direct / expected direct
    adjusted competitor count   = industry count x density ratio

and the adjusted count -- still on the industry scale the model was
trained on -- goes through the model. Three bakeries where six would be
normal halves the effective competition; twelve where six would be
normal doubles it. The input stays in the range the model knows, and
the only thing that changed is the thing that was measured.

It also degrades honestly. With no measured sub-category count, the
direct figure IS the expected figure, the ratio is exactly 1, and the
score is identical to the industry score. The system does not pretend
to know something about bakeries it has not measured; it says so.

WHERE THE DIRECT COUNT COMES FROM, IN ORDER
  1. A stored measurement (subcategory_market_data): a live Google
     Places search for this sub-category, or a count derived from the
     LGU permit register's line-of-business column. Across the two,
     the larger wins -- the same max() rule market_data uses, because
     both undercount in different directions.
  2. A live Places search now, if live fetching is on and the request
     and day budgets allow. The result is stored so it is paid for once.
  3. An estimate: industry count x expected share. Labelled "estimated"
     everywhere it appears, and it never changes the score.

THE EXPECTED SHARE
Where at least MIN_BARANGAYS_FOR_SHARE barangays have a measured count
for this sub-category, the share is measured too: total direct / total
industry across those barangays. Otherwise it is an even split across
the industry's countable sub-categories -- a crude assumption, stated as
one, and the reason a measured share replaces it as soon as data exists.

THE CLAMP
The density ratio is held to [0.25, 2.5]. The lower bound encodes a
real point rather than a safety margin: a barangay with no bakeries at
all is not an empty market for a bakery, because every other food
business still competes for the same customers' spending. The upper
bound keeps one dense barangay from producing an input far outside
anything the model was trained on.
"""

from datetime import date

from app.extensions import db
from app.ml.subcategories import (
    OTHER_KEY,
    countable_subcategories,
    get_subcategory,
)

MIN_BARANGAYS_FOR_SHARE = 3
RATIO_FLOOR = 0.25
RATIO_CEILING = 2.5
MEASURED_SOURCES = ("Google Places API", "DTI")


def _measured_count(industry_type, subcategory, location):
    """(count, source) of the freshest measurement per source, larger
    across sources; (None, None) if nothing is on file."""
    from app.models import SubcategoryMarketData

    rows = (
        SubcategoryMarketData.query
        .filter_by(industry_type=industry_type, subcategory=subcategory, location=location)
        .order_by(SubcategoryMarketData.date_recorded.desc(), SubcategoryMarketData.id.desc())
        .all()
    )
    freshest = {}
    for row in rows:
        if row.source not in freshest:
            freshest[row.source] = row
    if not freshest:
        return None, None
    winner = max(freshest.values(), key=lambda r: (r.competitor_count, r.source == "Google Places API"))
    return int(winner.competitor_count), winner.source


def _live_count(industry_type, entry, location):
    """A live Places search for this sub-category, stored on success.
    None when live fetching is off, the budget is spent, there is no key,
    or the search fell back to simulated results -- a simulated count is
    never stored as a measurement."""
    try:
        from flask import current_app

        from app.services.forecasting_service import _can_spend_live_refresh
        from app.services.places_service import live_fetch_enabled, search_competitors_detailed

        if not entry.get("query") or not live_fetch_enabled():
            return None
        api_key = (current_app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()
        if not api_key or not _can_spend_live_refresh():
            return None

        result = search_competitors_detailed(location, industry_type, api_key=api_key,
                                             search_term=entry["query"])
        if result.get("simulated"):
            return None

        from app.models import SubcategoryMarketData

        count = len(result.get("results") or [])
        db.session.add(SubcategoryMarketData(
            industry_type=industry_type, subcategory=entry["key"], location=location,
            competitor_count=count, source="Google Places API", date_recorded=date.today(),
        ))
        db.session.commit()
        return count
    except Exception:  # noqa: BLE001 -- a failed lookup falls back to the estimate
        db.session.rollback()
        return None


def expected_share(industry_type, subcategory):
    """(share, basis) -- basis is "measured" or "even split"."""
    from app.models import SubcategoryMarketData
    from app.services.forecasting_service import reconciled_competitor_counts

    rows = (
        SubcategoryMarketData.query
        .filter_by(industry_type=industry_type, subcategory=subcategory)
        .order_by(SubcategoryMarketData.date_recorded.desc(), SubcategoryMarketData.id.desc())
        .all()
    )
    per_location = {}
    for row in rows:
        best = per_location.get(row.location)
        if best is None or row.competitor_count > best:
            per_location[row.location] = int(row.competitor_count)

    if len(per_location) >= MIN_BARANGAYS_FOR_SHARE:
        industry_counts = reconciled_competitor_counts([industry_type], list(per_location))
        direct_total = industry_total = 0
        for location, direct in per_location.items():
            industry = industry_counts.get((industry_type, location))
            if industry:
                direct_total += direct
                industry_total += industry
        if industry_total > 0 and direct_total > 0:
            return min(1.0, direct_total / industry_total), "measured"

    countable = countable_subcategories(industry_type)
    return (1.0 / len(countable) if countable else 1.0), "even split"


def direct_competition(industry_type, subcategory, location, industry_count, allow_live=True):
    """Everything the forecast and the recommendation need to know about
    direct competition. Returns None when the plan has no sub-category
    (or "other"), meaning: score at the industry level, unadjusted.
    """
    if not subcategory or subcategory == OTHER_KEY:
        return None
    entry = get_subcategory(industry_type, subcategory)
    if entry is None:
        return None

    industry_count = max(0, int(industry_count or 0))
    share, share_basis = expected_share(industry_type, subcategory)
    expected_direct = industry_count * share

    count, source = _measured_count(industry_type, subcategory, location)
    if count is None and allow_live:
        live = _live_count(industry_type, entry, location)
        if live is not None:
            count, source = live, "Google Places API"

    if count is None:
        # Nothing measured: the estimate IS the expectation, so the ratio
        # is exactly 1 and the score is left alone. Saying "we estimate"
        # while quietly changing the number would be the worst of both.
        return {
            "subcategory": subcategory,
            "label": entry["label"],
            "direct_count": int(round(expected_direct)),
            "is_estimated": True,
            "source": "estimated",
            "industry_count": industry_count,
            "expected_share": round(share, 3),
            "share_basis": share_basis,
            "density_ratio": 1.0,
            "adjusted_competitor_count": industry_count,
            "adjusts_score": False,
        }

    if expected_direct <= 0:
        ratio = 1.0 if count == 0 else RATIO_CEILING
    else:
        ratio = count / expected_direct
    ratio = max(RATIO_FLOOR, min(RATIO_CEILING, ratio))

    return {
        "subcategory": subcategory,
        "label": entry["label"],
        "direct_count": int(count),
        "is_estimated": False,
        "source": source,
        "industry_count": industry_count,
        "expected_share": round(share, 3),
        "share_basis": share_basis,
        "density_ratio": round(ratio, 2),
        "adjusted_competitor_count": int(round(industry_count * ratio)),
        "adjusts_score": industry_count > 0 and abs(ratio - 1.0) > 1e-9,
    }


def adjusted_scores(industry_type, location, scores, analysis):
    """The plan's scores after the direct-competition adjustment.

    `scores` is compute_scores()'s industry-level dict. Returns a copy
    with saturation_index / viability_score / cluster_label replaced by
    the adjusted figures, plus industry_saturation_index kept for the
    comparison the page shows. Confidence stays the industry run's: the
    adjustment changes one input, not how much the trees agree about
    the market.
    """
    result = dict(scores)
    result["industry_saturation_index"] = scores["saturation_index"]
    result["industry_viability_score"] = scores["viability_score"]
    result["industry_cluster_label"] = scores["cluster_label"]
    if not analysis or not analysis.get("adjusts_score"):
        return result

    from app.services.forecasting_service import _cluster_label_for, saturation_for_counts

    adjusted = saturation_for_counts([(industry_type, location, analysis["adjusted_competitor_count"])])[0]
    if adjusted is None:
        return result
    result["saturation_index"] = round(float(adjusted), 2)
    result["viability_score"] = round(max(0.0, min(10.0, (100.0 - adjusted) / 10.0)), 1)
    result["cluster_label"] = _cluster_label_for(adjusted)
    return result
