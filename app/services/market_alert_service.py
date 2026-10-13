"""
app/services/market_alert_service.py
---------------------------------------
Automatic alerts when the market actually moves.

WHAT WAS THERE BEFORE, AND WHY IT WAS NOT ENOUGH

One alert existed: _maybe_fire_early_warning() in forecasting_service,
which fires when an SME's own forecast is regenerated and comes back
above the saturation threshold. Two limits made it the wrong shape for
"tell me when the market changes":

  * It is about a LEVEL, not a CHANGE. A barangay that has been 80%
    saturated for a year fires every single time; one that jumps from
    30% to 70% -- the genuinely alarming case -- fires nothing until it
    crosses the line.
  * It only runs when that one SME's forecast happens to be recomputed.
    Nobody is told anything because the CITY changed.

This module answers the other question: what moved, who does it affect,
and tell them. It compares each (industry, barangay) against its own
previous recorded state, so the subject is the delta.

WHAT COUNTS AS A FLUCTUATION

A move has to clear two bars, and the second one is what keeps this
from becoming noise nobody reads:

  1. The saturation index moved by at least MIN_SATURATION_DELTA
     points. Small drift is the data breathing, not news.
  2. The competitor count actually changed. A saturation move with no
     underlying change in competitors is the model reacting to a
     revised population or rent figure -- real, but not "the market
     moved", and an alert that cannot point at a cause is one people
     learn to ignore.

     Worth being precise about where that guarantee comes from, since
     it moved. It is now structural rather than a filter: both figures
     are predicted from the SAME (newest) row, varying only the
     competitor count, so a pure feature revision cannot produce a
     delta at all. Bar 2 survives as the thing that keeps unchanged
     pairs out of the scoring matrix entirely -- on a city-wide
     re-import that is most of the city -- not as the last line of
     defence it used to be.

Crossing a cluster boundary (Low -> Moderate -> High -> Saturated) is
reported below that bar, because the tier is what the rest of the app
shows and what an SME acts on -- but not at ANY size. A combination
sitting at 49.8 would otherwise cross into "High" when one shop opens
and back to "Moderate" when one closes, alerting forever over a
one-point move. MIN_TIER_CROSS_DELTA is the floor that stops it.

Both saturation figures are PREDICTED from the two recorded competitor
counts, holding every other feature at the row's recorded values, so
the difference is attributable to the competitor count and nothing
else. The detector reads; it never writes a market_data row.

WHO HEARS ABOUT IT
  * An SME hears about the barangays and industries their own saved
    plans cover. Nothing else -- a coffee-shop plan in San Roque has
    no use for a hardware-store move in Matatalaib.
  * LGU and Admin accounts hear the city-wide summary, because that is
    their job.

HOW IT RUNS WITHOUT A SCHEDULER
Render's free tier sleeps and has no cron, so there is no background
worker to lean on. The sweep runs when the DATA CHANGES instead -- an
LGU upload, a Places refresh -- which is also the only time it could
have anything new to say. A cooldown stops a burst of uploads from
producing a burst of emails.
"""

from datetime import datetime, timedelta

from app.extensions import db
from app.models import MarketData, Notification, SystemSetting
from app.models.user import User

MIN_SATURATION_DELTA = 10.0

MIN_TIER_CROSS_DELTA = 3.0

ALERT_COOLDOWN_MINUTES = 60
LAST_SWEEP_KEY = "market_alert_last_sweep"

MAX_REPORTED_MOVES = 5


def _cluster_for(saturation):
    from app.ml.constants import CLUSTER_LABELS_ORDERED, CLUSTER_THRESHOLDS

    for label, threshold in zip(CLUSTER_LABELS_ORDERED, CLUSTER_THRESHOLDS):
        if saturation <= threshold:
            return label
    return CLUSTER_LABELS_ORDERED[-1]


def is_reportable(delta, was_cluster, now_cluster):
    """The rule for "is this move worth telling somebody about?".

    Pulled out of detect_fluctuations() so it can be tested for what it
    is -- a threshold decision with three branches -- rather than only
    through whatever saturation figures the model happens to produce
    for a given pair of competitor counts. Testing it through the
    detector meant the boundary cases were never actually exercised:
    the assertions passed because the numbers never landed near a tier
    edge, not because the rule was right.
    """
    moved = abs(delta)
    if moved >= MIN_SATURATION_DELTA:
        return True
    if was_cluster != now_cluster and moved >= MIN_TIER_CROSS_DELTA:
        return True
    return False


