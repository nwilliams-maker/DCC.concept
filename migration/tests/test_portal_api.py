"""Offline, DB-backed verification of migration/portal_api.py -- the Step 5
portal endpoint. Runs against a real (throwaway) Postgres like the other
migration tests, exercises the FastAPI app directly via Starlette's
TestClient (no network/port needed), and mocks every Onfleet/Monday.com HTTP
call so it never touches a live API.

This test specifically checks the request/response SHAPES against what
docs/portal-dcc-rw.html actually sends/expects (see that file's window.onload
and submitFinalResponse) -- not just that data_access.process_decision()
works, which migration/tests/test_process_decision_and_fn_flow.py already
covers.

Usage:
    createdb dcc_test3
    psql dcc_test3 -f migration/schema.sql
    export DATABASE_URL="postgresql://localhost/dcc_test3"
    pip install fastapi httpx
    python migration/tests/test_portal_api.py
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["ONFLEET_KEY"] = "test-onfleet-key"
os.environ.pop("MONDAY_API_TOKEN", None)

import sqlalchemy as sa
from fastapi.testclient import TestClient

import data_access as da
import fn_side_effects as fx
import portal_api

engine = portal_api.engine
client = TestClient(portal_api.app)


def _resp(status=200, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.text = str(json_body or "")
    r.json.return_value = json_body if json_body is not None else {}
    return r


with engine.begin() as conn:
    conn.execute(sa.text("DELETE FROM route_events"))
    conn.execute(sa.text("DELETE FROM routes"))

# ---------------------------------------------------------------------------
# GET ?action=getRoute -- matches portal-dcc-rw.html's window.onload fetch
# ---------------------------------------------------------------------------
print("=== GET getRoute: unknown routeId returns {error: ...}, not a 404/500 ===")
resp = client.get("/", params={"action": "getRoute", "routeId": "NOPE-1"})
assert resp.status_code == 200, resp.status_code
body = resp.json()
assert "error" in body and "payload" not in body, body
print("OK:", body)

print("\n=== GET getRoute: known route returns {payload: {...}} with the exact fields the portal reads ===")
da.save_route(engine, "Portal-Test-1", "Jane IC", {
    "cluster_hash": "hash-portal-1", "icn": "Jane IC", "ice": "jane@example.com",
    "wo": "Portal-Test-1", "due": "2026-10-01", "comp": 275.5, "lCnt": 2, "mi": 12.3, "time": "1h 30m",
    "phone": "5551234567", "taskIds": "task1,task2", "digitalTaskIds": "", "tCnt": 2,
    "kCnt": 1, "rCnt": 0, "dCnt": 0, "stopOrder": "task1,task2",
    "locs": "100 Home St, Chicago, IL|200 Main St, Chicago, IL|100 Home St, Chicago, IL",
    "stopData": "[]",
})
resp = client.get("/", params={"action": "getRoute", "routeId": "Portal-Test-1"})
assert resp.status_code == 200
body = resp.json()
assert "payload" in body and "error" not in body, body
d = body["payload"]
# Exactly the fields portal-dcc-rw.html's window.onload destructures off `d`.
for key in ("icn", "wo", "due", "lCnt", "tCnt", "kCnt", "rCnt", "dCnt", "time", "mi", "comp", "phone", "taskIds", "stopOrder"):
    assert key in d, f"missing {key!r} in payload: {d}"
assert d["wo"] == "Portal-Test-1" and float(d["comp"]) == 275.5
print("OK -- payload has every field the portal template renders:", {k: d[k] for k in ("wo", "comp", "lCnt", "tCnt")})

print("\n=== GET with a wrong action is rejected, not silently treated as getRoute ===")
resp = client.get("/", params={"action": "bogus", "routeId": "Portal-Test-1"})
assert resp.status_code == 400
print("OK:", resp.json())

# ---------------------------------------------------------------------------
# POST {action: processDecision} -- matches submitFinalResponse's gasPayload
# ---------------------------------------------------------------------------
print("\n=== POST processDecision: accept, mirrors GAS's onfleetSuccess/routeSuccess shape ===")
fx._phone_map_cache["map"] = None  # reset in-process cache between tests
with patch("fn_side_effects.requests.get") as mock_get, \
     patch("fn_side_effects.requests.request") as mock_req, \
     patch("fn_side_effects.requests.post") as mock_post:
    mock_get.side_effect = [
        _resp(200, [{"id": "worker-1", "phone": "5551234567"}]),
        _resp(200, []),
        _resp(200, {"teams": ["t-orange"]}),
        _resp(200, [{"id": "t-orange", "name": "POD: Orange"}]),
    ]
    mock_req.return_value = _resp(200, {"ok": True})
    mock_post.return_value = _resp(200, {"id": "rp-1"})

    resp = client.post("/", json={
        "action": "processDecision",
        "routeId": "Portal-Test-1",
        "decision": "Accepted",  # exact capitalization the portal's radio buttons send
        "signature": "Jane Signed",
        "notes": "",
        "phone": "5551234567",
        "taskIds": "task1,task2",
        "stopOrder": "task1,task2",
        "comp": 275.5,
        "wo": "Portal-Test-1",
    })
    assert resp.status_code == 200
    result = resp.json()
    # These are the exact fields submitFinalResponse() branches on.
    assert result.get("onfleetSuccess") is True, result
    assert result.get("routeSuccess") is True, result
    print("OK:", result)

with engine.connect() as conn:
    row = conn.execute(sa.text("SELECT status FROM routes WHERE wo = 'Portal-Test-1'")).mappings().one()
    assert row["status"] == "accepted"
print("OK -- route flipped to accepted through the actual HTTP layer, not just the Python function")

print("\n=== POST processDecision: decline path ===")
da.save_route(engine, "Portal-Test-2", "Jane IC", {
    "cluster_hash": "hash-portal-2", "comp": 100.0, "due": "2026-10-02",
})
resp = client.post("/", json={
    "action": "processDecision", "routeId": "Portal-Test-2", "decision": "Declined",
    "signature": "", "notes": "Too far", "phone": "", "taskIds": "", "stopOrder": "", "comp": 100.0,
})
assert resp.status_code == 200
result = resp.json()
assert result.get("success") is True
with engine.connect() as conn:
    row = conn.execute(sa.text("SELECT status FROM routes WHERE wo = 'Portal-Test-2'")).mappings().one()
    assert row["status"] == "declined"
print("OK:", result)

print("\n=== POST processDecision: unknown routeId returns {error: ...} (portal shows it, not a raw 500) ===")
resp = client.post("/", json={"action": "processDecision", "routeId": "NOPE-2", "decision": "Accepted"})
assert resp.status_code == 200
result = resp.json()
assert "error" in result, result
print("OK:", result)

print("\n=== POST with a wrong action is rejected ===")
resp = client.post("/", json={"action": "bogus"})
assert resp.status_code == 400
print("OK:", resp.json())

print("\n=== CORS: the configured portal origin is allowed on a preflight ===")
resp = client.options("/", headers={
    "Origin": "https://nwilliams-maker.github.io",
    "Access-Control-Request-Method": "POST",
})
assert resp.status_code in (200, 204), resp.status_code
assert resp.headers.get("access-control-allow-origin") == "https://nwilliams-maker.github.io", dict(resp.headers)
print("OK -- portal's origin is allowed:", resp.headers.get("access-control-allow-origin"))

print("\nALL CHECKS PASSED")
