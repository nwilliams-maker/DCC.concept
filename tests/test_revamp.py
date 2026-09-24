"""Focused checks for the new distance filter and bulk FN handoff."""

import ast
import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def load_functions(filename, names, extra=None):
    tree = ast.parse((ROOT / filename).read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in names]
    scope = dict(extra or {})
    exec(compile(ast.Module(body=functions, type_ignores=[]), filename, "exec"), scope)
    return scope


class RevampTests(unittest.TestCase):
    def test_nearest_active_contractor_and_over_50_miles(self):
        scope = load_functions("revamp_workspace.py", ["_eligible_ics", "_nearest_ic"])
        contractors = pd.DataFrame([
            {"name": "Inactive Nearby", "ic list": "INACTIVE", "lat": 0, "lng": 0},
            {"name": "Active Farther", "ic list": "ACTIVE", "lat": 60, "lng": 0},
            {"name": "Missing Position", "ic list": "ACTIVE", "lat": None, "lng": None},
        ])
        eligible = scope["_eligible_ics"](contractors)
        nearest = scope["_nearest_ic"]({"center": (0, 0)}, eligible,
                                       lambda a, b, c, d: abs(a - c) + abs(b - d))
        self.assertEqual(nearest, ("Active Farther", 60))

    def test_exactly_50_is_ready_and_over_50_is_flagged(self):
        scope = load_functions("revamp_workspace.py", ["_route_hash", "_route_status"],
                               {"hashlib": hashlib,
                                "st": SimpleNamespace(session_state={})})
        route = {"status": "Ready", "data": [{"id": "t1"}]}
        self.assertEqual(scope["_route_status"](route, {}, 50), "Ready")
        self.assertEqual(scope["_route_status"](route, {}, 50.1), "Flagged")
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "field_nation"}}, 50.1), "Field Nation")
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "sent"}}, 50.1), "Sent")
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "declined"}}, 50.1), "Declined")
        scope["st"].session_state["route_state_" + scope["_route_hash"](route)] = "email_sent"
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "accepted"}}, 50.1), "Accepted")

    def test_bulk_fn_retries_skip_existing_and_keep_route_stops(self):
        stored = {}
        moved = []

        class Conn:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, query, params):
                return SimpleNamespace(scalar=lambda: stored.get(params["route_hash"]))

        class Engine:
            def connect(self): return Conn()

        def save(engine, wo, payload):
            stored[payload["cluster_hash"]] = wo
            self.assertEqual(json.loads(payload["stopData"])[0]["venue"], "Example Venue")
            return {"success": True}

        def move(ids, team, **kwargs):
            moved.append((ids, team, kwargs["fn_worker_id"]))

        scope = load_functions("revamp_bulk_fn.py", ["cluster_hash", "_payload", "bulk_assign"],
                               {"hashlib": hashlib, "json": json, "datetime": datetime,
                                "sa": SimpleNamespace(text=lambda s: s),
                                "data_access": SimpleNamespace(save_to_field_nation=save)})
        route = {"city": "Example", "state": "IL", "stops": 1,
                 "center": (41, -87), "data": [{"id": "task-1", "full": "1 Main St",
                 "venue_name": "Example Venue", "task_type": "New Ad"}]}
        selected = [("Blue", route)]
        first = scope["bulk_assign"](Engine(), selected, "2026-10-10", move, "team", "worker")
        second = scope["bulk_assign"](Engine(), selected, "2026-10-10", move, "team", "worker")
        self.assertEqual(len(first[0]), 1)
        self.assertEqual(len(second[1]), 1)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0], (["task-1"], "team", "worker"))

    def test_bulk_fn_refuses_missing_worker(self):
        scope = load_functions("revamp_bulk_fn.py", ["bulk_assign"])
        saved, skipped, errors = scope["bulk_assign"](object(), [], "2026-10-10", None,
                                                       fn_team_id="team")
        self.assertFalse(saved or skipped)
        self.assertEqual(len(errors), 1)

    def test_field_nation_stages_and_csv_rebuild_from_saved_route(self):
        scope = load_functions("revamp_workspace.py", ["_fn_stage", "_fn_csv_route"])
        h = "saved-hash"
        self.assertEqual(scope["_fn_stage"](h, {}, {}), "Pending")
        self.assertEqual(scope["_fn_stage"](h, {h: "posted"}, {}), "Posted")
        self.assertEqual(scope["_fn_stage"](h, {h: "posted"}, {h: "Alex"}), "Assigned")
        ghost = {"hash": h, "task_ids": ["one"]}
        calls = []
        def rebuild(saved, skip_geocode=False):
            calls.append((saved, skip_geocode))
            return {"data": [{"id": "one", "full": "123 Main St"}], "city": "Chicago"}
        route = scope["_fn_csv_route"]({"_is_ghost": True, "_ghost_record": ghost}, h, rebuild)
        self.assertEqual(route["_cluster_hash"], h)
        self.assertEqual(route["data"][0]["full"], "123 Main St")
        self.assertEqual(calls, [(ghost, True)])

    def test_onfleet_route_plan_rename_failure_is_visible(self):
        import time
        calls = []
        def request(method, url, json_body=None):
            calls.append((method, url, json_body))
            if method == "get":
                return SimpleNamespace(status_code=200, json=lambda: {"routePlan": "plan-1", "worker": None})
            return SimpleNamespace(status_code=403)
        scope = load_functions("migration/fn_side_effects.py", ["sync_onfleet_for_fn_assignment"],
                               {"Any": Any, "time": time, "_FA_BUDGET_S": 300, "ONFLEET_BASE": "https://onfleet.example",
                                "_onfleet_auth_header": lambda: {}, "onfleet_fetch_with_backoff": request})
        result = scope["sync_onfleet_for_fn_assignment"](["task-1"], "FN-Alex-9/24", "Alex")
        self.assertTrue(result["partial"])
        self.assertIn("Route Plan Name", result["partialReason"])
        self.assertIn(("put", "https://onfleet.example/routePlans/plan-1", {"name": "FN-Alex-9/24"}), calls)

    def test_state_toggle_keeps_view_and_fn_selection_updates_actions(self):
        from streamlit.testing.v1 import AppTest
        source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import pandas as pd
