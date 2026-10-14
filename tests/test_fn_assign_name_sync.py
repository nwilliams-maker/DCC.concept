"""Staged-only FN Assign name synchronization regressions.

Every OnFleet call is simulated in-memory; Postgres is a disposable CI
container. No production OnFleet, Railway, Monday, or other external writes.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa

from migration import data_access as da
from migration import fn_side_effects as fx
from revamp_workspace import _fn_assigned_history_entries


class FakeResponse:
    def __init__(self, status, value):
        self.status_code = status
        self.value = copy.deepcopy(value)
        self.text = json.dumps(value)

    def json(self):
        return copy.deepcopy(self.value)


class FakeOnfleet:
    def __init__(self, plan_id="rp-1", old_name="FN10142026-Chicago IL-1", task_ids=("task-1", "task-2")):
        self.plan_id = plan_id
        self.tasks = {
            tid: {
                "id": tid, "routePlan": plan_id, "worker": "field-nation-worker",
                "metadata": [
                    {"name": "KIOSK_ID", "value": f"kiosk-{tid}", "type": "string", "visibility": ["api"]},
                    {"name": "WO_NAME", "value": old_name, "type": "string", "visibility": ["api"]},
                    {"name": "INSTALLER_NAME", "value": "Field Nation", "type": "string", "visibility": ["api"]},
                ],
            } for tid in task_ids
        }
        self.plans = {
            plan_id: {"id": plan_id, "name": old_name, "tasks": list(task_ids)}
        }
        self.calls = []
        self.fail_task_put = None
        self.fail_plan_put = False
        self.ignore_plan_put = False
        self.reject_worker_refresh = False

    def __call__(self, method, url, *, json_body=None, max_retries=5):
        path = urlsplit(url).path
        self.calls.append((method.lower(), path, copy.deepcopy(json_body)))
        method = method.lower()
        if "/routePlans/" in path:
            rid = path.rsplit("/", 1)[-1]
            if rid not in self.plans:
                return FakeResponse(404, {"message": "No such plan"})
            if method == "get":
                return FakeResponse(200, self.plans[rid])
            if method == "put":
                if self.fail_plan_put:
                    return FakeResponse(422, {"message": "Rename rejected"})
                if not self.ignore_plan_put:
                    self.plans[rid]["name"] = json_body.get("name")
                return FakeResponse(200, self.plans[rid])
        if "/tasks/" in path:
            tid = path.rsplit("/", 1)[-1]
            if tid not in self.tasks:
                return FakeResponse(404, {"message": "Unknown task"})
            if method == "get":
                return FakeResponse(200, self.tasks[tid])
            if method == "put":
                if self.fail_task_put == tid:
                    return FakeResponse(400, {"message": "Task update rejected"})
                if self.reject_worker_refresh and "worker" in (json_body or {}):
                    return FakeResponse(400, {"message": "Cannot refresh same worker"})
                self.tasks[tid].update(copy.deepcopy(json_body or {}))
                return FakeResponse(200, self.tasks[tid])
        raise AssertionError("Unrecognized or unsafe OnFleet call: " + method + " " + url)

    @property
    def plan_puts(self):
        return [call for call in self.calls
                if call[0] == "put" and "/routePlans/" in call[1]]

    @property
    def task_puts(self):
        return [call for call in self.calls
                if call[0] == "put" and "/tasks/" in call[1]]


@pytest.fixture(autouse=True)
def fake_onfleet_environment(monkeypatch):
    monkeypatch.setenv("ONFLEET_KEY", "offline-test-value")
    monkeypatch.setattr(fx.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        fx, "sync_monday_for_stops", lambda *args, **kwargs: {"skipped": "offline CI"})
    monkeypatch.setattr(da, "_mirror_route_to_peer", lambda *args, **kwargs: None)


@pytest.fixture
def database():
    uri = os.environ.get("FN_ASSIGN_TEST_DATABASE_URL")
    if not uri:
        pytest.skip("Requires disposable FN_ASSIGN_TEST_DATABASE_URL, never production")
    engine = sa.create_engine(uri, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM route_events"))
        conn.execute(sa.text("DELETE FROM routes"))
        conn.execute(sa.text("DELETE FROM field_nation_orders"))
    yield engine
    engine.dispose()


def seed(engine, wo, provider="Acme Installs", plan_id="rp-1", task_ids=("task-1", "task-2")):
    payload = {
        "wo": wo, "taskIds": ",".join(task_ids), "cluster_hash": "cluster:" + wo,
        "pod": "Orange", "city": "Chicago", "state": "IL",
        "lCnt": len(task_ids), "tCnt": len(task_ids), "comp": 137.5,
        "locs": "Address A|Address B", "stopData": json.dumps([
            {"addr": "Address A"}, {"addr": "Address B"}]),
        "due": "2026-10-20", "fn_provider": provider,
        "fn_posted_ts": "2026-10-14T12:00:00+00:00",
    }
    with engine.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO field_nation_orders (work_order,status,provider,route_plan_id,payload)
            VALUES (:wo, 'posted', :provider, :route_plan_id, CAST(:payload AS jsonb))
        """), {"wo": wo, "provider": provider, "route_plan_id": plan_id, "payload": json.dumps(payload)})
    return payload


