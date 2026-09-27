"""
tests/test_analytics_and_geocoding.py
----------------------------------------
Regression tests for three defects that were visible on screen:

  1. The Trend Reports "Industry Distribution" pie rendered as a single
     100%-wide slice, and "Total Businesses" read 1, on a database that
     held thousands of market_data rows. Cause: both figures counted
     ONLY rows whose source was exactly "Google Places API" and threw
     away every other row.

  2. The LGU Dashboard had no per-barangay view of how many businesses
     are actually on file.

  3. The Saturation Map plotted barangays at synthetic placeholder
     coordinates (76 points spread evenly across the city's bounding
     box), so a zone labelled "Laoang" could render next to a different
     barangay entirely. The fix ships REAL, PhilAtlas-sourced
     coordinates for all 76 barangays as the primary source, keeps live
     Google geocoding only as a bounds-checked fallback for a name
     outside that list, and discards anything the fallback resolves
     outside Tarlac City.

Uses the in-memory SQLite TestingConfig, like tests/test_app.py, so it
never touches real MySQL data and never makes a network call.

Run with:
    pytest tests/test_analytics_and_geocoding.py -v
"""

from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import MarketData, SystemSetting


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _market_row(industry, location, count, source, recorded=None):
    return MarketData(
        industry_type=industry,
        location=location,
        competitor_count=count,
        population_density=8000,
        historical_success_rate=0.5,
        foot_traffic_index=45,
        average_rent=15000,
        source=source,
        date_recorded=recorded or date.today(),
    )


@pytest.fixture
def seeded(app):
    """Mirrors the reported situation: lots of non-Places market data on
    file, and only one combo confirmed from a live Google lookup."""
    db.session.add_all(
        [
            _market_row("Food & Beverage", "Poblacion", 40, "Manual"),
            _market_row("Retail", "Poblacion", 30, "PSA"),
            _market_row("Food & Beverage", "San Vicente", 20, "Manual"),
            _market_row("Repair & Maintenance Services", "Tibag", 10, "Google Places API"),
            # Stale duplicate of the first row -- must be collapsed away.
            _market_row("Food & Beverage", "Poblacion", 999, "Manual", date(2020, 1, 1)),
        ]
    )
    db.session.commit()
    return app


# ------------------------------------------------------------------ 1.
def test_distribution_counts_every_source_not_just_places(seeded):
    from app.services.trend_analytics_service import get_industry_distribution

    dist = {d["name"]: d for d in get_industry_distribution()}

    # The bug: only "Repair & Maintenance Services" survived -> one slice.
    assert len(dist) == 3, "pie must show every industry on file, not one slice"
    assert dist["Food & Beverage"]["count"] == 60, "summed across both barangays"
    assert dist["Retail"]["count"] == 30, "a PSA-sourced row is still real businesses on file"
    assert dist["Repair & Maintenance Services"]["count"] == 10


def test_distribution_ignores_stale_duplicate_rows(seeded):
    from app.services.trend_analytics_service import get_industry_distribution

    counts = [d["count"] for d in get_industry_distribution()]
    assert 999 not in counts, "only the freshest row per (industry, location) counts"


def test_distribution_reports_provenance_per_slice(seeded):
    from app.services.trend_analytics_service import get_industry_distribution

    dist = {d["name"]: d for d in get_industry_distribution()}
    assert dist["Repair & Maintenance Services"]["from_places_api"] == 10
    assert dist["Retail"]["from_market_data"] == 30
    assert dist["Retail"]["from_places_api"] == 0


def test_total_businesses_counts_all_market_data(seeded):
    from app.services.trend_analytics_service import get_overview_stats

    stats = get_overview_stats(baseline=[], real_forecasts=[])

    assert stats["total_businesses"] == 100, "40 + 30 + 20 + 10, no SME plans in this fixture"
    assert stats["businesses_from_places_api"] == 10
    assert stats["businesses_from_market_data"] == 90
    assert stats["market_combos_total"] == 4


def test_places_verification_table_lists_only_google_rows(seeded):
    from app.services.trend_analytics_service import get_places_api_rows

    rows = get_places_api_rows()
    assert len(rows) == 1
    assert rows[0]["source"] == "Google Places API"
    assert rows[0]["location"] == "Tibag"
    assert rows[0]["competitor_count"] == 10


