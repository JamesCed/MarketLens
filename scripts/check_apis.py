"""
check_apis.py
----------------------------------
Answers one question, out loud: are the two external APIs this project
depends on actually working RIGHT NOW, with the keys in your .env?

    python check_apis.py

WHY THIS EXISTS. Both integrations are written to fail SOFTLY, which is
correct behaviour for a live site and terrible for finding out whether
they work:

  * app/services/llm_service.py wraps the whole OpenAI/OpenRouter call
    in `try: ... except Exception: return None`, and
    recommendation_service then quietly writes the rule-based text
    instead. A bad key, an expired key, or no credit all produce a
    normal-looking Recommendations page. Nothing on screen says the AI
    never answered.

  * app/services/places_service.py falls back to a clearly-flagged
    simulated competitor list when Google refuses. That flag reaches
    the page, but it looks the same as "this barangay was never
    fetched".

So neither page can tell you the key is good. This script asks each API
directly and prints the raw answer, including the error text when there
is one -- that error ("insufficient credits", "API key not valid",
"REQUEST_DENIED") is usually the entire diagnosis.

It sends ONE tiny request to each: a 5-token chat completion and a
single Text Search. Nothing is written to your database.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    """Reads .env the same way the app does, without needing Flask."""
    values = {}
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        print(f"!! No .env found at {path}")
        return values
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def post_json(url, payload, headers, timeout=45):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def check_llm(env):
    print("=" * 66)
    print("1. AI recommendations  (llm_service.py)")
    print("=" * 66)

    provider = (env.get("LLM_PROVIDER") or "openai").strip().lower()
    print(f"   provider  : {provider}")

    if provider != "openai":
        print("   SKIPPED: this script only checks the openai/OpenRouter path.")
        return None

    key = env.get("OPENAI_API_KEY", "")
    base = (env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = env.get("OPENAI_MODEL") or "gpt-4o-mini"
    print(f"   base_url  : {base}")
    print(f"   model     : {model}")
    print(f"   api key   : {'set, ' + str(len(key)) + ' chars' if key else 'MISSING'}")

    if not key:
        print("\n   VERDICT: NO -- there is no key, so every recommendation on")
        print("            the site is the rule-based text, not the AI.")
        return False

    try:
        import openai

        print(f"   openai pkg: installed ({getattr(openai, '__version__', '?')})")
    except ImportError:
        print("   openai pkg: NOT INSTALLED for this interpreter")
        print("               Run this with the SAME python the app uses --")
        print("               activate venv first, then: python check_apis.py")
        print("\n   VERDICT: NO -- llm_service catches the missing import and")
        print("            falls back silently. Fix: pip install openai")
        return False

    try:
        status, payload = post_json(
            f"{base}/chat/completions",
            {
                "model": model,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            },
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")[:500]
        print(f"\n   HTTP {err.code}")
        print(f"   {body}")
        print("\n   VERDICT: NO -- the API refused. 401 = bad/revoked key,")
        print("            402 = out of credit, 404 = wrong model name for")
        print("            this provider. The site still works; every")
        print("            recommendation is just the rule-based text.")
        return False
    except Exception as err:  # network, DNS, TLS, timeout
        print(f"\n   FAILED: {type(err).__name__}: {err}")
        print("\n   VERDICT: NO -- could not reach the API at all.")
        return False

    reply = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
    print(f"\n   HTTP {status}")
    print(f"   answered  : {reply!r}")
    print(f"   served by : {payload.get('model')}")
    print("\n   VERDICT: YES -- the AI answers, so Recommendations are")
    print("            LLM-written (when use_llm_recommendations is on in")
    print("            Admin > System Settings, which it is by default).")
    return True


def check_places(env):
    print()
    print("=" * 66)
    print("2. Google Places  (places_service.py)")
    print("=" * 66)

    key = env.get("GOOGLE_PLACES_API_KEY", "")
    print(f"   api key   : {'set, ' + str(len(key)) + ' chars' if key else 'MISSING'}")
    if not key:
        print("\n   VERDICT: NO -- no key, so every competitor count is a")
        print("            simulated fallback.")
        return False

    url = "https://maps.googleapis.com/maps/api/place/textsearch/json?" + urllib.parse.urlencode(
        {"query": "restaurants in San Nicolas, Tarlac City, Philippines", "key": key}
    )
    try:
        with urllib.request.urlopen(url, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as err:
        print(f"\n   FAILED: {type(err).__name__}: {err}")
        print("\n   VERDICT: NO -- could not reach Google at all.")
        return False

    status = payload.get("status")
    results = payload.get("results", [])
    print(f"   status    : {status}")
    print(f"   results   : {len(results)} on page 1"
          f"{' (more pages available)' if payload.get('next_page_token') else ''}")
    if payload.get("error_message"):
        print(f"   message   : {payload['error_message']}")
    for item in results[:3]:
        print(f"     - {item.get('name')}")

    if status in ("OK", "ZERO_RESULTS"):
        print("\n   VERDICT: YES -- Google answered with real businesses.")
        print("            NOTE: Text Search caps at 60 results (20 x 3 pages),")
        print("            so any competitor_count of exactly 60 means")
        print("            '60 or more', not 'exactly 60'.")
        return True

    print("\n   VERDICT: NO -- REQUEST_DENIED usually means the Places API is")
    print("            not enabled on the project, or the key is restricted")
    print("            to the wrong referrer/IP. OVER_QUERY_LIMIT means")
    print("            billing or quota. Either way the app is serving")
    print("            simulated counts.")
    return False


def main():
    print(f"python    : {sys.executable}")
    env = load_env()
    llm_ok = check_llm(env)
    places_ok = check_places(env)

    print()
    print("=" * 66)
    print("SUMMARY")
    print("=" * 66)

    def word(value):
        return {True: "YES", False: "NO", None: "SKIPPED"}[value]

    print(f"   AI API      : {word(llm_ok)}")
    print(f"   Places API  : {word(places_ok)}")
    return 0 if (llm_ok is not False and places_ok is not False) else 1


if __name__ == "__main__":
    sys.exit(main())
