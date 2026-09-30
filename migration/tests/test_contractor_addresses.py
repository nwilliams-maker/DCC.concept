from migration.contractor_sync import (
    _coordinate_updates, _discover_mapping, _extract_state_zip_from_query,
    _mapbox_match_is_acceptable,
)
from migration.contractor_address_backfill import plan_fixes

FRESNO = "1234 Main St, Fresno, CA 93721"


def test_state_zip_extracted_and_street_suffix_not_mistaken_for_state():
    assert _extract_state_zip_from_query(FRESNO) == ("CA", "93721")
    assert _extract_state_zip_from_query("55 Oak Ct, Phoenix, AZ") == ("AZ", None)
    assert _extract_state_zip_from_query("Fresno, California") == (None, None)


def _feat(rel, types, state="CA", zip_code="93721", address=None):
    return {"relevance": rel, "place_type": types, "center": [-119.78, 36.74], "address": address,
            "context": [{"id": "region.1", "short_code": f"US-{state}"},
                        {"id": "postcode.1", "text": zip_code}]}


def test_city_level_hit_rejected_for_street_address():
    assert not _mapbox_match_is_acceptable(FRESNO, _feat(0.95, ["place"]))
    assert not _mapbox_match_is_acceptable(FRESNO, _feat(0.85, ["address"], address="1234"))


def test_exact_address_hit_accepted_and_wrong_state_or_zip_rejected():
    assert _mapbox_match_is_acceptable(FRESNO, _feat(0.99, ["address"], address="1234"))
    assert not _mapbox_match_is_acceptable(FRESNO, _feat(0.99, ["address"], state="NV", address="1234"))
    assert not _mapbox_match_is_acceptable(FRESNO, _feat(0.99, ["address"], zip_code="93650", address="1234"))


def test_location_column_prefers_address_over_service_area_deterministically():
    cols = [{"id": "svc", "title": "Service Area"}, {"id": "loc", "title": "*Location"},
            {"id": "addr", "title": "Address"}, {"id": "em", "title": "Email"}]
    for _ in range(20):
        assert _discover_mapping(cols)["location"] == "addr"
    assert _discover_mapping(cols[1:2] + cols[3:])["location"] == "loc"


def test_changed_address_that_fails_geocode_clears_old_coords():
    existing = {"location": "old", "lat": 1.0, "lng": 2.0}
    assert _coordinate_updates(existing, {}, {"location": "new"}, geocode=lambda _: (None, None)) == \
        {"lat": None, "lng": None}
    assert _coordinate_updates(existing, {}, {"location": "new"}, geocode=lambda _: (3.0, 4.0)) == \
        {"lat": 3.0, "lng": 4.0}
    # Unchanged address with coords present: leave alone.
    assert _coordinate_updates(existing, {"location": "old"}, {}, geocode=lambda _: (9, 9)) == {}


def test_backfill_plan_classifies_rows():
    contractors = [
        {"id": 1, "name": "Moved", "email": "a@x.com", "phone": "", "location": FRESNO, "lat": 36.7, "lng": -119.7},
        {"id": 2, "name": "NewAddr", "email": "b@x.com", "phone": "", "location": "Fresno, CA", "lat": 36.7, "lng": -119.7},
        {"id": 3, "name": "ByPhone", "email": "old@x.com", "phone": "(559) 555-0100", "location": FRESNO, "lat": None, "lng": None},
        {"id": 4, "name": "Gone", "email": "d@x.com", "phone": "", "location": FRESNO, "lat": 1, "lng": 1},
        {"id": 5, "name": "Vague", "email": "e@x.com", "phone": "", "location": "Nowhere", "lat": 5, "lng": 5},
    ]
    sources = [
        {"email": "a@x.com", "location": FRESNO},
        {"email": "b@x.com", "location": "99 Elm Dr, Fresno, CA 93721"},
        {"email": "new@x.com", "phone": "5595550100", "location": FRESNO},
        {"email": "e@x.com", "location": "Nowhere"},
    ]
    geo = {FRESNO: (36.80, -119.80), "99 Elm Dr, Fresno, CA 93721": (36.75, -119.76)}
    plan = {p["name"]: p for p in plan_fixes(contractors, sources, geocode=lambda l: geo.get(l, (None, None)))}
    assert plan["Moved"]["action"] == "coords_moved"
    assert plan["NewAddr"]["action"] == "address_changed"
    assert plan["NewAddr"]["new_location"] == "99 Elm Dr, Fresno, CA 93721"
    assert plan["ByPhone"]["action"] == "coords_filled"
    assert plan["Gone"]["action"] == "no_monday_match"
    assert plan["Vague"]["action"] == "coords_unverified"
