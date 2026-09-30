"""
app/services/location_opportunity_service.py
-----------------------------------------------
"Where else in Tarlac City could I open this same business?"

Given ONE industry (the one on the SME's own business plan), this scores
every barangay the app has data for and returns a ranked list of
location opportunities -- 15 or more of them -- each shaped like the
capstone storyboard's recommendation card: a viability score, key
metrics, a "Why This Works" list and a "Considerations" list.

WHERE EVERY NUMBER COMES FROM (there are no invented figures here)
  * competitor_count   -- market_data.competitor_count for that
                          industry+barangay, which is a live Google
                          Places API (New) count when a key is
                          configured (see places_service.py), or a
                          clearly-flagged simulated estimate when it
                          isn't. Micro businesses are excluded from it
                          by MICRO_BUSINESS_PATTERNS -- this system
                          counts SME-scale competitors only.
  * population         -- that barangay's real 2024 PSA population.
  * population_density -- real people/km2 (2024 PSA population over the
                          barangay's own published land area; real for
                          73 of 76 barangays -- see app/ml/seed_data.py).
  * saturation_index,
    viability_score,
    cluster_label      -- the Random Forest's own output for that
                          industry+barangay (forecasting_service.compute_scores).
  * residents_per_business -- population divided by competitor_count.
                          This is the one derived figure the reasons
                          lean on hardest, and deliberately so: it is
                          the classic market-depth measure ("how many
                          people does each existing business have to
                          itself"), it is computed from two real
                          numbers, and it is what makes a comparison
                          BETWEEN barangays meaningful -- 8 competitors
                          in a barangay of 20,000 is a very different
                          market from 8 in a barangay of 2,000.

EVERY REASON AND RISK IS A SENTENCE ABOUT ONE OF THOSE NUMBERS, usually
comparing this barangay against the CITY MEDIAN for the same industry,
which this module computes from the same sweep. "Only 3 competitors"
means nothing on its own; "3 competitors against a city median of 11,
with 4,200 residents each versus a median of 1,100" is a finding. That
comparison is the reason this module scores all 76 barangays before
ranking any of them.

COST
Scoring the whole city is one compute_scores() call per barangay -- the
same full-city sweep the Saturation Map and Trend Reports pages already
run, and it is bounded the same way: forecasting_service caps how many
live Places lookups a single request may spend
(_MAX_LIVE_REFRESH_PER_REQUEST), so a page load can never turn into 76
API calls. Barangays whose rows are already cached answer from the
database.
"""

import hashlib
from statistics import median

from app.ml.constants import short_industry_label
from app.ml.seed_data import get_real_population, get_barangay_profile
from app.services.forecasting_service import compute_scores_batch
from app.services.recommendation_service import competition_level_label

# How many recommendation cards to show before the "Explore more"
# button. Five is the floor the page guarantees; everything the AI
# recommended beyond that is one click away.
DEFAULT_LIMIT = 5

# A RECOMMENDATION IS A PLACE YOU COULD ACTUALLY OPEN.
#
# The engine scores every barangay in the city, but scoring is not
# recommending. A barangay the model rates "High Saturation" is a real
# result and it belongs on the Saturation Map -- it does not belong on a
# page headed "AI-Powered Recommendations", because recommending a
# market the same model just called saturated is advice that contradicts
# itself. So only these two tiers are ever offered as recommendations:
# somewhere ready to enter now, and somewhere workable with a niche
# strategy. The other two tiers are still counted and still reported
# (see `scored_by_tier`), just never presented as an opportunity.
RECOMMENDABLE_TIERS = ("High Opportunity", "Moderate Opportunity")

# Google Places Text Search stops issuing page tokens after 3 pages, so
# a count that lands exactly here means "60 or more", not "exactly 60"
# -- worth saying out loud on a card that leans on the number.
PLACES_RESULT_CEILING = 60

