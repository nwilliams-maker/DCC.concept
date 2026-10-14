"""Regression checks for the exact dedicated Associate workspace (not Full tools)."""
import ast
import copy
import json
import os
from pathlib import Path
import sys

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def apply_integration(root=ROOT):
    path = root / 'revamp_workspace.py'
    text = path.read_text()
    marker = '    # Dedicated FN Associate History: do not change the other workspaces.\n'
    if marker in text:
        ast.parse(text)
        return
    old_nav = '    allowed_statuses = _workspace_statuses(auth_user)\n'
    new_nav = old_nav + marker + (
        '    if fn_only and auth_user.get("scope") == "field_nation":\n'
        '        allowed_statuses = ("Field Nation", "History")\n')
    old_view = '    matching = [entry for entry in all_routes if _entry_matches(entry, status, search)]\n'
    new_view = (
        '    if fn_only and auth_user.get("scope") == "field_nation" and status == "History":\n'
        '        from revamp_fn_history import render_history\n'
        '        render_history(db_engine, auth_user, selected_pods, pod_configs,\n'
        '                       fetch_sent_records_from_sheet, search=search)\n'
        '        return\n') + old_view
    assert text.count(old_nav) == 1, 'Navigation anchor changed; refusing a blind patch'
    assert text.count(old_view) == 1, 'Rendering anchor changed; refusing a blind patch'
    fixed = text.replace(old_nav, new_nav).replace(old_view, new_view)
    ast.parse(fixed)
    path.write_text(fixed)


def test_integration_is_scoped_and_preserves_cards():
    text = (ROOT / 'revamp_workspace.py').read_text()
    ast.parse(text)
    assert 'if fn_only and auth_user.get("scope") == "field_nation":' in text
    assert 'allowed_statuses = ("Field Nation", "History")' in text
    assert 'stage_columns = st.columns(3, gap="small")' in text
    assert 'zip(stage_columns, ("Pending", "Posted", "Assigned"))' in text
    assert 'st.session_state[stage_key] = stage_name' in text
    assert text.index('status == "History":') < text.index('matching = [entry for entry in all_routes')
    ast.parse((ROOT / 'tactical_workspace_master_rw.py').read_text())


APP = r'''
import copy
import sys
from types import SimpleNamespace
from unittest.mock import patch
import streamlit as st
import revamp_workspace as rw
import revamp_fn_history as history

st.session_state.setdefault('_auth_user', {'scope': 'field_nation', 'pod': 'Field Nation', 'username': 'fn_assoc', 'role': 'Field Nation Dispatch Associate', 'tier': 'guest'})

class Result:
    def scalar(self): return 0
class Connection:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, *args): return Result()
class Engine:
    def connect(self): return Connection()

class Fetch:
    def __call__(self):
        return {}, {**{p: [] for p in rw.PODS}, 'Global_Digital': [], '_fn_posted': {}, '_fn_provider': {}}, set(), {}
    def clear(self): pass

def fixture_rows():
    if '_fixture_history' not in st.session_state:
        st.session_state['_fixture_history'] = [
            {'Work Order': 'FN-test-' + state, 'Status': state, 'Pod': 'Orange',
             'Provider': 'Provider' if state == 'Assigned' else '',
             'Created (Central)': '2026-10-14 10:00:00 AM CDT',
             'Posted (Central)': '2026-10-14 11:00:00 AM CDT' if state != 'Pending' else '',
             'Assigned (Central)': '', 'Updated (Central)': '', 'Tasks': 2,
             '_posted': '2026-10-14T16:00:00+00:00' if state != 'Pending' else '',
             '_created': '2026-10-14T15:00:00+00:00', '_assigned': '', '_events': []}
            for state in ('Pending', 'Posted', 'Assigned')]
    return copy.deepcopy(st.session_state['_fixture_history'])

def recover(engine, expected, user, pods, configs):
    st.session_state['_fixture_recovered'] = list(expected)
    for r in st.session_state['_fixture_history']:
        if r['Work Order'] in expected:
            r['Status'] = 'Pending'
            r['_posted'] = ''
            r['Posted (Central)'] = ''
    return {'reverted': [{'work_order': wo, 'cluster_hash': 'hash'} for wo in expected], 'skipped': []}

with patch.dict(sys.modules, {
    'revamp_fn_queue': SimpleNamespace(recover_queued=lambda *a, **kw: None),
    'migration.data_access': SimpleNamespace(get_fn_assigned_orders=lambda *a: []),
    'fn_utils': SimpleNamespace(generate_combined_fn_upload=lambda *a: (None, 0, [])),
}), patch.object(rw, '_load_bundle_labels', lambda *a: []), \
    patch.object(history, 'load_records', lambda *a: fixture_rows()), \
    patch.object(history, 'restore_posted', recover):
    rw.render_workspace(
        lambda p: True, lambda *a, **kw: None, lambda *a: None,
        lambda *a: 0, Engine(), lambda *a, **kw: None, Fetch(),
        pod_configs={p: {'states': []} for p in rw.PODS})
'''


