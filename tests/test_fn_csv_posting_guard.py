"""Field Nation CSV green-check + Mark Posted gate regression suite.

Only disposable local PostgreSQL and simulated Streamlit download clicks.
No OnFleet, Field Nation, Railway production, or Monday API writes.
"""
import json
import os
from pathlib import Path
import hashlib

import pytest
import sqlalchemy as sa

from migration import data_access as da
from revamp_fn_history import restore_posted

ROOT = Path(__file__).resolve().parents[1]
POD_CONFIGS = {"Orange": {"states": ["AZ"]}}
STATE_MAP = {"ARIZONA": "AZ"}
TEST_CSV = b"Bundle,Venue,Address\n1,Test Venue,123 E Example St\n"
TEST_SHA = hashlib.sha256(TEST_CSV).hexdigest()


@pytest.fixture
def db():
    url = os.environ.get("FN_CSV_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires disposable FN_CSV_TEST_DATABASE_URL; never production")
    assert sa.engine.make_url(url).host in ("localhost", "127.0.0.1")
    engine = sa.create_engine(url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM route_events"))
        conn.execute(sa.text("DELETE FROM routes"))
        conn.execute(sa.text("DELETE FROM field_nation_orders"))
    yield engine
    engine.dispose()


def seed(engine, *, wo="FN10142026-Phoenix AZ-1", cluster_hash="fn-hash-1",
         status="posted", provider=None, posted=False, handoff="complete"):
    task = "task-" + cluster_hash
    p = {
        "wo": wo, "pod": "Orange", "city": "Phoenix", "state": "AZ",
        "cluster_hash": cluster_hash, "fn_provider": provider or "",
        "taskIds": task, "lCnt": 1, "tCnt": 1, "comp": 50, "due": "2026-10-25",
        "locs": "2 S Home St, Phoenix, AZ 85001|123 E Example St, Phoenix, AZ 85004|2 S Home St, Phoenix, AZ 85001",
        "stopData": json.dumps([{
            "addr": "123 E Example St, Phoenix, AZ 85004",
            "zip": "85004", "venue": "Test Venue", "venueId": "VENUE1",
            "tasks": [{"taskId": task, "venueId": "VENUE1",
                       "kioskId": "K1", "clientCompany": "Acme",
                       "taskType": "Kiosk Install"}],
        }]),
        "fn_handoff_state": handoff,
    }
    if posted:
        p["fn_posted_ts"] = "2026-10-14T16:00:00+00:00"
    with engine.begin() as conn:
        conn.execute(sa.text("""
            INSERT INTO field_nation_orders (work_order,status,provider,payload)
            VALUES (:wo,:status,:provider,CAST(:payload AS jsonb))
        """), {"wo": wo, "status": status, "provider": provider, "payload": json.dumps(p)})
    return p


def fetch_row(engine, wo):
    with engine.connect() as conn:
        return dict(conn.execute(sa.text("""
            SELECT work_order, status::text AS status, provider, payload
            FROM field_nation_orders WHERE work_order=:wo
        """), {"wo": wo}).mappings().one())


def acknowledge(engine, wo="FN10142026-Phoenix AZ-1", route_hash="fn-hash-1"):
    return da.record_fn_csv_download(
        engine, [{"work_order": wo, "cluster_hash": route_hash}],
        csv_sha256=TEST_SHA, actor="associate-test",
    )


def test_cannot_mark_unchecked_route_posted_even_direct_backend(db):
    seed(db)
    with pytest.raises(ValueError, match="download the current CSV.*green checkmark"):
        da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")
    row = fetch_row(db, "FN10142026-Phoenix AZ-1")
    assert row["status"] == "posted"
    assert "fn_posted_ts" not in row["payload"]
    assert not da.fn_csv_downloaded(row["payload"])


def test_click_durably_checks_only_exact_downloaded_wo_and_allows_posting(db):
    initial = seed(db)
    seed(db, wo="FN10142026-Phoenix AZ-2", cluster_hash="fn-hash-2")
    marked = acknowledge(db)
    assert [x["work_order"] for x in marked["marked"]] == ["FN10142026-Phoenix AZ-1"]

    checked = fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"]
    other = fetch_row(db, "FN10142026-Phoenix AZ-2")["payload"]
    assert checked["fn_csv_downloaded_by"] == "associate-test"
    assert checked["fn_csv_file_sha256"] == TEST_SHA
    assert da.fn_csv_downloaded(checked)
    assert not da.fn_csv_downloaded(other)

    # Persisted route ghost is what the actual associate card uses.
    _, ghosts, _, _ = da.get_sent_records_from_db(db, POD_CONFIGS, STATE_MAP)
    by_wo = {g["wo"]: g for g in ghosts["Orange"]}
    assert by_wo["FN10142026-Phoenix AZ-1"]["fn_csv_downloaded"] is True
    assert by_wo["FN10142026-Phoenix AZ-2"]["fn_csv_downloaded"] is False

    da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")
    assert fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"]["fn_posted_ts"]
    assert not fetch_row(db, "FN10142026-Phoenix AZ-2")["payload"].get("fn_posted_ts")
    with pytest.raises(ValueError, match="already Posted"):
        da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")


def test_mixed_csv_batch_fails_atomically_and_cannot_mark_unrelated_wo(db):
    seed(db)
    seed(db, wo="FN10142026-Phoenix AZ-2", cluster_hash="fn-hash-2", posted=True)
    with pytest.raises(ValueError, match="already Posted"):
        da.record_fn_csv_download(db, [
            {"work_order": "FN10142026-Phoenix AZ-1", "cluster_hash": "fn-hash-1"},
            {"work_order": "FN10142026-Phoenix AZ-2", "cluster_hash": "fn-hash-2"},
        ], csv_sha256=TEST_SHA, actor="associate-test")
    assert not da.fn_csv_downloaded(fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"])
    with pytest.raises(ValueError, match="download the current CSV"):
        da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")


def test_mismatch_route_id_does_not_mark_wrong_wo(db):
    seed(db)
    with pytest.raises(ValueError, match="identity changed"):
        da.record_fn_csv_download(db, [
            {"work_order": "FN10142026-Phoenix AZ-1", "cluster_hash": "another-wo"}
        ], csv_sha256=TEST_SHA, actor="associate-test")
    assert not da.fn_csv_downloaded(fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"])


@pytest.mark.parametrize("handoff", ["queued", "running", "error"])
def test_incomplete_handoff_not_downloadable_or_postable(db, handoff):
    seed(db, handoff=handoff)
    with pytest.raises(ValueError, match="OnFleet handoff is incomplete"):
        acknowledge(db)
    assert not da.fn_csv_downloaded(fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"])


def test_invalid_or_empty_csv_cannot_set_green_check(db):
    seed(db)
    with pytest.raises(ValueError, match="Invalid downloaded CSV checksum"):
        da.record_fn_csv_download(db, [{
            "work_order": "FN10142026-Phoenix AZ-1",
            "cluster_hash": "fn-hash-1"}], csv_sha256="", actor="associate-test")
    with pytest.raises(ValueError, match="between 1 and 500"):
        da.record_fn_csv_download(db, [], csv_sha256=TEST_SHA, actor="associate-test")


def test_editing_saved_route_after_download_invalidates_check_and_post(db):
    seed(db)
    acknowledge(db)
    with db.begin() as conn:
        conn.execute(sa.text("""
            UPDATE field_nation_orders
            SET payload = jsonb_set(payload, '{due}', to_jsonb(CAST('2026-11-01' AS text)), true)
            WHERE work_order = 'FN10142026-Phoenix AZ-1'
        """))
    current = fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"]
    assert current["fn_csv_downloaded_at"]
    assert not da.fn_csv_downloaded(current)
    with pytest.raises(ValueError, match="download the current CSV"):
        da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")
    acknowledge(db)
    assert da.fn_csv_downloaded(fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"])


def test_history_restore_clears_checkmark_and_requires_fresh_csv(db):
    seed(db)
    acknowledge(db)
    da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")
    posted = fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"]["fn_posted_ts"]
    result = restore_posted(
        db, {"FN10142026-Phoenix AZ-1": posted},
        {"username": "associate-test", "scope": "field_nation"},
        ["Orange"], POD_CONFIGS)
    assert len(result["reverted"]) == 1
    after = fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"]
    assert not after.get("fn_posted_ts")
    assert not after.get("fn_csv_downloaded_at")
    assert not da.fn_csv_downloaded(after)
    with pytest.raises(ValueError, match="download the current CSV"):
        da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")


def test_legacy_restore_also_clears_download_confirmation(db):
    seed(db)
    acknowledge(db)
    da.mark_fn_posted(db, "FN10142026-Phoenix AZ-1")
    result = da.revert_fn_posted_to_pending(db, ["FN10142026-Phoenix AZ-1"])
    assert len(result["reverted"]) == 1
    assert not da.fn_csv_downloaded(fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"])


APP = r'''
import os
from unittest.mock import patch
from types import SimpleNamespace
import streamlit as st
import sqlalchemy as sa
import revamp_workspace as rw
from migration import data_access as da

# Streamlit AppTest can click ordinary buttons, not native download_button.
# Replace only the download control with a real button that retains exactly
# the same on_click, args, key and disabled flag for an end-to-end UI check.
def testing_download_button(label, *, on_click=None, args=None, key=None, disabled=False, **kw):
    return st.button(label, key=key, disabled=disabled,
                     on_click=on_click if callable(on_click) else None,
                     args=args or ())
st.download_button = testing_download_button

st.session_state.setdefault('_auth_user', {
    'scope': 'field_nation', 'pod': 'Field Nation', 'username': 'fn_csv_assoc',
    'role': 'Field Nation Dispatch Associate', 'tier': 'guest'})

engine = sa.create_engine(os.environ['FN_CSV_TEST_DATABASE_URL'])
config = {'Orange': {'states': ['AZ']}}
class Fetch:
    def __call__(self):
        return da.get_sent_records_from_db(engine, config, {'ARIZONA': 'AZ'})
    def clear(self):
        return None
def no_bundle(*a, **kw): return []

with patch.object(rw, '_load_bundle_labels', no_bundle), \
     patch.object(rw, '_pod_load_locks', lambda: {}), \
     patch('revamp_fn_queue.recover_queued', lambda *a, **kw: None):
    rw.render_workspace(
        lambda p: True, lambda *a, **kw: None, lambda *a: None,
        lambda *a: 0, engine, lambda *a, **kw: None, Fetch(),
        pod_configs=config)
'''


def test_associate_route_card_shows_green_check_after_csv_click_and_posts(db, tmp_path):
    from streamlit.testing.v1 import AppTest
    seed(db)
    file = tmp_path / "fn_csv_ui_test.py"
    file.write_text(APP, encoding="utf-8")
    at = AppTest.from_file(str(file), default_timeout=25).run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.radio(key="revamp_status").value == "Field Nation"

    # Pending route must be selected before the download is available.
    at.checkbox(key="revamp_fn_Orange:fn-hash-1").check().run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.button(key="revamp_fn_csv").disabled is False
    assert at.button(key="revamp_fn_posted").disabled is True

    # Clicking the mocked download executes the SAME production callback.
    at.button(key="revamp_fn_csv").click().run()
    assert not at.exception, [e.message for e in at.exception]
    assert da.fn_csv_downloaded(fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"])
    assert not at.button(key="revamp_fn_posted").disabled
    labels = [button.label for button in at.button]
    assert any("✅" in label and "CSV downloaded" in label and "Phoenix" in label
               for label in labels), labels

    at.button(key="revamp_fn_posted").click().run()
    assert not at.exception, [e.message for e in at.exception]
    assert fetch_row(db, "FN10142026-Phoenix AZ-1")["payload"]["fn_posted_ts"]
