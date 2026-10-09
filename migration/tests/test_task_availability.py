import json
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from migration.task_availability import TaskAssignmentConflict, assert_tasks_available
from migration import fn_side_effects as fx
from migration import data_access as da


def task(tid='a', **fields):
    return {'id': tid, 'state': 0, 'worker': None, 'metadata': [], **fields}


@pytest.mark.parametrize('fields', [
    {'state': 1}, {'state': 2}, {'state': 3}, {'state': 0, 'worker': 'other'},
    {'state': None}, {'state': True}, {'completionDetails': {'success': True}},
])
def test_assigned_active_completed_and_unknown_tasks_block(fields):
    with pytest.raises(TaskAssignmentConflict):
        assert_tasks_available(['a'], fetch_task=lambda _: task(**fields), wo='WO')


def test_entire_route_checked_before_mutation():
    seen = []
    def fetch(tid):
        seen.append(tid)
        return task(tid, state=1 if tid == 'b' else 0)
    with pytest.raises(TaskAssignmentConflict) as err:
        assert_tasks_available('a,b,a,c', fetch_task=fetch)
    assert sorted(seen) == ['a', 'b', 'c']
    assert err.value.conflicts[0]['taskId'] == 'b'


@pytest.mark.parametrize('response', [None, {}, {'id': 'wrong', 'state': 0}])
def test_invalid_lookup_blocks(response):
    with pytest.raises(TaskAssignmentConflict):
        assert_tasks_available('a', fetch_task=lambda _: response)


def test_lookup_failure_blocks():
    def fetch(_):
        raise TimeoutError()
    with pytest.raises(TaskAssignmentConflict, match='could not verify'):
        assert_tasks_available('a', fetch_task=fetch)


def test_exact_same_worker_and_wo_retry_allowed_but_other_wo_blocked():
    existing = task(state=1, worker='worker', metadata=[{'name':'WO_NAME','value':'WO'}])
    assert assert_tasks_available('a', fetch_task=lambda _: existing, wo='WO', worker_id='worker', allow_existing=True)['a'] == existing
    for wo, worker in [('OTHER', 'worker'), ('WO', 'other')]:
        with pytest.raises(TaskAssignmentConflict):
            assert_tasks_available('a', fetch_task=lambda _: existing, wo=wo, worker_id=worker, allow_existing=True)


def test_same_worker_manual_assignment_without_wo_metadata_is_reused():
    existing = task(state=1, worker='worker', metadata=[])
    assert assert_tasks_available('a', fetch_task=lambda _: existing, wo='NEW-WO', worker_id='worker', allow_existing=True)['a'] == existing
    with pytest.raises(TaskAssignmentConflict):
        assert_tasks_available('a', fetch_task=lambda _: existing, wo='NEW-WO', worker_id='different', allow_existing=True)


def test_same_worker_different_wo_remains_blocked():
    existing = task(state=1, worker='worker', metadata=[{'name': 'WO_NAME', 'value': 'OTHER-WO'}])
    with pytest.raises(TaskAssignmentConflict):
        assert_tasks_available('a', fetch_task=lambda _: existing, wo='NEW-WO', worker_id='worker', allow_existing=True)


@pytest.fixture
def assignment(monkeypatch):
    monkeypatch.setenv('ONFLEET_KEY', 'test-key')
    monkeypatch.setattr(fx, '_load_onfleet_phone_map', lambda **_: {'5551234567': 'target'})
    monkeypatch.setattr(fx.time, 'sleep', lambda _: None)
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Mock(status_code=200, json=lambda: task(url.rsplit('/',1)[-1]))
    monkeypatch.setattr(fx, 'onfleet_fetch_with_backoff', request)
    return calls


def test_unassigned_tasks_write_only_after_all_checked(assignment):
    result = fx.assign_tasks_to_worker('5551234567', 'a,b,a', 'WO', 100, '2026-10-01')
    assert result['success'] and result['assignedCount'] == 2
    assert [c[0] for c in assignment[:2]] == ['get', 'get']
    assert sum(c[0] == 'put' for c in assignment) == 4


