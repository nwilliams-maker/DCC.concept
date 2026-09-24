"""An isolated, list/detail dispatch workspace for the DCC revamp.

The existing DCC application is kept in the same checkout under Full tools.
This module only selects one live route for the existing dispatch renderer,
so a dashboard interaction does not build every dispatch card at once.
"""

import hashlib
import html
import base64
import os
from datetime import date, datetime, timedelta

import requests
import streamlit as st


STATUSES = ("All", "Ready", "Flagged", "Over 50 mi", "Selected", "Field Nation", "Routed", "Accepted")
PODS = ("Blue", "Green", "Orange", "Purple", "Red")


def _route_hash(route):
    ids = sorted(str(task.get("id", "")).strip() for task in route.get("data", []))
    return hashlib.md5("".join(ids).encode()).hexdigest()


def _route_status(route, sent_db, nearest_miles=None):
    route_hash = _route_hash(route)
    local = st.session_state.get(f"route_state_{route_hash}")
    if local == "field_nation":
        return "Field Nation"
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
            if status == "field_nation":
                return "Field Nation"
            if status in ("sent", "declined"):
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


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_fn_assignment_ids():
    """Resolve Field Nation separately from the capped dispatch worker feed."""
    key = (os.environ.get("ONFLEET_KEY") or "").strip()
    if not key:
        raise RuntimeError("ONFLEET_KEY is not configured")
    auth = {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}
    teams_response = requests.get("https://onfleet.com/api/v2/teams", headers=auth, timeout=15)
    teams_response.raise_for_status()
    team_data = teams_response.json()
    teams = team_data if isinstance(team_data, list) else team_data.get("teams", [])
    team = next((t for t in teams if isinstance(t, dict)
                 and "field nation" in str(t.get("name", "")).lower()), None)
    if not team:
        raise RuntimeError("Field Nation team is missing from this Onfleet account")
    seen = set()
    last_id = None
    for _ in range(100):
        url = "https://onfleet.com/api/v2/workers" + (f"?lastId={last_id}" if last_id else "")
        response = requests.get(url, headers=auth, timeout=15)
        response.raise_for_status()
        data = response.json()
        workers = data if isinstance(data, list) else data.get("workers", [])
        if not workers:
            break
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            phone = "".join(c for c in str(worker.get("phone") or "") if c.isdigit())[-10:]
            if phone == "6302869764":
                return {"fn_team_id": team.get("id"), "fn_worker_id": worker.get("id")}
        next_id = (data.get("lastId") if isinstance(data, dict) else None) or workers[-1].get("id")
        if not next_id or next_id in seen:
            break
        seen.add(next_id)
        last_id = next_id
    raise RuntimeError("Field Nation placeholder worker (ending 9764) was not found in Onfleet")


