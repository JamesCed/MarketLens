"""
tests/test_llm_recommendations.py
------------------------------------
Turning the real AI on, and being able to tell when it is off.

THE PROBLEM THIS FILE EXISTS FOR

"Use the real AI" failed silently in four independent ways at once,
and not one of them produced a symptom you could act on -- the page
just showed rule-based wording, every time, with nothing in the logs:

  1. httpx. requirements.txt pinned openai==1.51.2 and let httpx
     float. openai 1.51.2 passes `proxies` to httpx.Client
     unconditionally; httpx REMOVED that argument in 0.28.0. So every
     fresh `pip install -r requirements.txt` produced an openai client
     that could not be constructed at all -- no API key fixes that.
  2. USE_LLM_RECOMMENDATIONS was dead config. Documented in .env, in
     render.yaml and in DEPLOYMENT.md as the switch; read into Config
     and then never consulted. The real switch was a database row.
  3. Every failure was `except Exception: return None`, so a wrong
     key, an unknown model, a spent quota and malformed JSON were
     indistinguishable from each other and from "feature disabled".
  4. No way to ask from outside the process what any of it was doing.

These tests pin the fixes. They do NOT call a real API -- that is what
/admin/llm-status/probe is for, on a deployment with a real key.
"""

import os
import re

import pytest

from app import create_app
from app.extensions import db
from app.models import SystemSetting
from app.models.user import User

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _clean_llm_state():
    """The service caches its client and its last failure per process."""
    from app.services import llm_service

    llm_service.reset_client_cache()
    yield
    llm_service.reset_client_cache()


# ---------------------------------------------------------------------
# 1. The dependency that made all of this moot
# ---------------------------------------------------------------------

def test_httpx_is_held_below_the_release_that_breaks_the_openai_client():
    """openai 1.51.2 calls httpx.Client(proxies=...) whether or not a
    proxy is configured. httpx 0.28.0 removed that argument, so the
    pair raises TypeError at client construction -- before any key,
    model or prompt is involved. Pip is free to pick httpx unless it is
    told otherwise, so this has to be pinned or it comes back on the
    next deploy."""
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as handle:
        requirements = handle.read()

    openai_pin = re.search(r"^openai==(\S+)", requirements, re.M)
    httpx_pin = re.search(r"^httpx([<>=!,\d.\s]+)$", requirements, re.M)

    assert openai_pin, "openai is no longer pinned; re-check the httpx pairing"
    assert httpx_pin, (
        "httpx is unpinned again. openai==%s passes `proxies` to httpx.Client, "
        "which httpx removed in 0.28.0 -- pip will install 0.28+ and the AI "
        "recommendations will silently never work." % openai_pin.group(1)
    )
    assert "<0.28" in httpx_pin.group(1).replace(" ", ""), (
        f"httpx pin is {httpx_pin.group(1).strip()!r}; it must exclude 0.28+ "
        f"while openai=={openai_pin.group(1)} is in use"
    )


# ---------------------------------------------------------------------
# 2. The switch the deployment guide promises
# ---------------------------------------------------------------------

def test_the_environment_variable_turns_the_llm_on(app, monkeypatch):
    """USE_LLM_RECOMMENDATIONS is what .env, render.yaml and
    DEPLOYMENT.md all tell you to set. It has to be the thing that
    decides."""
    from app.services.recommendation_service import llm_recommendations_enabled

    with app.app_context():
        SystemSetting.set("use_llm_recommendations", "false")

        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "true")
        assert llm_recommendations_enabled() is True, (
            "setting the documented environment variable did nothing -- this is "
            "the bug where an operator edits Render and watches nothing change"
        )


def test_the_environment_variable_can_also_turn_it_off(app, monkeypatch):
    """Explicitly off must be distinguishable from unset, or "false"
    would silently mean "ask the database"."""
    from app.services.recommendation_service import llm_recommendations_enabled

    with app.app_context():
        SystemSetting.set("use_llm_recommendations", "true")

        monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
        assert llm_recommendations_enabled() is False


