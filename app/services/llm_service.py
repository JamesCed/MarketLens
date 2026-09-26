"""
app/services/llm_service.py
------------------------------
OPTIONAL. Only used when USE_LLM_RECOMMENDATIONS=true (see .env and
Admin > System Settings). This is the "beginner-friendly AI plugin"
referenced in the README: one function, one API call, no ML
infrastructure to run yourself -- matching the paper's 1.2 Purpose and
Description, SME Module: "AI-driven business recommendations and
alternative industry suggestions".

Why an LLM for this piece specifically (and only this piece):
  - The forecasting NUMBERS (viability score, saturation index) already
    come from the paper's own scikit-learn models (K-Means + Random
    Forest) in forecasting_service.py -- that part should NOT be an
    LLM, because you need reproducible, explainable, gradeable math.
  - What an LLM is genuinely good at is turning those numbers -- PLUS
    the SME's own input parameters and a real comparison against the
    businesses already on file for that industry/location -- into the
    structured "Why This Works" / "Considerations" recommendation the
    capstone paper's storyboard calls for (see
    Reference/Figma_Storyboard's recommendations page, and
    app/templates/sme/recommendations.html).

Two providers are supported -- pick with LLM_PROVIDER in .env:
  - "openai"    -- GPT (the `openai` package). Default. Also used to
    reach OpenRouter (openrouter.ai): OpenRouter is intentionally
    OpenAI-SDK-compatible, so pointing OPENAI_BASE_URL at
    "https://openrouter.ai/api/v1" with an OpenRouter key (the
    "sk-or-v1-..." format) and OPENAI_MODEL="openai/gpt-4o-mini" routes
    through OpenRouter with no other code change.
  - "anthropic" -- Claude (the `anthropic` package).

generate_recommendation_json() tries the configured provider first; if
that provider has no API key set, it automatically tries the OTHER one
(so setting either key alone just works); if NEITHER key is set, or the
call fails for any reason (network, quota, bad key, malformed JSON
back), this returns None and the caller (recommendation_service.py)
keeps using its rule-based recommendation -- the page never breaks
because of this.
"""

import json
import re

_client_cache = {}

# The exact shape every caller can rely on getting back from a
# successful LLM call -- validated in _coerce_llm_payload() below so a
# malformed or partial response is treated as a failure (triggering the
# rule-based fallback) rather than rendered with missing pieces.
_REQUIRED_KEYS = ("headline", "opportunity_type", "summary", "reasons", "risks")


def _prompt_for(context):
    """Builds the grounding prompt: the SME's OWN business parameters,
    the AI engine's own numbers, and a real comparison against the
    businesses already on file for this industry+location (seeded
    market_data and/or live Google Places API results) -- so the LLM is
    writing ABOUT this specific plan and this specific market, not
    generic advice. `context` is the dict built by
    recommendation_service.build_recommendation_context()."""
    competitor_lines = (
        "\n".join(f"  - {name}" for name in context["competitor_sample"])
        if context["competitor_sample"]
        else "  (none found on file)"
    )
    return (
        "You are a market-entry advisor inside a Decision Support System for Philippine "
        "SMEs in Tarlac City. An entrepreneur has entered a business plan; compare it "
        "against the real market data already on file and respond with ONLY a JSON object "
        "(no markdown, no code fences) with exactly these keys:\n"
        '  "headline": one short ALL-CAPS-STYLE verdict line, e.g. '
        '"MODERATE OPPORTUNITY -- viable with a solid differentiation strategy."\n'
        '  "opportunity_type": one of "High Opportunity", "Moderate Opportunity", '
        '"Low Opportunity", "High Saturation"\n'
        '  "summary": ONE sentence (max 40 words) stating the verdict for THIS specific plan\n'
        '  "reasons": a JSON array of 2-4 short strings, each a concrete reason this plan '
        "could work, grounded in the numbers given (\"Why This Works\")\n"
        '  "risks": a JSON array of 2-3 short strings, each a concrete consideration or risk '
        '("Considerations")\n\n'
        "Do not invent numbers beyond the ones given below. Reference the SME's own capital "
        "and the named competitors where relevant.\n\n"
        f"Business name: {context['business_name']}\n"
        f"Industry type: {context['industry_type']}\n"
        f"Location: {context['location']} (Barangay, Tarlac City)\n"
        f"Business stage: {context['business_stage']}"
        + (f", {context['years_in_operation']} year(s) operating" if context["years_in_operation"] else " (not yet opened)")
        + "\n"
        f"Startup capital: PHP {context['startup_capital']:,.0f}\n"
        f"Employee count: {context['employee_count']}\n"
        f"Estimated monthly revenue: PHP {context['monthly_revenue_est']:,.0f}\n\n"
        f"AI Market Saturation Index: {round(context['saturation_index'])}%\n"
        f"Saturation cluster: {context['cluster_label']}\n"
        f"Viability score: {context['viability_score']}/10\n"
        f"Model confidence: {round(context['confidence_level'])}%\n"
        f"Real population of {context['location']}: {context['population']}\n\n"
        f"Existing {context['industry_type']} businesses on file in {context['location']} "
        f"({context['competitor_count']} total"
        + (", from live Google Places API data" if not context["competitor_simulated"] else ", simulated -- no live Places API data yet")
        + f"), including:\n{competitor_lines}\n"
    )


