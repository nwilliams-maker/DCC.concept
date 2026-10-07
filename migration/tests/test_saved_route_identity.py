"""A saved bundle and its live fragments are one dispatchable work order."""
import hashlib
import itertools
from types import SimpleNamespace
from pathlib import Path

from tests.test_revamp import load_functions


def dedupe():
    return load_functions('revamp_workspace.py',
        ['_dedupe_route_entries', '_route_entry_key', '_saved_route_fields'],
        {'hashlib': hashlib, 'st': SimpleNamespace(session_state={})})['_dedupe_route_entries']


def test_saved_bundle_wins_over_fragments_in_every_input_order():
    wo='Michael Maricle-10072026-3'
    first=('Orange', {'wo':wo, 'city':'Simi Valley',
        'data':[{'id':'a'}]}, 'Accepted', 'fragment-a', None)
    second=('Orange', {'wo':wo, 'city':'Arcadia',
        'data':[{'id':'b'}]}, 'Accepted', 'fragment-b', None)
    bundle=('Orange', {'wo':wo, '_is_ghost':True, '_ghost_record':{
        'wo':wo,'task_ids':['a','b'],'tasks':2,'stops':2}},
        'Accepted', 'bundle', None)
    for entries in itertools.permutations([first,second,bundle]):
        assert dedupe()(entries)==[bundle]


def test_numbered_work_orders_and_finalized_history_stay_distinct():
    entries=[('Orange',{'wo':f'Michael Maricle-10072026-{n}'},
        'Accepted','same-task-hash',None) for n in (1,2,3)]
    history=('Orange',{'wo':'Michael Maricle-10062026-1'},
        'Finalized','same-task-hash',None)
    assert dedupe()([*entries,history])==[*entries,history]


def test_same_saved_order_has_one_action_key_across_cluster_hashes():
    run=dedupe()
    a=('Orange',{'wo':'WO-1','data':[{'id':'a'}]},'Sent','a',None)
    b=('Orange',{'wo':'WO-1','data':[{'id':'a'},{'id':'b'}]},'Sent','b',None)
    assert run([a,b])==[b]


def test_workspace_renders_one_complete_accepted_card_for_split_bundle():
    from streamlit.testing.v1 import AppTest
    root=str(Path(__file__).resolve().parents[2])
    source=f'''
import sys
sys.path.insert(0, {root!r})
import streamlit as st
from revamp_workspace import render_workspace
wo='Michael Maricle-10072026-3'
routes=[{{'city':city,'state':'CA','stops':1,'data':[{{'id':tid,'full':city}}]}}
        for city,tid in [('Simi Valley','a'),('Arcadia','b')]]
st.session_state.setdefault('clusters_Orange', routes)
def records():
    sent={{tid:{{'wo':wo,'name':'Michael Maricle','status':'accepted'}} for tid in ['a','b']}}
    ghost={{'hash':'bundle','wo':wo,'status':'accepted','city':'Arcadia','state':'CA',
           'task_ids':['a','b'],'tasks':2,'stops':2,'contractor_name':'Michael Maricle'}}
    return sent,{{'Orange':[ghost]}},set(),{{}}
records.clear=lambda:None
render_workspace(lambda pod:pod=='Orange',lambda pod:None,
    lambda *args:None,lambda *args:0,None,lambda *args:None,records)
'''
    app=AppTest.from_string(source).run()
    app.radio(key='revamp_status').set_value('Accepted').run()
    assert not app.exception
    cards=[b for b in app.button if 'Michael Maricle-10072026-3' in b.label]
    assert len(cards)==1
    assert '2 stops' in cards[0].label
    assert '2 tasks' in cards[0].label
