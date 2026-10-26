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

After the data is committed the same thread runs the alert sweep and
re-computes the city-wide figures (see warm_after_upload), so the next
page an LGU or SME user opens is already up to date and fast. The
uploader is not kept waiting for that part: the result is reported the
moment the data is saved.

One upload runs at a time per process; a second one queues behind it
rather than racing it for the same rows. Jobs live in memory -- the
app runs one web process -- and are forgotten an hour after they end.
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


class UploadJob:
    """One upload: what was sent, where it has got to, and its result."""

    def __init__(self, user_id, filename, dataset_type, source):
        self.id = uuid.uuid4().hex
        self.user_id = user_id
        self.filename = filename
        self.dataset_type = dataset_type
        self.source = source
        self.state = "queued"
        self.step = "Waiting to start"
        self.count = 0
        self.status = None
        self.message = None
        self.created = time.time()
        self.ended = None
        self.shown = False
        self.log_in_background = False
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

    log_action("dataset_upload",
               details=f"{job.dataset_type}/{job.source}: {job.filename} ({job.status})",
               user_id=job.user_id)


def run_job(app, job, file_path):
    """The import itself. Runs on the job's thread (or inline when
    UPLOAD_IN_BACKGROUND is off, as in the tests)."""
    with _RUN_LOCK:
        job.state = "running"
        try:
            with app.app_context():
                from app.services.data_import_service import process_upload

                try:
                    count, status, message = process_upload(
                        job.dataset_type, file_path, job.source, job.user_id, progress=job
                    )
                    job.finish(count, status, message)
                except Exception as exc:
                    log.exception("upload job %s failed", job.id)
                    job.finish(0, "failed", f"The file could not be imported: {exc}")
                finally:
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except OSError:
                            pass

                with job.lock:
                    log_here = job.log_in_background
                if log_here:
                    _log_result(job)

                if job.status == "success" and app.config.get("WARM_AFTER_UPLOAD", True):
                    job.step_to("Refreshing city-wide figures")
                    warm_after_upload()
                job.step_to("Finished")
        except Exception:
            log.exception("upload job %s crashed", job.id)
            job.finish(0, "failed", "The upload stopped unexpectedly. Please try again.")


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


def warm_after_upload():
    """Re-compute what the next pages will ask for, now, on this thread:
    the LGU dashboard's industry cards and rankings, the Diversification
    Plan and the city-wide trend sweep. Every one of them was cleared by
    the upload, and the trend sweep alone is many CPU-seconds on a small
    instance -- the first visitor after an upload would otherwise wait
    for all of it. Never raises."""
    started = time.perf_counter()
    try:
        from app.ml.constants import BUSINESS_TYPES
        from app.ml.seed_data import BARANGAY_NAMES, get_real_population
        from app.services.diversification_service import build_diversification_plan
        from app.services.forecasting_service import compute_scores_batch
        from app.services.trend_analytics_service import _sweep_baseline, latest_market_data_by_key

        sample = sorted(BARANGAY_NAMES, key=lambda b: -(get_real_population(b) or 0))[:6]
        compute_scores_batch([(industry, barangay) for industry in BUSINESS_TYPES for barangay in sample])
        compute_scores_batch([(BUSINESS_TYPES[0], barangay) for barangay in BARANGAY_NAMES])
        latest_market_data_by_key()
        build_diversification_plan()
        _sweep_baseline(None)
    except Exception:
        log.warning("warm-up after upload did not finish", exc_info=True)
    finally:
        try:
            from app.extensions import db

            db.session.remove()
        except Exception:
            pass
    log.info("warm-up after upload took %.1f s", time.perf_counter() - started)
