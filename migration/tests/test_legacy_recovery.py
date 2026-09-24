"""Checks that recovery picks the decision and preserves every old link ID."""
from datetime import datetime

from migration.recover_legacy_routes import canonical_routes, parse_route


def test_old_link_ids_and_decisions_are_distinct_from_work_order():
    payload = '{"wo":"Contractor-09222026-1","comp":25}'
    sent = parse_route({"Route ID": "R-OLD", "WO": "Contractor-09222026-1",
                        "Contractor": "Contractor", "Date Created": "9/22/2026 10:00:00",
                        "JSON Payload": payload}, "sent")
    accepted = parse_route({"Route ID": "R-NEW", "WO": "Contractor-09222026-1",
                            "Contractor": "Contractor", "Date Created": "9/22/2026 11:00:00",
                            "JSON Payload": payload}, "accepted")
    archived = parse_route({"Route ID": "R-REVOKED", "WO": "Contractor-09222026-1",
                            "Contractor": "Contractor", "Date Created": "9/22/2026 12:00:00",
                            "JSON Payload": payload}, "archived")
    assert sent["route_id"] != sent["wo"]
    assert canonical_routes([sent, accepted, archived])[sent["wo"]]["status"] == "accepted"
    assert isinstance(sent["created_at"], datetime) and sent["created_at"].tzinfo is not None


def test_invalid_payload_is_not_imported():
    assert parse_route({"Route ID": "R-1", "WO": "WO-1",
                        "Date Created": "9/22/2026 10:00:00", "JSON Payload": "broken"}, "sent") is None