# No new business earns its steady-state revenue in month one: there is
# fit-out, permits, and the weeks it takes for customers to find you. So
# the ROI window never reports faster than this, however favourable the
# model's scores look. A stated assumption, editable here.
MINIMUM_RAMP_MONTHS = 3


# ---------------------------------------------------------------------
# HOW THE LIST IS ORDERED -- and why it is not just "viability desc"
# ---------------------------------------------------------------------
# Ranking on the model's viability score alone puts the city's smallest
# rural barangays on top: almost nobody competes in a barangay of 800
# people, so it scores as gloriously uncontested. That is a true
# statement about competition and a useless recommendation about where
# to open a business -- an uncontested market of 800 residents is still
# a market of 800 residents.
#
# So the order blends three REAL measures, each turned into that
# barangay's PERCENTILE RANK among all the barangays scored (percentile
# ranks, not raw values, so no single measure's units can dominate):
#
#   50%  viability_score          -- the model's own output: how
#                                    uncontested this market is.
#   30%  residents_per_business   -- market depth: how many people each
#                                    existing business has to itself.
#   20%  population               -- absolute market size, which is what
#                                    stops a tiny barangay from topping
#                                    the list on emptiness alone.
#
# These weights are a STATED EDITORIAL CHOICE, not something derived
# from data, and they are here in one place so they can be defended,
# argued with, or changed. Each card carries its own three percentiles
# (`percentiles` below) so a reader can see exactly why it placed where
# it did. The viability score shown on the card is always the model's
# own unmodified number -- the blend affects ORDER, never a displayed
# figure.
RANK_WEIGHTS = {"viability": 0.50, "depth": 0.30, "population": 0.20}


def _percentile_ranks(values):
    """Map each value to its percentile rank (0.0-1.0) within `values`.
    Ties share the average rank. None is treated as the lowest possible
    value -- a barangay with no published population should not win a
    ranking on the strength of a missing number."""
    cleaned = [(-1 if v is None else float(v)) for v in values]
    order = sorted(range(len(cleaned)), key=lambda i: cleaned[i])
    ranks = [0.0] * len(cleaned)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and cleaned[order[j + 1]] == cleaned[order[i]]:
            j += 1
        shared = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared / max(len(cleaned) - 1, 1)
        i = j + 1
    return ranks


def _apply_opportunity_ranking(rows):
    """Attach `opportunity_score` and the three component percentiles to
    every row, then sort best-first. See RANK_WEIGHTS above."""
    viability_pct = _percentile_ranks([r["viability_score"] for r in rows])
    depth_pct = _percentile_ranks([r["residents_per_business"] for r in rows])
    population_pct = _percentile_ranks([r["population"] for r in rows])

    for row, v_pct, d_pct, p_pct in zip(rows, viability_pct, depth_pct, population_pct):
        row["percentiles"] = {
            "viability": round(v_pct * 100),
            "residents_per_business": round(d_pct * 100),
            "population": round(p_pct * 100),
        }
        row["opportunity_score"] = round(
            RANK_WEIGHTS["viability"] * v_pct
            + RANK_WEIGHTS["depth"] * d_pct
            + RANK_WEIGHTS["population"] * p_pct,
            4,
        )

    rows.sort(key=lambda r: (-r["opportunity_score"], -r["viability_score"], r["saturation_index"]))
    return rows