def render_workspace(can_access_tab, process_pod, render_dispatch,
                     haversine, db_engine, assign_tasks_to_fn_team,
                     fetch_sent_records_from_sheet, default_due_days=14):
    """Render one selected route while retaining the existing dispatch actions."""
    st.markdown("""
    <style>
    .revamp-heading {font-size:1.65rem;font-weight:750;color:#243047;margin:0 0 6px}
    .revamp-meta {font-size:.82rem;color:#52617c;margin:0 0 12px}
    .revamp-pill {border:1px solid #dfe4ef;border-radius:7px;padding:6px 10px;
                  font-size:.78rem;color:#43506c;background:#fff;display:inline-block;margin:0 6px 8px 0}
    .revamp-pill b {color:#253454}
    .revamp-panel-title {font-weight:700;color:#2a3040;margin:8px 0}
    div[class*="st-key-revamp_status"] div[role="radiogroup"] {border:1px solid #dfe4ef;
        border-radius:11px;padding:8px 10px;gap:6px;background:#fff;flex-wrap:wrap}
    div[class*="st-key-revamp_status"] label {border-radius:9px;padding:7px 10px;
        cursor:pointer;white-space:nowrap}
    div[class*="st-key-revamp_status"] label:has(input:checked) {background:#f1eaff;color:#5730a3}
    div[class*="st-key-revamp_route_"] button {height:auto!important;min-height:4.4rem;
        border-radius:8px;border:1px solid #e8e2f3;background:#fff;color:#35405b;
        padding:10px 12px;text-align:left;justify-content:flex-start;white-space:normal}
    div[class*="st-key-revamp_route_"] button p {white-space:pre-line!important;
        overflow-wrap:anywhere;line-height:1.4;margin:0;text-align:left}
    div[class*="st-key-revamp_route_"] button:hover {border-color:#6841b0;background:#f8f4ff}
    div[class*="st-key-revamp_bulk_"] label {width:100%;cursor:pointer;align-items:flex-start}
    div[class*="st-key-revamp_bulk_"] label p {white-space:normal;overflow-wrap:anywhere;
        line-height:1.35;font-weight:650;color:#243047}
    </style>
    """, unsafe_allow_html=True)

    heading, search_col, tools_col = st.columns([1.6, 3, .7], vertical_alignment="center")
    with heading:
        st.markdown('<div class="revamp-heading">Dispatch</div>', unsafe_allow_html=True)
    with search_col:
        search = st.text_input(
            "Search routes", placeholder="Search venue, VID, city, state, ZIP, SIO or kiosk",
            label_visibility="collapsed", key="revamp_search",
        ).strip().lower()
    with tools_col:
        st.button("Full tools", key="revamp_top_tools", on_click=_open_full_tools,
                  use_container_width=True)

    accessible = [pod for pod in PODS if can_access_tab(pod)]
    if not accessible:
        st.info("Your account has no static pod access. Open Full tools for your available workspaces.")
        return
    pod_options = (["All my pods"] if len(accessible) > 1 else []) + accessible
    filter_col, _, refresh_col = st.columns([1.4, 4.5, 1], vertical_alignment="bottom")
    with filter_col:
        pod_choice = st.selectbox("Pod", pod_options, key="revamp_pod")
    selected_pods = accessible if pod_choice == "All my pods" else [pod_choice]
    with refresh_col:
        sync_clicked = st.button("↻ Sync routes", key="revamp_sync", use_container_width=True)
    if sync_clicked:
        fetch_sent_records_from_sheet.clear()
        for pod in selected_pods:
            process_pod(pod)

    # The original pod tabs populate these session keys during their own
    # render. This workspace runs before those tabs, so hydrate the same
    # Postgres-backed state here for routed and accepted cards.
    sent_db, ghost_db, archived_wos, history_db = fetch_sent_records_from_sheet()
    st.session_state["sent_db"] = sent_db
    st.session_state["ghost_db"] = ghost_db
    st.session_state["archived_wos"] = archived_wos
    st.session_state["_history_db"] = history_db

    loaded = [pod for pod in selected_pods if f"clusters_{pod}" in st.session_state]
    if not loaded:
        st.info("Routes are not loaded yet. Click Sync routes to fetch this pod's tasks.")
        return
    missing = [pod for pod in selected_pods if pod not in loaded]
    if missing:
        st.caption("Not loaded yet: " + ", ".join(missing) + ". Sync routes to add them.")

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
        for ghost in (ghost_db or {}).get(pod, []):
            route_hash = str(ghost.get("hash") or "")
            if not route_hash or route_hash in seen_hashes:
                continue
            seen_hashes.add(route_hash)
            ghost_status = str(ghost.get("status", "")).lower()
            state = ("Accepted" if ghost_status in ("accepted", "finalized") else
                     "Field Nation" if ghost_status in ("field_nation", "posted") else "Routed")
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
    counts["Selected"] = sum(1 for entry in all_routes
                             if st.session_state.get(f"revamp_bulk_{entry[0]}:{entry[3]}", False))
    counts["All"] = len(all_routes)
    total_tasks = sum(len(route.get("data", [])) for _, route, _, _, _ in all_routes)
    last_sync = st.session_state.get("_last_sync_ts")
    sync_age = "Sync available"
    if isinstance(last_sync, datetime):
        minutes = max(0, int((datetime.now() - last_sync).total_seconds() // 60))
        sync_age = f"Synced {minutes}m ago"
    unselected = sum(entry[2] in ("Ready", "Flagged") for entry in all_routes) - counts["Selected"]
    st.markdown(
        f'<span class="revamp-pill">◇ Pod <b>{html.escape(str(pod_choice))}</b></span>'
        f'<span class="revamp-pill">↻ {html.escape(sync_age)}</span>'
        f'<span class="revamp-pill">Routes <b>{len(all_routes)}</b></span>'
        f'<span class="revamp-pill">Tasks <b>{total_tasks}</b></span>'
        f'<span class="revamp-pill">Flagged <b>{counts["Flagged"]}</b></span>'
        f'<span class="revamp-pill">Unselected <b>{max(0, unselected)}</b></span>'
        f'<span class="revamp-pill">Field Nation <b>{counts["Field Nation"]}</b></span>'
        f'<span class="revamp-pill">Accepted <b>{counts["Accepted"]}</b></span>',
        unsafe_allow_html=True,
    )

    if st.session_state.pop("_revamp_show_fn_next", False):
        st.session_state["revamp_status"] = "Field Nation"
    status = st.radio("Route status", STATUSES, horizontal=True,
                      label_visibility="collapsed", key="revamp_status",
                      format_func=lambda option: f"{option}  {counts[option]}")
    show_cvs = st.toggle("Show CVS Kiosk Removal routes", value=False,
                         key="revamp_show_cvs_removal",
                         help="Show removal routes in Ready and Flagged. Routes already sent or assigned remain visible.")
    matching = [entry for entry in all_routes if
                (status == "All" or entry[2] == status or
                 (status == "Over 50 mi" and entry[4] and entry[4][1] > 50
                  and entry[2] in ("Ready", "Flagged")) or
                 (status == "Selected" and
                  st.session_state.get(f"revamp_bulk_{entry[0]}:{entry[3]}", False))) and
                (not search or search in _searchable(entry[1])) and
                (show_cvs or entry[2] not in ("Ready", "Flagged") or
                 not entry[1].get("is_removal"))]
    matching.sort(key=lambda entry: (str(entry[1].get("state") or "").upper(),
                                     str(entry[1].get("city") or "").lower(), entry[0]))
    visible_keys = [f"{entry[0]}:{entry[3]}" for entry in matching
                    if entry[2] in ("Ready", "Flagged")]
    select_col, clear_col = st.columns([5, 1], vertical_alignment="bottom")
    with select_col:
        st.button(f"☑ Select all {len(visible_keys)} matching on this tab", key="revamp_select_visible",
                  on_click=_select_visible, args=(visible_keys,),
                  disabled=not visible_keys, use_container_width=True)
    with clear_col:
        st.button("Clear selection", key="revamp_clear_selection", on_click=_clear_selection,
                  args=([f"{e[0]}:{e[3]}" for e in all_routes],),
                  use_container_width=True)
    st.caption(f"Showing {len(matching)} matching route{'s' if len(matching) != 1 else ''}")
    _, due_col, action_col = st.columns([2.5, 1.4, 1.7], vertical_alignment="bottom")
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
                                   disabled=not chosen or db_engine is None,
                                   use_container_width=True)
    if db_engine is None:
        st.caption("Field Nation assignment needs the new Railway database connection.")
    if assign_clicked:
        if not fn_team_id or not fn_worker_id:
            try:
                connection = _fetch_fn_assignment_ids()
                fn_team_id = connection.get("fn_team_id")
                fn_worker_id = connection.get("fn_worker_id")
                st.session_state["_fn_team_id"] = fn_team_id
                st.session_state["_fn_worker_id"] = fn_worker_id
            except Exception as exc:
                st.error(f"Could not load Field Nation from Onfleet: {exc}")
        if not fn_team_id or not fn_worker_id:
            st.error("Field Nation team or placeholder worker was not found in Onfleet. Selection is preserved; check the Onfleet team and worker setup.")
        else:
            from revamp_bulk_fn import bulk_assign
            saved, skipped, errors = bulk_assign(
                db_engine, [(pod, route) for pod, route, _, _, _ in chosen], fn_due,
                assign_tasks_to_fn_team, fn_team_id, fn_worker_id,
            )
            for route_hash, _ in saved:
                st.session_state[f"route_state_{route_hash}"] = "field_nation"
                st.session_state[f"reverted_{route_hash}"] = False
            _clear_selection([f"{pod}:{route_hash}" for pod, _, _, route_hash, _ in chosen
                              if route_hash in {saved_hash for saved_hash, _ in saved}])
            if saved:
                fetch_sent_records_from_sheet.clear()
                st.success(f"Saved {len(saved)} route(s) to Field Nation.")
            if skipped:
                st.info(f"{len(skipped)} route(s) were already assigned.")
            if errors:
                st.error(f"{len(errors)} route(s) could not be assigned: " +
                         "; ".join(msg for _, msg in errors[:3]))
            elif saved:
                st.session_state["_revamp_show_fn_next"] = True
                st.rerun()

    left, right = st.columns([2, 3], gap="medium")
    with left:
        st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
        with st.container(height=650, border=True):
            if not matching:
                st.info("No matching routes.")
            current_state = None
            for pod, route, state, route_hash, nearest in matching:
                state_name = str(route.get("state") or "Unknown state").strip().upper()
                if state_name != current_state:
                    current_state = state_name
                    state_count = sum(1 for entry in matching
                                      if str(entry[1].get("state") or "Unknown state").strip().upper() == state_name)
                    st.markdown(f"#### 📍 {html.escape(state_name)} · {state_count} routes")
                key = f"{pod}:{route_hash}"
                city = route.get("city") or "Unknown city"
                select_col, card_col = st.columns([.11, .89], vertical_alignment="center")
                with select_col:
                    if state in ("Ready", "Flagged"):
                        st.checkbox("Select route for Field Nation", key=f"revamp_bulk_{key}",
                                    label_visibility="collapsed")
                with card_col:
                    removal = " · CVS Removal" if route.get("is_removal") else ""
                    label = (f"{city}, {route.get('state', '')} · {state}{removal}\n"
                             f"{pod} pod · {route.get('stops', 0)} stops · "
                             f"{len(route.get('data', []))} tasks")
                    if nearest:
                        label += f"\nClosest IC: {nearest[0]} · {nearest[1]:.1f} mi"
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
            dispatch_route = dict(route)
            if nearest and nearest[1] > 50:
                dispatch_route["status"] = "Flagged"
            render_dispatch(20000, dispatch_route, pod)
        else:
            st.info("This route's follow-up actions are available in Full tools.")
            st.button("Open Full tools", key="revamp_open_tools",
                      on_click=_open_full_tools)
