"""Fast, durable Field Nation bulk handoff for DCC Revamp.

Commit route reservations first so the FN queue is visible immediately. Run
the slower OnFleet verification/worker/route-plan handoff in one bounded
background worker. A successful queue is NOT a successful OnFleet assignment.
Pending work is persisted in Postgres for recovery after a process restart.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import sqlalchemy as sa

from revamp_bulk_fn import _payload, cluster_hash

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dcc-fn-handoff")
_lock = threading.Lock()
_active = set()
_last_recovery = 0.0


def _ids(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").replace("|", ",").split(",") if v.strip()]


def queue_bulk(engine, selected_routes, due, fn_team_id, fn_worker_id):
    """Return (queued, skipped, errors) after a DB-only, atomic handoff.

    The caller must show a queued/processing message, not claim that OnFleet
    already has these routes. Overlapping task IDs and repeated clicks are
    rejected under advisory transaction locks before the database changes.
    """
    queued, skipped, errors = [], [], []
    if engine is None or not fn_team_id or not fn_worker_id:
        return queued, skipped, [("", "Field Nation database, team and worker are required")]
    candidates = []
    seen_hashes = set()
    for pod, route in selected_routes:
        route_hash = cluster_hash(route)
        task_ids = [str(t.get("id") or "").strip() for t in route.get("data") or []]
        if not task_ids or not all(task_ids) or len(task_ids) != len(set(task_ids)):
            errors.append((route_hash, "Route contains missing or duplicate task IDs"))
            continue
        if route_hash in seen_hashes:
            skipped.append((route_hash, "Already selected"))
            continue
        seen_hashes.add(route_hash)
        candidates.append((pod, route, route_hash, task_ids))
    if not candidates:
        return queued, skipped, errors

    # Use one transaction and one reservation scan for the whole selection.
    # Both normal dispatch and FN queue lock task IDs with this same advisory
    # lock scheme, preventing two dispatchers reserving a task simultaneously.
    with engine.begin() as conn:
        if conn.dialect.name != "postgresql":
            raise RuntimeError("Durable FN bulk queue requires Railway Postgres")
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended('dcc-fn-bulk-queue', 0))"))
        all_ids = sorted(set(tid for *_, tids in candidates for tid in tids))
        if all_ids:
            # Fully consume the SELECT so every task lock is acquired before
            # scanning reservations (including drivers with lazy cursors).
            conn.execute(sa.text("""
                SELECT pg_advisory_xact_lock(hashtextextended(tid, 0))
                FROM (
                    SELECT unnest(string_to_array(:task_ids, ',')) AS tid
                    ORDER BY tid
                ) AS ordered_ids
            """), {"task_ids": ",".join(all_ids)}).all()
        fn_rows = conn.execute(sa.text("""
            SELECT work_order, status, route_plan_id, payload
            FROM field_nation_orders
            WHERE status IN ('posted', 'assigned')
        """)).mappings().all()
        active_rows = conn.execute(sa.text("""
            SELECT wo, payload->>'taskIds' AS task_ids
            FROM routes WHERE CAST(status AS TEXT) IN ('sent', 'accepted')
        """)).mappings().all()

        reservations = {}
        existing_by_hash = {}
        used_wos = set()
        for row in fn_rows:
            wo = str(row["work_order"])
            used_wos.add(wo)
            p = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
            ch = str(p.get("cluster_hash") or "")
            if ch:
                existing_by_hash[ch] = (wo, row, p)
            for task_id in _ids(p.get("taskIds") or p.get("task_ids")):
                reservations.setdefault(task_id, wo)
        for row in active_rows:
            for task_id in _ids(row["task_ids"]):
                reservations.setdefault(task_id, str(row["wo"]))

        date_tag = datetime.now().strftime("%m%d%Y")
        suffixes = {}
        for pod, route, route_hash, task_ids in candidates:
            existing = existing_by_hash.get(route_hash)
            if existing:
                wo, row, p = existing
                if row["status"] == "assigned" or row["route_plan_id"]:
                    skipped.append((route_hash, wo))
                    continue
                # A prior attempt may have persisted the FN row before an
                # OnFleet outage. Reuse that work order instead of duplicating.
                updated = dict(p)
                updated.update(fn_handoff_state="queued", fn_handoff_error="",
                               fn_team_id=str(fn_team_id), fn_worker_id=str(fn_worker_id))
                conn.execute(sa.text("""
                    UPDATE field_nation_orders SET payload = CAST(:payload AS jsonb), updated_at = now()
                    WHERE work_order = :wo AND status = 'posted' AND route_plan_id IS NULL
                """), {"wo": wo, "payload": json.dumps(updated)})
                queued.append((route_hash, wo))
                continue
            collision = next((tid for tid in task_ids if tid in reservations), None)
            if collision:
                errors.append((route_hash, f"Task {collision} already reserved by {reservations[collision]}"))
                continue
            city = str(route.get("city") or "Unknown").strip()
            state = str(route.get("state") or "").strip().upper()
            prefix = f"FN{date_tag}-{city} {state}-"
            if prefix not in suffixes:
                existing_numbers = []
                for wo in used_wos:
                    if wo.startswith(prefix):
                        try:
                            existing_numbers.append(int(wo[len(prefix):]))
                        except (ValueError, TypeError):
                            pass
                suffixes[prefix] = max(existing_numbers, default=0)
            suffixes[prefix] += 1
            wo = f"{prefix}{suffixes[prefix]}"
            payload = _payload(route, pod, due, wo, route_hash)
            payload.update(fn_handoff_state="queued",
                           fn_handoff_queued_at=datetime.now(timezone.utc).isoformat(),
                           fn_team_id=str(fn_team_id), fn_worker_id=str(fn_worker_id))
            conn.execute(sa.text("""
                INSERT INTO field_nation_orders(work_order, status, payload)
                VALUES (:wo, 'posted', CAST(:payload AS jsonb))
            """), {"wo": wo, "payload": json.dumps(payload)})
            used_wos.add(wo)
            existing_by_hash[route_hash] = (wo, {"status": "posted", "route_plan_id": None}, payload)
            for tid in task_ids:
                reservations[tid] = wo
            queued.append((route_hash, wo))
        if queued:
            # Notify other logged-in sessions. The Revamp workspace checks
            # route_events.version to invalidate its 60-second saved-route
            # cache, so FN Associates see queued work on their next interaction.
            conn.execute(sa.text("""
                INSERT INTO route_events (route_id, action, payload)
                VALUES (NULL, 'fnHandoffQueued', CAST(:payload AS jsonb))
            """), {"payload": json.dumps({"work_orders": [wo for _, wo in queued]})})
    return queued, skipped, errors


def _patch(engine, wo, **fields):
    with engine.begin() as conn:
        conn.execute(sa.text("""
            UPDATE field_nation_orders
            SET payload = COALESCE(payload, '{}'::jsonb) || CAST(:patch AS jsonb),
                updated_at = now()
            WHERE work_order = :wo
        """), {"wo": wo, "patch": json.dumps(fields)})


def _run_handoff(engine, wo, handoff):
    try:
        with engine.begin() as conn:
            row = conn.execute(sa.text("""
                UPDATE field_nation_orders
                SET payload = COALESCE(payload, '{}'::jsonb)
                   || '{"fn_handoff_state":"running","fn_handoff_error":""}'::jsonb,
                    updated_at = now()
                WHERE work_order = :wo AND status = 'posted' AND route_plan_id IS NULL
                  AND COALESCE(payload->>'fn_handoff_state', '') = 'queued'
                RETURNING payload
            """), {"wo": wo}).first()
        if not row:
            return
        payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        ids = _ids(payload.get("taskIds"))
        team = str(payload.get("fn_team_id") or "")
        worker = str(payload.get("fn_worker_id") or "")
        if not ids or not team or not worker:
            _patch(engine, wo, fn_handoff_state="error",
                   fn_handoff_error="Missing task IDs or Field Nation worker/team.")
            return
        try:
            handoff(ids, team, fn_worker_id=worker,
                    wo_name=wo, due_date=str(payload.get("due") or ""),
                    cluster_hash=str(payload.get("cluster_hash") or ""))
        except Exception as exc:
            _patch(engine, wo, fn_handoff_state="error",
                   fn_handoff_error=f"OnFleet handoff failed: {type(exc).__name__}")
            return
        with engine.connect() as conn:
            route_plan = conn.execute(sa.text("""
                SELECT route_plan_id FROM field_nation_orders WHERE work_order = :wo
            """), {"wo": wo}).scalar()
        if route_plan:
            _patch(engine, wo, fn_handoff_state="complete", fn_handoff_error="")
            # Preserve the old nonblocking Monday placeholder side effect.
            try:
                from migration import fn_side_effects
                fn_side_effects.push_fn_placeholder_to_monday(payload, wo)
            except Exception as exc:
                print(f"[fn-handoff] Monday placeholder {wo}: {type(exc).__name__}", flush=True)
        else:
            _patch(engine, wo, fn_handoff_state="error",
                   fn_handoff_error="OnFleet Route Plan was not confirmed. Retry the handoff.")
    finally:
        with _lock:
            _active.discard(wo)


def schedule_queued(engine, work_orders, handoff):
    """Queue background work once per WO in this Streamlit process."""
    scheduled = 0
    with _lock:
        for wo in dict.fromkeys(work_orders):
            if wo in _active:
                continue
            _active.add(wo)
            try:
                _executor.submit(_run_handoff, engine, wo, handoff)
                scheduled += 1
            except Exception:
                _active.discard(wo)
                raise
    return scheduled


def recover_queued(engine, handoff, *, force=False):
    """Restart durable queued work after worker/process restarts.

    Do not automatically retry confirmed errors: the operator can explicitly
    resend an errored route. This avoids a rate-limit retry storm.
    """
    global _last_recovery
    if engine is None:
        return 0
    with _lock:
        now = time.monotonic()
        if not force and now - _last_recovery < 30:
            return 0
        _last_recovery = now
    with engine.begin() as conn:
        conn.execute(sa.text("""
            UPDATE field_nation_orders
            SET payload = payload || '{"fn_handoff_state":"queued"}'::jsonb
            WHERE status = 'posted' AND route_plan_id IS NULL
              AND payload->>'fn_handoff_state' = 'running'
              AND updated_at < now() - INTERVAL '30 minutes'
        """))
        work_orders = conn.execute(sa.text("""
            SELECT work_order FROM field_nation_orders
            WHERE status = 'posted' AND route_plan_id IS NULL
              AND payload->>'fn_handoff_state' = 'queued'
            ORDER BY created_at LIMIT 500
        """)).scalars().all()
    return schedule_queued(engine, work_orders, handoff)


def retry_handoff(engine, wo, handoff):
    """Explicitly retry one failed OnFleet handoff without creating a new WO."""
    with engine.begin() as conn:
        updated = conn.execute(sa.text("""
            UPDATE field_nation_orders
            SET payload = payload || '{"fn_handoff_state":"queued","fn_handoff_error":""}'::jsonb,
                updated_at = now()
            WHERE work_order = :wo AND status = 'posted' AND route_plan_id IS NULL
              AND payload->>'fn_handoff_state' = 'error'
        """), {"wo": wo})
        changed = bool(updated.rowcount)
    if changed:
        schedule_queued(engine, [wo], handoff)
    return changed
