"""
tests/test_fast_forest.py
---------------------------
forecasting_service.forest_predict() reads each tree's compiled
structure directly instead of going through RandomForestRegressor.
predict(). It is only allowed to be faster, never different: the mean
must be bit-identical to the forest's own predict(), and the per-tree
answers identical to each tree's predict().
"""

import numpy as np
import pytest

from app import create_app
from app.extensions import db
from app.ml.constants import FEATURE_NAMES
from app.models import SystemSetting


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _forest(app):
    from app.services import forecasting_service as fs

    with app.app_context():
        fs._load_models()
        model = fs._MODEL_CACHE["rf"]
    if model is None:
        pytest.skip("no trained model on disk in this environment")
    return model


def _rows(n, seed=0):
    rng = np.random.default_rng(seed)
    scale = np.array([40, 6000, 100, 45000, 1, 12, 15, 21], dtype=float)
    rows = rng.uniform(0, 1, (n, len(FEATURE_NAMES))) * scale
    rows[:, 0] = np.round(rows[:, 0])
    rows[:, 7] = np.round(rows[:, 7])
    return rows


@pytest.mark.parametrize("n", [1, 4, 76, 500])
def test_the_fast_path_is_bit_identical_to_the_forests_own_predict(app, n):
    from app.services.forecasting_service import forest_predict

    model = _forest(app)
    rows = _rows(n, seed=n)
    means, per_tree = forest_predict(model, rows)
    assert np.array_equal(means, model.predict(rows))
    assert np.array_equal(per_tree, np.array([tree.predict(rows) for tree in model.estimators_]))


def test_the_saturation_and_confidence_are_unchanged(app):
    """_predict_saturation's two outputs, computed the old way by hand."""
    from app.services.forecasting_service import _predict_saturation

    model = _forest(app)
    with app.app_context():
        for row in _rows(25, seed=7):
            saturation, confidence, version = _predict_saturation(list(row))
            X = np.array([row])
            expected = max(0.0, min(100.0, float(model.predict(X)[0])))
            spread = float(np.std([tree.predict(X)[0] for tree in model.estimators_]))
            assert version == "rf_v1"
            assert saturation == round(expected, 2)
            assert confidence == round(max(40.0, min(100.0, 100.0 - (spread / 25.0) * 60.0)), 2)
