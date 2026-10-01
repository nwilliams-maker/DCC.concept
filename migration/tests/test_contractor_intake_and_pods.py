"""Pods follow the state map; the hourly intake never regresses IC addresses."""
import os
import sys

import pytest
import sqlalchemy as sa

from migration.contractor_sync import (
    _discover_mapping, _item_to_source, resolved_pod, state_from_location,
)

FULL = "7153 East Warren Drive, Denver, CO, USA"


@pytest.mark.parametrize("loc,state", [
    ("1234 Main St, Fresno, CA 93721", "CA"),
    ("55 Oak Ct, Phoenix, AZ", "AZ"),           # "Ct" is not Connecticut
    ("12 Elm Dr, Moore, OK 73160", "OK"),
    ("101 Lauryn Drive apt 1, San Juan, Texas 78589, USA", "TX"),
    ("1167 Hartwell RD. Locust Grove GA 30248", "GA"),
    ("12856 3rd Street, Clearlake Oaks, California, USA", "CA"),
    ("92 Spring Valley Dr", None),
])
def test_state_from_location(loc, state):
    assert state_from_location(loc) == state


def test_pod_comes_from_state_map_not_monday_cell():
    assert resolved_pod("1313 Wheeler Ave SE, Albuquerque, NM, USA", None) == "Purple"
    assert resolved_pod("8 A St, Dallas, TX, USA", "Red") == "Purple"
    assert resolved_pod("9 B St, Seattle, WA, USA", "Purple") == "Blue"
    assert resolved_pod("92 Spring Valley Dr", "Green") == "Green"  # no state: fallback


def test_item_to_source_sets_pod_from_location_state():
    cols = [{"id": "loc", "title": "*Location", "type": "location"},
            {"id": "em", "title": "Email", "type": "email"},
            {"id": "pod", "title": "Pod Color", "type": "status"}]
    mapping = _discover_mapping(cols)
    item = {"id": "1", "name": "Tim", "column_values": [
        {"id": "em", "text": "t@x.com", "value": None},
        {"id": "pod", "text": "Red", "value": None},
        {"id": "loc", "text": "1 Main St, Columbia, SC, USA", "value": '{"lat":"34.0","lng":"-81.0"}'},
    ]}
    assert _item_to_source(item, mapping)["pod_color"] == "Blue"


# ---- receiver (/internal/contractors/sync) end to end on SQLite ----------

@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("REVAMP_CONTRACTOR_SYNC_TOKEN", "tok")
    monkeypatch.delenv("ONFLEET_KEY", raising=False)
    monkeypatch.delenv("MAPBOX_TOKEN", raising=False)
    sys.modules.pop("migration.portal_api", None)
    from fastapi.testclient import TestClient
    from migration import portal_api
    eng = sa.create_engine(f"sqlite:///{tmp_path}/t.db")
    with eng.begin() as c:
        c.execute(sa.text("""CREATE TABLE contractors (id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT,
            phone TEXT, location TEXT, lat REAL, lng REAL, ic_list TEXT, pod_color TEXT,
            digital_certified BOOLEAN, unrestricted BOOLEAN, updated_at TIMESTAMP)"""))
    monkeypatch.setattr(portal_api, "engine", eng)
    # SQLite has no now(); emulate it for the receiver's SQL.
    @sa.event.listens_for(eng, "connect")
    def _now(dbapi_conn, _):
        dbapi_conn.create_function("now", 0, lambda: "2026-09-30 00:00:00")
    eng.dispose()
    return TestClient(portal_api.app), eng


def _send(client, refresh_addresses=False, **row):
    base = {"monday_item_id": "1", "monday_created_at": "2026-01-01T00:00:00Z", "email": "a@x.com",
            "name": "Adel Bahri", "phone": "7209035938", "ic_list": "ACTIVE"}
    r = client.post("/internal/contractors/sync", headers={"Authorization": "Bearer tok"},
                    json={"contractors": [{**base, **row}], "refresh_addresses": refresh_addresses})
    assert r.status_code == 200, r.text
    return r.json()


