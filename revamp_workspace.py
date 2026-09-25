"""An isolated, list/detail dispatch workspace for the DCC revamp.

The existing DCC application is kept in the same checkout under Full tools.
This module only selects one live route for the existing dispatch renderer,
so a dashboard interaction does not build every dispatch card at once.
"""

import hashlib
import html
import base64
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests
import streamlit as st


STATUSES = ("All", "Ready", "Flagged", "Over 50 mi", "Selected", "Field Nation", "Sent", "Accepted", "Declined", "Routed")
PODS = ("Blue", "Green", "Orange", "Purple", "Red")


@st.cache_resource(show_spinner=False)
def _pod_load_locks():
    """Do not build the same pod twice when dispatchers log in together."""
    return {pod: threading.Lock() for pod in PODS}


@st.cache_resource(show_spinner=False)
def _pod_build_jobs():
    """Keep first loads alive when a mobile browser drops its connection."""
    return {"lock": threading.Lock(), "jobs": {},
            "executor": ThreadPoolExecutor(max_workers=2, thread_name_prefix="pod-build")}


def _background_pod_build(pod, process_pod, cluster_store, refresh=False):
    jobs = _pod_build_jobs()
    with jobs["lock"]:
        current = jobs["jobs"].get(pod)
        previous = None
        if current is not None:
            if not current.done():
                if not refresh or jobs.get("refreshing", {}).get(pod):
                    return current
                previous = current  # manual refresh follows the running load
            elif not refresh and pod in cluster_store():
                return current

        progress = {"phase": "connecting", "downloaded": 0, "pages": 0,
                    "value": 0.02, "message": "Connecting to OnFleet"}
        jobs.setdefault("progress", {})[pod] = progress

        def report_download(count, page, done=False, rate_limited=False):
            with jobs["lock"]:
                progress["downloaded"] = count
                progress["pages"] = page
                if done:
                    progress["phase"] = "routing"
                    progress["value"] = 0.4
                else:
                    progress["phase"] = "rate_limited" if rate_limited else "downloading"
                    # OnFleet does not provide a total page count. Move the
                    # bar gradually through the download portion without
                    # presenting this estimate as an exact percentage.
                    progress["value"] = max(progress["value"], min(0.38, 0.4 * page / (page + 10)))

        def report_build(value, message):
            with jobs["lock"]:
                if value >= 0.4:
                    progress["phase"] = "routing"
                    progress["value"] = max(progress["value"], min(value, 0.99))
                    progress["message"] = message

        def build():
            if previous is not None:
                previous.result()
            with _pod_load_locks()[pod]:
                print(f"[revamp/sync] background build starting {pod}", flush=True)
                result = process_pod(pod, warm_only=True, refresh_tasks=refresh,
                                     _task_download_progress=report_download,
                                     _build_progress=report_build)
                ready = result is True and pod in cluster_store()
                with jobs["lock"]:
                    progress["phase"] = "complete" if ready else "failed"
                    progress["value"] = 1.0 if ready else progress["value"]
                print(f"[revamp/sync] background build {pod}: {'ready' if ready else 'failed'}", flush=True)
                return ready

        current = jobs["executor"].submit(build)
        jobs["jobs"][pod] = current
        jobs.setdefault("refreshing", {})[pod] = refresh
        return current


def _build_progress_display(pod):
    jobs = _pod_build_jobs()
    with jobs["lock"]:
        status = dict(jobs.get("progress", {}).get(pod) or {})
    count = status.get("downloaded", 0)
    page = status.get("pages", 0)
    phase = status.get("phase", "connecting")
    if phase == "rate_limited":
        label = f"OnFleet is limiting requests. {count:,} tasks downloaded; retrying page {page + 1}."
    elif phase == "downloading":
        label = f"Downloading OnFleet tasks: {count:,} received (page {page}; all pods)."
    elif phase in ("routing", "complete"):
        message = str(status.get("message", ""))
        message = message.replace("📡 ", "").replace("🗺️ ", "")
        label = f"{count:,} OnFleet tasks downloaded. {message or 'Building routes...'}"
    elif phase == "failed":
        label = "Task extraction failed. Check new tasks to retry."
    else:
        label = "Connecting to OnFleet; waiting for the first task page..."
    return max(0.0, min(float(status.get("value", 0.02)), 1.0)), label


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


def _saved_route_fields(route, ghost=None):
    """Read the same persisted values used by DCC's Sent/Accepted cards."""
    ghost = ghost or route.get("_ghost_record") or {}
    tasks = route.get("data") or []
    return {
        "contractor": ghost.get("contractor_name") or route.get("contractor_name") or "Unknown",
        "wo": ghost.get("wo") or route.get("wo") or "",
        "pay": ghost.get("pay", route.get("comp", 0)),
        "due": ghost.get("due") or route.get("due") or "N/A",
        "stops": ghost.get("stops", route.get("stops", 0)),
        "tasks": ghost.get("tasks", len(tasks)),
        "kiosks": ghost.get("kCnt", sum(
            "install" in str(task.get("task_type", "")).lower() for task in tasks)),
        "ghost": ghost,
    }


def _outlook_route_url(route_hash, fields, ic_df=None, portal_base_url=None):
    """Use DCC's exact draft when available; rebuild a draft after sign-in."""
    saved_draft = st.session_state.get(f"_persisted_outlook_{route_hash}")
    if saved_draft:
        return saved_draft
    wo = str(fields.get("wo") or "").strip()
    if not wo:
        return None
    ghost = fields.get("ghost") or {}
    recipient = str(ghost.get("contractor_email") or "").strip()
    if not recipient and ic_df is not None and not ic_df.empty:
        cols = {str(col).strip().lower(): col for col in ic_df.columns}
        if "name" in cols and "email" in cols:
            matching = ic_df[ic_df[cols["name"]].astype(str).str.strip().str.casefold() ==
                             str(fields["contractor"]).strip().casefold()]
            if not matching.empty:
                value = str(matching.iloc[0][cols["email"]] or "").strip()
                recipient = "" if value.lower() in ("nan", "none") else value
    base = portal_base_url or os.environ.get("PORTAL_BASE_URL") or (
        "https://nwilliams-maker.github.io/DCC/TerraboostRouteRequest.html")
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"route": wo, "v2": "true"})
    route_link = urlunsplit((parts.scheme, parts.netloc, parts.path,
                            urlencode(query), parts.fragment))
    body = (f"Hello {fields['contractor']},\n\n"
            f"Here is your Terraboost route request for {wo}.\n"
            f"Due date: {fields['due']}\n\n"
            f"Open the route and respond using this link:\n{route_link}")
    return "https://outlook.office.com/mail/deeplink/compose?" + urlencode({
        "to": recipient, "subject": f"Route Request | {wo}", "body": body,
    })