from revamp_workspace import render_workspace
st.session_state.setdefault("clusters_Blue", [
    {{"city":"Chicago","state":"IL","stops":1,"data":[{{"id":"ready-1","full":"101 Main St"}}]}},
    {{"city":"Detroit","state":"MI","stops":1,"data":[{{"id":"ready-2","full":"102 Main St"}}]}},
    {{"city":"Madison","state":"WI","stops":1,"data":[{{"id":"fn-1","full":"103 Main St, Madison, WI 53703"}}]}},
])
st.session_state.setdefault("ic_df", pd.DataFrame())
def records():
    return {{"fn-1":{{"status":"field_nation"}}}}, {{"Blue":[],"_fn_posted":{{}},"_fn_provider":{{}}}}, set(), {{}}
records.clear = lambda: None
render_workspace(lambda pod: pod == "Blue", lambda pod: None,
    lambda i, route, pod: st.write("Detail: " + route["city"]),
    lambda *args: 0, object(), lambda *args: None, records)
'''
        app = AppTest.from_string(source).run()
        app.radio(key="revamp_status").set_value("Ready").run()
        app.button(key="revamp_state_toggle_Ready__MI").click().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Ready")
        self.assertEqual(app.query_params["view"], ["Ready"])
        detroit = next(button for button in app.button if button.label.startswith("Detroit"))
        detroit.click().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Ready")
        self.assertFalse(app.exception)
        app.radio(key="revamp_status").set_value("Field Nation").run()
        selected = next(checkbox for checkbox in app.checkbox if checkbox.key.startswith("revamp_fn_"))
        selected.check().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Field Nation")
        self.assertEqual(app.button(key="revamp_fn_posted").label, "Mark 1 Posted")
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
