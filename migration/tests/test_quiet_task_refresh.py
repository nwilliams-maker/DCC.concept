from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from migration.quiet_task_refresh import QuietTaskRefresh, reconcile_task_pool


def route(*ids):
    return {'data': [{'id': tid, 'full': tid, 'lat': 1, 'lon': 2} for tid in ids], 'stops': len(ids)}


def test_new_tasks_append_without_changing_bundle_or_edited_route():
    bundle = route('a', 'b')
    result, added, removed = reconcile_task_pool([bundle], [route('a', 'c'), route('b', 'd')])
    assert result[0] is bundle
    assert [[t['id'] for t in r['data']] for r in result] == [['a', 'b'], ['c'], ['d']]
    assert (added, removed) == (2, 0)


def test_partial_and_whole_removals_and_additions_together():
    old = [route('a', 'b'), route('c')]
    result, added, removed = reconcile_task_pool(old, [route('b', 'd')])
    assert [[t['id'] for t in r['data']] for r in result] == [['b'], ['d']]
    assert (added, removed) == (1, 2)
    assert len(old[0]['data']) == 2


def test_saved_routes_and_history_are_preserved_and_not_added_twice():
    saved = route('a', 'b')
    result, added, removed = reconcile_task_pool([saved], [route('a', 'c')], protected=lambda _: True)
    assert result[0] is saved
    assert (added, removed) == (1, 0)
    assert [t['id'] for t in result[1]['data']] == ['c']


def test_unchanged_pool_has_zero_counts_and_same_objects():
    original = route('a', 'b')
    result, added, removed = reconcile_task_pool([original], [route('a'), route('b')])
    assert result == [original] and result[0] is original
    assert (added, removed) == (0, 0)


def test_same_ids_correct_classification_and_split_mixed_bundle():
    original = {**route('ad', 'removal'), 'is_removal': True, 'bundle_count': 2}
    ad = {**route('ad'), 'is_removal': False}
    ad['data'][0]['task_type'] = 'New Ad'
    removal = {**route('removal'), 'is_removal': True}
    removal['data'][0]['task_type'] = 'Remove Kiosk'
    result, added, removed = reconcile_task_pool([original], [ad, removal])
    assert (added, removed) == (0, 0)
    assert [[t['id'] for t in r['data']] for r in result] == [['ad'], ['removal']]
    assert [r['is_removal'] for r in result] == [False, True]
    assert result[0]['remov_count'] == 0 and result[1]['remov_count'] == 1
    assert len(original['data']) == 2


def test_protected_offer_keeps_original_tasks_and_classification():
    original = {**route('a'), 'is_removal': True}
    fresh = {**route('a'), 'is_removal': False}
    assert reconcile_task_pool([original], [fresh], protected=lambda _: True)[0] == [original]


def test_duplicate_new_ids_are_added_once():
    result, added, removed = reconcile_task_pool([], [route('a', 'b'), route('a', 'c')])
    assert [t['id'] for r in result for t in r['data']] == ['a', 'b', 'c']
    assert (added, removed) == (3, 0)


def test_sessions_share_running_and_recent_checks_and_manual_refresh():
    now = [0]
    service = QuietTaskRefresh(clock=lambda: now[0])
    gate = Event()
    calls = []
    def build(pods, refresh):
        calls.append((pods, refresh))
        gate.wait(2)
        return {'Blue': [route('a')]}
    first = service.poll(['Blue'], build)
    assert service.poll(['Blue'], build, force=True) is first
    gate.set()
    first.result(2)
    assert service.poll(['Blue'], build) is first
    now[0] = 301
    second = service.poll(['Blue'], build)
    second.result(2)
    assert calls == [(('Blue',), True), (('Blue',), True)]
    third = service.poll(['Blue'], build, force=True)
    third.result(2)
    assert third is not second
    service.executor.shutdown()


def test_pod_switch_uses_same_fresh_source_and_failed_check_is_not_retried_every_tick():
    service = QuietTaskRefresh(clock=lambda: 0)
    refreshes = []
    def build(pods, refresh):
        refreshes.append(refresh)
        if 'Digital' in pods:
            raise RuntimeError('incomplete download')
        return {'Blue': []}
    service.poll(['Blue'], build).result(2)
    failed = service.poll(['Digital'], build)
    with pytest.raises(RuntimeError): failed.result(2)
    assert service.poll(['Digital'], build) is failed
    assert refreshes == [True, False]
    service.executor.shutdown()


