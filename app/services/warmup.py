"""
app/services/warmup.py
------------------------
Does the one-off work every fresh process would otherwise make its FIRST
visitors pay for, once, at boot.

WHY THIS EXISTS
A free Render instance sleeps after 15 idle minutes and has a fraction of
one CPU. Every wake-up is a brand-new process, and before this module the
first request to each page paid, on top of its own work:

  * importing scikit-learn and unpickling both trained forests
    (about 1.1 CPU-seconds on a laptop, many times that on the host),
  * compiling that page's Jinja templates (0.3 CPU-seconds for all 35
    on a laptop -- roughly ten times that on 0.1 of a CPU),
  * re-reading reference data (the 2024 PSA population figures) from
    disk.

With gunicorn's --preload (see render.yaml / Procfile) create_app() runs
in the master BEFORE the port is opened, so doing the work here means no
visitor is ever handed to a half-warm process: the wake-up wait they
already sit through absorbs it, and every page after that is served by a
process that has nothing left to load.

Never raises. A model file that will not load, a template with an error,
anything -- it is logged and the app boots exactly as it did before,
loading lazily on first use.
"""

import logging
import time

log = logging.getLogger(__name__)


def _compile_templates(app):
    env = app.jinja_env
    count = 0
    for name in env.list_templates(filter_func=lambda n: n.endswith(".html")):
        try:
            env.get_template(name)
            count += 1
        except Exception:
            log.exception("warm-up: template %s did not compile", name)
    return count


def _load_models(app):
    """Only models that are actually on disk. A fresh build has none until
    seed.py trains them -- and loading "nothing" now would mark the cache
    as loaded-with-no-model for the rest of that process, so a script that
    trains and then scores in the same run would fall back to the
    formula. Absent files are simply left to load lazily, as before."""
    import os

    from app.ml import plan_model as pm

    model_dir = app.config["MODEL_DIR"]
    with app.app_context():
        if os.path.exists(os.path.join(model_dir, "rf_model.pkl")):
            from app.services.forecasting_service import _load_models as load_market_model

            load_market_model()
        if os.path.exists(os.path.join(model_dir, pm.PLAN_MODEL_FILENAME)):
            from app.services.plan_forecast_service import _load_plan_model

            _load_plan_model()


def _load_reference_data():
    from app.ml.seed_data import _parse_source_comments

    _parse_source_comments()


def warm_up(app):
    """Load models, compile templates and parse reference data now.
    Returns {step: seconds} for the log and the tests."""
    timings = {}
    for step, fn in (
        ("models", lambda: _load_models(app)),
        ("templates", lambda: _compile_templates(app)),
        ("reference_data", _load_reference_data),
    ):
        started = time.perf_counter()
        try:
            fn()
        except Exception:
            log.exception("warm-up step %s failed; it will load lazily instead", step)
        timings[step] = round(time.perf_counter() - started, 3)
    log.info("warm-up finished: %s", timings)
    return timings