def _render_saved_route_card(route, state, route_hash, pod, ghost,
                             make_venue_details, make_venue_details_ghost,
                             venue_section, render_finalization_checklist,
                             move_to_dispatch, is_dispatch_associate):
    """Render the DCC route summary and actions only for the selected route."""
    fields = _saved_route_fields(route, ghost)
    ghost = fields["ghost"]
    is_ghost = bool(route.get("_is_ghost"))
    if is_ghost:
        raw_locs = [part.strip() for part in str(ghost.get("locs") or "").split("|") if part.strip()]
        locs = raw_locs[1:-1] if len(raw_locs) >= 3 else raw_locs
        locs = list(dict.fromkeys(locs))
        if not locs:
            locs = list(dict.fromkeys(str(stop.get("addr") or "").strip()
                                      for stop in (ghost.get("stop_data") or [])
                                      if stop.get("addr")))
        venues = make_venue_details_ghost(locs, stop_data=ghost.get("stop_data") or []) if locs else ""
    else:
        venues = make_venue_details(route.get("data") or [])
    venues_html = venue_section(venues) if venues else ""
    esc = lambda value: html.escape(str(value))
    st.markdown(f"""<div class="revamp-route-summary">
    <div style="background:#f7f8fa;border-bottom:1px solid #e4e7ec;padding:9px 12px;">
      <span style="font-size:9px;font-weight:900;color:#94a3b8;text-transform:uppercase;letter-spacing:0.1em;">Route Summary</span>
    </div>
    <div style="padding:12px 14px;display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #f1f5f9;gap:12px;">
      <div><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;">Contractor</div>
      <div style="font-size:14px;font-weight:800;color:#0f172a;">{esc(fields['contractor'])}</div></div>
      <div style="text-align:right;"><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;">Stops / Tasks</div>
      <div style="font-size:14px;font-weight:800;color:#0f172a;">{esc(fields['stops'])} <span style="color:#94a3b8;font-size:11px;font-weight:500;">Stops / {esc(fields['tasks'])} Tasks</span></div></div>
    </div>
    <div style="padding:10px 14px;display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #f1f5f9;gap:12px;">
      <div><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;">Due Date</div>
      <div style="font-size:13px;font-weight:700;color:#0f172a;">{esc(fields['due'])}</div></div>
      <div style="text-align:right;"><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;">Total Compensation</div>
      <div style="font-size:18px;font-weight:900;color:#16a34a;">${esc(fields['pay'])}</div></div>
    </div>{venues_html}</div>""", unsafe_allow_html=True)

    outlook_url = _outlook_route_url(route_hash, fields, st.session_state.get("ic_df"))
    if outlook_url:
        with st.container(key="revamp_outlook_action"):
            st.link_button("Open Outlook", outlook_url, use_container_width=True,
                           help="Open an Outlook draft for this route.")
    st.session_state[f"wo_{route_hash}"] = fields["wo"]
    if state == "Accepted" or (state == "Sent" and not is_ghost):
        render_finalization_checklist(
            route_hash, pod, "g_chk" if is_ghost else "sent_chk" if state == "Sent" else "chk",
            is_fn=fields["contractor"] == "Field Nation",
            has_kiosks=bool(fields["kiosks"]),
        )
    if state == "Accepted" and fields["kiosks"]:
        with st.container(key="revamp_shopify_action"):
            st.link_button("Order Kiosks on Shopify",
                           "https://admin.shopify.com/store/terraboost/draft_orders/new",
                           use_container_width=True)
    if is_dispatch_associate():
        return
    with st.popover("Re-route" if state == "Sent" else "Remove route"):
        st.write(f"Remove this route from {fields['contractor']}?")
        if st.button("Confirm re-route" if state == "Sent" else "Confirm removal",
                     key=f"revamp_revoke_{state}_{pod}_{route_hash}", type="primary"):
            hashes = (ghost.get("_merged_hashes") or [route_hash]) if is_ghost else [route_hash]
            for saved_hash in hashes:
                move_to_dispatch(
                    saved_hash, fields["contractor"], pod,
                    action_label=("Ghost Archived" if is_ghost and state == "Accepted" else
                                  "Re-Routed" if state == "Sent" else "Revoked"),
                    check_onfleet=True, cluster_data=ghost if is_ghost else route,
                    check_completed=state == "Accepted",
                )
            st.rerun()


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


def _fn_dates(route, route_hash, fn_posted):
    """Human-readable FN workflow dates from persisted route data."""
    ghost = route.get("_ghost_record") or {}
    wo = str(ghost.get("wo") or route.get("wo") or "")
    due = str(ghost.get("due") or "").strip()
    posted = str(fn_posted.get(route_hash) or "").strip()
    created = ""
    m = re.match(r"^FN(\d{2})(\d{2})(\d{4})-", wo)
    if m:
        created = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    raw = ghost.get("route_ts") or ""
    if not created and raw:
        created = str(raw)
    return created or "—", posted or "—", due or "—"


def _fn_ghost_tasks(ghost):
    """Rebuild enough route task detail from persisted FN stop_data to keep
    Field Nation cards/CSV usable after OnFleet tasks leave the open feed."""
    ghost = ghost or {}
    stop_data = ghost.get("stop_data") or []
    if isinstance(stop_data, str):
        try:
            stop_data = json.loads(stop_data)
        except Exception:
            stop_data = []
    locs = [x.strip() for x in str(ghost.get("locs") or "").split("|") if x.strip()]
    # FN locs are [home, stop1, stop2, ..., home]. Prefer those addresses.
    stop_addresses = locs[1:-1] if len(locs) >= 3 else locs
    rebuilt = []
    for idx, stop in enumerate(stop_data or []):
        if not isinstance(stop, dict):
            continue
        addr = str(stop.get("addr") or (stop_addresses[idx] if idx < len(stop_addresses) else "")).strip()
        if not addr:
            continue
        campaigns = stop.get("campaigns") or []
        if not isinstance(campaigns, list):
            campaigns = []
        clients = [
            str(c.get("name") or "").strip()
            for c in campaigns if isinstance(c, dict) and str(c.get("name") or "").strip()
        ]
        client = clients[0] if clients else "Terraboost Media"
        venue = str(stop.get("venue") or "Terraboost Media").strip()
        common = {
            "full": addr,
            "venue_name": venue,
            "venue_id": str(stop.get("venueId") or ""),
            "kiosk_id": str(stop.get("kioskId") or ""),
            "location_in_venue": str(stop.get("locationInVenue") or ""),
            "client_company": client,
            "customer_type": str(stop.get("customerType") or ""),
            "boosted_standard": str(stop.get("boostedStandard") or ""),
            "art_file": str(stop.get("artFile") or ""),
            "zip": str(stop.get("zip") or ""),
            "sio": str(stop.get("sio") or ""),
            "escalated": bool(stop.get("esc")),
        }
        category_counts = [
            ("Kiosk Install", int(stop.get("inst") or 0)),
            ("Kiosk Removal", int(stop.get("remov") or 0)),
            ("New Ad", int(stop.get("n_ad") or 0)),
            ("Continuity", int(stop.get("c_ad") or 0)),
            ("Default", int(stop.get("d_ad") or 0)),
        ]
        created = 0
        for task_type, count in category_counts:
            for _ in range(max(0, count)):
                rebuilt.append({**common, "task_type": task_type})
                created += 1
        # Older FN rows may only have t_count. Preserve the displayed/exported
        # task count instead of showing zero.
        target = int(stop.get("t_count") or stop.get("tCnt") or 0)
        while created < target:
            rebuilt.append({**common, "task_type": "Kiosk Install"})
            created += 1
        if created == 0:
            rebuilt.append({**common, "task_type": "Kiosk Install"})
    return rebuilt


