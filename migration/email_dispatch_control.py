"""Shared email dispatch stop control. Reads are deliberately never cached."""
from functools import lru_cache
import json
import uuid

import sqlalchemy as sa


def is_dev_admin(user):
    tier = str((user or {}).get('tier') or '').strip().lower()
    return tier == 'admin' if tier else str((user or {}).get('pod') or '').upper() in ('ADMIN', 'ALL')


@lru_cache(maxsize=8)
def _ensure_table(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("""CREATE TABLE IF NOT EXISTS email_dispatch_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            paused BOOLEAN NOT NULL DEFAULT FALSE,
            changed_by TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""))
        conn.execute(sa.text("INSERT INTO email_dispatch_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))


def email_dispatch_paused(engine):
    # No database or an unreadable stop setting must never enable dispatch.
    if engine is None:
        return True
    try:
        _ensure_table(engine)
        with engine.connect() as conn:
            return bool(conn.execute(sa.text('SELECT paused FROM email_dispatch_control WHERE id = 1')).scalar_one())
    except Exception as exc:
        print(f'[email-control] unavailable: {type(exc).__name__}', flush=True)
        return True


def set_email_dispatch_paused(engine, paused, user):
    if not is_dev_admin(user):
        raise PermissionError('Only the dev admin can pause or resume email dispatch')
    if engine is None:
        raise RuntimeError('Railway database is unavailable')
    _ensure_table(engine)
    with engine.begin() as conn:
        conn.execute(sa.text('UPDATE email_dispatch_control SET paused = :paused, changed_by = :actor, updated_at = CURRENT_TIMESTAMP WHERE id = 1'),
                     {'paused': bool(paused), 'actor': str(user.get('email') or user.get('name') or 'admin')})


def require_email_dispatch_enabled(engine):
    if email_dispatch_paused(engine):
        raise RuntimeError('Email dispatch is paused or its control is unavailable')


def render_dev_email_control(engine, user):
    import streamlit as st
    if not is_dev_admin(user):
        return
    with st.expander('Dev tools'):
        paused = email_dispatch_paused(engine)
        st.caption('Email dispatch paused for everyone.' if paused else 'Email dispatch enabled.')
        if st.button('Resume email dispatch' if paused else 'Pause email dispatch',
                     key='dev_email_dispatch_control', disabled=engine is None):
            try:
                set_email_dispatch_paused(engine, not paused, user)
                st.rerun()
            except Exception as exc:
                st.error(f'Could not change email dispatch: {exc}')


def render_email_open(engine, label, url, key):
    """Server button rechecks the shared stop even in an already-open tab."""
    import streamlit as st
    paused = email_dispatch_paused(engine)
    if st.button(label, key=key, disabled=paused, use_container_width=True):
        if email_dispatch_paused(engine):
            st.warning('Email dispatch is paused.')
            return
        # Unique intent allows repeated deliberate opens, with no persistent href.
        st.components.v1.html(
            f'<script>/*{uuid.uuid4().hex}*/window.open({json.dumps(url)}, "_blank");</script>', height=0)
    if paused:
        st.caption('Email dispatch is paused.')