def test_without_the_variable_the_admin_toggle_still_decides(app, monkeypatch):
    """The admin UI has to keep working for anyone who is not setting
    the variable at all."""
    from app.services.recommendation_service import llm_recommendations_enabled

    monkeypatch.delenv("USE_LLM_RECOMMENDATIONS", raising=False)
    with app.app_context():
        SystemSetting.set("use_llm_recommendations", "true")
        assert llm_recommendations_enabled() is True
        SystemSetting.set("use_llm_recommendations", "false")
        assert llm_recommendations_enabled() is False


# ---------------------------------------------------------------------
# 3. Failures are recorded, not swallowed
# ---------------------------------------------------------------------

def test_a_missing_key_says_so_instead_of_saying_nothing(app):
    from app.services import llm_service

    with app.app_context():
        app.config["OPENAI_API_KEY"] = ""
        assert llm_service._generate_with_openai("hi") is None

        failure = llm_service.last_failure()

    assert failure, "the failure was swallowed -- nothing to show the operator"
    assert failure["stage"] == "no_client"
    assert "OPENAI_API_KEY" in failure["detail"]


def test_an_api_error_records_the_model_it_was_asking_for(app, monkeypatch):
    """Pointing OPENAI_BASE_URL at Gemini while OPENAI_MODEL is still
    the default gpt-4o-mini is the single most common way to get a 404
    here, and the error is useless without naming the model."""
    from app.services import llm_service

    class _Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**_kwargs):
                    raise RuntimeError("Error code: 404 - model not found")

    with app.app_context():
        app.config["OPENAI_MODEL"] = "gpt-4o-mini"
        monkeypatch.setattr(llm_service, "_get_openai_client", lambda: _Boom)

        assert llm_service._generate_with_openai("hi") is None
        failure = llm_service.last_failure()

    assert failure["stage"] == "api_call"
    assert "gpt-4o-mini" in failure["detail"]
    assert "404" in failure["detail"]


def _city():
    return {"barangays_scored": 3, "median_competitors": 6,
            "median_residents_per_business": 1200, "median_density": 900}


def _card(location="Poblacion", **overrides):
    card = {
        "location": location, "competitor_count": 12, "population": 8400,
        "residents_per_business": 700, "population_density": 1500,
        "saturation_index": 61.5, "viability_score": 3.9, "confidence_level": 82.0,
        "roi_timeframe": "11-17 months", "roi_basis": "plan",
        "competitor_is_live": True, "reasons": ["original"], "risks": ["original"],
        "generated_by": "rule_based",
    }
    card.update(overrides)
    return card


def test_a_reply_that_is_not_json_is_reported_as_a_parse_failure(app, monkeypatch):
    from app.services import llm_service

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        monkeypatch.setitem(llm_service._GENERATORS, "openai", lambda _p: "I'm sorry Dave")
        monkeypatch.setitem(llm_service._GENERATORS, "anthropic", lambda _p: None)

        assert llm_service.generate_opportunity_cards_json("Cafe", _city(), [_card()]) == {}
        failure = llm_service.last_failure()

    assert failure["stage"] == "parse"


# ---------------------------------------------------------------------
# 3b. The native Gemini provider
# ---------------------------------------------------------------------
# Google AI Studio changed key issuance on 28 May 2026: new keys are
# "auth keys" (AQ.Ab...) bound to a Cloud service account. Sent the way
# the OpenAI SDK sends a key -- Authorization: Bearer -- Gemini's
# OpenAI-compatible endpoint refuses them, with 400 "Multiple
# authentication credentials received" or a 401 that reads like a bad
# key. The native endpoint takes the same key as x-goog-api-key and
# works. The older AIza keys that did work over Bearer are being
# retired, so this is the path that has a future.