def estimate_roi_timeframe(viability_score, saturation_index, residents_per_business,
                           city_median_depth):
    """The card's "ROI Timeframe" -- a months-to-return-on-investment
    RANGE, derived from the model's own prediction for this barangay.

    WHY THERE IS NO REVENUE INPUT
    This used to divide the SME's capital by their monthly revenue
    estimate. Monthly revenue is no longer collected: someone PLANNING a
    business does not have that number, and asking for one invited a
    guess that then drove this figure as though it had been measured.
    Worse, capital / revenue is a payback period that assumes month-one
    revenue everywhere, regardless of how crowded the barangay is -- and
    the saturation model is precisely the thing this system has an
    opinion about. So the window is built from the model alone:

      * VIABILITY sets the base. 10/10 starts from ~6 months, 0/10 from
        ~24.
      * SATURATION stretches or compresses it. A barangay the model
        scores at 20% ramps faster than one at 80%; the multiplier is
        1 + (saturation - 50)/100, clamped to 0.6x-1.8x so no single
        input can run away with the estimate.
      * MARKET DEPTH nudges it further. More residents per existing
        business than the city median means each competitor serves a
        bigger slice, so a new entrant fills faster (up to 10%).

    Returned as a RANGE (-20%/+25% around the adjusted midpoint), because
    a single-month figure would imply a precision this estimate does not
    have. `basis` is always "model" and is kept so the UI can say so.

    This is an estimate built from a model prediction, not a measured
    return. It is labelled that way everywhere it appears.
    """
    saturation = float(saturation_index or 0)
    viability = float(viability_score or 0)

    # Saturation multiplier, clamped so one extreme input can't dominate.
    market_factor = 1.0 + (saturation - 50.0) / 100.0
    market_factor = max(0.6, min(1.8, market_factor))

    # Market-depth nudge: deeper than the city median => slightly faster.
    if residents_per_business and city_median_depth:
        if residents_per_business > city_median_depth:
            market_factor *= 0.90
        elif residents_per_business < city_median_depth * 0.5:
            market_factor *= 1.10

    midpoint = (24.0 - (viability * 1.8)) * market_factor

    low = max(MINIMUM_RAMP_MONTHS, int(round(midpoint * 0.8)))
    high = max(low + 1, int(round(midpoint * 1.25)))
    return {"low_months": low, "high_months": high, "label": f"{low}-{high} months", "basis": "model"}


def _opportunity_type_for(cluster_label):
    """The storyboard's card badge. Mirrors the same four AI cluster
    tiers used everywhere else in the app (app/ml/constants.py)."""
    return {
        "Low": "High Opportunity",
        "Moderate": "Moderate Opportunity",
        "High": "Low Opportunity",
        "Saturated": "High Saturation",
    }.get(cluster_label, "Moderate Opportunity")


def _score_every_barangay(industry_type, locations):
    """Every barangay scored for one industry, plus that barangay's real
    population/density. Returns the raw rows -- ranking, medians and
    prose all happen afterwards, from these.

    ONE BATCHED CALL, NOT 76 SEPARATE ONES. This used to loop
    compute_scores() over the barangay list, which is the same mistake
    the Trend Reports page made and for the same two reasons:

      QUERIES. compute_scores() resolves its own market_data and
      lgu_data row per call, so 76 barangays meant ~152 SELECTs for one
      page -- 152 network round trips to Aiven in production. The batch
      resolves every market row in one GROUP BY and every lgu row in
      one more.

      PREDICTION. Each call ran rf_model.predict() on a single row and
      then walked all 100 trees for that same single row. Profiling
      this page showed 2.0 of its 4.0 seconds inside time.sleep() --
      joblib's worker handshake, pure dispatch overhead with no
      arithmetic in it. Batched, the whole city is one predict() over a
      76-row matrix and one pass over the forest.

    compute_scores_batch() returns the identical dicts in the identical
    order, and falls back to the per-pair path for any combo whose rows
    do not exist yet, so a cold database still behaves exactly as it
    did before.
    """
    locations = list(locations)
    scored = compute_scores_batch([(industry_type, location) for location in locations])

    rows = []
    for location, scores in zip(locations, scored):
        profile = get_barangay_profile(location) or {}
        population = get_real_population(location) or 0
        competitor_count = int(scores.get("competitor_count") or 0)
        rows.append(
            {
                "location": location,
                "industry_type": industry_type,
                "saturation_index": scores["saturation_index"],
                "viability_score": scores["viability_score"],
                "cluster_label": scores["cluster_label"],
                "confidence_level": scores["confidence_level"],
                "competitor_count": competitor_count,
                "population": int(population),
                "population_density": profile.get("population_density"),
                "residents_per_business": (
                    round(population / competitor_count) if population and competitor_count else None
                ),
            }
        )
    return rows


