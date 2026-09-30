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
NEW_IC_CUTOFF = datetime(2026, 9, 24, 5, tzinfo=timezone.utc)  # Sep 24 midnight Chicago
MAX_CREATES_PER_RUN = 20
# Team used when a new IC's pod can't be read from their address. Blank
# (the default) keeps the old behavior: log it for review, don't create.
import os as _os
DEFAULT_POD = (_os.environ.get("ONFLEET_DEFAULT_POD") or "").strip()
RECONCILE_INTERVAL_SECONDS = 300
_background_lock = threading.Lock()
_background_started = False

STATE_TO_POD = {
    **{s: "Blue" for s in ("AL","AR","FL","IL","IA","LA","MI","MN","MS","MO","NC","SC","WI","OR","WA","NV")},
    **{s: "Green" for s in ("CO","DC","GA","IN","KY","MD","NJ","OH","UT")},
    **{s: "Orange" for s in ("AK","AZ","CA","HI","ID")},
    **{s: "Purple" for s in ("KS","MT","NE","NM","ND","OK","SD","TN","TX","WY")},
    **{s: "Red" for s in ("CT","DE","ME","MA","NH","NY","PA","RI","VT","VA","WV")},
}
STATE_NAME_TO_ABBR = {
    "ALABAMA":"AL","ALASKA":"AK","ARIZONA":"AZ","ARKANSAS":"AR","CALIFORNIA":"CA",
    "COLORADO":"CO","CONNECTICUT":"CT","DELAWARE":"DE","FLORIDA":"FL","GEORGIA":"GA",
    "HAWAII":"HI","IDAHO":"ID","ILLINOIS":"IL","INDIANA":"IN","IOWA":"IA","KANSAS":"KS",
    "KENTUCKY":"KY","LOUISIANA":"LA","MAINE":"ME","MARYLAND":"MD","MASSACHUSETTS":"MA",
    "MICHIGAN":"MI","MINNESOTA":"MN","MISSISSIPPI":"MS","MISSOURI":"MO","MONTANA":"MT",
    "NEBRASKA":"NE","NEVADA":"NV","NEW HAMPSHIRE":"NH","NEW JERSEY":"NJ","NEW MEXICO":"NM",
    "NEW YORK":"NY","NORTH CAROLINA":"NC","NORTH DAKOTA":"ND","OHIO":"OH","OKLAHOMA":"OK",
    "OREGON":"OR","PENNSYLVANIA":"PA","RHODE ISLAND":"RI","SOUTH CAROLINA":"SC",
    "SOUTH DAKOTA":"SD","TENNESSEE":"TN","TEXAS":"TX","UTAH":"UT","VERMONT":"VT",
    "VIRGINIA":"VA","WASHINGTON":"WA","WEST VIRGINIA":"WV","WISCONSIN":"WI","WYOMING":"WY",
    "DISTRICT OF COLUMBIA":"DC",
}


def _pod_for(ic: dict) -> str | None:
    pod = _clean_text(ic.get("pod_color"))
    if pod:
        return pod
    import re
    address = str(ic.get("location") or "").upper()
    match = re.search(r"(?:,|\s)\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?|\s|,|$)", address)
    if match and match.group(1) in STATE_TO_POD:
        return STATE_TO_POD.get(match.group(1))
    for name, abbr in STATE_NAME_TO_ABBR.items():
        if re.search(rf"\b{re.escape(name)}\b", address):
            return STATE_TO_POD.get(abbr)
    return None


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


