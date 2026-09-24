"""Bulk Field Nation persistence for the isolated DCC revamp.

This uses the same Postgres write and Onfleet handoff as the existing
single-route checkbox. It never sends contractor email or creates a
contractor acceptance link.
"""

import hashlib
import json
from datetime import datetime

import sqlalchemy as sa

from migration import data_access


def cluster_hash(route):
    ids = sorted(str(task.get("id", "")).strip() for task in route.get("data", []))
    return hashlib.md5("".join(ids).encode()).hexdigest()


def _payload(route, pod, due, work_order, route_hash):
    tasks = route.get("data", [])
    addresses = list(dict.fromkeys(str(t.get("full", "")).strip() for t in tasks if t.get("full")))
    stops = []
    for address in addresses:
        at_stop = [t for t in tasks if str(t.get("full", "")).strip() == address]
        first = at_stop[0]
        stops.append({
            "addr": address,
            "venue": first.get("venue_name", ""),
            "t_count": len(at_stop),
            "esc": any(t.get("escalated") for t in at_stop),
            "inst": sum("install" in str(t.get("task_type", "")).lower() for t in at_stop),
            "remov": sum("remov" in str(t.get("task_type", "")).lower() for t in at_stop),
            "n_ad": sum("new ad" in str(t.get("task_type", "")).lower() for t in at_stop),
            "c_ad": sum("continuity" in str(t.get("task_type", "")).lower() for t in at_stop),
            "d_ad": sum("default" in str(t.get("task_type", "")).lower() for t in at_stop),
            "kioskId": first.get("kiosk_id", ""),
            "venueId": first.get("venue_id", ""),
            "locationInVenue": first.get("location_in_venue", ""),
            "campaigns": list({
                str(t.get("client_company", "")): {"name": t.get("client_company", ""),
                    "esc": bool(t.get("escalated")), "bs": str(t.get("boosted_standard", "")).lower()}
                for t in at_stop if t.get("client_company")
            }.values()),
            "customerType": first.get("customer_type", ""),
            "boostedStandard": first.get("boosted_standard", ""),
            "artFile": first.get("art_file", ""),
            "zip": first.get("zip", ""),
            "sio": first.get("sio", ""),
        })
    center = route.get("center") or (0, 0)
    home = f"{center[0]},{center[1]}"
    return {
        "cluster_hash": route_hash,
        "icn": "Field Nation",
        "pod": pod,
        "city": route.get("city", "Unknown"),
        "state": route.get("state", "Unknown"),
        "taskIds": ",".join(str(t["id"]).strip() for t in tasks),
        "wo": work_order,
        "due": str(due),
        "lCnt": route.get("stops", len(addresses)),
        "tCnt": len(tasks),
        "kCnt": route.get("inst_count", 0),
        "locs": " | ".join([home] + addresses + [home]),
        "stopData": json.dumps(stops),
    }


def bulk_assign(engine, selected_routes, due, assign_tasks_to_fn_team,
                fn_team_id=None, fn_worker_id=None):
    """Return (saved, skipped, errors); each result identifies a route hash.

    Selection must already be limited to undispatched live routes. Recheck
    persisted cluster hashes before any write to prevent repeat clicks from
    making duplicate Field Nation orders.
    """
    saved, skipped, errors = [], [], []
    if engine is None or not fn_team_id or not fn_worker_id:
        return saved, skipped, [("", "Database, Field Nation team and worker are required")]
    for pod, route in selected_routes:
        route_hash = cluster_hash(route)
        task_ids = [str(t.get("id", "")).strip() for t in route.get("data", [])]
        if not task_ids or any(not tid for tid in task_ids):
            errors.append((route_hash, "Route contains a task without an ID"))
            continue
        try:
            with engine.connect() as conn:
                existing = conn.execute(sa.text("""
                    SELECT work_order FROM field_nation_orders
                    WHERE payload->>'cluster_hash' = :route_hash LIMIT 1
                """), {"route_hash": route_hash}).scalar()
            if existing:
                skipped.append((route_hash, existing))
                continue
            # A stable suffix keeps retries on the same WO and avoids a race
            # between multiple selected routes in the same city.
            tag = datetime.now().strftime("%m%d%Y")
            city = str(route.get("city") or "Unknown").strip()
            state = str(route.get("state") or "").strip().upper()
            work_order = f"FN{tag}-{city} {state}-{route_hash[:10]}"
            payload = _payload(route, pod, due, work_order, route_hash)
            result = data_access.save_to_field_nation(engine, work_order, payload)
            if not result.get("success"):
                raise RuntimeError(str(result))
            saved.append((route_hash, work_order))
            if fn_team_id or fn_worker_id:
                assign_tasks_to_fn_team(task_ids, fn_team_id, fn_worker_id=fn_worker_id,
                                        wo_name=work_order, due_date=str(due),
                                        cluster_hash=route_hash)
        except Exception as exc:
            errors.append((route_hash, str(exc)))
    return saved, skipped, errors