# ------------------------------------------------------------------ 2.
def test_barangay_table_rolls_up_per_barangay(seeded):
    from app.services.trend_analytics_service import get_barangay_business_table

    rows = {r["location"]: r for r in get_barangay_business_table()}

    assert rows["Poblacion"]["total_businesses"] == 70
    assert rows["Poblacion"]["industries"] == 2
    assert rows["Poblacion"]["from_places_api"] == 0
    assert rows["Tibag"]["from_places_api"] == 10
    assert rows["Poblacion"]["top_industry"] == "Food & Beverage"


def test_barangay_table_includes_barangays_with_no_data(seeded):
    from app.ml.seed_data import BARANGAY_NAMES
    from app.services.trend_analytics_service import get_barangay_business_table

    listed = {r["location"] for r in get_barangay_business_table()}
    missing = set(BARANGAY_NAMES) - listed
    assert not missing, "the LGU dashboard must list every barangay, even empty ones"


# --------------------------------------------- unverified location names
@pytest.fixture
def seeded_with_junk_location(app):
    """"Baras" is not one of the 76 real barangays (the real one is
    "Baras-baras"), so it has no PSA population behind it."""
    db.session.add_all(
        [
            _market_row("Retail", "Tibag", 12, "Google Places API"),
            _market_row("Retail", "Baras", 7, "Google Places API"),
        ]
    )
    db.session.commit()
    return app


def test_locations_without_real_population_are_dropped_from_tables(seeded_with_junk_location):
    from app.services.trend_analytics_service import get_barangay_business_table, get_places_api_rows

    assert "Baras" not in {r["location"] for r in get_places_api_rows()}
    assert "Baras" not in {r["location"] for r in get_barangay_business_table()}
    # ...but the real barangay with a similar name is untouched.
    assert "Baras-baras" in {r["location"] for r in get_barangay_business_table()}


def test_places_rows_carry_real_population(seeded_with_junk_location):
    from app.services.trend_analytics_service import get_places_api_rows

    rows = {r["location"]: r for r in get_places_api_rows()}
    assert rows["Tibag"]["population"] == 17936


def test_dropping_junk_locations_does_not_change_the_totals(seeded_with_junk_location):
    """The tables hide unverifiable names, but the underlying data is
    still counted -- filtering is presentational, not destructive."""
    from app.services.trend_analytics_service import get_overview_stats

    stats = get_overview_stats(baseline=[], real_forecasts=[])
    assert stats["total_businesses"] == 19, "12 (Tibag) + 7 (Baras) still counted"


# ------------------------------------------------------------------ 3.
@pytest.mark.parametrize(
    "lat,lng,inside",
    [
        (15.4869, 120.5900, True),   # Tarlac City centre
        (14.5995, 120.9842, False),  # Manila
        (13.5000, 121.0000, False),  # a "San Isidro" in another province
        (15.6100, 120.5900, False),  # just past the northern edge
        (0.0, 0.0, False),           # null island -- a classic bad geocode
    ],
)
def test_geocode_results_outside_tarlac_city_are_rejected(lat, lng, inside):
    from app.services.geocoding_service import _is_in_tarlac_city_bounds

    assert _is_in_tarlac_city_bounds(lat, lng) is inside


def test_geocode_query_is_disambiguated_to_tarlac_city():
    from app.services.geocoding_service import _query_for

    query = _query_for("San Isidro")
    assert "Tarlac City" in query and "Philippines" in query


