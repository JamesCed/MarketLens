"""
tests/test_recommendations_performance.py
--------------------------------------------
The AI-Powered Recommendations page on a 512 MB instance.

WHAT WAS WRONG

The page scored all 76 barangays by calling compute_scores() in a loop.
Profiling one request: 3.99 s total, of which 3.86 s was that loop and
2.04 s was time.sleep() inside joblib's worker handshake -- dispatch
overhead with no arithmetic in it. 163 SQL statements for one page,
each of which is a network round trip to Aiven in production.

Exactly the disease already cured on Trend Reports and the Saturation
Map, in a third place that never got the cure. So the fixes are the
ones that worked there, and these tests pin them:

  1. ONE BATCHED SWEEP, not 76 calls. The batch must return the
     IDENTICAL numbers -- a faster page that disagrees with the slow
     one is not an optimisation, it is a bug with better timings.
  2. THE SWEEP IS MEMOISED ON THE DATA, not on a clock. Scoring the
     city is user-independent, so a reload, another SME on the same
     industry, and "Explore more recommendations" should all answer
     from one sweep -- but new market_data must invalidate it
     immediately rather than waiting out a timer.
  3. THE FOREST PREDICTS ON ONE THREAD. n_jobs=-1 is right for
     training and wrong for serving: slower on this workload, a thread
     pile-up on a fractional-CPU instance, and -- the reason it
     decided the matter -- NON-REPRODUCIBLE, because the summation
     order across workers varies run to run.
  4. PANDAS IS NOT IMPORTED AT BOOT. 40 MB for one admin upload page.
"""

import os
import sys
from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.ml.constants import BUSINESS_TYPES, FEATURED_BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.models import LguData, MarketData, SmeProfile, SystemSetting
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


