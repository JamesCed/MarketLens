"""
app/services/places_service.py
--------------------------------
Google Places API (NEW) integration -- Text Search
(POST https://places.googleapis.com/v1/places:searchText).

This replaces the legacy `maps.googleapis.com/maps/api/place/textsearch`
GET endpoint used by an earlier version of this project. The New Places
API is what Google now recommends for new integrations (the legacy
Places API is on a deprecation path), and it's also what lets every
request be HARD-CONSTRAINED to Tarlac City only via `locationRestriction`
(a real lat/lng rectangle) -- the legacy API only supported a soft
"location" + "radius" bias, which could still return results outside
the city.

WHY locationRestriction (a rectangle), NOT locationBias:
  - locationBias just nudges ranking towards an area -- Google can and
    will still return places outside it.
  - locationRestriction is a hard filter -- Google will ONLY return
    places whose location falls inside the given rectangle. That's
    exactly "fetch real data across Tarlac City ONLY" from the current
    task, so this file always sends locationRestriction, never
    locationBias.

TARLAC_CITY_BOUNDS below is an approximate bounding rectangle around
Tarlac City's built-up area and its 76 barangays (center ~15.4869N,
120.5900E per Tarlac City's Wikipedia infobox; the city's official
land area is ~274.66 km2, but there is no publicly published exact
polygon/boundary GeoJSON this project could source -- see
app/ml/seed_data.py's own docstring for the same "no official
barangay-level geospatial dataset exists" gap). The rectangle below is
sized generously so every real barangay is inside it, at the cost of
also covering a modest buffer of neighboring municipalities' borders --
Google's own place `formattedAddress` string is what still ultimately
says whether a specific result is actually "..., Tarlac City" (see
_is_in_tarlac_city below, used as an extra post-filter). If you have a
more precise city-boundary polygon, tightening this rectangle only
makes results more precise, never breaks anything else in this file.

Get a key: https://console.cloud.google.com/google/maps-apis
Enable: "Places API (New)". Billing must be enabled on the project.
Which file to put your key in: see .env -> GOOGLE_PLACES_API_KEY=...
(app/config.py reads it from there -- never hardcode a key in this
file).

If GOOGLE_PLACES_API_KEY is not configured, or a request fails for any
reason (offline, quota, bad/unknown location string), this
transparently falls back to a clearly-labeled SIMULATED competitor
list, matching the paper's own Limitations section: "in cases where
data is not available in real-time, the system utilizes AI-driven
simulation ... to estimate current market conditions." This means the
app is fully demoable before you ever touch a Google Cloud console.
"""

import random

import requests

SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

TARLAC_CITY_BOUNDS = {
    "low": {"latitude": 15.38, "longitude": 120.48},
    "high": {"latitude": 15.60, "longitude": 120.70},
}

FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.types"

PAGE_SIZE = 20
MAX_PAGES = 3
HARD_RESULT_CEILING = PAGE_SIZE * MAX_PAGES

SEARCH_TERM_MAP = {
    "Agriculture, Forestry, and Fishing":
        "farm OR agricultural producer OR poultry farm OR fishery OR agri-processing",
    "Mining and Quarrying":
        "quarry OR sand and gravel supplier OR aggregates supplier OR mining company",
    "Manufacturing":
        "manufacturer OR factory OR food processing plant OR furniture maker OR garments manufacturer OR printing press",
    "Electricity, Gas, Steam, and Air Conditioning Supply":
        "electric cooperative OR power utility OR LPG distributor OR industrial gas supplier",
    "Water Supply; Sewerage, Waste Management, and Remediation Activities":
        "water district OR water supply company OR waste management services OR septic tank services",
    "Construction":
        "construction company OR general contractor OR builder OR electrical and plumbing contractor",
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles":
        "grocery store OR supermarket OR hardware store OR wholesale distributor OR auto repair shop OR motorcycle dealer",
    "Transportation and Storage":
        "trucking company OR logistics company OR courier service OR bus terminal OR warehouse",
    "Accommodation and Food Service Activities":
        "hotel OR inn OR resort OR restaurant OR catering service",
    "Food and Beverage":
        "bakeshop OR cafe OR coffee shop OR beverage manufacturer OR food products company",
    "Information and Communication":
        "IT services company OR software company OR telecommunications office OR publishing house OR radio station",
    "Financial and Insurance Activities":
        "bank OR lending company OR insurance agency OR pawnshop OR money transfer service",
    "Real Estate Activities":
        "real estate agency OR property developer OR realty office OR property management",
    "Professional, Scientific, and Technical Activities":
        "law office OR accounting firm OR engineering firm OR architectural firm OR consulting firm OR veterinary clinic",
    "Administrative and Support Service Activities":
        "manpower agency OR security agency OR travel agency OR janitorial services OR equipment rental",
    "Education":
        "school OR college OR review center OR tutorial center OR training center OR driving school",
    "Human Health and Social Work Activities":
        "hospital OR medical clinic OR dental clinic OR pharmacy OR diagnostic laboratory OR care facility",
    "Arts, Entertainment, and Recreation":
        "gym OR fitness center OR event venue OR sports complex OR amusement center OR cinema",
    "Other Service Activities":
        "salon OR spa OR laundry service OR appliance repair shop OR funeral services",
    "Activities of Households as Employers":
        "household staffing agency OR domestic helper agency OR household services",
}

