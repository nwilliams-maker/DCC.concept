"""Durable, shared OnFleet open-task snapshot.

This is a snapshot/reconciliation layer, NOT an OnFleet delta API. Full audits
still run in the background; callers reuse verified snapshots between audits.
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import text

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_INFLIGHT = False
_TABLE_READY = False
_REFRESH_SECONDS = 600
_MAX_STALE_SECONDS = 1800


def _ensure_table(engine):
    global _TABLE_READY
    if _TABLE_READY:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dcc_onfleet_task_snapshot (
                snapshot_key TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                task_count INTEGER NOT NULL
            )
        """))
    _TABLE_READY = True


def _read(engine):
    _ensure_table(engine)
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT payload, fetched_at, task_count
            FROM dcc_onfleet_task_snapshot WHERE snapshot_key='open_state0'
        """)).mappings().first()
    if not row:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    age = max(0.0, (datetime.now(timezone.utc) - row["fetched_at"]).total_seconds())
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        return None
    if len(payload["tasks"]) != row["task_count"]:
        return None
    return payload, age


def _write(engine, payload):
    _ensure_table(engine)
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise ValueError("Refusing incomplete OnFleet snapshot")
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO dcc_onfleet_task_snapshot(snapshot_key,payload,fetched_at,task_count)
            VALUES ('open_state0', CAST(:payload AS JSONB), NOW(), :count)
            ON CONFLICT (snapshot_key) DO UPDATE SET
                payload=EXCLUDED.payload,
                fetched_at=EXCLUDED.fetched_at,
                task_count=EXCLUDED.task_count
        """), {"payload": json.dumps(payload), "count": len(payload["tasks"])})


def get_snapshot(engine, fetch_live, force=False):
    """Return recent shared snapshot; background refresh when aging.

    A snapshot older than 30 minutes is never served. On failure, caller gets
    the error rather than silently dispatching from an unsafe stale dataset.
    """
    if engine is None:
        return fetch_live()
    try:
        previous = _read(engine)
    except Exception:
        _LOG.exception("Snapshot read unavailable; falling back to live OnFleet")
        return fetch_live()
    if not force and previous and previous[1] < _MAX_STALE_SECONDS:
        if previous[1] >= _REFRESH_SECONDS:
            _start_refresh(engine, fetch_live)
        return previous[0]
    result = fetch_live()
    try:
        _write(engine, result)
    except Exception:
        _LOG.exception("Could not persist verified OnFleet snapshot")
    return result


def _start_refresh(engine, fetch_live):
    global _INFLIGHT
    with _LOCK:
        if _INFLIGHT:
            return
        _INFLIGHT = True

    def worker():
        global _INFLIGHT
        try:
            # Shared cache may return the same value for a 15-minute TTL.
            # Clear only for this background audit, never on ordinary page load.
            clear = getattr(fetch_live, "clear", None)
            if clear:
                clear()
            payload = fetch_live()
            _write(engine, payload)
        except Exception:
            _LOG.exception("OnFleet snapshot background reconciliation failed")
        finally:
            with _LOCK:
                _INFLIGHT = False

    threading.Thread(target=worker, daemon=True, name="onfleet-snapshot-audit").start()