def _fn_csv_route(route, route_hash, ghost_to_cluster):
    if route.get("_is_ghost"):
        ghost = route.get("_ghost_record") or {}
        rebuilt = ghost_to_cluster(ghost, skip_geocode=True) if ghost_to_cluster else None
        if rebuilt and rebuilt.get("data"):
            route = rebuilt
        else:
            route = dict(route)
            route["data"] = _fn_ghost_tasks(ghost)
            route["stops"] = int(ghost.get("stops") or ghost.get("lCnt") or route.get("stops") or 0)
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


def _important_route_badges(route):
    """Mirror DCC's important route identifiers in compact Revamp cards."""
    badges = []
    data = route.get("data") or []
    ghost = route.get("_ghost_record") or {}

    # Live route-level counters are already calculated by DCC's clustering.
    inst_count = int(route.get("inst_count") or 0)
    esc_count = int(route.get("esc_count") or 0)
    boosted_tag = str(route.get("boosted_tag") or "").strip().lower()

    # Persisted ghost/FN rows may no longer have live task objects. Recover
    # important flags from stop_data/campaign payloads.
    stop_data = ghost.get("stop_data") or []
    if isinstance(stop_data, str):
        try:
            stop_data = json.loads(stop_data)
        except Exception:
            stop_data = []

    if not inst_count:
        inst_count = int(ghost.get("kCnt") or 0)
        if not inst_count:
            inst_count = sum(int(stop.get("inst") or 0) for stop in stop_data if isinstance(stop, dict))

    if not esc_count:
        esc_count = sum(
            1 for t in data if t.get("escalated")
        ) or sum(
            int(bool(stop.get("esc"))) for stop in stop_data if isinstance(stop, dict)
        )

    # Detect Boosted / Local Plus from either live task values or persisted
    # campaign bs fields.
    tier_text = " ".join([
        boosted_tag,
        *[
            str(t.get("boosted_standard") or t.get("boosted_tag") or "").lower()
            for t in data
        ],
        *[
            str(c.get("bs") or "").lower()
            for stop in stop_data if isinstance(stop, dict)
            for c in (stop.get("campaigns") or []) if isinstance(c, dict)
        ],
    ])

    if inst_count > 0:
        badges.append(f"🛠 KIOSK INSTALL" + (f" ×{inst_count}" if inst_count > 1 else ""))
    if "local plus" in tier_text:
        badges.append("⭐ LOCAL PLUS")
    if "boosted" in tier_text:
        badges.append("🔥 BOOSTED")
    if esc_count > 0:
        badges.append(f"❗ ESCALATION" + (f" ×{esc_count}" if esc_count > 1 else ""))
    return badges


@st.fragment
def _render_route_list(matching, status, fn_posted, fn_providers):
    """State toggles rerun only this list; route clicks refresh the detail pane."""
    st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
    with st.container(height=680, border=False, key="revamp_route_scroll"):
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
                    _ghost = route.get("_ghost_record") or {}
                    tasks = (len(route.get("data", []))
                             or len(_ghost.get("task_ids") or [])
                             or int(_ghost.get("tasks") or _ghost.get("tCnt") or 0))
                    state_icon = {
                        "Ready": "●", "Flagged": "!", "Field Nation": "FN",
                        "Sent": "→", "Accepted": "✓", "Declined": "×", "Routed": "◆"
                    }.get(card_state, "•")
                    status_text = f"{state_icon} {card_state}"
                    if provider:
                        status_text += f" · {provider}"
                    if removal:
                        status_text += " · CVS Removal"
                    label = (f"{city}, {route.get('state', '')}    {status_text}\n"
                             f"{pod} Pod  ·  {stops} {'stop' if stops == 1 else 'stops'}  ·  "
                             f"{tasks} {'task' if tasks == 1 else 'tasks'}")
                    _important = _important_route_badges(route)
                    if _important:
                        label += "\n" + "  ·  ".join(_important)
                    if status == "Field Nation":
                        _created, _posted, _due = _fn_dates(route, route_hash, fn_posted)
                        _date_bits = [f"Due {_due}"]
                        if card_state == "Pending":
                            _date_bits.insert(0, f"Sent {_created}")
                        elif card_state in ("Posted", "Assigned"):
                            _date_bits.insert(0, f"Posted {_posted}")
                        label += "\n" + "  ·  ".join(_date_bits)
                    elif card_state == "Accepted":
                        _accepted_due = _saved_route_fields(route, _ghost).get("due") or "N/A"
                        label += f"\nDue {_accepted_due}"
                    if nearest:
                        label += f"\nClosest IC  ·  {nearest[0]}  ·  {nearest[1]:.1f} mi"
                    selected = st.session_state.get("revamp_selected_route") == key
                    state_key = str(card_state).replace(" ", "_")
                    if st.button(label, key=f"revamp_route_{state_key}_{key}",
                                 type="primary" if selected else "secondary",
                                 use_container_width=True):
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

    team_id = team.get("id")
    team_workers = team.get("workers") or []
    team_worker_ids = {
        str(w.get("id") if isinstance(w, dict) else w).strip()
        for w in team_workers
        if str(w.get("id") if isinstance(w, dict) else w).strip()
    }
    if len(team_worker_ids) == 1:
        return {"fn_team_id": team_id, "fn_worker_id": next(iter(team_worker_ids))}

    # Resolve from the full worker feed when the team response contains
    # multiple workers or only IDs. Prefer an explicitly named FN placeholder,
    # then the legacy known placeholder phone, then a single team member.
    seen = set()
    last_id = None
    candidates = []
    legacy_phone_match = None
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
            wid = str(worker.get("id") or "").strip()
            worker_teams = {str(x).strip() for x in (worker.get("teams") or [])}
            on_fn_team = (wid in team_worker_ids) or (team_id and str(team_id) in worker_teams)
            if not on_fn_team:
                continue
            candidates.append(worker)
            name = str(worker.get("name") or "").strip().lower()
            if "field nation" in name or "fieldnation" in name or "fn placeholder" in name:
                return {"fn_team_id": team_id, "fn_worker_id": wid}
            phone = "".join(c for c in str(worker.get("phone") or "") if c.isdigit())[-10:]
            if phone == "6302869764":
                legacy_phone_match = wid
        next_id = (data.get("lastId") if isinstance(data, dict) else None) or workers[-1].get("id")
        if not next_id or next_id in seen:
            break
        seen.add(next_id)
        last_id = next_id

    if legacy_phone_match:
        return {"fn_team_id": team_id, "fn_worker_id": legacy_phone_match}
    unique_candidate_ids = {str(w.get("id") or "").strip() for w in candidates if w.get("id")}
    if len(unique_candidate_ids) == 1:
        return {"fn_team_id": team_id, "fn_worker_id": next(iter(unique_candidate_ids))}
    raise RuntimeError(
        f"Could not uniquely resolve the Field Nation placeholder worker "
        f"from {len(unique_candidate_ids)} Field Nation team worker(s)"
    )


