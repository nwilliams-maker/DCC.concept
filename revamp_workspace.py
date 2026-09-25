"""An isolated, list/detail dispatch workspace for the DCC revamp.

The existing DCC application is kept in the same checkout under Full tools.
This module only selects one live route for the existing dispatch renderer,
so a dashboard interaction does not build every dispatch card at once.
"""

import hashlib
import html
import base64
import os
import time
from datetime import date, datetime, timedelta

import requests
import streamlit as st


STATUSES = ("All", "Ready", "Flagged", "Over 50 mi", "Selected", "Field Nation", "Sent", "Accepted", "Declined", "Routed")
PODS = ("Blue", "Green", "Orange", "Purple", "Red")


def _route_hash(route):
    ids = sorted(str(task.get("id", "")).strip() for task in route.get("data", []))
    return hashlib.md5("".join(ids).encode()).hexdigest()


def _route_status(route, sent_db, nearest_miles=None):
    route_hash = _route_hash(route)
    local = st.session_state.get(f"route_state_{route_hash}")
    persisted_status = ""
    if not st.session_state.get(f"reverted_{route_hash}"):
        match = next(
            (sent_db.get(str(t.get("id", "")).strip()) for t in route.get("data", [])
             if str(t.get("id", "")).strip() in sent_db),
            None,
        )
        if match:
            persisted_status = str(match.get("status", "")).lower()
    if persisted_status in ("accepted", "finalized"):
        return "Accepted"
    if persisted_status == "declined":
        return "Declined"
    if local == "field_nation" or persisted_status == "field_nation":
        return "Field Nation"
    if local == "email_sent" or persisted_status == "sent":
        return "Sent"
    if local == "declined":
        return "Declined"
    if local == "finalized":
        return "Accepted"
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


def _fn_stage(route_hash, posted, providers):
    if str(providers.get(route_hash) or "").strip():
        return "Assigned"
    return "Posted" if route_hash in posted else "Pending"


def _fn_csv_route(route, route_hash, ghost_to_cluster):
    if route.get("_is_ghost"):
        route = ghost_to_cluster(route.get("_ghost_record") or {}, skip_geocode=True) if ghost_to_cluster else None
    if not route or not route.get("data"):
        return None
    return {**route, "_cluster_hash": route_hash}


def _toggle_state_group(key):
    st.session_state[key] = not st.session_state.get(key, False)


def _refresh_bulk_actions():
    st.session_state["_revamp_refresh_bulk_actions"] = True


def _remember_view():
    status = st.session_state.get("revamp_status")
    if status in STATUSES:
        st.query_params["view"] = status


def _remember_pod():
    pod = st.session_state.get("revamp_pod")
    if pod:
        st.query_params["pod"] = pod


