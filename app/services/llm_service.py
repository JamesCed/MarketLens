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
from datetime import datetime

_client_cache = {}


# ---------------------------------------------------------------------
# WHY A FAILURE RECORD EXISTS AT ALL
# ---------------------------------------------------------------------
# Every call below is wrapped so a flaky LLM can never break the page,
# and that is right: a recommendation must still render when the API is
# down. But the original wrapping was `except Exception: return None`,
# which made four completely different situations look identical from
# the outside:
#
#   * no API key configured
#   * a key that is the wrong KIND (an OAuth token where an AI Studio
#     API key was expected -- both are long opaque strings, and only
#     one of them authenticates)
#   * a model name the endpoint does not recognise (pointing
#     OPENAI_BASE_URL at Gemini while OPENAI_MODEL is still the default
#     gpt-4o-mini gives a 404, not an error anyone can see)
#   * the model answering with something that is not the JSON asked for
#
# In all four the page quietly showed rule-based text and nothing was
# logged, so "the AI isn't working" was unanswerable without adding
# print statements to a live deployment. So failures are now LOGGED and
# the most recent one is kept in memory for the diagnostics endpoint
# (admin > LLM status). The page's behaviour is unchanged: it still
# falls back, still never raises.
_LAST_FAILURE = {}


def _redact(text):
    """The configured API key must never reach a log line or a
    diagnostics page. Provider SDKs sometimes echo request details into
    exception messages, so anything that looks like the key we hold is
    replaced before the message is stored."""
    message = str(text or "")
    try:
        from flask import current_app

        for config_key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            secret = (current_app.config.get(config_key) or "").strip()
            if len(secret) >= 8:
                message = message.replace(secret, "***redacted***")
    except Exception:  # pragma: no cover - no app context
        pass
    return message[:500]


def _begin_attempt():
    """Start a fresh attempt, forgetting the previous one's failure.

    Called at the top of each public entry point rather than inside
    _record_failure, and that placement is the whole point -- see
    below.
    """
    _LAST_FAILURE.clear()


