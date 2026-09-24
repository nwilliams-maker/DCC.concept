"""Offline verification of import_from_sheets.py and data_access.py against a
real Postgres, using sample data shaped like the real Sheet tabs instead of a
live fetch -- so this runs in CI/offline without hitting the production Sheet
on every run.

Usage (against a throwaway/local database -- this DROPS and recreates data):
    createdb dcc_test
    psql dcc_test -f migration/schema.sql
    export DATABASE_URL="postgresql://localhost/dcc_test"
    export IC_SHEET_URL="https://docs.google.com/spreadsheets/d/x/edit"  # any value; not fetched
    python migration/tests/test_migration.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import sqlalchemy as sa

import import_from_sheets as imp
import data_access as da

DATABASE_URL = os.environ["DATABASE_URL"]
engine = sa.create_engine(DATABASE_URL)

# --- sample data, shaped like the real tabs per the earlier investigation ---

CONTRACTORS_CSV = """Email,Name,Location,Phone,IC List,Lat,Lng,Pod Color,Digital Certified
vnouchi2002@yahoo.com,Vance Nouchi,"855 Olokele Avenue #205, Honolulu HI",18087220610,IC Active,21.2851391,-157.8155618,Orange,
dwilliams@apexsolutions.one,Darron Williams,"416 karla ct martinsburg WV 25404",13042836400,IC Active,39.4696131,-77.9440144,Red,YES
biscardijoe@example.com,Joe Biscardi,"1 Test St",15551234567,IC Active,,,Blue,
"""

route_payload = {
    "wo": "Test Contractor-09182026-1", "comp": 150.5, "due": "2026-09-25",
    "locs": ["123 Main St"], "stopData": [{"addr": "123 Main St"}],
    "cluster_hash": "abc123",
}
SENT_CSV = f"""Contractor,Date Created,JSON Payload
Test Contractor,9/18/2026 10:00:00,"{json.dumps(route_payload).replace('"', '""')}"
"""

fn_payload = {"wo": "FN-Test-1", "address": "456 Oak Ave"}
FN_CSV = f"""Contractor,Work Order,JSON Payload,Date Created,Status
Field Nation,FN-Test-1,"{json.dumps(fn_payload).replace('"', '""')}",9/18/2026 11:00:00,field_nation
"""

archive_payload = {"archived_wo": "Test Contractor-09182026-1", "archive_action": "finalizeRoute", "archive_ts": "2026-09-18T12:00:00Z"}
ARCHIVE_CSV = f"""Contractor,Date Created,JSON Payload
Test Contractor,9/18/2026 12:00:00,"{json.dumps(archive_payload).replace('"', '""')}"
"""

_orig_fetch_csv = imp.fetch_csv
def fake_fetch_csv(gid):
    src = {
        imp.CONTRACTORS_GID: CONTRACTORS_CSV,
        "1477617688": SENT_CSV,
        "934075207": "Contractor,Date Created,JSON Payload\n",
        "600909788": "Contractor,Date Created,JSON Payload\n",
        "1907347870": "Contractor,Date Created,JSON Payload\n",
        imp.FIELD_NATION_GID: FN_CSV,
        imp.ARCHIVE_GID: ARCHIVE_CSV,
    }[gid]
    df = pd.read_csv(io.StringIO(src))
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df

imp.fetch_csv = fake_fetch_csv

print("=== Running import ===")
imp.main()

print("\n=== Verifying imported rows ===")
with engine.connect() as conn:
    contractors = conn.execute(sa.text("SELECT email, name, pod_color, unrestricted FROM contractors ORDER BY email")).mappings().all()
    for c in contractors:
        print(dict(c))
    assert len(contractors) == 3
    assert any(c["unrestricted"] for c in contractors), "Biscardi should be flagged unrestricted"

    routes = conn.execute(sa.text("SELECT wo, status, comp, cluster_hash FROM routes")).mappings().all()
    print(routes)
    assert len(routes) == 1 and routes[0]["status"] == "sent" and routes[0]["cluster_hash"] == "abc123"

    fn = conn.execute(sa.text("SELECT work_order, status FROM field_nation_orders")).mappings().all()
    print(fn)
    assert len(fn) == 1 and fn[0]["status"] == "posted"

    # Archive tab history is intentionally NOT imported (see import_from_sheets.py
    # main()) -- route_events starts empty from the import itself, even though
    # ARCHIVE_CSV above has a row. import_archive_events() still exists and is
    # exercised directly below, just not wired into main().
    events = conn.execute(sa.text("SELECT action, route_id FROM route_events")).mappings().all()
    print(events)
    assert len(events) == 0, "archive import should be skipped by main() -- see import_from_sheets.py"

    imp.import_archive_events(engine)
    with engine.connect() as backfill_conn:
        events = backfill_conn.execute(sa.text("SELECT action, route_id FROM route_events")).mappings().all()
    print(events)
    assert len(events) == 1 and events[0]["action"] == "finalizeRoute" and events[0]["route_id"] is not None

print("\n=== Exercising data_access.py ===")
df = da.get_contractors(engine)
assert len(df) == 3
print("get_contractors OK:", list(df.columns))

da.save_route(engine, "New Contractor-09182026-2", "New Contractor", {"comp": 200, "cluster_hash": "xyz"})
routes = da.get_routes(engine, statuses=["sent"])
assert any(r["wo"] == "New Contractor-09182026-2" for r in routes)
print("save_route + get_routes OK:", [r["wo"] for r in routes])

da.archive_route(engine, "New Contractor-09182026-2", {"note": "test archive"})
routes = da.get_routes(engine, statuses=["archived"])
assert any(r["wo"] == "New Contractor-09182026-2" for r in routes)
print("archive_route OK")

changes = da.get_changes_since(engine, 0)
print("get_changes_since OK, events:", [c["action"] for c in changes])
assert len(changes) >= 2

da.save_bundle_map(engine, "Orange", "nwilliams", {"bundles": [["t1", "t2"]]})
loaded = da.load_bundle_map(engine, "Orange", "nwilliams")
assert loaded == {"bundles": [["t1", "t2"]]}
print("save/load_bundle_map OK")

n1 = da.next_wo_suffix(engine, "Test Contractor", __import__("datetime").date(2026, 9, 18))
n2 = da.next_wo_suffix(engine, "Test Contractor", __import__("datetime").date(2026, 9, 18))
print("next_wo_suffix OK:", n1, n2)
assert n2 == n1 + 1

da.set_fn_provider(engine, "FN-Test-1", "Acme Installs")
da.mark_fn_assigned(engine, "FN-Test-1", route_plan_id="rp-123")
with engine.connect() as conn:
    row = conn.execute(sa.text("SELECT provider, status, route_plan_id FROM field_nation_orders WHERE work_order='FN-Test-1'")).mappings().one()
    print(dict(row))
    assert row["provider"] == "Acme Installs" and row["status"] == "assigned" and row["route_plan_id"] == "rp-123"

print("\nALL CHECKS PASSED")
