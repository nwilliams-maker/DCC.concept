"""Offline, DB-backed verification of the three data_access.py functions that
now call into fn_side_effects.py: process_decision(), mark_fn_assigned(), and
save_to_field_nation(). Runs against a real (throwaway) Postgres like
test_migration.py, but mocks every Onfleet/Monday.com HTTP call so it never
touches a live API or needs a real key.

Usage:
    createdb dcc_test2
    psql dcc_test2 -f migration/schema.sql
    export DATABASE_URL="postgresql://localhost/dcc_test2"
    python migration/tests/test_process_decision_and_fn_flow.py
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlalchemy as sa

import data_access as da  # noqa: E402

DATABASE_URL = os.environ["DATABASE_URL"]
engine = sa.create_engine(DATABASE_URL)

os.environ["ONFLEET_KEY"] = "test-onfleet-key"
os.environ.pop("MONDAY_API_TOKEN", None)  # Monday sync stays skipped -> no network needed for it

def _resp(status=200, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.text = str(json_body or "")
    r.json.return_value = json_body if json_body is not None else {}
    return r

with engine.begin() as conn:
    conn.execute(sa.text("DELETE FROM route_events"))
    conn.execute(sa.text("DELETE FROM routes"))
    conn.execute(sa.text("DELETE FROM field_nation_orders"))

# ---------------------------------------------------------------------------
# process_decision: accept -> Onfleet assign + route create, status flips
# ---------------------------------------------------------------------------
print("=== process_decision: accept with task IDs ===")
da.save_route(engine, "PD-Test-1", "Jane IC", {
    "comp": 150.0, "due": "2026-10-01", "cluster_hash": "hash-pd-1",
    "icn": "Jane IC", "ice": "jane@example.com",
})

with patch("fn_side_effects.requests.get") as mock_get, \
     patch("fn_side_effects.requests.request") as mock_req, \
     patch("fn_side_effects.requests.post") as mock_post:
    mock_get.side_effect = [
        _resp(200, [{"id": "worker-1", "phone": "5551234567"}]),  # phone map page 0
        _resp(200, []),  # phone map page 1 -- empty, stop pagination
        _resp(200, {"teams": ["t-orange"]}),  # worker lookup
        _resp(200, [{"id": "t-orange", "name": "POD: Orange"}]),  # teams lookup
    ]
    mock_req.return_value = _resp(200, {"ok": True})
    mock_post.return_value = _resp(200, {"id": "rp-1"})

    result = da.process_decision(
        engine, "PD-Test-1", decision="accept", signature="Jane Signed", notes="",
        phone="5551234567", task_ids="task1,task2", comp=150.0,
    )
    print(result)
    assert result["success"] is True
    assert result["onfleetSuccess"] is True
    assert result["routeSuccess"] is True

with engine.connect() as conn:
    row = conn.execute(sa.text("SELECT status FROM routes WHERE wo = 'PD-Test-1'")).mappings().one()
    assert row["status"] == "accepted"
    events = conn.execute(sa.text(
        "SELECT re.action, re.payload FROM route_events re JOIN routes r ON r.id = re.route_id WHERE r.wo = 'PD-Test-1'"
    )).mappings().all()
    assert any(e["action"] == "processDecision" for e in events)
print("OK -- route flipped to accepted, Onfleet ran, event logged")

print("\n=== process_decision: idempotent re-submit is a no-op ===")
with patch("fn_side_effects.requests.get") as mock_get, patch("fn_side_effects.requests.request") as mock_req:
    result = da.process_decision(engine, "PD-Test-1", decision="accept", signature="x", notes="", phone="5551234567", task_ids="task1,task2")
    assert result["success"] is True
    assert "Already recorded" in result["onfleetMsg"]
    assert mock_get.call_count == 0 and mock_req.call_count == 0
print("OK -- no Onfleet calls made on a duplicate accept")

print("\n=== process_decision: accepted route cannot flip to declined ===")
result = da.process_decision(engine, "PD-Test-1", decision="decline", signature="x", notes="", phone="")
assert result["success"] is False
assert "already accepted" in result["error"]
print("OK:", result["error"])

# ---------------------------------------------------------------------------
# save_to_field_nation -> mark_fn_assigned, full lifecycle
# ---------------------------------------------------------------------------
print("\n=== save_to_field_nation then mark_fn_assigned (Monday sync disabled 2026-09-21) ===")
fn_payload = {
    "wo": "FN-Placeholder-1", "cluster_hash": "hash-fn-1", "taskIds": "task9",
    "locs": "100 Home St, Chicago, IL|200 Main St, Chicago, IL|100 Home St, Chicago, IL",
}
save_result = da.save_to_field_nation(engine, "FN-Placeholder-1", fn_payload)
assert save_result["success"] is True
assert save_result["monday"]["skipped"] == "Monday.com sync disabled 2026-09-21 (Terraboost no longer uses Monday)"
print("OK:", save_result)

da.set_fn_provider(engine, "FN-Placeholder-1", "Acme Installs")
with engine.connect() as conn:
    row = conn.execute(sa.text("SELECT payload FROM field_nation_orders WHERE work_order = 'FN-Placeholder-1'")).mappings().one()
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert payload.get("fn_provider") == "Acme Installs", payload
print("OK -- set_fn_provider stamped fn_provider into the JSON payload, not just the column")

with patch("fn_side_effects.requests.get") as mock_get, patch("fn_side_effects.requests.request") as mock_req:
    mock_get.return_value = _resp(200, {"routePlan": None})  # task GET during routePlan discovery -- no routePlan found
    mock_req.return_value = _resp(200, {"worker": "worker-1"})
    assign_result = da.mark_fn_assigned(engine, "FN-Placeholder-1")
    print(assign_result)
    assert assign_result["success"] is True
    assert assign_result["wo"].startswith("FN-Acme Installs-")

with engine.connect() as conn:
    fn_row = conn.execute(sa.text("SELECT status FROM field_nation_orders WHERE work_order = 'FN-Placeholder-1'")).mappings().one()
    assert fn_row["status"] == "assigned"
    route_row = conn.execute(sa.text("SELECT status, contractor_name FROM routes WHERE wo = :wo"), {"wo": assign_result["wo"]}).mappings().one()
    assert route_row["status"] == "accepted" and route_row["contractor_name"] == "Field Nation"
print("OK -- field_nation_orders marked assigned AND a matching accepted `routes` row now exists")

print("\nALL CHECKS PASSED")
