"""Completed work orders must not reserve reopened OnFleet tasks."""
from datetime import datetime
from pathlib import Path

import pytest
from migration import data_access as da


@pytest.mark.parametrize('active_status', ['sent', 'accepted', 'field_nation'])
@pytest.mark.parametrize('finalized_first', [True, False])
def test_finalized_history_never_overwrites_active_reservation(active_status, finalized_first):
    sent, ghosts, history = {}, {}, {}
    statuses = ['finalized', active_status] if finalized_first else [active_status, 'finalized']
    for status in statuses:
        da._ingest_sent_record(
            p={'wo': status + '-WO', 'taskIds': 'task', 'city': 'Chicago', 'state': 'IL'},
            c_name='Contractor', dt_obj=datetime.now(), ts_display='10/06 08:00 PM',
            status_label=status, sent_dict=sent, ghost_routes=ghosts,
            fn_posted_dict={}, fn_provider_dict={}, history_db=history,
            pod_configs={'Blue': {'states': {'IL'}}}, state_map={})
    assert sent['task']['status'] == active_status
    assert any(g['status'] == 'finalized' for g in ghosts['Blue'])
    assert {r['status'] for r in history['task']} == {active_status, 'finalized'}


def test_finalized_only_record_keeps_history_without_reserving_task():
    sent, ghosts, history = {}, {}, {}
    da._ingest_sent_record(
        p={'wo': 'OLD-WO', 'taskIds': 'task', 'city': 'Chicago', 'state': 'IL'},
        c_name='Contractor', dt_obj=datetime.now(), ts_display='10/06 08:00 PM',
        status_label='finalized', sent_dict=sent, ghost_routes=ghosts,
        fn_posted_dict={}, fn_provider_dict={}, history_db=history,
        pod_configs={'Blue': {'states': {'IL'}}}, state_map={})
    assert sent == {}
    assert ghosts['Blue'][0]['wo'] == 'OLD-WO'
    assert history['task'][0]['status'] == 'finalized'


def test_reopened_task_and_finalized_card_render_separately(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import revamp_workspace as w
    monkeypatch.setattr(w, '_nearest_ic', lambda *a: ('IC', 10))
    root = str(Path(__file__).resolve().parents[2])
    source = f'''
import sys
sys.path.insert(0, {root!r})
import streamlit as st
import revamp_workspace as w
route = {{'city':'Chicago', 'state':'IL', 'stops':1,
          'data':[{{'id':'task', 'full':'101 Main St', '_onfleet_unassigned':True}}]}}
route_hash = w._route_hash(route)
st.session_state.setdefault('clusters_Blue', [route])
st.session_state.setdefault('route_state_' + route_hash, 'finalized')
def records():
    return {{}}, {{'Blue':[{{'hash':route_hash, 'wo':'OLD-WO', 'status':'finalized',
        'city':'Chicago','state':'IL','task_ids':['task'],'stops':1}}]}}, set(), {{}}
records.clear = lambda: None
w.render_workspace(lambda pod: pod == 'Blue', lambda pod: None,
    lambda i, route, pod: st.write('Detail'), lambda *args: 0,
    None, lambda *args: None, records)
'''
    app = AppTest.from_string(source).run()
    assert not app.exception
    app.radio(key='revamp_status').set_value('Routes').run()
    assert not app.exception
    assert any('Chicago, IL' in b.label and 'Ready' in b.label for b in app.button)
    app.radio(key='revamp_status').set_value('Finalized').run()
    assert not app.exception
    assert any('OLD-WO' in b.label and 'Finalized' in b.label for b in app.button)