def detect_fluctuations(limit_pairs=None):
    """Material moves since each combination's previous recorded state.

    Returns a list of dicts, biggest move first. Reads only
    market_data -- no model run per pair, because the comparison is
    between two recorded competitor counts and the saturation each
    implies, and a full sweep per upload would be the slowest thing in
    the app.
    """
    from app.services.forecasting_service import saturation_for_counts

    rows = (
        db.session.query(
            MarketData.industry_type, MarketData.location,
            MarketData.competitor_count, MarketData.date_recorded, MarketData.market_id,
        )
        .filter(MarketData.competitor_count.isnot(None))
        .order_by(MarketData.industry_type, MarketData.location,
                  MarketData.date_recorded.desc(), MarketData.market_id.desc())
        .all()
    )

    history = {}
    for industry, location, count, recorded, market_id in rows:
        entries = history.setdefault((industry, location), [])
        if len(entries) < 2:
            entries.append((int(count), recorded))

    changed = {
        pair: entries for pair, entries in history.items()
        if len(entries) == 2 and entries[0][0] != entries[1][0]
    }
    if not changed:
        return []

    pairs = list(changed)
    if limit_pairs:
        pairs = pairs[:limit_pairs]

    scored = saturation_for_counts(
        [(industry, location, changed[(industry, location)][0][0])
         for industry, location in pairs]
        + [(industry, location, changed[(industry, location)][1][0])
           for industry, location in pairs]
    )
    now_scores = dict(zip(pairs, scored[:len(pairs)]))
    was_scores = dict(zip(pairs, scored[len(pairs):]))

    moves = []
    for pair in pairs:
        (now_count, now_date), (was_count, was_date) = changed[pair]
        now_saturation = now_scores.get(pair)
        was_saturation = was_scores.get(pair)

        if now_saturation is None or was_saturation is None:
            continue

        delta = now_saturation - was_saturation
        now_cluster = _cluster_for(now_saturation)
        was_cluster = _cluster_for(was_saturation)

        crossed_tier = now_cluster != was_cluster
        if not is_reportable(delta, was_cluster, now_cluster):
            continue

        moves.append({
            "industry_type": pair[0],
            "location": pair[1],
            "competitors_now": now_count,
            "competitors_before": was_count,
            "saturation_now": round(now_saturation, 1),
            "saturation_before": round(was_saturation, 1),
            "delta": round(delta, 1),
            "direction": "rose" if delta > 0 else "fell",
            "cluster_now": now_cluster,
            "cluster_before": was_cluster,
            "crossed_tier": crossed_tier,
            "viability_now": round(max(0.0, min(10.0, (100.0 - now_saturation) / 10.0)), 1),
            "observed_on": now_date.isoformat() if now_date else None,
            "previously_on": was_date.isoformat() if was_date else None,
        })

    moves.sort(key=lambda move: (-abs(move["delta"]), move["location"]))
    return moves


_OPENINGS = {
    ("warning", "sme"): "Market conditions changed in barangays your saved plans cover.",
    ("warning", "lgu"): "Market conditions changed across Tarlac City.",
    ("opportunity", "sme"): "A barangay has opened up in an industry you are planning in.",
    ("opportunity", "lgu"): "Barangays that have become less saturated across Tarlac City.",
    ("digest", "sme"): "Your market digest: how saturation moved where you are planning.",
    ("digest", "lgu"): "Tarlac City market digest: how saturation moved across the city.",
}


def _rule_based_summary(moves, audience, kind="warning"):
    """The deterministic version. Always produced, always correct, and
    what stands when the LLM is off or fails."""
    shown = moves[:MAX_REPORTED_MOVES]
    lines = []
    for move in shown:
        tier = (f", now rated {move['cluster_now']} (was {move['cluster_before']})"
                if move["crossed_tier"] else "")
        lines.append(
            f"{move['industry_type']} in {move['location']}: saturation "
            f"{move['direction']} {abs(move['delta'])} points to "
            f"{move['saturation_now']}%{tier}, with competitors going from "
            f"{move['competitors_before']} to {move['competitors_now']}."
        )

    remaining = len(moves) - len(shown)
    if remaining > 0:
        lines.append(f"{remaining} further barangay/industry combination(s) also moved.")

    opening = _OPENINGS[(kind, audience)]
    return opening + " " + " ".join(lines)


