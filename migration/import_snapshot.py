"""Merge a read-only original DCC database snapshot into isolated revamp DB."""

from __future__ import annotations

import io
import json
import zipfile


def import_snapshot(data: bytes, database_url: str) -> dict:
    import sqlalchemy as sa

    if len(data) > 10_000_000:
        raise ValueError("Snapshot archive exceeds 10 MB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("dcc_source.json")
        if info.file_size > 30_000_000:
            raise ValueError("Snapshot expands beyond 30 MB")
        snapshot = json.loads(archive.read(info))
    required = ("contractors", "routes", "field_nation_orders", "legacy_route_links",
                "route_events", "bundle_maps", "wo_counters")
    if any(not isinstance(snapshot.get(table), list) for table in required):
        raise ValueError("Snapshot is missing a DCC table")
    original_contractors = {row["id"]: row["email"] for row in snapshot["contractors"]}
    original_routes = {row["id"]: row["wo"] for row in snapshot["routes"]}
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(7720230924)"))
        for row in snapshot["contractors"]:
            conn.execute(sa.text("""
                INSERT INTO contractors (email, name, location, phone, ic_list, lat, lng,
                                         pod_color, digital_certified, unrestricted, created_at, updated_at)
                VALUES (:email, :name, :location, :phone, :ic_list, :lat, :lng,
                        :pod_color, :digital_certified, :unrestricted, :created_at, :updated_at)
                ON CONFLICT (email) DO UPDATE SET
                    name=EXCLUDED.name, location=EXCLUDED.location, phone=EXCLUDED.phone,
                    ic_list=EXCLUDED.ic_list, lat=EXCLUDED.lat, lng=EXCLUDED.lng,
                    pod_color=EXCLUDED.pod_color, digital_certified=EXCLUDED.digital_certified,
                    unrestricted=EXCLUDED.unrestricted, updated_at=EXCLUDED.updated_at
            """), {key: row.get(key) for key in (
                "email", "name", "location", "phone", "ic_list", "lat", "lng", "pod_color",
                "digital_certified", "unrestricted", "created_at", "updated_at")})
        for row in snapshot["routes"]:
            params = {key: row.get(key) for key in (
                "wo", "contractor_name", "status", "comp", "due", "cluster_hash", "created_at", "updated_at")}
            params.update(contractor_email=original_contractors.get(row.get("contractor_id")),
                          locs=json.dumps(row.get("locs")), stop_data=json.dumps(row.get("stop_data")),
                          payload=json.dumps(row["payload"]))
            conn.execute(sa.text("""
                INSERT INTO routes (wo, contractor_id, contractor_name, status, comp, due,
                                    locs, stop_data, cluster_hash, payload, created_at, updated_at)
                VALUES (:wo, (SELECT id FROM contractors WHERE email=:contractor_email),
                        :contractor_name, CAST(:status AS route_status), :comp, :due,
                        CAST(:locs AS jsonb), CAST(:stop_data AS jsonb), :cluster_hash,
                        CAST(:payload AS jsonb), :created_at, :updated_at)
                ON CONFLICT (wo) DO UPDATE SET
                    contractor_id=EXCLUDED.contractor_id,
                    contractor_name=EXCLUDED.contractor_name, status=EXCLUDED.status,
                    comp=EXCLUDED.comp, due=EXCLUDED.due, locs=EXCLUDED.locs,
                    stop_data=EXCLUDED.stop_data, cluster_hash=EXCLUDED.cluster_hash,
                    payload=EXCLUDED.payload, updated_at=EXCLUDED.updated_at
                WHERE routes.status='sent' OR EXCLUDED.status!='sent'
            """), params)
        for row in snapshot["field_nation_orders"]:
            conn.execute(sa.text("""
                INSERT INTO field_nation_orders (work_order, status, provider, route_plan_id,
                                                 payload, created_at, updated_at)
                VALUES (:work_order, CAST(:status AS fn_status), :provider, :route_plan_id,
                        CAST(:payload AS jsonb), :created_at, :updated_at)
                ON CONFLICT (work_order) DO UPDATE SET
                    status=EXCLUDED.status, provider=EXCLUDED.provider,
                    route_plan_id=EXCLUDED.route_plan_id, payload=EXCLUDED.payload,
                    updated_at=EXCLUDED.updated_at
                WHERE field_nation_orders.status='posted' OR EXCLUDED.status='assigned'
            """), {**{key: row.get(key) for key in (
                "work_order", "status", "provider", "route_plan_id", "created_at", "updated_at")},
                   "payload": json.dumps(row["payload"])})
        for row in snapshot["legacy_route_links"]:
            conn.execute(sa.text("""
                INSERT INTO legacy_route_links (route_id, wo, payload, source_status, active, created_at)
                VALUES (:route_id, :wo, CAST(:payload AS jsonb), :source_status, :active, :created_at)
                ON CONFLICT (route_id) DO UPDATE SET
                    wo=EXCLUDED.wo, payload=EXCLUDED.payload,
                    source_status=EXCLUDED.source_status, active=EXCLUDED.active
            """), {**{key: row.get(key) for key in (
                "route_id", "wo", "source_status", "active", "created_at")},
                   "payload": json.dumps(row["payload"])})
        for row in snapshot["bundle_maps"]:
            conn.execute(sa.text("""
                INSERT INTO bundle_maps (pod, dispatcher_id, task_id_sets, updated_at)
                VALUES (:pod, :dispatcher_id, CAST(:task_id_sets AS jsonb), :updated_at)
                ON CONFLICT (pod, dispatcher_id) DO UPDATE SET
                    task_id_sets=EXCLUDED.task_id_sets, updated_at=EXCLUDED.updated_at
            """), {"pod": row["pod"], "dispatcher_id": row["dispatcher_id"],
                   "task_id_sets": json.dumps(row["task_id_sets"]), "updated_at": row["updated_at"]})
        for row in snapshot["wo_counters"]:
            conn.execute(sa.text("""
                INSERT INTO wo_counters (contractor_name, wo_date, next_suffix)
                VALUES (:contractor_name, :wo_date, :next_suffix)
                ON CONFLICT (contractor_name, wo_date) DO UPDATE SET
                    next_suffix=GREATEST(wo_counters.next_suffix, EXCLUDED.next_suffix)
            """), {key: row[key] for key in ("contractor_name", "wo_date", "next_suffix")})
        for row in snapshot["route_events"]:
            wo = original_routes.get(row.get("route_id"))
            conn.execute(sa.text("""
                INSERT INTO route_events (route_id, action, payload, created_at)
                SELECT (SELECT id FROM routes WHERE wo=:wo), :action, CAST(:payload AS jsonb), :created_at
                WHERE NOT EXISTS (
                    SELECT 1 FROM route_events WHERE action=:action AND created_at=:created_at
                    AND payload=CAST(:payload AS jsonb)
                )
            """), {"wo": wo, "action": row["action"], "payload": json.dumps(row["payload"]),
                   "created_at": row["created_at"]})
        counts = {table: conn.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
                  for table in required}
    engine.dispose()
    return {"source": snapshot["_source_counts"], "destination": counts}
