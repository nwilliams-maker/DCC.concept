"""One-time import of the three requested WO workbook tabs into an isolated DB.

The workbook is uploaded through a temporary, token-gated Streamlit form; its
contents are never checked into GitHub or written to an app disk. This module
does not call Onfleet, email, or the contractor portal.
"""

from __future__ import annotations

import io
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
CENTRAL = ZoneInfo("America/Chicago")
TABS = ("Saved_Routes", "Accepted routes", "Field Nation")


def _rows(sheet):
    source = iter(sheet.values)
    headers = [str(v or "").strip().lower() for v in next(source)]
    for raw in source:
        if any(v is not None and str(v).strip() for v in raw):
            yield dict(zip(headers, raw))


def _date(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=CENTRAL) if value.tzinfo is None else value
    if value:
        return datetime.fromisoformat(str(value)).replace(tzinfo=CENTRAL)
    raise ValueError("Route is missing Date Created")


def _route(row, status):
    route_id = str(row.get("route id") or "").strip()
    wo = str(row.get("wo") or "").strip()
    if not route_id or not wo:
        raise ValueError("Route is missing Route ID or WO")
    payload = json.loads(row.get("json payload") or "")
    if not isinstance(payload, dict):
        raise ValueError("Route JSON Payload must be an object")
    if payload.get("wo") and str(payload["wo"]).strip() != wo:
        raise ValueError("Route WO differs from JSON Payload WO")
    return {"route_id": route_id, "wo": wo, "status": status,
            "contractor_name": str(row.get("contractor") or payload.get("icn") or "").strip(),
            "created": _date(row.get("date created")), "payload": payload}


def _stage(workbook_bytes):
    if len(workbook_bytes) > 5_000_000:
        raise ValueError("Workbook exceeds 5 MB")
    book = openpyxl.load_workbook(io.BytesIO(workbook_bytes), read_only=True, data_only=True)
    if any(name not in book.sheetnames for name in TABS):
        raise ValueError("Workbook needs Saved_Routes, Accepted routes, and Field Nation tabs")
    routes = [_route(row, status) for tab, status in ((TABS[0], "sent"), (TABS[1], "accepted"))
              for row in _rows(book[tab])]
    fn = []
    for row in _rows(book[TABS[2]]):
        wo = str(row.get("work order") or "").strip()
        if not wo:
            raise ValueError("Field Nation row is missing Work Order")
        payload = json.loads(row.get("json payload") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Field Nation JSON Payload must be an object")
        state = str(row.get("status") or "").lower()
        fn.append({"wo": wo, "status": "assigned" if "assign" in state else "posted",
                   "payload": payload})
    book.close()
    chosen = {}
    for row in routes:
        previous = chosen.get(row["wo"])
        if previous is None or ((row["status"] == "accepted"), row["created"]) > (
                (previous["status"] == "accepted"), previous["created"]):
            chosen[row["wo"]] = row
    if len({r["route_id"] for r in routes}) != len(routes):
        raise ValueError("Duplicate Route ID in workbook")
    if len({r["wo"] for r in fn}) != len(fn):
        raise ValueError("Duplicate Field Nation work order in workbook")
    return routes, chosen, fn


def _schema(database_url):
    import psycopg2
    # A fresh isolated database has no tables. No other project is consulted.
    raw = psycopg2.connect(database_url)
    try:
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute("SELECT to_regclass('public.routes')")
            if cur.fetchone()[0] is None:
                cur.execute(Path(__file__).with_name("schema.sql").read_text())
    finally:
        raw.close()


def import_workbook(workbook_bytes: bytes, database_url: str) -> dict:
    import sqlalchemy as sa
    routes, chosen, fn = _stage(workbook_bytes)
    _schema(database_url)
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(7720230924)"))
        # Existing decisions are left intact on a repeat import. Accepted wins
        # over sent within the workbook, including duplicates in both tabs.
        for wo, row in chosen.items():
            p = row["payload"]
            conn.execute(sa.text("""
                INSERT INTO routes (wo, contractor_name, status, comp, due, locs,
                                    stop_data, cluster_hash, payload, created_at, updated_at)
                VALUES (:wo, :name, CAST(:status AS route_status), :comp, :due,
                        CAST(:locs AS jsonb), CAST(:stop_data AS jsonb), :hash,
                        CAST(:payload AS jsonb), :created, :created)
                ON CONFLICT (wo) DO NOTHING
            """), {"wo": wo, "name": row["contractor_name"], "status": row["status"],
                   "comp": p.get("comp"), "due": p.get("due") or None,
                   "locs": json.dumps(p.get("locs")), "stop_data": json.dumps(p.get("stopData")),
                   "hash": p.get("cluster_hash"), "payload": json.dumps(p), "created": row["created"]})
        for row in routes:
            conn.execute(sa.text("""
                INSERT INTO legacy_route_links (route_id, wo, payload, source_status, active, created_at)
                VALUES (:route_id, :wo, CAST(:payload AS jsonb), :status, TRUE, :created)
                ON CONFLICT (route_id) DO NOTHING
            """), {"route_id": row["route_id"], "wo": row["wo"],
                   "payload": json.dumps(row["payload"]), "status": row["status"], "created": row["created"]})
        for row in fn:
            conn.execute(sa.text("""
                INSERT INTO field_nation_orders (work_order, status, payload)
                VALUES (:wo, CAST(:status AS fn_status), CAST(:payload AS jsonb))
                ON CONFLICT (work_order) DO NOTHING
            """), {"wo": row["wo"], "status": row["status"], "payload": json.dumps(row["payload"])})
        counts = dict(conn.execute(sa.text("""
            SELECT 'sent' AS kind, count(*) FROM routes WHERE status='sent'
            UNION ALL SELECT 'accepted', count(*) FROM routes WHERE status='accepted'
            UNION ALL SELECT 'field_nation', count(*) FROM field_nation_orders
            UNION ALL SELECT 'legacy_links', count(*) FROM legacy_route_links
        """)).all())
    engine.dispose()
    return {"source_saved": sum(r["status"] == "sent" for r in routes),
            "source_accepted": sum(r["status"] == "accepted" for r in routes),
            "source_field_nation": len(fn), "unique_routes": len(chosen),
            "database": counts}
