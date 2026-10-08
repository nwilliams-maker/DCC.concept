"""Permissions and rendered FN-only navigation, including stale session state."""
import ast
import pytest
from pathlib import Path
from types import SimpleNamespace
from streamlit.testing.v1 import AppTest
from test_revamp import load_functions

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def restore_workspace_functions():
    import revamp_workspace as rw
    names = ('_route_status', '_eligible_ics', '_load_bundle_labels')
    originals = {name: getattr(rw, name) for name in names}
    yield
    for name, original in originals.items():
        setattr(rw, name, original)


def test_fn_access_is_cross_pod_without_admin_access():
    st = SimpleNamespace(session_state={'_auth_user': {'pod': 'Field Nation', 'tier': 'guest', 'scope': 'field_nation'}})
    access = load_functions('tactical_workspace_master_rw.py', ['_can_access_tab'], {'st': st})['_can_access_tab']
    for pod in ('Blue', 'Green', 'Orange', 'Purple', 'Red', 'Digital'):
        assert access(pod)
    assert not access('Global')
    st.session_state['_auth_user'] = {'pod': 'Orange', 'tier': 'guest'}
    assert access('Orange')
    assert not access('Purple')
    assert not access('Digital')


def test_routes_combine_ready_flagged_and_keep_cvs_separate():
    scope = load_functions('revamp_workspace.py', ['_entry_matches'], {'st': SimpleNamespace(session_state={}), '_searchable': lambda route: route['city']})
    match = scope['_entry_matches']
    for state in ('Ready', 'Flagged'):
        entry = ('Orange', {'city': 'houston'}, state, state, None)
        assert match(entry, 'Routes', '')
        assert not match(entry, 'Routes', 'phoenix')
        removal = ('Orange', {'city': 'houston', 'is_removal': True}, state, state, None)
        assert not match(removal, 'Routes', '')
        assert match(removal, 'CVS Removal', '')
    assert not match(('Orange', {'city': 'houston'}, 'Accepted', 'a', None), 'Routes', '')


def test_fn_state_groups_do_not_mix_pods_or_split_by_stage():
    group = load_functions('revamp_workspace.py', ['_route_list_groups'])['_route_list_groups']
    entries = [(pod, {'state': state}, 'Field Nation', task, None) for pod, state, task in
               [('Purple', 'TX', 'assigned'), ('Orange', 'TX', 'pending'), ('Orange', 'TX', 'posted'), ('Orange', 'AZ', 'az'), ('Global_Digital', 'TX', 'digital')]]
    groups = group(entries, 'Field Nation')
    assert list(groups) == [('Digital', 'TX'), ('Orange', 'AZ'), ('Orange', 'TX'), ('Purple', 'TX')]
    assert [e[3] for e in groups['Orange', 'TX']] == ['pending', 'posted']
    assert sum(map(len, groups.values())) == len(entries)


def test_guest_renders_only_fn_despite_stale_ready_view_and_without_task_build():
    source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import revamp_workspace as rw
st.session_state['_auth_user'] = {{'name': 'Field Nation Dispatch Associate', 'pod': 'Field Nation', 'tier': 'guest', 'scope': 'field_nation'}}
if '_seeded' not in st.session_state:
    st.session_state['_seeded'] = True
    st.session_state['revamp_status'] = 'Ready'
    st.session_state['_revamp_show_accepted_next'] = True
    st.query_params['view'] = 'Accepted'
def forbidden(*args, **kwargs):
    raise AssertionError('FN associate must not build or render normal dispatch routes')
def records():
    return {{}}, {{'Orange': [{{'hash': 'orange-tx', 'wo': 'FN10082026-1', 'status': 'field_nation', 'city': 'Houston', 'state': 'TX', 'stops': 1, 'task_ids': ['one']}}], 'Purple': [{{'hash': 'purple-tx', 'wo': 'FN10082026-2', 'status': 'field_nation', 'city': 'Dallas', 'state': 'TX', 'stops': 1, 'task_ids': ['two']}}]}}, set(), {{}}