@st.fragment
def _render_route_list(matching, status, fn_posted, fn_providers):
    """State toggles rerun only this list; route clicks refresh the detail pane."""
    st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
    with st.container(height=650, border=False):
        if not matching:
            st.info("No matching routes.")
        grouped = {}
        for entry in matching:
            stage = _fn_stage(entry[3], fn_posted, fn_providers) if status == "Field Nation" else ""
            state_name = str(entry[1].get("state") or "Unknown state").strip().upper()
            grouped.setdefault((stage, state_name), []).append(entry)
        last_stage = None
        for group_index, ((stage, state_name), entries) in enumerate(grouped.items()):
            if status == "Field Nation" and stage != last_stage:
                st.markdown(f"**{stage}**")
                last_stage = stage
            group_key = f"_revamp_group_{status}_{stage}_{state_name}"
            st.session_state.setdefault(group_key, group_index == 0)
            is_open = st.session_state[group_key]
            st.button(f"{state_name}  ·  {len(entries)} {'route' if len(entries) == 1 else 'routes'}  {'−' if is_open else '+'}",
                      key=f"revamp_state_toggle_{status}_{stage}_{state_name}",
                      on_click=_toggle_state_group, args=(group_key,),
                      use_container_width=True)
            if not is_open:
                continue
            for pod, route, state, route_hash, nearest in entries:
                key = f"{pod}:{route_hash}"
                city = route.get("city") or "Unknown city"
                select_col, card_col = st.columns([.09, .91], gap="small", vertical_alignment="center")
                with select_col:
                    if state in ("Ready", "Flagged"):
                        st.checkbox("Select route for Field Nation", key=f"revamp_bulk_{key}",
                                    label_visibility="collapsed", on_change=_refresh_bulk_actions)
                    elif status == "Field Nation":
                        st.checkbox("Select for Field Nation CSV", key=f"revamp_fn_{key}",
                                    label_visibility="collapsed", on_change=_refresh_bulk_actions)
                with card_col:
                    removal = " · CVS Removal" if route.get("is_removal") else ""
                    card_state = stage if status == "Field Nation" else state
                    provider = str(fn_providers.get(route_hash) or "").strip()
                    provider_label = f" · FN: {provider}" if provider else ""
                    stops = route.get("stops", 0)
                    tasks = len(route.get("data", [])) or len((route.get("_ghost_record") or {}).get("task_ids") or [])
                    label = (f"{city}, {route.get('state', '')} · {card_state}{provider_label}{removal}\n"
                             f"{pod} · {stops} {'stop' if stops == 1 else 'stops'} · "
                             f"{tasks} {'task' if tasks == 1 else 'tasks'}")
                    if nearest:
                        label += f"\nClosest IC: {nearest[0]} · {nearest[1]:.1f} mi"
                    if st.button(label, key=f"revamp_route_{key}", use_container_width=True):
                        st.session_state["revamp_selected_route"] = key
                        st.rerun(scope="app")
    if st.session_state.pop("_revamp_refresh_bulk_actions", False):
        st.rerun(scope="app")


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
                     fetch_sent_records_from_sheet, default_due_days=14,
                     fn_ghost_to_cluster=None):
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
    div[class*="st-key-revamp_route_"] button {height:auto!important;min-height:4rem;
        border-radius:9px;border:1px solid #e9e5f1;background:#fff;color:#35405b;
        padding:9px 12px;text-align:left;justify-content:flex-start;white-space:normal;
        box-shadow:none;margin:2px 0 4px}
    div[class*="st-key-revamp_route_"] button p {white-space:pre-line!important;
        overflow-wrap:break-word;line-height:1.4;margin:0;text-align:left;font-size:.82rem}
    div[class*="st-key-revamp_route_"] button:hover {border-color:#6841b0;background:#f8f4ff}
    div[class*="st-key-revamp_state_toggle_"] button {background:#f6f3fc;
        border:1px solid #e5dcf3;border-radius:9px;min-height:2.55rem;
        width:100%;box-shadow:none;text-align:left;justify-content:space-between;
        color:#493278;font-weight:700;padding:6px 13px;margin:8px 0 5px}
    div[class*="st-key-revamp_state_toggle_"] button:hover {background:#eee7fa;
        border-color:#c7b2e7}
    div[class*="st-key-revamp_bulk_"] label, div[class*="st-key-revamp_fn_"] label
        {width:100%;cursor:pointer;align-items:flex-start}
    div[class*="st-key-revamp_bulk_"] label p, div[class*="st-key-revamp_fn_"] label p
        {white-space:normal;overflow-wrap:anywhere;
        line-height:1.35;font-weight:650;color:#243047}
    </style>
    """, unsafe_allow_html=True)

    heading, search_col = st.columns([1.6, 3], vertical_alignment="center")
    with heading:
        st.markdown('<div class="revamp-heading">Dispatch</div>', unsafe_allow_html=True)
    with search_col:
        search = st.text_input(
            "Search routes", placeholder="Search venue, VID, city, state, ZIP, SIO or kiosk",
            label_visibility="collapsed", key="revamp_search",
        ).strip().lower()

    accessible = [pod for pod in PODS if can_access_tab(pod)]
    if not accessible:
        st.info("Your account has no pod access. Contact an administrator to update your access.")
        return
    # Start with one pod so the first sync cannot queue five full Mapbox
    # routing passes before any routes appear. Multi-pod remains explicit.
    pod_options = accessible + (["All my pods"] if len(accessible) > 1 else [])
    remembered_pod = st.query_params.get("pod")
    if st.session_state.get("revamp_pod") not in pod_options:
        st.session_state["revamp_pod"] = remembered_pod if remembered_pod in accessible else accessible[0]
    filter_col, _, refresh_col = st.columns([1.4, 4.5, 1.5], vertical_alignment="bottom")
    with filter_col:
        pod_choice = st.selectbox("Pod", pod_options, key="revamp_pod", on_change=_remember_pod)
    selected_pods = accessible if pod_choice == "All my pods" else [pod_choice]
    with refresh_col:
        refresh_clicked = st.button("Check new tasks", key="revamp_sync", use_container_width=True)
    # A new signed-in session loads its selected pod automatically. Switching
    # pods loads only pods this session has not yet built; the refresh button
    # explicitly rebuilds the selected pod(s) against fresh Onfleet data.
    pending_pods = [pod for pod in selected_pods if
                    f"clusters_{pod}" not in st.session_state and
                    not st.session_state.get(f"_revamp_load_attempted_{pod}")]
    if refresh_clicked or pending_pods:
        fetch_sent_records_from_sheet.clear()
        for index, pod in enumerate(selected_pods if refresh_clicked else pending_pods):
            st.session_state[f"_revamp_load_attempted_{pod}"] = True
            started = time.monotonic()
            print(f"[revamp/sync] starting {pod}", flush=True)
            with st.spinner(f"Loading {pod} routes from Onfleet..."):
                if refresh_clicked and index == 0:
                    process_pod(pod, refresh_tasks=True)
                else:
                    process_pod(pod)
            loaded_count = len(st.session_state.get(f"clusters_{pod}", []))
            print(f"[revamp/sync] finished {pod}: {loaded_count} routes in {time.monotonic() - started:.1f}s", flush=True)
        st.session_state["_last_sync_ts"] = datetime.now()

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
        st.error("Routes could not be loaded. Click Check new tasks to retry.")
        return
    missing = [pod for pod in selected_pods if pod not in loaded]
    if missing:
        st.caption("Could not load: " + ", ".join(missing) + ". Click Check new tasks to retry.")

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
                     "Field Nation" if ghost_status in ("field_nation", "posted") else
                     "Sent" if ghost_status == "sent" else
                     "Declined" if ghost_status == "declined" else "Routed")
            route = {
                "_is_ghost": True, "wo": ghost.get("wo", ""),
                "_ghost_record": ghost,
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
        f'<span class="revamp-pill">Pod <b>{html.escape(str(pod_choice))}</b></span>'
        f'<span class="revamp-pill">{html.escape(sync_age)}</span>'
        f'<span class="revamp-pill">Routes <b>{len(all_routes)}</b></span>'
        f'<span class="revamp-pill">Tasks <b>{total_tasks}</b></span>'
        f'<span class="revamp-pill">Flagged <b>{counts["Flagged"]}</b></span>'
        f'<span class="revamp-pill">Unselected <b>{max(0, unselected)}</b></span>'
        f'<span class="revamp-pill">Field Nation <b>{counts["Field Nation"]}</b></span>'
        f'<span class="revamp-pill">Sent <b>{counts["Sent"]}</b></span>'
        f'<span class="revamp-pill">Accepted <b>{counts["Accepted"]}</b></span>',
        unsafe_allow_html=True,
    )

    for key in st.session_state.pop("_revamp_fn_clear_next", []):
        st.session_state[f"revamp_fn_{key}"] = False
    remembered_status = st.query_params.get("view")
    if st.session_state.get("revamp_status") not in STATUSES:
        st.session_state["revamp_status"] = remembered_status if remembered_status in STATUSES else "All"
    if st.session_state.pop("_revamp_show_fn_next", False):
        st.session_state["revamp_status"] = "Field Nation"
        _remember_view()
    if st.session_state.pop("_revamp_show_accepted_next", False):
        st.session_state["revamp_status"] = "Accepted"
        _remember_view()
    status = st.radio("Route status", STATUSES, horizontal=True,
                      label_visibility="collapsed", key="revamp_status", on_change=_remember_view)
    notice = st.session_state.pop("_revamp_notice", None)
    if notice:
        (st.warning if notice[0] == "warning" else st.success)(notice[1])
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
    fn_posted = (ghost_db or {}).get("_fn_posted", {}) or {}
    fn_providers = (ghost_db or {}).get("_fn_provider", {}) or {}
    matching.sort(key=lambda entry: (({"Pending": 0, "Posted": 1, "Assigned": 2}[
                                      _fn_stage(entry[3], fn_posted, fn_providers)]
                                      if status == "Field Nation" else 0),
                                     str(entry[1].get("state") or "").upper(),
                                     str(entry[1].get("city") or "").lower(), entry[0]))
    selection_prefix = "revamp_fn_" if status == "Field Nation" else "revamp_bulk_"
    visible_keys = [f"{entry[0]}:{entry[3]}" for entry in matching
                    if entry[2] == "Field Nation" or
                    (status != "Field Nation" and entry[2] in ("Ready", "Flagged"))]
    def select_visible():
        for key in visible_keys:
            st.session_state[f"{selection_prefix}{key}"] = True
    def clear_visible():
        for key in visible_keys:
            st.session_state[f"{selection_prefix}{key}"] = False
    select_col, clear_col = st.columns([5, 1], vertical_alignment="bottom")
    with select_col:
        st.button(f"Select all {len(visible_keys)} matching on this tab", key="revamp_select_visible",
                  on_click=select_visible,
                  disabled=not visible_keys, use_container_width=True)
    with clear_col:
        st.button("Clear selection", key="revamp_clear_selection", on_click=clear_visible,
                  use_container_width=True)
    st.caption(f"Showing {len(matching)} matching route{'s' if len(matching) != 1 else ''}")
    if status == "Field Nation":
        from fn_utils import generate_combined_fn_upload
        from migration import data_access as fn_data
        fn_selected = [entry for entry in all_routes if entry[2] == "Field Nation"
                       and st.session_state.get(f"revamp_fn_{entry[0]}:{entry[3]}")]
        pending = [entry for entry in fn_selected if _fn_stage(entry[3], fn_posted, fn_providers) == "Pending"]
        csv_routes = [_fn_csv_route(entry[1], entry[3], fn_ghost_to_cluster) for entry in fn_selected]
        csv_routes = [route for route in csv_routes if route]
        csv_data = None
        if csv_routes:
            try:
                csv_data, stop_count, _ = generate_combined_fn_upload(csv_routes)
            except Exception as exc:
                st.error(f"Could not build Field Nation CSV: {exc}")
        csv_col, posted_col, link_col = st.columns([2, 1.5, 1], vertical_alignment="bottom")
        with csv_col:
            st.download_button(f"Download bulk CSV ({len(csv_routes)} routes)",
                               data=csv_data.getvalue() if csv_data else b"",
                               file_name=f"FN_Combined_{date.today():%Y%m%d}.csv",
                               mime="text/csv", disabled=csv_data is None,
                               use_container_width=True, key="revamp_fn_csv")
        with posted_col:
            posted_clicked = st.button(f"Mark {len(pending)} Posted", disabled=not pending or db_engine is None,
                                       use_container_width=True, key="revamp_fn_posted")
        with link_col:
            st.link_button("Open Field Nation", "https://app.fieldnation.com/projects", use_container_width=True)
        if fn_selected and not csv_routes:
            st.warning("Selected routes have no task addresses available for a CSV.")
        if posted_clicked:
            failures = []
            for pod, route, _, route_hash, _ in pending:
                try:
                    result = fn_data.mirror_mark_fn_posted_by_cluster_hash(db_engine, route_hash)
                    if not result.get("success"):
                        raise RuntimeError(result.get("skipped") or result.get("error") or "Order not found")
                    st.session_state.setdefault("_revamp_fn_clear_next", []).append(f"{pod}:{route_hash}")
                except Exception as exc:
                    failures.append(f"{route.get('city', 'Route')}: {exc}")
            if failures:
                st.error("Could not mark Posted: " + "; ".join(failures[:3]))
            else:
                fetch_sent_records_from_sheet.clear()
                st.rerun()
        stage_counts = {stage: sum(_fn_stage(e[3], fn_posted, fn_providers) == stage
                                   for e in matching) for stage in ("Pending", "Posted", "Assigned")}
        st.caption("  |  ".join(f"{stage}: {count}" for stage, count in stage_counts.items()))
    chosen = [entry for entry in all_routes if entry[2] in ("Ready", "Flagged")
              and st.session_state.get(f"revamp_bulk_{entry[0]}:{entry[3]}", False)]
    fn_team_id = st.session_state.get("_fn_team_id")
    fn_worker_id = st.session_state.get("_fn_worker_id")
    assign_clicked = False
    if status != "Field Nation":
        _, due_col, action_col = st.columns([2.5, 1.4, 1.7], vertical_alignment="bottom")
        with due_col:
            fn_due = st.date_input("Field Nation due", value=date.today() + timedelta(days=default_due_days),
                                   key="revamp_fn_due")
        with action_col:
            assign_clicked = st.button(f"Assign {len(chosen)} to Field Nation",
                                       key="revamp_assign_fn", type="primary",
                                       disabled=not chosen or db_engine is None,
                                       use_container_width=True)
    if db_engine is None and status != "Field Nation":
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

    left, right = st.columns([2.1, 3], gap="medium")
    with left:
        _render_route_list(matching, status, fn_posted, fn_providers)

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
        elif state == "Field Nation":
            from migration import data_access as fn_data
            stage = _fn_stage(route_hash, fn_posted, fn_providers)
            st.caption(f"Field Nation: {stage}")
            if stage == "Pending":
                st.info("Select this route for the bulk CSV. After posting the CSV to Field Nation, mark it Posted above.")
            else:
                provider = st.text_input("Field Nation rep", value=str(fn_providers.get(route_hash) or ""),
                                         key=f"revamp_provider_{pod}_{route_hash}",
                                         placeholder="Enter the rep's name")
                save_col, assigned_col = st.columns(2)
                with save_col:
                    if st.button("Save rep", key=f"revamp_save_rep_{pod}_{route_hash}",
                                 disabled=db_engine is None, use_container_width=True):
                        try:
                            result = fn_data.mirror_set_fn_provider_by_cluster_hash(db_engine, route_hash, provider.strip())
                            if not result.get("success"):
                                raise RuntimeError(result.get("skipped") or result.get("error") or "Order not found")
                            fetch_sent_records_from_sheet.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not save Field Nation rep: {exc}")
                with assigned_col:
                    if st.button("Mark Assigned", key=f"revamp_mark_assigned_{pod}_{route_hash}",
                                 disabled=db_engine is None or not provider.strip(), use_container_width=True):
                        try:
                            # Persist the current input before promoting the order to Accepted.
                            saved = fn_data.mirror_set_fn_provider_by_cluster_hash(db_engine, route_hash, provider.strip())
                            if not saved.get("success"):
                                raise RuntimeError(saved.get("skipped") or saved.get("error") or "Order not found")
                            result = fn_data.mark_fn_assigned(db_engine, saved["work_order"])
                            if not result.get("success"):
                                raise RuntimeError(result.get("error") or "Assignment failed")
                            fetch_sent_records_from_sheet.clear()
                            st.session_state.pop(f"route_state_{route_hash}", None)
                            if result.get("partial"):
                                st.session_state["_revamp_notice"] = (
                                    "warning", f"Route moved to Accepted, but Onfleet Route Plan Name needs attention: {result.get('partialReason', 'unknown error')}")
                            else:
                                st.session_state["_revamp_notice"] = (
                                    "success", f"Assigned to {provider.strip()}. Onfleet route named {result.get('wo', 'FN route')}.")
                            st.session_state["_revamp_show_accepted_next"] = True
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not assign Field Nation rep: {exc}")
        else:
            st.caption(f"Route {route.get('wo') or route_hash}")