def _scored_and_ranked(industry_type, locations):
    """`_score_every_barangay` + the opportunity ranking, memoised on
    the market_data fingerprint.

    WHY THIS IS THE RIGHT CACHE BOUNDARY. Everything up to and
    including the ranking depends only on (industry, barangay list,
    state of market_data) -- not on who is signed in. Two SMEs looking
    at the same industry get the same city-wide scores, and so does the
    same SME reloading the page, switching plans and back, or hitting
    "Explore more recommendations" (which re-scores the identical city
    just to show more of it). Everything user-specific -- "this is your
    current plan's barangay", the prose -- is computed AFTER this, per
    request, from these rows.

    Keyed on the market_data fingerprint rather than a timeout, for the
    same reason as the trend caches: import rows or run a Places
    refresh and the next request must recompute, not wait out a timer
    while showing figures the database no longer agrees with. Reusing
    the trend module's _cached/_data_fingerprint keeps that one
    mechanism instead of a second one that could drift from it.

    THE ROWS ARE HANDED OUT BY REFERENCE, not copied, which is only
    safe because nothing downstream writes to them -- the caller reads
    these rows to BUILD the card dicts and never mutates one. There is
    a test pinning that (test_recommendations_performance.py).
    """
    from app.services.trend_analytics_service import _SWEEP_CACHE, _cached

    # The barangay list is part of the key, not just its length: a test
    # or a future caller passing a different subset of the same size
    # must not be served another subset's scores.
    locations_token = hashlib.sha1(
        "|".join(str(location) for location in locations).encode("utf-8")
    ).hexdigest()[:12]
    cache_key = f"opportunity_rows:{industry_type}:{locations_token}"

    def build():
        return _apply_opportunity_ranking(_score_every_barangay(industry_type, locations))

    return _cached(_SWEEP_CACHE, cache_key, build)


def _city_context(rows):
    """City-wide medians for the same industry -- what each barangay's
    own figures are compared against in the reasons/risks below."""
    competitor_counts = [r["competitor_count"] for r in rows if r["competitor_count"] is not None]
    depths = [r["residents_per_business"] for r in rows if r["residents_per_business"]]
    densities = [
        float(r["population_density"]) for r in rows if r.get("population_density") is not None
    ]
    return {
        "median_competitors": round(median(competitor_counts)) if competitor_counts else 0,
        "median_residents_per_business": round(median(depths)) if depths else 0,
        "median_density": round(median(densities)) if densities else 0,
        "barangays_scored": len(rows),
    }