def _row(eng):
    with eng.connect() as c:
        return dict(c.execute(sa.text("SELECT location, lat, lng, pod_color FROM contractors")).mappings().one())


def test_receiver_insert_uses_pin_and_state_pod(client):
    c, eng = client
    _send(c, location=FULL, monday_lat="39.6763", monday_lng="-104.9012", pod_color="Red")
    assert _row(eng) == {"location": FULL, "lat": 39.6763, "lng": -104.9012, "pod_color": "Green"}


def test_receiver_syncs_existing_address_and_pin_automatically(client):
    c, eng = client
    _send(c, location=FULL, monday_lat="39.6763", monday_lng="-104.9012")
    before = _row(eng)
    _send(c, location="7153 East Warren Drive", monday_lat="30.2", monday_lng="-97.7")
    assert _row(eng) == before
    result = _send(c, location="99 Elm St, Austin, TX, USA", monday_lat="30.2", monday_lng="-97.7")
    assert result["addresses_updated"] == 1
    assert _row(eng) == {"location": "99 Elm St, Austin, TX, USA", "lat": 30.2, "lng": -97.7, "pod_color": "Purple"}
    assert _send(c, location="99 Elm St, Austin, TX, USA", monday_lat="30.2", monday_lng="-97.7")["addresses_updated"] == 0


def test_receiver_baseline_preserves_corrected_roster_then_tracks_edits(client):
    c, eng = client
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO contractors (email,name,location,lat,lng,pod_color) VALUES ('a@x.com','Adel Bahri',:loc,39.6763,-104.9012,'Green')"), {"loc": FULL})
    before = _row(eng)
    _send(c, location="88 Old St, Austin, TX, USA", monday_lat=30.1, monday_lng=-97.6)
    assert _row(eng) == before
    _send(c, location="99 New St, Austin, TX, USA", monday_lat=30.2, monday_lng=-97.7)
    assert _row(eng)["location"] == "99 New St, Austin, TX, USA"


def test_receiver_targeted_refresh_and_failed_geocode_clear_old_pin(client):
    c, eng = client
    _send(c, location=FULL, monday_lat=39.6763, monday_lng=-104.9012)
    _send(c, refresh_addresses=True, location="99 Elm St, Austin, TX, USA")
    assert _row(eng) == {"location": "99 Elm St, Austin, TX, USA", "lat": None, "lng": None, "pod_color": "Purple"}


def test_receiver_pin_only_change_and_blank_are_safe(client):
    c, eng = client
    _send(c, location=FULL, monday_lat=39.6763, monday_lng=-104.9012)
    _send(c, location=None)
    assert _row(eng)["lat"] == 39.6763
    _send(c, location=FULL, monday_lat=39.6764, monday_lng=-104.9013)
    assert _row(eng)["lat"] == 39.6764


def test_receiver_still_updates_status_for_existing_ic(client):
    c, eng = client
    _send(c, location=FULL)
    _send(c, location=FULL, ic_list="NEED INSURANCE")
    with eng.connect() as conn:
        assert conn.execute(sa.text("SELECT ic_list FROM contractors")).scalar() == "NEED INSURANCE"


def test_update_ic_address_single_ic_plan():
    from migration.update_ic_address import plan_update
    new = plan_update({"pod_color": "Red"}, "99  Elm St, Austin, TX 78701, USA", 30.2, -97.7)
    assert new == {"location": "99 Elm St, Austin, TX 78701, USA", "lat": 30.2, "lng": -97.7, "pod_color": "Purple"}
    with pytest.raises(ValueError):
        plan_update({}, "99 Elm St")


def test_bulk_backfill_cli_is_retired():
    from migration.contractor_address_backfill import main
    assert main([]) == 2