def _app(tmp_path):
    from streamlit.testing.v1 import AppTest
    path = tmp_path / 'app.py'
    path.write_text(APP)
    at = AppTest.from_file(str(path), default_timeout=20).run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def test_exact_associate_navigation_with_empty_live_queue(tmp_path):
    at = _app(tmp_path)
    assert at.radio(key='revamp_status').options == ['Field Nation', 'History']
    for stage in ('pending', 'posted', 'assigned'):
        assert at.button(key='revamp_fn_stage_' + stage)
        at.button(key='revamp_fn_stage_' + stage).click().run()
        assert not at.exception, [e.message for e in at.exception]
        assert at.session_state['revamp_fn_stage_filter'] == stage.title()
    at.radio(key='revamp_status').set_value('History').run()
    assert not at.exception, [e.message for e in at.exception]
    assert set(at.dataframe[0].value['Status']) == {'Pending', 'Posted', 'Assigned'}
    for stage in ('Pending', 'Posted', 'Assigned'):
        at.radio(key='fn_history_category').set_value(stage).run()
        assert not at.exception
        assert set(at.dataframe[0].value['Status']) == {stage}
    at.radio(key='revamp_status').set_value('Field Nation').run()
    assert not at.exception
    assert at.session_state['revamp_fn_stage_filter'] == 'Assigned'
    assert len([b for b in at.button if str(b.key).startswith('revamp_fn_stage_')]) == 3


def test_ui_requires_confirmation_and_only_offers_posted(tmp_path):
    at = _app(tmp_path)
    at.radio(key='revamp_status').set_value('History').run()
    assert at.button(key='fn_history_restore').disabled
    options = at.multiselect(key='fn_history_selection').options
    assert len(options) == 1 and 'FN-test-Posted' in options[0]
    at.multiselect(key='fn_history_selection').set_value(['FN-test-Posted']).run()
    assert not at.exception
    assert at.button(key='fn_history_restore').disabled
    confirm = next(c for c in at.checkbox if str(c.key).startswith('fn_history_confirm_'))
    confirm.check().run()
    assert not at.button(key='fn_history_restore').disabled
    at.button(key='fn_history_restore').click().run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.session_state['_fixture_recovered'] == ['FN-test-Posted']
    assert any('Returned 1 work order' in s.value for s in at.success)
    assert not at.multiselect(key='fn_history_selection').value


@pytest.fixture
def database():
    url = os.environ.get('FN_HISTORY_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Ephemeral PostgreSQL is provided in CI')
    assert sa.engine.make_url(url).host in ('localhost', '127.0.0.1'), 'Tests must never use production'
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text('DROP TABLE IF EXISTS field_nation_orders, route_events'))
        conn.execute(sa.text("CREATE TABLE field_nation_orders (work_order text PRIMARY KEY, status text CHECK(status IN ('posted','assigned')), provider text, payload jsonb, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), route_plan_id text)"))
        conn.execute(sa.text('CREATE TABLE route_events (id bigserial PRIMARY KEY, route_id bigint NULL, action text, payload jsonb)'))
    yield engine
    engine.dispose()