def test_geocoding_is_a_no_op_without_an_api_key(app, tmp_path):
    """No key must mean no network call and no invented coordinates for
    a location OUTSIDE the shipped 76 -- just an honest count of what is
    still unresolved. (The 76 real barangays never hit this path at all
    -- they're answered from the static file, key or no key.)

    THE instance_path LINE IS LOAD-BEARING, not tidiness. The geocode
    cache is a real file under app.instance_path, and instance/ is
    gitignored -- so it is empty on CI and on a fresh clone, and full on
    the machine of anyone who has actually run the app against a live
    Google key. Without the redirect this test reads that developer's
    cache and fails on a name it never asked about, which is exactly how
    it failed: `assert {'Balibago': ...} == {}`, Balibago being a real
    location their own deployment had resolved and cached months
    earlier. The code was right; the test was reading the developer's
    filesystem.
    """
    from app.services.geocoding_service import resolve_missing

    app.instance_path = str(tmp_path)  # isolated cache file -- see above

    names = ["Not A Real Barangay", "Also Fake"]
    cache, resolved, pending = resolve_missing(app, names, api_key="", limit=20)

    assert resolved == 0
    assert pending == 2
    assert cache == {}

    # The claim that actually matters, asserted about the names this
    # call was given rather than about the cache as a whole: nothing was
    # invented for either of them. This survives the cache being
    # non-empty for unrelated reasons, which the assertion above does
    # not.
    assert all(name not in cache for name in names)


# --------------------------------------------- startup data migration
def test_places_cap_migration_runs_and_lifts_the_old_default(app):
    """The regression that made every barangay report exactly 20.

    `places_max_results` shipped as 0 (unlimited), but ensure_defaults()
    was only ever called by seed.py -- never by the running app -- so an
    existing database kept the "20" row it was seeded with and every
    Places lookup stayed capped. create_app() now runs the migration at
    boot.
    """
    from app.models import SystemSetting
    from app.services.startup_migrations import run_startup_migrations

    SystemSetting.set("places_max_results", "20")
    run_startup_migrations(app)

    assert SystemSetting.get("places_max_results") == "0", "the superseded default must be migrated to unlimited"


def test_migration_never_clobbers_a_value_an_admin_chose(app):
    from app.models import SystemSetting
    from app.services.startup_migrations import run_startup_migrations

    SystemSetting.set("places_max_results", "35")
    run_startup_migrations(app)

    assert SystemSetting.get("places_max_results") == "35", "a deliberately tuned value must survive"


def test_recap_watermark_is_recorded_once_and_does_not_drift(app):
    """Rows at/below the watermark were fetched under the old cap and get
    one re-fetch. Re-running must not move it forward, or rows fetched
    since would be needlessly re-fetched (and re-billed)."""
    from app.services.startup_migrations import get_places_recap_watermark, run_startup_migrations

    db.session.add(_market_row("Retail", "Tibag", 20, "Google Places API"))
    db.session.commit()
    first_id = MarketData.query.first().market_id

    run_startup_migrations(app)
    watermark = get_places_recap_watermark()
    assert watermark == first_id

    db.session.add(_market_row("Retail", "Poblacion", 47, "Google Places API"))
    db.session.commit()
    run_startup_migrations(app)

    assert get_places_recap_watermark() == watermark, "watermark must be written once, not bumped on every boot"


def test_rows_captured_under_the_old_cap_are_re_fetched(app, monkeypatch):
    """A real-but-capped Google row is stale by CONTENT, not by age --
    it stopped at 20. It must be re-fetched even though it is recent."""
    from app.services import forecasting_service, places_service
    from app.services.startup_migrations import run_startup_migrations

    app.config["GOOGLE_PLACES_API_KEY"] = "test-key"
    db.session.add(_market_row("Retail", "Tibag", 20, "Google Places API"))
    db.session.commit()
    run_startup_migrations(app)  # records the watermark above that row

    # find_or_create_market_data imports this inside the function body,
    # so patching the module attribute is what takes effect.
    monkeypatch.setattr(places_service, "get_competitor_count", lambda *a, **k: (47, False))

    row = forecasting_service.find_or_create_market_data("Retail", "Tibag")
    assert int(row.competitor_count) == 47, "the capped row should have been replaced by a full count"


def test_uncapped_rows_are_not_re_fetched(app, monkeypatch):
    """Once a row sits above the watermark it is cached normally -- it
    must not be re-fetched on every page load (that would be billable
    calls for nothing)."""
    from app.services import forecasting_service, places_service
    from app.services.startup_migrations import run_startup_migrations

    app.config["GOOGLE_PLACES_API_KEY"] = "test-key"
    run_startup_migrations(app)  # watermark = 0, table is empty
    db.session.add(_market_row("Retail", "Tibag", 47, "Google Places API"))
    db.session.commit()

    calls = []

    def _tracked(*a, **k):
        calls.append(a)
        return (99, False)

    monkeypatch.setattr(places_service, "get_competitor_count", _tracked)

    row = forecasting_service.find_or_create_market_data("Retail", "Tibag")
    assert int(row.competitor_count) == 47
    assert not calls, "an already-uncapped row must not trigger another Google call"