class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (__import__("json").dumps(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _gemini_ok(text):
    return _FakeResponse(200, {"candidates": [
        {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
    ]})


def test_gemini_sends_the_key_as_a_header_and_never_in_the_url(app, monkeypatch):
    """Two separate requirements in one test because they are the same
    decision: the key travels in x-goog-api-key, and not as a `?key=`
    query parameter, because URLs end up in access logs and error
    reports and that is how credentials leak."""
    import requests

    from app.services import llm_service

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers or {}, body=json or {})
        return _gemini_ok('{"ok": true}')

    with app.app_context():
        app.config["GEMINI_API_KEY"] = "AQ.AbSECRET_value_here"
        app.config["GEMINI_MODEL"] = "gemini-3-flash"
        app.config["GEMINI_BASE_URL"] = "https://generativelanguage.googleapis.com/v1beta"
        monkeypatch.setattr(requests, "post", fake_post)

        assert llm_service._generate_with_gemini("hi") == '{"ok": true}'

    assert seen["headers"].get("x-goog-api-key") == "AQ.AbSECRET_value_here"
    assert "Authorization" not in seen["headers"], (
        "an auth key sent as a Bearer token is exactly what Gemini refuses"
    )
    assert "key=" not in seen["url"], "the API key must never travel in the URL"
    assert seen["url"].endswith("/models/gemini-3-flash:generateContent")


def test_gemini_asks_the_api_itself_for_json(app, monkeypatch):
    """responseMimeType is a guarantee from the API. Asking for JSON in
    the prompt and hoping is what the other providers have to do."""
    import requests

    from app.services import llm_service

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(json or {})
        return _gemini_ok('{"ok": true}')

    with app.app_context():
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test"
        monkeypatch.setattr(requests, "post", fake_post)
        llm_service._generate_with_gemini("hi")

    assert seen["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["contents"][0]["parts"][0]["text"] == "hi"


def test_gemini_request_is_written_for_gemini_3_models(app, monkeypatch):
    """Gemini 3.x ignores or rejects sampling parameters, thinks before it
    answers, and counts the thinking as output. So: no temperature, a low
    thinking level, and an output allowance large enough that thinking
    cannot use it all up before the answer is written."""
    import requests

    from app.services import llm_service

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(json or {})
        seen["timeout"] = timeout
        return _gemini_ok('{"ok": true}')

    with app.app_context():
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test"
        monkeypatch.setattr(requests, "post", fake_post)
        assert llm_service._generate_with_gemini("hi") == '{"ok": true}'

    config = seen["generationConfig"]
    for unsupported in ("temperature", "topP", "topK", "top_p", "top_k"):
        assert unsupported not in config, unsupported
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}
    assert config["maxOutputTokens"] >= 8192
    assert seen["timeout"] <= 30, "several calls in one request must stay inside gunicorn's 120 s"


def test_gemini_retries_without_thinking_when_the_setting_is_refused(app, monkeypatch):
    """A model that refuses the thinking setting answers 400 naming it.
    That must cost one retry, not the transcript."""
    import requests

    from app.services import llm_service

    bodies = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(json["generationConfig"])
        if "thinkingConfig" in json["generationConfig"]:
            return _FakeResponse(400, {"error": {"message": "Unknown name \"thinkingConfig\""}},
                                 text='{"error": {"message": "Unknown name thinkingConfig"}}')
        return _gemini_ok('{"ok": true}')

    with app.app_context():
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test"
        monkeypatch.setattr(requests, "post", fake_post)
        assert llm_service._generate_with_gemini("hi") == '{"ok": true}'

    assert len(bodies) == 2 and "thinkingConfig" not in bodies[1]


def test_gemini_thought_parts_are_not_part_of_the_answer(app, monkeypatch):
    import requests

    from app.services import llm_service

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(200, {"candidates": [{"content": {"parts": [
            {"text": "Let me think about the figures...", "thought": True},
            {"text": '{"ok": true}'},
        ]}, "finishReason": "STOP"}]})

    with app.app_context():
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test"
        monkeypatch.setattr(requests, "post", fake_post)
        assert llm_service._generate_with_gemini("hi") == '{"ok": true}'


def test_gemini_reports_googles_own_error(app, monkeypatch):
    """A 401, a 404 for an unknown model and a 429 for a spent quota
    are three different problems with three different fixes. Google
    says which; the point is to pass that on rather than swallow it."""
    import requests

    from app.services import llm_service

    with app.app_context():
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test"
        app.config["GEMINI_MODEL"] = "gemini-3-flash"
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(
            429, text='{"error":{"message":"Quota exceeded","status":"RESOURCE_EXHAUSTED"}}'
        ))

        assert llm_service._generate_with_gemini("hi") is None
        failure = llm_service.last_failure()

    assert failure["provider"] == "gemini"
    assert failure["stage"] == "api_call"
    assert "429" in failure["detail"]
    assert "RESOURCE_EXHAUSTED" in failure["detail"]
    assert "gemini-3-flash" in failure["detail"]


def test_a_blocked_response_reports_the_finish_reason(app, monkeypatch):
    """An empty answer is usually a safety block, and "empty" is not a
    diagnosis."""
    import requests

    from app.services import llm_service

    with app.app_context():
        app.config["GEMINI_API_KEY"] = "AQ.Ab_test"
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(
            200, {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
        ))

        assert llm_service._generate_with_gemini("hi") is None
        assert "SAFETY" in llm_service.last_failure()["detail"]


def test_the_gemini_key_falls_back_to_the_openai_slot(monkeypatch):
    """So a deployment that already pasted the key into OPENAI_API_KEY
    starts working by changing LLM_PROVIDER alone."""
    monkeypatch.setenv("OPENAI_API_KEY", "AQ.Ab_already_here")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    import importlib

    from app import config as config_module

    importlib.reload(config_module)
    try:
        assert config_module.Config.GEMINI_API_KEY == "AQ.Ab_already_here"
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)


