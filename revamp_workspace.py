"""An isolated, list/detail dispatch workspace for the DCC revamp.

The existing DCC application is kept in the same checkout under Full tools.
This module only selects one live route for the existing dispatch renderer,
so a dashboard interaction does not build every dispatch card at once.
"""

import hashlib
import html
from datetime import date, timedelta

import streamlit as st


STATUSES = ("All", "Ready", "Flagged", "Over 50 mi", "Selected", "Routed", "Accepted")
PODS = ("Blue", "Green", "Orange", "Purple", "Red")


def _route_hash(route):
    ids = sorted(str(task.get("id", "")).strip() for task in route.get("data", []))
    return hashlib.md5("".join(ids).encode()).hexdigest()


def _route_status(route, sent_db, nearest_miles=None):
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
    if route.get("status") == "Flagged" or (nearest_miles is not None and nearest_miles > 50):
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


def _eligible_ics(ic_df):
    if ic_df is None or ic_df.empty:
        return []
    columns = {str(c).strip().lower(): c for c in ic_df.columns}
    if not all(k in columns for k in ("lat", "lng", "ic list")):
        return []
    result = []
    for _, row in ic_df.iterrows():
        if str(row.get(columns["ic list"], "")).strip().upper() not in (
            "ACTIVE", "IN TRAINING", "NEED INSURANCE"
        ):
            continue
        try:
            latitude = float(row[columns["lat"]])
            longitude = float(row[columns["lng"]])
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
        except (ValueError, TypeError):
            continue
        result.append((str(row.get(columns.get("name"), "Unknown")), latitude, longitude))
    return result


def _nearest_ic(route, eligible_ics, haversine):
    center = route.get("center")
    if not center or len(center) != 2:
        return None
    distances = []
    for name, latitude, longitude in eligible_ics:
        distance = haversine(center[0], center[1], latitude, longitude)
        if distance is not None and 0 <= distance < float("inf"):
            distances.append((name, distance))
    return min(distances, key=lambda row: row[1]) if distances else None


def _select_visible(keys):
    for key in keys:
        st.session_state[f"revamp_bulk_{key}"] = True


def _clear_selection(keys):
    for key in keys:
        st.session_state[f"revamp_bulk_{key}"] = False


