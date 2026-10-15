"""Render regression checks for expanded REGULAR route-card kiosk details.

No application boot, secrets, production DB, OnFleet or UI actions. Extract
the two pure HTML venue renderers from DCC's large entrypoint as AST nodes.
"""
import ast
import html
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "tactical_workspace_master_rw.py").read_text(encoding="utf-8")
REVAMP = (ROOT / "revamp_workspace.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def renderers():
    tree = ast.parse(SOURCE, filename="tactical_workspace_master_rw.py")
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in ("make_venue_details", "make_venue_details_ghost")]
    assert {n.name for n in selected} == {"make_venue_details", "make_venue_details_ghost"}
    env = {
        "esc": html.escape,
        "st": SimpleNamespace(session_state={}),
        "task_location_address": lambda address, tasks: address,
        "address_with_zip": lambda address, zip_code="": str(address),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                 "kiosk_location_test", "exec"), env)
    return env


def task(kid, location, *, client="Acme", task_type="Kiosk Install"):
    return {
        "full": "123 Main St, Chicago, IL 60601",
        "venue_name": "Test CVS", "venue_id": "VENUE7",
        "kiosk_id": kid, "location_in_venue": location,
        "client_company": client, "task_type": task_type,
    }


def test_saved_expanded_route_row_has_location_immediately_after_kiosk_id(renderers):
    output = renderers["make_venue_details"]([task("K123", "Near front entrance")])
    assert "Kiosk ID: K123&nbsp;·&nbsp; Kiosk Location: Near front entrance" in output
    assert "Venue ID: VENUE7" in output


def test_same_venue_different_kiosks_keep_their_own_locations(renderers):
    output = renderers["make_venue_details"]([
        task("K123", "Front wall"), task("K456", "Next to pharmacy")])
    assert "Kiosk ID: K123&nbsp;·&nbsp; Kiosk Location: Front wall" in output
    assert "Kiosk ID: K456&nbsp;·&nbsp; Kiosk Location: Next to pharmacy" in output
    assert output.count("Kiosk Location:") == 2


def test_same_kiosk_id_with_different_locations_is_not_misgrouped(renderers):
    output = renderers["make_venue_details"]([
        task("K123", "Front wall"), task("K123", "Rear wall")])
    assert "Kiosk ID: K123&nbsp;·&nbsp; Kiosk Location: Front wall" in output
    assert "Kiosk ID: K123&nbsp;·&nbsp; Kiosk Location: Rear wall" in output


def test_missing_location_is_shown_as_unavailable_not_guessed(renderers):
    output = renderers["make_venue_details"]([task("K123", "")])
    assert "Kiosk ID: K123&nbsp;·&nbsp; Kiosk Location: —" in output


def test_location_and_kiosk_id_are_html_escaped(renderers):
    output = renderers["make_venue_details"]([
        task('K<&123', '<script>unsafe & wrong</script>')])
    assert "Kiosk ID: K&lt;&amp;123&nbsp;·&nbsp; Kiosk Location: &lt;script&gt;" in output
    assert "<script>unsafe" not in output


def ghost_stop(tasks, *, legacy_location="Wrong stop-level first kiosk"):
    return {
        "addr": "123 Main St, Chicago, IL 60601", "venue": "CVS Test",
        "venueId": "VENUE7", "kioskId": "K123",
        "locationInVenue": legacy_location,
        "t_count": len(tasks), "inst": len(tasks),
        "tasks": tasks,
    }


def test_saved_ghost_expanded_tasks_show_individual_kiosk_locations(renderers):
    tasks = [
        {"taskType": "Kiosk Install", "venueId": "VENUE7",
         "kioskId": "K123", "locationInVenue": "Lobby"},
        {"taskType": "New Ad", "venueId": "VENUE7",
         "kioskId": "K456", "locationInVenue": "Checkout"},
    ]
    output = renderers["make_venue_details_ghost"](
        ["123 Main St, Chicago, IL 60601"], [ghost_stop(tasks)])
    assert "Kiosk ID: K123 · Kiosk Location: Lobby" in output
    assert "Kiosk ID: K456 · Kiosk Location: Checkout" in output
    assert "Wrong stop-level first kiosk" not in output


def test_ghost_does_not_assign_stop_location_to_multiple_unknown_kiosks(renderers):
    tasks = [
        {"taskType": "New Ad", "kioskId": "K123"},
        {"taskType": "New Ad", "kioskId": "K456"},
    ]
    output = renderers["make_venue_details_ghost"](
        ["123 Main St, Chicago, IL 60601"], [ghost_stop(tasks)])
    assert "Kiosk ID: K123 · Kiosk Location: —" in output
    assert "Kiosk ID: K456 · Kiosk Location: —" in output
    assert "Wrong stop-level first kiosk" not in output


def test_ghost_one_task_can_use_legacy_stop_level_location(renderers):
    tasks = [{"taskType": "New Ad", "kioskId": "K123"}]
    output = renderers["make_venue_details_ghost"](
        ["123 Main St, Chicago, IL 60601"],
        [ghost_stop(tasks, legacy_location="Front right")])
    assert "Kiosk ID: K123 · Kiosk Location: Front right" in output


def test_live_ready_flagged_dispatch_expansion_includes_location():
    tree = ast.parse(SOURCE)
    func = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "render_dispatch")
    segment = ast.get_source_segment(SOURCE, func)
    assert "kiosk_location_text" in segment
    assert "{kiosk_text}{kiosk_location_text}" in segment
    assert "location_in_venue" in segment


def test_field_nation_dedicated_view_unmodified_and_still_venue_id_only():
    tree = ast.parse(REVAMP)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_fn_stop_markup")
    body = ast.get_source_segment(REVAMP, fn)
    assert "Venue ID:" in body
    assert "Kiosk ID:" not in body
    assert "Kiosk Location:" not in body