def _get_openai_client():
    if "openai" in _client_cache:
        return _client_cache["openai"]
    client = None
    try:
        from flask import current_app
        from openai import OpenAI

        api_key = current_app.config.get("OPENAI_API_KEY", "")
        base_url = current_app.config.get("OPENAI_BASE_URL", "") or None
        if api_key:
            client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception:
        client = None
    _client_cache["openai"] = client
    return client


def _get_anthropic_client():
    if "anthropic" in _client_cache:
        return _client_cache["anthropic"]
    client = None
    try:
        from flask import current_app
        import anthropic

        api_key = current_app.config.get("ANTHROPIC_API_KEY", "")
        if api_key:
            client = anthropic.Anthropic(api_key=api_key)
    except Exception:
        client = None
    _client_cache["anthropic"] = client
    return client


def _generate_with_openai(prompt):
    from flask import current_app

    client = _get_openai_client()
    if client is None:
        return None
    try:
        model = current_app.config.get("OPENAI_MODEL", "gpt-4o-mini")
        response = client.chat.completions.create(
            model=model,
            max_tokens=500,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = (response.choices[0].message.content or "").strip()
        return text or None
    except Exception:
        return None


def _generate_with_anthropic(prompt):
    from flask import current_app

    client = _get_anthropic_client()
    if client is None:
        return None
    try:
        model = current_app.config.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
        response = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt + "\n\nRespond with ONLY the JSON object, no other text."}],
        )
        text_blocks = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        text = " ".join(text_blocks).strip()
        return text or None
    except Exception:
        return None


_GENERATORS = {"openai": _generate_with_openai, "anthropic": _generate_with_anthropic}