def _ai_summary(moves, audience, kind="warning"):
    """The same figures, written up by the configured LLM.

    Only the WORDING is delegated, exactly as on the Recommendations
    page: every number below is computed before the model is called,
    and the deterministic text stands whenever the call does not
    produce something usable. An alert is not a place to let a model
    improvise a figure.
    """
    from app.services.recommendation_service import llm_recommendations_enabled

    fallback = _rule_based_summary(moves, audience, kind)
    if not llm_recommendations_enabled():
        return fallback, "rule_based"

    try:
        from app.services.llm_service import generate_alert_summary

        shown = moves[:MAX_REPORTED_MOVES]
        facts = "\n".join(
            f"- {m['industry_type']} in {m['location']}: saturation {m['saturation_before']}%"
            f" -> {m['saturation_now']}% ({m['direction']} {abs(m['delta'])} points),"
            f" competitors {m['competitors_before']} -> {m['competitors_now']},"
            f" tier {m['cluster_before']} -> {m['cluster_now']}"
            for m in shown
        )
        who = ("a small business owner whose own plans are in these barangays"
               if audience == "sme"
               else "a Tarlac City LGU officer responsible for the whole city")
        purpose = {
            "warning": "This is a WARNING that the market moved -- say what moved and "
                       "what the reader should watch.",
            "opportunity": "These barangays became LESS saturated, so this is an "
                           "OPPORTUNITY notice -- say which ones opened up and how "
                           "much, without promising success.",
            "digest": "This is a periodic DIGEST -- summarise the period's movement "
                      "calmly, without urgency language.",
        }[kind]

        prompt = (
            f"You are writing a short market alert for {who}.\n\n"
            f"{purpose}\n\n"
            f"WHAT CHANGED (these are the only figures that exist -- do not invent "
            f"any others, and do not round them differently):\n{facts}\n\n"
            "Write 2-3 sentences in plain English saying what moved and what it means "
            "for the reader. Quote the actual numbers. Do not speculate about causes "
            "you have not been told. Do not give advice that depends on facts not "
            'listed above.\n\nReturn ONLY JSON: {"summary": "..."}'
        )

        result = generate_alert_summary(prompt)
        if result:
            summary, source = result
            return summary, source
    except Exception:
        from flask import current_app

        current_app.logger.warning("AI alert wording failed; using rule-based text",
                                   exc_info=True)
    return fallback, "rule_based"


def _sme_recipients(moves):
    """{user_id: [moves that touch one of their saved plans]}.

    Scoped deliberately, and scoped on BOTH industry and barangay. A
    coffee-shop plan in San Roque has no use for a hardware-store move
    in Matatalaib, and an alert full of things that do not concern you
    is how people learn to ignore alerts.
    """
    from app.models import SmeProfile

    by_pair = {}
    for move in moves:
        by_pair.setdefault((move["industry_type"], move["location"]), []).append(move)

    recipients = {}
    profiles = (
        db.session.query(SmeProfile.user_id, SmeProfile.industry_type, SmeProfile.location)
        .all()
    )
    for user_id, industry, location in profiles:
        matched = by_pair.get((industry, location))
        if matched:
            recipients.setdefault(user_id, []).extend(matched)
    return recipients


def _sme_recipients_by_industry(moves):
    """{user_id: [moves in an industry they are planning in, ANY barangay]}.

    A DIFFERENT scope from _sme_recipients, and the difference is the
    whole point of having both.

    A warning is about the plan you already have, so it is scoped to
    that plan's own barangay. A recommendation is about where to go
    NEXT, so scoping it to the barangay you already chose would leave
    only the one place you are not looking for -- the useful message is
    "Balibago just opened up for the trade you are in", about a
    barangay you have no plan in at all.
    """
    from app.models import SmeProfile

    by_industry = {}
    for move in moves:
        by_industry.setdefault(move["industry_type"], []).append(move)

    recipients = {}
    rows = db.session.query(SmeProfile.user_id, SmeProfile.industry_type).distinct().all()
    for user_id, industry in rows:
        matched = by_industry.get(industry)
        if matched:
            recipients.setdefault(user_id, []).extend(matched)
    return recipients