def test_one_conflict_prevents_every_task_write(assignment, monkeypatch):
    writes = []
    def request(method,url,**kwargs):
        if method == 'put': writes.append(url)
        tid = url.rsplit('/',1)[-1]
        return Mock(status_code=200, json=lambda: task(tid, state=1 if tid == 'b' else 0, worker='other' if tid == 'b' else None))
    monkeypatch.setattr(fx, 'onfleet_fetch_with_backoff', request)
    result = fx.assign_tasks_to_worker('5551234567','a,b','WO',100,'2026-10-01')
    assert not result['success'] and result['assignmentBlocked']
    assert writes == []


def test_task_assigned_after_initial_check_is_not_overwritten(assignment, monkeypatch):
    reads = 0
    writes = []
    def request(method,url,**kwargs):
        nonlocal reads
        if method == 'put': writes.append(url)
        if method == 'get': reads += 1
        return Mock(status_code=200, json=lambda: task(state=0 if reads == 1 else 1, worker=None if reads == 1 else 'other'))
    monkeypatch.setattr(fx, 'onfleet_fetch_with_backoff', request)
    result = fx.assign_tasks_to_worker('5551234567','a','WO',100,'2026-10-01')
    assert not result['success'] and result['assignmentBlocked']
    assert writes == []


@pytest.mark.parametrize('table,status,wo_col', [('routes','sent','wo'),('routes','accepted','wo'),('field_nation_orders','posted','work_order'),('field_nation_orders','assigned','work_order')])
def test_database_reservations_block_other_routes(table,status,wo_col):
    engine=sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE routes (wo TEXT, status TEXT, payload TEXT)'))
        conn.execute(sa.text('CREATE TABLE field_nation_orders (work_order TEXT, status TEXT, payload TEXT)'))
        conn.execute(sa.text(f'INSERT INTO {table} ({wo_col},status,payload) VALUES (:wo,:status,:payload)'), {'wo':'OTHER','status':status,'payload':json.dumps({'taskIds':'a,b'})})
        with pytest.raises(TaskAssignmentConflict, match='OTHER'):
            da._assert_tasks_not_reserved(conn,['b'],'WO')
        da._assert_tasks_not_reserved(conn,['b'],'OTHER')
        da._assert_tasks_not_reserved(conn,['c'],'WO')


def test_finalized_history_does_not_reserve_reopened_tasks():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE routes (wo TEXT, status TEXT, payload TEXT)'))
        conn.execute(sa.text('CREATE TABLE field_nation_orders (work_order TEXT, status TEXT, payload TEXT)'))
        conn.execute(sa.text("INSERT INTO routes VALUES ('OLD-WO','finalized',:payload)"),
                     {'payload': json.dumps({'taskIds': 'a,b'})})
        da._assert_tasks_not_reserved(conn, ['b'], 'NEW-WO')
        assert conn.execute(sa.text("SELECT status FROM routes WHERE wo='OLD-WO'")).scalar() == 'finalized'


def test_blocked_acceptance_keeps_route_sent(monkeypatch):
    engine=sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE routes (wo TEXT, status TEXT, payload TEXT)'))
        conn.execute(sa.text("INSERT INTO routes VALUES ('WO','sent',:payload)"), {"payload": json.dumps({"comp":100})})
    monkeypatch.setattr(fx,'apply_onfleet_decision',lambda **_: {'onfleetSuccess':False,'assignmentBlocked':True,'onfleetMsg':'Already assigned in OnFleet'})
    result=da.process_decision(engine,'WO','accept','signature','','5551234567',task_ids='a')
    assert not result['success']
    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT status FROM routes')).scalar() == 'sent'


@pytest.mark.parametrize('fn', ['save_route','save_to_field_nation'])
def test_send_and_fn_check_onfleet_before_any_database_write(monkeypatch,fn):
    engine=Mock()
    def blocked(*args,**kwargs):
        raise TaskAssignmentConflict([{'taskId':'a','reason':'already assigned'}])
    monkeypatch.setattr(fx,'assert_tasks_available',blocked)
    with pytest.raises(TaskAssignmentConflict):
        if fn=='save_route': da.save_route(engine,'WO','IC',{'taskIds':'a'})
        else: da.save_to_field_nation(engine,'WO',{'taskIds':'a'})
    engine.begin.assert_not_called()


