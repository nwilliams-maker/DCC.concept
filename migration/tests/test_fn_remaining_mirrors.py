"""Offline, DB-backed verification of the 2026-09-21 mirror functions for the
rest of the Field Nation family: mirror_remove_field_nation_by_cluster_hash(),
mirror_mark_fn_posted_by_cluster_hash(), mirror_set_fn_provider_by_cluster_hash(),
and mirror_set_fn_route_plan_id_by_cluster_hash().

Unlike save_to_field_nation()/mark_fn_assigned() (see
test_fn_mirror_writes.py), the underlying functions these wrap --
remove_field_nation(), mark_fn_posted(), set_fn_provider(),
set_fn_route_plan_id() -- never called into fn_side_effects.py to begin
with (removeFieldNation/markFNPosted/setFnProvider/setFnRoutePlanId have no
Onfleet/Monday side effect in the live GAS either), so there's no
mirror_only flag or contrast test needed here. What these mirrors add is
purely the cluster_hash -> work_order resolution the live app's call sites
need (they only have cluster_hash in scope, same as
mirror_mark_fn_assigned_by_cluster_hash before them) -- this test proves
that resolution is correct and that each mirror produces the same DB end
state the direct (work_order-keyed) function would.

Usage:
    createdb dcc_test2
    psql dcc_test2 -f migration/schema.sql
    export DATABASE_URL="postgresql://localhost/dcc_test2"
    python migration/tests/test_fn_remaining_mirrors.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlalchemy as sa

import data_access as da  # noqa: E402

DATABASE_URL = os.environ["DATABASE_URL"]
engine = sa.create_engine(DATABASE_URL)

with engine.begin() as conn:
    conn.execute(sa.text("DELETE FROM route_events"))
    conn.execute(sa.text("DELETE FROM routes"))
    conn.execute(sa.text("DELETE FROM field_nation_orders"))

# ---------------------------------------------------------------------------
# Seed a posted FN order the same way mirror_save_to_field_nation() would
# ---------------------------------------------------------------------------
seed_payload = {
    "wo": "FN-Remaining-1", "cluster_hash": "hash-remaining-1", "taskIds": "task1",
    "address": "100 Home St, Chicago, IL",
    "locs": "100 Home St, Chicago, IL|200 Main St, Chicago, IL|100 Home St, Chicago, IL",
}
da.mirror_save_to_field_nation(engine, "FN-Remaining-1", seed_payload)

# ---------------------------------------------------------------------------
# mirror_set_fn_provider_by_cluster_hash
# ---------------------------------------------------------------------------
print("=== mirror_set_fn_provider_by_cluster_hash: resolves work_order and sets provider ===")
result = da.mirror_set_fn_provider_by_cluster_hash(engine, "hash-remaining-1", "Acme Installs")
assert result["success"] is True
assert result["work_order"] == "FN-Remaining-1"
with engine.connect() as conn:
    row = conn.execute(
        sa.text("SELECT provider, payload FROM field_nation_orders WHERE work_order = 'FN-Remaining-1'")
    ).mappings().one()
    assert row["provider"] == "Acme Installs"
    payload = row["payload"] if isinstance(row["payload"], dict) else __import__("json").loads(row["payload"])
    assert payload.get("fn_provider") == "Acme Installs"
print("OK:", result)

# ---------------------------------------------------------------------------
# mirror_set_fn_route_plan_id_by_cluster_hash
# ---------------------------------------------------------------------------
print("\n=== mirror_set_fn_route_plan_id_by_cluster_hash: resolves work_order and sets route_plan_id ===")
result = da.mirror_set_fn_route_plan_id_by_cluster_hash(engine, "hash-remaining-1", "rp_123")
assert result["success"] is True
with engine.connect() as conn:
    rpid = conn.execute(
        sa.text("SELECT route_plan_id FROM field_nation_orders WHERE work_order = 'FN-Remaining-1'")
    ).scalar()
    assert rpid == "rp_123"
print("OK:", result)

# ---------------------------------------------------------------------------
# mirror_mark_fn_posted_by_cluster_hash
# ---------------------------------------------------------------------------
print("\n=== mirror_mark_fn_posted_by_cluster_hash: resolves work_order and marks posted ===")
result = da.mirror_mark_fn_posted_by_cluster_hash(engine, "hash-remaining-1")
assert result["success"] is True
with engine.connect() as conn:
    status = conn.execute(
        sa.text("SELECT status FROM field_nation_orders WHERE work_order = 'FN-Remaining-1'")
    ).scalar()
    assert status == "posted"
print("OK:", result)

# ---------------------------------------------------------------------------
# mirror_remove_field_nation_by_cluster_hash
# ---------------------------------------------------------------------------
print("\n=== mirror_remove_field_nation_by_cluster_hash: resolves work_order and deletes the row ===")
result = da.mirror_remove_field_nation_by_cluster_hash(engine, "hash-remaining-1")
assert result["success"] is True
with engine.connect() as conn:
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM field_nation_orders WHERE work_order = 'FN-Remaining-1'")
    ).scalar()
    assert count == 0
print("OK:", result)

# ---------------------------------------------------------------------------
# Unknown cluster_hash: every mirror no-ops cleanly, never raises
# ---------------------------------------------------------------------------
print("\n=== unknown cluster_hash: every mirror skips cleanly, never raises ===")
for fn, args in [
    (da.mirror_set_fn_provider_by_cluster_hash, ("hash-does-not-exist", "Someone")),
    (da.mirror_set_fn_route_plan_id_by_cluster_hash, ("hash-does-not-exist", "rp_x")),
    (da.mirror_mark_fn_posted_by_cluster_hash, ("hash-does-not-exist",)),
    (da.mirror_remove_field_nation_by_cluster_hash, ("hash-does-not-exist",)),
]:
    r = fn(engine, *args)
    assert r["success"] is False
    assert "no field_nation_orders row found" in r["skipped"]
    print(f"OK: {fn.__name__} ->", r)

# ---------------------------------------------------------------------------
# Re-seed and prove mirror_mark_fn_posted_by_cluster_hash resolves correctly
# even after set_fn_provider/set_fn_route_plan_id have modified the row --
# the cluster_hash stays in the JSON payload throughout (never overwritten
# by these updates), so lookup still works after other mirrors have run.
# ---------------------------------------------------------------------------
print("\n=== cluster_hash lookup survives other mirrors having modified the row ===")
da.mirror_save_to_field_nation(engine, "FN-Remaining-2", {
    "wo": "FN-Remaining-2", "cluster_hash": "hash-remaining-2", "taskIds": "task2",
    "address": "300 Home St, Chicago, IL",
})
da.mirror_set_fn_provider_by_cluster_hash(engine, "hash-remaining-2", "Beta Installs")
da.mirror_set_fn_route_plan_id_by_cluster_hash(engine, "hash-remaining-2", "rp_456")
result = da.mirror_mark_fn_posted_by_cluster_hash(engine, "hash-remaining-2")
assert result["success"] is True
assert result["work_order"] == "FN-Remaining-2"
with engine.connect() as conn:
    row = conn.execute(
        sa.text("SELECT status, provider, route_plan_id FROM field_nation_orders WHERE work_order = 'FN-Remaining-2'")
    ).mappings().one()
    assert row["status"] == "posted"
    assert row["provider"] == "Beta Installs"
    assert row["route_plan_id"] == "rp_456"
print("OK -- all three mirrored fields present on the same row:", dict(row))

print("\nALL CHECKS PASSED")