def _return_fn_route_to_regular(route, route_hash, pod, db_engine,
                                move_to_dispatch, fetch_sent_records_from_sheet):
    """Remove FN tracking and push its OnFleet tasks back to normal dispatch."""
    if db_engine is None:
        raise RuntimeError("Railway database is unavailable")

    from migration import data_access as _fn_data

    # Remove the Field Nation tracking row first so the next refresh cannot
    # immediately classify the route back into the FN bucket.
    result = _fn_data.mirror_remove_field_nation_by_cluster_hash(db_engine, route_hash)
    if not result.get("success"):
        raise RuntimeError(result.get("skipped") or result.get("error") or "Field Nation order not found")

    ghost = route.get("_ghost_record") or {}
    cluster_data = ghost if ghost else route

    # Existing DCC re-route logic already handles the critical OnFleet side:
    # FN tasks are state=1 under the FN placeholder, so check_completed=True
    # causes them to be PUT back to worker=None and flow into the open pool.
    move_to_dispatch(
        route_hash,
        "Field Nation",
        pod,
        action_label="Field Nation Revoked",
        check_onfleet=True,
        cluster_data=cluster_data,
        check_completed=True,
    )

    try:
        fetch_sent_records_from_sheet.clear()
    except Exception:
        pass

    st.session_state.pop(f"revamp_fn_{pod}:{route_hash}", None)
    st.session_state.pop(f"route_state_{route_hash}", None)
    return True