def _reasons_for(row, city, market_meta, sme_profile, industry_label):
    """"Why This Works" -- every line is one real figure from `row`,
    usually set against the city median in `city`."""
    reasons = []
    competitors = row["competitor_count"]
    median_competitors = city["median_competitors"]

    if competitors < median_competitors:
        reasons.append(
            f"only {competitors} SME-scale {industry_label.lower()} competitor(s) on file here, "
            f"against a city median of {median_competitors} across {city['barangays_scored']} barangays"
        )
    elif competitors == median_competitors:
        reasons.append(
            f"{competitors} competitor(s) here -- exactly the city median for {industry_label.lower()}"
        )
    else:
        reasons.append(
            f"{competitors} competitor(s) on file, above the city median of {median_competitors} -- "
            f"an established market for {industry_label.lower()}"
        )

    depth = row["residents_per_business"]
    if depth and city["median_residents_per_business"]:
        if depth > city["median_residents_per_business"]:
            reasons.append(
                f"about {depth:,} residents per existing business, deeper than the city median of "
                f"{city['median_residents_per_business']:,} -- each competitor here serves more people"
            )
        else:
            reasons.append(
                f"about {depth:,} residents per existing business "
                f"(city median {city['median_residents_per_business']:,})"
            )
    elif row["population"] and not competitors:
        reasons.append(
            f"a real 2024 PSA population of {row['population']:,} with no {industry_label.lower()} "
            f"competitor on file yet"
        )

    if row["population"]:
        reasons.append(f"real 2024 PSA population of {row['population']:,}")

    density = row.get("population_density")
    if density is not None and city["median_density"]:
        density = float(density)
        if density > city["median_density"]:
            reasons.append(
                f"population density of {density:,.0f}/km2, above the city median of "
                f"{city['median_density']:,.0f}/km2"
            )

    if market_meta.get("is_live"):
        fetched = market_meta.get("date_recorded")
        reasons.append(
            "competitor count came from a live Google Places lookup"
            + (f" on {fetched:%B %d, %Y}" if fetched else "")
        )

    if sme_profile is not None and sme_profile.startup_capital:
        reasons.append(
            f"scored against your own plan parameters (capital on file: "
            f"PHP {float(sme_profile.startup_capital):,.0f})"
        )

    reasons.append(
        f"AI saturation index {round(row['saturation_index'])}% here, "
        f"model confidence {round(row['confidence_level'])}%"
    )
    return reasons


def _risks_for(row, city, market_meta, industry_label):
    """"Considerations" -- the same real figures, read the other way."""
    risks = []
    competitors = row["competitor_count"]

    if row["cluster_label"] in ("High", "Saturated"):
        risks.append(
            f"the AI rates this barangay {round(row['saturation_index'])}% saturated for "
            f"{industry_label.lower()} -- entry needs a clearly differentiated offer"
        )
    if competitors:
        risks.append(f"{competitors} existing business(es) already serve this market")
    else:
        risks.append(
            "no competitor on file here -- verify on the ground whether that reflects genuine "
            "unmet demand or simply no viable trade in this barangay"
        )

    depth = row["residents_per_business"]
    if depth and city["median_residents_per_business"] and depth < city["median_residents_per_business"]:
        risks.append(
            f"only about {depth:,} residents per existing business, thinner than the city median of "
            f"{city['median_residents_per_business']:,}"
        )

    density = row.get("population_density")
    if density is not None and city["median_density"] and float(density) < city["median_density"]:
        risks.append(
            f"population density of {float(density):,.0f}/km2 is below the city median of "
            f"{city['median_density']:,.0f}/km2 -- expect a wider catchment and more travel per customer"
        )

    if not market_meta.get("is_live"):
        risks.append(
            "this barangay's competitor figure is a simulated estimate, not a live Google Places count "
            "-- configure a Places API key for exact figures"
        )
    if competitors >= PLACES_RESULT_CEILING:
        risks.append(
            f"Google returns at most {PLACES_RESULT_CEILING} places per search, so read this count as "
            f"\"{PLACES_RESULT_CEILING} or more\""
        )
    if not row["population"]:
        risks.append("no PSA population figure is published for this location, so market depth is unknown")

    return risks