def _record_failure(provider, stage, detail):
    """Remember and log why a call did not produce usable text.
    `stage` is one of "no_client", "api_call", "parse".

    THE FIRST FAILURE OF AN ATTEMPT IS THE ONE KEPT, not the last.

    When the configured provider fails, the callers fall through and
    try the others -- which is good behaviour, since a deployment with
    only one key set should work whichever slot the key is in. But
    those fallbacks fail too, almost always with a dull "no API key
    set", and if each overwrote the last the operator would be shown
    the least informative message of the three.

    Concretely: Gemini answers 401 with Google's own explanation, then
    OpenAI is tried and reports "no key", then Anthropic reports "no
    key". The 401 is the one worth reading. So a failure is recorded
    only when the slot is empty, and the slot is emptied at the START
    of an attempt by _begin_attempt().
    """
    if _LAST_FAILURE:
        return
    _LAST_FAILURE.update({
        "provider": provider,
        "stage": stage,
        "detail": _redact(detail),
        "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })
    hint = _hint_for(_LAST_FAILURE["detail"])
    if hint:
        _LAST_FAILURE["hint"] = hint
    try:
        from flask import current_app

        # "No key configured" is a STATE, not an incident: it is the
        # default install, and the rule-based generator handles it by
        # design. Logging that at warning level on every page view
        # would bury the failures that do matter -- a 401, a 404 on the
        # model name, a spent quota -- under noise from a deployment
        # that is working exactly as intended. It is still recorded
        # above, so /admin/llm-status can report it on request.
        log = current_app.logger.info if stage == "no_client" else current_app.logger.warning
        log(
            "LLM recommendation unavailable (provider=%s stage=%s): %s",
            provider, stage, _LAST_FAILURE["detail"],
        )
    except Exception:  # pragma: no cover - no app context
        pass


def _hint_for(detail):
    """Turn the provider's own error into the thing to go and change.

    These are the failures that actually happen with this setup, and
    each of them reads like something it is not. That is the whole
    reason for translating them: a 404 on a model name and a 401 on a
    key produce the same visible symptom -- rule-based text with no
    explanation -- and the natural response to both is to regenerate the
    key, which fixes only one of them.
    """
    lowered = (detail or "").lower()

    if "404" in lowered or "is not found" in lowered or "not found for api version" in lowered:
        return ("The MODEL NAME is wrong, not the key. Google serves the Flash line "
                "numbered in tenths -- gemini-3.8-flash (newest), 3.7/3.6/3.5-flash, "
                "gemini-3.5-flash-lite -- so a plain 'gemini-3-flash' is a 404. Set "
                "GEMINI_MODEL (or OPENAI_MODEL, if you are on that provider) to an "
                "exact current id.")

    if "multiple authentication credentials" in lowered:
        return ("An AQ.-format AI Studio key was sent as `Authorization: Bearer` to "
                "Gemini's OpenAI-compatibility endpoint, which refuses it. Set "
                "LLM_PROVIDER=gemini so the same key goes to the native endpoint as "
                "`x-goog-api-key` instead, and clear OPENAI_BASE_URL.")

    if "access_token_type_unsupported" in lowered:
        return ("Google read the key as an OAuth token rather than an API key. Check "
                "GEMINI_API_KEY holds the whole AQ.Ab... value with no trailing space, "
                "and regenerate it in AI Studio if it still fails.")

    if "401" in lowered or "unauthenticated" in lowered or "api key not valid" in lowered:
        return ("The key was rejected. Confirm it is an AI Studio key for the Gemini "
                "API (AQ.Ab... is the current format) and that it is in GEMINI_API_KEY "
                "or OPENAI_API_KEY, copied whole.")

    if "429" in lowered or "quota" in lowered or "rate limit" in lowered:
        return ("The key's quota or rate limit is spent, so this is temporary. The "
                "rule-based generator covers it until the window resets.")

    return None


def last_failure():
    """The most recent failure record, or {} if nothing has failed in
    this process. Read by the diagnostics endpoint."""
    return dict(_LAST_FAILURE)


def reset_client_cache():
    """Forget the cached provider clients.

    The clients are built once per process from config read at that
    moment, so a key corrected in the Render dashboard would otherwise
    not take effect until the worker restarted -- and the symptom of
    that is "I fixed the key and it still says rule-based", which is a
    miserable thing to debug. The diagnostics probe calls this first so
    it always tests the CURRENT configuration.
    """
    _client_cache.clear()
    _LAST_FAILURE.clear()

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


# Each of these reports WHICH of the three preconditions failed --
# package, key, construction -- rather than collapsing them into one
# message. "no key configured, or the package is missing" is exactly
# the kind of unfalsifiable diagnostic this whole change exists to get
# rid of: it is no help at all to someone staring at a key they can
# see is set.

def _get_openai_client():
    if "openai" in _client_cache:
        return _client_cache["openai"]

    from flask import current_app

    client = None
    api_key = (current_app.config.get("OPENAI_API_KEY") or "").strip()
    base_url = (current_app.config.get("OPENAI_BASE_URL") or "").strip() or None

    if not api_key:
        _record_failure("openai", "no_client", "OPENAI_API_KEY is not set")
    else:
        try:
            from openai import OpenAI
        except Exception as exc:  # noqa: BLE001
            _record_failure("openai", "no_client",
                            f"the `openai` package could not be imported: {exc}")
        else:
            try:
                client = OpenAI(api_key=api_key, base_url=base_url)
            except Exception as exc:  # noqa: BLE001
                _record_failure("openai", "no_client",
                                f"OpenAI client could not be built (base_url={base_url!r}): "
                                f"{type(exc).__name__}: {exc}")

    _client_cache["openai"] = client
    return client


def _get_anthropic_client():
    if "anthropic" in _client_cache:
        return _client_cache["anthropic"]

    from flask import current_app

    client = None
    api_key = (current_app.config.get("ANTHROPIC_API_KEY") or "").strip()

    if not api_key:
        _record_failure("anthropic", "no_client", "ANTHROPIC_API_KEY is not set")
    else:
        try:
            import anthropic
        except Exception as exc:  # noqa: BLE001
            _record_failure("anthropic", "no_client",
                            f"the `anthropic` package could not be imported: {exc}")
        else:
            try:
                client = anthropic.Anthropic(api_key=api_key)
            except Exception as exc:  # noqa: BLE001
                _record_failure("anthropic", "no_client",
                                f"Anthropic client could not be built: {type(exc).__name__}: {exc}")

    _client_cache["anthropic"] = client
    return client


def _generate_with_openai(prompt):
    from flask import current_app

    client = _get_openai_client()
    if client is None:
        return None  # _get_openai_client recorded which precondition failed
    model = current_app.config.get("OPENAI_MODEL", "gpt-4o-mini")
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=500,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            _record_failure("openai", "api_call", f"model {model} returned an empty message")
            return None
        return text
    except Exception as exc:  # noqa: BLE001 -- a flaky API must not break the page
        # The model name is included deliberately: pointing
        # OPENAI_BASE_URL at a non-OpenAI provider while leaving
        # OPENAI_MODEL on its default is the single most common way to
        # get a 404 here, and the message is useless without it.
        _record_failure("openai", "api_call", f"model={model}: {type(exc).__name__}: {exc}")
        return None


def _generate_with_anthropic(prompt):
    from flask import current_app

    client = _get_anthropic_client()
    if client is None:
        return None  # _get_anthropic_client recorded which precondition failed
    model = current_app.config.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt + "\n\nRespond with ONLY the JSON object, no other text."}],
        )
        text_blocks = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        text = " ".join(text_blocks).strip()
        if not text:
            _record_failure("anthropic", "api_call", f"model {model} returned no text block")
            return None
        return text
    except Exception as exc:  # noqa: BLE001
        _record_failure("anthropic", "api_call", f"model={model}: {type(exc).__name__}: {exc}")
        return None