def render_workspace(can_access_tab, process_pod, render_dispatch,
                     haversine, db_engine, assign_tasks_to_fn_team,
                     fetch_sent_records_from_sheet, default_due_days=14,
                     fn_ghost_to_cluster=None, saved_route_helpers=None,
                     merge_same_wo_ghosts=None, cluster_store=None):
    """Render one selected route while retaining the existing dispatch actions."""
    st.markdown("""
    <style>
    /* ── DCC Revamp visual system ─────────────────────────────────────────
       Neutral workspace first. Purple is a brand accent, not the UI color.
       ─────────────────────────────────────────────────────────────────── */
    :root {
      --rv-ink:#172033;
      --rv-text:#344054;
      --rv-muted:#667085;
      --rv-faint:#98a2b3;
      --rv-border:#e4e7ec;
      --rv-border-strong:#d0d5dd;
      --rv-surface:#ffffff;
      --rv-soft:#f7f8fa;
      --rv-soft-2:#f2f4f7;
      --rv-brand:#633094;
      --rv-brand-soft:#f5f1f8;
      --rv-green:#16804a;
      --rv-green-soft:#ecf8f1;
      --rv-blue:#2563eb;
      --rv-blue-soft:#eff6ff;
      --rv-amber:#b7791f;
      --rv-amber-soft:#fff8e7;
      --rv-red:#c43232;
      --rv-red-soft:#fff1f1;
      --rv-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 14px rgba(16,24,40,.05);
    }

    html, body, [class*="css"], .stApp {
      font-family: Inter, "Segoe UI", Arial, sans-serif !important;
      color:var(--rv-text);
    }
    .stApp {background:#f5f6f8}
    [data-testid="stAppViewContainer"] > .main {background:#f5f6f8}
    [data-testid="stMainBlockContainer"] {padding-top:1.25rem}

    /* Typography */
    h1,h2,h3,h4,h5,h6 {font-family:Inter,"Segoe UI",Arial,sans-serif!important;color:var(--rv-ink)!important;letter-spacing:-.018em}
    .revamp-heading {font-size:1.72rem;font-weight:780;color:var(--rv-ink);margin:0 0 5px;letter-spacing:-.025em}
    .revamp-meta {font-size:.82rem;color:var(--rv-muted);margin:0 0 12px}
    .revamp-panel-title {font-weight:760;color:var(--rv-ink);margin:8px 0 7px;font-size:.88rem;letter-spacing:-.01em}

    /* Search, selects, date and text inputs */
    [data-testid="stTextInput"] input,
    [data-testid="stDateInput"] input,
    [data-testid="stSelectbox"] div[data-baseweb="select"] > div {
      background:var(--rv-surface)!important;
      border-color:var(--rv-border-strong)!important;
      color:var(--rv-ink)!important;
      border-radius:9px!important;
      box-shadow:none!important;
    }
    [data-testid="stTextInput"] input:focus,
    [data-testid="stDateInput"] input:focus {
      border-color:#8a93a3!important;
      box-shadow:0 0 0 3px rgba(71,84,103,.08)!important;
    }
    [data-testid="stWidgetLabel"] p {color:var(--rv-muted)!important;font-weight:650!important;font-size:.78rem!important}

    /* Summary chips — quiet, not purple */
    .revamp-pill {
      border:1px solid var(--rv-border);border-radius:999px;padding:5px 9px;
      font-size:.75rem;color:#667085;background:#fff;display:inline-block;
      margin:0 5px 7px 0;box-shadow:0 1px 1px rgba(16,24,40,.02)
    }
    .revamp-pill b {color:#344054;font-weight:750}

    /* Status nav is a quiet segmented control */
    div[class*="st-key-revamp_status"] div[role="radiogroup"] {
      border:1px solid var(--rv-border);border-radius:10px;padding:4px;gap:2px;
      background:#eef1f4;flex-wrap:wrap
    }
    div[class*="st-key-revamp_status"] label {
      border-radius:7px;padding:5px 8px;cursor:pointer;white-space:nowrap;
      color:#667085;font-weight:650;border:1px solid transparent!important
    }
    div[class*="st-key-revamp_status"] label:has(input:checked) {
      background:#fff!important;color:#1d2939!important;
      border-color:#d8dde5!important;box-shadow:0 1px 3px rgba(16,24,40,.07)!important
    }
    div[class*="st-key-revamp_status"] label:hover {background:#f7f8fa!important;color:#344054!important}

    /* Buttons: neutral by default, dark primary. Purple only for branded links. */
    .stButton > button,
    .stDownloadButton > button,
    .stLinkButton > a {
      border-radius:7px!important;
      border:1px solid var(--rv-border-strong)!important;
      background:#fff!important;
      color:#344054!important;
      font-weight:660!important;
      font-size:.76rem!important;
      min-height:30px!important;
      height:auto!important;
      padding:.28rem .58rem!important;
      line-height:1.05!important;
      box-shadow:0 1px 2px rgba(16,24,40,.04)!important;
      transition:background .12s ease,border-color .12s ease,box-shadow .12s ease,transform .12s ease!important;
    }
    .stButton > button:hover,
    .stDownloadButton > button:hover,
    .stLinkButton > a:hover {
      background:#f9fafb!important;border-color:#aeb5c0!important;
      color:#1d2939!important;box-shadow:0 2px 5px rgba(16,24,40,.07)!important
    }
    .stButton > button[kind="primary"] {
      background:#27364a!important;border-color:#27364a!important;color:#fff!important;
      box-shadow:0 2px 5px rgba(16,24,40,.12)!important
    }
    .stButton > button[kind="primary"]:hover {
      background:#1f2b3c!important;border-color:#1f2b3c!important
    }
    .stButton > button:disabled,
    .stDownloadButton > button:disabled {
      background:#f2f4f7!important;color:#98a2b3!important;border-color:#eaecf0!important;box-shadow:none!important
    }

    /* Only Outlook remains a branded Terraboost-purple action */
    div[class*="st-key-revamp_outlook_action"] a {
      min-height:30px!important;display:flex;align-items:center;justify-content:center;
      background:var(--rv-brand)!important;color:#fff!important;border:1px solid var(--rv-brand)!important;
      border-radius:9px!important;font-size:.78rem!important;font-weight:720!important;
      box-shadow:0 3px 9px rgba(99,48,148,.16)!important
    }
    div[class*="st-key-revamp_outlook_action"] a:hover {background:#52257e!important;border-color:#52257e!important}

    /* Sync action is utilitarian, not purple */
    div[class*="st-key-revamp_sync"] button {
      background:#fff!important;border:1px solid var(--rv-border-strong)!important;
      color:#344054!important;border-radius:8px!important;min-height:32px!important
    }

    /* Route inbox */
    div[class*="st-key-revamp_route_scroll"] {
      border:1px solid var(--rv-border);border-radius:12px;background:#eef1f4;
      box-shadow:var(--rv-shadow);padding:5px 6px 8px
    }
    div[class*="st-key-revamp_route_scroll"] [data-testid="stVerticalBlock"] {gap:.1rem}

    div[class*="st-key-revamp_state_toggle_"] button {
      background:transparent!important;border:0!important;border-bottom:1px solid #dde2e8!important;
      border-radius:0!important;min-height:1.85rem!important;width:100%;box-shadow:none!important;
      text-align:left;justify-content:space-between;color:#667085!important;font-weight:760!important;
      padding:5px 7px;margin:9px 0 4px
    }
    div[class*="st-key-revamp_state_toggle_"] button p {
      font-size:.72rem!important;text-transform:uppercase;letter-spacing:.065em
    }
    div[class*="st-key-revamp_state_toggle_"] button:hover {background:#e8ebef!important;border-radius:6px!important}

    div[class*="st-key-revamp_route_"] button {
      height:auto!important;min-height:3.65rem;border-radius:9px!important;
      border:1px solid #dfe3e8!important;background:#fff!important;color:#27364a!important;
      padding:7px 10px!important;text-align:left!important;justify-content:flex-start!important;
      white-space:normal!important;box-shadow:0 1px 2px rgba(16,24,40,.035)!important;
      margin:2px 0 5px!important;transition:all .12s ease!important
    }
    div[class*="st-key-revamp_route_"] button p {
      white-space:pre-line!important;overflow-wrap:break-word;line-height:1.28!important;
      margin:0;text-align:left!important;font-size:.75rem!important;font-weight:620!important
    }
    div[class*="st-key-revamp_route_"] button:hover {
      transform:translateY(-1px);border-color:#aeb5c0!important;background:#fff!important;
      box-shadow:0 4px 10px rgba(16,24,40,.08)!important
    }
    /* Selected route: subtle operational highlight. Never use the global dark-primary treatment. */
    div[class*="st-key-revamp_route_"] button[kind="primary"],
    div[class*="st-key-revamp_route_"] button[kind="primary"]:hover,
    div[class*="st-key-revamp_route_"] button[kind="primary"]:focus,
    div[class*="st-key-revamp_route_"] button[kind="primary"]:active {
      background:#f0f8f4!important;
      color:#172033!important;
      border:1px solid #88b89f!important;
      border-left:4px solid var(--rv-green)!important;
      box-shadow:0 1px 3px rgba(16,24,40,.05)!important;
      transform:none!important;
    }

    /* Meaningful route colors only */
    div[class*="st-key-revamp_route_Ready_"] button {border-left:4px solid var(--rv-green)!important}
    div[class*="st-key-revamp_route_Flagged_"] button {border-left:4px solid var(--rv-red)!important;background:#fffafa!important}
    div[class*="st-key-revamp_route_Field_Nation_"] button {border-left:4px solid var(--rv-amber)!important;background:#fffdf8!important}
    div[class*="st-key-revamp_route_Sent_"] button {border-left:4px solid var(--rv-blue)!important}
    div[class*="st-key-revamp_route_Accepted_"] button {border-left:4px solid var(--rv-green)!important;background:#fbfefc!important}
    div[class*="st-key-revamp_route_Declined_"] button {border-left:4px solid #98a2b3!important}
    div[class*="st-key-revamp_route_Routed_"] button {border-left:4px solid #667085!important}

    /* Selection checkboxes: remove giant purple emphasis */
    div[class*="st-key-revamp_bulk_"] label,
    div[class*="st-key-revamp_fn_"] label {
      width:100%;cursor:pointer;align-items:center;justify-content:center;padding-top:4px
    }
    div[class*="st-key-revamp_bulk_"] [data-baseweb="checkbox"] > div,
    div[class*="st-key-revamp_fn_"] [data-baseweb="checkbox"] > div {
      border-color:#98a2b3!important
    }

    /* Bulk/action toolbar */
    div[class*="st-key-revamp_action_bar"] {
      border:1px solid var(--rv-border);border-radius:10px;background:#fff;
      padding:6px 8px;margin:6px 0 8px;box-shadow:0 1px 3px rgba(16,24,40,.035)
    }
    div[class*="st-key-revamp_action_bar"] button {min-height:34px!important}

    /* Alerts */
    [data-testid="stAlert"] {border-radius:9px!important;border-width:1px!important}
    [data-testid="stNotification"] {border-radius:9px!important}

    /* Popovers */
    [data-testid="stPopover"] button {background:#fff!important;color:#475467!important;border-color:#d0d5dd!important}

    /* Saved route detail surface */
    .revamp-route-summary {
      background:#fff;border:1px solid var(--rv-border);border-radius:11px;
      overflow:hidden;margin-bottom:10px;box-shadow:0 1px 3px rgba(16,24,40,.04)
    }

    div[class*="st-key-revamp_shopify_action"] a {
      background:var(--rv-green)!important;color:#fff!important;border-color:var(--rv-green)!important;
      min-height:32px!important;font-weight:720!important
    }
    div[class*="st-key-revamp_shopify_action"] a:hover {
      background:#12683d!important;border-color:#12683d!important;color:#fff!important
    }

    /* Field Nation workspace */
    .fn-stage-row {display:flex;gap:8px;margin:4px 0 10px}
    .fn-stage-card {
      flex:1;background:#fff;border:1px solid var(--rv-border);border-radius:9px;
      padding:8px 10px;min-height:52px;box-shadow:0 1px 2px rgba(16,24,40,.03)
    }
    .fn-stage-card .k {font-size:.68rem;text-transform:uppercase;letter-spacing:.055em;
      color:#7a8493;font-weight:760}
    .fn-stage-card .v {font-size:1.02rem;line-height:1.15;color:#1d2939;font-weight:790;margin-top:3px}
    .fn-stage-card.pending {border-left:3px solid #d59b22}
    .fn-stage-card.posted {border-left:3px solid #3b82f6}
    .fn-stage-card.assigned {border-left:3px solid #16804a}
    div[class*="st-key-revamp_fn_toolbar"] {
      background:#fff;border:1px solid var(--rv-border);border-radius:10px;
      padding:6px 8px;margin-bottom:10px;box-shadow:0 1px 3px rgba(16,24,40,.035)
    }
    div[class*="st-key-revamp_fn_toolbar"] button,
    div[class*="st-key-revamp_fn_toolbar"] a {
      min-height:30px!important;font-size:.74rem!important
    }
    .fn-detail {
      background:#fff;border:1px solid var(--rv-border);border-radius:11px;
      padding:14px 16px;margin-bottom:10px;box-shadow:0 1px 3px rgba(16,24,40,.04)
    }
    .fn-detail-title {font-size:1.08rem;font-weight:790;color:#172033;margin-bottom:2px}
    .fn-detail-sub {font-size:.76rem;color:#667085}
    .fn-stop {
      background:#f8fafc;border:1px solid #e5e9ef;border-radius:8px;
      padding:8px 10px;margin:5px 0;font-size:.75rem;color:#344054
    }
    .fn-stop b {color:#1d2939}
    .fn-date-row {display:flex;gap:8px;margin:8px 0 12px}
    .fn-date {flex:1;background:#f8fafc;border:1px solid #e5e9ef;border-radius:8px;padding:7px 9px}
    .fn-date .k {font-size:.62rem;text-transform:uppercase;letter-spacing:.05em;color:#7a8493;font-weight:750}
    .fn-date .v {font-size:.78rem;color:#1d2939;font-weight:720;margin-top:2px}
    div[class*="st-key-revamp_fn_"] [data-testid="stCheckbox"] {
      transform:scale(.9);transform-origin:center
    }

    /* Mobile */
    @media (max-width: 800px) {
      .revamp-heading {font-size:1.45rem;margin-top:8px}
      .revamp-pill {padding:4px 7px;font-size:.7rem;margin:0 3px 5px 0}
      div[class*="st-key-revamp_status"] div[role="radiogroup"] {
        flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin
      }
      div[class*="st-key-revamp_route_scroll"] {max-height:420px;overflow-y:auto}
      div[class*="st-key-revamp_route_"] button {min-height:3.35rem}
    }
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
            print(f"[revamp/sync] waiting for {pod} load", flush=True)
            if cluster_store is not None:
                # The server builds routes independently of the browser.
                # Poll its task-page counter so mobile users see real counts
                # and can reconnect without restarting the download.
                future = _background_pod_build(
                    pod, process_pod, cluster_store,
                    refresh=refresh_clicked and index == 0,
                )
                indicator = st.progress(0.02, text="Connecting to OnFleet...")
                last_display = None
                try:
                    while True:
                        try:
                            ready = future.result(timeout=1)
                            break
                        except FutureTimeout:
                            display = _build_progress_display(pod)
                            if display != last_display:
                                indicator.progress(display[0], text=display[1])
                                last_display = display
                            if time.monotonic() - started > 240:
                                st.session_state[f"_revamp_load_attempted_{pod}"] = False
                                st.info("OnFleet extraction continues in the background. Reload to see the completed routes.")
                                return
                except Exception as exc:
                    print(f"[revamp/sync] background build failed for {pod}: {type(exc).__name__}: {exc}", flush=True)
                    ready = False
                finally:
                    indicator.empty()
                if not ready:
                    st.error("OnFleet task extraction failed. Click Check new tasks to retry.")
                    continue
            with st.spinner(f"Finishing {pod} routes..."):
                with _pod_load_locks()[pod]:
                    print(f"[revamp/sync] starting {pod}", flush=True)
                    if cluster_store is None and refresh_clicked and index == 0:
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
    ghosts_by_pod = {
        pod: (merge_same_wo_ghosts((ghost_db or {}).get(pod, []))
              if merge_same_wo_ghosts else (ghost_db or {}).get(pod, []))
        for pod in loaded
    }
    saved_by_hash = {
        (pod, str(ghost.get("hash") or "")): ghost
        for pod, ghosts in ghosts_by_pod.items() for ghost in ghosts
        if ghost.get("hash")
    }
    for pod in loaded:
        for route in st.session_state.get(f"clusters_{pod}", []):
            if route.get("is_digital"):
                continue
            nearest = _nearest_ic(route, eligible_ics, haversine)
            route_hash = _route_hash(route)
            seen_hashes.add(route_hash)
            display_route = dict(route)
            route_state = _route_status(route, sent_db, nearest[1] if nearest else None)
            saved = saved_by_hash.get((pod, route_hash))
            if saved:
                display_route["_ghost_record"] = saved
                live_ids = {str(task.get("id") or "").strip() for task in route.get("data", [])}
                saved_ids = {str(task_id).strip() for task_id in saved.get("task_ids", [])}
                if saved_ids and saved_ids != live_ids and route_state in ("Sent", "Accepted"):
                    # A bundled WO can persist several route rows while the
                    # live feed exposes only one fragment. Show the full saved
                    # card and its stop data, like DCC's unified ghost view.
                    display_route["_is_ghost"] = True
                    display_route["data"] = []
            elif route_state in ("Sent", "Accepted"):
                record = next((sent_db.get(str(task.get("id") or "").strip())
                               for task in route.get("data", [])
                               if str(task.get("id") or "").strip() in sent_db), None)
                if record:
                    display_route.update(contractor_name=record.get("name") or "Unknown",
                                         wo=record.get("wo") or "",
                                         comp=record.get("comp", 0),
                                         due=record.get("due") or "N/A")
            all_routes.append((pod, display_route, route_state,
                               route_hash, nearest))
    # Accepted routes often leave Onfleet's unassigned feed; include the
    # persisted ghost records so they remain visible in this workspace.
    for pod in loaded:
        for ghost in ghosts_by_pod[pod]:
            route_hash = str(ghost.get("hash") or "")
            if (not route_hash or route_hash in seen_hashes or
                    st.session_state.get(f"reverted_{route_hash}", False)):
                continue
            seen_hashes.add(route_hash)
            ghost_status = str(ghost.get("status", "")).lower()
            state = ("Accepted" if ghost_status in ("accepted", "finalized") else
                     "Field Nation" if ghost_status in ("field_nation", "posted") else
                     "Sent" if ghost_status == "sent" else
                     "Declined" if ghost_status == "declined" else "Routed")
            rebuilt_fn_data = _fn_ghost_tasks(ghost) if state == "Field Nation" else []
            route = {
                "_is_ghost": True, "wo": ghost.get("wo", ""),
                "_ghost_record": ghost,
                "city": ghost.get("city", "Unknown"),
                "state": ghost.get("state", ""),
                "stops": ghost.get("stops", ghost.get("lCnt", 0)),
                "data": rebuilt_fn_data,
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
    total_tasks = sum(len(route.get("data", [])) or
                      len((route.get("_ghost_record") or {}).get("task_ids") or [])
                      for _, route, _, _, _ in all_routes)
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
        # Field Nation bulk selection is intentionally Pending-only. Posted
        # and Assigned routes must be selected individually if needed.
        keys_to_select = visible_keys
        if status == "Field Nation":
            keys_to_select = [
                f"{entry[0]}:{entry[3]}" for entry in matching
                if entry[2] == "Field Nation"
                and _fn_stage(entry[3], fn_posted, fn_providers) == "Pending"
            ]
        for key in keys_to_select:
            st.session_state[f"{selection_prefix}{key}"] = True
    def clear_visible():
        for key in visible_keys:
            st.session_state[f"{selection_prefix}{key}"] = False
    if status != "Field Nation":
        with st.container(key="revamp_action_bar"):
            count_col, select_col, clear_col = st.columns([3.2, 1.35, 1.15], vertical_alignment="center")
            with count_col:
                st.caption(f"{len(matching)} route{'s' if len(matching) != 1 else ''} shown")
            with select_col:
                st.button("Select all", key="revamp_select_visible",
                          on_click=select_visible, disabled=not visible_keys,
                          use_container_width=True)
            with clear_col:
                st.button("Clear", key="revamp_clear_selection", on_click=clear_visible,
                          disabled=not visible_keys, use_container_width=True)
    if status == "Field Nation":
        from fn_utils import generate_combined_fn_upload
        from migration import data_access as fn_data
        fn_selected = [entry for entry in all_routes if entry[2] == "Field Nation"
                       and st.session_state.get(f"revamp_fn_{entry[0]}:{entry[3]}")]
        pending = [entry for entry in fn_selected if _fn_stage(entry[3], fn_posted, fn_providers) == "Pending"]
        stage_counts = {stage: sum(_fn_stage(e[3], fn_posted, fn_providers) == stage
                                   for e in matching) for stage in ("Pending", "Posted", "Assigned")}
        st.markdown(
            '<div class="fn-stage-row">'
            f'<div class="fn-stage-card pending"><div class="k">Pending</div><div class="v">{stage_counts["Pending"]}</div></div>'
            f'<div class="fn-stage-card posted"><div class="k">Posted</div><div class="v">{stage_counts["Posted"]}</div></div>'
            f'<div class="fn-stage-card assigned"><div class="k">Assigned</div><div class="v">{stage_counts["Assigned"]}</div></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        csv_routes = [_fn_csv_route(entry[1], entry[3], fn_ghost_to_cluster) for entry in fn_selected]
        csv_routes = [route for route in csv_routes if route]
        csv_data = None
        stop_count = 0
        bulk_return_clicked = False
        if csv_routes:
            try:
                csv_data, stop_count, _ = generate_combined_fn_upload(csv_routes)
            except Exception as exc:
                st.error(f"Could not build Field Nation CSV: {exc}")
        with st.container(key="revamp_fn_toolbar"):
            sel_col, select_col, csv_col, posted_col, return_col, link_col, clear_col = st.columns(
                [1.0, .85, 1.35, 1.15, 1.25, 1.0, .65], vertical_alignment="center"
            )
            with sel_col:
                st.caption(f"{len(fn_selected)} selected · {stop_count} stops")
            with select_col:
                _pending_selectable = stage_counts["Pending"]
                st.button("Select all pending", key="revamp_fn_select_all",
                          on_click=select_visible, disabled=not _pending_selectable,
                          use_container_width=True)
            with csv_col:
                st.download_button(f"Download CSV ({len(csv_routes)})",
                                   data=csv_data.getvalue() if csv_data else b"",
                                   file_name=f"FN_Combined_{date.today():%Y%m%d}.csv",
                                   mime="text/csv", disabled=csv_data is None,
                                   use_container_width=True, key="revamp_fn_csv")
            with posted_col:
                posted_clicked = st.button(f"Mark Posted ({len(pending)})",
                                           disabled=not pending or db_engine is None,
                                           use_container_width=True, key="revamp_fn_posted")
            with return_col:
                bulk_return_clicked = st.button(
                    f"Return selected ({len(fn_selected)})",
                    key="revamp_fn_return_selected",
                    disabled=not fn_selected or db_engine is None,
                    use_container_width=True,
                )
            with link_col:
                st.link_button("Open Field Nation", "https://app.fieldnation.com/projects",
                               use_container_width=True)
            with clear_col:
                st.button("Clear", key="revamp_clear_selection", on_click=clear_visible,
                          disabled=not visible_keys, use_container_width=True)
        if fn_selected and not csv_routes:
            st.warning("Selected routes have no task addresses available for a CSV.")

        if bulk_return_clicked:
            st.session_state["_revamp_fn_bulk_return_confirm"] = True

        if st.session_state.get("_revamp_fn_bulk_return_confirm"):
            with st.container(border=True):
                st.warning(
                    f"Return {len(fn_selected)} selected Field Nation route"
                    f"{'s' if len(fn_selected) != 1 else ''} to regular dispatch?"
                )
                confirm_col, cancel_col = st.columns([1, 1])
                with confirm_col:
                    if st.button(
                        "Confirm return",
                        key="revamp_fn_bulk_return_confirm_btn",
                        type="primary",
                        disabled=not fn_selected,
                        use_container_width=True,
                    ):
                        failures = []
                        returned = 0
                        for pod, route, _, route_hash, _ in list(fn_selected):
                            try:
                                _return_fn_route_to_regular(
                                    route, route_hash, pod, db_engine,
                                    move_to_dispatch, fetch_sent_records_from_sheet,
                                )
                                returned += 1
                            except Exception as exc:
                                failures.append(f"{route.get('city', 'Route')}: {exc}")
                        st.session_state["_revamp_fn_bulk_return_confirm"] = False
                        fetch_sent_records_from_sheet.clear()
                        if failures:
                            st.session_state["_revamp_notice"] = (
                                "warning",
                                f"Returned {returned} route(s). "
                                f"{len(failures)} could not be returned: " + "; ".join(failures[:3]),
                            )
                        else:
                            st.session_state["_revamp_notice"] = (
                                "success",
                                f"Returned {returned} Field Nation route(s) to regular dispatch.",
                            )
                        st.rerun()
                with cancel_col:
                    if st.button(
                        "Cancel",
                        key="revamp_fn_bulk_return_cancel_btn",
                        use_container_width=True,
                    ):
                        st.session_state["_revamp_fn_bulk_return_confirm"] = False
                        st.rerun()

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

    left, right = st.columns([1.75, 3.25], gap="large")
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
        saved_fields = _saved_route_fields(route) if state in ("Sent", "Accepted") else None
        if saved_fields:
            st.markdown(f"### {html.escape(str(saved_fields['wo'] or title))} | "
                        f"${html.escape(str(saved_fields['pay']))} | Due: "
                        f"{html.escape(str(saved_fields['due']))}")
            st.caption(f"{title} · {state} · {pod} pod · {saved_fields['stops']} stops · "
                       f"{saved_fields['tasks']} tasks")
        else:
            st.markdown(f"### {html.escape(title)}  ·  {html.escape(state)}")
            _ghost = route.get("_ghost_record") or {}
            _detail_tasks = (len(route.get("data", []))
                             or len(_ghost.get("task_ids") or [])
                             or int(_ghost.get("tasks") or _ghost.get("tCnt") or 0))
            st.caption(f"{pod} pod · {route.get('stops', 0)} stops · "
                       f"{_detail_tasks} tasks")
        if nearest:
            st.caption(f"Closest eligible IC: {nearest[0]} · {nearest[1]:.1f} mi")
        if state in ("Ready", "Flagged"):
            # Reuse the existing contractor, compensation, routing, FN,
            # bundling and link-generation logic for the selected live route.
            #
            # IMPORTANT: DCC's per-route "Assign to Field Nation" control reads
            # _fn_team_id/_fn_worker_id from session state. Revamp previously
            # populated those only in the separate bulk-FN action, which meant
            # the route-card toggle could move tasks to the FN team but then
            # skipped worker assignment + route-plan creation.
            if not st.session_state.get("_fn_team_id") or not st.session_state.get("_fn_worker_id"):
                try:
                    _fn_conn = _fetch_fn_assignment_ids()
                    st.session_state["_fn_team_id"] = _fn_conn.get("fn_team_id")
                    st.session_state["_fn_worker_id"] = _fn_conn.get("fn_worker_id")
                except Exception as _fn_exc:
                    # Do not block normal dispatch rendering, but make the
                    # failure visible so the dispatcher does not assume FN
                    # route creation is available.
                    st.session_state["_fn_assignment_lookup_error"] = str(_fn_exc)
            if st.session_state.get("_fn_assignment_lookup_error") and not st.session_state.get("_fn_worker_id"):
                st.warning("Field Nation OnFleet worker could not be resolved. "
                           "FN assignment will not create a Route Plan until this is fixed.")

            dispatch_route = dict(route)
            if nearest and nearest[1] > 50:
                dispatch_route["status"] = "Flagged"
            render_dispatch(20000, dispatch_route, pod)
        elif state == "Field Nation":
            from migration import data_access as fn_data
            stage = _fn_stage(route_hash, fn_posted, fn_providers)
            ghost = route.get("_ghost_record") or {}
            wo = str(ghost.get("wo") or route.get("wo") or "Field Nation route")
            due = str(ghost.get("due") or "Not set")
            fn_tasks = (len(route.get("data", []))
                        or len(ghost.get("task_ids") or [])
                        or int(ghost.get("tasks") or 0))
            st.markdown(
                '<div class="fn-detail">'
                f'<div class="fn-detail-title">{html.escape(wo)}</div>'
                f'<div class="fn-detail-sub">{html.escape(title)} · {html.escape(stage)} · '
                f'{route.get("stops", 0)} stops · {fn_tasks} tasks</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            created_date, posted_date, due_date = _fn_dates(route, route_hash, fn_posted)
            st.markdown(
                '<div class="fn-date-row">'
                f'<div class="fn-date"><div class="k">Sent to FN</div><div class="v">{html.escape(created_date)}</div></div>'
                f'<div class="fn-date"><div class="k">Posted</div><div class="v">{html.escape(posted_date)}</div></div>'
                f'<div class="fn-date"><div class="k">Due</div><div class="v">{html.escape(due_date)}</div></div>'
                '</div>',
                unsafe_allow_html=True,
            )

            stop_data = ghost.get("stop_data") or []
            if isinstance(stop_data, str):
                try:
                    stop_data = json.loads(stop_data)
                except Exception:
                    stop_data = []
            if stop_data:
                st.caption("Stops")
                for idx, stop in enumerate(stop_data[:12], 1):
                    addr = str(stop.get("addr") or "").strip()
                    venue = str(stop.get("venue") or "").strip()
                    count = int(stop.get("t_count") or 0)
                    st.markdown(
                        f'<div class="fn-stop"><b>{idx}. {html.escape(venue or "Location")}</b>'
                        f' · {count} task{"s" if count != 1 else ""}<br>'
                        f'{html.escape(addr)}</div>',
                        unsafe_allow_html=True,
                    )
                if len(stop_data) > 12:
                    st.caption(f"+ {len(stop_data) - 12} more stops")

            with st.popover("Return to regular routes", use_container_width=True):
                st.warning(
                    "This will remove the route from Field Nation tracking and "
                    "unassign its OnFleet tasks so they return to normal dispatch."
                )
                if st.button(
                    "Confirm return to regular routes",
                    key=f"revamp_fn_return_one_{pod}_{route_hash}",
                    type="primary",
                    disabled=db_engine is None,
                    use_container_width=True,
                ):
                    try:
                        _return_fn_route_to_regular(
                            route, route_hash, pod, db_engine,
                            move_to_dispatch, fetch_sent_records_from_sheet,
                        )
                        st.session_state["_revamp_notice"] = (
                            "success",
                            f"{title} returned to regular dispatch.",
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not return route: {exc}")

            if stage == "Pending":
                st.info("Select this route on the left, download the FN CSV, post it in Field Nation, then click Mark Posted.")
            else:
                provider = st.text_input("Assigned Field Nation rep",
                                         value=str(fn_providers.get(route_hash) or ""),
                                         key=f"revamp_provider_{pod}_{route_hash}",
                                         placeholder="Type the rep's name")
                save_col, assigned_col = st.columns([1, 1])
                with save_col:
                    if st.button("Save name", key=f"revamp_save_rep_{pod}_{route_hash}",
                                 disabled=db_engine is None or not provider.strip(),
                                 use_container_width=True):
                        try:
                            result = fn_data.mirror_set_fn_provider_by_cluster_hash(db_engine, route_hash, provider.strip())
                            if not result.get("success"):
                                raise RuntimeError(result.get("skipped") or result.get("error") or "Order not found")
                            fetch_sent_records_from_sheet.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not save Field Nation rep: {exc}")
                with assigned_col:
                    if st.button("Assigned", key=f"revamp_mark_assigned_{pod}_{route_hash}",
                                 type="primary",
                                 disabled=db_engine is None or not provider.strip(),
                                 use_container_width=True):
                        try:
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
        elif state in ("Sent", "Accepted"):
            if not saved_route_helpers:
                st.error("Saved route details are unavailable. Refresh the page.")
            else:
                _render_saved_route_card(
                    route, state, route_hash, pod, route.get("_ghost_record"),
                    **saved_route_helpers,
                )
        else:
            st.caption(f"Route {route.get('wo') or route_hash}")