def database_states(engine):
    with engine.connect() as conn:
        fn = conn.execute(sa.text("""
            SELECT work_order,status::text AS status,route_plan_id,payload
            FROM field_nation_orders ORDER BY work_order
        """)).mappings().all()
        accepted = conn.execute(sa.text("""
            SELECT wo,status::text AS status,contractor_name,cluster_hash,payload
            FROM routes ORDER BY wo
        """)).mappings().all()
        return [dict(r) for r in fn], [dict(r) for r in accepted]


def test_onfleet_route_and_all_task_metadata_renamed_and_verified(monkeypatch):
    remote = FakeOnfleet()
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = fx.sync_onfleet_for_fn_assignment(
        ["task-1", "task-2"], "FN-Acme Installs-10/14", "Acme Installs", "rp-1")
    assert result["partial"] is False
    assert result["routePlanNameVerified"] is True
    assert result["routePlanId"] == "rp-1"
    assert remote.plans["rp-1"]["name"] == "FN-Acme Installs-10/14"
    assert len(remote.plan_puts) == 1
    assert remote.plans["rp-1"]["tasks"] == ["task-1", "task-2"]
    for tid, task in remote.tasks.items():
        meta = {m["name"]: m["value"] for m in task["metadata"]}
        assert meta["WO_NAME"] == remote.plans["rp-1"]["name"]
        assert meta["INSTALLER_NAME"] == "Acme Installs"
        assert meta["KIOSK_ID"] == "kiosk-" + tid
        assert task["worker"] == "field-nation-worker"


def test_stale_saved_plan_id_uses_task_actual_plan_and_never_renames_other_plan(monkeypatch):
    remote = FakeOnfleet()
    remote.plans["stale-plan"] = {"id": "stale-plan", "name": "Do not rename", "tasks": ["not-this-wo"]}
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = fx.sync_onfleet_for_fn_assignment(
        ["task-1", "task-2"], "FN-Actual-New", "Acme Installs", "stale-plan")
    assert result["routePlanNameVerified"] is True
    assert result["routePlanId"] == "rp-1"
    assert remote.plans["stale-plan"]["name"] == "Do not rename"


def test_mixed_plan_tasks_are_rejected_before_any_mutations(monkeypatch):
    remote = FakeOnfleet()
    remote.tasks["task-2"]["routePlan"] = "rp-other"
    remote.plans["rp-other"] = {"id": "rp-other", "name": "Other", "tasks": ["task-2"]}
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = fx.sync_onfleet_for_fn_assignment(
        ["task-1", "task-2"], "FN-Should-Not-Rename", "Acme Installs", "rp-1")
    assert result["partial"] is True
    assert not result["routePlanNameVerified"]
    assert not remote.task_puts and not remote.plan_puts


def test_plan_with_unrelated_tasks_is_never_renamed(monkeypatch):
    remote = FakeOnfleet()
    remote.plans["rp-1"]["tasks"].append("unrelated-task")
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = fx.sync_onfleet_for_fn_assignment(
        ["task-1", "task-2"], "FN-Should-Not-Rename", "Acme Installs", "rp-1")
    assert result["partial"] is True
    assert "tasks differ" in result["partialReason"]
    assert not remote.task_puts and not remote.plan_puts


def test_onfleet_rejected_metadata_prevents_route_name_change(monkeypatch):
    remote = FakeOnfleet()
    remote.fail_task_put = "task-2"
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = fx.sync_onfleet_for_fn_assignment(
        ["task-1", "task-2"], "FN-Should-Not-Rename", "Acme Installs", "rp-1")
    assert result["partial"] is True
    assert "task-2" in result["partialReason"]
    assert not remote.plan_puts


@pytest.mark.parametrize("reject,ignore", [(True, False), (False, True)])
def test_onfleet_unverified_plan_name_prevents_success(monkeypatch, reject, ignore):
    remote = FakeOnfleet()
    remote.fail_plan_put = reject
    remote.ignore_plan_put = ignore
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = fx.sync_onfleet_for_fn_assignment(
        ["task-1", "task-2"], "FN-New", "Acme Installs", "rp-1")
    assert result["partial"] is True
    assert result["routePlanNameVerified"] is False


def test_failed_onfleet_rename_keeps_posted_and_no_accepted_route(database, monkeypatch):
    original = "FN10142026-Chicago IL-1"
    seed(database, original)
    remote = FakeOnfleet(old_name=original)
    remote.fail_plan_put = True
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = da.mark_fn_assigned(database, original)
    assert result["success"] is False
    assert "remains Posted" in result["error"]
    fn, accepted = database_states(database)
    assert len(fn) == 1 and fn[0]["status"] == "posted"
    assert fn[0]["payload"]["wo"] == original
    assert not accepted
    assert remote.plans["rp-1"]["name"] == original


