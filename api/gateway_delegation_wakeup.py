"""Wake WebUI sessions for async-delegation completions delivered by the Gateway.

With the Gateway chat backend the agent runs in the Gateway process, so its
``async_delegation`` completions never reach this process's
``process_registry.completion_queue`` (``api.background_process`` drain).
For api_server sessions the Gateway instead persists each completion as a
delivery row in the profile's state.db (``display_kind`` ``async_delegation_complete``,
or ``hidden`` for presentation-suppressed notices) and deliberately starts no turn: the client owns the next turn
(``gateway.wake.persist_delegation_delivery``). Without a consumer here the
parent agent only sees the result when the user next types.

This poller leases those rows (a ``caller_history_reservation`` lease in the row's
``display_metadata``, written through upstream ``SessionDB._execute_write``; a
``SessionDB`` that ships ``reserve/commit/release_caller_history_deliveries`` is
used directly instead), copies each result once into
the session's model-facing ``context_messages`` (keyed by row id, tagged
``_source: process_wakeup`` so the transcript shows it as a wakeup notice, not a
user turn), and starts a wakeup turn whose prompt is a FIXED nudge. The results
and the lease (``delegation_reservation``, bound to the wake turn's stream id)
are written by ``start_session_turn``'s ``on_admitted`` hook, under the session
lock, in the same save that admits the wake turn: a turn that loses the race to
a human send publishes nothing. Only the worker of THAT stream settles the
lease: committed once the Gateway accepts the run (runs API: run id admitted;
legacy transport: response opened), released if the turn ends before that. The
sidecar keeps the lease until the database settle succeeds; a failed commit is
retried by the next poll. A restart before settlement reattaches the run and
settles it, or the lease expires and the next poll retries. Refused wakes, and
wakes the Gateway never accepted, back off per session.
Works against upstream hermes-agent ``main`` (``append_delegation_delivery``);
without a usable ``SessionDB`` the poller stays inert.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
BACKOFF_MAX_S = 300.0
RESERVATION_OWNER = "hermes-webui"
RESERVATION_TTL_S = 120.0
WAKE_PROMPT = ("[IMPORTANT: A delegated subagent finished. Its result is already in your "
               "conversation history above; review it and continue the task.]")
# Upstream hermes-agent main: delivery rows + the writer transaction this module leases them with.
_MAIN_API = ("append_delegation_delivery", "_execute_write")
# A SessionDB that ships the lease API itself (used as-is when present).
_LEASE_API = ("reserve_caller_history_deliveries", "commit_caller_history_deliveries",
              "release_caller_history_deliveries")
# Rows older than this at first sight are left for the next user turn to fold.
MAX_ROW_AGE_S = 6 * 3600

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()
_BACKOFF: dict[tuple[str, str], tuple[int, float]] = {}  # (db, state sid) -> (failures, retry at)
_FLOOR: dict[str, int] = {}  # db -> lowest message id the next poll scans (rowid high-water mark)
_UNOWNED: dict[str, set[str]] = {}  # db -> state sids with no WebUI sidecar of that profile: never pin the floor

_PENDING_SQL = (
    "SELECT session_id, MIN(id) FROM messages NOT INDEXED WHERE id >= ? AND role = 'user'"
    " AND display_kind IN ('async_delegation_complete', 'hidden')"
    " AND coalesce(json_extract(display_metadata, '$.delegation_id'), '') != ''"
    " AND json_extract(display_metadata, '$.caller_history_consumed') IS NULL"
    " AND timestamp >= ? GROUP BY session_id"
)


def _profile_state_dbs() -> list[tuple[str, Path]]:
    from api.profiles import _DEFAULT_HERMES_HOME

    base = Path(_DEFAULT_HERMES_HOME)
    out = [("default", base / "state.db")]
    profiles_dir = base / "profiles"
    if profiles_dir.is_dir():
        out += [(p.name, p / "state.db") for p in sorted(profiles_dir.iterdir()) if p.is_dir()]
    return [(name, path) for name, path in out if path.is_file()]


def _pending_session_ids(db_path: Path, since: float) -> list[str]:
    """Sessions with pending deliveries. Scans only ids >= the lowest id still pending
    last time (a rowid range), so the steady-state poll is O(new rows), not O(table)."""
    import sqlite3

    key = str(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        top = conn.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
        rows = conn.execute(_PENDING_SQL, (_FLOOR.get(key, 0), since)).fetchall()
    finally:
        conn.close()
    # Rows stay pending until committed, so the lowest pending id is a safe floor. Rows of sessions this
    # poller never wakes (no sidecar of the profile) would pin it forever, so they are left out.
    unowned = _UNOWNED.get(key, ())
    _FLOOR[key] = min((r[1] for r in rows if r[0] not in unowned), default=top + 1)
    return [r[0] for r in rows]


def _session_db_cls():
    from hermes_state import SessionDB

    return SessionDB


def _claim_api_available() -> bool:
    try:
        cls = _session_db_cls()
    except Exception:
        return False
    return any(all(callable(getattr(cls, name, None)) for name in api) for api in (_MAIN_API, _LEASE_API))


# Same lease semantics as the Agent's caller-history API: ``caller_history_consumed`` is final,
# ``caller_history_reservation`` = {token, owner, until} is a lease that expires on its own.
_LINEAGE_IDS_SQL = """WITH RECURSIVE lineage(id) AS (SELECT ? UNION
    SELECT s.parent_session_id FROM sessions s JOIN lineage l ON s.id = l.id
    JOIN sessions p ON p.id = s.parent_session_id WHERE p.end_reason = 'compression'
) SELECT id FROM lineage"""
_CH_PENDING = (
    " AND role = 'user' AND display_kind IN ('async_delegation_complete', 'hidden')"
    " AND json_extract(display_metadata, '$.delegation_id') IS NOT NULL"
    " AND json_extract(display_metadata, '$.caller_history_consumed') IS NULL"
    " AND COALESCE(json_extract(display_metadata, '$.caller_history_reservation.until'), 0) <= ?")


def _has(db, name: str) -> bool:
    return callable(getattr(db, name, None))


def _write(db, fn):
    return db._execute_write(fn, patience_s=getattr(db, "_TRANSCRIPT_WRITE_PATIENCE_S", 5.0))


def _decode(db, name: str, raw):
    dec = getattr(db, name, None)
    if callable(dec):
        return dec(raw)
    import json
    try:
        return json.loads(raw) if isinstance(raw, str) and name == "_decode_display_metadata" else raw
    except ValueError:
        return {}


def _reserve_deliveries(db, session_id: str, owner: str, ttl_seconds: float) -> list[dict]:
    """Lease *session_id*'s (and its compression parents') pending delivery rows to *owner*."""
    if _has(db, "reserve_caller_history_deliveries"):
        return db.reserve_caller_history_deliveries(session_id, owner, ttl_seconds)
    import json
    import uuid
    token = uuid.uuid4().hex

    def _do(conn):
        now = time.time()
        lineage = [r[0] for r in conn.execute(_LINEAGE_IDS_SQL, (session_id,)).fetchall()]
        rows = conn.execute(
            f"SELECT id, session_id, content, display_metadata FROM messages"
            f" WHERE session_id IN ({','.join('?' * len(lineage))})" + _CH_PENDING + " ORDER BY id",
            (*lineage, now)).fetchall()
        lease = json.dumps({"token": token, "owner": str(owner), "until": now + float(ttl_seconds)})
        out = []
        for row_id, sid, content, meta in rows:
            conn.execute("UPDATE messages SET display_metadata = json_set(display_metadata,"
                         " '$.caller_history_reservation', json(?)) WHERE id = ?", (lease, row_id))
            content = _decode(db, "_decode_content", content)
            out.append({"id": row_id, "delivery_id": row_id, "session_id": sid, "content": content,
                        "result": content, "reservation_token": token,
                        "display_metadata": _decode(db, "_decode_display_metadata", meta) or {}})
        return out

    return _write(db, _do)


def _held_where(token_or_ids, owner):
    clause = (" AND json_extract(display_metadata, '$.caller_history_consumed') IS NULL"
              " AND json_extract(display_metadata, '$.caller_history_reservation.owner') = ?")
    if isinstance(token_or_ids, str):
        # A re-reserve replaces the token, so a matching token proves nobody took the rows over:
        # its holder may settle them even after the lease ran out (e.g. after a restart).
        return ("json_extract(display_metadata, '$.caller_history_reservation.token') = ?" + clause,
                [token_or_ids, str(owner)])
    ids = [int(i) for i in token_or_ids or ()]
    if not ids:
        return None, None
    clause += " AND json_extract(display_metadata, '$.caller_history_reservation.until') > ?"
    return (f"id IN ({','.join('?' * len(ids))})" + clause, [*ids, str(owner), time.time()])


def _commit_deliveries(db, token_or_ids, owner: str) -> int:
    """Mark rows still leased to *owner* (by token, or by live-lease row ids) delivered for good."""
    if _has(db, "commit_caller_history_deliveries"):
        return db.commit_caller_history_deliveries(token_or_ids, owner)
    where, params = _held_where(token_or_ids, owner)
    if where is None:
        return 0
    return _write(db, lambda conn: conn.execute(
        "UPDATE messages SET display_metadata = json_remove(json_set(display_metadata,"
        " '$.caller_history_consumed', 1), '$.caller_history_reservation') WHERE " + where,
        tuple(params)).rowcount)


def _release_deliveries(db, *, reservation_token: str, owner: str) -> int:
    """Return rows leased to *owner* under *reservation_token* to pending."""
    if _has(db, "release_caller_history_deliveries"):
        return db.release_caller_history_deliveries(reservation_token=reservation_token, owner=owner)
    where, params = _held_where(reservation_token, owner)
    return _write(db, lambda conn: conn.execute(
        "UPDATE messages SET display_metadata = json_remove(display_metadata,"
        " '$.caller_history_reservation') WHERE " + where, tuple(params)).rowcount)


class OwnershipUnknown(Exception):
    """A sidecar could not be read: ownership is retried next poll, never recorded as unowned."""


def _sidecar_profile_matches(sidecar: Path, profile: str) -> bool:
    import json
    from api.profiles import _profiles_match

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OwnershipUnknown(str(sidecar)) from exc
    return isinstance(data, dict) and _profiles_match(data.get("profile"), profile)


def _webui_session_id(db_path: Path, session_id: str, profile: str) -> str | None:
    """The WebUI sidecar of *profile* that owns *session_id*, following compression lineage upward."""
    import sqlite3
    from api.config import SESSION_DIR

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        sid, seen = session_id, set()
        while sid and sid not in seen:
            sidecar = Path(SESSION_DIR) / f"{sid}.json"
            if sidecar.is_file():
                return sid if _sidecar_profile_matches(sidecar, profile) else None
            seen.add(sid)
            row = conn.execute(
                "SELECT p.id FROM sessions s JOIN sessions p ON p.id = s.parent_session_id"
                " WHERE s.id = ? AND p.end_reason = 'compression'", (sid,)).fetchone()
            sid = row[0] if row else None
        return None
    finally:
        conn.close()


def _live_webui_sid(webui_sid: str, profile: str) -> str | None:
    """*webui_sid*, or its compression continuation's sidecar; None when it is sealed without one.
    A sealed parent is never reopened by a wake (the rows stay for the next turn's fold)."""
    from api.compression_continuation import durable_compression_continuation
    from api.config import SESSION_DIR
    from api.models import get_session

    sealed, tip = durable_compression_continuation(get_session(webui_sid))
    if not sealed:
        return webui_sid
    sidecar = Path(SESSION_DIR) / f"{tip}.json" if tip else None
    return tip if sidecar is not None and sidecar.is_file() and _sidecar_profile_matches(sidecar, profile) else None


def _start_wakeup(webui_sid: str, on_admitted) -> bool:
    """True once the wake turn is persisted as the session's pending turn."""
    from api.routes import start_session_turn

    try:
        resp = start_session_turn(webui_sid, WAKE_PROMPT, source="process_wakeup",
                                  on_admitted=on_admitted) or {}
    except Exception:
        logger.warning("gateway delegation wakeup raised for %s", webui_sid, exc_info=True)
        return False
    status = int(resp.get("_status", 200) or 200)
    if status >= 400 and status != 409:
        logger.warning("gateway delegation wakeup failed for %s: %s %r", webui_sid, status, resp.get("error"))
    return status < 400 and bool(resp.get("stream_id"))


def _publish_results(s, rows: list[dict], reservation: dict) -> None:
    """Append each reserved result once to *s*'s model-facing context and record the lease.

    Runs as ``on_admitted`` under the session lock; the caller's chat-start save persists it
    together with the admitted wake turn (and its launch-failure rollback undoes it)."""
    context = list(getattr(s, "context_messages", None) or [])
    if not context:
        context = [m for m in (getattr(s, "messages", None) or []) if isinstance(m, dict)
                   and m.get("role") in ("user", "assistant") and not m.get("_error")]
    have = {m.get("_delegation_delivery_id") for m in context if isinstance(m, dict)}
    added = [{"role": "user", "content": r["content"], "_source": "process_wakeup",
              "_delegation_delivery_id": r["id"], "timestamp": time.time()}
             for r in rows if r["id"] not in have]
    s.context_messages = context + added
    s.delegation_reservation = reservation


def _record_outcome(res: dict, accepted: bool) -> None:
    """Back-off bookkeeping for a settled wake: accepted resets it, unaccepted bumps it."""
    key = (str(res.get("db")), str(res.get("sid")))
    if accepted:
        _BACKOFF.pop(key, None)
        return
    fails = _BACKOFF.get(key, (0, 0.0))[0] + 1
    _BACKOFF[key] = (fails, time.time() + min(BACKOFF_MAX_S, POLL_INTERVAL_S * 2 ** fails))


def _held_reservation(webui_sid: str, stream_id) -> dict | None:
    from api.config import _get_session_agent_lock
    from api.models import get_session

    with _get_session_agent_lock(webui_sid):
        res = getattr(get_session(webui_sid), "delegation_reservation", None)
    if not isinstance(res, dict) or (stream_id is not None and res.get("stream_id") != stream_id):
        return None
    return res


def _clear_reservation(webui_sid: str, token: str, **mark) -> None:
    """Drop (or, with *mark*, annotate) the sidecar lease if it is still *token*'s."""
    from api.config import _get_session_agent_lock
    from api.models import get_session

    with _get_session_agent_lock(webui_sid):
        s = get_session(webui_sid)
        res = getattr(s, "delegation_reservation", None)
        if isinstance(res, dict) and res.get("token") == token:
            s.delegation_reservation = {**res, **mark} if mark else None
            s.save(touch_updated_at=False)


def settle_reservation(webui_sid: str, stream_id, accepted: bool) -> None:
    """Commit (Gateway accepted the wake run) or release the lease of *stream_id*'s wake turn.

    Only the stream the lease was published with may settle it, so a concurrent human turn
    never consumes it. The sidecar keeps the lease until the database write succeeds; an
    accepted lease whose commit failed is marked and re-committed by the next poll. Never raises."""
    res = None
    try:
        res = _held_reservation(webui_sid, stream_id)
        if res is None:
            return
        # A lease already accepted (its commit failed earlier) is never released by a later teardown.
        accepted = accepted or bool(res.get("accepted"))
        db = _session_db_cls()(Path(res["db"]))
        try:
            if accepted:
                _commit_deliveries(db, res["token"], res["owner"])
            else:
                _release_deliveries(db, reservation_token=res["token"], owner=res["owner"])
        finally:
            db.close()
    except Exception:
        logger.warning("gateway delegation reservation settle failed for %s", webui_sid, exc_info=True)
        if accepted and res is not None:
            try:
                _clear_reservation(webui_sid, res["token"], accepted=True)
            except Exception:
                logger.warning("gateway delegation reservation mark failed for %s", webui_sid, exc_info=True)
        return
    _record_outcome(res, accepted)
    try:
        _clear_reservation(webui_sid, res["token"])
    except Exception:
        logger.warning("gateway delegation reservation clear failed for %s", webui_sid, exc_info=True)


def _retry_accepted_commit(webui_sid: str) -> bool:
    """Re-commit a lease whose wake run the Gateway accepted but whose commit failed.
    True while it is still unsettled (the session must not be re-reserved)."""
    res = _held_reservation(webui_sid, None)
    if res is None or not res.get("accepted"):
        return False
    settle_reservation(webui_sid, res.get("stream_id"), accepted=True)
    return _held_reservation(webui_sid, None) is not None


def _profile_uses_gateway(profile: str) -> bool:
    """True when *profile*'s wake turns go to the Gateway worker that settles the lease.

    Same resolution as the wake's routing and the worker's endpoint: the profile's own runtime env applies.
    Runner-owned turns (``HERMES_WEBUI_RUNTIME_ADAPTER=runner-local``, read by ``_start_run`` from the process
    env) never reach that worker, so they are not woken: their rows stay for the next turn's fold."""
    from api import profiles as _profiles
    from api.config import get_config_for_profile_home
    from api.gateway_chat import webui_gateway_chat_enabled
    from api.runtime_adapter import runtime_adapter_runner_enabled

    if runtime_adapter_runner_enabled():
        return False
    home = _profiles.get_hermes_home_for_profile(profile)
    environ = {k: v for k, v in os.environ.items() if k not in _profiles._loaded_profile_env_keys}
    environ.update(_profiles.filter_runtime_env_for_gateway_parity(_profiles.get_profile_runtime_env(home)))
    return webui_gateway_chat_enabled(get_config_for_profile_home(home), environ)


def _claim_and_wake(db_path: Path, state_sid: str, webui_sid: str) -> int:
    """Reserve the session's pending rows, store them once, and wake with a fixed prompt.

    The Gateway worker commits the lease once the run is accepted (``settle_reservation``).
    """
    from api.background_process import _session_has_active_turn

    key = (str(db_path), state_sid)
    if _BACKOFF.get(key, (0, 0.0))[1] > time.time() or _session_has_active_turn(webui_sid):
        return 0  # rows stay pending: the running turn's successor folds them, or a later poll
    if _retry_accepted_commit(webui_sid):
        return 0
    db = _session_db_cls()(db_path)
    try:
        reserved = _reserve_deliveries(db, state_sid, RESERVATION_OWNER, RESERVATION_TTL_S)
        rows = [r for r in reserved if isinstance(r.get("content"), str) and r["content"].strip()]
        blank = [r["id"] for r in reserved if r not in rows]
        if blank:  # nothing to deliver: settle them as the Gateway fold would
            _commit_deliveries(db, blank, RESERVATION_OWNER)
        if not rows:
            return 0
        lease = {"db": str(db_path), "sid": state_sid, "token": rows[0]["reservation_token"],
                 "owner": RESERVATION_OWNER}

        def on_admitted(s, stream_id):
            _publish_results(s, rows, {**lease, "stream_id": stream_id})

        if _start_wakeup(webui_sid, on_admitted):
            return 1  # the wake turn's worker settles the lease and the back-off
        # Not admitted: nothing was published (or the launch rollback removed it).
        _release_deliveries(db, reservation_token=lease["token"], owner=RESERVATION_OWNER)
        _record_outcome(lease, accepted=False)
        return 0
    finally:
        db.close()


def poll_once(since: float) -> int:
    woken = 0
    for profile, db_path in _profile_state_dbs():
        try:
            if not _profile_uses_gateway(profile):
                continue  # in-process profiles never get Gateway deliveries: don't scan their state.db
            session_ids = _pending_session_ids(db_path, since)
        except Exception:
            logger.debug("gateway delegation poll failed for profile %s", profile, exc_info=True)
            continue
        for sid in session_ids:
            try:
                webui_sid = _webui_session_id(db_path, sid, profile)
                if webui_sid:
                    webui_sid = _live_webui_sid(webui_sid, profile)
                    if webui_sid:
                        woken += _claim_and_wake(db_path, sid, webui_sid)
                else:  # not this profile's WebUI session: its rows must not pin the scan floor
                    _UNOWNED.setdefault(str(db_path), set()).add(sid)
            except OwnershipUnknown:
                _UNOWNED.get(str(db_path), set()).discard(sid)
                logger.debug("gateway delegation ownership unreadable for %s; retrying", sid, exc_info=True)
            except Exception:
                logger.warning("gateway delegation wakeup raised for %s", sid, exc_info=True)
    return woken


def _loop() -> None:
    since = time.time() - MAX_ROW_AGE_S
    while not _STOP.wait(POLL_INTERVAL_S):
        poll_once(since)


def start_gateway_delegation_poller() -> bool:
    """Start the poller (any profile may chat through the Gateway; checked per row). Never raises."""
    global _THREAD
    try:
        if not _claim_api_available():
            logger.info("gateway delegation poller inactive: installed Hermes Agent SessionDB lacks delegation delivery rows")
            return False
        with _LOCK:
            if _THREAD is not None and _THREAD.is_alive():
                return False
            _STOP.clear()
            _THREAD = threading.Thread(target=_loop, name="hermes-webui-gateway-deleg-wakeup", daemon=True)
            _THREAD.start()
        return True
    except Exception:
        logger.warning("gateway delegation poller failed to start", exc_info=True)
        return False


def stop_gateway_delegation_poller(timeout: float = 2.0) -> None:
    _STOP.set()
    th = _THREAD
    if th is not None and th.is_alive():
        th.join(timeout=timeout)
