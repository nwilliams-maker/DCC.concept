"""Focused checks for the new distance filter and bulk FN handoff."""

import ast
import hashlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import parse_qs, urlparse, urlencode, urlsplit, urlunsplit, parse_qsl
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
    def test_background_build_survives_waiter_timeout_and_deduplicates(self):
        cache = {}
        started, release = threading.Event(), threading.Event()
        calls = []

        def process(pod, warm_only=False, refresh_tasks=False,
                    _task_download_progress=None, _build_progress=None):
            calls.append((pod, warm_only, refresh_tasks))
            started.set()
            _task_download_progress(120, 2)
            release.wait(3)
            _task_download_progress(120, 2, done=True)
            _build_progress(.7, "Routing 20 remaining tasks")
            cache[pod] = {"clusters": []}
            return True

        class Cached:
            def __init__(self): self.value = None
            def __call__(self, fn):
                def get():
                    if self.value is None: self.value = fn()
                    return self.value
                return get

        scope = load_functions("revamp_workspace.py", ["_pod_load_locks", "_pod_build_jobs", "_background_pod_build", "_build_progress_display"], {
            "st": SimpleNamespace(cache_resource=lambda **kwargs: Cached()),
            "threading": threading, "ThreadPoolExecutor": ThreadPoolExecutor,
            "PODS": ("Orange",), "print": lambda *args, **kwargs: None,
        })
        future = scope["_background_pod_build"]("Orange", process, lambda: cache)
        self.assertTrue(started.wait(2))
        with self.assertRaises(FutureTimeout):
            future.result(timeout=.01)
        value, label = scope["_build_progress_display"]("Orange")
        self.assertGreater(value, 0)
        self.assertIn("120 received", label)
        self.assertIs(scope["_background_pod_build"]("Orange", process, lambda: cache), future)
        refresh = scope["_background_pod_build"]("Orange", process, lambda: cache, refresh=True)
        self.assertIsNot(refresh, future)
        self.assertIs(scope["_background_pod_build"]("Orange", process, lambda: cache, refresh=True), refresh)
        release.set()
        self.assertTrue(future.result(timeout=2))
        self.assertTrue(refresh.result(timeout=2))
        self.assertIn("120 OnFleet tasks downloaded", scope["_build_progress_display"]("Orange")[1])
        self.assertEqual(calls, [("Orange", True, False), ("Orange", True, True)])
        scope["_pod_build_jobs"]()["executor"].shutdown(wait=True)

    def test_outlook_button_restores_saved_draft_or_rebuilds_route_link(self):
        scope = load_functions("revamp_workspace.py", ["_outlook_route_url"], {
            "st": SimpleNamespace(session_state={}), "os": SimpleNamespace(environ={}),
            "urlencode": urlencode, "urlsplit": urlsplit, "urlunsplit": urlunsplit,
            "parse_qsl": parse_qsl,
        })
        fields = {"wo": "WO 24&5", "contractor": "Morgan", "due": "2026-10-10",
                  "ghost": {"contractor_email": "morgan@example.com"}}
        draft = scope["_outlook_route_url"](
            "saved-hash", fields, portal_base_url="https://example.com/request?auth=valid")
        query = parse_qs(urlparse(draft).query)
        self.assertEqual(query["to"], ["morgan@example.com"])
        self.assertIn("route=WO+24%265&v2=true", query["body"][0])
        self.assertIn("auth=valid", query["body"][0])
        scope["st"].session_state["_persisted_outlook_saved-hash"] = "https://outlook.example/exact-draft"
        self.assertEqual(scope["_outlook_route_url"]("saved-hash", fields),
                         "https://outlook.example/exact-draft")

    def test_saved_route_fields_use_persisted_dcc_values(self):
        scope = load_functions("revamp_workspace.py", ["_saved_route_fields"])
        route = {"stops": 1, "data": [{"id": "one"}], "comp": 25}
        ghost = {"contractor_name": "Pat Contractor", "wo": "WO-1", "pay": 75,
                 "due": "2026-10-10", "stops": 3, "tasks": 4, "kCnt": 2}
        fields = scope["_saved_route_fields"](route, ghost)
        self.assertEqual((fields["contractor"], fields["wo"], fields["pay"],
                          fields["due"], fields["stops"], fields["tasks"], fields["kiosks"]),
                         ("Pat Contractor", "WO-1", 75, "2026-10-10", 3, 4, 2))

    def test_live_sent_card_uses_saved_wo_pay_and_due(self):
        from streamlit.testing.v1 import AppTest
        source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import pandas as pd
