"""
app/services/upload_jobs.py
------------------------------
Runs a Gov't Data Upload on a background thread, so a large file can
never hang the page.

WHY
A permit register can hold tens of thousands of rows. Imported inside
the web request, the browser waits for the whole import -- and the web
server kills any request that runs past its 120 s limit, rolling the
import back while the page sits on the loading screen. Here the request
only saves the file and starts the job. It then waits a short while
(UPLOAD_WAIT_SECONDS): a normal file finishes inside that window and
the page shows the result straight away, exactly as before. A file that
takes longer gets a progress card that asks /lgu/upload-status/<id>
until the job is done.

The job validates the file and saves it to the STAGING AREA
(staging_service): nothing live changes until a Data Steward approves
the batch and a System Admin publishes it. Publishing then runs the
alert sweep and re-computes the city-wide figures on a separate,
low-priority background thread (schedule_followups), so neither the
publisher nor the next uploader waits for it.

One import runs at a time per process and holds the import lock only
while it reads and stages the file; a second one waits a few seconds at
most. Jobs live in memory -- the app runs one web process -- and are
forgotten an hour after they end.
"""

import logging
import os
import threading
import time
import uuid

log = logging.getLogger(__name__)

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 3600
LOCK_WAIT_SECONDS = 120


class UploadJob:
    """One upload: what was sent, where it has got to, and its result."""

    def __init__(self, user_id, filename, dataset_type, source):
        self.id = uuid.uuid4().hex
        self.user_id = user_id
        self.filename = filename
        self.dataset_type = dataset_type
        self.source = source
        self.state = "queued"
        self.step = "Starting"
        self.count = 0
        self.status = None
        self.message = None
        self.created = time.time()
        self.ended = None
        self.shown = False
        self.log_in_background = False
        self.batch_id = None
        self.done = threading.Event()
        self.lock = threading.Lock()

    def step_to(self, text):
        self.step = text

    def finish(self, count, status, message):
        """The import's result. Called by process_upload as soon as the
        data is committed (or the file is refused)."""
        with self.lock:
            if self.done.is_set():
                return
            self.count, self.status, self.message = count, status, message
            self.state = "done"
            self.ended = time.time()
            self.done.set()

    def as_dict(self):
        return {
            "id": self.id,
            "state": self.state,
            "step": self.step,
            "status": self.status,
            "message": self.message,
            "count": self.count,
            "filename": self.filename,
            "batch_id": self.batch_id,
            "elapsed": round((self.ended or time.time()) - self.created, 1),
        }


def _prune():
    cutoff = time.time() - _JOB_TTL_SECONDS
    with _JOBS_LOCK:
        for job_id in [k for k, job in _JOBS.items() if job.ended and job.ended < cutoff]:
            _JOBS.pop(job_id, None)


def get_job(job_id, user_id):
    """The job, only for the account that started it."""
    job = _JOBS.get(job_id or "")
    if job is None or job.user_id != user_id:
        return None
    return job


def running_job_for(user_id):
    """The newest unfinished job of this account, or None."""
    with _JOBS_LOCK:
        jobs = [job for job in _JOBS.values() if job.user_id == user_id and not job.done.is_set()]
    return max(jobs, key=lambda job: job.created) if jobs else None


def unshown_results_for(user_id):
    """Finished jobs whose result this account has not been shown yet,
    oldest first; each is marked shown."""
    with _JOBS_LOCK:
        jobs = sorted((job for job in _JOBS.values()
                       if job.user_id == user_id and job.done.is_set() and not job.shown),
                      key=lambda job: job.created)
        for job in jobs:
            job.shown = True
    return jobs


def _log_result(job):
    from app.utils.audit import log_action

    staged = f", staged as batch #{job.batch_id}" if job.batch_id else ""
    log_action("dataset_upload",
               details=f"{job.dataset_type}/{job.source}: {job.filename} ({job.status}{staged})",
               user_id=job.user_id)


def run_job(app, job, file_path):
    """Validate the file and save it to the staging area. Runs on the
    job's thread (or inline when UPLOAD_IN_BACKGROUND is off, as in the
    tests). Only this step holds the import lock -- a few seconds -- so
    one upload never waits behind another's follow-up work."""
    if _RUN_LOCK.locked():
        job.step_to("Waiting for the previous upload to finish")
    if not _RUN_LOCK.acquire(timeout=LOCK_WAIT_SECONDS):
        job.finish(0, "failed", "Another upload is still being processed. Please try again in a minute.")
        _remove(file_path)
        return
    try:
        job.state = "running"
        with app.app_context():
            from app.services.staging_service import stage_upload

            try:
                count, status, message, batch_id = stage_upload(
                    job.dataset_type, file_path, job.source, job.user_id, job.filename, progress=job
                )
                job.batch_id = batch_id
                job.finish(count, status, message)
            except Exception as exc:
                log.exception("upload job %s failed", job.id)
                job.finish(0, "failed", f"The file could not be imported: {exc}")
            finally:
                _remove(file_path)

            with job.lock:
                log_here = job.log_in_background
            if log_here:
                _log_result(job)
            job.step_to("Finished")
    except Exception:
        log.exception("upload job %s crashed", job.id)
        job.finish(0, "failed", "The upload stopped unexpectedly. Please try again.")
    finally:
        _RUN_LOCK.release()


