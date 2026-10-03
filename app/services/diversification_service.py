"""
app/services/diversification_service.py
-----------------------------------------
THE LGU DIVERSIFICATION PLAN -- the most useful thing this system can
hand a city planner: which barangays depend on too few industries, which
industries the city should encourage or slow down, and where.

Everything is computed from data already on file, in one pass:

  * the freshest, cross-source-reconciled business count per
    (industry, barangay) -- trend_analytics_service.latest_market_data_by_key,
    the same figures the Trend Reports and the map read;
  * each pair's market saturation from the trained market model
    (forecasting_service.saturation_for_counts -- ONE matrix through the
    forest, and it never creates a row: this is a report, not a fetch);
  * the real 2024 PSA population of each barangay.

THE MEASURES, and why each one

  Diversity score (0-100)   How evenly a barangay's businesses spread
                            across industries. From the Herfindahl-
                            Hirschman Index of industry shares,
                            HHI = sum(share^2), normalised so 0 means one
                            industry holds everything and 100 means every
                            industry holds an equal share:
                              diversity = (1 - HHI) / (1 - 1/N) * 100,
                            N = the number of industries the system has
                            counts for in that barangay (an industry with
                            no row yet is unknown, not zero), scored only
                            once N >= MIN_INDUSTRIES_ON_FILE. HHI is the
                            standard concentration measure economists and
                            competition regulators use, so the figure is
                            defensible, not invented.
  Dominant industry         The largest share, and how large. A barangay
                            where one industry holds over half of all
                            businesses is exposed to that one market.
  Businesses per 1,000      Business access. Few businesses for many
  residents                 residents is an underserved community.
  Promote / Limit lists     Per barangay: the industries the model scores
                            as having the most room (lowest saturation,
                            preferring ones the barangay barely has) and
                            those already saturated there (above the
                            "Saturated" cut, CLUSTER_THRESHOLDS[2]).
  Industry stance           City-wide, per industry: ENCOURAGE (clearly
                            more room than the city's typical industry),
                            MONITOR, or REGULATE (crowded in many
                            barangays) -- with the counts behind it.
  Priority barangays        Where intervention pays most: low diversity
                            AND/OR low business access, ranked by a simple
                            need score that is printed next to them.

All thresholds are named constants below, so the panel can see -- and
question -- exactly where each line is drawn.
"""

from app.ml.constants import BUSINESS_TYPES, CLUSTER_THRESHOLDS, short_industry_label
from app.ml.seed_data import BARANGAY_NAMES, get_real_population

# A barangay where one industry holds at least this share is "dependent"
# on it.
DOMINANCE_SHARE = 0.40
# Diversity below this is "low" (most of the activity is in a few
# industries).
LOW_DIVERSITY = 45.0
# Saturation at or above this is "saturated" -- the model's own top tier.
SATURATED_AT = CLUSTER_THRESHOLDS[2]
# ...and at or below this is open ("Low" tier).
OPEN_AT = CLUSTER_THRESHOLDS[0]
# City-wide industry stance. REGULATE: saturated in at least
# REGULATE_SHARE of the barangays that have it, or an average saturation
# of REGULATE_AVG or more. ENCOURAGE: an average saturation at least
# ENCOURAGE_MARGIN points below the city's median industry, with fewer
# than ENCOURAGE_MAX_SATURATED of its barangays saturated -- RELATIVE to
# the city, because a fixed cut-off says "monitor everything" in a city
# where every industry sits in the same band, which helps no one choose.
REGULATE_SHARE = 0.30
REGULATE_AVG = 60.0
ENCOURAGE_MARGIN = 2.0
ENCOURAGE_MAX_SATURATED = 0.15
PRIORITY_LIMIT = 10
# A barangay's diversity is only scored once the system has counts for at
# least this many of its industries. With two or three on file the score
# would describe the gaps in the data, not the barangay's economy.
MIN_INDUSTRIES_ON_FILE = 5


def _hhi(counts):
    total = sum(counts)
    if total <= 0:
        return None
    return sum((c / total) ** 2 for c in counts)


def diversity_score(counts, n_industries=None):
    """0-100 from the industry counts of one area -- see the module note."""
    n = n_industries or len(BUSINESS_TYPES)
    hhi = _hhi(counts)
    if hhi is None or n <= 1:
        return None
    return round(max(0.0, min(100.0, (1 - hhi) / (1 - 1 / n) * 100)), 1)