MICRO_BUSINESS_PATTERNS = [
    "sari-sari", "sari sari", "sarisari", "tindahan", "variety store",
    "carinderia", "karinderia", "karinderya", "turo-turo", "turo turo",
    "food cart", "foodcart", "street food", "fishball", "kwek-kwek",
    "kwek kwek", "isawan", "banana cue", "bananacue", "lugawan",
    "silogan", "eatery stall",
    "tiangge", "talipapa", "puesto", "market stall", "public market stall",
    "sidewalk", "ambulant",
    "piso wifi", "pisowifi", "piso-wifi", "pisonet", "piso net", "piso-net",
    "piso print", "pisoprint",
    "vulcanizing", "bulcanizing", "vulcanize",
    "backyard", "home-based", "home based",
]

MICRO_BUSINESS_TYPES = set()


def is_micro_business(name, types=None):
    """True when a Google Places result looks like a MICRO enterprise
    rather than an SME-scale establishment -- see the long note above
    MICRO_BUSINESS_PATTERNS for the definition being applied and why it
    is name-based. `name` is the place's display name; `types` is
    Google's own type list for it."""
    haystack = (name or "").lower()
    if any(pattern in haystack for pattern in MICRO_BUSINESS_PATTERNS):
        return True
    if types and MICRO_BUSINESS_TYPES.intersection({str(t).lower() for t in types}):
        return True
    return False


def _search_term_for(industry_type):
    return SEARCH_TERM_MAP.get(industry_type, industry_type)


def _simulate_competitors(industry_type, location):
    """Deterministic-per-(industry, location) fake competitor list,
    clearly flagged as simulated, so a demo doesn't show a different
    number every time the same page is refreshed."""
    seed = hash((industry_type, location)) % (2 ** 31)
    rng = random.Random(seed)
    n = rng.randint(2, 22)
    return [
        {"name": f"[Simulated] {industry_type} business #{i + 1}", "address": location, "simulated": True}
        for i in range(n)
    ]


def _is_in_tarlac_city(formatted_address):
    """Cheap extra safety net on top of locationRestriction -- Google's
    rectangle filter is a hard geographic box, but that box (see module
    docstring) is deliberately a little larger than the city itself so
    every barangay fits inside it. Places whose own formatted address
    doesn't mention Tarlac City at all are dropped so results stay
    "Tarlac City only" even for a place that happens to sit inside the
    rectangle but just across the city line."""
    if not formatted_address:
        return True
    return "tarlac city" in formatted_address.lower()


def live_fetch_enabled():
    """Whether this deployment is allowed to call the Google Places API at
    all. THE single gate: every outbound Places request in this codebase
    goes through search_competitors_detailed() below, so turning this off
    stops all of them -- the per-page background upgrades, the bulk
    "Fetch remaining" sweep, and the competitor sample the recommendation
    writer asks for.

    Off means the app reads the competitor counts already stored in
    `market_data` and falls back to a clearly-flagged estimate for any
    (industry, barangay) combo it has never seen. Nothing breaks; the
    maps and charts keep working on the data already collected.

    WHY A PUBLIC DEPLOYMENT SHOULD SET PLACES_LIVE_FETCH=false: a full
    city sweep is thousands of billable Google calls charged to whoever
    owns the key, and on a public URL the button that starts one is
    reachable by anyone who can log in. Locally it defaults to ON, so
    development is unchanged. See app/config.py and DEPLOYMENT.md.

    Outside an application context (CLI scripts, seed.py, tests) there is
    no config to read, so fetching is allowed -- those are run
    deliberately, by hand.
    """
    try:
        from flask import current_app, has_app_context

        if not has_app_context():
            return True
        return bool(current_app.config.get("PLACES_LIVE_FETCH", True))
    except (ImportError, RuntimeError):
        return True


