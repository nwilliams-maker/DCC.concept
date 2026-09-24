from __future__ import annotations
import json, os
import sqlalchemy as sa

db=(os.environ.get("DATABASE_URL") or "").strip()
if not db:
    raise RuntimeError("DATABASE_URL missing")
engine=sa.create_engine(db,pool_pre_ping=True)
with engine.connect() as conn:
    rows=conn.execute(sa.text("""
      SELECT
        r.id,
        r.wo,
        r.contractor_name,
        r.contractor_id,
        c.pod_color,
        r.updated_at,
        (
          SELECT max(e.created_at)
          FROM route_events e
          WHERE e.route_id=r.id
            AND e.action='processDecision'
            AND lower(coalesce(e.payload->>'decision',''))='accept'
        ) AS accepted_at
      FROM routes r
      LEFT JOIN contractors c ON c.id=r.contractor_id
      WHERE r.status::text='accepted'
        AND lower(coalesce(c.pod_color,r.payload->>'pod',r.payload->>'pod_name',''))='orange'
      ORDER BY coalesce((
          SELECT max(e.created_at)
          FROM route_events e
          WHERE e.route_id=r.id
            AND e.action='processDecision'
            AND lower(coalesce(e.payload->>'decision',''))='accept'
        ), r.updated_at) DESC
      LIMIT 10
    """)).mappings().all()
print("ORANGE_ACCEPTED_POSTGRES="+json.dumps([dict(x) for x in rows],default=str,separators=(",",":")),flush=True)