def test_real_routes_fragment_has_small_control_change_notice_and_preserves_inputs():
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[2])
    source = f'''
import sys
sys.path.insert(0, {root!r})
import streamlit as st
import revamp_workspace as w
from concurrent.futures import Future

def route(tid):
    return {{'city':'Chicago','state':'IL','stops':1,'center':[1,2],
             'data':[{{'id':tid,'full':tid,'lat':1,'lon':2}}]}}
class Service:
    def __init__(self):
        self.future = Future()
        self.calls = 0
    def poll(self, pods, build, force=False):
        if force:
            self.calls += 1
            self.future = Future()
            self.future.set_result({{'Blue':[route('new')]}})
        return self.future
@st.cache_resource
def service(): return Service()
w._quiet_refresh_service = service
st.session_state.setdefault('clusters_Blue',[route('old')])
st.session_state.setdefault('revamp_selected_route','keep-selection')
w._render_route_list([], 'All', {{}}, {{}},
    (['Blue'], None, None, lambda:{{}}, lambda:{{}}, [('IC',1,2)], lambda *args:0,''))
st.text_input('Contractor',key='contractor')
st.number_input('Rate',key='rate',value=25)
st.date_input('Due',key='due')
'''
    app = AppTest.from_string(source).run()
    assert not app.exception
    assert len(app.get('progress')) == 0
    assert app.button(key='revamp_quiet_refresh').label == '↻'
    assert any('width:12px;height:12px' in m.value and 'animation:none' in m.value for m in app.markdown)
    assert any('border:0!important' in m.value and '.st-key-revamp_quiet_control button' in m.value for m in app.markdown)
    app.text_input(key='contractor').set_value('Michael').run()
    app.number_input(key='rate').set_value(30).run()
    due = app.date_input(key='due').value
    app.button(key='revamp_quiet_refresh').click().run()
    assert not app.exception
    assert app.text_input(key='contractor').value == 'Michael'
    assert app.number_input(key='rate').value == 30
    assert app.date_input(key='due').value == due
    assert app.session_state['revamp_selected_route'] == 'keep-selection'
    assert [t['id'] for r in app.session_state['clusters_Blue'] for t in r['data']] == ['new']
    assert any('1 task added · 1 task removed' in m.value for m in app.markdown)
    assert len(app.get('progress')) == 0
    app.button(key='revamp_quiet_refresh').click().run()
    assert not app.exception
    assert any('Up to date' in m.value for m in app.markdown)


def _quiet_check_scope(source, session, cap=False, fail_pod=None, saved_records=None):
    import ast
    import threading
    from concurrent.futures import Future
    from types import SimpleNamespace
    calls = []
    cache = {'Blue': {'sig': 'unchanged-ids', 'clusters': []}}
    def fetch(): return {'_hit_cap': cap}
    fetch.clear = lambda: calls.append('pull')
    def process(pod, warm_only=False):
        calls.append((pod, warm_only))
        assert 'sig' not in cache.get(pod, {})
        if fail_pod == pod: return False
        cache[pod] = {'clusters': [route('new-' + pod)]}
        return True
    class ImmediateService:
        def poll(self, pods, build, force=False):
            future = Future()
            try: future.set_result(build(pods, True))
            except Exception as exc: future.set_exception(exc)
            return future
    node = next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name == '_quiet_routes_check')
    scope = {'st': SimpleNamespace(session_state=session,button=lambda *a,**k:True),
             '_quiet_refresh_service':ImmediateService, '_route_hash':lambda r:'hash',
             '_routes_refresh_style':lambda *a:None,
             '_pod_load_locks':lambda:{'Blue':threading.Lock(),'Digital':threading.Lock()},
             'time': __import__('time'), 'datetime': __import__('datetime').datetime,
             'ZoneInfo': __import__('zoneinfo').ZoneInfo}
    exec(compile(ast.Module(body=[node], type_ignores=[]),'revamp_workspace.py','exec'),scope)
    notes = []
    placeholder = SimpleNamespace(markdown=lambda text, **kw: notes.append(text))
    scope['_quiet_routes_check'](['Blue','Digital'],process,lambda warm_only:process('Digital',warm_only),lambda:cache,fetch, placeholder, saved_records)
    assert 'Checking tasks' in notes[0]
    return calls


@pytest.mark.parametrize('cap,fail_pod', [(True,None),(False,'Digital')])
def test_incomplete_download_or_failed_pod_keeps_every_session_pool(cap,fail_pod):
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2]/'revamp_workspace.py').read_text()
    original = [route('old')]
    session = {'clusters_Blue':original,'global_digital_clusters':original}
    calls = _quiet_check_scope(source, session, cap, fail_pod)
    assert session['clusters_Blue'] is original
    assert session['global_digital_clusters'] is original
    assert session['_revamp_quiet_error']
    if cap: assert calls == ['pull']


