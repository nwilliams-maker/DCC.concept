"""Focused checks for the new distance filter and bulk FN handoff."""

import ast
import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
