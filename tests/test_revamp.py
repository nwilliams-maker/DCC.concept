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
    def test_pending_cvs_routes_appear_only_in_cvs_filter(self):
        scope = load_functions("revamp_workspace.py", ["_entry_matches"],
                               {"st": SimpleNamespace(session_state={"revamp_bulk_Orange:cvs": True}), "_searchable": lambda r: "phoenix"})
        matches = scope["_entry_matches"]
        for state in ("Ready", "Flagged"):
            cvs = ("Orange", {"is_removal": True}, state, "cvs", ("IC", 65))
            regular = ("Orange", {"is_removal": False}, state, "regular", ("IC", 65))
            for view in ("All", "Ready", "Flagged", "Over 50 mi", "Selected"):
                self.assertFalse(matches(cvs, view, ""))
            for view in ("All", state, "Over 50 mi"):
                self.assertTrue(matches(regular, view, ""))
            self.assertTrue(matches(cvs, "CVS Removal", ""))
            self.assertFalse(matches(regular, "CVS Removal", ""))
            self.assertFalse(matches(cvs, "CVS Removal", "missing city"))
        sent = ("Orange", {"is_removal": True}, "Sent", "sent", None)
        self.assertFalse(matches(sent, "Ready", ""))
        self.assertFalse(matches(sent, "CVS Removal", ""))
        self.assertTrue(matches(sent, "Sent", ""))

    def test_saved_card_headings_use_work_order_for_sent_and_other_statuses(self):
        scope = load_functions("revamp_workspace.py", ["_route_list_heading", "_saved_route_fields"],
                               {"st": SimpleNamespace(session_state={})})
        route = {"wo": "Michael Burres-10012026-1", "city": "Manassas", "state": "VA", "data": []}
        for state, icon in (("Sent", "→"), ("Accepted", "✓"), ("Declined", "×"), ("Finalized", "✓")):
            self.assertEqual(scope["_route_list_heading"](route, state, "hash"),
                f"**Michael Burres-10012026-1** - Manassas, VA - {icon} {state}")
        self.assertIsNone(scope["_route_list_heading"](route, "Ready", "hash"))

    def test_search_finds_client_names_in_live_and_saved_routes(self):
        searchable = load_functions("revamp_workspace.py", ["_searchable"])["_searchable"]
        live = {"city": "San Diego", "data": [{"venue_name": "Store 12",
                 "client_company": "Confie Insurance", "task_type": "Kiosk Install",
                 "sio": "SIO-101"}], "contractor_name": "Morgan Lee"}
        saved = {"wo": "WO-123", "_ghost_record": {"stop_data": [
            {"venue": "Store 24", "campaigns": [{"name": "Local Plus Health"}]}]}}
        self.assertIn("confie insurance", searchable(live))
        self.assertIn("morgan lee", searchable(live))
        self.assertIn("kiosk install", searchable(live))
        self.assertIn("sio-101", searchable(live))
        self.assertIn("local plus health", searchable(saved))
        self.assertIn("wo-123", searchable(saved))

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
        self.assertTrue(any(item.label == "Open Outlook" for item in app.button))
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
        self.assertTrue(any(item.label == "Open Outlook" for item in app.button))
        app.radio(key="revamp_status").set_value("Accepted").run()
        html_output = " ".join(markdown.value for markdown in app.markdown)
        self.assertIn("WO-ACCEPTED", html_output)
        self.assertIn("Site B", html_output)
        self.assertIn("Total Compensation", html_output)
        self.assertTrue(any("DCC checklist accepted-hash" in item.value for item in app.markdown))
        self.assertTrue(any(item.label == "Open Outlook" for item in app.button))
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
                "duration": 3600}], "routes": [{"distance": 1609.344, "duration": 3600}], "waypoints": [{"waypoint_index": i} for i in range(10)]})

        scope = load_functions("tactical_workspace_master_rw.py", ["get_gmaps"], {
            "time": time, "ThreadPoolExecutor": ThreadPoolExecutor, "MAPBOX_TOKEN": "test",
            "_mapbox_geocode_cache": lambda: cache, "_gmaps_route_cache": lambda: {},
            "_mapbox_geocode": geocode,
            "requests": SimpleNamespace(get=route_response), "_log_err": lambda *args: None,
            "plan_round_trip": __import__("route_planning").plan_round_trip,
            "task_stop_locations": __import__("route_planning").task_stop_locations,
        })
        result = scope["get_gmaps"]("home", tuple(f"stop-{i}" for i in range(8)))
        self.assertGreater(peak[0], 1)
        self.assertEqual(result[0], 1.0)
        self.assertAlmostEqual(result[1], 1 + 8 * 10 / 60)
        self.assertEqual(result[3], list(range(8)))

    def test_login_loads_one_pod_and_switching_loads_only_new_pod(self):
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
        self.assertFalse(any(button.key == "revamp_sync" for button in app.button))
        app.run()
        self.assertEqual(app.session_state["load_calls"], [("Blue", False), ("Green", False)])
        self.assertFalse(app.exception)

    def test_nearest_active_contractor_and_over_50_miles(self):
        scope = load_functions("revamp_workspace.py", ["_eligible_ics", "_nearest_ic", "_route_center"])
        contractors = pd.DataFrame([
            {"name": "Inactive Nearby", "ic list": "INACTIVE", "lat": 0, "lng": 0},
            {"name": "Active Farther", "ic list": "ACTIVE", "lat": 60, "lng": 0},
            {"name": "Missing Position", "ic list": "ACTIVE", "lat": None, "lng": None},
        ])
        eligible = scope["_eligible_ics"](contractors)
        nearest = scope["_nearest_ic"]({"center": (0, 0)}, eligible,
                                       lambda a, b, c, d: abs(a - c) + abs(b - d))
        self.assertEqual(nearest, ("Active Farther", 60))

    def test_over_50_filter_and_60_mile_flag_are_separate(self):
        scope = load_functions("revamp_workspace.py", ["_route_hash", "_route_status"],
                               {"hashlib": hashlib,
                                "HIGH_RATE_FLAG_THRESHOLD": 25.00,
                                "st": SimpleNamespace(session_state={})})
        route = {"status": "Ready", "data": [{"id": "t1"}]}
        self.assertEqual(scope["_route_status"](route, {}, 50), "Ready")
        self.assertEqual(scope["_route_status"](route, {}, 50.1), "Ready")
        self.assertEqual(scope["_route_status"](route, {}, 60), "Ready")
        self.assertEqual(scope["_route_status"](route, {}, 60.1), "Flagged")
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "field_nation"}}, 50.1), "Field Nation")
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "sent"}}, 50.1), "Sent")
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "declined"}}, 50.1), "Declined")
        scope["st"].session_state["route_state_" + scope["_route_hash"](route)] = "email_sent"
        self.assertEqual(scope["_route_status"](
            route, {"t1": {"status": "accepted"}}, 50.1), "Accepted")

    def test_fresh_unassigned_task_escapes_stale_finalized_flag(self):
        session = {}
        scope = load_functions("revamp_workspace.py", ["_route_hash", "_route_status"],
                               {"hashlib": hashlib, "HIGH_RATE_FLAG_THRESHOLD": 25.00,
                                "st": SimpleNamespace(session_state=session)})
        route = {"data": [{"id": "task", "_onfleet_unassigned": True}]}
        key = "route_state_" + scope["_route_hash"](route)
        for miles, expected in ((10, "Ready"), (61, "Flagged")):
            session[key] = "finalized"
            self.assertEqual(scope["_route_status"](route, {"task": {"status": "finalized"}}, miles), expected)
            self.assertNotIn(key, session)
        for active, expected in (("sent", "Sent"), ("accepted", "Accepted"), ("field_nation", "Field Nation")):
            session[key] = "finalized"
            self.assertEqual(scope["_route_status"](route, {"task": {"status": active}}, 10), expected)

    def test_autocalculated_rate_over_2499_moves_card_to_flagged(self):
        session = {}
        scope = load_functions("revamp_workspace.py", ["_route_hash", "_route_status"],
                               {"hashlib": hashlib,
                                "HIGH_RATE_FLAG_THRESHOLD": 25.00,
                                "st": SimpleNamespace(session_state=session)})
        route = {"status": "Ready", "data": [{"id": "rate-task"}]}
        route_hash = scope["_route_hash"](route)
        auto_key = "_auto_rate_Orange_" + route_hash
        manual_key = "_rate_master_Orange_" + route_hash
        session[auto_key] = 24.99
        session[manual_key] = 24.99
        self.assertEqual(scope["_route_status"](route, {}, 10, "Orange"), "Ready")
        session[manual_key] = 35.00
        self.assertEqual(scope["_route_status"](route, {}, 10, "Orange"), "Ready")
        session[auto_key] = 25.00
        self.assertEqual(scope["_route_status"](route, {}, 10, "Orange"), "Flagged")
        self.assertEqual(scope["_route_status"](
            route, {"rate-task": {"status": "sent"}}, 10, "Orange"), "Sent")
        session["_route_fa_Orange_" + scope["_route_hash"](route)] = True
        self.assertEqual(scope["_route_status"](route, {}, 10, "Orange"), "Ready")

    def test_duplicate_live_route_has_one_selection_key_and_keeps_flag(self):
        dedupe = load_functions("revamp_workspace.py", ["_dedupe_route_entries", "_route_entry_key"])["_dedupe_route_entries"]
        ready = ("Orange", {"city": "Los Angeles"}, "Ready", "same-hash", None)
        flagged = ("Orange", {"city": "Los Angeles"}, "Flagged", "same-hash", None)
        other_pod = ("Blue", {"city": "Chicago"}, "Ready", "same-hash", None)
        routes = dedupe([ready, flagged, ready, other_pod])
        self.assertEqual([(pod, state, route_hash) for pod, _, state, route_hash, _ in routes],
                         [("Orange", "Flagged", "same-hash"),
                         ("Blue", "Ready", "same-hash")])

    def test_duplicate_live_routes_render_one_checkbox(self):
        from streamlit.testing.v1 import AppTest
        source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import pandas as pd