def test_the_status_warns_about_an_auth_key_on_the_compatibility_endpoint(app):
    """The single misconfiguration that produces a 401 reading like a
    bad key when the key is fine. It cost a real afternoon, so the
    diagnostics name it."""
    from app.services import llm_service

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "AQ.Ab8RN6ITOh9KaU6CF4C0u3g"
        app.config["OPENAI_BASE_URL"] = "https://generativelanguage.googleapis.com/v1beta/openai/"
        status = llm_service.llm_status()

    assert "warning" in status
    assert "LLM_PROVIDER=gemini" in status["warning"]


def test_no_warning_when_the_setup_is_coherent(app):
    from app.services import llm_service

    with app.app_context():
        app.config["LLM_PROVIDER"] = "gemini"
        app.config["GEMINI_API_KEY"] = "AQ.Ab8RN6ITOh9KaU6CF4C0u3g"
        app.config["OPENAI_BASE_URL"] = ""
        assert "warning" not in llm_service.llm_status()


# ---------------------------------------------------------------------
# 4. The diagnostics endpoint, and what it must never print
# ---------------------------------------------------------------------

def test_the_status_never_returns_the_api_key(app):
    """It reports set/not-set, the length and the first four
    characters. Four is enough to tell an AI Studio key ("AIza") from
    an OAuth token ("AQ.") -- the mistake that actually happens -- and
    is no use to anyone who steals it."""
    from app.services import llm_service

    secret = "AIzaSyTOTALLY_NOT_A_REAL_KEY_1234567890"
    with app.app_context():
        app.config["OPENAI_API_KEY"] = secret
        status = llm_service.llm_status()

    flat = repr(status)
    assert secret not in flat
    assert secret[4:] not in flat
    assert status["openai_key"] == {"set": True, "length": len(secret), "starts_with": "AIza"}


