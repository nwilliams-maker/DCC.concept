"""Offline, DB-backed verification of the 2026-09-21 "mirror-only" dual-write
path for the Field Nation family: mirror_save_to_field_nation(),
mirror_mark_fn_assigned(), and mirror_mark_fn_assigned_by_cluster_hash().

These exist because save_to_field_nation() and mark_fn_assigned() normally
call into fn_side_effects.py (_fx) to replicate GAS's real Onfleet/Monday.com
side effects. That's correct when this module is the ONE place those actions
happen -- but wrong for a dual-write, where GAS has already performed the
real action live and this call only needs to mirror the resulting row into
Postgres. Calling the non-mirror functions from a dual-write would silently
re-run those side effects a second time (a duplicate Monday push, a second
Onfleet routePlan rename/re-PUT pass) -- exactly what migration/README.md's
"Deliberately NOT wired" section (Step 4, 2026-09-20) flagged as the reason
the whole FN family was left out of the first dual-write pass. This test
proves the mirror path never touches fn_side_effects at all (every
_fx.* call is mocked with no return value configured and asserted at
call_count == 0 -- a real call would still "succeed" against a bare
MagicMock, so counting calls is the only way to prove it didn't happen),
while still landing in the exact same Postgres end state the live,
non-mirror path would produce.

Usage:
    createdb dcc_test2
    psql dcc_test2 -f migration/schema.sql
    export DATABASE_URL="postgresql://localhost/dcc_test2"
    python migration/tests/test_fn_mirror_writes.py
"""
import os
import sys
from unittest.mock import patch

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
# mirror_save_to_field_nation: DB row written, fn_side_effects never touched
# ---------------------------------------------------------------------------
print("=== mirror_save_to_field_nation: writes the row, never calls fn_side_effects ===")
mirror_payload = {
    "wo": "FN-Mirror-1", "cluster_hash": "hash-mirror-1", "taskIds": "task1",
    "locs": "100 Home St, Chicago, IL|200 Main St, Chicago, IL|100 Home St, Chicago, IL",
}
with patch("fn_side_effects.push_fn_placeholder_to_monday") as mock_push:
    result = da.mirror_save_to_field_nation(engine, "FN-Mirror-1", mirror_payload)
    assert result["success"] is True
    assert result["monday"]["skipped"] == "mirror-only write -- GAS already performed the real Monday push live"
    assert mock_push.call_count == 0, f"expected fn_side_effects never called, got {mock_push.call_count} call(s)"
print("OK:", result)

with engine.connect() as conn:
    row = conn.execute(
        sa.text("SELECT status FROM field_nation_orders WHERE work_order = 'FN-Mirror-1'")
    ).mappings().one()
    assert row["status"] == "posted"
print("OK -- field_nation_orders row landed with status='posted'")

print("\n=== mirror_save_to_field_nation: retried POST for the same work_order is a no-op ===")
with patch("fn_side_effects.push_fn_placeholder_to_monday") as mock_push:
    result2 = da.mirror_save_to_field_nation(engine, "FN-Mirror-1", mirror_payload)
    assert result2["success"] is True
    assert mock_push.call_count == 0
with engine.connect() as conn:
    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM field_nation_orders WHERE work_order = 'FN-Mirror-1'")
    ).scalar()
    assert count == 1, f"expected exactly 1 row after a retried mirror write, got {count}"
print("OK -- ON CONFLICT DO NOTHING held, no duplicate row")

# ---------------------------------------------------------------------------
# mirror_mark_fn_assigned: same DB move as the live version, fn_side_effects
# never touched
# ---------------------------------------------------------------------------
print("\n=== mirror_mark_fn_assigned: moves the order into an accepted route, never calls fn_side_effects ===")
da.set_fn_provider(engine, "FN-Mirror-1", "Acme Installs")
with patch("fn_side_effects.apply_fn_assigned_side_effects") as mock_side_effects:
    assign_result = da.mirror_mark_fn_assigned(engine, "FN-Mirror-1")
    assert assign_result["success"] is True
    assert assign_result["wo"].startswith("FN-Acme Installs-")
    assert assign_result["partial"] is False
    assert mock_side_effects.call_count == 0, f"expected fn_side_effects never called, got {mock_side_effects.call_count} call(s)"