def search_competitors_detailed(location, industry_type, api_key=None, max_results=0,
                                exclude_micro=True, search_term=None):
    """
    The full-detail version of search_competitors() (which is a thin
    wrapper over this one). Returns a dict:

        {
          "results":        [ {name, address, types, simulated}, ... ],
          "simulated":      bool,
          "excluded_micro": [ {name, address, types}, ... ],
        }

    `results` holds the SME-scale establishments this system counts as
    competitors. `excluded_micro` holds every place Google returned that
    the MICRO_BUSINESS_PATTERNS filter dropped -- kept, not discarded, so
    the count is auditable: you can show exactly which establishments
    were left out of a barangay's competitor_count and why. Pass
    exclude_micro=False to count every establishment (the pre-filter
    behavior).

    max_results applies to the KEPT results, so a barangay full of
    sari-sari stores doesn't silently eat the result budget: a dropped
    micro business doesn't count against the cap, and paging continues
    until enough SME-scale results are found or Google runs out.

    max_results=0 (or None) means UNLIMITED: keep paging until Google
    stops handing back a nextPageToken, so a busy barangay reports the
    real number of businesses it has instead of being truncated. Pass a
    positive number to cap it.

    HOW FAR "UNLIMITED" ACTUALLY GOES -- worth knowing when reading the
    numbers: Text Search returns at most 20 places per page and Google
    itself stops issuing nextPageToken after 3 pages, so 60 results per
    (industry x barangay) query is a HARD CEILING imposed by the API,
    not by this app. A combo sitting at exactly 60 should be read as
    "60 or more". Before this change the ceiling was the first 20 --
    which is why a dense barangay like Matatalaib flat-lined at 20 --
    and the app now collects every page Google is willing to give.

    Falls back to a simulated list when:
      - live fetching is switched off for this deployment
        (PLACES_LIVE_FETCH=false -- see below), OR
      - no API key is configured, OR
      - the location string is empty, OR
      - the HTTP request fails, times out, or Google returns an error.

    This function deliberately never raises -- a flaky external API
    should never crash a student's live demo.
    """
    if not live_fetch_enabled() or not api_key or not location or not str(location).strip():
        return {
            "results": _simulate_competitors(industry_type, location),
            "simulated": True,
            "excluded_micro": [],
        }

    limit = int(max_results or 0)
    unlimited = limit <= 0

    query = f"{search_term or _search_term_for(industry_type)} in {location}, Tarlac City"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": f"{FIELD_MASK},nextPageToken",
    }

    results = []
    excluded_micro = []
    seen_ids = set()
    page_token = None
    try:
        for _page in range(MAX_PAGES):
            body = {
                "textQuery": query,
                "locationRestriction": {"rectangle": TARLAC_CITY_BOUNDS},
                "regionCode": "PH",
                "languageCode": "en",
                "pageSize": PAGE_SIZE if unlimited else min(max(1, limit - len(results)), PAGE_SIZE),
            }
            if page_token:
                body["pageToken"] = page_token

            response = requests.post(SEARCH_TEXT_URL, json=body, headers=headers, timeout=10)
            if response.status_code != 200:
                if results:
                    break
                return {
                    "results": _simulate_competitors(industry_type, location),
                    "simulated": True,
                    "excluded_micro": [],
                }
            payload = response.json()

            for place in payload.get("places", []):
                address = place.get("formattedAddress")
                if not _is_in_tarlac_city(address):
                    continue
                place_id = place.get("id") or (place.get("displayName") or {}).get("text")
                if place_id and place_id in seen_ids:
                    continue
                if place_id:
                    seen_ids.add(place_id)

                name = (place.get("displayName") or {}).get("text")
                types = place.get("types", [])

                if exclude_micro and is_micro_business(name, types):
                    excluded_micro.append({"name": name, "address": address, "types": types})
                    continue

                results.append(
                    {
                        "name": name,
                        "address": address,
                        "types": types,
                        "simulated": False,
                    }
                )

            if not unlimited and len(results) >= limit:
                results = results[:limit]
                break

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return {"results": results, "simulated": False, "excluded_micro": excluded_micro}
    except (requests.RequestException, ValueError):
        if results:
            return {"results": results, "simulated": False, "excluded_micro": excluded_micro}
        return {
            "results": _simulate_competitors(industry_type, location),
            "simulated": True,
            "excluded_micro": [],
        }


def search_competitors(location, industry_type, api_key=None, max_results=0, exclude_micro=True):
    """Returns (results, simulated) -- the two values most callers need.
    See search_competitors_detailed() for the full result, including the
    list of micro businesses that were filtered out of the count."""
    detail = search_competitors_detailed(
        location, industry_type, api_key=api_key, max_results=max_results, exclude_micro=exclude_micro
    )
    return detail["results"], detail["simulated"]


def get_competitor_count(location, industry_type, api_key=None, max_results=0):
    """Convenience wrapper for callers that only need the count (e.g.
    forecasting_service.find_or_create_market_data). Returns
    (competitor_count, simulated). max_results=0 means unlimited -- see
    search_competitors(). The count EXCLUDES micro businesses, matching
    this system's SME scope."""
    results, simulated = search_competitors(location, industry_type, api_key=api_key, max_results=max_results)
    return len(results), simulated