def test_an_error_message_cannot_leak_the_key(app):
    """Provider SDKs sometimes echo request details into exception
    text, so the recorded message is scrubbed."""
    from app.services import llm_service

    secret = "AIzaSyTOTALLY_NOT_A_REAL_KEY_1234567890"
    with app.app_context():
        app.config["OPENAI_API_KEY"] = secret
        llm_service._record_failure("openai", "api_call", f"401 using {secret} at ...")
        failure = llm_service.last_failure()

    assert secret not in failure["detail"]
    assert "***redacted***" in failure["detail"]


def test_the_gemini_key_is_redacted_too(app):
    """Gemini is now asked first for every forecast narration, so its
    errors are the ones most often recorded."""
    from app.services import llm_service

    secret = "AQ.AbTOTALLY_NOT_A_REAL_GEMINI_KEY_123"
    with app.app_context():
        app.config["GEMINI_API_KEY"] = secret
        llm_service._record_failure("gemini", "api_call", f"HTTP 401 for {secret}")
        failure = llm_service.last_failure()

    assert secret not in failure["detail"]
    assert "***redacted***" in failure["detail"]


def test_a_real_error_is_not_overwritten_by_a_later_missing_key(app):
    """The original rule: Gemini's 401 first, then "no key" from the
    fallbacks -- the 401 is the one worth reading."""
    from app.services import llm_service

    with app.app_context():
        llm_service._begin_attempt()
        llm_service._record_failure("gemini", "api_call", "HTTP 401: API key not valid")
        llm_service._record_failure("openai", "no_client", "OPENAI_API_KEY is not set")
        failure = llm_service.last_failure()

    assert failure["provider"] == "gemini"
    assert failure["stage"] == "api_call"


def test_a_skip_gives_way_to_a_later_real_error(app):
    """The one exception to first-failure-wins. The forecast narration
    asks Gemini first even where the operator never chose it, and passes
    over it when it has no usable key; that skip must not hide the
    error from the provider the operator did configure."""
    from app.services import llm_service

    with app.app_context():
        llm_service._begin_attempt()
        llm_service._record_failure("gemini", "skipped", "GEMINI_API_KEY is not set")
        llm_service._record_failure("openai", "api_call", "model=openai/gpt-4o-mini: Error code: 404")
        llm_service._record_failure("anthropic", "no_client", "ANTHROPIC_API_KEY is not set")
        failure = llm_service.last_failure()

    assert failure["provider"] == "openai"
    assert failure["stage"] == "api_call"
    assert "MODEL NAME" in failure["hint"], "the hint is recomputed for the replacing failure"


def test_a_skip_gives_way_to_a_later_missing_client_too(app):
    """...including a precondition failure: an OpenAI client that could
    not be built is the real cause, not "Gemini skipped"."""
    from app.services import llm_service

    with app.app_context():
        llm_service._begin_attempt()
        llm_service._record_failure("gemini", "skipped", "the Gemini key is an sk-... key")
        llm_service._record_failure("openai", "no_client", "OpenAI client could not be built: TypeError")
        failure = llm_service.last_failure()

    assert failure["provider"] == "openai"
    assert failure["stage"] == "no_client"


def test_a_missing_client_is_not_replaced_by_a_fallbacks_rejection(app, monkeypatch):
    """First-failure-wins still holds for every other record, no_client
    included. LLM_PROVIDER=openai with the `openai` package broken: the
    alert wording falls through to Gemini, which is handed the inherited
    sk- key and answers 400 "API key not valid". The package is the
    cause; pointing the operator at the Gemini key would send them to
    the wrong provider."""
    import sys

    import requests

    from app.services import llm_service

    class _Rejected:
        status_code = 400
        text = '{"error": {"message": "API key not valid. Please pass a valid API key."}}'

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "sk-or-v1-0000000000000000"
        app.config["GEMINI_API_KEY"] = "sk-or-v1-0000000000000000"
        app.config["ANTHROPIC_API_KEY"] = ""
        monkeypatch.setitem(sys.modules, "openai", None)  # `from openai import OpenAI` now fails
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Rejected())

        assert llm_service.generate_alert_summary("anything") is None
        failure = llm_service.last_failure()

    assert failure["provider"] == "openai"
    assert failure["stage"] == "no_client"
    assert "could not be imported" in failure["detail"]