def _city_recipients():
    """LGU and Admin accounts, who are told about the city as a whole."""
    return [
        user for user in User.query.filter(User.role.in_(("LGU", "Admin"))).all()
        if user.is_active
    ]


def _improving_moves(moves):
    """The subset of moves that are an OPPORTUNITY rather than a warning.

    "New recommendation" is derived from the same detector rather than
    from a fresh city-wide ranking, and that is a deliberate choice
    worth writing down.

    Re-ranking every industry against every barangay would be ~1,500
    scored combinations per upload, and it would only be a *new*
    recommendation if the previous ranking were stored -- which it
    cannot be: system_settings.setting_value is VARCHAR(255) and a
    twenty-industry top-three does not fit in it. Storing a hash
    instead would tell us that something changed but not what, which is
    no use in the message body.

    A barangay whose saturation has just materially FALLEN, and which
    now sits in a tier an SME would actually enter, is the same finding
    arrived at honestly: it is new because it moved, and the figures
    proving it are already in hand.
    """
    from app.ml.constants import CLUSTER_LABELS_ORDERED

    enterable = set(CLUSTER_LABELS_ORDERED[:2])

    return [
        move for move in moves
        if move["delta"] < 0 and (move["cluster_now"] in enterable or move["crossed_tier"])
    ]


def _elapsed_since(key):
    """How long since `key` was last stamped, or None if never.

    None means "never run", which every caller below treats as due --
    a first deployment should send its first digest rather than wait a
    week to start counting.
    """
    last = SystemSetting.get(key, "")
    if not last:
        return None
    try:
        return datetime.utcnow() - datetime.fromisoformat(last)
    except ValueError:
        return None


def _stamp(key):
    SystemSetting.set(key, datetime.utcnow().isoformat())


def _may_sweep_now():
    """False while the cooldown is still running. Three uploads in a
    row are one event to a person, not three."""
    elapsed = _elapsed_since(LAST_SWEEP_KEY)
    return elapsed is None or elapsed >= timedelta(minutes=ALERT_COOLDOWN_MINUTES)


def _record_sweep():
    _stamp(LAST_SWEEP_KEY)


def _deliver(user, summary, subject, send_email=None, kind="early_warning"):
    """One in-app notification, plus an email when a sender is given.

    The in-app copy is written either way: it is free, it cannot bounce,
    and it is what the bell shows. `send_email` is the email_service
    function for this category, or None when the account has that
    switch off -- passing the function rather than a boolean keeps the
    "which switch, which wording, which footer" decision in one place
    at the call site instead of spread across three flags.
    """
    db.session.add(Notification(
        user_id=user.user_id,
        type=kind,
        message=summary[:500],
    ))

    if send_email is None:
        return False
    return bool(send_email(user.email, user.name, subject, summary))


_PREF_DEFAULTS = {
    "notify_saturation_change": True,
    "notify_recommendations": False,
    "notify_weekly_trends": False,
}


def _wants(user, field):
    """The account's switch, defaulting to the column default.

    getattr with a default, because a database created before these
    columns existed and not yet touched by startup_migrations would
    otherwise raise -- and an alert failing to send must never be what
    breaks the upload that triggered it.
    """
    return bool(getattr(user, field, _PREF_DEFAULTS[field]))


def _notify_group(moves, pref_field, sender, subject_sme, subject_lgu, kind,
                  summary_kind, sme_scope):
    """Send one category of alert to both audiences.

    Written once and parameterised rather than copied three times: the
    ordering (cache the wording, check the switch, write the in-app row,
    then try the email) is the part that is easy to get subtly wrong,
    and three copies of it is three places for the switch check to go
    missing.
    """
    from app.services import email_service

    notified = emailed = 0

    cache = {}
    for user_id, their_moves in sme_scope(moves).items():
        user = db.session.get(User, user_id)
        if user is None or not user.is_active or not _wants(user, pref_field):
            continue

        key = tuple(sorted((m["industry_type"], m["location"]) for m in their_moves))
        if key not in cache:
            cache[key] = _ai_summary(their_moves, "sme", summary_kind)[0]

        if _deliver(user, cache[key], subject_sme,
                    send_email=getattr(email_service, sender), kind=kind):
            emailed += 1
        notified += 1

    city_recipients = [user for user in _city_recipients() if _wants(user, pref_field)]
    if city_recipients:
        city_summary = _ai_summary(moves, "lgu", summary_kind)[0]
        for user in city_recipients:
            if _deliver(user, city_summary, subject_lgu,
                        send_email=getattr(email_service, sender), kind=kind):
                emailed += 1
            notified += 1

    return notified, emailed