def test_confirmed_assign_updates_same_wo_in_onfleet_and_dcc(database, monkeypatch):
    original = "FN10142026-Chicago IL-1"
    seed(database, original)
    remote = FakeOnfleet(old_name=original)
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = da.mark_fn_assigned(database, original)
    assert result["success"] is True
    new_name = result["wo"]
    assert new_name.startswith("FN-Acme Installs-")
    assert new_name == remote.plans["rp-1"]["name"]
    fn, accepted = database_states(database)
    assert fn[0]["status"] == "assigned"
    assert fn[0]["payload"]["wo"] == new_name
    assert fn[0]["payload"]["fn_onfleet_name_verified"] is True
    assert accepted[0]["wo"] == new_name
    assert accepted[0]["payload"]["wo"] == new_name
    assert accepted[0]["contractor_name"] == "Acme Installs"
    assert accepted[0]["cluster_hash"] == "cluster:" + original
    assert all(
        [m["value"] for m in t["metadata"] if m["name"] == "WO_NAME"] == [new_name]
        for t in remote.tasks.values())
    # Verify the exact Associate's Assigned history payload points at the
    # renamed route, rather than the original FN reservation name.
    records = da.get_fn_assigned_orders(database)
    entries = _fn_assigned_history_entries(records, ["Orange"], {"Orange": {"states": ["IL"]}})
    assert len(entries) == 1
    assert entries[0][1]["wo"] == new_name
    assert entries[0][1]["_fn_original_wo"] == original


def test_same_provider_two_routes_unique_names_and_no_overwrite(database, monkeypatch):
    first, second = "FN10142026-Chicago IL-1", "FN10142026-Chicago IL-2"
    seed(database, first, plan_id="rp-1", task_ids=("task-1", "task-2"))
    seed(database, second, plan_id="rp-2", task_ids=("task-3", "task-4"))
    one = FakeOnfleet(plan_id="rp-1", old_name=first, task_ids=("task-1", "task-2"))
    two = FakeOnfleet(plan_id="rp-2", old_name=second, task_ids=("task-3", "task-4"))
    def remote(method, url, **kwargs):
        path = urlsplit(url).path
        return (one if path.endswith(("rp-1", "task-1", "task-2")) else two)(
            method, url, **kwargs)
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    r1 = da.mark_fn_assigned(database, first)
    r2 = da.mark_fn_assigned(database, second)
    assert r1["success"] and r2["success"]
    assert r2["wo"] == r1["wo"] + "-2"
    assert one.plans["rp-1"]["name"] == r1["wo"]
    assert two.plans["rp-2"]["name"] == r2["wo"]
    fn, accepted = database_states(database)
    assert len(accepted) == 2 and len({r["wo"] for r in accepted}) == 2
    assert {r["cluster_hash"] for r in accepted} == {
        "cluster:" + first, "cluster:" + second}
    assert all(r["status"] == "assigned" for r in fn)


def test_double_click_does_not_mutate_any_other_order(database, monkeypatch):
    original = "FN10142026-Chicago IL-1"
    seed(database, original)
    remote = FakeOnfleet(old_name=original)
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    first = da.mark_fn_assigned(database, original)
    n_calls = len(remote.calls)
    second = da.mark_fn_assigned(database, original)
    assert first["success"] and second["success"]
    assert second["alreadyAssigned"] is True
    assert first["wo"] == second["wo"]
    assert len(remote.calls) == n_calls
    _, accepted = database_states(database)
    assert len(accepted) == 1


def test_missing_route_plan_keeps_order_posted(database, monkeypatch):
    original = "FN10142026-Chicago IL-1"
    seed(database, original, plan_id=None)
    remote = FakeOnfleet(old_name=original)
    for task in remote.tasks.values():
        task.pop("routePlan", None)
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = da.mark_fn_assigned(database, original)
    assert not result["success"]
    assert "No OnFleet Route Plan ID" in result["error"]
    fn, accepted = database_states(database)
    assert fn[0]["status"] == "posted"
    assert not accepted


def test_worker_webhook_refresh_failure_does_not_hide_verified_names(database, monkeypatch):
    original = "FN10142026-Chicago IL-1"
    seed(database, original)
    remote = FakeOnfleet(old_name=original)
    remote.reject_worker_refresh = True
    monkeypatch.setattr(fx, "onfleet_fetch_with_backoff", remote)
    result = da.mark_fn_assigned(database, original)
    assert result["success"] is True
    assert len(result["workerRefreshWarnings"]) == 2
    assert remote.plans["rp-1"]["name"] == result["wo"]


def test_only_expected_source_files_and_assigned_cards_are_updated():
    root = Path(__file__).resolve().parents[1]
    source = (root / "revamp_workspace.py").read_text(encoding="utf-8")
    assert 'key=f"revamp_mark_assigned_{pod}_{route_hash}"' in source
    assert 'if status == "Field Nation" and route.get("_fn_assigned_history"):' in source
    assert 'assigned_wo = str(route.get("wo")' in source
    assert '_revamp_show_accepted_next' in source