def seed(engine, wo, *, status='posted', provider='', posted='2026-10-14T16:00:00+00:00', pod='Orange'):
    p = {'pod': pod, 'taskIds': ['a', 'b'], 'stopData': '[{"addr":"preserve me"}]', 'comp': 123, 'cluster_hash': wo}
    if posted:
        p['fn_posted_ts'] = posted
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO field_nation_orders(work_order,status,provider,payload,route_plan_id) VALUES (:wo,:status,:provider,CAST(:payload AS jsonb),'KEEP-ONFLEET-PLAN')"),
                     dict(wo=wo,status=status,provider=provider,payload=json.dumps(p)))
    return p


def test_nine_exact_posted_records_recover_without_touching_other_work(database):
    import revamp_fn_history as h
    originals = {f'FN-{i}': seed(database, f'FN-{i}') for i in range(9)}
    seed(database, 'LEGIT-POSTED')
    seed(database, 'ASSIGNED', status='assigned', provider='Worker')
    seed(database, 'PROVIDER', provider='Worker')
    seed(database, 'PENDING', posted='')
    seed(database, 'OTHER-POD', pod='Purple')
    seed(database, 'REPOSTED', posted='2026-10-14T17:00:00+00:00')
    selected = {wo:'2026-10-14T16:00:00+00:00' for wo in list(originals)+['ASSIGNED','PROVIDER','PENDING','OTHER-POD','REPOSTED']}
    out = h.restore_posted(database, selected, {'scope':'field_nation','username':'fn_assoc'}, ['Orange'], {})
    assert {r['work_order'] for r in out['reverted']} == set(originals)
    assert len(out['skipped']) == 5
    with database.connect() as conn:
        rows = conn.execute(sa.text('SELECT * FROM field_nation_orders')).mappings().all()
        assert conn.execute(sa.text("SELECT count(*) FROM route_events WHERE action='bulkRevertFnState'")).scalar() == 1
    for row in rows:
        if row['work_order'] in originals:
            assert row['status'] == 'posted'
            assert row['route_plan_id'] == 'KEEP-ONFLEET-PLAN'
            assert 'fn_posted_ts' not in row['payload']
            event = row['payload']['fn_history'][0]
            assert event['actor'] == 'fn_assoc' and event['to'] == 'Pending'
            for key in ('taskIds','stopData','comp','cluster_hash'):
                assert row['payload'][key] == originals[row['work_order']][key]
        elif row['work_order'] != 'PENDING':
            assert row['payload']['fn_posted_ts']
    loaded = h.load_records(database, ['Orange'], {})
    assert len([r for r in loaded if r['Status']=='Pending']) == 10
    repeat = h.restore_posted(database, selected, {'scope':'field_nation'}, ['Orange'], {})
    assert not repeat['reverted']


def test_audit_failure_rolls_back_recovery(database):
    import revamp_fn_history as h
    seed(database, 'FN-one')
    with database.begin() as conn:
        conn.execute(sa.text('DROP TABLE route_events'))
    with pytest.raises(Exception):
        h.restore_posted(database, {'FN-one':'2026-10-14T16:00:00+00:00'}, {'scope':'field_nation'}, ['Orange'], {})
    with database.connect() as conn:
        p = conn.execute(sa.text("SELECT payload FROM field_nation_orders WHERE work_order='FN-one'")).scalar()
        assert p['fn_posted_ts'] and 'fn_history' not in p


def test_wrong_role_cannot_restore():
    import revamp_fn_history as h
    with pytest.raises(PermissionError):
        h.restore_posted(None, {'x':'time'}, {'scope':'pod','tier':'admin'}, ['Orange'], {})


if __name__ == '__main__' and '--apply' in sys.argv:
    apply_integration()
