"""One-time, additive recovery of DCC routes and old contractor link IDs.

Runs inside Railway with DATABASE_URL and the old IC_SHEET_URL. Dry-run by
default; set RECOVERY_APPLY=1 for the actual transaction. Never schedules
itself, emails contractors, or calls OnFleet. Safe to rerun.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa

GIDS = (
    ("sent", "1477617688"),  # Saved_Routes
    ("accepted", "934075207"),  # Accepted routes
)
FN_GID = "1396320527"
CHICAGO = ZoneInfo("America/Chicago")
WO_PATTERN = re.compile(r"^(.*)-(\d{8})-(\d+)$")


def source_rows(url: str, gid: str) -> list[dict]:
    base = url.split("/edit", 1)[0].rstrip("/")
    if not base.startswith("https://docs.google.com/spreadsheets/d/"):
        raise ValueError("IC_SHEET_URL must be a Google spreadsheet URL")
    export = f"{base}/export?format=csv&gid={gid}"
    last_error = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(export, headers={"User-Agent": "DCC-one-time-recovery/1.0"})
            with urllib.request.urlopen(req, timeout=35) as response:
                raw = response.read()
            return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        except Exception as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not read sheet tab {gid}: {type(last_error).__name__}") from last_error


def sheet_time(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%m/%d/%Y %H:%M:%S").replace(tzinfo=CHICAGO)


def archive_time(row: dict) -> datetime:
    try:
        archived_at = datetime.fromisoformat(str(row["archive_time"]).replace("Z", "+00:00"))
        return archived_at if archived_at.tzinfo else archived_at.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return row["created_at"]


def parse_route(row: dict, status: str) -> dict | None:
    identifier = str(row.get("Route ID") or "").strip()
    wo = str(row.get("WO") or row.get("WO#") or "").strip()
    if not identifier or not wo:
        return None
    try:
        payload = json.loads(row.get("JSON Payload") or "")
        created = sheet_time(row.get("Date Created") or "")
        if not isinstance(payload, dict):
            return None
    except (ValueError, TypeError):
        return None
    return {"route_id": identifier, "wo": wo, "status": status,
            "contractor_name": str(row.get("Contractor") or payload.get("icn") or "").strip(),
            "payload": payload, "created_at": created,
            "archive_time": payload.get("archive_ts")}


def canonical_routes(rows: list[dict]) -> dict[str, dict]:
    # If the same WO appears more than once, a decision beats an older sent
    # copy. Within a status, keep the most recent spreadsheet row.
    priority = {"sent": 0, "declined": 1, "accepted": 2, "finalized": 3}
    chosen = {}
    for row in rows:
        if row["status"] == "archived":
            continue
        prior = chosen.get(row["wo"])
        if prior is None or (priority[row["status"]], row["created_at"]) > (priority[prior["status"]], prior["created_at"]):
            chosen[row["wo"]] = row
    return chosen


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    sheet_url = os.environ["IC_SHEET_URL"]
    apply = os.environ.get("RECOVERY_APPLY") == "1"
    routes = []
    for status, gid in GIDS:
        fetched = source_rows(sheet_url, gid)
        parsed = [r for raw in fetched if (r := parse_route(raw, status))]
        if fetched and len(parsed) < len(fetched) * 0.9:
            raise RuntimeError(f"Too many invalid {status} rows: {len(parsed)}/{len(fetched)}")
        routes.extend(parsed)
        print(f"source {status}: {len(parsed)} valid rows", flush=True)
    fn_rows = []  # Only the two requested route tabs are recovered.
    chosen = canonical_routes(routes)
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        existing = {row[0]: row[1] for row in conn.execute(sa.text("SELECT wo, status::text FROM routes"))}
        fn_existing = {row[0] for row in conn.execute(sa.text("SELECT work_order FROM field_nation_orders"))}
    missing = [wo for wo in chosen if wo not in existing]
    new_archive = [r for r in routes if r["status"] == "archived" and r["wo"] not in existing
                   and r["wo"] not in chosen and archive_time(r) >= datetime(2026, 9, 18, tzinfo=timezone.utc)]
    print(json.dumps({"mode": "apply" if apply else "dry_run", "source_routes": len(routes),
                      "unique_active_wos": len(chosen), "missing_active_wos": len(missing),
                      "missing_by_status": dict(Counter(chosen[w]["status"] for w in missing)),
                      "missing_archived_wos": len({r["wo"] for r in new_archive}),
                      "source_field_nation": len(fn_rows),
                      "missing_field_nation": sum(1 for r in fn_rows if (r.get("Work Order") or "").strip() not in fn_existing)}, sort_keys=True), flush=True)
    if not apply:
        return

    counts = Counter()
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(7720230918)"))
        conn.execute(sa.text("""
            CREATE TABLE IF NOT EXISTS legacy_route_links (
                route_id TEXT PRIMARY KEY, wo TEXT NOT NULL, payload JSONB NOT NULL,
                source_status TEXT NOT NULL, active BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
        """))
        for wo, row in chosen.items():
            payload = row["payload"]
            result = conn.execute(sa.text("""
                INSERT INTO routes (wo, contractor_name, status, comp, due, locs, stop_data, cluster_hash, payload, created_at, updated_at)
                VALUES (:wo, :name, CAST(:status AS route_status), :comp, :due, CAST(:locs AS jsonb), CAST(:stop_data AS jsonb), :hash, CAST(:payload AS jsonb), :created, :created)
                ON CONFLICT (wo) DO NOTHING
            """), {"wo": wo, "name": row["contractor_name"], "status": row["status"],
                   "comp": payload.get("comp"), "due": payload.get("due") or None,
                   "locs": json.dumps(payload.get("locs")), "stop_data": json.dumps(payload.get("stopData")),
                   "hash": payload.get("cluster_hash"), "payload": json.dumps(payload), "created": row["created_at"]})
            counts["routes_inserted"] += result.rowcount
            # A legacy GAS acceptance can have happened after the earlier DB
            # snapshot. Only promote a still-sent row with no DB decision.
            if row["status"] in ("accepted", "declined") and not result.rowcount:
                result = conn.execute(sa.text("""
                    UPDATE routes SET status = CAST(:status AS route_status), updated_at = now()
                    WHERE wo = :wo AND status = 'sent'
                      AND NOT EXISTS (
                          SELECT 1 FROM route_events e
                          WHERE e.route_id = routes.id AND e.action = 'processDecision'
                      )
                """), {"wo": wo, "status": row["status"]})
                counts["legacy_decisions_recovered"] += result.rowcount
            match = WO_PATTERN.match(wo)
            if match:
                try:
                    wo_date = datetime.strptime(match[2], "%m%d%Y").date()
                except ValueError:
                    continue
                conn.execute(sa.text("""
                    INSERT INTO wo_counters (contractor_name, wo_date, next_suffix)
                    VALUES (:name, :date, :next)
                    ON CONFLICT (contractor_name, wo_date) DO UPDATE
                    SET next_suffix = GREATEST(wo_counters.next_suffix, EXCLUDED.next_suffix)
                """), {"name": match[1], "date": wo_date, "next": int(match[3]) + 1})
        for row in new_archive:
            payload = row["payload"]
            conn.execute(sa.text("""
                INSERT INTO routes (wo, contractor_name, status, comp, due, locs, stop_data, cluster_hash, payload, created_at, updated_at)
                VALUES (:wo, :name, 'archived', :comp, :due, CAST(:locs AS jsonb), CAST(:stop_data AS jsonb), :hash, CAST(:payload AS jsonb), :created, :created)
                ON CONFLICT (wo) DO NOTHING
            """), {"wo": row["wo"], "name": row["contractor_name"], "comp": payload.get("comp"),
                   "due": payload.get("due") or None, "locs": json.dumps(payload.get("locs")),
                   "stop_data": json.dumps(payload.get("stopData")), "hash": payload.get("cluster_hash"),
                   "payload": json.dumps(payload), "created": row["created_at"]})
        for row in routes:
            # Archive rows invalidate their exact R-ID, even if a new route
            # happens to reuse the same WO. Keep every historic ID distinct.
            result = conn.execute(sa.text("""
                INSERT INTO legacy_route_links (route_id, wo, payload, source_status, active, created_at)
                VALUES (:id, :wo, CAST(:payload AS jsonb), :status, :active, :created)
                ON CONFLICT (route_id) DO NOTHING
            """), {"id": row["route_id"], "wo": row["wo"], "payload": json.dumps(row["payload"]),
                   "status": row["status"], "active": row["status"] != "archived", "created": row["created_at"]})
            counts["links_inserted"] += result.rowcount
        archived_ids = {r["route_id"] for r in routes if r["status"] == "archived"}
        for identifier in archived_ids:
            conn.execute(sa.text("UPDATE legacy_route_links SET active = FALSE, source_status = 'archived' WHERE route_id = :id"), {"id": identifier})
        for row in routes:
            if row["status"] != "archived":
                continue
            archived_at = archive_time(row)
            if archived_at < datetime(2026, 9, 18, tzinfo=timezone.utc):
                continue
            event = {"action_label": row["payload"].get("archive_action") or "Archived",
                     "ic_name": row["payload"].get("archive_ic") or row["contractor_name"],
                     "legacy_route_id": row["route_id"]}
            result = conn.execute(sa.text("""
                INSERT INTO route_events (route_id, action, payload, created_at)
                SELECT r.id, 'archiveRoute', CAST(:payload AS jsonb), :created
                FROM routes r
                WHERE r.wo = :wo AND r.status = 'archived'
                  AND NOT EXISTS (
                    SELECT 1 FROM route_events e WHERE e.action = 'archiveRoute'
                      AND e.payload->>'legacy_route_id' = :legacy_id
                  )
            """), {"wo": row["wo"], "payload": json.dumps(event),
                   "created": archived_at, "legacy_id": row["route_id"]})
            counts["archive_events_inserted"] += result.rowcount
        for row in fn_rows:
            wo = str(row.get("Work Order") or "").strip()
            if not wo:
                continue
            try:
                payload = json.loads(row.get("JSON Payload") or "{}")
                created = sheet_time(row.get("Date Created") or "")
            except (ValueError, TypeError):
                counts["fn_invalid"] += 1
                continue
            status = "assigned" if "assign" in str(row.get("Status") or "").lower() else "posted"
            result = conn.execute(sa.text("""
                INSERT INTO field_nation_orders (work_order, status, route_plan_id, payload, created_at, updated_at)
                VALUES (:wo, CAST(:status AS fn_status), :plan, CAST(:payload AS jsonb), :created, :created)
                ON CONFLICT (work_order) DO NOTHING
            """), {"wo": wo, "status": status, "plan": payload.get("routePlanId"),
                   "payload": json.dumps(payload), "created": created})
            counts["field_nation_inserted"] += result.rowcount
    print(json.dumps({"committed": dict(counts)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
