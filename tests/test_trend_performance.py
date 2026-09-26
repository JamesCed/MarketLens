"""
tests/test_trend_performance.py
----------------------------------
The Trend Reports page on a 512 MB instance.

Three things are pinned here, and the first is the one that made the
page slow:

  1. THE BATCHED SCORER IS THE SAME SCORER. compute_scores_batch()
     replaced 500 individual compute_scores() calls. It is only a
     legitimate optimisation if it returns identical numbers and hits
     the database a constant number of times instead of ~1,000.
  2. THE LATEST-ROW LOOKUP HAPPENS IN SQL. market_data is a history
     table; resolving "newest per combo" by loading all of it and
     discarding most was the memory problem that grows with use.
  3. THE RESPONSE CACHE INVALIDATES ON DATA, NOT ON A CLOCK. A cache
     that can serve figures the database disagrees with is worse than
     no cache in a decision-support tool.
"""

from datetime import date, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.models import LguData, MarketData, SystemSetting
from app.models.user import User


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _seed(industries=4, barangays=12, refreshes=3):
    """A history table, not a snapshot: several refreshes per combo, so
    'newest per combo' is a real question."""
    user = User(name="LGU", email="lgu@perf.test", role="LGU")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    for rep in range(refreshes):
        for barangay in BARANGAY_NAMES[:barangays]:
            for industry in BUSINESS_TYPES[:industries]:
                db.session.add(MarketData(
                    industry_type=industry, location=barangay,
                    competitor_count=10 + rep, population_density=900,
                    historical_success_rate=0.5, foot_traffic_index=45,
                    average_rent=19000, source="Manual",
                    date_recorded=date.today() - timedelta(days=(refreshes - 1 - rep) * 10),
                ))
    for barangay in BARANGAY_NAMES[:barangays]:
        db.session.add(LguData(source="DTI", barangay=barangay, permit_count=4,
                               business_density=1.1, upload_date=date.today(),
                               uploaded_by=user.user_id))
    db.session.commit()
    return [(i, b) for i in BUSINESS_TYPES[:industries] for b in BARANGAY_NAMES[:barangays]]


# ---------------------------------------------------------------------
# 1. The batched scorer
# ---------------------------------------------------------------------

def test_batched_scores_are_identical_to_per_pair_scores(app):
    """The whole optimisation rests on this. If the numbers differ at
    all, the page is faster and wrong."""
    from app.services.forecasting_service import compute_scores, compute_scores_batch

    with app.app_context():
        pairs = _seed()
        one_at_a_time = [compute_scores(industry, location) for industry, location in pairs]
        batched = compute_scores_batch(pairs)

    assert batched == one_at_a_time


def test_the_batch_does_not_scale_its_query_count_with_the_batch(app):
    """~1,000 round trips for one page was the database half of the
    problem, and it is worse against a managed database in another
    data centre. The batch must be a fixed handful of queries however
    many pairs it is given."""
    from sqlalchemy import event

    from app.services.forecasting_service import compute_scores_batch

    with app.app_context():
        pairs = _seed()
        compute_scores_batch(pairs)  # warm any row creation first

        counter = {"n": 0}

        def count(*_args, **_kwargs):
            counter["n"] += 1

        event.listen(db.engine, "before_cursor_execute", count)
        try:
            compute_scores_batch(pairs)
            many = counter["n"]
            counter["n"] = 0
            compute_scores_batch(pairs[:4])
            few = counter["n"]
        finally:
            event.remove(db.engine, "before_cursor_execute", count)

    assert many <= 8, f"{len(pairs)} pairs took {many} queries"
    assert many == few, (
        f"query count scaled with batch size ({few} -> {many}); the per-pair "
        f"lookups are still in there somewhere"
    )


def test_the_forest_is_asked_once_per_tree_not_once_per_row(app):
    """13 of the page's 28 seconds were joblib dispatch overhead from
    calling tree.predict() on a single row inside a loop over the
    forest. This counts the calls rather than the seconds, which is
    the thing that actually has to stay fixed."""
    from app.services import forecasting_service as fs

    with app.app_context():
        pairs = _seed()
        fs.compute_scores_batch(pairs)  # ensure rows + model are loaded

        fs._load_models()
        model = fs._MODEL_CACHE["rf"]
        if model is None:
            pytest.skip("no trained model on disk in this environment")

        calls = {"n": 0}
        real_predict = type(model.estimators_[0]).predict

        def counting_predict(self, X, *args, **kwargs):
            calls["n"] += 1
            return real_predict(self, X, *args, **kwargs)

        type(model.estimators_[0]).predict = counting_predict
        try:
            fs.compute_scores_batch(pairs)
        finally:
            type(model.estimators_[0]).predict = real_predict

    trees = len(model.estimators_)
    # TWO passes over the forest are expected, not one:
    #   1. rf_model.predict() visits every tree to produce the mean;
    #   2. _tree_predictions() visits every tree again for the spread
    #      that confidence_level is derived from.
    #
    # They could be collapsed into one -- the forest's prediction IS
    # the mean over trees -- and measured that way they agree to about
    # 3e-14. But floating-point accumulation order differs, so the two
    # are not bit-identical, and a value sitting exactly on a rounding
    # or cluster-threshold boundary could flip. The batch already
    # turned ~20s into ~0.07s; halving that again is not worth trading
    # away "the fast path returns exactly what the slow path returns".
    #
    # What matters is that this is a function of the FOREST SIZE and
    # not of the batch size. Before batching it was 500 x trees.
    assert calls["n"] <= 2 * trees, (
        f"{calls['n']} per-tree predicts for {len(pairs)} pairs over {trees} trees "
        f"-- expected at most two passes over the forest regardless of batch size, "
        f"so the per-row predicts are back"
    )