def _generate_with_gemini(prompt):
    """Google Gemini over its NATIVE endpoint.

    Deliberately not the OpenAI SDK pointed at Gemini's compatibility
    layer, and deliberately not httpx either -- see the note on
    GEMINI_API_KEY in app/config.py for why the compatibility route
    stopped working for keys issued after 28 May 2026. In short: an
    AQ.-format auth key sent as `Authorization: Bearer` is refused,
    and the same key sent as `x-goog-api-key` to the native endpoint
    is accepted.

    `requests` is already a dependency of this project (the Places
    API uses it), so this path pulls in nothing new and is unaffected
    by the openai/httpx version pairing that broke the other one.

    responseMimeType=application/json makes the API itself guarantee
    JSON, rather than the prompt asking for it and the parser hoping.
    The code-fence stripping downstream stays anyway: it costs
    nothing and still covers the other two providers.
    """
    from flask import current_app

    import requests

    api_key = (current_app.config.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        _record_failure("gemini", "no_client", "GEMINI_API_KEY is not set")
        return None

    model = (current_app.config.get("GEMINI_MODEL") or "gemini-3-flash").strip()
    base_url = (current_app.config.get("GEMINI_BASE_URL")
                or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    url = f"{base_url}/models/{model}:generateContent"

    try:
        response = requests.post(
            url,
            # The key goes in a HEADER, never in the query string: a URL
            # ends up in access logs and error reports, and ?key= is how
            # credentials leak.
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.4,
                    "maxOutputTokens": 2048,
                    "responseMimeType": "application/json",
                },
            },
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        _record_failure("gemini", "api_call", f"model={model}: {type(exc).__name__}: {exc}")
        return None

    if response.status_code != 200:
        # Google's own message is the most useful thing there is here --
        # it distinguishes a bad key from an unknown model from a spent
        # quota, which is exactly what was impossible to tell before.
        _record_failure("gemini", "api_call",
                        f"model={model}: HTTP {response.status_code}: {response.text[:300]}")
        return None

    try:
        payload = response.json()
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts).strip()
    except Exception as exc:  # noqa: BLE001
        _record_failure("gemini", "parse",
                        f"unexpected response shape ({exc}): {response.text[:200]}")
        return None

    if not text:
        # A response with no text is usually a safety block or a
        # finishReason worth seeing, so report the reason rather than
        # just "empty".
        reason = ""
        try:
            reason = payload["candidates"][0].get("finishReason", "")
        except Exception:  # pragma: no cover
            pass
        _record_failure("gemini", "api_call",
                        f"model {model} returned no text (finishReason={reason!r})")
        return None
    return text


_GENERATORS = {
    "openai": _generate_with_openai,
    "anthropic": _generate_with_anthropic,
    "gemini": _generate_with_gemini,
}