from revamp_workspace import render_workspace
st.session_state.setdefault("ic_df", pd.DataFrame())
st.session_state.setdefault("clusters_Blue", [{{"city":"Chicago", "state":"IL",
    "stops":1, "data":[{{"id":"live-one", "full":"123 Main, Chicago, IL"}}]}}])
def records():
    return {{"live-one":{{"status":"sent", "name":"Jordan", "wo":"WO-LIVE",
        "comp":40, "due":"2026-10-11"}}}}, {{"Blue":[]}}, set(), {{}}
records.clear = lambda: None
helpers = dict(make_venue_details=lambda tasks: "<p>Live venue</p>",
    make_venue_details_ghost=lambda *args, **kwargs: "", venue_section=lambda content: content,
    render_finalization_checklist=lambda *args, **kwargs: None,
    move_to_dispatch=lambda *args, **kwargs: None, is_dispatch_associate=lambda: True)
render_workspace(lambda pod: pod == "Blue", lambda pod: None,
    lambda *args: None, lambda *args: 0, object(), lambda *args: None, records,
    saved_route_helpers=helpers)
'''
        app = AppTest.from_string(source).run()
        app.radio(key="revamp_status").set_value("Sent").run()
        output = " ".join(item.value for item in app.markdown)
        self.assertIn("WO-LIVE", output)
        self.assertIn("$40", output)
        self.assertIn("2026-10-11", output)
        self.assertIn("Live venue", output)
        self.assertTrue(any(item.label == "Open Outlook" for item in app.get("link_button")))
        self.assertFalse(app.exception)

    def test_sent_and_accepted_render_dcc_saved_card_for_selected_route(self):
        from streamlit.testing.v1 import AppTest
        source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import pandas as pd
from revamp_workspace import render_workspace
st.session_state.setdefault("ic_df", pd.DataFrame())
st.session_state.setdefault("clusters_Blue", [])
ghosts = [
    {{"hash":"sent-hash", "status":"sent", "city":"Chicago", "state":"IL",
      "contractor_name":"Morgan", "wo":"WO-SENT", "pay":65, "due":"2026-10-10",
      "stops":1, "tasks":2, "locs":"Home | 100 Main, Chicago, IL | Home",
      "stop_data":[{{"addr":"100 Main, Chicago, IL", "venue":"Site A"}}], "task_ids":["one", "two"]}},
    {{"hash":"accepted-hash", "status":"accepted", "city":"Madison", "state":"WI",
      "contractor_name":"Taylor", "wo":"WO-ACCEPTED", "pay":90, "due":"2026-10-12",
      "stops":1, "tasks":1, "kCnt":1, "locs":"Home | 200 Main, Madison, WI | Home",
      "stop_data":[{{"addr":"200 Main, Madison, WI", "venue":"Site B"}}], "task_ids":["three"]}}
]
def records(): return {{}}, {{"Blue":ghosts}}, set(), {{}}
records.clear = lambda: None
def detail(locs, stop_data=None): return "<p>" + stop_data[0]["venue"] + "</p>"
def checklist(*args, **kwargs): st.write("DCC checklist " + args[0])
helpers = dict(make_venue_details=lambda tasks: "", make_venue_details_ghost=detail,
    venue_section=lambda content: content, render_finalization_checklist=checklist,
    move_to_dispatch=lambda *args, **kwargs: None, is_dispatch_associate=lambda: True)
render_workspace(lambda pod: pod == "Blue", lambda pod: None,
    lambda *args: None, lambda *args: 0, object(), lambda *args: None, records,
    saved_route_helpers=helpers)
'''
        app = AppTest.from_string(source).run()
        app.radio(key="revamp_status").set_value("Sent").run()
        self.assertIn("WO-SENT", " ".join(markdown.value for markdown in app.markdown))
        self.assertIn("Site A", " ".join(markdown.value for markdown in app.markdown))
        self.assertTrue(any(item.label == "Open Outlook" for item in app.get("link_button")))
        app.radio(key="revamp_status").set_value("Accepted").run()
        html_output = " ".join(markdown.value for markdown in app.markdown)
        self.assertIn("WO-ACCEPTED", html_output)
        self.assertIn("Site B", html_output)
        self.assertIn("Total Compensation", html_output)
        self.assertTrue(any("DCC checklist accepted-hash" in item.value for item in app.markdown))
        self.assertTrue(any(item.label == "Open Outlook" for item in app.get("link_button")))
        self.assertFalse(app.exception)

    def test_route_geocodes_first_load_in_parallel_without_changing_result(self):
        cache = {}
        active = [0]
        peak = [0]
        lock = threading.Lock()

        def geocode(address, cache=None):
            if cache is None:
                cache = {}
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(.02)
            result = (float(len(address)), 41.0)
            cache[address] = result
            with lock:
                active[0] -= 1
            return result

        def route_response(url, timeout):
            return SimpleNamespace(json=lambda: {"code": "Ok", "trips": [{"distance": 1609.344,
                "duration": 3600}], "waypoints": [{"waypoint_index": i} for i in range(10)]})

        scope = load_functions("tactical_workspace_master_rw.py", ["get_gmaps"], {
            "time": time, "ThreadPoolExecutor": ThreadPoolExecutor, "MAPBOX_TOKEN": "test",
            "_mapbox_geocode_cache": lambda: cache, "_gmaps_route_cache": lambda: {},
            "_mapbox_geocode": geocode,
            "requests": SimpleNamespace(get=route_response), "_log_err": lambda *args: None,
        })
        result = scope["get_gmaps"]("home", tuple(f"stop-{i}" for i in range(8)))
        self.assertGreater(peak[0], 1)
        self.assertEqual(result[0], 1.0)
        self.assertAlmostEqual(result[1], 1 + 8 * 10 / 60)
        self.assertEqual(result[3], list(range(8)))

    def test_login_loads_one_pod_and_check_new_tasks_refreshes(self):
        from streamlit.testing.v1 import AppTest
        source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import pandas as pd