def test_pruning_removes_tasks_not_whole_venues_and_recounts():
    from migration.task_availability import remove_unavailable_tasks
    cluster={'data':[{'id':'a','full':'same','lat':1,'lon':2,'task_type':'Kiosk Install'},
                     {'id':'b','full':'same','lat':1,'lon':2,'task_type':'Kiosk Removal','escalated':True},
                     {'id':'c','full':'other','lat':3,'lon':4,'task_type':'Kiosk Install'}],
             'stops':2,'inst_count':2,'remov_count':1,'esc_count':1,'center':[3,4]}
    new=remove_unavailable_tasks([cluster],['a','c'])[0]
    assert [t['id'] for t in new['data']]==['b']
    assert (new['stops'],new['inst_count'],new['remov_count'],new['esc_count'])==(1,0,1,1)
    assert new['center']==[1,2]
    assert len(cluster['data'])==3
    assert remove_unavailable_tasks([cluster],['a','b','c'])==[]


def test_quiet_refresh_recalculates_pay_and_resumes_send():
    import ast, hashlib
    from pathlib import Path
    from types import SimpleNamespace
    source=Path(__file__).resolve().parents[2]/'tactical_workspace_master_rw.py'
    tree=ast.parse(source.read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_refresh_available_route')
    cluster={'data':[{'id':'a','full':'one','lat':1,'lon':2},{'id':'b','full':'two','lat':3,'lon':4}], 'stops':2,'center':[1,2]}
    old_hash=hashlib.md5('ab'.encode()).hexdigest();new_hash=hashlib.md5('b'.encode()).hexdigest()
    session={'clusters_Blue':[cluster],f'sel_Blue_{old_hash}_identity_v2':'id:123',f'last_sel_Blue_{old_hash}_identity_v2':'id:123',f'dd_Blue_{old_hash}':'due',f'_rate_master_Blue_{old_hash}':25}
    class Rerun(Exception): pass
    reruns=[]
    def rerun(**kwargs):
        reruns.append(kwargs)
        raise Rerun()
    scope={'time':__import__('time'),'hashlib':hashlib,'PAY_CAP':10000,'st':SimpleNamespace(session_state=session,rerun=rerun),'_fetch_onfleet_open_tasks_cached':Mock(),'_pod_cluster_store':lambda:{}}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),scope)
    with pytest.raises(Rerun): scope['_refresh_available_route'](cluster,'Blue',old_hash,['a'],action='send')
    assert reruns == [{'scope':'fragment'}]
    assert session[f'_pay_master_Blue_{new_hash}']==25
    assert session[f'_resume_generate_Blue_{new_hash}'] is True
    assert session[f'sel_Blue_{new_hash}_identity_v2']=='id:123'
    assert session[f'dd_Blue_{new_hash}']=='due'
    assert [t['id'] for t in session['clusters_Blue'][0]['data']]==['b']


def test_unassigned_task_with_historical_metadata_is_available():
    previous = task(metadata=[{'name':'WO_NAME','value':'OLD-WO'}])
    assert assert_tasks_available('a', fetch_task=lambda _: previous, wo='NEW-WO')['a']==previous


def test_partial_assignment_route_plan_excludes_tasks_not_assigned(monkeypatch):
    monkeypatch.setattr(fx,'assign_tasks_to_worker',lambda *a: {
        'success':True,'workerId':'worker','partial':True,'assignedTaskIds':['a','c']})
    plans=[]
    monkeypatch.setattr(fx,'create_onfleet_route',lambda wo,ids,worker: plans.append(ids) or {'success':True,'routeId':'route'})
    result=fx.apply_onfleet_decision(decision='accept',task_ids='a,b,c',stop_order='c,b,a',
        wo='WO',phone='5551234567',comp=100,due='2026-10-10')
    assert plans == [['c','a']]
    assert result['route_incomplete']