def test_digital_background_builder_does_not_touch_session_or_render_progress():
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    import pandas as pd
    source = (Path(__file__).resolve().parents[2]/'tactical_workspace_master_rw.py').read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name == 'process_digital_pool')
    class NoUI:
        def __getattr__(self,name): raise AssertionError(f'Background accessed Streamlit {name}')
    store = {}
    scope = {'st':NoUI(),'pd':pd,'_fetch_onfleet_open_tasks_cached':lambda:{'tasks':[], 'target_team_ids':[], 'esc_team_ids':[]},
             '_cached_fetch_sent_records_from_db':lambda:({}, {}, set(), {}),
             '_warm_load_ic_df':lambda:pd.DataFrame(), '_routing_ic_pool':lambda df:(df,None,None),
             '_pod_cluster_store':lambda:store}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'tactical_workspace_master_rw.py','exec'),scope)
    assert scope['process_digital_pool'](warm_only=True) is True
    assert store == {'Digital':{'clusters':[]}}


def test_bundled_badge_live_saved_merged_and_registry_without_false_positives():
    import revamp_workspace as w
    assert w._bundle_label({'bundle_count':1}) == '🔗 Bundled'
    assert w._bundle_label({'_ghost_record':{'bundle_count':2}}) == '🔗 Bundled'
    assert w._bundle_label({'_ghost_record':{'_merged_count':2}}) == '🔗 Bundled'
    assert w._bundle_label({'_ghost_record':{'task_ids':['a','b']}},[{'a','b'}]) == '🔗 Bundled'
    assert w._bundle_label({'data':[{'id':'a'},{'id':'b'},{'id':'c'}]},[{'a','b'}]) == '🔗 Bundled'
    assert w._bundle_label({'data':[{'id':'a'}]},[{'a','b'}]) == ''
    assert w._bundle_label({'data':[{'id':'a'},{'id':'b'}]}) == ''
    assert w._bundle_label({'_ghost_record':{'_merged_count':1}}) == ''
    assert w._bundle_label({'bundle_count':'not a number'}) == ''


def test_bundle_identity_survives_postgres_reconstruction_for_every_saved_status():
    from datetime import datetime
    from migration import data_access as da
    import revamp_workspace as w
    for status in ('sent','accepted','declined','finalized','field_nation'):
        ghosts={}
        da._ingest_sent_record(p={'wo':'WO','taskIds':'a,b','bundle_count':1,'city':'Chicago','state':'IL','pod':'Blue'},
            c_name='Michael',dt_obj=datetime.now(),ts_display='10/01 01:00 PM',status_label=status,
            sent_dict={},ghost_routes=ghosts,fn_posted_dict={},fn_provider_dict={},history_db={},
            pod_configs={'Blue':{'states':{'IL'}}},state_map={})
        ghost=ghosts['Blue'][0]
        assert ghost['bundle_count']==1
        assert w._bundle_label({'_ghost_record':ghost})=='🔗 Bundled'


def test_bundled_cards_display_badge_and_are_searchable():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    root=str(Path(__file__).resolve().parents[2])
    source=f'''
import sys
sys.path.insert(0,{root!r})
import streamlit as st
import revamp_workspace as w
route={{'wo':'WO-BUNDLE','city':'Chicago','state':'IL','stops':2,'bundle_count':1,
        '_bundle_label':'🔗 Bundled','data':[{{'id':'a'}},{{'id':'b'}}]}}
matching=[('Blue',route,'Sent',w._route_hash(route),None)]
w._render_route_list(matching,'Sent',{{}},{{}})
st.write(w._searchable(route))
'''
    app=AppTest.from_string(source).run()
    assert not app.exception
    card=next(button for button in app.button if button.key.startswith('revamp_route_'))
    assert '**WO-BUNDLE** - Chicago, IL - → Sent · 🔗 Bundled' in card.label
    assert any('bundled' in item.value for item in app.markdown)


def test_bulk_field_nation_payload_keeps_bundle_identity():
    from revamp_bulk_fn import _payload
    payload=_payload({'bundle_count':2,'data':[]},'Blue','2026-10-10','WO','hash')
    assert payload['bundle_count']==2