def _strip_code_fence(raw_text):
    """Some models wrap their JSON in a ```json fence even when asked
    not to. Shared by both response parsers below."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _coerce_llm_payload(raw_text):
    """Parses the model's raw text into the validated dict every caller
    expects, or None if it isn't usable. Pulled out as its own function
    (no network, no Flask context needed) so the parsing logic itself is
    unit-testable without ever calling a real LLM."""
    if not raw_text:
        return None
    text = _strip_code_fence(raw_text)
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not all(key in payload for key in _REQUIRED_KEYS):
        return None
    reasons = payload.get("reasons")
    risks = payload.get("risks")
    if not isinstance(reasons, list) or not isinstance(risks, list):
        return None
    return {
        "headline": str(payload["headline"]).strip(),
        "opportunity_type": str(payload["opportunity_type"]).strip(),
        "summary": str(payload["summary"]).strip(),
        "reasons": [str(r).strip() for r in reasons if str(r).strip()],
        "risks": [str(r).strip() for r in risks if str(r).strip()],
    }


def _opportunity_batch_prompt(industry_type, city, cards):
    """One prompt covering EVERY displayed opportunity card, rather than
    one call per card. 18 separate LLM round trips on a page load would
    be slow and expensive; a single batched call keeps the page usable
    and costs one request."""
    lines = []
    for card in cards:
        lines.append(
            f"- location: {card['location']}"
            f" | competitors_on_file: {card['competitor_count']}"
            f" | population: {card['population']}"
            f" | residents_per_existing_business: {card['residents_per_business']}"
            f" | population_density_per_km2: {card['population_density']}"
            f" | ai_saturation_index_percent: {card['saturation_index']}"
            f" | ai_viability_score_out_of_10: {card['viability_score']}"
            f" | model_confidence_percent: {card['confidence_level']}"
            f" | roi_timeframe: {card['roi_timeframe']}"
            f" | competitor_count_is_live_google_data: {card['competitor_is_live']}"
        )

    return (
        "You are advising a small or medium enterprise (SME) on where to open a "
        f"{industry_type} business in Tarlac City, Philippines.\n\n"
        "CITY BENCHMARKS for this same industry, computed across "
        f"{city['barangays_scored']} barangays:\n"
        f"- median competitors per barangay: {city['median_competitors']}\n"
        f"- median residents per existing business: {city['median_residents_per_business']}\n"
        f"- median population density: {city['median_density']} per km2\n\n"
        "BARANGAYS TO WRITE UP (each line is one barangay and its real figures):\n"
        + "\n".join(lines)
        + "\n\nFor EACH barangay above, write 3-5 'reasons' (why this location works) "
        "and 2-4 'risks' (considerations before entering).\n\n"
        "HARD RULES -- these matter more than style:\n"
        "1. Every sentence must be grounded in the figures given above. Quote the "
        "actual numbers and compare them to the city medians. Do NOT invent "
        "rent, foot traffic, demographics, landmarks, or any figure not listed.\n"
        "2. Never claim to know something the data does not say (nearby schools, "
        "offices, road works, local events, customer preferences).\n"
        "3. If competitor_count_is_live_google_data is false, one risk MUST say the "
        "competitor figure is a simulated estimate rather than a live count.\n"
        "4. Keep each bullet to one clear sentence, plain English.\n"
        "5. For roi_timeframe, start from the roi_timeframe already given for that "
        "barangay and adjust it only if the competitor count and market depth clearly "
        "justify it. Never go below 3 months -- no business reaches steady-state "
        "revenue sooner than that.\n\n"
        'Return ONLY valid JSON of the form: {"cards": [{"location": "<exact name>", '
        '"reasons": ["..."], "risks": ["..."], "roi_timeframe": "N-M months"}]} '
        "with one entry per barangay listed."
    )


def generate_opportunity_cards_json(industry_type, city, cards):
    """Rewrites the Recommendations page's location-opportunity cards
    with the configured LLM, in ONE batched call. Returns
    {location: {"reasons": [...], "risks": [...]}} for whatever the model
    returned usably, or {} when the LLM is off, unconfigured, or the
    response can't be parsed -- callers keep their own data-derived text
    for anything missing. Never raises.

    The prompt hands the model ONLY real figures (see
    _opportunity_batch_prompt) and forbids inventing any others, so the
    LLM is doing the writing, not the analysis: the numbers, the ranking
    and the ROI window are all computed before this is ever called.
    """
    from flask import current_app

    if not cards:
        return {}

    prompt = _opportunity_batch_prompt(industry_type, city, cards)
    provider = current_app.config.get("LLM_PROVIDER", "openai")
    order = [provider] + [p for p in _GENERATORS if p != provider]

    for name in order:
        generate = _GENERATORS.get(name)
        if generate is None:
            continue
        raw_text = generate(prompt)
        if not raw_text:
            continue
        try:
            payload = json.loads(_strip_code_fence(raw_text))
        except (ValueError, TypeError):
            continue
        entries = payload.get("cards") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            continue

        out = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            location = entry.get("location")
            reasons = [str(r).strip() for r in (entry.get("reasons") or []) if str(r).strip()]
            risks = [str(r).strip() for r in (entry.get("risks") or []) if str(r).strip()]
            if location and reasons and risks:
                card = {
                    "reasons": reasons[:5],
                    "risks": risks[:4],
                    "generated_by": f"llm:{name}",
                }
                # Only accept an ROI window that actually looks like one
                # -- "N-M months", both numbers sane and in order. A
                # model that returns prose here is ignored and the
                # computed window stands.
                roi = str(entry.get("roi_timeframe") or "").strip()
                match = re.fullmatch(r"(\d{1,2})\s*[-\u2013]\s*(\d{1,2})\s*months?", roi, re.I)
                if match:
                    low, high = int(match.group(1)), int(match.group(2))
                    if 1 <= low < high <= 60:
                        card["roi_timeframe"] = f"{low}-{high} months"
                out[str(location)] = card
        if out:
            return out
    return {}


def generate_recommendation_json(context):
    """Tries the configured LLM provider (falling back to the other one
    if only its key is set) and returns a validated recommendation dict
    -- {headline, opportunity_type, summary, reasons, risks} -- or None
    if every provider is unavailable or every response was unusable.
    Never raises."""
    from flask import current_app

    prompt = _prompt_for(context)

    provider = current_app.config.get("LLM_PROVIDER", "openai")
    order = [provider] + [p for p in _GENERATORS if p != provider]

    for name in order:
        generate = _GENERATORS.get(name)
        if generate is None:
            continue
        raw_text = generate(prompt)
        payload = _coerce_llm_payload(raw_text)
        if payload:
            payload["generated_by"] = f"llm:{name}"
            return payload
    return None