def _remove(file_path):
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass


_followups = {"generation": 0, "running": False}
_FOLLOWUP_LOCK = threading.Lock()


def schedule_followups(app):
    """After a batch is published: run the alert sweep and re-compute the
    city-wide figures on ONE background thread. A publish that arrives
    while it is running makes it start over with the newest data instead
    of queueing another thread. Inline when UPLOAD_IN_BACKGROUND is off."""
    if not app.config.get("UPLOAD_IN_BACKGROUND", True):
        _run_followups_once(app, lambda: False)
        return
    with _FOLLOWUP_LOCK:
        _followups["generation"] += 1
        if _followups["running"]:
            return
        _followups["running"] = True
    threading.Thread(target=_followup_loop, args=(app,), name="publish-followups", daemon=True).start()


def _lower_priority():
    """Let page requests win the CPU over this thread (Linux: niceness is
    per thread)."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (AttributeError, OSError):
        pass


def _followup_loop(app):
    _lower_priority()
    try:
        while True:
            with _FOLLOWUP_LOCK:
                generation = _followups["generation"]
            _run_followups_once(app, lambda: _followups["generation"] != generation)
            with _FOLLOWUP_LOCK:
                if _followups["generation"] == generation:
                    _followups["running"] = False
                    return
    except Exception:
        log.exception("follow-up work after publishing failed")
        with _FOLLOWUP_LOCK:
            _followups["running"] = False


def _run_followups_once(app, superseded):
    with app.app_context():
        from app.services.data_import_service import _run_alert_sweeps

        _run_alert_sweeps()
        if app.config.get("WARM_AFTER_UPLOAD", True) and not superseded():
            warm_after_upload(superseded)


def start_upload(app, user_id, file_path, filename, dataset_type, source):
    """Register a job and start it. In the background unless the app is
    configured otherwise."""
    _prune()
    job = UploadJob(user_id, filename, dataset_type, source)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    if app.config.get("UPLOAD_IN_BACKGROUND", True):
        thread = threading.Thread(target=run_job, args=(app, job, file_path),
                                  name=f"upload-{job.id[:8]}", daemon=True)
        thread.start()
    else:
        run_job(app, job, file_path)
    return job


def claim_result_logging(job):
    """Decide who writes the audit entry: the request, when the job has
    already finished (it has the visitor's IP and route), else the job's
    thread when it finishes. Returns True when the caller should log."""
    with job.lock:
        if job.done.is_set():
            return True
        job.log_in_background = True
        return False


def warm_after_upload(superseded=lambda: False):
    """Re-compute what the next pages will ask for, now, on this thread:
    the LGU dashboard's industry cards and rankings, the Diversification
    Plan and the city-wide trend sweep. Every one of them was cleared by
    the publish, and the trend sweep alone is many CPU-seconds on a small
    instance -- the first visitor afterwards would otherwise wait for all
    of it. Stops early when `superseded()` turns true (newer data was
    published meanwhile). Never raises."""
    started = time.perf_counter()
    try:
        from app.ml.constants import BUSINESS_TYPES
        from app.ml.seed_data import BARANGAY_NAMES, get_real_population
        from app.services.diversification_service import build_diversification_plan
        from app.services.forecasting_service import compute_scores_batch
        from app.services.trend_analytics_service import _sweep_baseline, latest_market_data_by_key

        sample = sorted(BARANGAY_NAMES, key=lambda b: -(get_real_population(b) or 0))[:6]
        steps = (
            lambda: compute_scores_batch([(industry, barangay) for industry in BUSINESS_TYPES for barangay in sample]),
            lambda: compute_scores_batch([(BUSINESS_TYPES[0], barangay) for barangay in BARANGAY_NAMES]),
            latest_market_data_by_key,
            build_diversification_plan,
            lambda: _sweep_baseline(None),
        )
        for step in steps:
            if superseded():
                break
            step()
    except Exception:
        log.warning("warm-up after publishing did not finish", exc_info=True)
    finally:
        try:
            from app.extensions import db

            db.session.remove()
        except Exception:
            pass
    log.info("warm-up after publishing took %.1f s", time.perf_counter() - started)
