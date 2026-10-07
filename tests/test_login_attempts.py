"""Exercise the real login form through repeated failures and a stale lockout."""
import ast
from pathlib import Path
from streamlit.testing.v1 import AppTest


def test_repeated_password_attempts_never_lock_out_and_correct_password_succeeds():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'tactical_workspace_master_rw.py').read_text())
    login = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_render_login_form')
    source = '''
import streamlit as st
if '_seeded' not in st.session_state:
    st.session_state['_seeded'] = True
    st.session_state['_login_lock_until'] = 10000000000
    st.session_state['_login_fails'] = 50
    st.session_state['_checks'] = 0

def _check_password(username, password):
    st.session_state['_checks'] += 1
    if username == 'test-user' and password == 'test-correct':
        return {'pod': 'Orange', 'role': 'Dispatcher', 'tier': 'user'}
    return None

def _stay_token_for(username):
    return None
''' + ast.unparse(login) + '''
if st.session_state.get('_auth_user'):
    st.success('Signed in')
else:
    _render_login_form()
'''
    app = AppTest.from_string(source).run()
    app.text_input[0].set_value('test-user')
    for attempt in range(10):
        app.text_input[1].set_value('wrong-password')
        app.button[0].click().run()
        assert not app.exception
        assert app.error[0].value == 'Invalid username or password.'
        assert app.session_state['_checks'] == attempt + 1
        assert '_auth_user' not in app.session_state
        assert '_login_lock_until' not in app.session_state
    app.text_input[1].set_value('test-correct')
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state['_auth_user']['username'] == 'test-user'
    assert app.session_state['_checks'] == 11
    assert app.success[0].value == 'Signed in'