# ------------------------------------ uncapped Google Places paging
class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _place(i):
    return {
        "id": f"place-{i}",
        "displayName": {"text": f"Business {i}"},
        "formattedAddress": f"{i} Some St, Tarlac City, Tarlac",
        "types": ["store"],
    }


def _page(start, count, token=None):
    payload = {"places": [_place(i) for i in range(start, start + count)]}
    if token:
        payload["nextPageToken"] = token
    return payload


def test_places_search_pages_past_the_first_20(monkeypatch):
    """competitor_count used to flat-line at 20 for every barangay
    because only the first Text Search page was ever requested -- a
    dense barangay like Matatalaib reporting exactly 20 was the tell."""
    from app.services import places_service

    pages = [_page(0, 20, "tok1"), _page(20, 20, "tok2"), _page(40, 15)]
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _FakeResponse(pages[len(sent) - 1])

    monkeypatch.setattr(places_service.requests, "post", fake_post)

    results, simulated = places_service.search_competitors(
        "Matatalaib", "Retail", api_key="test-key", max_results=0
    )

    assert simulated is False
    assert len(results) == 55, "all three pages, not just the first 20"
    assert sent[1]["pageToken"] == "tok1"
    assert sent[2]["pageToken"] == "tok2"


def test_places_search_respects_googles_own_three_page_ceiling(monkeypatch):
    from app.services import places_service

    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _FakeResponse(_page(len(sent) * 20, 20, f"tok{len(sent)}"))

    monkeypatch.setattr(places_service.requests, "post", fake_post)

    results, _ = places_service.search_competitors("Poblacion", "Retail", api_key="k", max_results=0)

    assert len(sent) == places_service.MAX_PAGES == 3
    assert len(results) == places_service.HARD_RESULT_CEILING == 60


def test_places_search_counts_a_repeated_place_once(monkeypatch):
    """Google can repeat a place across a page boundary; counting it
    twice would inflate a barangay's competitor_count."""
    from app.services import places_service

    pages = [_page(0, 20, "tok1"), {"places": [_place(19), _place(20), _place(21)]}]
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return _FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(places_service.requests, "post", fake_post)

    results, _ = places_service.search_competitors("Tibag", "Retail", api_key="k", max_results=0)
    assert len(results) == 22, "place-19 appears on both pages but counts once"


def test_places_search_keeps_earlier_pages_when_a_later_one_fails(monkeypatch):
    from app.services import places_service

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _FakeResponse(_page(0, 20, "tok1"))
        return _FakeResponse({}, status=500)

    monkeypatch.setattr(places_service.requests, "post", fake_post)

    results, simulated = places_service.search_competitors("Tibag", "Retail", api_key="k", max_results=0)
    assert len(results) == 20
    assert simulated is False, "real data already fetched must not be relabelled as simulated"


# ------------------------------- geocoding: results and diagnostics
class _GeoResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _geocode_payload(lat, lng, status="OK"):
    return {"status": status, "results": [{"geometry": {"location": {"lat": lat, "lng": lng}}}]}


def test_geocoding_returns_coordinates_for_a_good_answer(app, monkeypatch):
    from app.services import geocoding_service as geo

    monkeypatch.setattr(
        geo.requests, "get", lambda *a, **k: _GeoResponse(_geocode_payload(15.4869, 120.5900))
    )
    coords, status = geo._geocode_via_geocoding_api("Tibag", "key")

    assert coords == {"lat": 15.4869, "lng": 120.59}
    assert status == "OK"