def render_workspace(can_access_tab, process_pod, render_dispatch,
                     haversine, db_engine, assign_tasks_to_fn_team,
                     fetch_sent_records_from_sheet, default_due_days=14):
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
    eligible_ics = _eligible_ics(st.session_state.get("ic_df"))
    all_routes = []
    seen_hashes = set()
    for pod in loaded:
        for route in st.session_state.get(f"clusters_{pod}", []):
            if route.get("is_digital"):
                continue
            nearest = _nearest_ic(route, eligible_ics, haversine)
            route_hash = _route_hash(route)
            seen_hashes.add(route_hash)
            all_routes.append((pod, route,
                               _route_status(route, sent_db, nearest[1] if nearest else None),
                               route_hash, nearest))
    # Accepted routes often leave Onfleet's unassigned feed; include the
    # persisted ghost records so they remain visible in this workspace.
    for pod in loaded:
        for ghost in (st.session_state.get("ghost_db", {}) or {}).get(pod, []):
            route_hash = str(ghost.get("hash") or "")
            if not route_hash or route_hash in seen_hashes:
                continue
            seen_hashes.add(route_hash)
            ghost_status = str(ghost.get("status", "")).lower()
            state = "Accepted" if ghost_status in ("accepted", "finalized") else "Routed"
            route = {
                "_is_ghost": True, "wo": ghost.get("wo", ""),
                "city": ghost.get("city", "Unknown"),
                "state": ghost.get("state", ""),
                "stops": ghost.get("stops", ghost.get("lCnt", 0)),
                "data": [],
            }
            all_routes.append((pod, route, state, route_hash, None))
    counts = {status: sum(1 for entry in all_routes if entry[2] == status)
              for status in STATUSES[1:]}
    counts["Over 50 mi"] = sum(1 for entry in all_routes
                                if entry[4] and entry[4][1] > 50 and
                                entry[2] in ("Ready", "Flagged"))
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
                 (status == "Over 50 mi" and entry[4] and entry[4][1] > 50
                  and entry[2] in ("Ready", "Flagged")) or
                 (status == "Selected" and f"{entry[0]}:{entry[3]}" == selected_key)) and
                (not search or search in _searchable(entry[1]))]
    st.caption(f"Showing {len(matching)} matching route{'s' if len(matching) != 1 else ''}")

    visible_keys = [f"{entry[0]}:{entry[3]}" for entry in matching
                    if entry[2] in ("Ready", "Flagged")]
    select_col, clear_col, due_col, action_col = st.columns([1, 1, 1.4, 1.7],
                                                           vertical_alignment="bottom")
    with select_col:
        st.button("Select visible", key="revamp_select_visible",
                  on_click=_select_visible, args=(visible_keys,),
                  disabled=not visible_keys, use_container_width=True)
    with clear_col:
        st.button("Clear", key="revamp_clear_selection", on_click=_clear_selection,
                  args=([f"{e[0]}:{e[3]}" for e in all_routes],),
                  use_container_width=True)
    with due_col:
        fn_due = st.date_input("Field Nation due", value=date.today() + timedelta(days=default_due_days),
                               key="revamp_fn_due")
    chosen = [entry for entry in all_routes if entry[2] in ("Ready", "Flagged")
              and st.session_state.get(f"revamp_bulk_{entry[0]}:{entry[3]}", False)]
    fn_team_id = st.session_state.get("_fn_team_id")
    fn_worker_id = st.session_state.get("_fn_worker_id")
    with action_col:
        assign_clicked = st.button(f"Assign {len(chosen)} to Field Nation",
                                   key="revamp_assign_fn", type="primary",
                                   disabled=not chosen or db_engine is None or
                                            not fn_team_id or not fn_worker_id,
                                   use_container_width=True)
    if db_engine is None:
        st.caption("Field Nation assignment needs the new Railway database connection.")
    elif not fn_team_id or not fn_worker_id:
        st.caption("Field Nation team and worker must be loaded before bulk assignment.")
    if assign_clicked:
        from revamp_bulk_fn import bulk_assign
        saved, skipped, errors = bulk_assign(
            db_engine, [(pod, route) for pod, route, _, _, _ in chosen], fn_due,
            assign_tasks_to_fn_team, fn_team_id, fn_worker_id,
        )
        for route_hash, _ in saved:
            st.session_state[f"route_state_{route_hash}"] = "field_nation"
            st.session_state[f"reverted_{route_hash}"] = False
        _clear_selection([f"{pod}:{route_hash}" for pod, _, _, route_hash, _ in chosen])
        if saved:
            fetch_sent_records_from_sheet.clear()
            st.success(f"Saved {len(saved)} route(s) to Field Nation.")
        if skipped:
            st.info(f"{len(skipped)} route(s) were already assigned.")
        if errors:
            st.error(f"{len(errors)} route(s) could not be assigned: " +
                     "; ".join(msg for _, msg in errors[:3]))

    left, right = st.columns([1, 3], gap="small")
    with left:
        st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
        with st.container(height=650, border=True):
            if not matching:
                st.info("No matching routes.")
            for pod, route, state, route_hash, nearest in matching:
                key = f"{pod}:{route_hash}"
                city = route.get("city") or "Unknown city"
                label = (f"{city}, {route.get('state', '')} · {state}"
                         f"\n{pod} pod · {route.get('stops', 0)} stops · "
                         f"{len(route.get('data', []))} tasks")
                if nearest:
                    label += f"\nClosest IC: {nearest[0]} · {nearest[1]:.1f} mi"
                select_cell, route_cell = st.columns([0.13, 0.87], vertical_alignment="center")
                with select_cell:
                    if state in ("Ready", "Flagged"):
                        st.checkbox("Select for FN", key=f"revamp_bulk_{key}",
                                    label_visibility="collapsed")
                with route_cell:
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
        pod, route, state, route_hash, nearest = current
        title = f"{route.get('city', 'Route')}, {route.get('state', '')}"
        st.markdown(f"### {html.escape(title)}  ·  {html.escape(state)}")
        st.caption(f"{pod} pod · {route.get('stops', 0)} stops · "
                   f"{len(route.get('data', []))} tasks")
        if nearest:
            st.caption(f"Closest eligible IC: {nearest[0]} · {nearest[1]:.1f} mi")
        if state in ("Ready", "Flagged"):
            # Reuse the existing contractor, compensation, routing, FN,
            # bundling and link-generation logic for the selected live route.
            render_dispatch(20000, route, pod)
        else:
            st.info("This route's follow-up actions are available in Full tools.")
            st.button("Open Full tools", key="revamp_open_tools",
                      on_click=_open_full_tools)