from revamp_workspace import render_workspace
st.session_state.setdefault("ic_df", pd.DataFrame())
def process(pod, refresh_tasks=False):
    st.session_state.setdefault("load_calls", []).append((pod, refresh_tasks))
    st.session_state["clusters_" + pod] = [{{"city":"Chicago","state":"IL","stops":1,
        "data":[{{"id":"task-" + pod,"full":"101 Main St"}}]}}]
def records():
    return {{}}, {{}}, set(), {{}}
records.clear = lambda: None
render_workspace(lambda pod: pod in ("Blue", "Green"), process,
    lambda *args: None, lambda *args: 0, object(), lambda *args: None, records)
'''
        app = AppTest.from_string(source).run()
        self.assertEqual(app.session_state["load_calls"], [("Blue", False)])
        self.assertFalse(app.exception)
        app.run()
        self.assertEqual(app.session_state["load_calls"], [("Blue", False)])
        app.selectbox(key="revamp_pod").set_value("Green").run()
        self.assertEqual(app.session_state["load_calls"], [("Blue", False), ("Green", False)])
        app.button(key="revamp_sync").click().run()
        self.assertEqual(app.session_state["load_calls"][-1], ("Green", True))
        self.assertEqual(app.button(key="revamp_sync").label, "Check new tasks")
        self.assertFalse(app.exception)

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
        app.button(key="revamp_select_visible").click().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Ready")
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