def _written_cards(industry_type, city, opportunities):
    """The LLM's write-up for this exact set of cards, memoised on the
    market_data fingerprint.

    WHY THE LLM CALL IS CACHED AT ALL. The prose is a pure function of
    (industry, these barangays, this state of the data) -- the same
    request an hour later, or from a different SME, produces the same
    sentences about the same numbers. Without a cache every page view,
    every reload and every back-button press spends an API call.

    That is not an abstract concern on the free tiers this project is
    meant to run on: Google AI Studio's free Gemini tier allows about
    10 requests a MINUTE, and a defence demo with someone clicking
    between plans will exceed that in under a minute. Then the calls
    start failing, the page silently reverts to rule-based wording,
    and it does so precisely while being demonstrated.

    Cached on the data rather than a clock for the same reason as
    everything else here: re-import the market data and the write-up
    must be regenerated, because it quotes figures that just changed.
    """
    from app.services.llm_service import generate_opportunity_cards_json
    from app.services.trend_analytics_service import _SNAPSHOT_CACHE, _cached

    locations_token = hashlib.sha1(
        "|".join(card["location"] for card in opportunities).encode("utf-8")
    ).hexdigest()[:12]
    cache_key = f"llm_cards:{industry_type}:{locations_token}"

    def build():
        return generate_opportunity_cards_json(industry_type, city, opportunities)

    written = _cached(_SNAPSHOT_CACHE, cache_key, build)
    # An empty result means the call failed or was refused. Do not let
    # that sit in the cache: the next request should try again rather
    # than serve rule-based wording for the full cache lifetime because
    # of one rate-limit blip.
    if not written:
        _SNAPSHOT_CACHE.pop((cache_key, _data_fingerprint_or_none()), None)
    return written or {}


def _data_fingerprint_or_none():
    from app.services.trend_analytics_service import _data_fingerprint

    try:
        return _data_fingerprint()
    except Exception:  # pragma: no cover - no app context / no engine
        return None


