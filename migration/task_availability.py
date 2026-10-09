"""Fresh OnFleet checks for dispatch actions; never rely on the task-list cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class TaskAssignmentConflict(RuntimeError):
    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        detail = "; ".join(f"{c['taskId']}: {c['reason']}" for c in conflicts[:5])
        extra = f"; and {len(conflicts) - 5} more" if len(conflicts) > 5 else ""
        super().__init__(f"Work cannot be dispatched. {detail}{extra}. Sync Routes and review the affected tasks.")


def task_ids(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    return list(dict.fromkeys(str(t).strip() for t in values if str(t or "").strip()))


def assert_tasks_available(
    ids: Any, *, fetch_task: Callable[[str], dict], wo: str = "",
    worker_id: str | None = None, allow_existing: bool = False,
) -> dict[str, dict]:
    """Check every task before writes; allow same-worker retries with no conflicting WO metadata."""
    def check(tid):
        try:
            task = fetch_task(tid)
            if not isinstance(task, dict) or str(task.get("id") or "") != tid:
                raise ValueError("invalid task response")
            state = task.get("state")
            worker = task.get("worker")
            worker = str(worker.get("id") or "") if isinstance(worker, dict) else str(worker or "")
            prior_wo = next((str(m.get("value") or "").strip() for m in task.get("metadata") or []
                             if str(m.get("name") or "").upper() == "WO_NAME"), "")
            same_assignment = bool(allow_existing and worker_id and worker == worker_id and (not prior_wo or prior_wo == wo))
            if state == 3 or bool((task.get("completionDetails") or {}).get("success")):
                reason = "already completed in OnFleet"
            elif state not in (0, 1, 2) or isinstance(state, bool):
                reason = "OnFleet status could not be verified"
            elif (worker or state != 0) and not same_assignment:
                reason = "already assigned in OnFleet" + (f" to worker {worker}" if worker else "") + (f" (WO {prior_wo})" if prior_wo else "")
            else:
                return tid, task, None
            return tid, task, {"taskId": tid, "reason": reason, "state": state, "worker": worker, "wo": prior_wo}
        except Exception as exc:
            return tid, None, {"taskId": tid, "reason": f"could not verify OnFleet ({type(exc).__name__})"}

    tids = task_ids(ids)
    if not tids:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(tids))) as pool:
        results = list(pool.map(check, tids))
    conflicts = [conflict for _, _, conflict in results if conflict]
    if conflicts:
        raise TaskAssignmentConflict(conflicts)
    return {tid: task for tid, task, _ in results}


def remove_unavailable_tasks(clusters: list[dict], unavailable: Any) -> list[dict]:
    """Remove only unavailable tasks, retaining other campaigns at the same stop."""
    excluded = set(task_ids(unavailable))
    rebuilt = []
    for original in clusters:
        remaining = [t for t in original.get('data', []) if str(t.get('id') or '').strip() not in excluded]
        if not remaining:
            continue
        if len(remaining) == len(original.get('data', [])):
            rebuilt.append(original)
            continue
        cluster = {**original, 'data': remaining}
        cluster['stops'] = len({t.get('full') for t in remaining})
        cluster['inst_count'] = sum('install' in str(t.get('task_type','')).lower() for t in remaining)
        cluster['remov_count'] = sum(str(t.get('task_type','')).lower() in ('kiosk removal','remove kiosk') for t in remaining)
        cluster['esc_count'] = sum(bool(t.get('escalated')) for t in remaining)
        coords = [(float(t['lat']), float(t['lon'])) for t in remaining if t.get('lat') is not None and t.get('lon') is not None]
        if coords:
            cluster['center'] = [sum(p[i] for p in coords) / len(coords) for i in (0,1)]
        cluster['city'], cluster['state'] = remaining[0].get('city', cluster.get('city')), remaining[0].get('state', cluster.get('state'))
        rebuilt.append(cluster)
    return rebuilt
