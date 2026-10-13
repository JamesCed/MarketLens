"""
app/utils/request_memo.py
---------------------------
Remember a value for the rest of ONE request.

Several read-only questions are asked many times while a single page is
built -- "has the LGU uploaded anything?", "what is the market_data
fingerprint?", "what are the system settings?", "how many unread
notifications?". Each was its own database round trip every time it was
asked: the Recommendations page fired the same fingerprint query five
times, Home counted unread notifications twice. On a remote MySQL every
one of those is a network round trip.

The memo lives on the request's WSGI environ, so it can never leak from
one request into the next -- not even in the tests, where several client
requests can share one application context (and therefore one flask.g).

ANY database flush clears it (see install_flush_invalidation), so a
value can never outlive a write made later in the same request: the
fingerprint taken after compute_scores() saves a Places lookup is a fresh
one, exactly as before.
"""

from flask import has_request_context, request

_ENV_KEY = "dss.request_memo"


def _session_has_pending_writes():
    """True while the session holds changes not yet flushed. A memoised
    answer could not see them (only a real query autoflushes), so the
    memo steps aside until they are written."""
    try:
        from app.extensions import db

        session = db.session
        return bool(session.new or session.dirty or session.deleted)
    except Exception:
        return False


def request_memo(key, compute):
    """compute() once per request for `key`; outside a request (scripts,
    the CLI, a background warm-up) it simply calls compute()."""
    if not has_request_context() or _session_has_pending_writes():
        return compute()
    store = request.environ.setdefault(_ENV_KEY, {})
    if key in store:
        return store[key]
    value = compute()
    store[key] = value
    return value


def clear_request_memo():
    if has_request_context():
        request.environ.pop(_ENV_KEY, None)


_PROCESS_KEY = "dss.process_memo"


def _process_store():
    from flask import current_app, has_app_context

    if not has_app_context():
        return None
    return current_app.extensions.setdefault(_PROCESS_KEY, {})


def process_memo(key, compute, ttl=None, keep=lambda value: True):
    """compute() once for `key` and keep the answer across requests, for
    `ttl` seconds (None: until cleared). For lookups every page repeats
    whose answer almost never changes -- the system account's id, the
    settings table -- so a page does not pay a database round trip for
    them each time. Held per application, so two apps (the tests) never
    share it. Skipped while the session holds unflushed writes, like the
    request memo; `keep` decides whether an answer is worth keeping."""
    import time

    store = _process_store()
    if store is None or _session_has_pending_writes():
        return compute()
    hit = store.get(key)
    now = time.monotonic()
    if hit is not None and (ttl is None or now - hit[0] <= ttl):
        return hit[1]
    value = compute()
    if keep(value):
        store[key] = (now, value)
    return value


def clear_process_memo(prefix=None):
    """Forget every process memo entry, or only those whose key starts
    with `prefix` (keys are tuples or strings)."""
    store = _process_store()
    if not store:
        return
    if prefix is None:
        store.clear()
        return
    for key in list(store):
        head = key[0] if isinstance(key, tuple) else key
        if head == prefix:
            store.pop(key, None)


def install_flush_invalidation(session_class):
    """Forget everything memoised whenever the session writes or its
    transaction ends: a flush, a commit, a rollback (which can undo a
    flush the memo already saw), and a bulk UPDATE/DELETE statement --
    query.update()/delete() do not flush, so without the last one
    "mark all as read" would leave the unread count memoised."""
    from sqlalchemy import event

    if getattr(session_class, "_dss_memo_listener", False):
        return

    def _forget(*_args, **_kwargs):
        clear_request_memo()

    for name in ("after_flush", "after_commit", "after_rollback", "after_soft_rollback"):
        event.listen(session_class, name, _forget)

    @event.listens_for(session_class, "after_flush")
    def _forget_settings(session, _context):
        from app.models.system_setting import SystemSetting

        if any(isinstance(obj, SystemSetting) for obj in (*session.new, *session.dirty, *session.deleted)):
            try:
                clear_process_memo("system_settings")
            except Exception:
                pass

    @event.listens_for(session_class, "do_orm_execute")
    def _forget_on_bulk_write(state):
        if state.is_update or state.is_delete:
            clear_request_memo()

    session_class._dss_memo_listener = True