def preview_new_contractors(engine, *, cutoff=NEW_IC_CUTOFF) -> list[dict]:
    where_clause = "WHERE c.created_at >= :cutoff OR mi.monday_created_at >= :cutoff" if cutoff is not None else ""
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE IF NOT EXISTS contractor_monday_intake (
                email TEXT PRIMARY KEY, monday_item_id TEXT NOT NULL,
                monday_created_at TIMESTAMPTZ NOT NULL
            )
        """))
    with engine.connect() as conn:
        rows = conn.execute(sa.text(f"""
            SELECT c.id, c.name, c.email, c.phone, c.location, c.pod_color,
                   c.ic_list, c.created_at
            FROM contractors c
            LEFT JOIN contractor_monday_intake mi ON mi.email = c.email
            {where_clause}
            ORDER BY c.created_at DESC, c.id DESC
        """), {"cutoff": cutoff} if cutoff is not None else {}).mappings().all()

    workers = _onfleet_list_workers()
    teams_data = _onfleet_request("GET", "/teams").json()
    teams = teams_data if isinstance(teams_data, list) else teams_data.get("teams", [])
    team_ids = {_norm_title(t.get("name")): t.get("id") for t in teams}
    result = []
    for row in rows:
        ic = dict(row)
        status = (_clean_text(ic.get("ic_list")) or "").upper()
        pod = _pod_for(ic)
        phone = normalize_phone(ic.get("phone"))
        email = normalize_email(ic.get("email"))
        matched_worker = None
        reason = None
        if status not in ELIGIBLE:
            outcome = "ineligible"
            reason = f"ic_list={status or 'blank'}"
        elif not phone or len(phone) != 10 or not email or not _clean_text(ic.get("name")):
            outcome = "incomplete_contact"
            missing = []
            if not _clean_text(ic.get("name")):
                missing.append("name")
            if not phone or len(phone) != 10:
                missing.append("phone")
            if not email:
                missing.append("email")
            reason = "missing_or_invalid=" + ",".join(missing)
        else:
            # Check OnFleet first: an IC who is already a worker doesn't need a
            # pod/team, so a blank or unreadable pod must not hide that.
            outcome, matched_worker = _worker_matches(ic, workers)
            if outcome == "conflict":
                reason = "name/email/phone conflicts with existing OnFleet worker"
            elif outcome == "missing":
                team_id = team_ids.get(f"pod: {_norm_title(pod)}") if pod else None
                if not team_id:
                    team_id = team_ids.get(f"pod: {_norm_title(DEFAULT_POD)}") if DEFAULT_POD else None
                    if team_id:
                        reason = f"pod={pod or 'blank'} -> default POD: {DEFAULT_POD}"
                        pod = DEFAULT_POD
                if not team_id:
                    outcome = "missing_pod_team"
                    reason = f"pod={pod or 'blank'} location={_clean_text(ic.get('location')) or 'blank'}"
        result.append({
            **ic,
            "pod_color": pod,
            "outcome": outcome,
            "team_id": team_ids.get(f"pod: {_norm_title(pod)}"),
            "onfleet_worker_id": matched_worker.get("id") if matched_worker else None,
            "reason": reason,
        })
    return result


def _create_worker_in_onfleet(ic: dict) -> dict:
    phone = normalize_phone(ic["phone"])
    address = _clean_text(ic.get("location"))
    if not phone or len(phone) != 10 or not address:
        raise ValueError("valid phone and address required for OnFleet routing")
    destination = _onfleet_request("POST", "/destinations",
                                   json={"address": {"unparsed": address}}).json()
    destination_id = destination.get("id")
    if not destination_id:
        raise RuntimeError("OnFleet destination returned no id")
    body = {
        "name": _clean_text(ic["name"]), "phone": "+1" + phone,
        "email": normalize_email(ic["email"]), "teams": [ic["team_id"]],
        "addresses": {"routing": destination_id},
        "metadata": [{"name": "Address", "type": "string", "value": address}],
    }
    worker = _onfleet_request("POST", "/workers", json=body).json()
    worker_id = worker.get("id")
    verified = _onfleet_request("GET", f"/workers/{worker_id}").json() if worker_id else {}
    okay = (verified.get("id") == worker_id and normalize_phone(verified.get("phone")) == phone
            and bool((verified.get("addresses") or {}).get("routing")))
    return {"name": ic["name"], "status": "created" if okay else "verify_failed", "worker_id": worker_id}


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
                try:
                    results.append(_create_worker_in_onfleet(ic))
                except Exception as exc:
                    results.append({"name": ic["name"], "status": "failed", "reason": str(exc)})
            return results
        finally:
            lock_conn.execute(sa.text("SELECT pg_advisory_unlock(817496325)"))


def reconcile_missing_once(engine) -> list[dict]:
    """Sync one bounded batch; the next cycle picks up any remaining workers."""
    preview = preview_new_contractors(engine)
    missing_ids = {ic["id"] for ic in preview if ic["outcome"] == "missing"}
    print(f"[onfleet/ic-sync] checked={len(preview)} missing={len(missing_ids)}", flush=True)
    for ic in preview:
        worker_suffix = f" worker_id={ic['onfleet_worker_id']}" if ic.get("onfleet_worker_id") else ""
        reason_suffix = f" reason={ic['reason']}" if ic.get("reason") else ""
        print(
            f"[onfleet/ic-sync/detail] name={ic.get('name') or '(blank)'} "
            f"outcome={ic['outcome']}{worker_suffix}{reason_suffix}",
            flush=True,
        )
    if not missing_ids:
        return []
    # Keep each pass bounded, even when a large intake lands at once.
    return create_missing_contractors(engine, selected_ids=set(sorted(missing_ids)[:MAX_CREATES_PER_RUN]))


def audit_full_roster_once(engine) -> None:
    """Count older contractors omitted by the intake cutoff; never create."""
    preview = preview_new_contractors(engine, cutoff=None)
    outcomes: dict[str, int] = {}
    for ic in preview:
        outcome = ic["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    newest = max((str(ic["created_at"]) for ic in preview if ic.get("created_at")), default="none")
    print(f"[onfleet/ic-sync] full_roster={len(preview)} outcomes={outcomes} newest_created_at={newest}", flush=True)
    # Name every eligible IC who isn't cleanly in OnFleet, so they can be
    # reviewed/added from the logs (counts alone don't say who).
    for ic in preview:
        if ic["outcome"] in ("missing", "missing_pod_team", "conflict", "incomplete_contact"):
            print(
                f"[onfleet/ic-sync/roster] name={ic.get('name') or '(blank)'} outcome={ic['outcome']} "
                f"phone={normalize_phone(ic.get('phone')) or 'blank'} pod={ic.get('pod_color') or 'blank'} "
                f"location={_clean_text(ic.get('location')) or 'blank'}"
                + (f" reason={ic['reason']}" if ic.get("reason") else ""),
                flush=True,
            )


def start_background_reconciliation(engine) -> None:
    """Start once per app process; PostgreSQL locks serialize multiple replicas."""
    global _background_started
    if engine is None:
        return
    with _background_lock:
        if _background_started:
            return
        _background_started = True
    print("[onfleet/ic-sync] worker started", flush=True)

    def run() -> None:
        try:
            audit_full_roster_once(engine)
        except Exception as exc:
            print(f"[onfleet/ic-sync] full roster audit failed: {type(exc).__name__}: {exc}", flush=True)
        while True:
            try:
                results = reconcile_missing_once(engine)
                if results:
                    counts: dict[str, int] = {}
                    for result in results:
                        status = result["status"]
                        counts[status] = counts.get(status, 0) + 1
                        worker_suffix = f" worker_id={result['worker_id']}" if result.get("worker_id") else ""
                        reason_suffix = f" reason={result['reason']}" if result.get("reason") else ""
                        print(
                            f"[onfleet/ic-sync/result] name={result.get('name') or '(blank)'} "
                            f"status={status}{worker_suffix}{reason_suffix}",
                            flush=True,
                        )
                    print(f"[onfleet/ic-sync] {counts}", flush=True)
            except Exception as exc:
                # Keep the app available and retry on the next cycle.
                print(f"[onfleet/ic-sync] {type(exc).__name__}: {exc}", flush=True)
            threading.Event().wait(RECONCILE_INTERVAL_SECONDS)

    threading.Thread(target=run, name="onfleet-ic-sync", daemon=True).start()