from revamp_workspace import render_workspace
route = {{"city":"Chicago","state":"IL","stops":1,
         "data":[{{"id":"duplicate-task","full":"101 Main St"}}]}}
st.session_state.setdefault("clusters_Blue", [route, dict(route)])
st.session_state.setdefault("ic_df", pd.DataFrame())
def records():
    return {{}}, {{"Blue":[]}}, set(), {{}}
records.clear = lambda: None
render_workspace(lambda pod: pod == "Blue", lambda pod: None,
    lambda i, route, pod: st.write("Detail: " + route["city"]),
    lambda *args: 0, object(), lambda *args: None, records)
'''
        app = AppTest.from_string(source).run()
        app.radio(key="revamp_status").set_value("Routes").run()
        self.assertFalse(app.exception)
        self.assertEqual(len([box for box in app.checkbox
                              if box.key.startswith("revamp_bulk_")]), 1)

    def test_remove_stops_updates_live_cluster_not_rendered_copy(self):
        detach = load_functions("tactical_workspace_master_rw.py", ["_detach_stops_from_cluster"])["_detach_stops_from_cluster"]
        original = {"data": [
            {"id": "one", "full": "A"}, {"id": "two", "full": "B"},
            {"id": "three", "full": "B"}], "stops": 2}
        store = [original, dict(original)]
        rendered_copy = dict(original)
        groups, remaining = detach(store, [t["id"] for t in rendered_copy["data"]], ["B"])
        self.assertEqual([[t["id"] for t in group] for group in groups], [["two", "three"]])
        self.assertEqual(remaining, ["one"])
        self.assertEqual(original["stops"], 1)
        self.assertEqual([t["id"] for t in original["data"]], ["one"])
        self.assertEqual(len(store), 1)
        self.assertEqual(len(rendered_copy["data"]), 3)
        self.assertEqual(detach(store, ["one", "two", "three"], ["B"]), ([], []))

    def test_bulk_fn_retries_skip_existing_and_keep_route_stops(self):
        stored = {}
        moved = []

        class Conn:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, query, params):
                if "route_hash" in params:
                    row = stored.get(params["route_hash"])
                    return SimpleNamespace(mappings=lambda: SimpleNamespace(
                        first=lambda: row))
                if "prefix" in params:
                    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
                return SimpleNamespace(scalar=lambda: next(
                    (row["route_plan_id"] for row in stored.values()
                     if row["work_order"] == params["work_order"]), None))

        class Engine:
            def connect(self): return Conn()

        def save(engine, wo, payload):
            stored[payload["cluster_hash"]] = {"work_order": wo, "route_plan_id": None}
            self.assertEqual(json.loads(payload["stopData"])[0]["venue"], "Example Venue")
            return {"success": True}

        def move(ids, team, **kwargs):
            moved.append((ids, team, kwargs["fn_worker_id"]))
            if len(moved) == 2:
                stored[kwargs["cluster_hash"]]["route_plan_id"] = "plan-1"

        scope = load_functions("revamp_bulk_fn.py", ["cluster_hash", "_payload", "_next_dcc_work_order", "bulk_assign"],
                               {"hashlib": hashlib, "json": json, "datetime": datetime,
                                "sa": SimpleNamespace(text=lambda s: s),
                                "data_access": SimpleNamespace(save_to_field_nation=save)})
        route = {"city": "Example", "state": "IL", "stops": 1,
                 "center": (41, -87), "data": [{"id": "task-1", "full": "1 Main St",
                 "venue_name": "Example Venue", "task_type": "New Ad"}]}
        selected = [("Blue", route)]
        first = scope["bulk_assign"](Engine(), selected, "2026-10-10", move, "team", "worker")
        second = scope["bulk_assign"](Engine(), selected, "2026-10-10", move, "team", "worker")
        third = scope["bulk_assign"](Engine(), selected, "2026-10-10", move, "team", "worker")
        self.assertEqual(len(first[2]), 1)
        self.assertIn("safe to retry", first[2][0][1])
        self.assertEqual(len(second[0]), 1)
        self.assertEqual(len(third[1]), 1)
        self.assertEqual(len(moved), 2)
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
        app.radio(key="revamp_status").set_value("Routes").run()
        app.button(key="revamp_group_toggle_Routes__MI").click().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Routes")
        self.assertEqual(app.query_params["view"], ["Routes"])
        app.button(key="revamp_select_visible").click().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Routes")
        detroit = next(button for button in app.button if button.label.startswith("Detroit"))
        detroit.click().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Routes")
        self.assertFalse(app.exception)
        app.radio(key="revamp_status").set_value("Field Nation").run()
        selected = next(checkbox for checkbox in app.checkbox if checkbox.key.startswith("revamp_fn_"))
        selected.check().run()
        self.assertEqual(app.radio(key="revamp_status").value, "Field Nation")
        self.assertEqual(app.button(key="revamp_fn_posted").label, "Mark Posted (1)")
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()


def test_stale_session_field_nation_label_returns_to_normal_route():
    scope = load_functions("revamp_workspace.py", ["_route_hash", "_route_status"],
                           {"hashlib": hashlib, "HIGH_RATE_FLAG_THRESHOLD": 25.0,
                            "st": SimpleNamespace(session_state={})})
    route = {"city": "Mount Vernon", "state": "IL",
             "data": [{"id": "t1"}, {"id": "t2"}]}
    route_hash = scope["_route_hash"](route)
    scope["st"].session_state[f"route_state_{route_hash}"] = "field_nation"
    # The legacy code showed a phantom FN card despite no persisted reservation.
    assert scope["_route_status"](route, {}, 22) == "Ready"
    assert f"route_state_{route_hash}" not in scope["st"].session_state
    assert scope["_route_status"](
        route, {"t1": {"status": "field_nation"}}, 22) == "Field Nation"


def test_return_fn_lookup_requires_complete_task_identity():
    from migration import data_access as da
    stored = [
        {"work_order": "FN10132026-Mount Vernon IL-1", "status": "posted",
         "payload": {"cluster_hash": "original-hash", "taskIds": "a,b,c,d,e"}},
    ]
    # Rebundled route gets another hash but retains exactly the same tasks.
    result = da._match_fn_return_order(stored, "new-hash", "none",
                                       ["a", "b", "c", "d", "e"])
    assert result["success"] and result["work_order"] == stored[0]["work_order"]
    assert result["cluster_hash"] == "original-hash"
    assert da._match_fn_return_order(
        stored, "original-hash", "", ["a", "b"])["reason"] == "partial"
    assert da._match_fn_return_order(
        stored, "stale-hash", "none", ["other"])["reason"] == "not_found"
    assert da._match_fn_return_order(
        [{**stored[0], "status": "assigned"}], "new-hash", "",
        ["a", "b", "c", "d", "e"])["reason"] == "already_assigned"
    syncing = {**stored[0], "payload": {
        **stored[0]["payload"], "fn_handoff_state": "running"}}
    assert da._match_fn_return_order(
        [syncing], "original-hash", "",
        ["a", "b", "c", "d", "e"])["reason"] == "handoff_running"


def test_return_orphan_fn_route_does_not_unassign_or_delete(monkeypatch):
    from migration import data_access as da
    calls = []
    monkeypatch.setattr(da, "resolve_fn_return_order",
                        lambda *args: {"success": False, "reason": "not_found"})
    monkeypatch.setattr(da, "remove_field_nation",
                        lambda *args: calls.append("DELETE"))
    scope = load_functions(
        "revamp_workspace.py", ["_return_fn_route_to_regular"],
        {"st": SimpleNamespace(session_state={
            "route_state_stale-hash": "field_nation",
            "revamp_selected_route": "Blue:stale-hash",
        })})
    class Refresh:
        def clear(self):
            calls.append("invalidate")
    route = {"wo": "none", "city": "Mount Vernon", "state": "IL",
             "data": [{"id": "a"}, {"id": "b"}]}
    outcome = scope["_return_fn_route_to_regular"](
        route, "stale-hash", "Blue", object(),
        lambda *args, **kw: calls.append("OnFleet PUT"), Refresh())
    assert outcome == "stale_cleared"
    assert calls == ["invalidate"]
    assert "route_state_stale-hash" not in scope["st"].session_state


def test_return_real_fn_route_uses_db_identity_and_only_then_removes(monkeypatch):
    from migration import data_access as da
    events = []
    saved = {
        "success": True, "work_order": "FN10132026-Mount Vernon IL-1",
        "cluster_hash": "persisted-hash",
        "task_ids": ["a", "b"], "payload": {
            "taskIds": "a,b", "city": "Mount Vernon", "state": "IL"}}
    monkeypatch.setattr(da, "resolve_fn_return_order",
                        lambda *args: saved)
    monkeypatch.setattr(da, "remove_field_nation",
                        lambda engine, wo: events.append(("deleted", wo)))
    scope = load_functions(
        "revamp_workspace.py", ["_return_fn_route_to_regular"],
        {"st": SimpleNamespace(session_state={})})
    class Refresh:
        def clear(self):
            events.append(("cache", None))
    def dispatch(route_hash, ic, pod, **kwargs):
        assert route_hash == "persisted-hash"
        assert kwargs["cluster_data"]["taskIds"] == "a,b"
        assert kwargs["verify_fn_return"] is True
        events.append(("OnFleet", route_hash))
    route = {"wo": "none", "city": "Mount Vernon", "state": "IL",
             "data": [{"id": "a"}, {"id": "b"}]}
    outcome = scope["_return_fn_route_to_regular"](
        route, "new-hash", "Blue", object(), dispatch, Refresh())
    assert outcome == "returned"
    assert [event[0] for event in events] == ["OnFleet", "deleted", "cache"]


def test_fn_return_keeps_tracking_if_onfleet_release_fails(monkeypatch):
    from migration import data_access as da
    deleted = []
    monkeypatch.setattr(da, "resolve_fn_return_order", lambda *args: {
        "success": True, "work_order": "FN10132026-Mount Vernon IL-1",
        "cluster_hash": "old-hash", "task_ids": ["a", "b"],
        "payload": {"taskIds": "a,b"}})
    monkeypatch.setattr(da, "remove_field_nation",
                        lambda *args: deleted.append("removed"))
    scope = load_functions("revamp_workspace.py", ["_return_fn_route_to_regular"],
                           {"st": SimpleNamespace(session_state={})})
    def fail_release(*args, **kwargs):
        raise RuntimeError("OnFleet did not confirm release of every task")
    with unittest.TestCase().assertRaisesRegex(RuntimeError, "did not confirm"):
        scope["_return_fn_route_to_regular"](
            {"data": [{"id": "a"}, {"id": "b"}]}, "new-hash", "Blue",
            object(), fail_release, SimpleNamespace(clear=lambda: None))
    assert deleted == []


def test_fn_saved_entries_hide_missing_work_orders_and_keep_actual_fn_routes():
    scope = load_functions("revamp_workspace.py",
        ["_has_linked_fn_work_order", "_fn_saved_entries"], {
            "st": SimpleNamespace(session_state={}),
            "_fn_ghost_tasks": lambda ghost: [],
            "_bundle_label": lambda route, bundles: "",
            "_bundle_sets_for_pod": lambda pod: [],
            "_dedupe_route_entries": lambda routes: routes,
        })
    fn = scope["_has_linked_fn_work_order"]
    assert not fn({})
    assert not fn({"wo": "none"})
    assert not fn({"wo": "Field Nation route (no linked work order)"})
    assert fn({"wo": "FN10142026-Seward AK-1"})
    ghosts = {"Orange": [
        {"wo": "none", "hash": "orphan-juneau", "status": "field_nation",
         "city": "Juneau", "state": "AK"},
        {"wo": "", "hash": "orphan-wasilla", "status": "posted",
         "city": "Wasilla", "state": "AK"},
        {"wo": "FN10142026-Seward AK-1", "hash": "saved-seward",
         "status": "field_nation", "city": "Seward", "state": "AK"},
        {"wo": "FN10142026-Another AK-1", "hash": "assigned-history",
         "status": "assigned", "city": "Juneau", "state": "AK"},
    ]}
    entries = scope["_fn_saved_entries"](ghosts, ["Orange"])
    assert len(entries) == 1
    assert entries[0][3] == "saved-seward"
    assert entries[0][1]["wo"] == "FN10142026-Seward AK-1"


def test_field_nation_dispatcher_view_omits_unlinked_live_cluster():
    from streamlit.testing.v1 import AppTest
    source = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import pandas as pd
from revamp_workspace import render_workspace
st.session_state.setdefault("ic_df", pd.DataFrame())
st.session_state.setdefault("clusters_Orange", [
    {{"city": "Juneau", "state": "AK", "stops": 1,
      "data": [{{"id": "orphan-task", "full": "Juneau, AK", "state": "AK"}}]}}
])
def records():
    return ({{"orphan-task": {{"status": "field_nation"}}}},
        {{"Orange": [{{"wo": "FN10142026-Seward AK-1", "hash": "persisted-123",
                      "status": "field_nation", "city": "Seward", "state": "AK",
                      "stops": 1, "tasks": 2, "task_ids": ["s1", "s2"]}}]}},
        set(), {{}})
records.clear = lambda: None
helpers = dict(
    make_venue_details=lambda *args: "",
    make_venue_details_ghost=lambda *args, **kw: "",
    venue_section=lambda content: content,
    render_finalization_checklist=lambda *args, **kw: None,
    move_to_dispatch=lambda *args, **kw: None,
    is_dispatch_associate=lambda: True)
render_workspace(lambda pod: pod == "Orange", lambda pod: None,
    lambda *args: None, lambda *args: 0, None, lambda *args: None,
    records, saved_route_helpers=helpers)
"""
    app = AppTest.from_string(source).run(timeout=30)
    assert not app.exception
    app.radio(key="revamp_status").set_value("Field Nation").run(timeout=30)
    assert not app.exception
    labels = " ".join(x.label for x in app.button)
    assert "Seward" in labels
    assert "Juneau" not in labels
    assert "no linked work order" not in labels.lower()


def test_fn_bulk_payload_preserves_ids_for_each_task_at_same_stop():
    scope = load_functions("revamp_bulk_fn.py", ["_payload"], {"json": json})
    route = {
        "city": "Seward", "state": "AK", "stops": 1, "center": (60, -149),
        "data": [
            {"id": "task-A", "full": "100 Main St, Seward, AK 99664",
             "venue_name": "Market", "venue_id": "VEN-1", "kiosk_id": "K-101",
             "task_type": "New Ad", "client_company": "Client A"},
            {"id": "task-B", "full": "100 Main St, Seward, AK 99664",
             "venue_name": "Market", "venue_id": "VEN-2", "kiosk_id": "K-202",
             "task_type": "Kiosk Install", "client_company": "Client B"},
        ],
    }
    payload = scope["_payload"](route, "Orange", "2026-11-05", "FN-TEST-1", "h1")
    stops = json.loads(payload["stopData"])
    assert len(stops) == 1
    assert stops[0]["venueId"] == "VEN-1"  # legacy stop-level value
    assert stops[0]["kioskId"] == "K-101"
    assert [(task["taskId"], task["venueId"], task["kioskId"])
            for task in stops[0]["tasks"]] == [
                ("task-A", "VEN-1", "K-101"),
                ("task-B", "VEN-2", "K-202"),
            ]


def test_fn_saved_task_rebuild_and_csv_keep_distinct_kiosk_ids():
    scope = load_functions("revamp_workspace.py",
                           ["_fn_ghost_tasks", "_fn_csv_route"], {"json": json})
    stop = {
        "addr": "100 Main St, Seward, AK 99664", "venue": "Market",
        "venueId": "VEN-1", "kioskId": "K-101", "t_count": 2,
        "tasks": [
            {"taskId": "task-A", "taskType": "New Ad", "venueId": "VEN-1",
             "kioskId": "K-101", "clientCompany": "Client A"},
            {"taskId": "task-B", "taskType": "Kiosk Install", "venueId": "VEN-2",
             "kioskId": "K-202", "clientCompany": "Client B"},
        ],
    }
    ghost = {"stops": 1, "stop_data": [stop]}
    tasks = scope["_fn_ghost_tasks"](ghost)
    assert [(t["id"], t["venue_id"], t["kiosk_id"]) for t in tasks] == [
        ("task-A", "VEN-1", "K-101"), ("task-B", "VEN-2", "K-202")
    ]
    called = []
    def old_rebuilder(*args, **kwargs):
        called.append(True)
        return {"data": [{"id": "wrong", "kiosk_id": "K-101"}]}
    output = scope["_fn_csv_route"](
        {"_is_ghost": True, "_ghost_record": ghost}, "hash", old_rebuilder)
    assert not called
    assert [t["kiosk_id"] for t in output["data"]] == ["K-101", "K-202"]


def test_fn_route_stop_details_show_ids_per_stop_and_task_without_guessing():
    scope = load_functions("revamp_workspace.py", ["_fn_stop_markup"],
                           {"html": __import__("html")})
    saved = {"venue": "Test & Shop", "t_count": 2,
             "venueId": "VEN-1", "kioskId": "K-101",
             "tasks": [
                 {"taskId": "task-A", "venueId": "VEN-1", "kioskId": "K-101",
                  "taskType": "New Ad"},
                 {"taskId": "task-B", "venueId": "VEN-2", "kioskId": "K-202",
                  "taskType": "Kiosk Install"},
             ]}
    markup = scope["_fn_stop_markup"](saved, 1, "100 Main St")
    assert "Test &amp; Shop" in markup
    assert "Venue IDs: VEN-1, VEN-2" in markup
    assert "Kiosk IDs: K-101, K-202" in markup
    assert "Task 1 (task-A)" in markup and "Task 2 (task-B)" in markup
    assert "Kiosk ID: K-202" in markup
    legacy = scope["_fn_stop_markup"]({
        "venue": "Market", "kioskId": "K-101", "venueId": "VEN-1",
        "t_count": 2}, 2, "200 Main St")
    assert "Per-task identifiers were not saved" in legacy
    assert "Kiosk IDs: K-101, K-202" not in legacy


def test_field_nation_csv_location_slot_keeps_template_and_task_identifiers():
    helper = load_functions("fn_utils.py", ["_fn_task_location_text"])["_fn_task_location_text"]
    assert helper({
        "task_type": "New Ad", "location_in_venue": "Front Entrance",
        "venue_id": "VEN-1", "kiosk_id": "K-101",
    }) == "New Ad — Front Entrance | Venue ID: VEN-1 | Kiosk ID: K-101"
    assert helper({
        "task_type": "Kiosk Install", "venue_id": "VEN-2", "kiosk_id": "K-202",
    }) == "Kiosk Install | Venue ID: VEN-2 | Kiosk ID: K-202"
    assert helper({"task_type": "Default"}) == "Default"


def test_route_venue_accordions_show_multiple_task_identifiers():
    scope = load_functions("tactical_workspace_master_rw.py",
                           ["make_venue_details", "make_venue_details_ghost"], {
        "esc": __import__("html").escape,
        "task_location_address": lambda loc, tasks: loc,
        "address_with_zip": lambda address, zipcode: address,
    })
    live_tasks = [
        {"full": "100 Main St", "venue_name": "Market", "task_type": "New Ad",
         "client_company": "Client", "venue_id": "VEN-1", "kiosk_id": "K-101"},
        {"full": "100 Main St", "venue_name": "Market", "task_type": "New Ad",
         "client_company": "Client", "venue_id": "VEN-2", "kiosk_id": "K-202"},
    ]
    live = scope["make_venue_details"](live_tasks)
    assert "Venue IDs: VEN-1, VEN-2" in live
    assert "Kiosk IDs: K-101, K-202" in live
    assert "Task 2" in live and "Kiosk ID: K-202" in live
    ghost = scope["make_venue_details_ghost"](["100 Main St"], [{
        "addr": "100 Main St", "venue": "Market",
        "tasks": [
            {"taskType": "New Ad", "venueId": "VEN-1", "kioskId": "K-101"},
            {"taskType": "New Ad", "venueId": "VEN-2", "kioskId": "K-202"},
        ],
    }])
    assert "Venue IDs: VEN-1, VEN-2" in ghost
    assert "Kiosk ID: K-202" in ghost


def test_legacy_fn_ghost_rebuild_uses_saved_per_task_ids_not_first_kiosk():
    build = load_functions("tactical_workspace_master_rw.py", ["_fn_ghost_to_cluster"])["_fn_ghost_to_cluster"]
    saved = {
        "wo": "FN10142026-Seward AK-1", "task_ids": ["task-A", "task-B"],
        "city": "Seward", "state": "AK",
        "stop_data": [{
            "addr": "100 Main St, Seward, AK 99664", "venue": "Market",
            "venueId": "VEN-1", "kioskId": "K-101",
            "inst": 1, "n_ad": 1,
            "tasks": [
                {"taskId": "task-A", "venueId": "VEN-1", "kioskId": "K-101",
                 "taskType": "New Ad", "clientCompany": "Client A"},
                {"taskId": "task-B", "venueId": "VEN-2", "kioskId": "K-202",
                 "taskType": "Kiosk Install", "clientCompany": "Client B"},
            ],
        }],
    }
    route = build(saved, skip_geocode=True)
    assert route is not None
    assert [(task["id"], task["venue_id"], task["kiosk_id"]) for task in route["data"]] == [
        ("task-A", "VEN-1", "K-101"), ("task-B", "VEN-2", "K-202")
    ]
