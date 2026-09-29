"""Reconcile recently added, eligible Postgres contractors with OnFleet workers.

The former Monday import only synchronized OnFleet when it inserted a row.
This module uses the current DCC contractor table instead and never changes an
existing worker. Phone and email conflicts are left for manual review.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading

import sqlalchemy as sa

try:
    from .contractor_sync import (
        _clean_text, _norm_title, _onfleet_list_workers, _onfleet_request,
        normalize_email, normalize_phone,
    )
except ImportError:
    from contractor_sync import (
        _clean_text, _norm_title, _onfleet_list_workers, _onfleet_request,
        normalize_email, normalize_phone,
    )

ELIGIBLE = {"ACTIVE", "IN TRAINING", "NEED INSURANCE"}
NEW_IC_CUTOFF = datetime(2026, 9, 25, tzinfo=timezone.utc)
MAX_CREATES_PER_RUN = 20
RECONCILE_INTERVAL_SECONDS = 300
_background_lock = threading.Lock()
_background_started = False


def _worker_matches(contractor: dict, workers: list[dict]) -> tuple[str, dict | None]:
    phone = normalize_phone(contractor.get("phone"))
    email = normalize_email(contractor.get("email"))
    by_phone = [w for w in workers if phone and normalize_phone(w.get("phone")) == phone]
    by_email = [w for w in workers if email and normalize_email(w.get("email")) == email]
    ids = {str(w.get("id")) for w in by_phone + by_email}
    if len(ids) > 1 or len(by_phone) > 1 or len(by_email) > 1:
        return "conflict", None
    if by_phone and by_email and by_phone[0].get("id") != by_email[0].get("id"):
        return "conflict", None
    match = (by_phone or by_email or [None])[0]
    if match:
        # One identifier can match an unrelated person after a phone is
        # reassigned. Require names and any populated contact fields to agree.
        if _norm_title(match.get("name")) != _norm_title(contractor.get("name")):
            return "conflict", None
        worker_phone = normalize_phone(match.get("phone"))
        worker_email = normalize_email(match.get("email"))
        if (worker_phone and phone and worker_phone != phone) or (
            worker_email and email and worker_email != email
        ):
            return "conflict", None
        return "already_present", match
    if any(_norm_title(w.get("name")) == _norm_title(contractor.get("name")) for w in workers):
        return "conflict", None
    return "missing", None


def preview_new_contractors(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("""
            SELECT id, name, email, phone, location, pod_color, ic_list, created_at
            FROM contractors
            WHERE created_at >= :cutoff
            ORDER BY created_at DESC, id DESC
        """), {"cutoff": NEW_IC_CUTOFF}).mappings().all()

    workers = _onfleet_list_workers()
    teams_data = _onfleet_request("GET", "/teams").json()
    teams = teams_data if isinstance(teams_data, list) else teams_data.get("teams", [])
    team_ids = {_norm_title(t.get("name")): t.get("id") for t in teams}
    result = []
    for row in rows:
        ic = dict(row)
        status = _clean_text(ic.get("ic_list")).upper()
        pod = _clean_text(ic.get("pod_color"))
        phone = normalize_phone(ic.get("phone"))
        email = normalize_email(ic.get("email"))
        if status not in ELIGIBLE:
            outcome = "ineligible"
        elif not pod or not team_ids.get(f"pod: {_norm_title(pod)}"):
            outcome = "missing_pod_team"
        elif not phone or len(phone) != 10 or not email or not _clean_text(ic.get("name")):
            outcome = "incomplete_contact"
        else:
            outcome, _ = _worker_matches(ic, workers)
        result.append({**ic, "outcome": outcome, "team_id": team_ids.get(f"pod: {_norm_title(pod)}")})
    return result


def create_missing_contractors(engine, *, selected_ids: set[int]) -> list[dict]:
    """Recheck immediately before each create so repeated clicks are safe."""
    with engine.connect() as lock_conn:
        # A Postgres session lock serializes concurrent admin clicks across
        # Streamlit sessions and replicas. Hold it through the OnFleet writes.
        locked = lock_conn.execute(sa.text("SELECT pg_try_advisory_lock(817496325)")).scalar()
        if not locked:
            raise RuntimeError("Another contractor sync is running. Check again shortly.")
        try:
            preview = preview_new_contractors(engine)
            targets = [ic for ic in preview if ic["id"] in selected_ids and ic["outcome"] == "missing"]
            if len(targets) > MAX_CREATES_PER_RUN:
                raise ValueError(f"More than {MAX_CREATES_PER_RUN} missing ICs selected; review in smaller batches.")

            results = []
            for ic in targets:
                # A worker may have been created since the initial preview.
                outcome, _ = _worker_matches(ic, _onfleet_list_workers())
                if outcome != "missing":
                    results.append({"name": ic["name"], "status": outcome})
                    continue
                phone = normalize_phone(ic["phone"])
                body = {
                    "name": _clean_text(ic["name"]),
                    "phone": "+1" + phone,
                    "email": normalize_email(ic["email"]),
                    "teams": [ic["team_id"]],
                }
                address = _clean_text(ic.get("location"))
                if address:
                    body["metadata"] = [{"name": "Address", "type": "string", "value": address}]
                try:
                    worker = _onfleet_request("POST", "/workers", json=body).json()
                    worker_id = worker.get("id")
                    verified = _onfleet_request("GET", f"/workers/{worker_id}").json() if worker_id else {}
                    okay = verified.get("id") == worker_id and normalize_phone(verified.get("phone")) == phone
                    results.append({"name": ic["name"], "status": "created" if okay else "verify_failed", "worker_id": worker_id})
                except Exception as exc:
                    results.append({"name": ic["name"], "status": "failed", "reason": str(exc)})
            return results
        finally:
            lock_conn.execute(sa.text("SELECT pg_advisory_unlock(817496325)"))


def reconcile_missing_once(engine) -> list[dict]:
    """Sync one bounded batch; the next cycle picks up any remaining workers."""
    preview = preview_new_contractors(engine)
    missing_ids = {ic["id"] for ic in preview if ic["outcome"] == "missing"}
    if not missing_ids:
        return []
    # Keep each pass bounded, even when a large intake lands at once.
    return create_missing_contractors(engine, selected_ids=set(sorted(missing_ids)[:MAX_CREATES_PER_RUN]))


def start_background_reconciliation(engine) -> None:
    """Start once per app process; PostgreSQL locks serialize multiple replicas."""
    global _background_started
    if engine is None:
        return
    with _background_lock:
        if _background_started:
            return
        _background_started = True

    def run() -> None:
        while True:
            try:
                results = reconcile_missing_once(engine)
                if results:
                    counts: dict[str, int] = {}
                    for result in results:
                        status = result["status"]
                        counts[status] = counts.get(status, 0) + 1
                    print(f"[onfleet/ic-sync] {counts}", flush=True)
            except Exception as exc:
                # Keep the app available and retry on the next cycle.
                print(f"[onfleet/ic-sync] {type(exc).__name__}: {exc}", flush=True)
            threading.Event().wait(RECONCILE_INTERVAL_SECONDS)

    threading.Thread(target=run, name="onfleet-ic-sync", daemon=True).start()