def test_the_status_endpoint_is_admin_only(app):
    with app.app_context():
        sme = User(name="SME", email="sme@llm.test", role="SME")
        sme.set_password("password123")
        db.session.add(sme)
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"email": "sme@llm.test", "password": "password123"},
                follow_redirects=True)
    response = client.get("/admin/llm-status", follow_redirects=False)

    assert response.status_code in (302, 403), (
        "an SME could read the LLM diagnostics"
    )


def test_an_admin_gets_the_configuration_back(app):
    with app.app_context():
        admin = User(name="Admin", email="admin@llm.test", role="Admin")
        admin.set_password("password123")
        db.session.add(admin)
        db.session.commit()
        app.config["OPENAI_MODEL"] = "gemini-3-flash"

    client = app.test_client()
    client.post("/login", data={"email": "admin@llm.test", "password": "password123"},
                follow_redirects=True)
    payload = client.get("/admin/llm-status").get_json()

    assert payload["status"]["openai_model"] == "gemini-3-flash"
    assert "provider" in payload["status"]
    assert "enabled" in payload["status"]


# ---------------------------------------------------------------------
# 5. The write-up is cached, because the free tier is 10 requests/minute
# ---------------------------------------------------------------------

def test_the_write_up_is_not_regenerated_for_an_unchanged_page(app, monkeypatch):
    """Google AI Studio's free Gemini tier allows roughly 10 requests a
    minute. Without caching, clicking between plans during a demo
    exhausts it in under a minute and the page silently reverts to
    rule-based wording while being demonstrated."""
    from app.services import location_opportunity_service as los

    calls = {"n": 0}

    def fake_cards(_industry, _city, cards):
        calls["n"] += 1
        return {
            card["location"]: {"reasons": ["ai reason"], "risks": ["ai risk"],
                               "generated_by": "llm:openai"}
            for card in cards
        }

    with app.app_context():
        monkeypatch.setattr(
            "app.services.llm_service.generate_opportunity_cards_json", fake_cards
        )
        cards = [{"location": "Poblacion"}]
        city = {"barangays_scored": 1}

        first = los._written_cards("Cafe", city, cards)
        second = los._written_cards("Cafe", city, cards)

    assert first == second
    assert calls["n"] == 1, f"the LLM was called {calls['n']} times for one unchanged page"


def test_a_failed_write_up_is_not_cached(app, monkeypatch):
    """A rate-limit blip must not condemn the page to rule-based
    wording for the whole cache lifetime -- the next request should try
    again."""
    from app.services import location_opportunity_service as los

    calls = {"n": 0}

    def flaky(_industry, _city, cards):
        calls["n"] += 1
        if calls["n"] == 1:
            return {}          # rate limited
        return {card["location"]: {"reasons": ["ai reason"], "risks": ["ai risk"],
                                   "generated_by": "llm:openai"} for card in cards}

    with app.app_context():
        monkeypatch.setattr(
            "app.services.llm_service.generate_opportunity_cards_json", flaky
        )
        cards = [{"location": "Poblacion"}]
        city = {"barangays_scored": 1}

        assert los._written_cards("Cafe", city, cards) == {}
        second = los._written_cards("Cafe", city, cards)

    assert calls["n"] == 2, "the empty result was cached, so the retry never happened"
    assert second["Poblacion"]["generated_by"] == "llm:openai"


# ---------------------------------------------------------------------
# 6. The numbers stay the model's, not the LLM's
# ---------------------------------------------------------------------