def test_geocoding_surfaces_request_denied_instead_of_failing_silently(app, monkeypatch):
    """The failure that cost two rounds: a disabled API returns
    REQUEST_DENIED, the old code turned that into a bare None, and the
    map just kept showing approximate points with no explanation."""
    from app.services import geocoding_service as geo

    monkeypatch.setattr(
        geo.requests,
        "get",
        lambda *a, **k: _GeoResponse(
            {"status": "REQUEST_DENIED", "error_message": "This API project is not authorized to use this API."}
        ),
    )
    coords, status = geo._geocode_via_geocoding_api("Tibag", "key")

    assert coords is None
    assert "REQUEST_DENIED" in status
    assert "not authorized" in status


def test_geocoding_refuses_an_answer_outside_tarlac_city(app, monkeypatch):
    from app.services import geocoding_service as geo

    # Manila -- a plausible-looking but wrong match.
    monkeypatch.setattr(
        geo.requests, "get", lambda *a, **k: _GeoResponse(_geocode_payload(14.5995, 120.9842))
    )
    coords, status = geo._geocode_via_geocoding_api("San Isidro", "key")

    assert coords is None
    assert status == "OUT_OF_BOUNDS"


def test_geocoding_no_longer_pins_administrative_area(app, monkeypatch):
    """Pinning administrative_area:Tarlac made Google answer ZERO_RESULTS
    whenever its own admin naming didn't match ours."""
    from app.services import geocoding_service as geo

    seen = {}

    def _capture(url, params=None, timeout=None):
        seen.update(params or {})
        return _GeoResponse(_geocode_payload(15.4869, 120.59))

    monkeypatch.setattr(geo.requests, "get", _capture)
    geo._geocode_via_geocoding_api("Tibag", "key")

    assert seen["components"] == "country:PH"
    assert "bounds" in seen, "the Tarlac City viewport hint should still be sent"


def test_places_fallback_rejects_a_nearby_business_that_isnt_the_barangay(app, monkeypatch):
    """Text Search returns PLACES, not administrative areas. A hardware
    store that merely sits nearby must not become the barangay's centre."""
    from app.services import geocoding_service as geo

    monkeypatch.setattr(
        geo.requests,
        "post",
        lambda *a, **k: _GeoResponse(
            {
                "places": [
                    {
                        "location": {"latitude": 15.49, "longitude": 120.60},
                        "formattedAddress": "RB Quilala Hardware, Romulo Hwy, Tarlac City",
                        "displayName": {"text": "RB Quilala Hardware"},
                    }
                ]
            }
        ),
    )
    coords, status = geo._geocode_via_places_api("Laoang", "key")

    assert coords is None
    assert status == "NAME_MISMATCH"


def test_places_fallback_accepts_a_result_that_names_the_barangay(app, monkeypatch):
    from app.services import geocoding_service as geo

    monkeypatch.setattr(
        geo.requests,
        "post",
        lambda *a, **k: _GeoResponse(
            {
                "places": [
                    {
                        "location": {"latitude": 15.49, "longitude": 120.60},
                        "formattedAddress": "Laoang, Tarlac City, Tarlac",
                        "displayName": {"text": "Laoang"},
                    }
                ]
            }
        ),
    )
    coords, status = geo._geocode_via_places_api("Laoang", "key")

    assert coords == {"lat": 15.49, "lng": 120.6}
    assert status == "OK"


def test_verbose_geocode_reports_what_every_attempt_did(app, monkeypatch):
    from app.services import geocoding_service as geo

    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: _GeoResponse({"status": "REQUEST_DENIED"}))
    monkeypatch.setattr(geo.requests, "post", lambda *a, **k: _GeoResponse({"places": []}))

    coords, source, status = geo.geocode_barangay_verbose("Tibag", "places-key", "geo-key")

    assert coords is None and source is None
    assert "Geocoding API: REQUEST_DENIED" in status
    assert "Places API (New): ZERO_RESULTS" in status


def test_verbose_geocode_says_so_when_no_key_is_configured(app):
    from app.services.geocoding_service import geocode_barangay_verbose

    coords, source, status = geocode_barangay_verbose("Tibag", "", "")
    assert coords is None and source is None
    assert status == "no API key configured"


