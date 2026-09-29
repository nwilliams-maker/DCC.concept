from datetime import datetime, timezone
from unittest.mock import patch

import sqlalchemy as sa

from migration.onfleet_contractor_reconcile import (
    MAX_CREATES_PER_RUN, NEW_IC_CUTOFF, _worker_matches,
    _create_worker_in_onfleet, preview_new_contractors, reconcile_missing_once,
)


def test_worker_matching_catches_conflicting_contacts_and_duplicate_names():
    ic = {"name": "Alex Example", "phone": "(555) 111-2222", "email": "alex@example.com"}
    worker = {"id": "w1", "name": "Alex Example", "phone": "+15551112222", "email": "alex@example.com"}
    assert _worker_matches(ic, [worker])[0] == "already_present"
    assert _worker_matches(ic, [worker, {**worker, "id": "w2"}])[0] == "conflict"
    assert _worker_matches(ic, [{**worker, "phone": "+15553334444"}])[0] == "conflict"
    assert _worker_matches(ic, [{**worker, "email": "other@example.com"}])[0] == "conflict"
    assert _worker_matches(ic, [{**worker, "id": "w2", "email": "other@example.com",
                                  "phone": "+15553334444"}])[0] == "conflict"
    assert _worker_matches(ic, [])[0] == "missing"


def test_preview_filters_status_and_requires_a_pod_team():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE contractors (
                id INTEGER, name TEXT, email TEXT, phone TEXT, location TEXT,
                pod_color TEXT, ic_list TEXT, created_at TIMESTAMP
            )
        """))
        rows = [
            (1, "New IC", "new@example.com", "5551112222", "Chicago, IL", "Blue", "ACTIVE"),
            (2, "Inactive IC", "inactive@example.com", "5551113333", "Chicago, IL", "Blue", "INACTIVE"),
            (3, "No Pod", "nopod@example.com", "5551114444", "Chicago, IL", None, "ACTIVE"),
            (4, "No Status", "nostatus@example.com", "5551115555", "Chicago, IL", "Blue", None),
        ]
        for row in rows:
            conn.execute(sa.text("""
                INSERT INTO contractors VALUES (:id, :name, :email, :phone, :location,
                    :pod, :status, :created)
            """), dict(zip(("id", "name", "email", "phone", "location", "pod", "status", "created"),
                            (*row, datetime(2026, 9, 29, tzinfo=timezone.utc)))))
    with patch("migration.onfleet_contractor_reconcile._onfleet_list_workers", return_value=[]), \
         patch("migration.onfleet_contractor_reconcile._onfleet_request") as request:
        request.return_value.json.return_value = [{"id": "team1", "name": "POD: Blue"}]
        outcomes = {r["name"]: r["outcome"] for r in preview_new_contractors(engine)}
    assert outcomes == {"New IC": "missing", "Inactive IC": "ineligible",
                        "No Pod": "missing", "No Status": "ineligible"}
    assert NEW_IC_CUTOFF == datetime(2026, 9, 24, 5, tzinfo=timezone.utc)


def test_create_worker_uses_onfleet_routing_address_and_verifies_it():
    ic = {"id": 1, "name": "New IC", "email": "new@example.com",
          "phone": "3125551212", "location": "123 Main St, Chicago, IL",
          "team_id": "blue", "outcome": "missing"}

    def request(method, path, **kwargs):
        class Response:
            def json(self):
                if path == "/destinations":
                    return {"id": "destination-1"}
                if method == "POST" and path == "/workers":
                    assert kwargs["json"]["addresses"] == {"routing": "destination-1"}
                    return {"id": "worker-1"}
                return {"id": "worker-1", "phone": "+13125551212",
                        "addresses": {"routing": "destination-1"}}
        return Response()

    with patch("migration.onfleet_contractor_reconcile._onfleet_request", side_effect=request):
        result = _create_worker_in_onfleet(ic)
    assert result["status"] == "created"


def test_automatic_reconciliation_only_sends_a_bounded_missing_batch():
    candidates = [{"id": n, "outcome": "missing"} for n in range(1, 24)]
    candidates += [{"id": 24, "outcome": "already_present"},
                   {"id": 25, "outcome": "conflict"}]
    with patch("migration.onfleet_contractor_reconcile.preview_new_contractors", return_value=candidates), \
         patch("migration.onfleet_contractor_reconcile.create_missing_contractors", return_value=[{"status": "created"}]) as create:
        assert reconcile_missing_once(object()) == [{"status": "created"}]
    selected = create.call_args.kwargs["selected_ids"]
    assert selected == set(range(1, MAX_CREATES_PER_RUN + 1))