def test_the_llm_only_rewrites_wording_never_the_figures(app, monkeypatch):
    """The point of the whole design. An LLM that could move the
    saturation index or the competitor count would take away the thing
    that makes this defensible: the numbers come from the Random
    Forest and from real PSA/Places data, and the model is only
    allowed to write sentences about them.

    The fake here is the LLM's RAW TEXT, not the parser -- so the real
    validation in generate_opportunity_cards_json is what has to reject
    the model's attempts to move a figure.
    """
    import json as _json

    from app.services import llm_service

    lying_response = _json.dumps({"cards": [{
        "location": "Poblacion",
        "reasons": ["rewritten"], "risks": ["rewritten"],
        # every field below is an attempt to move a number
        "saturation_index": 0.0, "viability_score": 10.0,
        "competitor_count": 0, "population": 999999,
        "roi_timeframe": "immediately, honestly",
    }]})

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        monkeypatch.setitem(llm_service._GENERATORS, "openai", lambda _p: lying_response)

        written = llm_service.generate_opportunity_cards_json("Cafe", _city(), [_card()])

    entry = written["Poblacion"]

    # Only wording and provenance survive the parser.
    assert entry["reasons"] == ["rewritten"]
    assert entry["generated_by"] == "llm:openai"
    assert set(entry) <= {"reasons", "risks", "generated_by", "roi_timeframe"}, (
        f"the parser passed through fields the model should not control: "
        f"{sorted(set(entry) - {'reasons', 'risks', 'generated_by', 'roi_timeframe'})}"
    )
    assert "roi_timeframe" not in entry, (
        "an ROI value that is not a month range must be rejected, leaving the "
        "computed window in place"
    )


def test_a_sane_roi_window_from_the_model_is_accepted(app, monkeypatch):
    """The counterpart to the test above: the ROI window is the one
    figure the model is allowed to adjust, and only when it parses as a
    plausible month range."""
    import json as _json

    from app.services import llm_service

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        monkeypatch.setitem(llm_service._GENERATORS, "openai", lambda _p: _json.dumps(
            {"cards": [{"location": "Poblacion", "reasons": ["r"], "risks": ["k"],
                        "roi_timeframe": "9-14 months"}]}
        ))
        written = llm_service.generate_opportunity_cards_json("Cafe", _city(), [_card()])

    assert written["Poblacion"]["roi_timeframe"] == "9-14 months"


# ---------------------------------------------------------------------
# THE KEY FORMAT DECIDES THE TRANSPORT
# ---------------------------------------------------------------------
# Google retired the `AIza` Standard key for the Gemini API -- unrestricted
# ones began being rejected on 19 June 2026, the format was end-of-lifed
# through September 2026 -- so an AI Studio key is now an `AQ.`-prefixed
# auth key. That format is accepted on the NATIVE endpoint
# (x-goog-api-key) and refused on the OpenAI-compatibility endpoint,
# where the same key sent as `Authorization: Bearer` returns HTTP 400
# "Multiple authentication credentials received" or a 401 calling the key
# invalid.
#
# A deployment that sets LLM_PROVIDER=openai, points OPENAI_BASE_URL at
# Gemini's /openai/ path and supplies an `AQ.` key is therefore
# guaranteed to fail its first attempt on every single call. The key's
# own prefix says which transport can carry it, so _provider_order()
# corrects for it.

def test_an_aq_key_is_sent_natively_rather_than_as_a_bearer_token(app):
    """The correction, which is what makes an AQ. key work at all."""
    from app.services.llm_service import _provider_order

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "AQ.Ab0000000000000000000000000000"

        assert _provider_order()[0] == "gemini"


def test_a_real_openai_key_is_left_alone(app):
    """Deliberately narrow. The point is to stop a guaranteed-failing
    attempt, not to second-guess a configuration that works."""
    from app.services.llm_service import _provider_order

    with app.app_context():
        app.config["LLM_PROVIDER"] = "openai"
        app.config["OPENAI_API_KEY"] = "sk-proj-0000000000000000"

        assert _provider_order()[0] == "openai"