def _provider_order():
    """Which generators to try, in order: the configured one first, then
    the rest as fallbacks.

    WITH ONE CORRECTION, AND IT IS NOT A WORKAROUND.

    Google has retired the old `AIza` Standard keys for the Gemini API
    -- unrestricted ones began being rejected on 19 June 2026 and the
    format was end-of-lifed through September 2026 -- so an AI Studio
    key is now an `AQ.`-prefixed auth key. That format is accepted on
    Gemini's NATIVE endpoint (`x-goog-api-key`) and refused on its
    OpenAI-compatibility endpoint, where the same key sent as
    `Authorization: Bearer` returns either HTTP 400 "Multiple
    authentication credentials received" or a 401 calling the key
    invalid.

    Which means the pairing LLM_PROVIDER=openai + OPENAI_BASE_URL
    pointed at Gemini's /openai/ path + an `AQ.` key cannot ever work,
    and every call spends a round trip finding that out again. The key's
    own prefix says which transport can carry it, so the order is
    corrected from the credential rather than leaving the deployment to
    discover it in the logs.

    Deliberately narrow: this only reorders when the configured provider
    is `openai` AND the key is an `AQ.` one, i.e. exactly the
    combination that is known-broken. A real OpenAI key, or an
    explicitly chosen provider, is left alone -- the point is to stop
    wasting a guaranteed-failing attempt, not to second-guess a
    configuration that works.
    """
    from flask import current_app

    provider = (current_app.config.get("LLM_PROVIDER") or "openai").strip().lower()
    if provider not in _GENERATORS:
        provider = "openai"

    if provider == "openai":
        key = (current_app.config.get("OPENAI_API_KEY") or "").strip()
        if key.startswith("AQ."):
            provider = "gemini"

    return [provider] + [name for name in _GENERATORS if name != provider]


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
    if not cards:
        return {}

    _begin_attempt()
    prompt = _opportunity_batch_prompt(industry_type, city, cards)
    order = _provider_order()

    for name in order:
        generate = _GENERATORS.get(name)
        if generate is None:
            continue
        raw_text = generate(prompt)
        if not raw_text:
            continue  # the generator already recorded why
        try:
            payload = json.loads(_strip_code_fence(raw_text))
        except (ValueError, TypeError) as exc:
            _record_failure(name, "parse", f"response was not JSON ({exc}): {raw_text[:160]}")
            continue
        entries = payload.get("cards") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            _record_failure(name, "parse",
                            f'JSON had no "cards" list: {str(payload)[:160]}')
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
        _record_failure(name, "parse",
                        "no card in the response had both a location and usable reasons/risks")
    return {}


def generate_recommendation_json(context):
    """Tries the configured LLM provider (falling back to the other one
    if only its key is set) and returns a validated recommendation dict
    -- {headline, opportunity_type, summary, reasons, risks} -- or None
    if every provider is unavailable or every response was unusable.
    Never raises."""
    _begin_attempt()
    prompt = _prompt_for(context)
    order = _provider_order()

    for name in order:
        generate = _GENERATORS.get(name)
        if generate is None:
            continue
        raw_text = generate(prompt)
        if not raw_text:
            continue  # the generator already recorded why
        payload = _coerce_llm_payload(raw_text)
        if payload:
            payload["generated_by"] = f"llm:{name}"
            return payload
        _record_failure(name, "parse",
                        f"response did not contain the required keys: {raw_text[:160]}")
    return None


def generate_alert_summary(prompt):
    """One short piece of alert prose from the LLM, or None.

    Used by market_alert_service for notification wording, and it goes
    through THIS function rather than reaching into _GENERATORS itself
    so that the alerts get exactly what the Recommendations page gets:

      * the same provider order -- _provider_order(), including its
        correction for `AQ.` keys -- so a deployment whose LLM_PROVIDER
        is misconfigured but which has a usable key still produces real
        AI text instead of quietly falling back to rule-based wording;
      * the same _begin_attempt()/_record_failure() diagnostics, so
        "why is my alert text rule-based?" is answerable from Admin >
        LLM status, the same page that answers it for recommendations;
      * the same response handling, including the code-fence strip.

    Returns (summary, "llm:<provider>") or None. Never raises: an alert
    whose wording could not be generated still has to go out in the
    deterministic wording, and a dead API must never swallow the alert
    itself.
    """
    import json

    _begin_attempt()
    order = _provider_order()

    for name in order:
        generate = _GENERATORS.get(name)
        if generate is None:
            continue
        try:
            raw_text = generate(prompt)
        except Exception as exc:  # noqa: BLE001
            _record_failure(name, "api_call", f"{type(exc).__name__}: {exc}")
            continue
        if not raw_text:
            continue  # the generator already recorded why

        try:
            payload = json.loads(_strip_code_fence(raw_text))
            summary = str(payload.get("summary") or "").strip()
        except Exception:  # noqa: BLE001
            # Not JSON. Accept the text as-is if it is plausibly a couple
            # of sentences: unlike a recommendation card there is only
            # one field wanted here, so a model that answered in plain
            # prose has still answered.
            summary = _strip_code_fence(raw_text)
            if len(summary) > 700 or "{" in summary:
                _record_failure(name, "parse",
                                f"response was neither JSON nor short prose: {raw_text[:160]}")
                continue

        if summary:
            return summary, f"llm:{name}"
        _record_failure(name, "parse", f"response had no summary text: {raw_text[:160]}")
    return None