@pytest.mark.parametrize('manual', [False, True])
def test_task_check_only_runs_on_click_and_timestamps_completion(manual):
    import revamp_workspace as w
    from concurrent.futures import Future
    from types import SimpleNamespace
    from unittest.mock import patch
    future = Future()
    future.set_result({'Blue': []})
    session = {'clusters_Blue': []}
    calls = []
    fake_st = SimpleNamespace(session_state=session, button=lambda *a, **k: manual)
    def poll(*args, **kwargs):
        calls.append(kwargs['force'])
        return future
    service = SimpleNamespace(poll=poll)
    with patch.object(w, 'st', fake_st), patch.object(w, '_quiet_refresh_service', lambda: service), patch.object(w, '_routes_refresh_style'):
        assert not w._quiet_routes_check(['Blue'], None, None, None, None)
    assert calls == ([True] if manual else [])
    if manual:
        assert session['_revamp_quiet_notice'][0].startswith('Up to date · ')
        assert session['_revamp_quiet_notice'][0].endswith(' CT')
    else:
        assert '_revamp_quiet_notice' not in session


def test_routes_and_summary_have_no_scheduled_reruns():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[2] / 'revamp_workspace.py').read_text())
    for name in ('_render_route_list', '_render_workspace_summary'):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert [ast.unparse(d) for d in node.decorator_list] == ['st.fragment']


@pytest.mark.parametrize('state', ['Ready', 'Flagged'])
def test_loaded_pending_cards_survive_empty_secondary_pool(state):
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[2])
    source = f"""
import sys
sys.path.insert(0, {root!r})
import streamlit as st
import revamp_workspace as w
route = {{'city':'Phoenix','state':'AZ','stops':1,'center':[1,2],
          'data':[{{'id':'loaded','full':'Phoenix','lat':1,'lon':2}}]}}
st.session_state['clusters_Orange'] = []
w._render_route_list([('Orange',route,{state!r},'loaded-hash',None)], {state!r}, {{}}, {{}},
    (['Orange'], None, None, lambda:{{}}, lambda:{{}}, [], lambda *a:0, ''))
"""
    app = AppTest.from_string(source).run()
    assert not app.exception
    assert any('Phoenix, AZ' in b.label and state in b.label for b in app.button)


def test_manual_check_rebuilds_same_id_cache_and_reports_completion():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'revamp_workspace.py').read_text()
    session = {'clusters_Blue': [route('old')], 'global_digital_clusters': []}
    calls = _quiet_check_scope(source, session)
    assert calls == ['pull', ('Blue', True), ('Digital', True)]
    assert session['_revamp_quiet_notice'][0].endswith(' CT')
    assert 'tasks added' in session['_revamp_quiet_notice'][0]
    assert not session['_revamp_quiet_error']


def test_same_id_reclassification_restores_ready_card_after_manual_check():
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[2])
    app = AppTest.from_string(f'''
import sys
sys.path.insert(0, {root!r})
import streamlit as st
import revamp_workspace as w
from concurrent.futures import Future
def route(removal):
    return {{'city':'Phoenix','state':'AZ','center':[1,2], 'stops':1,
        'is_removal':removal,'data':[{{'id':'same-id','full':'101 Main St',
        'task_type':'New Ad','lat':1,'lon':2,'city':'Phoenix','state':'AZ'}}]}}
class Service:
    def poll(self, pods, build, force=False):
        future = Future()
        future.set_result({{'Orange':[route(False)]}})
        return future
w._quiet_refresh_service = Service
st.session_state.setdefault('clusters_Orange',[route(True)])
w._render_route_list([], 'Ready', {{}}, {{}},
    (['Orange'],None,None,lambda:{{}},lambda:{{}},[('IC',1,2)],lambda *a:0,''))
st.text_input('Contractor', key='contractor')
''').run()
    assert not app.exception
    assert app.info[0].value == 'No matching routes.'
    app.text_input(key='contractor').set_value('Michael').run()
    app.button(key='revamp_quiet_refresh').click().run()
    assert not app.exception
    assert app.text_input(key='contractor').value == 'Michael'
    assert any('Phoenix, AZ' in b.label and 'Ready' in b.label for b in app.button)
    assert not app.session_state['clusters_Orange'][0]['is_removal']
    assert any('Routes updated' in m.value for m in app.markdown)


@pytest.mark.parametrize('still_reserved', [False, True])
def test_manual_refresh_reads_archives_before_protecting_old_routes(still_reserved):
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'revamp_workspace.py').read_text()
    record = {'wo': 'OLD-WO', 'status': 'sent'}
    session = {'clusters_Blue': [route('old')], 'route_state_hash': 'email_sent',
               'sent_db': {'old': record}}
    def saved():
        return ({'old': record} if still_reserved else {}), {}, {'OLD-WO'}, {}
    saved.clear = lambda: None
    _quiet_check_scope(source, session, saved_records=saved)
    if still_reserved:
        assert session['route_state_hash'] == 'email_sent'
        assert session['clusters_Blue'][0]['data'][0]['id'] == 'old'
    else:
        assert 'route_state_hash' not in session
        assert all(t['id'] != 'old' for r in session['clusters_Blue'] for t in r['data'])