def test_route_plan_fresh_check_prevents_including_other_workers_task(monkeypatch):
    get=Mock(side_effect=[Mock(status_code=200,json=lambda:{'teams':['blue']}),
                          Mock(status_code=200,json=lambda:[{'id':'blue','name':'POD: Blue'}])])
    monkeypatch.setattr(fx.requests,'get',get)
    post=Mock()
    monkeypatch.setattr(fx.requests,'post',post)
    monkeypatch.setattr(fx,'_onfleet_auth_header',lambda:{})
    def conflict(*args,**kwargs):
        raise TaskAssignmentConflict([{'taskId':'a','reason':'already assigned elsewhere'}])
    monkeypatch.setattr(fx,'assert_tasks_available',conflict)
    result=fx.create_onfleet_route('WO',['a'],'worker')
    assert not result['success']
    post.assert_not_called()


def test_bulk_field_nation_quietly_prunes_and_commits_remaining_tasks(monkeypatch):
    import revamp_bulk_fn as bulk
    engine=sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE routes (wo TEXT, status TEXT, payload TEXT)'))
        conn.execute(sa.text('CREATE TABLE field_nation_orders (work_order TEXT PRIMARY KEY, status TEXT, payload TEXT, route_plan_id TEXT)'))
    checks=[]
    def available(ids,**kwargs):
        checks.append(ids)
        if 'b' in str(ids).split(','):
            raise TaskAssignmentConflict([{'taskId':'b','reason':'already assigned in OnFleet'}])
        return {}
    monkeypatch.setattr(fx,'assert_tasks_available',available)
    monkeypatch.setattr(fx,'push_fn_placeholder_to_monday',lambda *args:{'skipped':True})
    handed=[]
    def handoff(ids,team,**kwargs):
        handed.append(ids)
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE field_nation_orders SET route_plan_id='plan' WHERE work_order=:wo"),{'wo':kwargs['wo_name']})
    route={'city':'Chicago','state':'IL','center':[1,2],'stops':2,'data':[
        {'id':'a','full':'one','lat':1,'lon':2},{'id':'b','full':'two','lat':3,'lon':4}]}
    saved,skipped,errors=bulk.bulk_assign(engine,[('Blue',route)],'2026-10-10',handoff,'team','worker')
    assert len(saved)==1 and skipped==[] and errors==[]
    assert handed==[['a']]
    with engine.connect() as conn:
        payload=json.loads(conn.execute(sa.text('SELECT payload FROM field_nation_orders')).scalar())
    assert payload['taskIds']=='a' and payload['lCnt']==1 and payload['tCnt']==1
    assert [t['id'] for t in route['data']]==['a']


def test_field_nation_does_not_create_plan_for_partially_assigned_route():
    import ast, base64
    from pathlib import Path
    from types import SimpleNamespace
    from concurrent.futures import ThreadPoolExecutor
    source=Path(__file__).resolve().parents[2]/'tactical_workspace_master_rw.py'
    node=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='assign_tasks_to_fn_team')
    def check(ids,**kwargs):
        if ids==['b']: raise TaskAssignmentConflict([{'taskId':'b','reason':'already assigned elsewhere'}])
        return {}
    http=SimpleNamespace(put=Mock(return_value=Mock(status_code=200)),post=Mock())
    scope={'ONFLEET_KEY':'test','base64':base64,'json':json,'requests':http,
           '_da':SimpleNamespace(_fx=SimpleNamespace(assert_tasks_available=check)),
           '_log_err':lambda *args:None,'ThreadPoolExecutor':ThreadPoolExecutor}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),scope)
    scope['assign_tasks_to_fn_team'](['a','b'],'team','worker',wo_name='WO')
    http.post.assert_not_called()
    task_urls=[call.args[0] for call in http.put.call_args_list if '/tasks/' in call.args[0]]
    assert task_urls == ['https://onfleet.com/api/v2/tasks/a']
