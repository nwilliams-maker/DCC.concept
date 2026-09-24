"""Offline, DB-backed verification of get_sent_records_from_db() (2026-09-22)
-- the Postgres equivalent of tactical_workspace_master_rw.py's
_cached_fetch_sent_records_from_sheet(). This is read-side infrastructure
only; nothing in the live app calls it yet (see the big docstring on top of
that function in migration/data_access.py for why, and for the known gaps
versus the sheet path that this test file also exercises/documents).

Seeds real local Postgres through the SAME write-path functions the live app
already dual-writes through (save_route, mirror_save_to_field_nation,
mirror_mark_fn_assigned_by_cluster_hash, archive_route, ...) rather than
inserting rows directly, so the fixture data is realistic and exercises the
write side too, not just a hand-crafted shape the read side happens to like.

Usage:
    createdb dcc_test2
    psql dcc_test2 -f migration/schema.sql
    export DATABASE_URL="postgresql://localhost/dcc_test2"
    python migration/tests/test_get_sent_records_from_db.py
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

# Mirrors tactical_workspace_master_rw.py's POD_CONFIGS / STATE_MAP as of
# 2026-09-21 -- copied rather than imported (see get_sent_records_from_db's
# docstring: data_access.py takes these as parameters so it stays free of
# app/UI dependencies). Keep in sync by hand if the app's pod->state mapping
# changes; a drift here would only affect this test, never production.
POD_CONFIGS = {
    "Blue": {"states": {"AL", "AR", "FL", "IL", "IA", "LA", "MI", "MN", "MS", "MO", "NC", "SC", "WI", "OR", "WA", "NV"}},
    "Green": {"states": {"CO", "DC", "GA", "IN", "KY", "MD", "NJ", "OH", "UT"}},
    "Orange": {"states": {"AK", "AZ", "CA", "HI", "ID"}},
    "Purple": {"states": {"KS", "MT", "NE", "NM", "ND", "OK", "SD", "TN", "TX", "WY"}},
    "Red": {"states": {"CT", "DE", "ME", "MA", "NH", "NY", "PA", "RI", "VT", "VA", "WV"}},
}
STATE_MAP = {"CALIFORNIA": "CA", "TEXAS": "TX", "NEW YORK": "NY", "FLORIDA": "FL"}


def _get():
    return da.get_sent_records_from_db(engine, POD_CONFIGS, STATE_MAP)


# ---------------------------------------------------------------------------
# 'sent' route -> sent_dict + ghost_routes (pod derived from the locs string)
# ---------------------------------------------------------------------------
print("=== 'sent' route: lands in sent_dict and its pod's ghost_routes ===")
da.save_route(engine, "Sent-Test-1", "Jane IC", {
    "cluster_hash": "hash-sent-1", "wo": "Sent-Test-1", "comp": 150, "due": "2026-10-01",
    "lCnt": 2, "tCnt": 2, "kCnt": 1, "taskIds": "s1,s2",
    "locs": "100 Home St, Los Angeles, CA|200 Main St, Los Angeles, CA|100 Home St, Los Angeles, CA",
    "stopData": "[]",
})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert sent_dict["s1"]["status"] == "sent" and sent_dict["s1"]["name"] == "Jane IC"
assert sent_dict["s2"]["wo"] == "Sent-Test-1"
assert any(g["hash"] == "hash-sent-1" for g in ghost_routes["Orange"]), ghost_routes["Orange"]
assert "s1" in history_db and history_db["s1"][0]["status"] == "sent"
print("OK:", sent_dict["s1"])

# ---------------------------------------------------------------------------
# 'accepted' / 'declined' / 'finalized' routes
# ---------------------------------------------------------------------------
print("\n=== 'accepted' route (TX -> Purple pod) ===")
da.save_route(engine, "Accepted-Test-1", "John IC", {
    "cluster_hash": "hash-accepted-1", "wo": "Accepted-Test-1", "comp": 200, "taskIds": "a1",
    "locs": "1 Home Rd, Austin, TX|2 Main Rd, Austin, TX|1 Home Rd, Austin, TX", "stopData": "[]",
})
da._set_route_status(engine, "Accepted-Test-1", "accepted", "processDecision", {"accept": True})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert sent_dict["a1"]["status"] == "accepted"
assert any(g["hash"] == "hash-accepted-1" for g in ghost_routes["Purple"]), ghost_routes["Purple"]
print("OK:", sent_dict["a1"])

print("\n=== 'declined' route: sent_dict + history_db yes, ghost_routes NO (matches sheet path) ===")
da.save_route(engine, "Declined-Test-1", "Jane IC", {
    "cluster_hash": "hash-declined-1", "wo": "Declined-Test-1", "taskIds": "d1",
    "locs": "1 Home Rd, Austin, TX|2 Main Rd, Austin, TX|1 Home Rd, Austin, TX", "stopData": "[]",
})
da._set_route_status(engine, "Declined-Test-1", "declined", "processDecision", {"accept": False})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert sent_dict["d1"]["status"] == "declined"
assert not any(g["hash"] == "hash-declined-1" for pod in ghost_routes.values() if isinstance(pod, list) for g in pod)
print("OK -- declined route excluded from every pod's ghost_routes")

print("\n=== 'finalized' route (NY -> Red pod) ===")
da.save_route(engine, "Finalized-Test-1", "Jane IC", {
    "cluster_hash": "hash-finalized-1", "wo": "Finalized-Test-1", "taskIds": "fin1",
    "locs": "1 Home Rd, Buffalo, NY|2 Main Rd, Buffalo, NY|1 Home Rd, Buffalo, NY", "stopData": "[]",
})
da._set_route_status(engine, "Finalized-Test-1", "finalized", "finalizeRoute", {})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert sent_dict["fin1"]["status"] == "finalized"
assert any(g["hash"] == "hash-finalized-1" for g in ghost_routes["Red"]), ghost_routes["Red"]
print("OK:", sent_dict["fin1"])

# ---------------------------------------------------------------------------
# Field Nation: posted order (field_nation_orders, status='posted')
# ---------------------------------------------------------------------------
print("\n=== FN posted order: status_label 'field_nation', fn_posted_ts/provider hydration ===")
da.mirror_save_to_field_nation(engine, "FN-Test-1", {
    "cluster_hash": "hash-fn-1", "icn": "Field Nation", "wo": "FN-Test-1", "taskIds": "fn1",
    "locs": "1 Home Rd, Miami, FL|2 Main Rd, Miami, FL|1 Home Rd, Miami, FL", "stopData": "[]",
    "fn_posted_ts": "2026-09-15T12:00:00Z", "fn_provider": "Acme Installs",
})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert sent_dict["fn1"]["status"] == "field_nation" and sent_dict["fn1"]["name"] == "Field Nation"
assert any(g["hash"] == "hash-fn-1" for g in ghost_routes["Blue"]), ghost_routes["Blue"]
assert ghost_routes["_fn_posted"].get("hash-fn-1"), ghost_routes["_fn_posted"]
assert ghost_routes["_fn_provider"].get("hash-fn-1") == "Acme Installs"
print("OK:", ghost_routes["_fn_posted"], ghost_routes["_fn_provider"])

# ---------------------------------------------------------------------------
# Field Nation: assigned order moves OUT of field_nation_orders' "posted"
# set and becomes a real 'accepted' route (mirrors GAS moving the sheet row
# into Accepted) -- and payload["fn_provider"] (this session's fix to
# mark_fn_assigned) is what lets fn_provider_dict get populated here.
# ---------------------------------------------------------------------------
print("\n=== FN assigned order: no longer 'field_nation', now an 'accepted' route with fn_provider ===")
da.mirror_save_to_field_nation(engine, "FN-Test-2", {
    "cluster_hash": "hash-fn-2", "icn": "Field Nation", "wo": "FN-Test-2", "taskIds": "fn2",
    "locs": "1 Home Rd, Miami, FL|2 Main Rd, Miami, FL|1 Home Rd, Miami, FL", "stopData": "[]",
})
da.mirror_set_fn_provider_by_cluster_hash(engine, "hash-fn-2", "Beta Installs")
result = da.mirror_mark_fn_assigned_by_cluster_hash(engine, "hash-fn-2")
assert result["success"] is True, result
new_wo = result["wo"]
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert "fn2" not in {tid: None for tid, v in sent_dict.items() if v["status"] == "field_nation"} or sent_dict["fn2"]["status"] != "field_nation"
assert sent_dict["fn2"]["status"] == "accepted", sent_dict["fn2"]
assert ghost_routes["_fn_provider"].get("hash-fn-2") == "Beta Installs", ghost_routes["_fn_provider"]
print(f"OK -- moved to accepted route {new_wo!r}, fn_provider carried through:", ghost_routes["_fn_provider"]["hash-fn-2"])

# ---------------------------------------------------------------------------
# Archive history: Revoked / Re-Routed / Ghost Archived, via the exact
# {"action_label": ..., "ic_name": ...} shape background_sheet_move's
# dual-write actually sends (tactical_workspace_master_rw.py line ~2150).
# ---------------------------------------------------------------------------
print("\n=== archive_route: Revoked history event + archived_wos ===")
da.save_route(engine, "Archive-Test-1", "Jane IC", {
    "cluster_hash": "hash-archive-1", "wo": "Archive-Test-1", "taskIds": "arc1",
    "locs": "1 Home Rd, Los Angeles, CA|2 Main Rd, Los Angeles, CA|1 Home Rd, Los Angeles, CA", "stopData": "[]",
})
da.archive_route(engine, "Archive-Test-1", {"action_label": "Revoked", "ic_name": "Jane IC"})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert "Archive-Test-1" in archived_wos, archived_wos
assert any(e["status"] == "revoked" and e["name"] == "Jane IC" for e in history_db.get("arc1", [])), history_db.get("arc1")
assert not any(g["hash"] == "hash-archive-1" for pod in ghost_routes.values() if isinstance(pod, list) for g in pod), \
    "archived routes must not appear in ghost_routes, matching the sheet path"
print("OK:", history_db["arc1"])

print("\n=== archive_route: Re-Routed and Ghost Archived map to the right history status ===")
da.save_route(engine, "Archive-Test-2", "Jane IC", {"cluster_hash": "hash-archive-2", "wo": "Archive-Test-2", "taskIds": "arc2"})
da.archive_route(engine, "Archive-Test-2", {"action_label": "Re-Routed", "ic_name": "Jane IC"})
da.save_route(engine, "Archive-Test-3", "Jane IC", {"cluster_hash": "hash-archive-3", "wo": "Archive-Test-3", "taskIds": "arc3"})
da.archive_route(engine, "Archive-Test-3", {"action_label": "Ghost Archived", "ic_name": "G. Dispatcher"})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert history_db["arc2"][0]["status"] == "re-routed"
assert history_db["arc3"][0]["status"] == "ghost-archived" and history_db["arc3"][0]["name"] == "G. Dispatcher"
print("OK:", history_db["arc2"][0]["status"], history_db["arc3"][0]["status"])

print("\n=== Known gap (documented in data_access.py): a 'Ghost Archived' action_label with NO")
print("    matching routes row produces NO route_events row at all (INSERT...SELECT ... WHERE wo=:wo")
print("    matches zero rows), so it cannot appear in archived_wos/history_db here. Confirming that")
print("    behavior rather than silently relying on it: ===")
da.archive_route(engine, "No-Such-Route-Ever-Saved", {"action_label": "Ghost Archived", "ic_name": "G. Dispatcher"})
with engine.connect() as conn:
    _cnt = conn.execute(sa.text("SELECT COUNT(*) FROM route_events WHERE action = 'archiveRoute'")).scalar()
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert "No-Such-Route-Ever-Saved" not in archived_wos
print(f"OK -- confirmed gap: {_cnt} archiveRoute events total, none for the never-saved wo")

# ---------------------------------------------------------------------------
# Digital ghost detection via jobOnly trigger words
# ---------------------------------------------------------------------------
print("\n=== jobOnly trigger word routes a 'sent' route to Global_Digital regardless of state ===")
da.save_route(engine, "Digital-Test-1", "Jane IC", {
    "cluster_hash": "hash-digital-1", "wo": "Digital-Test-1", "taskIds": "dig1",
    "locs": "1 Home Rd, Los Angeles, CA|2 Main Rd, Los Angeles, CA|1 Home Rd, Los Angeles, CA",
    "jobOnly": "service call - offline kiosk", "stopData": "[]",
})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert any(g["hash"] == "hash-digital-1" for g in ghost_routes["Global_Digital"]), ghost_routes["Global_Digital"]
assert not any(g["hash"] == "hash-digital-1" for g in ghost_routes["Orange"])
print("OK -- classified Global_Digital instead of Orange despite the CA address")

# ---------------------------------------------------------------------------
# Unrecognized state -> pod_name stays UNKNOWN -> no ghost entry, but
# sent_dict/history_db are still populated (matches the sheet path's
# `if pod_name != "UNKNOWN":` gate).
# ---------------------------------------------------------------------------
print("\n=== Unrecognized state: no ghost entry anywhere, but sent_dict/history_db still populated ===")
da.save_route(engine, "Unknown-Test-1", "Jane IC", {
    "cluster_hash": "hash-unknown-1", "wo": "Unknown-Test-1", "taskIds": "unk1",
    "locs": "1 Home Rd, Somewhere, ZZ|2 Main Rd, Somewhere, ZZ|1 Home Rd, Somewhere, ZZ", "stopData": "[]",
})
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert sent_dict["unk1"]["status"] == "sent"
assert not any(g["hash"] == "hash-unknown-1" for pod in ghost_routes.values() if isinstance(pod, list) for g in pod)
print("OK -- sent_dict/history_db populated, no pod claimed it")

# ---------------------------------------------------------------------------
# cutoff_date filtering
# ---------------------------------------------------------------------------
print("\n=== cutoff_date excludes routes created before it ===")
da.save_route(engine, "OldCutoff-Test-1", "Jane IC", {"cluster_hash": "hash-old-1", "wo": "OldCutoff-Test-1", "taskIds": "old1"})
with engine.begin() as conn:
    conn.execute(sa.text("UPDATE routes SET created_at = '2026-01-01' WHERE wo = 'OldCutoff-Test-1'"))
sent_dict, ghost_routes, archived_wos, history_db = _get()
assert "old1" not in sent_dict, "route created before MIGRATION_CUTOFF_DATE should be filtered out"
print("OK -- pre-cutoff route excluded")

print("\nALL CHECKS PASSED")
