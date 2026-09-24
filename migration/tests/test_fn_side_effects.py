"""Offline verification of migration/fn_side_effects.py.

Every Onfleet/Monday.com HTTP call is mocked (via unittest.mock.patch on
`requests.get`/`requests.post`/`requests.request`) -- this proves the SHAPE
of every request (endpoints, payloads, retry/backoff behavior, the Monday
"board-corruption guard") matches the ported GAS source, but it has never
been run against a real Onfleet/Monday sandbox. See migration/README.md for
what that means for cutover readiness.

Usage (no database, no live API keys needed):
    python migration/tests/test_fn_side_effects.py
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("ONFLEET_KEY", "test-onfleet-key")

import fn_side_effects as fx  # noqa: E402

def _resp(status=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text or (str(json_body) if json_body is not None else "")
    r.json.return_value = json_body if json_body is not None else {}
    return r

# ---------------------------------------------------------------------------
# onfleet_fetch_with_backoff
# ---------------------------------------------------------------------------

print("=== onfleet_fetch_with_backoff: retries 429 then succeeds ===")
with patch("fn_side_effects.requests.request") as mock_req, patch("fn_side_effects.time.sleep") as mock_sleep:
    mock_req.side_effect = [_resp(429, text="bandwidth quota exceeded"), _resp(200, {"ok": True})]
    resp = fx.onfleet_fetch_with_backoff("get", "https://onfleet.com/api/v2/tasks/abc")
    assert resp.status_code == 200
    assert mock_req.call_count == 2
    assert mock_sleep.call_count == 1
    print("OK -- retried once on 429, then returned the 200")

print("\n=== onfleet_fetch_with_backoff: 401 with 'unauthorized' body is retryable ===")
with patch("fn_side_effects.requests.request") as mock_req, patch("fn_side_effects.time.sleep"):
    mock_req.side_effect = [_resp(401, text="Unauthorized"), _resp(200, {"ok": True})]
    resp = fx.onfleet_fetch_with_backoff("get", "https://onfleet.com/api/v2/tasks/abc")
    assert resp.status_code == 200
    print("OK")

print("\n=== onfleet_fetch_with_backoff: a plain 404 is NOT retried ===")
with patch("fn_side_effects.requests.request") as mock_req:
    mock_req.return_value = _resp(404, text="not found")
    resp = fx.onfleet_fetch_with_backoff("get", "https://onfleet.com/api/v2/tasks/abc")
    assert resp.status_code == 404
    assert mock_req.call_count == 1
    print("OK -- no retry on a genuine 404")

# ---------------------------------------------------------------------------
# create_onfleet_route
# ---------------------------------------------------------------------------

print("\n=== create_onfleet_route: rejects a worker not on an allowed team ===")
with patch("fn_side_effects.requests.get") as mock_get:
    mock_get.side_effect = [
        _resp(200, {"teams": ["t-not-allowed"]}),
        _resp(200, [{"id": "t-not-allowed", "name": "Some Other Team"}]),
    ]
    result = fx.create_onfleet_route("WO-1", ["task1"], "worker1")
    assert result["success"] is False
    assert "not on an allowed Onfleet team" in result["error"]
    print("OK:", result["error"])

print("\n=== create_onfleet_route: succeeds for an allowed team ===")
with patch("fn_side_effects.requests.get") as mock_get, patch("fn_side_effects.requests.post") as mock_post:
    mock_get.side_effect = [
        _resp(200, {"teams": ["t-orange"]}),
        _resp(200, [{"id": "t-orange", "name": "POD: Orange"}]),
    ]
    mock_post.return_value = _resp(200, {"id": "rp-123"})
    result = fx.create_onfleet_route("WO-1", ["task1", "task2"], "worker1")
    assert result == {"success": True, "routeId": "rp-123"}
    post_kwargs = mock_post.call_args.kwargs
    assert post_kwargs["json"]["team"] == "t-orange"
    assert post_kwargs["json"]["tasks"] == ["task1", "task2"]
    print("OK:", result)

# ---------------------------------------------------------------------------
# assign_tasks_to_worker
# ---------------------------------------------------------------------------

print("\n=== assign_tasks_to_worker: matches by phone, assigns both tasks ===")
fx._phone_map_cache["map"] = None  # reset in-process cache between tests
with patch("fn_side_effects.requests.get") as mock_get, patch("fn_side_effects.requests.request") as mock_req:
    mock_get.return_value = _resp(200, [{"id": "worker-9", "phone": "+1 555-123-4567"}])
    mock_req.return_value = _resp(200, {"ok": True})
    result = fx.assign_tasks_to_worker("5551234567", "task1,task2", "WO-1", 100.0, "2026-10-01")
    assert result["success"] is True
    assert result["workerId"] == "worker-9"
    assert result["assignedCount"] == 2
    # 2 tasks x 2 PUTs each (metadata + assignment) = 4 calls
    assert mock_req.call_count == 4
    print("OK:", result)

print("\n=== assign_tasks_to_worker: unknown phone fails cleanly ===")
fx._phone_map_cache["map"] = None
with patch("fn_side_effects.requests.get") as mock_get:
    mock_get.return_value = _resp(200, [{"id": "worker-9", "phone": "5559999999"}])
    result = fx.assign_tasks_to_worker("5551234567", "task1", "WO-1", 100.0, "2026-10-01")
    assert result["success"] is False
    assert "not found in Onfleet" in result["error"]
    print("OK:", result["error"])

# ---------------------------------------------------------------------------
# apply_onfleet_decision (the processDecision port)
# ---------------------------------------------------------------------------

print("\n=== apply_onfleet_decision: decline is a no-op (no Onfleet calls) ===")
with patch("fn_side_effects.requests.get") as mock_get, patch("fn_side_effects.requests.request") as mock_req:
    result = fx.apply_onfleet_decision(decision="decline", task_ids="", wo="WO-1", phone="", comp=0, due="")
    assert result["onfleetSuccess"] is True
    assert mock_get.call_count == 0 and mock_req.call_count == 0
    print("OK -- no Onfleet calls made for a decline")

print("\n=== apply_onfleet_decision: accept assigns + creates a route ===")
fx._phone_map_cache["map"] = None
with patch("fn_side_effects.requests.get") as mock_get, patch("fn_side_effects.requests.request") as mock_req, patch("fn_side_effects.requests.post") as mock_post:
    mock_get.side_effect = [
        _resp(200, [{"id": "worker-9", "phone": "5551234567"}]),  # workers page 0 (phone map)
        _resp(200, []),  # workers page 1 -- empty, stops the phone-map pagination loop
        _resp(200, {"teams": ["t-orange"]}),  # worker lookup for route creation
        _resp(200, [{"id": "t-orange", "name": "POD: Orange"}]),  # teams lookup
    ]
    mock_req.return_value = _resp(200, {"ok": True})
    mock_post.return_value = _resp(200, {"id": "rp-999"})
    result = fx.apply_onfleet_decision(decision="accept", task_ids="task1,task2", wo="WO-1", phone="5551234567", comp=200.0, due="2026-10-01")
    assert result["onfleetSuccess"] is True
    assert result["routeSuccess"] is True
    assert result["route_incomplete"] is False
    print("OK:", result)

# ---------------------------------------------------------------------------
# Monday.com: the board-corruption guard
# ---------------------------------------------------------------------------

print("\n=== _monday_group_filter: default 3-group allow-list when unset ===")
os.environ.pop("MONDAY_GROUP_FILTER", None)
os.environ.pop("MONDAY_GROUP_FILTER_ALLOW_WILDCARD", None)
assert fx._monday_group_filter() == fx._MONDAY_DEFAULT_GROUPS
print("OK")

print("\n=== _monday_group_filter: '*' alone is IGNORED, falls back to default ===")
os.environ["MONDAY_GROUP_FILTER"] = "*"
assert fx._monday_group_filter() == fx._MONDAY_DEFAULT_GROUPS
print("OK -- wildcard without the second confirmation property is a no-op")

print("\n=== _monday_group_filter: '*' + ALLOW_WILDCARD=yes disables the filter ===")
os.environ["MONDAY_GROUP_FILTER_ALLOW_WILDCARD"] = "yes"
assert fx._monday_group_filter() is None
print("OK -- guard correctly disabled only with BOTH properties set")
os.environ.pop("MONDAY_GROUP_FILTER", None)
os.environ.pop("MONDAY_GROUP_FILTER_ALLOW_WILDCARD", None)

print("\n=== sync_monday_for_stops: skipped cleanly when MONDAY_API_TOKEN unset ===")
os.environ.pop("MONDAY_API_TOKEN", None)
result = fx.sync_monday_for_stops("100 Home St, Chicago, IL|200 Main St, Chicago, IL|100 Home St, Chicago, IL", "WO-1", "Acme Installs")
assert result["skipped"] == "Monday.com sync disabled 2026-09-21 (Terraboost no longer uses Monday)"
print("OK:", result)

print("\n=== sync_monday_for_stops: 2026-09-21 hard kill switch -- stays skipped even WITH a valid token ===")
# Terraboost doesn't use Monday.com anymore (Nick, 2026-09-21). This is now a
# regression guard: sync_monday_for_stops must stay disabled unconditionally,
# not just "when MONDAY_API_TOKEN happens to be unset" -- a token getting set
# again by accident (e.g. copied into Railway from an old .env) must NOT
# silently revive real Monday.com writes. No mocked HTTP here on purpose --
# if this ever calls out to requests.post, the missing mock will raise and
# fail the test instead of hitting the real Monday API.
os.environ["MONDAY_API_TOKEN"] = "test-token"
result = fx.sync_monday_for_stops("100 Home St, Chicago, IL|200 Main St, Chicago, IL|100 Home St, Chicago, IL", "WO-1", "Acme Installs")
assert result["skipped"] == "Monday.com sync disabled 2026-09-21 (Terraboost no longer uses Monday)", result
assert result["matches"] == 0 and result["instUpdates"] == 0 and result["woUpdates"] == 0
print("OK -- still disabled with a token set:", result)
os.environ.pop("MONDAY_API_TOKEN", None)

print("\n=== _monday_mutation_ok: HTTP 200 with a GraphQL errors array is NOT ok ===")
resp_with_errors = _resp(200, {"errors": [{"message": "ColumnValueException"}]})
assert fx._monday_mutation_ok(resp_with_errors) is False
resp_clean = _resp(200, {"data": {"change_simple_column_value": {"id": "1"}}})
assert fx._monday_mutation_ok(resp_clean) is True
print("OK -- HTTP-200-but-failed mutations are correctly rejected")

print("\nALL CHECKS PASSED")