def test_all_76_shipped_barangays_have_real_bounds_checked_coordinates(app):
    """Regression guard for the whole class of bug this file exists to
    prevent: every barangay seed_data.py knows about must have a shipped
    coordinate, and every shipped coordinate must fall inside Tarlac
    City -- so the map is never one edit away from silently plotting a
    barangay in the wrong place again."""
    from app.ml.seed_data import BARANGAY_NAMES
    from app.services.geocoding_service import _is_in_tarlac_city_bounds, load_seed_coords

    seed = load_seed_coords(app)
    assert set(seed.keys()) == set(BARANGAY_NAMES)
    for name, point in seed.items():
        assert _is_in_tarlac_city_bounds(point["lat"], point["lng"]), f"{name} is outside Tarlac City"


def test_laoang_and_sapang_maragul_are_at_their_own_distinct_real_positions(app):
    """The exact bug the user reported: an early placeholder file spread
    76 points evenly across the city's bounding box, which happened to
    put "Laoang" and "Sapang Maragul" close enough together to look like
    the same spot. Pinned here to the real, PhilAtlas-sourced coordinate
    for each, so a future edit can't silently reintroduce that."""
    from app.services.geocoding_service import load_seed_coords

    seed = load_seed_coords(app)
    laoang = seed["Laoang"]
    sapang_maragul = seed["Sapang Maragul"]

    assert (laoang["lat"], laoang["lng"]) == (15.5465, 120.5452)
    assert (sapang_maragul["lat"], sapang_maragul["lng"]) == (15.5051, 120.5492)
    assert (laoang["lat"], laoang["lng"]) != (sapang_maragul["lat"], sapang_maragul["lng"])


def test_coords_pipeline_resolves_and_caches_names_outside_the_shipped_76(app, monkeypatch, tmp_path):
    """End-to-end fallback path: a location name that ISN'T one of the
    76 shipped barangays (e.g. a new one a future LGU upload introduces)
    still gets a real, cached coordinate instead of being silently
    dropped or guessed at. The 76 shipped barangays never reach this
    code at all -- they're answered from the static file every time, see
    test_all_76_shipped_barangays_have_real_bounds_checked_coordinates.
    """
    from app.services import geocoding_service as geo

    app.instance_path = str(tmp_path)  # isolated cache file
    app.config["GOOGLE_GEOCODING_API_KEY"] = "geo-key"

    # Distinct, in-bounds coordinate per name, so we can prove each one
    # lands on its OWN point rather than a shared spot.
    calls = []

    def fake_get(url, params=None, timeout=None):
        address = (params or {}).get("address", "")
        calls.append(address)
        offset = len(calls) * 0.001
        return _GeoResponse(_geocode_payload(15.47 + offset, 120.58 + offset))

    monkeypatch.setattr(geo.requests, "get", fake_get)

    names = ["New Subdivision", "Growth Extension Zone", "Riverside Annex"]
    coords, resolved, pending = geo.merged_coords(app, names, api_key="", limit=10, geocoding_key="geo-key")

    assert resolved == 3, "all three should have resolved"
    assert pending == 0
    for name in names:
        assert coords[name]["source"] == "google_geocoding"

    # Distinct positions -- the original bug drew several barangays on
    # top of each other / in the wrong place entirely.
    positions = {(c["lat"], c["lng"]) for c in coords.values()}
    assert len(positions) == 3

    # Cached: a second pass must not call Google again.
    before = len(calls)
    coords2, resolved2, pending2 = geo.merged_coords(app, names, api_key="", limit=10, geocoding_key="geo-key")
    assert len(calls) == before, "cached coordinates must not be re-geocoded"
    assert resolved2 == 0 and pending2 == 0
    assert coords2["New Subdivision"] == coords["New Subdivision"]


def test_barangay_coords_endpoint_never_invents_a_location(app):
    """merged_coords may fall back to the shipped real coordinates, but
    must never return a coordinate for a barangay it has no data for."""
    from app.services.geocoding_service import merged_coords

    coords, _resolved, _pending = merged_coords(
        app, ["Poblacion", "Definitely Not A Barangay"], api_key="", limit=0
    )

    assert "Definitely Not A Barangay" not in coords
    assert coords["Poblacion"]["source"] == "real"
    # Whatever it does return must be flagged with where it came from.
    for entry in coords.values():
        assert entry["source"] in {"real", "google_geocoding", "google_places", "google_maps_js"}
