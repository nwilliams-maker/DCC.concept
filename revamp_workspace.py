"""An isolated, list/detail dispatch workspace for the DCC revamp.

The existing DCC application is kept in the same checkout under Full tools.
This module only selects one live route for the existing dispatch renderer,
so a dashboard interaction does not build every dispatch card at once.
"""

import hashlib
import html

import streamlit as st


STATUSES = ("All", "Ready", "Flagged", "Selected", "Routed", "Accepted")
PODS = ("Blue", "Green", "Orange", "Purple", "Red")


def _route_hash(route):
    ids = sorted(str(task.get("id", "")).strip() for task in route.get("data", []))
    return hashlib.md5("".join(ids).encode()).hexdigest()


def _route_status(route, sent_db):
    route_hash = _route_hash(route)
    local = st.session_state.get(f"route_state_{route_hash}")
    if local == "field_nation":
        return "Routed"
    if local == "email_sent":
        return "Routed"
    if local == "finalized":
        return "Accepted"
    if not st.session_state.get(f"reverted_{route_hash}"):
        match = next(
            (sent_db.get(str(t.get("id", "")).strip()) for t in route.get("data", [])
             if str(t.get("id", "")).strip() in sent_db),
            None,
        )
        if match:
            status = str(match.get("status", "")).lower()
            if status in ("accepted", "finalized"):
                return "Accepted"
            if status in ("sent", "field_nation", "declined"):
                return "Routed"
    if route.get("status") == "Flagged":
        return "Flagged"
    return "Ready"


def _searchable(route):
    fields = [route.get("city", ""), route.get("state", ""), route.get("wo", "")]
    for task in route.get("data", []):
        fields.extend(str(task.get(k, "")) for k in (
            "full", "venue_name", "vid", "sio", "kiosk_id", "zip", "id"
        ))
    return " ".join(str(field) for field in fields).lower()


def _open_full_tools():
    st.session_state["revamp_mode"] = "Full tools"


def render_workspace(can_access_tab, process_pod, render_dispatch):
    """Render one selected route while retaining the existing dispatch actions."""
    st.markdown("""
    <style>
    .revamp-heading {font-size:1.65rem;font-weight:750;color:#243047;margin:0 0 6px}
    .revamp-meta {font-size:.82rem;color:#52617c;margin:0 0 12px}
    .revamp-pill {border:1px solid #e3ddf2;border-radius:8px;padding:6px 10px;
                  font-size:.82rem;color:#43506c;background:#fff;display:inline-block;margin:0 6px 8px 0}
    .revamp-panel-title {font-weight:700;color:#2a3040;margin:8px 0}
    div[class*="st-key-revamp_route_"] button {text-align:left;justify-content:flex-start;
        min-height:4.4rem;border-radius:8px;border:1px solid #e8e2f3;
        background:#fff;color:#35405b;white-space:pre-wrap}
    div[class*="st-key-revamp_route_"] button:hover {border-color:#6841b0;background:#f8f4ff}
    </style>
    """, unsafe_allow_html=True)

    heading, search_col = st.columns([2, 3], vertical_alignment="center")
    with heading:
        st.markdown('<div class="revamp-heading">Dispatch</div>', unsafe_allow_html=True)
    with search_col:
        search = st.text_input(
            "Search routes", placeholder="Search venue, VID, city, state, ZIP, SIO or kiosk",
            label_visibility="collapsed", key="revamp_search",
        ).strip().lower()

    accessible = [pod for pod in PODS if can_access_tab(pod)]
    if not accessible:
        st.info("Your account has no static pod access. Open Full tools for your available workspaces.")
        return
    pod_options = (["All my pods"] if len(accessible) > 1 else []) + accessible
    filter_col, refresh_col = st.columns([3, 1], vertical_alignment="bottom")
    with filter_col:
        pod_choice = st.selectbox("Pod", pod_options, key="revamp_pod")
    selected_pods = accessible if pod_choice == "All my pods" else [pod_choice]
    with refresh_col:
        if st.button("↻ Sync routes", key="revamp_sync", use_container_width=True):
            for pod in selected_pods:
                process_pod(pod)
            st.rerun()

    loaded = [pod for pod in selected_pods if f"clusters_{pod}" in st.session_state]
    if not loaded:
        st.info("Routes are not loaded yet. Click Sync routes to fetch this pod's tasks.")
        return
    missing = [pod for pod in selected_pods if pod not in loaded]
    if missing:
        st.caption("Not loaded yet: " + ", ".join(missing) + ". Sync routes to add them.")

    sent_db = st.session_state.get("sent_db", {}) or {}
    all_routes = []
    for pod in loaded:
        for route in st.session_state.get(f"clusters_{pod}", []):
            if route.get("is_digital"):
                continue
            all_routes.append((pod, route, _route_status(route, sent_db), _route_hash(route)))
    counts = {status: sum(1 for entry in all_routes if entry[2] == status)
              for status in STATUSES[1:]}
    selected_key = st.session_state.get("revamp_selected_route")
    counts["Selected"] = sum(1 for entry in all_routes
                             if f"{entry[0]}:{entry[3]}" == selected_key)
    total_tasks = sum(len(route.get("data", [])) for _, route, _, _ in all_routes)
    st.markdown(
        f'<span class="revamp-pill">Routes <b>{len(all_routes)}</b></span>'
        f'<span class="revamp-pill">Tasks <b>{total_tasks}</b></span>'
        f'<span class="revamp-pill">Flagged <b>{counts["Flagged"]}</b></span>'
        f'<span class="revamp-pill">Accepted <b>{counts["Accepted"]}</b></span>',
        unsafe_allow_html=True,
    )

    status = st.radio("Route status", STATUSES, horizontal=True,
                      label_visibility="collapsed", key="revamp_status")
    matching = [entry for entry in all_routes if
                (status == "All" or entry[2] == status or
                 (status == "Selected" and f"{entry[0]}:{entry[3]}" == selected_key)) and
                (not search or search in _searchable(entry[1]))]
    st.caption(f"Showing {len(matching)} matching route{'s' if len(matching) != 1 else ''}")

    left, right = st.columns([1, 3], gap="small")
    with left:
        st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
        with st.container(height=650, border=True):
            if not matching:
                st.info("No matching routes.")
            for pod, route, state, route_hash in matching:
                key = f"{pod}:{route_hash}"
                city = route.get("city") or "Unknown city"
                label = (f"{city}, {route.get('state', '')} · {state}"
                         f"\n{pod} pod · {route.get('stops', 0)} stops · "
                         f"{len(route.get('data', []))} tasks")
                if st.button(label, key=f"revamp_route_{key}", use_container_width=True):
                    st.session_state["revamp_selected_route"] = key

    with right:
        selection = st.session_state.get("revamp_selected_route")
        current = next((entry for entry in matching if
                        f"{entry[0]}:{entry[3]}" == selection),
                       matching[0] if matching else None)
        if current is None:
            st.info("Select a route from the list.")
            return
        pod, route, state, route_hash = current
        title = f"{route.get('city', 'Route')}, {route.get('state', '')}"
        st.markdown(f"### {html.escape(title)}  ·  {html.escape(state)}")
        st.caption(f"{pod} pod · {route.get('stops', 0)} stops · "
                   f"{len(route.get('data', []))} tasks")
        if state in ("Ready", "Flagged"):
            # Reuse the existing contractor, compensation, routing, FN,
            # bundling and link-generation logic for the selected live route.
            render_dispatch(20000, route, pod)
        else:
            st.info("This route's follow-up actions are available in Full tools.")
            st.button("Open Full tools", key="revamp_open_tools",
                      on_click=_open_full_tools)