def _seed(barangays=20, industries=4):
    user = User(name="Juan", email="sme@perf.test", role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    for barangay in BARANGAY_NAMES[:barangays]:
        for industry in BUSINESS_TYPES[:industries]:
            db.session.add(MarketData(
                industry_type=industry, location=barangay,
                competitor_count=4, population_density=900,
                historical_success_rate=0.55, foot_traffic_index=48,
                average_rent=19000, source="Google Places API",
                date_recorded=date.today(),
            ))
        db.session.add(LguData(source="DTI", barangay=barangay, permit_count=5,
                               business_density=1.3, upload_date=date.today(),
                               uploaded_by=user.user_id))
    db.session.commit()

    plan = SmeProfile(
        user_id=user.user_id, business_name="Juan's Coffee",
        industry_type=BUSINESS_TYPES[0], location=BARANGAY_NAMES[0],
        business_stage="startup", startup_capital=250000,
        employee_count=3, monthly_revenue_est=80000,
    )
    db.session.add(plan)
    db.session.commit()
    return user, plan


class _QueryCounter:
    """Counts SQL statements. Each one is a network round trip in
    production, which is why the count matters at least as much as the
    wall clock on a local SQLite file."""

    def __init__(self):
        self.n = 0

    def __call__(self, *_args, **_kwargs):
        self.n += 1


def _counting(app):
    from sqlalchemy import event

    counter = _QueryCounter()
    event.listen(db.engine, "before_cursor_execute", counter)
    return counter, lambda: event.remove(db.engine, "before_cursor_execute", counter)


def _login(app):
    client = app.test_client()
    client.post("/login", data={"email": "sme@perf.test", "password": "password123"},
                follow_redirects=True)
    return client


# ---------------------------------------------------------------------
# 1. The batch is the same scorer
# ---------------------------------------------------------------------

def test_the_city_sweep_returns_what_the_per_barangay_loop_returned(app):
    """The whole optimisation rests on this. Recommendations are what
    an SME acts on; if the fast path disagrees with the slow one by so
    much as a rounding step, the page is faster and wrong."""
    from app.services.forecasting_service import compute_scores, compute_scores_batch

    with app.app_context():
        _seed()
        locations = BARANGAY_NAMES[:20]
        industry = BUSINESS_TYPES[0]

        one_at_a_time = [compute_scores(industry, location) for location in locations]
        batched = compute_scores_batch([(industry, location) for location in locations])

    assert batched == one_at_a_time


def test_the_page_does_not_issue_a_query_per_barangay(app):
    """163 statements for one page was the database half of the
    problem. The count must not scale with the number of barangays
    scored."""
    with app.app_context():
        _seed()
        client = _login(app)
        client.get("/recommendations")  # warm the model and the sweep

        counter, stop = _counting(app)
        try:
            response = client.get("/recommendations")
        finally:
            stop()

    assert response.status_code == 200
    assert counter.n <= 20, (
        f"{counter.n} SQL statements to render one Recommendations page -- "
        f"the per-barangay lookups are back"
    )


def test_the_forest_is_entered_once_for_the_whole_city(app):
    """2.0 of the page's 4.0 seconds were joblib dispatch, one handshake
    per barangay. This counts the entries into predict() rather than
    the seconds, which is the thing that has to stay fixed."""
    from app.services import forecasting_service as fs

    with app.app_context():
        _seed()
        fs._load_models()
        model = fs._MODEL_CACHE["rf"]
        if model is None:
            pytest.skip("no trained model on disk in this environment")

        client = _login(app)
        client.get("/recommendations")  # warm everything

        from app.services.trend_analytics_service import clear_trend_caches

        clear_trend_caches()

        calls = {"n": 0}
        real_predict = type(model).predict

        def counting_predict(self, X, *args, **kwargs):
            calls["n"] += 1
            return real_predict(self, X, *args, **kwargs)

        type(model).predict = counting_predict
        try:
            client.get("/recommendations")
        finally:
            type(model).predict = real_predict

    assert calls["n"] <= 2, (
        f"{calls['n']} separate forest predictions to score one city -- "
        f"expected one batched pass regardless of how many barangays there are"
    )


# ---------------------------------------------------------------------
# 2. The sweep is memoised on the data, never on a clock
# ---------------------------------------------------------------------

def test_a_reload_does_not_re_score_the_city(app):
    from app.services import location_opportunity_service as los

    with app.app_context():
        _seed()
        client = _login(app)
        client.get("/recommendations")

        calls = {"n": 0}
        real = los._score_every_barangay

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        los._score_every_barangay = counting
        try:
            client.get("/recommendations")
            client.get("/recommendations")
        finally:
            los._score_every_barangay = real

    assert calls["n"] == 0, "the city was re-scored on a plain reload"


def test_explore_more_reuses_the_same_sweep(app):
    """"Explore more recommendations" drops the 5-card cap. It is the
    same city and the same industry -- only how much of the answer is
    shown changes -- so it must not pay for a second sweep."""
    from app.services import location_opportunity_service as los

    with app.app_context():
        _seed()
        client = _login(app)
        client.get("/recommendations")

        calls = {"n": 0}
        real = los._score_every_barangay

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        los._score_every_barangay = counting
        try:
            response = client.get("/recommendations?all=1")
        finally:
            los._score_every_barangay = real

    assert response.status_code == 200
    assert calls["n"] == 0, "?all=1 re-scored a city that had just been scored"


def test_new_market_data_invalidates_the_sweep(app):
    """The reason this is keyed on a fingerprint rather than a timeout.
    Import rows or run a Places refresh and the next view must
    recompute -- not serve figures the database no longer agrees
    with."""
    from app.services import location_opportunity_service as los

    with app.app_context():
        _seed()
        client = _login(app)
        client.get("/recommendations")

        db.session.add(MarketData(
            industry_type=BUSINESS_TYPES[0], location=BARANGAY_NAMES[0],
            competitor_count=999, population_density=900,
            historical_success_rate=0.55, foot_traffic_index=48,
            average_rent=19000, source="Manual", date_recorded=date.today(),
        ))
        db.session.commit()

        calls = {"n": 0}
        real = los._score_every_barangay

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        los._score_every_barangay = counting
        try:
            client.get("/recommendations")
        finally:
            los._score_every_barangay = real

    assert calls["n"] == 1, "the page served a stale sweep after market_data changed"


def test_the_cached_rows_are_not_mutated_by_the_page(app):
    """The sweep is handed out by reference rather than copied, which is
    only safe while nothing downstream writes to a row. If that ever
    changes, one request would corrupt the next one's data -- silently,
    and only for the second visitor. So it is pinned here."""
    from app.services.location_opportunity_service import _scored_and_ranked

    with app.app_context():
        _seed()
        locations = BARANGAY_NAMES[:20]
        industry = BUSINESS_TYPES[0]

        rows = _scored_and_ranked(industry, locations)
        snapshot = [dict(row) for row in rows]

        client = _login(app)
        client.get("/recommendations")
        client.get("/recommendations?all=1")

        after = _scored_and_ranked(industry, locations)

    assert [dict(row) for row in after] == snapshot, (
        "rendering the page changed the cached sweep rows underneath it"
    )


def test_a_different_barangay_list_is_not_served_another_ones_scores(app):
    """The cache key carries a digest of the location list, not just its
    length -- two different subsets of the same size must not collide."""
    from app.services.location_opportunity_service import _scored_and_ranked

    with app.app_context():
        _seed()
        industry = BUSINESS_TYPES[0]
        first = _scored_and_ranked(industry, BARANGAY_NAMES[:6])
        second = _scored_and_ranked(industry, BARANGAY_NAMES[6:12])

    assert {r["location"] for r in first} == set(BARANGAY_NAMES[:6])
    assert {r["location"] for r in second} == set(BARANGAY_NAMES[6:12])


# ---------------------------------------------------------------------
# 3. The forest predicts on one thread, reproducibly
# ---------------------------------------------------------------------

def test_the_loaded_forest_predicts_on_one_thread(app):
    """n_jobs=-1 is right for training and wrong for serving. See
    forecasting_service._pin_to_one_thread for all three reasons."""
    from app.services import forecasting_service as fs

    with app.app_context():
        fs._load_models()
        model = fs._MODEL_CACHE["rf"]
        if model is None:
            pytest.skip("no trained model on disk in this environment")

    assert model.n_jobs == 1, (
        f"the served forest has n_jobs={model.n_jobs}; on a fractional-CPU "
        f"instance running gunicorn --threads 4 that is a thread pile-up, and "
        f"it makes the model's own output non-reproducible"
    )


def test_the_same_plan_scores_the_same_way_twice(app):
    """What n_jobs=1 actually buys. Measured before the change: 40 out
    of 40 repeat predictions on identical input differed, at the 1e-14
    level. Small -- but a saturation index sitting on a
    CLUSTER_THRESHOLDS boundary can fall either side of it, so the same
    plan could come back "Moderate" on one refresh and "High" on the
    next with nothing having changed."""
    from app.services.forecasting_service import compute_scores_batch

    with app.app_context():
        _seed()
        pairs = [(BUSINESS_TYPES[0], location) for location in BARANGAY_NAMES[:20]]
        first = compute_scores_batch(pairs)
        for _ in range(5):
            assert compute_scores_batch(pairs) == first, (
                "the same input scored differently on a repeat call"
            )


# ---------------------------------------------------------------------
# 4. Boot footprint
# ---------------------------------------------------------------------

def test_pandas_is_not_imported_just_by_starting_the_app():
    """40.3 MB measured, for a library used on one admin upload page.
    It has to be loaded on first use, not on every boot of every
    worker.

    Deliberately narrow: this asserts that STARTING the app does not
    pull pandas in. It does not claim pandas stays out of the process
    forever -- scikit-learn imports pandas itself
    (sklearn/utils/fixes.py), so the first scoring request brings it in
    regardless. The saving is real but it is a boot-time saving, and
    the test says only what is true.
    """
    import subprocess

    probe = (
        "import sys; "
        "sys.path.insert(0, %r); "
        "from app import create_app; "
        "create_app('testing'); "
        "print('pandas' in sys.modules)" % ROOT
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True,
        env={
            **os.environ,
            "TEST_DATABASE_URL": "sqlite:///:memory:",
            # A fresh interpreter has to be able to import what this one
            # can. Handing it this process's sys.path covers a plain
            # virtualenv and a test runner that assembled the path
            # itself, without the test caring which it is running under.
            "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        },
    )
    assert result.returncode == 0, (
        f"the probe subprocess could not start the app, so this test proved "
        f"nothing:\n{result.stderr[-600:]}"
    )

    assert result.stdout.strip().endswith("False"), (
        "pandas is imported at boot again -- check for a module-level "
        "`import pandas` reachable from create_app()"
    )


def test_the_upload_page_still_gets_its_pandas(app):
    """The other half of lazy importing: it has to actually work when
    the upload page needs it."""
    from app.services import data_import_service

    assert data_import_service._pandas() is not None
    assert hasattr(data_import_service._pandas(), "read_csv")


# ---------------------------------------------------------------------
# 5. The other pages built on the same engine
# ---------------------------------------------------------------------

def test_the_home_page_scores_its_industry_cards_in_one_batch(app):
    """SME Home draws a card per featured industry for one barangay. It
    is the first page after signing in, and it had the same per-card
    loop."""
    from app.services import forecasting_service as fs

    with app.app_context():
        _seed()
        client = _login(app)
        client.get("/planning")  # warm

        calls = {"n": 0}
        real = fs.compute_scores

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        fs.compute_scores = counting
        try:
            response = client.get("/planning")
        finally:
            fs.compute_scores = real

    assert response.status_code == 200
    assert calls["n"] == 0, (
        f"Home made {calls['n']} single-pair compute_scores() calls for "
        f"{len(FEATURED_BUSINESS_TYPES)} industry cards"
    )


def test_market_meta_only_loads_the_industry_it_was_asked_for(app):
    """This used to resolve the freshest row for every combo -- 20
    industries x 76 barangays -- and throw away nineteen twentieths of
    it. ORM instances are the expensive kind of row on a 512 MB box."""
    from app.controllers.sme_controller import _market_meta_for

    with app.app_context():
        _seed(barangays=20, industries=4)
        meta = _market_meta_for(BUSINESS_TYPES[0])

        total_combos = MarketData.query.count()

    assert len(meta) == 20, f"expected one entry per seeded barangay, got {len(meta)}"
    assert len(meta) < total_combos, (
        "the whole market_data table is still being resolved to answer a "
        "question about one industry"
    )
    assert all(entry["is_live"] for entry in meta.values())
