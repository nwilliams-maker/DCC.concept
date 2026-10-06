import pytest
import sqlalchemy as sa

from migration import email_dispatch_control as control


def test_pause_persists_across_sessions_and_stale_session_rechecks(tmp_path):
    url = f'sqlite:///{tmp_path / "control.db"}'
    admin_engine, dispatcher_engine = sa.create_engine(url), sa.create_engine(url)
    assert not control.email_dispatch_paused(dispatcher_engine)
    control.set_email_dispatch_paused(admin_engine, True, {'tier': 'admin', 'email': 'dev@example.test'})
    assert control.email_dispatch_paused(dispatcher_engine)
    with pytest.raises(RuntimeError, match='paused'):
        control.require_email_dispatch_enabled(dispatcher_engine)
    control.set_email_dispatch_paused(admin_engine, False, {'pod': 'ADMIN'})
    control.require_email_dispatch_enabled(dispatcher_engine)


@pytest.mark.parametrize('user', [{}, {'tier': 'manager'}, {'pod': 'MANAGER'}, {'tier': 'user', 'pod': 'ADMIN'}])
def test_only_dev_admin_may_change_stop(user):
    with pytest.raises(PermissionError):
        control.set_email_dispatch_paused(None, True, user)


def test_unavailable_control_disables_email():
    assert control.email_dispatch_paused(None)
    with pytest.raises(RuntimeError):
        control.require_email_dispatch_enabled(None)


def test_existing_email_button_rechecks_pause_before_opening():
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[2])
    app = AppTest.from_string(f'''
import sys
sys.path.insert(0, {root!r})
import streamlit as st
from migration import email_dispatch_control as c
calls = []
def paused(engine):
    calls.append(1)
    return len(calls) > 1
c.email_dispatch_paused = paused
c.render_email_open(None, 'Open Outlook', 'https://outlook.example.test', 'open')
''').run()
    assert not app.exception
    assert not app.button(key='open').disabled
    app.button(key='open').click().run()
    assert not app.exception
    assert app.warning[0].value == 'Email dispatch is paused.'
    assert not app.get('iframe')


def test_dev_control_hidden_from_manager():
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[2])
    app = AppTest.from_string(f'''
import sys
sys.path.insert(0, {root!r})
from migration.email_dispatch_control import render_dev_email_control
render_dev_email_control(None, {{'tier': 'manager'}})
''').run()
    assert not app.exception
    assert not app.button
    assert not app.expander