def run_market_alert_sweep(force=False):
    """Detect moves, decide who cares, and tell them.

    Covers two of the four Notifications switches in one pass, because
    both are answered by the same detection:

      * notify_saturation_change -- anything that moved, in barangays
        and industries the reader's own plans cover.
      * notify_recommendations   -- the subset that moved in the
        reader's FAVOUR, in their industry, anywhere in the city.

    Returns a summary dict for the caller and the tests. Never raises:
    this runs off the back of an LGU upload, and an alerting problem
    must not fail the import that triggered it.
    """
    from flask import current_app

    try:
        if not force and not _may_sweep_now():
            return {"ran": False, "reason": "cooldown", "moves": 0,
                    "notified": 0, "emailed": 0, "opportunities": 0}

        moves = detect_fluctuations()
        _record_sweep()
        if not moves:
            db.session.commit()
            return {"ran": True, "moves": 0, "notified": 0, "emailed": 0,
                    "opportunities": 0}

        notified, emailed = _notify_group(
            moves,
            pref_field="notify_saturation_change",
            sender="send_market_alert",
            subject_sme="MarketLens alert: a market you follow has moved",
            subject_lgu="MarketLens alert: Tarlac City market conditions changed",
            kind="early_warning",
            summary_kind="warning",
            sme_scope=_sme_recipients,
        )

        opportunities = _improving_moves(moves)
        if opportunities:
            extra_notified, extra_emailed = _notify_group(
                opportunities,
                pref_field="notify_recommendations",
                sender="send_recommendation_alert",
                subject_sme="MarketLens: a new location worth looking at",
                subject_lgu="MarketLens: barangays that opened up in Tarlac City",
                kind="info",
                summary_kind="opportunity",
                sme_scope=_sme_recipients_by_industry,
            )
            notified += extra_notified
            emailed += extra_emailed

        db.session.commit()
        return {"ran": True, "moves": len(moves), "notified": notified,
                "emailed": emailed, "opportunities": len(opportunities)}

    except Exception:
        db.session.rollback()
        current_app.logger.error("market alert sweep failed", exc_info=True)
        return {"ran": False, "reason": "error", "moves": 0, "notified": 0,
                "emailed": 0, "opportunities": 0}


WEEKLY_DIGEST_KEY = "market_alert_last_digest"
WEEKLY_DIGEST_DAYS = 7


def digest_is_due():
    elapsed = _elapsed_since(WEEKLY_DIGEST_KEY)
    return elapsed is None or elapsed >= timedelta(days=WEEKLY_DIGEST_DAYS)


def run_weekly_trend_digest(force=False):
    """The periodic city digest, to accounts that asked for it."""
    from flask import current_app

    try:
        if not force and not digest_is_due():
            return {"ran": False, "reason": "not_due", "notified": 0, "emailed": 0}

        moves = detect_fluctuations()
        if not moves:
            return {"ran": True, "reason": "nothing_moved", "notified": 0, "emailed": 0}

        _stamp(WEEKLY_DIGEST_KEY)

        notified, emailed = _notify_group(
            moves,
            pref_field="notify_weekly_trends",
            sender="send_trend_digest",
            subject_sme="Your MarketLens market digest",
            subject_lgu="MarketLens: Tarlac City market digest",
            kind="info",
            summary_kind="digest",
            sme_scope=_sme_recipients,
        )

        db.session.commit()
        return {"ran": True, "moves": len(moves), "notified": notified, "emailed": emailed}

    except Exception:
        db.session.rollback()
        current_app.logger.error("weekly trend digest failed", exc_info=True)
        return {"ran": False, "reason": "error", "notified": 0, "emailed": 0}


def run_all_alert_sweeps(force=False):
    """Everything the data-change hooks should trigger, in one call.

    One entry point so a new caller cannot wire up the fluctuation
    sweep and silently forget the digest.
    """
    return {
        "market": run_market_alert_sweep(force=force),
        "digest": run_weekly_trend_digest(force=force),
    }
