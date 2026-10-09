"""Idempotent OnFleet Route Plan reconciliation for accepted DCC routes.

Accepted status is persisted even when the OnFleet side effect fails. This
worker closes that gap: it verifies accepted routes against the tasks' actual
routePlan linkage and creates a plan only when no plan exists.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import sqlalchemy as sa

try:
    from . import fn_side_effects as _fx
except ImportError:
    import fn_side_effects as _fx

INTERVAL_SECONDS = 300
LOOKBACK_DAYS = 30
_LOCK_ID = 817496326
_start_lock = threading.Lock()
_started = False


def _parsed(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _ids(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").replace("|", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tid = str(item or "").strip()
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _recent_route_plans() -> dict[str, dict[str, Any]]:
    headers = _fx._onfleet_auth_header()
    now = datetime.now(timezone.utc)
    params = {
        "createdTimeFrom": int((now - timedelta(days=LOOKBACK_DAYS + 2)).timestamp() * 1000),
        "createdTimeTo": int((now + timedelta(days=2)).timestamp() * 1000),
        "limit": 500,
    }
    resp = requests.get(f"{_fx.ONFLEET_BASE}/routePlans", headers=headers, params=params, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"routePlans audit HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    plans = data if isinstance(data, list) else (data.get("routePlans") or data.get("plans") or [])
    return {str(p.get("name") or "").strip(): p for p in plans if p.get("name")}


def _task_state(task_ids: list[str]) -> tuple[set[str], set[str], list[str], list[str], list[str]]:
    headers = _fx._onfleet_auth_header()
    plan_ids: set[str] = set()
    workers: set[str] = set()
    errors: list[str] = []
    valid: list[str] = []
    missing: list[str] = []
    for tid in task_ids:
        try:
            resp = _fx.onfleet_fetch_with_backoff("get", f"{_fx.ONFLEET_BASE}/tasks/{tid}")
            if resp.status_code == 404:
                missing.append(tid)
                continue
            if resp.status_code != 200:
                errors.append(f"{tid}:HTTP {resp.status_code}")
                continue
            task = resp.json()
            valid.append(tid)
            if task.get("routePlan"):
                plan_ids.add(str(task["routePlan"]))
            if task.get("worker"):
                workers.add(str(task["worker"]))
        except Exception as exc:
            errors.append(f"{tid}:{type(exc).__name__}")
    return plan_ids, workers, errors, valid, missing


def _log_result(engine: sa.Engine, wo: str, result: dict[str, Any]) -> None:
    safe = dict(result)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """INSERT INTO route_events (route_id, action, payload)
                   SELECT id, 'onfleetReconcile', CAST(:payload AS jsonb)
                   FROM routes WHERE wo=:wo"""
            ),
            {"wo": wo, "payload": json.dumps(safe, default=str)},
        )
        route_id = safe.get("routePlanId")
        if route_id:
            row = conn.execute(sa.text("SELECT payload FROM routes WHERE wo=:wo"), {"wo": wo}).mappings().first()
            if row:
                payload = _parsed(row["payload"])
                payload["onfleet_route_plan_id"] = str(route_id)
                payload["onfleet_reconciled_at"] = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    sa.text("UPDATE routes SET payload=CAST(:payload AS jsonb), updated_at=now() WHERE wo=:wo"),
                    {"wo": wo, "payload": json.dumps(payload, default=str)},
                )


def reconcile_route_once(engine: sa.Engine, wo: str, plan_names: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Repair one accepted route only when OnFleet confirms no Route Plan exists."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                """SELECT r.wo, r.status::text AS status, r.payload, r.comp, r.due,
                          c.phone AS contractor_phone,
                          (SELECT e.payload->>'phone'
                           FROM route_events e
                           WHERE e.route_id=r.id AND e.action='processDecision'
                             AND e.payload->>'decision'='accept'
                           ORDER BY e.created_at DESC LIMIT 1) AS accept_phone
                   FROM routes r
                   LEFT JOIN contractors c ON c.id=r.contractor_id
                   WHERE r.wo=:wo"""
            ),
            {"wo": wo},
        ).mappings().first()
    if not row:
        return {"status": "not_found", "wo": wo}
    if str(row["status"]) != "accepted":
        return {"status": "not_accepted", "wo": wo, "dbStatus": str(row["status"])}

    payload = _parsed(row["payload"])
    task_ids = _ids(payload.get("taskIds") or payload.get("task_ids"))
    if not task_ids:
        result = {"status": "needs_review", "wo": wo, "reason": "accepted route has no taskIds in payload"}
        _log_result(engine, wo, result)
        return result

    if plan_names is None:
        plan_names = _recent_route_plans()
    exact = plan_names.get(wo)
    if exact:
        return {"status": "healthy", "wo": wo, "routePlanId": exact.get("id"), "source": "name"}

    plan_ids, workers, task_errors, valid_ids, missing_ids = _task_state(task_ids)
    if task_errors or not valid_ids:
        result = {"status": "needs_review", "wo": wo, "reason": "OnFleet task lookup failed or no usable tasks", "taskErrors": task_errors, "missingTaskIds": missing_ids, "verifiedTaskIds": valid_ids}
        _log_result(engine, wo, result)
        return result
    if len(plan_ids) == 1:
        plan_id = next(iter(plan_ids))
        result = {"status": "healthy", "wo": wo, "routePlanId": plan_id, "source": "task_link"}
        # Persist the discovered ID so future checks are cheap.
        _log_result(engine, wo, {**result, "discovered": True})
        return result
    if len(plan_ids) > 1:
        result = {
            "status": "needs_review", "wo": wo,
            "reason": "route tasks are split across multiple OnFleet Route Plans",
            "routePlanIds": sorted(plan_ids), "taskErrors": task_errors,
        }
        _log_result(engine, wo, result)
        return result

    phone = str(row["contractor_phone"] or row["accept_phone"] or payload.get("phone") or "").strip()
    if not phone:
        result = {"status": "needs_review", "wo": wo, "reason": "no contractor phone available", "taskErrors": task_errors}
        _log_result(engine, wo, result)
        return result

    ordered = ",".join(t for t in _ids(payload.get("stopOrder") or payload.get("stop_order") or task_ids) if t in set(valid_ids))
    try:
        comp = float(row["comp"] if row["comp"] is not None else payload.get("comp") or 0)
    except (TypeError, ValueError):
        comp = 0.0

    repair = _fx.apply_onfleet_decision(
        decision="accept",
        task_ids=",".join(valid_ids),
        wo=wo,
        phone=phone,
        comp=comp,
        due=str(row["due"] or payload.get("due") or ""),
        digital_task_ids=",".join(t for t in _ids(payload.get("digitalTaskIds") or "") if t in set(valid_ids)),
        stop_order=ordered,
    )
    route_id = None
    route_msg = str(repair.get("routeMsg") or "")
    if repair.get("routeSuccess") and "Route created:" in route_msg:
        route_id = route_msg.split("Route created:", 1)[1].strip() or None
    result = {
        "status": ("partially_repaired" if missing_ids else "repaired") if repair.get("routeSuccess") and not repair.get("route_incomplete") else "repair_failed",
        "wo": wo,
        "routePlanId": route_id,
        "taskCount": len(task_ids),
        "verifiedTaskIds": valid_ids,
        "missingTaskIds": missing_ids,
        "onfleet": repair,
        "taskErrorsBeforeRepair": task_errors,
    }
    _log_result(engine, wo, result)
    print(f"[onfleet/route-reconcile] {wo} {result['status']} {route_msg}", flush=True)
    return result


def reconcile_accepted_once(engine: sa.Engine) -> list[dict[str, Any]]:
    """Audit all recent accepted routes and repair only confirmed missing plans."""
    with engine.connect() as lock_conn:
        locked = bool(lock_conn.execute(sa.text(f"SELECT pg_try_advisory_lock({_LOCK_ID})")).scalar())
        if not locked:
            return []
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
            with engine.connect() as conn:
                wos = [
                    str(r[0]) for r in conn.execute(
                        sa.text(
                            """SELECT wo FROM routes
                               WHERE status::text='accepted' AND created_at >= :cutoff
                               ORDER BY created_at"""
                        ),
                        {"cutoff": cutoff},
                    ).all()
                ]
            plans = _recent_route_plans()
            results: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            for wo in wos:
                try:
                    result = reconcile_route_once(engine, wo, plans)
                except Exception as exc:
                    result = {"status": "error", "wo": wo, "reason": f"{type(exc).__name__}: {exc}"}
                    try:
                        _log_result(engine, wo, result)
                    except Exception:
                        pass
                results.append(result)
                counts[result["status"]] = counts.get(result["status"], 0) + 1
            print(f"[onfleet/route-reconcile] audited={len(wos)} outcomes={counts}", flush=True)
            for result in results:
                if result["status"] not in ("healthy",):
                    print(f"[onfleet/route-reconcile/detail] {json.dumps(result, default=str)}", flush=True)
            return results
        finally:
            lock_conn.execute(sa.text(f"SELECT pg_advisory_unlock({_LOCK_ID})"))


def start_background_reconciliation(engine: sa.Engine) -> None:
    global _started
    if engine is None:
        return
    with _start_lock:
        if _started:
            return
        _started = True
    print("[onfleet/route-reconcile] worker started", flush=True)

    def run() -> None:
        while True:
            try:
                reconcile_accepted_once(engine)
            except Exception as exc:
                print(f"[onfleet/route-reconcile] cycle failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(INTERVAL_SECONDS)

    threading.Thread(target=run, name="onfleet-route-reconcile", daemon=True).start()