def build_diversification_plan():
    """The whole plan as one JSON-safe dict -- see the module docstring."""
    from app.services.forecasting_service import saturation_for_counts
    from app.services.trend_analytics_service import latest_market_data_by_key

    latest = latest_market_data_by_key()
    counts = {}  # barangay -> {industry: count}
    for (industry, location), row in latest.items():
        if location not in BARANGAY_NAMES or industry not in BUSINESS_TYPES:
            continue
        counts.setdefault(location, {})[industry] = int(row.competitor_count or 0)

    pairs = [(industry, location, count)
             for location, by_industry in counts.items()
             for industry, count in by_industry.items()]
    saturations = dict(zip(((i, l) for i, l, _c in pairs), saturation_for_counts(pairs)))

    city_counts = {industry: 0 for industry in BUSINESS_TYPES}
    barangays = []
    for location in BARANGAY_NAMES:
        by_industry = counts.get(location, {})
        total = sum(by_industry.values())
        for industry, count in by_industry.items():
            city_counts[industry] += count
        population = get_real_population(location) or 0
        scored = [
            {"industry": industry, "short": short_industry_label(industry), "count": count,
             "saturation": round(float(saturations[(industry, location)]), 1)}
            for industry, count in by_industry.items()
            if saturations.get((industry, location)) is not None
        ]
        dominant = max(by_industry.items(), key=lambda kv: kv[1]) if total else None
        dominant_share = (dominant[1] / total) if dominant else 0.0
        # Promote: the most room first; among similar room, the industry
        # this barangay has least of -- that is what diversifies it.
        promote = sorted(
            (s for s in scored if s["saturation"] < SATURATED_AT),
            key=lambda s: (round(s["saturation"] / 5), s["count"]),
        )[:3]
        limit = sorted((s for s in scored if s["saturation"] >= SATURATED_AT),
                       key=lambda s: -s["saturation"])[:3]
        on_file = len(by_industry)
        enough = on_file >= MIN_INDUSTRIES_ON_FILE
        per_thousand = round(total / population * 1000, 1) if population and enough else None
        barangays.append({
            "location": location,
            "population": population,
            "businesses": total,
            "industries_present": sum(1 for c in by_industry.values() if c > 0),
            "industries_on_file": on_file,
            "per_thousand": per_thousand,
            # Evenness across the industries the system HAS counts for:
            # an industry with no row yet is unknown, not zero.
            "diversity": diversity_score(list(by_industry.values()), n_industries=on_file) if enough else None,
            "dominant": short_industry_label(dominant[0]) if dominant else None,
            "dominant_full": dominant[0] if dominant else None,
            "dominant_share": round(dominant_share * 100, 1),
            "promote": promote,
            "limit": limit,
        })

    # Business access, relative to the city's own median, so "low" means
    # low for Tarlac City rather than against an imported benchmark.
    access = sorted(b["per_thousand"] for b in barangays if b["per_thousand"] is not None)
    median_access = access[len(access) // 2] if access else None

    for b in barangays:
        reasons, need = [], 0.0
        if b["diversity"] is not None and b["diversity"] < LOW_DIVERSITY:
            reasons.append(f"low diversity ({b['diversity']}/100)")
            need += (LOW_DIVERSITY - b["diversity"]) / LOW_DIVERSITY
        if b["diversity"] is not None and b["dominant_share"] >= DOMINANCE_SHARE * 100 and b["dominant"]:
            reasons.append(f"{b['dominant']} holds {b['dominant_share']}% of its businesses")
            need += (b["dominant_share"] / 100 - DOMINANCE_SHARE)
        if median_access and b["per_thousand"] is not None and b["per_thousand"] < median_access / 2:
            reasons.append(f"only {b['per_thousand']} businesses per 1,000 residents "
                           f"(city median {median_access})")
            need += 1 - b["per_thousand"] / median_access
        b["need_score"] = round(need * 100, 1)
        b["reasons"] = reasons
        actions = []
        if b["promote"]:
            actions.append("Encourage new " + ", ".join(p["short"] for p in b["promote"]) + " businesses")
        if b["limit"]:
            actions.append("Review new permits for " + ", ".join(p["short"] for p in b["limit"])
                           + " (already saturated here)")
        if b["industries_on_file"] < MIN_INDUSTRIES_ON_FILE:
            actions.append(f"Only {b['industries_on_file']} of {len(BUSINESS_TYPES)} industries counted here yet: "
                           "upload this barangay's permit register for a full picture")
        b["actions"] = actions

    priority = sorted((b for b in barangays if b["reasons"]), key=lambda b: -b["need_score"])[:PRIORITY_LIMIT]

    # City-wide stance per industry -- see the thresholds at the top.
    industries = []
    city_total = sum(city_counts.values())
    averages = []
    for industry in BUSINESS_TYPES:
        sats = [saturations[(industry, b["location"])] for b in barangays
                if saturations.get((industry, b["location"])) is not None]
        if sats:
            averages.append(sum(sats) / len(sats))
    averages.sort()
    city_median = averages[len(averages) // 2] if averages else None
    for industry in BUSINESS_TYPES:
        sats = [saturations[(industry, b["location"])] for b in barangays
                if saturations.get((industry, b["location"])) is not None]
        saturated = sum(1 for s in sats if s >= SATURATED_AT)
        open_ = sum(1 for s in sats if s <= OPEN_AT)
        avg = round(sum(sats) / len(sats), 1) if sats else None
        if sats and (saturated / len(sats) >= REGULATE_SHARE or avg >= REGULATE_AVG):
            stance = "Regulate"
        elif (sats and city_median is not None and avg <= city_median - ENCOURAGE_MARGIN
              and saturated / len(sats) < ENCOURAGE_MAX_SATURATED):
            stance = "Encourage"
        else:
            stance = "Monitor"
        industries.append({
            "industry": industry,
            "short": short_industry_label(industry),
            "businesses": city_counts[industry],
            "share": round(city_counts[industry] / city_total * 100, 1) if city_total else 0.0,
            "avg_saturation": avg,
            "saturated_barangays": saturated,
            "open_barangays": open_,
            "barangays_scored": len(sats),
            "stance": stance,
        })
    industries.sort(key=lambda i: -i["businesses"])

    city_diversity = diversity_score(list(city_counts.values()))
    scored_barangays = [b for b in barangays if b["diversity"] is not None]
    low_div = [b for b in scored_barangays if b["diversity"] < LOW_DIVERSITY]
    encourage = [i for i in industries if i["stance"] == "Encourage"]
    regulate = [i for i in industries if i["stance"] == "Regulate"]

    summary = []
    if city_diversity is not None:
        summary.append(
            f"Tarlac City's economy scores {city_diversity}/100 for diversity across "
            f"{len(BUSINESS_TYPES)} industries ({city_total:,} businesses on file)."
        )
    unscored = len(barangays) - len(scored_barangays)
    if unscored:
        summary.append(f"{unscored} barangay(s) have counts for fewer than {MIN_INDUSTRIES_ON_FILE} industries, "
                       "so their diversity is not scored yet; uploading their permit registers fills the gap.")
    if low_div:
        summary.append(f"{len(low_div)} of {len(scored_barangays)} scored barangays have low diversity "
                       f"(below {LOW_DIVERSITY:.0f}/100) and depend on a few industries.")
    if encourage:
        summary.append("Industries with more room than the city's typical industry, worth encouraging: "
                       + ", ".join(i["short"] for i in encourage[:5]) + ".")
    if regulate:
        summary.append("Industries already saturated in many barangays, worth regulating: "
                       + ", ".join(i["short"] for i in regulate[:5]) + ".")
    if priority:
        summary.append("Start with " + ", ".join(b["location"] for b in priority[:3])
                       + "; they score highest on need below.")

    return {
        "city_diversity": city_diversity,
        "city_businesses": city_total,
        "median_access": median_access,
        "barangays": sorted(barangays, key=lambda b: (b["diversity"] is None, b["diversity"] or 0)),
        "priority": priority,
        "industries": industries,
        "summary": summary,
        "city_median_saturation": round(city_median, 1) if city_median is not None else None,
        "thresholds": {
            "min_industries": MIN_INDUSTRIES_ON_FILE,
            "regulate_avg": REGULATE_AVG,
            "encourage_margin": ENCOURAGE_MARGIN,
            "encourage_max_saturated_percent": round(ENCOURAGE_MAX_SATURATED * 100),
            "low_diversity": LOW_DIVERSITY,
            "dominance_percent": round(DOMINANCE_SHARE * 100),
            "saturated_at": SATURATED_AT,
            "open_at": OPEN_AT,
            "regulate_share_percent": round(REGULATE_SHARE * 100),

        },
    }