def test_an_explicitly_chosen_provider_is_never_overridden(app):
    from app.services.llm_service import _provider_order

    with app.app_context():
        app.config["OPENAI_API_KEY"] = "AQ.Ab0000000000000000000000000000"
        for provider in ("anthropic", "gemini"):
            app.config["LLM_PROVIDER"] = provider
            assert _provider_order()[0] == provider


def test_an_unknown_provider_name_does_not_lose_every_fallback(app):
    """A typo in LLM_PROVIDER used to put a non-existent generator at the
    head of the list. Harmless in itself -- it is skipped -- but it also
    meant the list held four entries for three generators, and the
    "configured provider" reported in diagnostics was a name that does
    not exist."""
    from app.services.llm_service import _GENERATORS, _provider_order

    with app.app_context():
        app.config["LLM_PROVIDER"] = "gemeni"  # typo
        app.config["OPENAI_API_KEY"] = "sk-proj-0000000000000000"

        order = _provider_order()
        assert set(order) == set(_GENERATORS)
        assert len(order) == len(_GENERATORS)


def test_every_generator_is_reachable_as_a_fallback(app):
    """Whatever is configured, all three remain in the order. A provider
    that drops out of the list is a key that can never be used."""
    from app.services.llm_service import _GENERATORS, _provider_order

    with app.app_context():
        for provider in list(_GENERATORS) + ["", "nonsense"]:
            app.config["LLM_PROVIDER"] = provider
            app.config["OPENAI_API_KEY"] = "AQ.Ab0000000000000000000000000000"
            assert set(_provider_order()) == set(_GENERATORS), provider


# ---------------------------------------------------------------------
# THE MODEL NAME
# ---------------------------------------------------------------------
# A 404 on a model name and a 401 on a key produce the same visible
# symptom -- rule-based text with no explanation -- and the natural
# response to both is to regenerate the key, which fixes only one.

def test_the_default_model_is_not_the_name_that_does_not_exist(app):
    """The default was `gemini-3-flash`, which Google does not serve:
    the stable Flash line is numbered in tenths. That produced a 404 on
    every call, indistinguishable from a bad key."""
    from app.config import Config

    assert Config.GEMINI_MODEL != "gemini-3-flash"
    assert re.match(r"^gemini-\d+\.\d+-flash(-lite)?$", Config.GEMINI_MODEL), \
        Config.GEMINI_MODEL


def test_a_404_blames_the_model_not_the_key(app):
    from app.services import llm_service

    with app.app_context():
        llm_service._begin_attempt()
        llm_service._record_failure(
            "gemini", "api_call",
            "model=gemini-3-flash: HTTP 404: models/gemini-3-flash is not found "
            "for API version v1beta",
        )
        hint = llm_service.last_failure()["hint"]

        assert "MODEL NAME" in hint
        assert "GEMINI_MODEL" in hint


def test_a_bearer_rejection_points_at_the_provider_setting(app):
    from app.services import llm_service

    with app.app_context():
        llm_service._begin_attempt()
        llm_service._record_failure(
            "openai", "api_call",
            "HTTP 400: Multiple authentication credentials received. Please pass only one.",
        )
        hint = llm_service.last_failure()["hint"]

        assert "LLM_PROVIDER=gemini" in hint
        assert "OPENAI_BASE_URL" in hint


def test_a_quota_failure_is_described_as_temporary(app):
    """So nobody spends an afternoon rotating a key that was fine."""
    from app.services import llm_service

    with app.app_context():
        llm_service._begin_attempt()
        llm_service._record_failure("gemini", "api_call",
                                    "HTTP 429: Quota exceeded for quota metric")
        hint = llm_service.last_failure()["hint"]

        assert "temporary" in hint.lower()


def test_a_plain_missing_key_gets_no_misleading_hint(app):
    """The default install. Not an incident, and not something to
    attach troubleshooting advice to."""
    from app.services import llm_service

    with app.app_context():
        llm_service._begin_attempt()
        llm_service._record_failure("anthropic", "no_client",
                                    "ANTHROPIC_API_KEY is not set")

        assert "hint" not in llm_service.last_failure()