# ---------------------------------------------------------------------
# DIAGNOSTICS
# ---------------------------------------------------------------------

def llm_status():
    """Everything needed to answer "why am I still seeing rule-based
    text?" WITHOUT exposing a secret.

    The API key is never returned -- only whether one is set, its
    length, and its first four characters. Those four are the single
    most useful diagnostic there is, because the common mistakes are
    mistakes of KIND rather than typos: a Google AI Studio key begins
    "AIza", an OpenRouter key "sk-o", an OpenAI key "sk-p"/"sk-s", and
    an OAuth access token "AQ." or "ya29." -- an OAuth token pasted
    where an API key belongs looks perfectly plausible and fails with a
    bare 401. Four characters identify which of those you have and are
    useless to anyone who steals them.
    """
    from flask import current_app

    from app.services.recommendation_service import llm_recommendations_enabled

    def describe(config_key):
        secret = (current_app.config.get(config_key) or "").strip()
        if not secret:
            return {"set": False}
        return {"set": True, "length": len(secret), "starts_with": secret[:4]}

    base_url = (current_app.config.get("OPENAI_BASE_URL") or "").strip()
    provider = current_app.config.get("LLM_PROVIDER", "openai")
    status = {
        "enabled": llm_recommendations_enabled(),
        "provider": provider,
        "gemini_model": current_app.config.get("GEMINI_MODEL", "gemini-3-flash"),
        "gemini_key": describe("GEMINI_API_KEY"),
        "openai_model": current_app.config.get("OPENAI_MODEL", "gpt-4o-mini"),
        "openai_base_url": base_url or "(default: api.openai.com)",
        "openai_key": describe("OPENAI_API_KEY"),
        "anthropic_model": current_app.config.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        "anthropic_key": describe("ANTHROPIC_API_KEY"),
        "last_failure": last_failure() or None,
    }

    # The one misconfiguration that produces a 401 reading like a bad
    # key when the key is in fact perfectly good. Worth saying out loud
    # rather than leaving someone to rediscover it.
    if provider == "openai" and "generativelanguage.googleapis.com" in base_url:
        key_prefix = (current_app.config.get("OPENAI_API_KEY") or "").strip()[:3]
        if key_prefix in ("AQ.", "ya2"):
            status["warning"] = (
                "This is an AQ.-format Google auth key being sent to Gemini's "
                "OpenAI-compatible endpoint, which authenticates with "
                "`Authorization: Bearer` and rejects auth keys (400 'Multiple "
                "authentication credentials received', or a 401 that looks like "
                "a bad key). Set LLM_PROVIDER=gemini to use the native endpoint "
                "instead; the same key works there."
            )
    return status


def probe():
    """Make the smallest possible real call and report exactly what
    came back.

    This exists because every other signal is ambiguous. Rule-based
    text on the page means "the LLM did not answer" and nothing more
    specific, and a key can be wrong in several ways that all look the
    same from the outside. A probe turns that into one sentence: it
    either returns the model's own words, or the provider's own error.

    The cached client is dropped first, so this tests the configuration
    as it stands RIGHT NOW rather than whatever was read when the
    worker started -- otherwise a key corrected in the dashboard would
    keep reporting the old failure until the next restart.
    """
    from flask import current_app

    reset_client_cache()

    provider = current_app.config.get("LLM_PROVIDER", "openai")
    generate = _GENERATORS.get(provider)
    if generate is None:
        return {"ok": False, "provider": provider, "error": f"unknown LLM_PROVIDER {provider!r}"}

    prompt = (
        'Reply with ONLY this JSON object and nothing else: '
        '{"ok": true, "note": "connection verified"}'
    )
    raw_text = generate(prompt)
    if not raw_text:
        return {"ok": False, "provider": provider, "error": last_failure() or "no response"}

    parsed = None
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
    except (ValueError, TypeError):
        pass
    model_key = {"openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL",
                 "gemini": "GEMINI_MODEL"}.get(provider)
    return {
        "ok": True,
        "provider": provider,
        "model": current_app.config.get(model_key) if model_key else None,
        "raw": raw_text[:300],
        "parsed_as_json": parsed is not None,
    }