print("OK:", assign_result)

with engine.connect() as conn:
    fn_row = conn.execute(
        sa.text("SELECT status FROM field_nation_orders WHERE work_order = 'FN-Mirror-1'")
    ).mappings().one()
    assert fn_row["status"] == "assigned"
    route_row = conn.execute(
        sa.text("SELECT status, contractor_name FROM routes WHERE wo = :wo"), {"wo": assign_result["wo"]}
    ).mappings().one()
    assert route_row["status"] == "accepted" and route_row["contractor_name"] == "Field Nation"
print("OK -- field_nation_orders marked assigned AND a matching accepted `routes` row exists, same as the live path")

# ---------------------------------------------------------------------------
# mirror_mark_fn_assigned_by_cluster_hash: the actual entry point the live
# app's three markFNAssigned call sites will use (they only have
# cluster_hash in scope, not the FN work_order)
# ---------------------------------------------------------------------------
print("\n=== mirror_mark_fn_assigned_by_cluster_hash: resolves work_order from cluster_hash, then mirrors ===")
mirror_payload_2 = {
    "wo": "FN-Mirror-2", "cluster_hash": "hash-mirror-2", "taskIds": "task2",
    "locs": "300 Home St, Chicago, IL|400 Main St, Chicago, IL|300 Home St, Chicago, IL",
}
with patch("fn_side_effects.push_fn_placeholder_to_monday"):
    da.mirror_save_to_field_nation(engine, "FN-Mirror-2", mirror_payload_2)
da.set_fn_provider(engine, "FN-Mirror-2", "Beta Installs")

with patch("fn_side_effects.apply_fn_assigned_side_effects") as mock_side_effects_2:
    by_hash_result = da.mirror_mark_fn_assigned_by_cluster_hash(engine, "hash-mirror-2")
    assert by_hash_result["success"] is True
    assert by_hash_result["wo"].startswith("FN-Beta Installs-")
    assert mock_side_effects_2.call_count == 0
print("OK:", by_hash_result)

with engine.connect() as conn:
    route_row_2 = conn.execute(
        sa.text("SELECT status, contractor_name FROM routes WHERE wo = :wo"), {"wo": by_hash_result["wo"]}
    ).mappings().one()
    assert route_row_2["status"] == "accepted" and route_row_2["contractor_name"] == "Field Nation"
print("OK -- cluster_hash correctly resolved to FN-Mirror-2's work_order and mirrored")

print("\n=== mirror_mark_fn_assigned_by_cluster_hash: unknown cluster_hash skips cleanly, never raises ===")
unknown_result = da.mirror_mark_fn_assigned_by_cluster_hash(engine, "hash-does-not-exist")
assert unknown_result["success"] is False
assert "no field_nation_orders row found" in unknown_result["skipped"]
print("OK:", unknown_result)

# ---------------------------------------------------------------------------
# Contrast: the non-mirror functions still call fn_side_effects as before --
# proves mirror_only isn't accidentally the new default
# ---------------------------------------------------------------------------
print("\n=== contrast: save_to_field_nation() WITHOUT mirror_only still calls fn_side_effects ===")
with patch("fn_side_effects.push_fn_placeholder_to_monday") as mock_push_real:
    mock_push_real.return_value = {"debug": [], "matches": 0, "instUpdates": 0, "woUpdates": 0}
    real_result = da.save_to_field_nation(engine, "FN-NonMirror-1", {
        "wo": "FN-NonMirror-1", "cluster_hash": "hash-nonmirror-1", "taskIds": "task3",
        "locs": "500 Home St, Chicago, IL|600 Main St, Chicago, IL|500 Home St, Chicago, IL",
    })
    assert real_result["success"] is True
    assert mock_push_real.call_count == 1, f"expected exactly 1 fn_side_effects call, got {mock_push_real.call_count}"
print("OK -- non-mirror path still runs the real side effect exactly once")

print("\nALL CHECKS PASSED")