records.clear = lambda: None
rw._load_bundle_labels = lambda *a: []
rw.render_workspace(lambda pod: pod in rw.PODS, forbidden, forbidden, forbidden, None, forbidden, records)
'''
    app = AppTest.from_string(source).run(timeout=15)
    assert not app.exception
    assert app.radio[0].options == ['Field Nation']
    assert app.radio[0].value == 'Field Nation'
    assert app.selectbox[0].value == 'All my pods'
    assert not any('Return' in button.label for button in app.button)
    buttons = {button.key: button for button in app.button}
    assert 'revamp_group_toggle_Field Nation_Orange_TX' in buttons
    assert 'revamp_group_toggle_Field Nation_Purple_TX' in buttons
    app.button(key='revamp_group_toggle_Field Nation_Purple_TX').click().run()
    assert not app.exception
    assert any('Dallas' in button.label for button in app.button)
    app.button(key='revamp_fn_saved_refresh').click().run()
    assert not app.exception
    assert app.radio[0].options == ['Field Nation']


def test_dispatch_navigation_has_one_routes_tab():
    tree = ast.parse((ROOT / 'revamp_workspace.py').read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'STATUSES' for t in node.targets))
    statuses = ast.literal_eval(assignment.value)
    assert 'Routes' in statuses
    assert all(tab not in statuses for tab in ('Ready', 'Flagged', 'Over 50 mi'))
    scope = load_functions('revamp_workspace.py', ['_workspace_statuses'], {'STATUSES': statuses})
    assert scope['_workspace_statuses']({'tier': 'user'}) == statuses
    assert scope['_workspace_statuses']({'role': 'Associate'}) == ('Field Nation',)


def test_routes_render_green_ready_and_red_flagged_cards_together():
    source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import revamp_workspace as rw
st.session_state['_auth_user'] = {{'pod': 'Orange', 'tier': 'user'}}
st.session_state.setdefault('revamp_status', 'Routes')
st.session_state['clusters_Orange'] = [{{'city': city, 'state': 'TX', 'stops': 1, 'status': status, 'is_removal': removal, 'data': [{{'id': city, 'full': city}}]}} for city, status, removal in [('Houston', 'Ready', False), ('Dallas', 'Flagged', False), ('Austin', 'Ready', True)]]
rw._route_status = lambda route, *args: route['status']
rw._eligible_ics = lambda *args: []
rw._load_bundle_labels = lambda *args: []
def records():
    return {{}}, {{}}, set(), {{}}
records.clear = lambda: None
rw.render_workspace(lambda pod: pod == 'Orange', lambda *a: None, lambda *a: None, lambda *a: 0, None, lambda *a: None, records)
'''
    app = AppTest.from_string(source).run()
    assert not app.exception
    assert app.radio[0].value == 'Routes'
    assert all(tab not in app.radio[0].options for tab in ('Ready', 'Flagged', 'Over 50 mi'))
    cards = [button for button in app.button if button.key.startswith('revamp_route_')]
    assert len(cards) == 2
    assert any('Houston' in card.label and 'Ready' in card.label for card in cards)
    assert any('Dallas' in card.label and 'Flagged' in card.label for card in cards)
    assert all('Austin' not in card.label for card in cards)
    app.radio[0].set_value('CVS Removal').run()
    assert not app.exception
    cards = [button for button in app.button if button.key.startswith('revamp_route_')]
    assert len(cards) == 1 and 'Austin' in cards[0].label


def test_fn_workflow_sections_render_within_each_pod_state():
    source = f'''
import sys
sys.path.insert(0, {str(ROOT)!r})
import streamlit as st
import revamp_workspace as rw
entries = [('Orange', {{'city': city, 'state': 'TX', 'stops': 1, 'data': [{{'id': key}}]}}, 'Field Nation', key, None) for city, key in [('Houston', 'pending'), ('Austin', 'posted'), ('Dallas', 'assigned')]]
rw._render_route_list(entries, 'Field Nation', {{'posted': '2026-10-08'}}, {{'assigned': 'Installer'}})
'''
    app = AppTest.from_string(source).run()
    assert not app.exception
    headings = [item.value for item in app.markdown if item.value.startswith('**')]
    assert headings == ['**Orange Pod**', '**Pending** · 1 route', '**Posted** · 1 route', '**Assigned** · 1 route']
    assert app.button(key='revamp_group_toggle_Field Nation_Orange_TX')
    assert len([button for button in app.button if button.key.startswith('revamp_route_')]) == 3
    app.button(key='revamp_group_toggle_Field Nation_Orange_TX').click().run()
    assert not app.exception
    assert not any(item.value.startswith('**Pending**') for item in app.markdown)