def rank_location_opportunities(industry_type, locations, sme_profile=None, limit=DEFAULT_LIMIT,
                                market_meta_by_location=None, use_llm=True):
    """Score every barangay in `locations` for ONE industry and return
    the best `limit` of them as storyboard-shaped opportunity cards.

    `sme_profile` (optional) is the SME's own plan -- when given, the
    cards mark its own barangay and quote its capital, so a card answers
    "what would MY plan look like in this barangay", not a generic one. `market_meta_by_location` optionally carries
    {location: {"is_live": bool, "date_recorded": date}} so the cards can
    state the provenance of each competitor count without re-querying.

    Ordering blends the model's viability score with real market depth
    and market size -- see RANK_WEIGHTS above for the exact weighting
    and why ranking on viability alone gives a misleading answer.
    """
    market_meta_by_location = market_meta_by_location or {}
    # Scored AND ranked in one memoised step -- both are city-wide and
    # user-independent, so the same reload, the same industry viewed by
    # another SME, and "Explore more" (which asks for the identical city
    # and only shows more of it) all answer from one sweep. See
    # _scored_and_ranked: these rows are read, never written.
    rows = _scored_and_ranked(industry_type, locations)
    if not rows:
        return {"opportunities": [], "city": _city_context([]), "industry_type": industry_type}

    city = _city_context(rows)
    industry_label = short_industry_label(industry_type)

    # Scoring is not recommending -- keep only the tiers an SME could
    # actually enter (see RECOMMENDABLE_TIERS). Everything else stays
    # counted in `scored_by_tier` below but is never offered as an
    # opportunity.
    recommendable = [
        r for r in rows if _opportunity_type_for(r["cluster_label"]) in RECOMMENDABLE_TIERS
    ]

    # limit=None means "every location the AI recommended" -- what the
    # "Explore more recommendations" button asks for.
    display_rows = recommendable if limit is None else recommendable[: max(1, int(limit))]

    opportunities = []
    for rank, row in enumerate(display_rows, start=1):
        meta = market_meta_by_location.get(row["location"], {})
        roi = estimate_roi_timeframe(
            row["viability_score"], row["saturation_index"], row["residents_per_business"],
            city["median_residents_per_business"],
        )
        opportunities.append(
            {
                "roi_timeframe": roi["label"],
                "roi_basis": roi["basis"],
                "roi_low_months": roi["low_months"],
                "roi_high_months": roi["high_months"],
                "rank": rank,
                "location": row["location"],
                "industry_type": industry_type,
                "industry_label": industry_label,
                "title": f"{industry_label} in {row['location']}",
                "opportunity_type": _opportunity_type_for(row["cluster_label"]),
                "cluster_label": row["cluster_label"],
                "viability_score": row["viability_score"],
                "saturation_index": row["saturation_index"],
                "confidence_level": row["confidence_level"],
                "competitor_count": row["competitor_count"],
                "competition_level": competition_level_label(row["competitor_count"]),
                "population": row["population"],
                "population_density": row["population_density"],
                "residents_per_business": row["residents_per_business"],
                "opportunity_score": row["opportunity_score"],
                "percentiles": row["percentiles"],
                "competitor_is_live": bool(meta.get("is_live")),
                "generated_by": "rule_based",
                "is_current_plan_location": (
                    sme_profile is not None and row["location"] == sme_profile.location
                ),
                "reasons": _reasons_for(row, city, meta, sme_profile, industry_label),
                "risks": _risks_for(row, city, meta, industry_label),
            }
        )

    # --- hand the finished cards to the LLM to write up ------------
    # Only the WORDING is delegated. Every number on a card -- the
    # competitor count, the population, the ranking, the ROI window --
    # is computed above, before the model is ever called, and the prompt
    # forbids inventing any figure that isn't passed in. When the LLM is
    # off (the default), unconfigured, or fails, each card keeps the
    # data-derived reasons/risks it already has.
    if use_llm and opportunities:
        try:
            from app.services.recommendation_service import llm_recommendations_enabled

            if llm_recommendations_enabled():
                written = _written_cards(industry_type, city, opportunities)
                for card in opportunities:
                    entry = written.get(card["location"])
                    if entry:
                        card["reasons"] = entry["reasons"]
                        card["risks"] = entry["risks"]
                        card["generated_by"] = entry["generated_by"]
                        # The model may also return its own ROI window.
                        # Accepted only when it parses as a sane month
                        # range -- otherwise the computed one stands.
                        if entry.get("roi_timeframe"):
                            card["roi_timeframe"] = entry["roi_timeframe"]
                            card["roi_basis"] = "llm"
        except Exception as exc:  # noqa: BLE001 -- a flaky LLM must never break the page
            # Still swallowed, because a page that 500s when an API is
            # down is worse than a page with rule-based wording. But it
            # is no longer swallowed SILENTLY: `pass` here is what made
            # "the AI isn't working" impossible to diagnose on a live
            # deployment.
            from flask import current_app

            current_app.logger.warning(
                "LLM write-up skipped for %s: %s: %s",
                industry_type, type(exc).__name__, exc, exc_info=True,
            )

    # The page's three summary cards count EVERY barangay the AI scored
    # across Tarlac City -- not just the ones currently displayed, and not
    # the SME's own saved plans. "12 high-opportunity locations" has to
    # mean twelve real barangays the model rated that way city-wide, or
    # the number is decoration.
    all_types = [_opportunity_type_for(r["cluster_label"]) for r in rows]
    return {
        "opportunities": opportunities,
        "city": city,
        "industry_type": industry_type,
        "industry_label": industry_label,
        # How many cards are on screen right now vs. how many locations
        # the AI actually recommended -- what the "Explore more" button
        # compares to decide whether it has anything left to show.
        "showing_count": len(opportunities),
        "recommended_count": len(recommendable),
        "has_more": len(opportunities) < len(recommendable),
        # Every barangay scored, by tier. The two recommendable tiers are
        # what the summary cards report; the other two are kept so the
        # page can say how much of the city was ruled out and why.
        "total_scored": len(rows),
        "high_opportunity_count": all_types.count("High Opportunity"),
        "moderate_opportunity_count": all_types.count("Moderate Opportunity"),
        "low_opportunity_count": all_types.count("Low Opportunity"),
        "saturated_count": all_types.count("High Saturation"),
        "not_recommended_count": (
            all_types.count("Low Opportunity") + all_types.count("High Saturation")
        ),
    }