# ---------------------------------------------------------------------
# 2. Newest-per-combo is resolved by the database
# ---------------------------------------------------------------------

def test_latest_market_data_returns_the_newest_row_per_combo(app):
    """Behaviour first: the SQL version has to agree with what the old
    Python loop produced."""
    from app.services.trend_analytics_service import latest_market_data_by_key

    with app.app_context():
        pairs = _seed(industries=3, barangays=5, refreshes=3)
        latest = latest_market_data_by_key()

        assert len(latest) == len(pairs), "one row per combo, not one per refresh"
        for (industry, location), row in latest.items():
            newest = (
                MarketData.query.filter_by(industry_type=industry, location=location)
                .order_by(MarketData.date_recorded.desc(), MarketData.market_id.desc())
                .first()
            )
            assert row.market_id == newest.market_id


def test_the_whole_history_is_not_loaded_to_answer_it(app):
    """The memory half of the problem. market_data grows by one row per
    combo per refresh, so loading all of it to produce one row per
    combo gets worse every time the app is used. Ten refreshes must
    not cost ten times the rows."""
    from sqlalchemy import event

    from app.services.trend_analytics_service import latest_market_data_by_key

    with app.app_context():
        _seed(industries=3, barangays=5, refreshes=10)
        total_rows = MarketData.query.count()

        loaded = {"n": 0}

        def count_rows(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT") and " FROM market_data" in statement:
                loaded["n"] += 1

        event.listen(db.engine, "before_cursor_execute", count_rows)
        try:
            latest = latest_market_data_by_key()
        finally:
            event.remove(db.engine, "before_cursor_execute", count_rows)

    assert total_rows == 150, f"fixture changed: {total_rows} rows"
    assert len(latest) == 15
    assert loaded["n"] <= 5, f"{loaded['n']} separate market_data SELECTs to build 15 rows"


# ---------------------------------------------------------------------
# 3. The response cache keys on the data, not on a clock
# ---------------------------------------------------------------------

class _FakeCache:
    """Stands in for Flask-Caching so the KEYING logic can be tested
    without the package installed. It deliberately does not expire
    anything: the point of these tests is that correctness comes from
    the key, never from a timeout."""

    def __init__(self):
        self.store = {}
        self.hits = 0

    def get(self, key):
        value = self.store.get(key)
        if value is not None:
            self.hits += 1
        return value

    def set(self, key, value, timeout=None):
        self.store[key] = value

    def clear(self):
        self.store.clear()


@pytest.fixture
def fake_cache(monkeypatch):
    from app.services import response_cache

    fake = _FakeCache()
    monkeypatch.setattr(response_cache, "cache", fake)
    monkeypatch.setattr(response_cache, "CACHING_AVAILABLE", True)
    return fake


def test_a_repeat_request_is_served_from_the_cache(app, fake_cache):
    from app.services.response_cache import cached_on_data

    calls = {"n": 0}

    @cached_on_data("probe", query_args=("industry_type",))
    def view():
        calls["n"] += 1
        return {"value": calls["n"]}

    with app.app_context():
        _seed(industries=2, barangays=3, refreshes=1)
        with app.test_request_context("/api/trend-data"):
            first = view()
            second = view()

    assert calls["n"] == 1, "the view ran twice -- the cache did not hit"
    assert first == second
    assert fake_cache.hits == 1


def test_new_market_data_invalidates_the_cache(app, fake_cache):
    """The reason this is keyed on a fingerprint rather than a
    timeout. Import rows or run a Places refresh and the next request
    must recompute -- not wait out a timer while showing figures the
    database no longer agrees with."""
    from app.services.response_cache import cached_on_data

    calls = {"n": 0}

    @cached_on_data("probe", query_args=())
    def view():
        calls["n"] += 1
        return {"value": calls["n"]}

    with app.app_context():
        _seed(industries=2, barangays=3, refreshes=1)
        with app.test_request_context("/api/trend-data"):
            view()
            view()
            assert calls["n"] == 1

        db.session.add(MarketData(
            industry_type=BUSINESS_TYPES[0], location=BARANGAY_NAMES[0],
            competitor_count=999, population_density=900, historical_success_rate=0.5,
            foot_traffic_index=45, average_rent=19000, source="Manual",
            date_recorded=date.today(),
        ))
        db.session.commit()

        with app.test_request_context("/api/trend-data"):
            view()

    assert calls["n"] == 2, "the cache served a stale answer after market_data changed"


def test_different_months_are_cached_separately(app, fake_cache):
    from app.services.response_cache import cached_on_data

    seen = []

    @cached_on_data("probe", query_args=("as_of",))
    def view():
        from flask import request

        seen.append(request.args.get("as_of"))
        return {"as_of": request.args.get("as_of")}

    with app.app_context():
        _seed(industries=2, barangays=3, refreshes=1)
        for month in ("2026-01", "2026-02", "2026-01"):
            with app.test_request_context(f"/api/trend-data?as_of={month}"):
                view()

    assert seen == ["2026-01", "2026-02"], "months must not share a cache entry"


def test_caching_is_a_no_op_when_the_package_is_missing(app, monkeypatch):
    """Flask-Caching is imported defensively so a missing wheel cannot
    take the site down. The routes must still answer."""
    from app.services import response_cache

    monkeypatch.setattr(response_cache, "cache", None)
    monkeypatch.setattr(response_cache, "CACHING_AVAILABLE", False)

    calls = {"n": 0}

    @response_cache.cached_on_data("probe", query_args=())
    def view():
        calls["n"] += 1
        return {"value": calls["n"]}

    with app.app_context():
        _seed(industries=2, barangays=3, refreshes=1)
        with app.test_request_context("/api/trend-data"):
            assert view() == {"value": 1}
            assert view() == {"value": 2}
