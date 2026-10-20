"""An isolated, list/detail dispatch workspace for the DCC revamp.

The existing DCC application is kept in the same checkout under Full tools.
This module only selects one live route for the existing dispatch renderer,
so a dashboard interaction does not build every dispatch card at once.
"""

import hashlib
import html
import json
import base64
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests
import streamlit as st
from task_addresses import address_with_zip


STATUSES = ("All", "Routes", "CVS Removal", "Field Nation", "Sent", "Accepted", "Declined", "Finalized")
PODS = ("Blue", "Green", "Orange", "Purple", "Red", "Digital")
HIGH_RATE_FLAG_THRESHOLD = 25.00  # Matches the dispatch card's $24.99 cutoff.


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
        label = "Task extraction failed. the Routes refresh icon to retry."
    else:
        label = "Connecting to OnFleet; waiting for the first task page..."
    return max(0.0, min(float(status.get("value", 0.02)), 1.0)), label


def _route_hash(route):
    ids = sorted(str(task.get("id", "")).strip() for task in route.get("data", []))
    return hashlib.md5("".join(ids).encode()).hexdigest()


def _route_status(route, sent_db, nearest_miles=None, pod=None):
    route_hash = _route_hash(route)
    local = st.session_state.get(f"route_state_{route_hash}")
    if local == "finalized" and route.get("data") and all(
            task.get("_onfleet_unassigned") for task in route["data"]):
        # A fresh unassigned task can be worked again; retain its old WO as
        # a separate Finalized card rather than pinning the live task there.
        local = None
        st.session_state.pop(f"route_state_{route_hash}", None)
    persisted_status = ""
    if not st.session_state.get(f"reverted_{route_hash}"):
        match = next(
            (sent_db.get(str(t.get("id", "")).strip()) for t in route.get("data", [])
             if str(t.get("id", "")).strip() in sent_db),
            None,
        )
        if match:
            persisted_status = str(match.get("status", "")).lower()
    if persisted_status == "finalized" and not (route.get("data") and all(
            task.get("_onfleet_unassigned") for task in route["data"])):
        return "Finalized"
    if persisted_status == "accepted":
        return "Accepted"
    if persisted_status == "declined":
        return "Declined"
    if persisted_status == "field_nation":
        return "Field Nation"
    if local == "field_nation":
        # Session-only FN labels are not proof of an actual transfer.
        # Successful sends have a Postgres reservation by task ID, and the
        # sender invalidates its cache before rerun. Drop any orphaned label.
        st.session_state.pop(f"route_state_{route_hash}", None)
        local = None
    if local == "email_sent" or persisted_status == "sent":
        return "Sent"
    if local == "declined":
        return "Declined"
    if local == "finalized":
        return "Finalized"
    # The dispatch card saves its original calculated rate once. Subsequent
    # manual edits to Rate/Stop must leave the preview in its current queue.
    calculated_rate = 0.0
    if pod and not st.session_state.get(f"_route_fa_{pod}_{route_hash}", False):
        calculated_rate = float(st.session_state.get(f"_auto_rate_{pod}_{route_hash}", 0) or 0)
    # Re-evaluate the queue from CURRENT contractor proximity/rate.
    # Do not let a stale pre-sync route["status"] keep a route Flagged after
    # the contractor pool has changed. The screenshot case was 1.4 mi in the
    # detail dropdown while the list still showed 138.3 mi + Flagged.
    if ((nearest_miles is None)
            or nearest_miles > 60
            or calculated_rate >= HIGH_RATE_FLAG_THRESHOLD):
        return "Flagged"
    return "Ready"


def _dedupe_route_entries(entries):
    """Show one saved work order, even when its tasks span live clusters."""
    unique = {}
    for entry in entries:
        key = _route_entry_key(*entry[:4])
        previous = unique.get(key)
        if previous is None:
            unique[key] = entry
        elif entry[2] in ("Sent", "Accepted", "Declined", "Finalized"):
            # A persisted bundle has the complete stop/task list. Live
            # fragments can inherit its WO through sent_db while retaining
            # their own hashes; those are views of the same order, not new
            # work orders. Prefer the saved card before deriving counts or
            # exposing actions for either fragment.
            def completeness(item):
                route = item[1]
                ghost = route.get("_ghost_record") or {}
                return (bool(route.get("_is_ghost") and ghost),
                        bool(ghost), len(ghost.get("task_ids") or []),
                        len(route.get("data") or []))
            if completeness(entry) > completeness(previous):
                unique[key] = entry
        elif previous[2] == "Ready" and entry[2] == "Flagged":
            # Identical task sets can arrive as separate live clusters. Keep
            # the review requirement if either copy was flagged.
            unique[key] = entry
    return list(unique.values())


def _route_entry_key(pod, route, state, route_hash):
    key = f"{pod}:{route_hash}"
    if route.get("_fn_assigned_history"):
        original_wo = str(route.get("_fn_original_wo") or route.get("wo") or route_hash)
        return f"{pod}:fn-assigned:" + hashlib.sha256(original_wo.encode()).hexdigest()[:16]
    # Different saved work orders can share task IDs/hash after a resend.
    # Keep their historical cards separate while actions use the real hash.
    if state in ("Sent", "Accepted", "Declined", "Finalized"):
        wo = str(_saved_route_fields(route)["wo"] or "").strip()
        if wo:
            # A saved order's identity is its WO, not the current live
            # clustering of its task IDs. Keep different historical WOs
            # and statuses separate, including finalized history.
            key = f"{pod}:saved:{state}:" + hashlib.sha256(wo.encode()).hexdigest()[:16]
    return key


def _searchable(route):
    """Index live tasks and saved-route payloads, including nested clients."""
    fields = []
    seen = set()

    def collect(value):
        if isinstance(value, (dict, list, tuple, set)):
            if id(value) in seen:
                return
            seen.add(id(value))
            values = value.values() if isinstance(value, dict) else value
            for item in values:
                collect(item)
        elif value is not None and not isinstance(value, bool):
            fields.append(str(value))

    collect(route)
    return " ".join(fields).casefold()


def _eligible_ics(ic_df, mapbox_geocode=None):
    """Same eligible contractor pool used by the route detail dropdown.

    Stored lat/lng is preferred. If coordinates are missing, resolve the
    contractor's current location text through the shared cached geocoder
    instead of silently dropping the contractor.
    """
    if ic_df is None or ic_df.empty:
        return []
    columns = {str(c).strip().lower(): c for c in ic_df.columns}
    if "ic list" not in columns:
        return []
    result = []
    for _, row in ic_df.iterrows():
        if str(row.get(columns["ic list"], "")).strip().upper() not in (
            "ACTIVE", "IN TRAINING", "NEED INSURANCE"
        ):
            continue

        latitude = longitude = None
        if "lat" in columns and "lng" in columns:
            try:
                latitude = float(row[columns["lat"]])
                longitude = float(row[columns["lng"]])
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    latitude = longitude = None
            except (ValueError, TypeError):
                latitude = longitude = None

        if (latitude is None or longitude is None) and mapbox_geocode and "location" in columns:
            location = str(row.get(columns["location"], "") or "").strip()
            if location and location.lower() not in ("nan", "none"):
                try:
                    coords = mapbox_geocode(location)
                    if coords:
                        longitude, latitude = float(coords[0]), float(coords[1])
                except Exception:
                    latitude = longitude = None

        if latitude is None or longitude is None:
            continue
        result.append((str(row.get(columns.get("name"), "Unknown")), latitude, longitude))
    return result


def _route_center(route):
    """True geographic center of the route's unique live stops."""
    points = []
    seen = set()
    for task in route.get("data", []) or []:
        key = str(task.get("full", "") or "")
        if key in seen:
            continue
        seen.add(key)
        try:
            lat = float(task.get("lat"))
            lng = float(task.get("lon"))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                points.append((lat, lng))
        except (ValueError, TypeError):
            continue
    if points:
        return (
            sum(lat for lat, _ in points) / len(points),
            sum(lng for _, lng in points) / len(points),
        )
    center = route.get("center")
    return center if center and len(center) == 2 else None


def _nearest_ic(route, eligible_ics, haversine):
    center = _route_center(route)
    if not center:
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


def _route_status_timestamp(route, state):
    dt = _saved_status_sort_key(route)
    if dt == datetime.min:
        return "—"
    return dt.strftime("%m/%d/%Y %I:%M %p") + " CT"


def _display_contractor_name(route, state):
    fields = _saved_route_fields(route)
    name = str(fields.get("contractor") or "").strip()
    ghost = fields.get("ghost") or {}
    if state in ("Sent", "Accepted", "Declined", "Finalized") and name.lower() == "field nation":
        provider = str(ghost.get("fn_provider") or route.get("fn_provider") or "").strip()
        return provider or "Assigned Contractor"
    return name or "Unknown"


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
                             move_to_dispatch, is_dispatch_associate, email_db_engine=None):
    """Render the DCC route summary and actions only for the selected route."""
    fields = _saved_route_fields(route, ghost)
    fields["contractor"] = _display_contractor_name(route, state)
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
      <div style="font-size:14px;font-weight:800;color:#0f172a;">{esc(fields['contractor'])}</div>
      <div style="font-size:11px;color:#64748b;margin-top:3px;">{esc(state)}: {esc(_route_status_timestamp(route, state))}</div></div>
      <div style="text-align:right;"><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;">Stops / Tasks</div>
      <div style="font-size:14px;font-weight:800;color:#0f172a;">{esc(fields['stops'])} <span style="color:#94a3b8;font-size:11px;font-weight:500;">Stops / {esc(fields['tasks'])} Tasks</span></div></div>
    </div>
    <div style="padding:10px 14px;display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #f1f5f9;gap:12px;">
      <div><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;">Due Date</div>
      <div style="font-size:13px;font-weight:700;color:#0f172a;">{esc(fields['due'])}</div></div>
      <div style="text-align:right;"><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;">Total Compensation</div>
      <div style="font-size:18px;font-weight:900;color:#16a34a;">${esc(fields['pay'])}</div></div>
    </div>{venues_html}</div>""", unsafe_allow_html=True)

    outlook_url = _outlook_route_url(route_hash, fields, st.session_state.get("ic_df")) if state != "Declined" else None
    if outlook_url:
        with st.container(key="revamp_outlook_action"):
            from migration.email_dispatch_control import render_email_open
            render_email_open(email_db_engine, "Open Outlook", outlook_url, f"saved_email_open_{pod}_{route_hash}")
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
                    action_label=("Ghost Archived" if is_ghost and state in ("Accepted", "Finalized") else
                                  "Re-Routed" if state == "Sent" else "Revoked"),
                    check_onfleet=True, cluster_data=ghost if is_ghost else route,
                    check_completed=state in ("Accepted", "Finalized"),
                )
            st.rerun()


def _select_visible(keys):
    for key in keys:
        st.session_state[f"revamp_bulk_{key}"] = True


def _clear_selection(keys):
    for key in keys:
        st.session_state[f"revamp_bulk_{key}"] = False


def _apply_bulk_fn_results(chosen, saved, skipped):
    """Settle selections using both original and post-reconciliation hashes."""
    completed = dict(saved)
    already_fn = {route_hash: wo for route_hash, wo in skipped if wo != "Already assigned"}
    resolved = set(completed) | {route_hash for route_hash, _ in skipped}
    for pod, route, _, original_hash, _ in chosen:
        current_hash = _route_hash(route)
        if original_hash not in resolved and current_hash not in resolved:
            continue
        _clear_selection([f"{pod}:{original_hash}", f"{pod}:{current_hash}"])
        if current_hash != original_hash:
            pool_key = "global_digital_clusters" if pod in ("Digital", "Global_Digital") else f"clusters_{pod}"
            st.session_state[pool_key] = [route if _route_hash(live) == original_hash else live
                                          for live in st.session_state.get(pool_key, [])]
        if original_hash in completed or current_hash in completed or original_hash in already_fn or current_hash in already_fn:
            for route_hash in {original_hash, current_hash}:
                st.session_state[f"route_state_{route_hash}"] = "field_nation"
                st.session_state[f"reverted_{route_hash}"] = False
        if st.session_state.get("revamp_selected_route") in {f"{pod}:{original_hash}", f"{pod}:{current_hash}"}:
            st.session_state.pop("revamp_selected_route", None)
    for key in ("_revamp_quiet_visible_keys", "_revamp_quiet_pending", "_revamp_quiet_all_pending", "_revamp_fn_checked_entries"):
        st.session_state.pop(key, None)


def _fn_stage(route_hash, posted, providers):
    if str(providers.get(route_hash) or "").strip():
        return "Assigned"
    return "Posted" if route_hash in posted else "Pending"


def _fn_entry_stage(entry, posted, providers):
    """Historical FN assignments remain Assigned after promotion to Accepted."""
    if entry[1].get("_fn_assigned_history"):
        return "Assigned"
    return _fn_stage(entry[3], posted, providers)


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


def _fn_stop_markup(stop, number, address):
    """Field Nation cards show Venue IDs only, never Kiosk IDs.

    The kiosk identifiers remain saved for Routes/Sent/Accepted and other
    non-Field-Nation views, but must not be rendered on Field Nation cards.
    """
    if not isinstance(stop, dict):
        return ""
    esc = lambda value: html.escape(str(value))
    tasks = [task for task in (stop.get("tasks") or []) if isinstance(task, dict)]
    venue_ids = list(dict.fromkeys(
        str(task.get("venueId") or "").strip()
        for task in tasks if str(task.get("venueId") or "").strip()
    ))
    if not tasks:
        legacy_venue = str(stop.get("venueId") or "").strip()
        if legacy_venue:
            venue_ids = [legacy_venue]
    venue_label = "Venue IDs" if len(venue_ids) > 1 else "Venue ID"
    venue_value = esc(", ".join(venue_ids) if venue_ids else "—")
    task_count = len(tasks) if tasks else int(stop.get("t_count") or stop.get("tCnt") or 0)
    venue = str(stop.get("venue") or "Location")
    head = (
        f'<div class="fn-stop"><b>{number}. {esc(venue)}</b>'
        f' · {task_count} task{"s" if task_count != 1 else ""}<br>'
        f'{esc(address)}'
        f'<div style="font-size:11px;color:#344054;margin-top:5px;">'
        f'{venue_label}: {venue_value}</div>'
    )
    if tasks:
        rows = []
        for task_index, task in enumerate(tasks, 1):
            task_type = str(task.get("taskType") or "Task")
            task_id = str(task.get("taskId") or "").strip()
            label = f"Task {task_index}" + (f" ({task_id})" if task_id else "")
            rows.append(
                f'<div style="padding:4px 0;border-top:1px solid #e4e7ec;">'
                f'<b>{esc(label)}</b> · {esc(task_type)}'
                f'<div style="color:#475467;">'
                f'Venue ID: {esc(str(task.get("venueId") or "").strip() or "—")}'
                f'</div></div>'
            )
        head += ('<div style="font-size:11px;margin-top:7px;">'
                 + "".join(rows) + '</div>')
    else:
        head += ('<div style="color:#667085;font-size:10px;margin-top:5px;">'
                 'Per-task Venue IDs were not saved on this older work order.</div>')
    return head + '</div>'

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
        # New FN work orders retain the original task-level IDs and types.
        # Never copy the first kiosk's ID onto other tasks at the same stop.
        saved_tasks = stop.get("tasks")
        if isinstance(saved_tasks, list) and saved_tasks:
            added = 0
            for task in saved_tasks:
                if not isinstance(task, dict):
                    continue
                rebuilt.append({
                    **common,
                    "id": str(task.get("taskId") or "").strip(),
                    "venue_id": str(task.get("venueId") or "").strip(),
                    "kiosk_id": str(task.get("kioskId") or "").strip(),
                    "location_in_venue": str(task.get("locationInVenue") or "").strip(),
                    "client_company": str(task.get("clientCompany") or "").strip() or client,
                    "sio": str(task.get("sio") or "").strip(),
                    "task_type": str(task.get("taskType") or "").strip() or "Task",
                })
                added += 1
            if added:
                continue
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
        stop_data = ghost.get("stop_data") or []
        if isinstance(stop_data, str):
            try:
                stop_data = json.loads(stop_data)
            except (ValueError, TypeError):
                stop_data = []
        has_task_details = any(
            isinstance(stop, dict) and isinstance(stop.get("tasks"), list)
            and stop["tasks"] for stop in stop_data
        )
        # Prefer persisted per-task identifiers over older synthetic rows,
        # which stamped one stop-level kiosk ID across every task.
        rebuilt = (ghost_to_cluster(ghost, skip_geocode=True)
                   if ghost_to_cluster and not has_task_details else None)
        if rebuilt and rebuilt.get("data"):
            route = rebuilt
        else:
            route = dict(route)
            route["data"] = _fn_ghost_tasks(ghost)
            route["stops"] = int(ghost.get("stops") or ghost.get("lCnt") or route.get("stops") or 0)
    if not route or not route.get("data"):
        return None
    return {**route, "_cluster_hash": route_hash}



def _fn_csv_download_callback(db_engine, work_orders, csv_digest, actor, refresh_saved):
    """Acknowledge actual Download CSV clicks with a durable, per-WO check.

    Streamlit runs this immediately after a browser download click, before the
    next route-list render. If saving fails, no green check is shown and the
    database independently blocks the Posted transition.
    """
    from migration import data_access as fn_data

    try:
        result = fn_data.record_fn_csv_download(
            db_engine, list(work_orders), csv_sha256=csv_digest, actor=actor)
        marked = result.get("marked") or []
        refresh_saved.clear()
        st.session_state.pop("_revamp_fn_checked_entries", None)
        st.session_state.pop("_revamp_saved_event_version", None)
        st.session_state["_revamp_notice"] = (
            "success",
            f"✅ CSV downloaded — {len(marked)} route(s) now have a green checkmark "
            "and can be marked Posted.",
        )
    except Exception as exc:
        print(f"[revamp/fn-csv-download] {type(exc).__name__}: {exc}", flush=True)
        st.session_state["_revamp_notice"] = (
            "warning",
            "CSV download started, but the green check could not be saved: "
            f"{exc}. No unchecked route can be marked Posted. "
            "Refresh, select the Pending routes, and download again.",
        )


def _fn_csv_pending_checked(entries):
    """Every selected Pending WO must have a durable verified CSV check."""
    return bool(entries) and all(
        bool((entry[1].get("_ghost_record") or {}).get("fn_csv_downloaded"))
        for entry in entries
    )


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


def _saved_status_sort_key(route):
    ghost = route.get("_ghost_record") or {}
    raw = ghost.get("route_ts_iso") or ghost.get("route_ts") or route.get("route_ts_iso") or route.get("route_ts") or ""
    text = str(raw or "").strip()
    if not text:
        return datetime.min
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("America/Chicago"))
        return dt.replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d %I:%M %p", "%m/%d/%Y", "%m/%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except Exception:
            continue
    return datetime.min


def _saved_status_group_label(route):
    dt = _saved_status_sort_key(route)
    return dt.strftime("%m/%d/%Y") if dt != datetime.min else "Unknown date"


def _is_cvs_removal_route(route):
    """Preserve the CVS Removal label after a route becomes a saved/ghost card."""
    ghost = route.get("_ghost_record") or {}
    for record in (route, ghost):
        value = record.get("is_removal")
        if value is True or str(value).strip().lower() in ("true", "1", "yes"):
            return True
        for field in ("route_type", "task_type", "type", "category"):
            val = str(record.get(field) or "").lower()
            if "cvs" in val and ("remov" in val or "pull down" in val):
                return True
    # Saved routes may no longer contain live tasks; use persisted stop details.
    all_tasks = list(route.get("data") or [])
    stop_data = ghost.get("stop_data") or []
    if isinstance(stop_data, str):
        try:
            stop_data = json.loads(stop_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            stop_data = []
    if isinstance(stop_data, list):
        for stop in stop_data:
            if isinstance(stop, dict):
                all_tasks.extend(t for t in (stop.get("tasks") or []) if isinstance(t, dict))
    return any(
        ("cvs" in " ".join(str(t.get(k) or "") for k in
                           ("client_company", "client", "client_name", "venue_name", "task_type")).lower()
         and ("remov" in str(t.get("task_type") or t.get("type") or "").lower()
              or "pull down" in str(t.get("task_type") or t.get("type") or "").lower()))
        for t in all_tasks if isinstance(t, dict)
    )


def _route_list_heading(route, state, route_hash):
    """Put saved work orders first without changing route queues."""
    if state not in ("Sent", "Accepted", "Declined", "Finalized"):
        return None
    fields = _saved_route_fields(route)
    ghost = fields["ghost"]
    finalized = state == "Accepted" and (
        str(ghost.get("status") or route.get("db_status") or "").lower() == "finalized"
        or st.session_state.get(f"route_state_{route_hash}") == "finalized"
    )
    display_status = "Finalized" if finalized else state
    icon = {"Sent": "→", "Accepted": "✓", "Declined": "×", "Finalized": "✓"}[display_status]
    wo = str(fields["wo"] or "").strip() or "Work order unavailable"
    city = route.get("city") or "Unknown city"
    return f"**{wo}** - {city}, {route.get('state', '')} - {icon} {display_status}"


def _bundle_label(route, bundle_sets=()):
    """Identify confirmed bundles, including saved orders after tasks leave the feed."""
    ghost = route.get('_ghost_record') or {}
    for value in (route.get('bundle_count'), ghost.get('bundle_count')):
        try:
            if int(value or 0) > 0:
                return '🔗 Bundled'
        except (TypeError, ValueError):
            pass
    try:
        if int(ghost.get('_merged_count') or route.get('_merged_count') or 0) > 1:
            return '🔗 Bundled'
    except (TypeError, ValueError):
        pass
    ids = {str(t.get('id') or '').strip() for t in route.get('data', [])}
    ids.update(str(tid).strip() for tid in ghost.get('task_ids', []))
    if any(len(entry) > 1 and entry.issubset(ids) for entry in bundle_sets):
        return '🔗 Bundled'
    return ''


@st.cache_data(ttl=300, show_spinner=False)
def _load_bundle_labels(_db_engine, pod):
    """Read the existing shared bundle registry without changing route grouping."""
    from migration.data_access import load_bundle_map
    from migration.task_availability import task_ids
    if _db_engine is None:
        return []
    try:
        return [set(task_ids(entry)) for entry in (load_bundle_map(_db_engine, pod, '_shared') or [])]
    except Exception as exc:
        print(f'[revamp/bundle-label] registry unavailable for {pod}: {type(exc).__name__}', flush=True)
        return []


def _bundle_sets_for_pod(pod):
    current = st.session_state.get('_bundle_map') or {}
    if pod in current:
        return current[pod]
    return (st.session_state.get('_revamp_bundle_labels') or {}).get(pod, [])


def _routes_refresh_style(busy=False):
    # The same 12px circle is both the refresh control and the activity indicator.
    animation = 'rv-task-spin 0.8s linear infinite' if busy else 'none'
    st.markdown("""<style>
    @keyframes rv-task-spin{to{transform:rotate(360deg)}}
    .st-key-revamp_routes_heading [data-testid="stHorizontalBlock"]{gap:6px!important;align-items:center;}
    .st-key-revamp_routes_heading [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(1){flex:0 0 auto!important;width:max-content!important;min-width:0!important;}
    .st-key-revamp_routes_heading [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2){flex:0 0 20px!important;width:20px!important;min-width:20px!important;}
    .st-key-revamp_routes_heading [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(3){flex:1 1 0!important;min-width:0!important;}
    .st-key-revamp_routes_heading .revamp-panel-title{margin:0!important;}
    .st-key-revamp_quiet_control button{min-height:20px!important;height:20px!important;min-width:20px!important;width:20px!important;padding:2px!important;border:0!important;box-shadow:none!important;background:transparent!important;display:flex;align-items:center;justify-content:center;}
    .st-key-revamp_quiet_control button p{font-size:0!important;margin:0!important;}
    .st-key-revamp_quiet_control button::after{content:'';width:12px;height:12px;box-sizing:border-box;border:2px solid #e4e7ec;border-top-color:#667085;border-radius:50%;animation:""" + animation + ";}" + """
    .st-key-revamp_quiet_control button:hover::after{border-top-color:#344054;}
    .st-key-revamp_quiet_control button:focus-visible{outline:2px solid #98a2b3!important;outline-offset:2px;}
    .rv-task-note{font-size:12px;font-weight:400;color:#667085;}
    </style>""", unsafe_allow_html=True)


@st.cache_resource(show_spinner=False)
def _quiet_refresh_service():
    from migration.quiet_task_refresh import QuietTaskRefresh
    return QuietTaskRefresh()


def _entry_matches(entry, status, search):
    state, route, nearest = entry[2], entry[1], entry[4]
    return ((status == "All" or state == status or
             (status == "Routes" and state in ("Ready", "Flagged")) or
             (status == "Over 50 mi" and nearest and nearest[1] > 50 and state in ("Ready", "Flagged")) or
             (status == "CVS Removal" and state in ("Ready", "Flagged") and route.get("is_removal")) or
             (status == "Selected" and st.session_state.get(f"revamp_bulk_{entry[0]}:{entry[3]}", False))) and
            (not search or search in _searchable(route)) and
            (status == "CVS Removal" or state not in ("Ready", "Flagged") or not route.get("is_removal")))


def _quiet_routes_check(pods, process_pod, process_digital_pool, cluster_store, fetch_open_tasks, notice_placeholder=None, fetch_saved_records=None):
    """Workers build caches only; this fragment applies results to its session."""
    from migration.quiet_task_refresh import reconcile_task_pool

    def build(selected, refresh_source):
        if refresh_source:
            fetch_open_tasks.clear()
        source = fetch_open_tasks()
        if source.get('_hit_cap'):
            raise RuntimeError('Incomplete OnFleet task download')
        result = {}
        for pod in selected:
            with _pod_load_locks()[pod]:
                # The old signature only counts task IDs. Assignment/team
                # changes can keep those IDs unchanged, so rebuild on a check.
                if pod in cluster_store():
                    cluster_store()[pod].pop('sig', None)
                ready = (process_digital_pool(warm_only=True) if pod == 'Digital'
                         else process_pod(pod, warm_only=True))
                if ready is not True:
                    raise RuntimeError(f'{pod} task check failed')
                result[pod] = cluster_store()[pod]['clusters']
        return result

    manual = st.button('↻', key='revamp_quiet_refresh',
                       use_container_width=True)
    if not manual:
        return False
    _routes_refresh_style(True)
    if notice_placeholder is not None:
        notice_placeholder.markdown('<span class="rv-task-note">Checking tasks…</span>', unsafe_allow_html=True)
    if fetch_saved_records is not None:
        try:
            fetch_saved_records.clear()
            sent, ghosts, archived, history = fetch_saved_records()
            previous_sent = st.session_state.get('sent_db') or {}
            # Revoking/rerouting in another session must clear this session's
            # old Sent/FN flag, but only after the DB explicitly archives it.
            for pod in pods:
                store_key = 'global_digital_clusters' if pod == 'Digital' else f'clusters_{pod}'
                for route in st.session_state.get(store_key, []):
                    ids = [str(t['id']).strip() for t in route.get('data', [])]
                    released = any((previous_sent.get(tid) or {}).get('wo') in archived for tid in ids)
                    reserved = any(str((sent.get(tid) or {}).get('status', '')).lower()
                                   in ('sent', 'accepted', 'field_nation') for tid in ids)
                    if released and not reserved:
                        st.session_state.pop(f'route_state_{_route_hash(route)}', None)
            st.session_state.update(sent_db=sent, ghost_db=ghosts, archived_wos=archived, _history_db=history)
        except Exception as exc:
            print(f'[revamp/task-check] saved status check failed: {type(exc).__name__}', flush=True)
            st.session_state['_revamp_quiet_error'] = True
            return False
    future = _quiet_refresh_service().poll(pods, build, force=True)
    applied = st.session_state.setdefault('_revamp_quiet_applied', {})
    key = tuple(sorted(pods))
    if applied.get(key) is not future:
        applied[key] = future
        try:
            fresh = future.result()
            total_added = total_removed = 0
            pending_changed = False
            sent_db = st.session_state.get('sent_db') or {}
            fresh_available = {str(t['id']).strip() for routes in fresh.values()
                               for route in routes for t in route.get('data', [])
                               if t.get('_onfleet_unassigned')}
            def protected(route):
                route_hash = _route_hash(route)
                ids = {str(t['id']).strip() for t in route.get('data', [])}
                if (st.session_state.get(f'route_state_{route_hash}') == 'finalized'
                        and ids and ids <= fresh_available):
                    st.session_state.pop(f'route_state_{route_hash}', None)
                return (st.session_state.get(f'route_state_{route_hash}') in ('email_sent', 'field_nation')
                        or any(str((sent_db.get(str(t['id']).strip()) or {}).get('status', '')).lower()
                               in ('sent', 'accepted', 'field_nation') for t in route.get('data', [])))
            for pod in pods:
                store_key = 'global_digital_clusters' if pod == 'Digital' else f'clusters_{pod}'
                current = st.session_state.get(store_key, [])
                updated, added, removed = reconcile_task_pool(current, fresh[pod], protected)
                pending_changed = pending_changed or updated != current
                st.session_state[store_key] = updated
                total_added += added
                total_removed += removed
            # A classification/status change matters even if IDs are identical.
            st.session_state['_revamp_quiet_revision'] = st.session_state.get('_revamp_quiet_revision', 0) + 1
            print(f'[revamp/task-check] pods={pods} added={total_added} removed={total_removed} '
                  f'routes={ {pod: len(fresh[pod]) for pod in pods} }', flush=True)
            checked_at = datetime.now(ZoneInfo('America/Chicago')).strftime('%I:%M:%S %p').lstrip('0') + ' CT'
            if total_added or total_removed or pending_changed:
                parts = []
                if total_added: parts.append(f"{total_added} {'task' if total_added == 1 else 'tasks'} added")
                if total_removed: parts.append(f"{total_removed} {'task' if total_removed == 1 else 'tasks'} removed")
                if not parts: parts.append('Routes updated')
                st.session_state['_revamp_quiet_notice'] = (' · '.join(parts) + ' · ' + checked_at, time.monotonic())
            else:
                st.session_state['_revamp_quiet_notice'] = ('Up to date · ' + checked_at, time.monotonic())
            st.session_state['_revamp_quiet_error'] = False
            st.session_state['_last_sync_ts'] = datetime.now()
        except Exception as exc:
            print(f'[revamp/task-check] keeping existing routes: {type(exc).__name__}: {exc}', flush=True)
            st.session_state['_revamp_quiet_error'] = True
    st.session_state['_revamp_manual_check_complete'] = True
    return False


def _route_list_groups(matching, status, fn_posted=None, fn_providers=None):
    """Field Nation uses Pod → Workflow → State; saved routes use dates."""
    grouped = {}
    for entry in matching:
        if status == "Field Nation":
            pod = "Digital" if entry[0] == "Global_Digital" else entry[0]
            stage = _fn_entry_stage(entry, fn_posted or {}, fn_providers or {})
            group = (pod, stage, str(entry[1].get("state") or "Unknown state").strip().upper())
        else:
            group = ("", "", _saved_status_group_label(entry[1])
                     if status in ("Sent", "Accepted", "Declined", "Finalized")
                     else str(entry[1].get("state") or "Unknown state").strip().upper())
        grouped.setdefault(group, []).append(entry)
    if status == "Field Nation":
        rank = {"Pending": 0, "Posted": 1, "Assigned": 2}
        return dict(sorted(grouped.items(), key=lambda item: (item[0][0], rank[item[0][1]], item[0][2])))
    return grouped


def _workspace_statuses(user):
    # Enforce on every render, including old sessions and URL view parameters.
    return ("Field Nation",) if user.get("tier") == "guest" or user.get("scope") == "field_nation" or str(user.get("role", "")).lower() == "associate" else STATUSES


def _has_linked_fn_work_order(ghost):
    """Only persisted FN orders belong in the Field Nation queue.

    Live OnFleet clusters may inherit a task's FN status even when their
    clustering/hash no longer corresponds to a saved FN work order. They
    must not appear as selectable phantom routes with blank WO/dates.
    """
    if not isinstance(ghost, dict):
        return False
    work_order = str(ghost.get("wo") or "").strip()
    return bool(work_order and work_order.lower() not in (
        "none", "null", "unknown", "n/a", "field nation route",
        "field nation route (no linked work order)",
    ))


def _fn_saved_entries(ghost_db, pods, merge_same_wo_ghosts=None):
    entries = []
    for pod in pods:
        route_pod = "Global_Digital" if pod == "Digital" else pod
        ghosts = (ghost_db or {}).get(route_pod, [])
        if merge_same_wo_ghosts:
            ghosts = merge_same_wo_ghosts(ghosts)
        for ghost in ghosts:
            route_hash = str(ghost.get("hash") or "")
            if (not route_hash or not _has_linked_fn_work_order(ghost)
                    or str(ghost.get("status") or "").lower() not in ("field_nation", "posted")):
                continue
            if st.session_state.get(f"reverted_{route_hash}", False):
                continue
            route = {"_is_ghost": True, "wo": ghost.get("wo", ""), "_ghost_record": ghost,
                     "city": ghost.get("city", "Unknown"), "state": ghost.get("state", ""),
                     "stops": ghost.get("stops", ghost.get("lCnt", 0)), "data": _fn_ghost_tasks(ghost)}
            route["_bundle_label"] = _bundle_label(route, _bundle_sets_for_pod(route_pod))
            entries.append((route_pod, route, "Field Nation", route_hash, None))
    return _dedupe_route_entries(entries)


def _fn_assigned_history_entries(assigned_orders, pods, pod_configs=None):
    """Read-only historical cards; original FN WO is the unique identity."""
    entries = []
    for order in assigned_orders or []:
        payload = order.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        state = str(payload.get("state") or "").strip().upper()
        hint = str(payload.get("pod") or "").strip()
        try:
            digital_count = int(payload.get("dCnt") or 0)
        except (TypeError, ValueError):
            digital_count = 0
        job_text = str(payload.get("jobOnly") or "").lower()
        digital = (hint in ("Digital", "Global_Digital") or digital_count > 0
                   or any(marker in job_text for marker in
                          ("service", "offline", "ins/rem", "🔌", "🔧", "⚙️", "📵")))
        if digital:
            pod = "Digital"
        elif hint in pods:
            pod = hint
        else:
            pod = next((name for name, config in (pod_configs or {}).items()
                        if state in config.get("states", ())), None)
        if pod not in pods:
            continue
        original_wo = str(order.get("work_order") or "").strip()
        if not original_wo:
            continue
        accepted_wo = str(order.get("assigned_work_order") or payload.get("wo")
                          or original_wo).strip()
        assigned_at = str(order.get("assigned_at") or "")
        provider = str(order.get("provider") or payload.get("fn_provider") or "").strip()
        route_hash = str(payload.get("cluster_hash") or "").strip()
        if not route_hash:
            route_hash = hashlib.sha256(original_wo.encode()).hexdigest()
        raw_ids = payload.get("taskIds") or ""
        task_ids = ([str(i).strip() for i in raw_ids if str(i).strip()]
                    if isinstance(raw_ids, list) else
                    [i.strip() for i in str(raw_ids).replace("|", ",").split(",") if i.strip()])
        stop_data = payload.get("stopData") or []
        if isinstance(stop_data, str):
            try:
                stop_data = json.loads(stop_data)
            except (TypeError, ValueError):
                stop_data = []
        if not isinstance(stop_data, list):
            stop_data = []
        try:
            stops = int(payload.get("lCnt") or len(stop_data))
            tasks = int(payload.get("tCnt") or len(task_ids))
        except (TypeError, ValueError):
            stops, tasks = len(stop_data), len(task_ids)
        ghost = {
            "wo": accepted_wo, "original_wo": original_wo,
            "status": "assigned", "contractor_name": provider or "Field Nation provider",
            "city": str(payload.get("city") or "Unknown"), "state": state,
            "hash": route_hash, "stops": stops, "tasks": tasks,
            "task_ids": task_ids, "route_ts": assigned_at,
            "due": payload.get("due") or "N/A", "locs": payload.get("locs") or "",
            "stop_data": stop_data, "fn_provider": provider,
        }
        route = {
            "_is_ghost": True, "_fn_assigned_history": True,
            "_fn_original_wo": original_wo, "_fn_assigned_at": assigned_at,
            "_fn_history_provider": provider, "_ghost_record": ghost,
            "wo": accepted_wo, "city": ghost["city"], "state": state,
            "stops": stops, "data": [],
        }
        entries.append(("Global_Digital" if pod == "Digital" else pod,
                        route, "Field Nation", route_hash, None))
    return entries


def _fn_route_identities(entries):
    # Changing workflow or task hashes on the same WO is not a new route.
    return {(entry[0], str(entry[1].get("_fn_original_wo")
                           or entry[1].get("wo") or entry[3])) for entry in entries}


def _refresh_fn_route_list(matching, posted, providers, context):
    fetch_records, pods, search, merge, engine, pod_configs = context
    signature = (tuple(pods), search)
    checked = st.session_state.get("_revamp_fn_checked_entries")
    if checked and checked[0] == signature:
        matching, posted, providers = checked[1:]
    with st.container(key="revamp_routes_heading"):
        title_col, check_col, notice_col = st.columns([58, 20, 300], vertical_alignment="center")
        with title_col:
            st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
        with notice_col:
            notice_placeholder = st.empty()
        with check_col:
            with st.container(key="revamp_quiet_control"):
                manual = st.button("↻", key="revamp_fn_saved_refresh", help="Refresh Field Nation routes", use_container_width=True)
        if manual:
            _routes_refresh_style(True)
            notice_placeholder.markdown('<span class="rv-task-note">Checking routes…</span>', unsafe_allow_html=True)
            try:
                old_ids = _fn_route_identities(matching)
                fetch_records.clear()
                sent, ghosts, archived, history = fetch_records()
                entries = _fn_saved_entries(ghosts, pods, merge)
                if engine is not None:
                    from migration import data_access as fn_data
                    entries.extend(_fn_assigned_history_entries(
                        fn_data.get_fn_assigned_orders(engine), pods, pod_configs))
                new_ids = _fn_route_identities(entries) - old_ids
                added = len(new_ids)
                st.session_state.setdefault("_revamp_fn_new_routes", set()).update(new_ids)
                matching = [entry for entry in entries if _entry_matches(entry, "Field Nation", search)]
                posted = (ghosts or {}).get("_fn_posted", {}) or {}
                providers = (ghosts or {}).get("_fn_provider", {}) or {}
                st.session_state.update(sent_db=sent, ghost_db=ghosts, archived_wos=archived, _history_db=history)
                st.session_state["_revamp_fn_checked_entries"] = (signature, matching, posted, providers)
                when = datetime.now(ZoneInfo("America/Chicago")).strftime("%I:%M:%S %p").lstrip("0") + " CT"
                st.session_state["_revamp_fn_refresh_notice"] = f"{added} new {'route' if added == 1 else 'routes'} added · {when}"
            except Exception as exc:
                print(f"[revamp/fn-check] keeping existing routes: {type(exc).__name__}", flush=True)
                st.session_state["_revamp_fn_refresh_notice"] = "Check unavailable · retry"
        with notice_col:
            notice = st.session_state.get("_revamp_fn_refresh_notice", "")
            notice_placeholder.markdown(f'<span class="rv-task-note">{html.escape(notice)}</span>', unsafe_allow_html=True)
    _routes_refresh_style(False)
    if _workspace_statuses(st.session_state.get("_auth_user") or {}) == ("Field Nation",):
        active_stage = st.session_state.get("revamp_fn_stage_filter", "Pending")
        matching = [entry for entry in matching
                    if _fn_entry_stage(entry, posted, providers) == active_stage]
    return matching, posted, providers


@st.fragment
def _render_route_list(matching, status, fn_posted, fn_providers,
                       quiet_context=None, fn_refresh_context=None):
    """State toggles rerun only this list; route clicks refresh the detail pane."""
    busy = False
    if quiet_context:
        pods, process_pod, process_digital_pool, cluster_store, fetch_open_tasks, eligible_ics, haversine, search = quiet_context[:8]
        fetch_saved_records = quiet_context[8] if len(quiet_context) > 8 else None
        with st.container(key='revamp_routes_heading'):
            title_col, check_col, notice_col = st.columns([58, 20, 300], vertical_alignment="center")
            with title_col:
                st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
            with notice_col:
                notice_placeholder = st.empty()
            with check_col:
                with st.container(key='revamp_quiet_control'):
                    busy = _quiet_routes_check(pods, process_pod, process_digital_pool, cluster_store, fetch_open_tasks, notice_placeholder, fetch_saved_records)
            with notice_col:
                notice, when = st.session_state.get('_revamp_quiet_notice', ('', 0))
                if st.session_state.get('_revamp_quiet_error'):
                    notice = 'Check unavailable · retry'
                notice_placeholder.markdown(f'<span class="rv-task-note">{html.escape(notice)}</span>', unsafe_allow_html=True)
        _routes_refresh_style(busy)
        completed = st.session_state.pop('_revamp_manual_check_complete', False)
        revision = (st.session_state.get('_revamp_quiet_revision', 0), tuple(pods), status, search)
        cached = st.session_state.get('_revamp_quiet_pending')
        # Initial and ordinary renders use the already-built main queue.
        # Only a completed manual check may replace pending entries here.
        if completed or (cached and cached[0] == revision):
            # Rebuild only pending cards. Saved/history cards and the detail pane
            # stay mounted. Dispatch actions independently verify every task live.
            if cached and cached[0] == revision:
                pending = cached[1]
            else:
                pending = []
                all_pending = []
                for pod in pods:
                    store_key = 'global_digital_clusters' if pod == 'Digital' else f'clusters_{pod}'
                    route_pod = 'Global_Digital' if pod == 'Digital' else pod
                    for route in st.session_state.get(store_key, []):
                        if bool(route.get('is_digital')) != (pod == 'Digital'):
                            continue
                        nearest = _nearest_ic(route, eligible_ics, haversine)
                        route_hash = _route_hash(route)
                        state = _route_status(route, st.session_state.get('sent_db') or {}, nearest[1] if nearest else None, route_pod)
                        display_route = {**route, '_bundle_label': _bundle_label(route, _bundle_sets_for_pod(route_pod))}
                        entry = (route_pod, display_route, state, route_hash, nearest)
                        if state in ('Ready', 'Flagged'):
                            all_pending.append(entry)
                            if _entry_matches(entry, status, search):
                                pending.append(entry)
                st.session_state['_revamp_quiet_all_pending'] = (tuple(pods), all_pending)
                st.session_state['_revamp_quiet_pending'] = (revision, pending)
            # Existing order survives checks; newly discovered routes append.
            by_key = {_route_entry_key(*entry[:4]): entry for entry in pending}
            old_order = st.session_state.get('_revamp_quiet_list_order', [])
            pending = [by_key.pop(key) for key in old_order if key in by_key] + list(by_key.values())
            st.session_state['_revamp_quiet_list_order'] = [_route_entry_key(*entry[:4]) for entry in pending]
            matching = [entry for entry in matching if entry[2] not in ('Ready', 'Flagged')] + pending
            st.session_state['_revamp_quiet_visible_keys'] = [f'{entry[0]}:{entry[3]}' for entry in pending]
    elif fn_refresh_context:
        matching, fn_posted, fn_providers = _refresh_fn_route_list(matching, fn_posted, fn_providers, fn_refresh_context)
    else:
        st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
    with st.container(height=680, border=False, key="revamp_route_scroll_fn" if status == "Field Nation" else "revamp_route_scroll"):
        if not matching:
            st.info("No matching routes.")
        grouped = _route_list_groups(matching, status, fn_posted, fn_providers)
        last_pod = None
        last_stage = None
        for group_index, ((group_pod, workflow_stage, group_label), entries) in enumerate(grouped.items()):
            if status == "Field Nation":
                if group_pod != last_pod:
                    pod_key = f"_revamp_fn_pod_open_{group_pod}"
                    st.session_state.setdefault(pod_key, True)
                    pod_count = sum(len(routes) for group, routes in grouped.items() if group[0] == group_pod)
                    st.button(f"{group_pod} Pod · {pod_count} {'route' if pod_count == 1 else 'routes'} {'−' if st.session_state[pod_key] else '+'}",
                              key=f"revamp_fn_pod_toggle_{group_pod}",
                              on_click=_toggle_state_group, args=(pod_key,),
                              use_container_width=True)
                    last_pod = group_pod
                    last_stage = None
                if not st.session_state[f"_revamp_fn_pod_open_{group_pod}"]:
                    continue
                if workflow_stage != last_stage:
                    st.markdown(f'<div class="fn-workflow-heading fn-workflow-{workflow_stage.lower()}">{workflow_stage}</div>', unsafe_allow_html=True)
                    last_stage = workflow_stage
            stage = f"{group_pod}_{workflow_stage}" if status == "Field Nation" else ""
            safe_group = re.sub(r"[^A-Za-z0-9_-]+", "_", group_label)
            group_key = f"_revamp_group_{status}_{stage}_{safe_group}"
            st.session_state.setdefault(group_key, group_index == 0)
            is_open = st.session_state[group_key]
            if status == "Sent":
                # Date-header checkbox selects this day only, including collapsed routes.
                day_keys = [
                    f"_reroute_chosen_{_route_entry_key(*entry[:4])}"
                    for entry in entries if entry[2] == "Sent"
                ]
                date_check_key = f"revamp_sent_day_select_{safe_group}"
                all_day_selected = bool(day_keys) and all(
                    st.session_state.get(k, False) for k in day_keys
                )
                st.session_state[date_check_key] = all_day_selected

                def _toggle_sent_day(keys=tuple(day_keys), checkbox_key=date_check_key):
                    checked = bool(st.session_state.get(checkbox_key))
                    for route_key in keys:
                        st.session_state[route_key] = checked
                        # A visible checkbox may already exist; synchronize its
                        # ephemeral widget key before the next fragment run.
                        widget_key = route_key.replace("_reroute_chosen_", "revamp_reroute_", 1)
                        st.session_state[widget_key] = checked
                    _refresh_bulk_actions()

                day_checkbox_col, day_label_col = st.columns(
                    [.09, .91], gap="small", vertical_alignment="center"
                )
                with day_checkbox_col:
                    st.checkbox(f"Select all Sent routes for {group_label}",
                                key=date_check_key, label_visibility="collapsed",
                                on_change=_toggle_sent_day)
                with day_label_col:
                    st.button(
                        f"{group_label}  ·  {len(entries)} {'route' if len(entries) == 1 else 'routes'}  {'−' if is_open else '+'}",
                        key=f"revamp_group_toggle_{status}_{stage}_{safe_group}",
                        on_click=_toggle_state_group, args=(group_key,),
                        use_container_width=True,
                    )
            else:
                st.button(f"{group_label}  ·  {len(entries)} {'route' if len(entries) == 1 else 'routes'}  {'−' if is_open else '+'}",
                          key=f"revamp_group_toggle_{status}_{stage}_{safe_group}",
                          on_click=_toggle_state_group, args=(group_key,),
                          use_container_width=True)
            if not is_open:
                continue
            for pod, route, state, route_hash, nearest in entries:
                stage = _fn_entry_stage((pod, route, state, route_hash, nearest), fn_posted, fn_providers) if status == "Field Nation" else ""
                key = _route_entry_key(pod, route, state, route_hash)
                city = route.get("city") or "Unknown city"
                select_col, card_col = st.columns([.09, .91], gap="small", vertical_alignment="center")
                with select_col:
                    if state in ("Ready", "Flagged"):
                        st.checkbox("Select route for Field Nation", key=f"revamp_bulk_{key}",
                                    label_visibility="collapsed", on_change=_refresh_bulk_actions)
                    elif status == "Field Nation" and not route.get("_fn_assigned_history"):
                        st.checkbox("Select for Field Nation CSV", key=f"revamp_fn_{key}",
                                    label_visibility="collapsed", on_change=_refresh_bulk_actions)
                    elif status in ("Sent", "Accepted", "Declined") and state == status:
                        # Persist selection outside widget state: collapsed date
                        # groups do not render their route checkboxes, and Streamlit
                        # deletes unrendered widget keys on subsequent reruns.
                        persistent_key = f"_reroute_chosen_{key}"
                        widget_key = f"revamp_reroute_{key}"
                        st.session_state[widget_key] = bool(
                            st.session_state.get(persistent_key, False)
                        )
                        def _route_checkbox_changed(w=widget_key, p=persistent_key):
                            st.session_state[p] = bool(st.session_state.get(w, False))
                            _refresh_bulk_actions()
                        st.checkbox("Select route for re-routing",
                                    key=widget_key, label_visibility="collapsed",
                                    on_change=_route_checkbox_changed)
                with card_col:
                    removal = " · CVS Removal" if _is_cvs_removal_route(route) else ""
                    card_state = stage if status == "Field Nation" else state
                    provider = str(route.get("_fn_history_provider") or fn_providers.get(route_hash) or "").strip()
                    stops = route.get("stops", 0)
                    _ghost = route.get("_ghost_record") or {}
                    tasks = (len(route.get("data", []))
                             or len(_ghost.get("task_ids") or [])
                             or int(_ghost.get("tasks") or _ghost.get("tCnt") or 0))
                    state_icon = {
                        "Ready": "●", "Flagged": "!", "Field Nation": "FN",
                        "Sent": "→", "Accepted": "✓", "Declined": "×", "Finalized": "✓", "Routed": "◆"
                    }.get(card_state, "•")
                    status_text = f"{state_icon} {card_state}"
                    if status == "Field Nation" and provider:
                        status_text += f" · {provider}"
                    if removal:
                        status_text += " · CVS Removal"
                    heading = _route_list_heading(route, state, route_hash)
                    if heading and removal:
                        heading += " · CVS Removal"
                    if heading is None:
                        heading = f"{city}, {route.get('state', '')}    {status_text}"
                    if status == "Field Nation" and card_state == "Pending" and _ghost.get("fn_csv_downloaded"):
                        # Green check must appear ON the exact route card.
                        # Durable saved-WO proof survives refresh/sign-in and
                        # is also required by the PostgreSQL Posted transition.
                        heading = f"✅ {heading} · CSV downloaded"
                    if status == "Field Nation" and route.get("_fn_assigned_history"):
                        # The Assigned card must display the new, confirmed
                        # work-order name, not merely the city or original FN
                        # queue identity. The detail pane already uses this WO.
                        assigned_wo = str(route.get("wo") or _ghost.get("wo") or "").strip()
                        if assigned_wo:
                            heading = f"{assigned_wo} · {heading}"
                    bundle = route.get('_bundle_label') or _bundle_label(route, _bundle_sets_for_pod(pod))
                    if bundle:
                        heading += f" · {bundle}"
                    if status == "Field Nation" and (pod, str(route.get("wo") or route_hash)) in st.session_state.get("_revamp_fn_new_routes", set()):
                        heading += " · :green-background[NEW]"
                    if status == "Field Nation":
                        sync_state = str((_ghost.get("fn_handoff_state") or "")).lower()
                        if sync_state in ("queued", "running"):
                            heading += " · OnFleet syncing"
                        elif sync_state == "error":
                            heading += " · OnFleet needs retry"
                    label = (f"{heading}\n"
                             f"{('Digital' if pod == 'Global_Digital' else pod)} Pod  ·  {stops} {'stop' if stops == 1 else 'stops'}  ·  "
                             f"{tasks} {'task' if tasks == 1 else 'tasks'}")
                    _important = _important_route_badges(route)
                    if _important:
                        label += "\n" + "  ·  ".join(_important)
                    if status == "Field Nation":
                        _created, _posted, _due = _fn_dates(route, route_hash, fn_posted)
                        _date_bits = [f"Due {_due}"]
                        if route.get("_fn_assigned_history"):
                            _date_bits.insert(0, f"Assigned {route.get('_fn_assigned_at') or '—'}")
                        elif card_state == "Pending":
                            _date_bits.insert(0, f"Sent {_created}")
                        elif card_state in ("Posted", "Assigned"):
                            _date_bits.insert(0, f"Posted {_posted}")
                        label += "\n" + "  ·  ".join(_date_bits)
                    elif card_state in ("Sent", "Accepted", "Declined", "Finalized"):
                        _saved = _saved_route_fields(route, _ghost)
                        _status_ts = _route_status_timestamp(route, card_state)
                        _date_bits = [f"{card_state} {_status_ts}"]
                        if card_state in ("Sent", "Accepted"):
                            _date_bits.append(f"Due {_saved.get('due') or 'N/A'}")
                        label += "\n" + "  ·  ".join(_date_bits)
                    if nearest:
                        label += f"\nClosest IC  ·  {nearest[0]}  ·  {nearest[1]:.1f} mi"
                    selected = st.session_state.get("revamp_selected_route") == key
                    state_key = str(card_state).replace(" ", "_")
                    if st.button(label, key=f"revamp_route_{state_key}_{key}",
                                 type="primary" if selected else "secondary",
                                 use_container_width=True):
                        st.session_state.get("_revamp_fn_new_routes", set()).discard((pod, str(route.get("wo") or route_hash)))
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
    """Return a verified FN order, or clear an orphaned local FN label safely.

    Re-clustering may change the live hash. Resolve by persisted WO or exact
    task IDs before touching OnFleet. Never revoke unrelated tasks based on
    a city, address, partial subset, or stale session label.
    """
    if db_engine is None:
        raise RuntimeError("Railway database is unavailable")
    from migration import data_access as _fn_data

    ghost = route.get("_ghost_record") or {}
    original_wo = str(ghost.get("wo") or route.get("wo") or "").strip()
    raw_task_ids = (ghost.get("task_ids") or ghost.get("taskIds")
                    or route.get("task_ids") or [
                        task.get("id") for task in (route.get("data") or [])
                        if isinstance(task, dict) and task.get("id")
                    ])
    resolved = _fn_data.resolve_fn_return_order(db_engine, route_hash,
                                                  original_wo, raw_task_ids)
    if not resolved.get("success"):
        if resolved.get("reason") == "not_found" and not ghost and route.get("data"):
            # This is a live, open OnFleet cluster that was mislabeled FN by
            # session_state after a failed / old transfer. No FN order exists.
            # Clear ONLY the false local UI label: no Postgres mutation and
            # critically NO OnFleet worker removal for unrelated tasks.
            st.session_state.pop(f"route_state_{route_hash}", None)
            st.session_state.pop(f"reverted_{route_hash}", None)
            st.session_state.pop(f"revamp_fn_{pod}:{route_hash}", None)
            st.session_state.pop("revamp_selected_route", None)
            for key in ("_revamp_quiet_pending", "_revamp_quiet_all_pending",
                        "_revamp_fn_checked_entries"):
                st.session_state.pop(key, None)
            fetch_sent_records_from_sheet.clear()
            return "stale_cleared"
        raise RuntimeError(resolved.get("error") or "Field Nation order was not verified")

    if not callable(move_to_dispatch):
        raise RuntimeError("The return-to-dispatch action is unavailable")
    # Use the durable task set (and saved original cluster hash), not an
    # incomplete live cluster with a possibly different/new route hash.
    saved_hash = resolved["cluster_hash"]
    if not saved_hash or not resolved["task_ids"]:
        raise RuntimeError("The saved Field Nation route is missing its task identity")
    payload = dict(resolved["payload"])
    payload["taskIds"] = ",".join(resolved["task_ids"])
    payload.setdefault("city", route.get("city") or "")
    payload.setdefault("state", route.get("state") or "")
    # A previous failed attempt may have set reverted_ locally while the
    # durable Field Nation record still exists. Explicit confirmation allows
    # retrying the release of exactly this verified task set.
    st.session_state.pop(f"reverted_{saved_hash}", None)
    release_result = move_to_dispatch(
        saved_hash, "Field Nation", pod,
        action_label="Field Nation Revoked",
        check_onfleet=True, cluster_data=payload, check_completed=True,
        verify_fn_return=True,
    )
    if release_result is False:
        raise RuntimeError("OnFleet release could not be confirmed. The Field Nation work order was preserved.")
    # Remove tracking only once all OnFleet tasks are confirmed released.
    # Failed attempts leave the original FN work order intact for retry.
    _fn_data.remove_field_nation(db_engine, resolved["work_order"])

    fetch_sent_records_from_sheet.clear()
    for candidate_hash in {route_hash, saved_hash}:
        st.session_state.pop(f"revamp_fn_{pod}:{candidate_hash}", None)
        st.session_state.pop(f"route_state_{candidate_hash}", None)
    st.session_state.pop("revamp_selected_route", None)
    for key in ("_revamp_quiet_pending", "_revamp_quiet_all_pending",
                "_revamp_fn_checked_entries"):
        st.session_state.pop(key, None)
    return "returned"

@st.fragment
def _render_workspace_summary(all_routes, pod_choice, loaded):
    current = st.session_state.get('_revamp_quiet_all_pending')
    if current and current[0] == tuple(loaded):
        all_routes = [entry for entry in all_routes if entry[2] not in ('Ready', 'Flagged')] + current[1]
    counts = {status: sum(1 for entry in all_routes if entry[2] == status)
              for status in (*STATUSES[1:], "Ready", "Flagged")}
    counts["Over 50 mi"] = sum(1 for entry in all_routes
                                if entry[4] and entry[4][1] > 50 and
                                entry[2] in ("Ready", "Flagged"))
    counts["CVS Removal"] = sum(
        1 for entry in all_routes
        if entry[2] in ("Ready", "Flagged") and entry[1].get("is_removal")
    )
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
        f'<span class="revamp-pill">Accepted <b>{counts["Accepted"]}</b></span>'
        f'<span class="revamp-pill">Finalized <b>{counts["Finalized"]}</b></span>',
        unsafe_allow_html=True,
    )




def _queue_fn_entries(chosen, fn_due, db_engine, assign_tasks_to_fn_team,
                      fetch_sent_records_from_sheet, notice_prefix=""):
    """Queue Ready/Flagged route entries in Field Nation (shared by the bulk
    button and the Dispatch Bot). Reruns the app on success."""
    fn_team_id = st.session_state.get("_fn_team_id")
    fn_worker_id = st.session_state.get("_fn_worker_id")
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
        return False
    from revamp_fn_queue import queue_bulk, schedule_queued
    try:
        saved, skipped, errors = queue_bulk(
            db_engine, [(pod, route) for pod, route, _, _, _ in chosen],
            fn_due, fn_team_id, fn_worker_id,
        )
    except Exception as exc:
        st.error(f"Could not queue Field Nation routes: {exc}")
        saved, skipped, errors = [], [], []
    _apply_bulk_fn_results(chosen, saved, skipped)
    if saved:
        try:
            schedule_queued(db_engine, [wo for _, wo in saved], assign_tasks_to_fn_team)
        except Exception as exc:
            st.warning("Field Nation rows were saved, but OnFleet sync could not "
                       "start immediately. It will resume on the next refresh.")
            print(f"[fn-handoff/schedule] {type(exc).__name__}: {exc}", flush=True)
    if saved or skipped:
        fetch_sent_records_from_sheet.clear()
        message = (f"{notice_prefix}Queued {len(saved)} route(s) in Field Nation. "
                   "OnFleet assignments and route plans are syncing separately.")
        if skipped:
            message += f" {len(skipped)} route(s) already existed."
        if errors:
            message += f" {len(errors)} could not be queued: " + "; ".join(msg for _, msg in errors[:3])
        st.session_state["_revamp_notice"] = ("warning" if errors else "success", message)
        if saved:
            st.session_state["_revamp_show_fn_next"] = True
        return True
    if errors:
        st.error(f"{len(errors)} route(s) could not be queued: " +
                 "; ".join(msg for _, msg in errors[:3]))
    return False


# ── Dispatch Bot ───────────────────────────────────────────────────────────
# Oct 15 2026 — Nick: "use a bot to go through my routes anytime I want,
# prioritize new IC and rate is $20-$25/stop." The bot only PROPOSES; the
# dispatcher reviews the plan, then the approved Field Nation routes are
# queued through the same path as "Send N to Field Nation", and the approved
# IC routes are pre-filled (IC + rate) on their normal dispatch cards so the
# existing Route Request send (collision checks, WO numbers, Outlook) is used.

def _bot_settings_from_state():
    from dispatch_bot import BotSettings
    d = BotSettings()
    return BotSettings(
        fn_distance_small_routes=float(st.session_state.get("bot_fn_miles", d.fn_distance_small_routes)),
        max_ic_distance=float(st.session_state.get("bot_max_miles", d.max_ic_distance)),
        max_open_tasks=int(st.session_state.get("bot_max_tasks", d.max_open_tasks)),
        max_waiting_routes=int(st.session_state.get("bot_max_waiting", d.max_waiting_routes)),
        new_ic_edge_miles=float(st.session_state.get("bot_new_edge", d.new_ic_edge_miles)),
        reoffer_max_miles=float(st.session_state.get("bot_reoffer_miles", d.reoffer_max_miles)),
        removal_max_miles=float(st.session_state.get("bot_removal_miles", d.removal_max_miles)),
    )


def _decline_history(db_engine):
    """(contractor_name, [task ids]) for every Route Request ever declined in the
    last 120 days, so a re-offer skips ICs who declined an earlier round."""
    if db_engine is None:
        return []
    cached = st.session_state.get("_bot_decline_history")
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    rows = []
    try:
        import sqlalchemy as sa
        with db_engine.connect() as conn:
            result = conn.execute(sa.text("""
                SELECT DISTINCT r.contractor_name, r.payload->>'taskIds' AS task_ids
                FROM routes r JOIN route_events e ON e.route_id = r.id
                WHERE e.action = 'processDecision' AND e.payload->>'decision' = 'decline'
                  AND r.created_at >= now() - interval '120 days'""")).mappings().all()
        rows = [(row["contractor_name"],
                 [t.strip() for t in str(row["task_ids"] or "").split(",") if t.strip()])
                for row in result]
    except Exception as exc:
        print(f"[dispatch-bot] decline history failed: {type(exc).__name__}: {exc}", flush=True)
    st.session_state["_bot_decline_history"] = (time.time(), rows)
    return rows


def _run_dispatch_bot(matching, helpers, haversine, sent_db, mode="routes", db_engine=None):
    import dispatch_bot as bot
    states = ("Declined",) if mode.startswith("reoffer") else ("Ready", "Flagged")
    entries = [e for e in matching if e[2] in states]
    ics = bot.candidate_ics(st.session_state.get("ic_df"), helpers.get("is_fa_employee"))
    try:
        worker_counts = helpers["fetch_worker_task_counts"]() or {}
        st.session_state["_worker_counts"] = worker_counts
    except Exception as exc:
        print(f"[dispatch-bot] worker counts failed: {type(exc).__name__}: {exc}", flush=True)
        worker_counts = {}
    get_gmaps, home_loc = helpers["get_gmaps"], helpers["ic_home_loc"]

    def route_hours(ic_row, route):
        center = _route_center(route) or route.get("center") or (0, 0)
        stops = list(dict.fromkeys(str(t.get("full") or "") for t in route.get("data", []) if t.get("full")))
        _, hours, _, _ = get_gmaps(home_loc(ic_row, f"{center[0]},{center[1]}"),
                                   tuple(stops), tasks=route.get("data"))
        return hours

    waiting = bot.waiting_routes_by_ic(sent_db)
    settings = _bot_settings_from_state()
    if mode.startswith("reoffer"):
        declined = bot.decliners_by_key(entries, sent_db, _decline_history(db_engine))
        removals = [e for e in entries if e[1].get("is_removal")]
        regular = [e for e in entries if not e[1].get("is_removal")]
        plan = (bot.plan_routes(regular, ics, haversine, _route_center, route_hours,
                                worker_counts, waiting, helpers["selection_key"], settings,
                                mode="reoffer", declined_by=declined)
                + bot.plan_routes(removals, ics, haversine, _route_center, route_hours,
                                  worker_counts, waiting, helpers["selection_key"], settings,
                                  mode="removals", declined_by=declined, states=("Declined",)))
    else:
        # Never offer a route to someone who already declined those tasks.
        declined = bot.decliners_by_key(entries, sent_db, _decline_history(db_engine))
        plan = bot.plan_routes(entries, ics, haversine, _route_center, route_hours,
                               worker_counts, waiting, helpers["selection_key"], settings,
                               mode=mode, declined_by=declined)
    st.session_state["_dispatch_bot_plan"] = plan
    st.session_state["_dispatch_bot_plan_mode"] = mode
    st.session_state["_dispatch_bot_counts_known"] = bool(worker_counts)


def _apply_bot_ic_prefill(p):
    """Pre-select the IC and rate on the route's normal dispatch card."""
    pod, h = p.pod, p.route_hash
    st.session_state[f"sel_{pod}_{h}_identity_v2"] = p.ic_identity
    st.session_state[f"last_sel_{pod}_{h}_identity_v2"] = p.ic_identity
    st.session_state[f"_pay_master_{pod}_{h}"] = p.pay
    st.session_state[f"_rate_master_{pod}_{h}"] = p.rate
    st.session_state[f"_auto_rate_{pod}_{h}"] = p.rate
    st.session_state[f"_pay_ver_{pod}_{h}"] = int(st.session_state.get(f"_pay_ver_{pod}_{h}", 0)) + 1
    # Approving the bot plan is the dispatcher's authorization for a Flagged
    # route whose IC is within the bot's distance limit.
    st.session_state[f"lock_{pod}_{h}"] = True


def _task_ids(route):
    return frozenset(str(t.get("id") or "").strip() for t in (route.get("data") or [])
                     if str(t.get("id") or "").strip())


def _resolve_bot_followups(all_routes, by_key, db_engine, default_due_days,
                           assign_tasks_to_fn_team, fetch_sent_records_from_sheet):
    """Second half of a re-offer: once a declined route is back in Routes /
    CVS Removal (after move_to_dispatch), prefill its next IC or queue it to FN.
    Matches by task IDs because re-clustering can change the route hash."""
    followups = st.session_state.get("_bot_decline_followups") or {}
    if not followups:
        return
    live_entries = [e for e in all_routes if e[2] in ("Ready", "Flagged")]
    fn_entries, ic_keys, info = [], [], {}
    remaining = {}
    for old_key, item in followups.items():
        p, tids = item["proposal"], item["task_ids"]
        match = by_key.get(old_key) if old_key in by_key and by_key[old_key][2] in ("Ready", "Flagged") else None
        if match is None and tids:
            match = next((e for e in live_entries if _task_ids(e[1]) and _task_ids(e[1]) <= tids
                          and len(_task_ids(e[1])) * 2 >= len(tids)), None)
        if match is None:
            if time.time() - item["at"] < 300:
                remaining[old_key] = item
            else:
                st.warning(f"🤖 {p.city}, {p.state}: the declined route did not come back to "
                           "Routes within 5 minutes. Check it in the Declined tab.")
            continue
        key = f"{match[0]}:{match[3]}"
        if p.action == "fn":
            fn_entries.append(match)
            continue
        p.pod, p.route_hash, p.key = match[0], match[3], key
        _apply_bot_ic_prefill(p)
        ic_keys.append(key)
        info[key] = {"ic": p.ic_name, "rate": p.rate, "removal": bool(match[1].get("is_removal"))}
    st.session_state["_bot_decline_followups"] = remaining
    if ic_keys:
        queue = list(st.session_state.get("_dispatch_bot_queue") or [])
        st.session_state["_dispatch_bot_queue"] = queue + [k for k in ic_keys if k not in queue]
        st.session_state.setdefault("_dispatch_bot_info", {}).update(info)
        st.session_state["revamp_selected_route"] = ic_keys[0]
        st.session_state["_revamp_show_cvs_next" if info[ic_keys[0]]["removal"]
                         else "_revamp_show_routes_next"] = True
    if fn_entries:
        due = min((item.get("due") or date.today() + timedelta(days=default_due_days))
                  for item in followups.values() if item["proposal"].action == "fn")
        if _queue_fn_entries(fn_entries, due, db_engine, assign_tasks_to_fn_team,
                             fetch_sent_records_from_sheet,
                             notice_prefix="🤖 Declined route(s) with nobody left nearby: "):
            st.rerun()
    elif ic_keys:
        st.session_state["_revamp_notice"] = (
            "success", f"🤖 {len(ic_keys)} declined route(s) lined up for the next IC.")
        st.rerun()


def _drive_bulk_generate(queue, by_key):
    """'Generate all': open each queued route's card and fire its normal
    GENERATE LINK (same collision / in-flight / pause checks) one per rerun.
    A route still live after its attempt means it failed: stop there."""
    if not queue:
        return
    current = queue[0]
    tried = st.session_state.get("_bot_bulk_current")
    if tried == current:
        st.session_state.pop("_bot_bulk_drafts", None)
        st.session_state.pop("_bot_bulk_current", None)
        entry = by_key.get(current)
        where = f"{entry[1].get('city', '')}, {entry[1].get('state', '')}" if entry else current
        st.session_state["revamp_selected_route"] = current
        st.warning(f"🤖 Generate all stopped at **{where}** — see the message on its card. "
                   "Fix it, then click Generate all again for the rest.")
        return
    entry = by_key.get(current)
    if entry is None:
        return
    pod, route, _state, route_hash, _ = entry
    want = "CVS Removal" if route.get("is_removal") else "Routes"
    st.session_state["_bot_bulk_current"] = current
    st.session_state["revamp_selected_route"] = current
    st.session_state[f"_resume_generate_{pod}_{route_hash}"] = True
    if st.session_state.get("revamp_status") != want:
        st.session_state.pop("_bot_bulk_current", None)
        st.session_state["_revamp_show_cvs_next" if want == "CVS Removal" else "_revamp_show_routes_next"] = True
        st.rerun()


def _render_bulk_draft_results():
    results = st.session_state.get("_bot_bulk_results")
    if not results:
        return
    saved = [r for r in results if r.get("saved")]
    pending = [r for r in results if not r.get("saved")]
    running = bool(st.session_state.get("_bot_bulk_drafts"))
    with st.container(key="revamp_bot_drafts", border=True):
        head, clear = st.columns([6, 1], vertical_alignment="center")
        with head:
            st.markdown(f"**📥 Generated {len(results)} Route Request(s)**"
                        + (" — still going…" if running else "")
                        + (f" · {len(saved)} saved to Outlook Drafts" if saved else ""))
        with clear:
            if not running and st.button("Clear", key="bot_drafts_clear", use_container_width=True):
                st.session_state.pop("_bot_bulk_results", None)
                st.rerun()
        if pending:
            st.caption("Not in Drafts yet. Ask Claude to \"save the drafts\" and it will save each email "
                       "below into your Outlook Drafts, or open each one with its Outlook link.")
            for i, r in enumerate(pending, 1):
                with st.expander(f"Draft {i}: {r['subject']} → {r['ic']} <{r['to'] or 'NO EMAIL'}>"
                                 + (f"  ⚠️ {r['error']}" if r.get("error") else "")):
                    st.markdown(f"[Open in Outlook]({r['outlook_url']})")
                    st.code(f"DRAFT-TO: {r['to']}\nDRAFT-SUBJECT: {r['subject']}\n"
                            f"DRAFT-BODY:\n{r['body']}\nDRAFT-END", language=None)


def _render_dispatch_bot(matching, all_routes, helpers, haversine, sent_db, db_engine,
                         default_due_days, assign_tasks_to_fn_team,
                         fetch_sent_records_from_sheet, status="Routes", move_to_dispatch=None):
    if not helpers:
        return
    by_key = {f"{e[0]}:{e[3]}": e for e in all_routes}

    def live(key):
        return key in by_key and by_key[key][2] in ("Ready", "Flagged")

    _resolve_bot_followups(all_routes, by_key, db_engine, default_due_days,
                           assign_tasks_to_fn_team, fetch_sent_records_from_sheet)

    _render_bulk_draft_results()

    # ── Active send queue from an approved plan ──
    if "_dispatch_bot_queue" in st.session_state:
        queue = [k for k in st.session_state["_dispatch_bot_queue"] if live(k)]
        if st.session_state.get("_bot_bulk_drafts"):
            _drive_bulk_generate(queue, by_key)
        if not queue:
            st.session_state.pop("_dispatch_bot_queue", None)
            st.session_state.pop("_bot_bulk_drafts", None)
            st.success("🤖 Bot queue finished — every approved Route Request was generated.")
        else:
            st.session_state["_dispatch_bot_queue"] = queue
            visible = {f"{e[0]}:{e[3]}" for e in matching}
            selected = st.session_state.get("revamp_selected_route")
            if selected not in queue and selected not in visible:
                st.session_state["revamp_selected_route"] = queue[0]
                selected = queue[0]
            info = st.session_state.get("_dispatch_bot_info", {})
            current = info.get(selected if selected in queue else queue[0], {})
            with st.container(key="revamp_bot_queue", border=True):
                msg_col, all_col, next_col, stop_col = st.columns([4.2, 1.9, 1.5, 0.9], vertical_alignment="center")
                with all_col:
                    from migration import outlook_drafts as _od
                    if st.button(f"📥 Generate all ({len(queue)})", key="bot_queue_all", type="primary",
                                 use_container_width=True,
                                 help=("Generates every queued Route Request and saves each email to "
                                       "Outlook Drafts." if _od.configured() else
                                       "Generates every queued Route Request without opening Outlook, "
                                       "then lists the emails so the drafts can be saved in one go.")):
                        st.session_state["_bot_bulk_drafts"] = True
                        st.session_state["_bot_bulk_results"] = []
                        st.session_state.pop("_bot_bulk_current", None)
                        st.rerun()
                with msg_col:
                    st.markdown(
                        f"**🤖 Bot queue — {len(queue)} Route Request(s) left.** "
                        f"The card at right has **{html.escape(current.get('ic', ''))}** at "
                        f"**${current.get('rate', 0):.2f}/stop** filled in. Click "
                        "**GENERATE LINK & OPEN OUTLOOK**, send the email, then go to the next one — "
                        "or click **Generate all** to do every route at once without opening Outlook.")
                with next_col:
                    if st.button("Next route ▶", key="bot_queue_next", use_container_width=True):
                        pos = queue.index(selected) + 1 if selected in queue else 0
                        nxt = queue[pos % len(queue)]
                        st.session_state["revamp_selected_route"] = nxt
                        if nxt in by_key:
                            st.session_state["_revamp_show_cvs_next" if by_key[nxt][1].get("is_removal")
                                             else "_revamp_show_routes_next"] = True
                        st.rerun()
                with stop_col:
                    if st.button("Stop", key="bot_queue_stop", use_container_width=True):
                        st.session_state.pop("_dispatch_bot_queue", None)
                        st.session_state.pop("_bot_bulk_drafts", None)
                        st.rerun()

    pod_label = lambda pod: "Digital" if pod == "Global_Digital" else pod

    # ── Declined routes: line up the next IC (Nick, Oct 15 2026) ──
    pending_old = set((st.session_state.get("_bot_decline_followups") or {}).keys())
    declined = [e for e in all_routes if e[2] == "Declined" and e[1].get("data")
                and e[0] not in ("Global_Digital", "Digital")
                and _display_contractor_name(e[1], "Declined").lower() != "field nation"
                and f"{e[0]}:{e[3]}" not in pending_old]
    if declined and callable(move_to_dispatch):
        with st.container(key="revamp_bot_declines", border=True):
            msg_col, btn_col = st.columns([4.4, 2], vertical_alignment="center")
            with msg_col:
                st.markdown(f"**🔁 {len(declined)} declined route(s).** The bot lines each one up for the "
                            "next closest IC who hasn't declined it (Routes: 40 mi, CVS Removals: 30 mi). "
                            "If nobody is left, it goes to Field Nation.")
            with btn_col:
                if st.button("🤖 Line up next IC", key="bot_reoffer", type="primary",
                             use_container_width=True):
                    with st.spinner("Finding the next IC for declined routes…"):
                        _run_dispatch_bot(declined, helpers, haversine, sent_db,
                                          mode="reoffer", db_engine=db_engine)
                        st.session_state["_dispatch_bot_plan_pod"] = "Declined"

    # ── Run / review ──
    # Nick: the bot runs on ONE pod at a time, and only from the Routes tab
    # (route rules) or the CVS Removal tab (removal rules).
    bot_mode = {"Routes": "routes", "CVS Removal": "removals"}.get(status)
    pods = sorted({e[0] for e in matching if e[2] in ("Ready", "Flagged")}, key=pod_label) if bot_mode else []
    if st.session_state.get("bot_pod") not in pods:
        st.session_state.pop("bot_pod", None)
    run = False
    if bot_mode:
        with st.container(key="revamp_dispatch_bot"):
            pod_col, run_col, set_col, info_col = st.columns([1.3, 1.7, 1.3, 2.7], vertical_alignment="center")
            with pod_col:
                bot_pod = st.selectbox("Pod", pods, key="bot_pod", format_func=pod_label,
                                       index=None, placeholder="Choose a pod",
                                       label_visibility="collapsed")
            pod_entries = [e for e in matching if e[0] == bot_pod and e[2] in ("Ready", "Flagged")]
            ready_count = len(pod_entries)
            with run_col:
                run = st.button(f"🤖 Run Dispatch Bot ({ready_count})", key="bot_run",
                                type="primary", disabled=not ready_count, use_container_width=True)
            with set_col:
                with st.popover("Bot settings", use_container_width=True):
                    from dispatch_bot import BotSettings
                    d = BotSettings()
                    st.number_input("1–5 stop routes go to Field Nation when the closest IC is over (mi)",
                                    min_value=1.0, value=d.fn_distance_small_routes, step=5.0, key="bot_fn_miles")
                    st.number_input("Farthest IC the bot will pick (mi)",
                                    min_value=5.0, value=d.max_ic_distance, step=5.0, key="bot_max_miles")
                    st.number_input("Skip ICs with this many open OnFleet tasks (🔵)",
                                    min_value=1, value=d.max_open_tasks, step=1, key="bot_max_tasks")
                    st.number_input("Skip ICs with this many Route Requests waiting on a reply",
                                    min_value=1, value=d.max_waiting_routes, step=1, key="bot_max_waiting")
                    st.number_input("A new IC (0 tasks) wins if within this many miles of the closest IC",
                                    min_value=0.0, value=d.new_ic_edge_miles, step=1.0, key="bot_new_edge")
                    st.number_input("After a decline, keep offering out to (mi), then Field Nation",
                                    min_value=5.0, value=d.reoffer_max_miles, step=5.0, key="bot_reoffer_miles")
                    st.number_input("CVS Removals: any IC within (mi), else Field Nation",
                                    min_value=5.0, value=d.removal_max_miles, step=5.0, key="bot_removal_miles")
                    st.caption("Rate: 1–5 stops = calculated rate kept between $20.00 and $24.50/stop. "
                               "6+ stops = $20.00/stop.")
            with info_col:
                st.caption(("CVS Removals: closest IC within 30 mi, nobody doubled up while others are free. "
                            if bot_mode == "removals" else "") +
                           "Pick a pod, then run. Nothing is sent until you approve.")
    if run:
        with st.spinner(f"Dispatch Bot is matching ICs and pricing {pod_label(bot_pod)} routes…"):
            _run_dispatch_bot(pod_entries, helpers, haversine, sent_db, mode=bot_mode,
                              db_engine=db_engine)
            st.session_state["_dispatch_bot_plan_pod"] = bot_pod

    plan = st.session_state.get("_dispatch_bot_plan")
    if not plan:
        return
    plan_mode = st.session_state.get("_dispatch_bot_plan_mode", "routes")
    reoffer = plan_mode.startswith("reoffer")
    if reoffer:
        plan = [p for p in plan if p.key in by_key and by_key[p.key][2] == "Declined"]
    else:
        plan = [p for p in plan if live(p.key)]
    actionable = [p for p in plan if p.action in ("ic", "fn")]
    skipped = [p for p in plan if p.action == "skip"]
    import pandas as pd
    with st.container(key="revamp_bot_review", border=True):
        n_ic = sum(p.action == "ic" for p in actionable)
        n_new = sum(p.action == "ic" and p.is_new_ic for p in actionable)
        n_fn = sum(p.action == "fn" for p in actionable)
        title = ("Declined routes — next IC" if reoffer else
                 f"{pod_label(st.session_state.get('_dispatch_bot_plan_pod', ''))} "
                 f"{'CVS Removal' if plan_mode == 'removals' else 'pod'} plan")
        st.markdown(f"**🤖 {title}** — {n_ic} Route Request(s) "
                    f"({n_new} to new ICs 🆕), {n_fn} to Field Nation, {len(skipped)} left for you. "
                    "Untick any row you don't want.")
        if not st.session_state.get("_dispatch_bot_counts_known"):
            st.warning("OnFleet task counts could not be loaded, so the 'new IC' and 'loaded up' "
                       "checks only used Route Requests waiting on a reply.")
        rows = [{
            "Send": True,
            "Route": f"{p.city}, {p.state}",
            "Pod": "Digital" if p.pod == "Global_Digital" else p.pod,
            "Stops": p.stops,
            "Goes to": (f"🆕 {p.ic_name}" if p.is_new_ic else p.ic_name) if p.action == "ic" else "Field Nation",
            "Mi": (f"{p.ic_miles:.1f}" if p.action == "ic" else
                   f"{p.closest_miles:.1f}" if p.closest_miles is not None else "—"),
            "🔵 Tasks": (str(p.ic_open_tasks) if p.action == "ic" and p.ic_open_tasks is not None
                        else "?" if p.action == "ic" else ""),
            "$/stop": f"${p.rate:.2f}" if p.action == "ic" else "",
            "Pay": f"${p.pay:.2f}" if p.action == "ic" else "",
            "Why": p.reason,
        } for p in actionable]
        keep = []
        if rows:
            edited = st.data_editor(
                pd.DataFrame(rows), key="bot_plan_editor", hide_index=True, use_container_width=True,
                disabled=[c for c in rows[0] if c != "Send"],
                column_config={
                    "Send": st.column_config.CheckboxColumn(width="small"),
                    "Why": st.column_config.TextColumn(width="large"),
                })
            keep = list(edited["Send"])
        if skipped:
            with st.expander(f"{len(skipped)} route(s) the bot left for you"):
                st.dataframe(pd.DataFrame([{
                    "Route": f"{p.city}, {p.state}",
                    "Pod": "Digital" if p.pod == "Global_Digital" else p.pod,
                    "Stops": p.stops, "Closest IC mi": p.closest_miles, "Why": p.reason}
                    for p in skipped]), hide_index=True, use_container_width=True)
        chosen = [p for p, k in zip(actionable, keep) if k]
        needs_db = any(p.action == "fn" for p in chosen) and db_engine is None
        due_col, go_col, clear_col = st.columns([1.3, 2, 1], vertical_alignment="bottom")
        with due_col:
            fn_due = st.date_input("Field Nation due date",
                                   value=date.today() + timedelta(days=default_due_days),
                                   key="bot_fn_due")
        with go_col:
            approve = st.button(f"Approve {len(chosen)} route(s)", key="bot_approve", type="primary",
                                disabled=not chosen or needs_db, use_container_width=True)
        with clear_col:
            if st.button("Discard plan", key="bot_discard", use_container_width=True):
                st.session_state.pop("_dispatch_bot_plan", None)
                st.rerun()
        if needs_db:
            st.caption("Field Nation routes need the Railway database connection.")
    if not approve:
        return
    if reoffer:
        # Send each declined route back to dispatch exactly like "Remove route"
        # does, then finish (prefill next IC / queue FN) once it is live again.
        followups = dict(st.session_state.get("_bot_decline_followups") or {})
        for p in chosen:
            entry = by_key.get(p.key)
            if not entry:
                continue
            pod, route, _state, route_hash, _ = entry
            move_to_dispatch(route_hash, _display_contractor_name(route, "Declined"), pod,
                             action_label="Revoked", check_onfleet=True, cluster_data=route)
            followups[p.key] = {"proposal": p, "task_ids": _task_ids(route), "at": time.time(),
                                "due": fn_due}
        st.session_state["_bot_decline_followups"] = followups
        st.session_state.pop("_dispatch_bot_plan", None)
        st.session_state["_revamp_notice"] = (
            "success", f"🤖 Moving {len(chosen)} declined route(s) back to dispatch…")
        st.rerun()
    ic_plans = [p for p in chosen if p.action == "ic"]
    fn_plans = [p for p in chosen if p.action == "fn"]
    for p in ic_plans:
        _apply_bot_ic_prefill(p)
    if ic_plans:
        st.session_state["_dispatch_bot_queue"] = [p.key for p in ic_plans]
        st.session_state["_dispatch_bot_info"] = {p.key: {"ic": p.ic_name, "rate": p.rate} for p in ic_plans}
        st.session_state["revamp_selected_route"] = ic_plans[0].key
    st.session_state.pop("_dispatch_bot_plan", None)
    prefix = f"🤖 {len(ic_plans)} Route Request(s) ready to send. " if ic_plans else "🤖 "
    if fn_plans:
        entries = [by_key[p.key] for p in fn_plans if p.key in by_key]
        if not _queue_fn_entries(entries, fn_due, db_engine, assign_tasks_to_fn_team,
                                 fetch_sent_records_from_sheet, notice_prefix=prefix):
            return
    else:
        st.session_state["_revamp_notice"] = ("success", prefix.strip())
    st.rerun()

def render_workspace(can_access_tab, process_pod, render_dispatch,
                     haversine, db_engine, assign_tasks_to_fn_team,
                     fetch_sent_records_from_sheet, default_due_days=14,
                     fn_ghost_to_cluster=None, saved_route_helpers=None,
                     merge_same_wo_ghosts=None, cluster_store=None,
                     mapbox_geocode=None, process_digital_pool=None, fetch_open_tasks=None,
                     pod_configs=None, dispatch_bot_helpers=None):
    """Render one selected route while retaining the existing dispatch actions."""
    move_to_dispatch = (saved_route_helpers or {}).get("move_to_dispatch")
    # Restore durable queued OnFleet handoffs after a Railway/Streamlit restart.
    # Scheduling is nonblocking and throttled across Streamlit sessions.
    if db_engine is not None:
        try:
            from revamp_fn_queue import recover_queued
            recover_queued(db_engine, assign_tasks_to_fn_team)
        except Exception as exc:
            print(f"[fn-handoff/recovery] {type(exc).__name__}: {exc}", flush=True)
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

    /* FN inbox: distinguish hierarchy and override inherited large block gaps. */
    .st-key-revamp_route_scroll_fn {background:#f8fafc!important;padding:6px!important;}
    .st-key-revamp_route_scroll_fn [data-testid="stVerticalBlock"] {gap:4px!important;}
    .st-key-revamp_route_scroll_fn [data-testid="stElementContainer"] {margin:0!important;}
    .st-key-revamp_route_scroll_fn [data-testid="stHorizontalBlock"] {margin:0 0 2px 18px!important;gap:6px!important;}
    .st-key-revamp_route_scroll_fn [class*="st-key-revamp_fn_pod_toggle_"] button {
      min-height:32px!important;padding:5px 9px!important;margin:7px 0 2px!important;
      background:#e8edf3!important;border:1px solid #d6dee8!important;border-radius:6px!important;
      box-shadow:none!important;justify-content:flex-start!important;color:#23354d!important;
    }
    .st-key-revamp_route_scroll_fn [class*="st-key-revamp_fn_pod_toggle_"] button p {font-size:12px!important;font-weight:750!important;}
    .st-key-revamp_route_scroll_fn [class*="st-key-revamp_group_toggle_"] button {
      min-height:25px!important;padding:3px 8px!important;margin:0 0 0 12px!important;width:calc(100% - 12px)!important;
      background:transparent!important;border:0!important;border-bottom:1px solid #e6eaf0!important;border-radius:0!important;
      box-shadow:none!important;justify-content:flex-start!important;color:#516079!important;
    }
    .st-key-revamp_route_scroll_fn [class*="st-key-revamp_group_toggle_"] button p {font-size:11px!important;font-weight:650!important;}
    .fn-workflow-heading {font-size:10px;font-weight:750;letter-spacing:.055em;text-transform:uppercase;margin:6px 0 0 12px;padding:3px 0;color:#64748b;}
    .fn-workflow-heading::before {content:'';display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px;vertical-align:1px;background:#94a3b8;}
    .fn-workflow-posted::before {background:#c69b37;}
    .fn-workflow-assigned::before {background:#4d9272;}
    .st-key-revamp_route_scroll_fn [class*="st-key-revamp_route_"] button {min-height:0!important;margin:0!important;padding:7px 9px!important;box-shadow:none!important;}
    .st-key-revamp_route_scroll_fn [class*="st-key-revamp_route_"] button:hover {transform:none!important;box-shadow:none!important;background:#fff!important;}

    /* Meaningful route colors only */
    div[class*="st-key-revamp_route_Ready_"] button {border-left:4px solid var(--rv-green)!important}
    div[class*="st-key-revamp_route_Flagged_"] button {border-left:4px solid var(--rv-red)!important;background:#fffafa!important}
    div[class*="st-key-revamp_route_Field_Nation_"] button {border-left:4px solid var(--rv-amber)!important;background:#fffdf8!important}
    div[class*="st-key-revamp_route_Sent_"] button {border-left:4px solid var(--rv-blue)!important}
    div[class*="st-key-revamp_route_Accepted_"] button {border-left:4px solid var(--rv-green)!important;background:#fbfefc!important}
    div[class*="st-key-revamp_route_Finalized_"] button {border-left:4px solid var(--rv-green)!important;background:#fbfefc!important}
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
    /* Associate-only: the workflow cards are accessible buttons, not HTML. */
    div[class*="st-key-revamp_fn_stage_"] button,
    div[class*="st-key-revamp_fn_stage_"] button:hover {
      min-height:58px!important;width:100%;padding:8px 10px!important;
      justify-content:flex-start!important;text-align:left!important;
      background:#fff!important;color:#1d2939!important;
      border:1px solid var(--rv-border)!important;border-left:3px solid #d59b22!important;
      border-radius:9px!important;box-shadow:0 1px 2px rgba(16,24,40,.03)!important;
    }
    div[class*="st-key-revamp_fn_stage_"] button p {
      font-size:.76rem!important;line-height:1.4!important;font-weight:760!important;
      text-align:left!important;white-space:pre-line!important;color:#1d2939!important
    }
    div[class*="st-key-revamp_fn_stage_posted"] button,
    div[class*="st-key-revamp_fn_stage_posted"] button:hover {border-left-color:#3b82f6!important}
    div[class*="st-key-revamp_fn_stage_assigned"] button,
    div[class*="st-key-revamp_fn_stage_assigned"] button:hover {border-left-color:#16804a!important}
    div[class*="st-key-revamp_fn_stage_"] button[kind="primary"],
    div[class*="st-key-revamp_fn_stage_"] button[kind="primary"]:hover {
      background:#edf4ff!important;border-color:#abbfd9!important;
      box-shadow:0 0 0 1px #abbfd9!important
    }
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
        st.markdown('<div class="revamp-heading">' + ("Field Nation" if _workspace_statuses(st.session_state.get("_auth_user") or {}) == ("Field Nation",) else "Dispatch") + "</div>", unsafe_allow_html=True)
    with search_col:
        search = st.text_input(
            "Search routes", placeholder="Search client, contractor, venue, WO, task or stop",
            label_visibility="collapsed", key="revamp_search",
        ).strip().lower()

    auth_user = st.session_state.get("_auth_user") or {}
    fn_only = _workspace_statuses(auth_user) == ("Field Nation",)
    auth_email = str(auth_user.get("email", "") or "").strip().lower()
    accessible = [
        pod for pod in PODS
        if can_access_tab(pod)
        or (pod == "Digital" and auth_email == "kheiden@terraboost.biz")
    ]
    if not accessible:
        st.info("Your account has no pod access. Contact an administrator to update your access.")
        return
    # Start with one pod so the first sync cannot queue five full Mapbox
    # routing passes before any routes appear. Multi-pod remains explicit.
    pod_options = accessible + (["All my pods"] if len(accessible) > 1 else [])
    remembered_pod = st.query_params.get("pod")
    if st.session_state.get("revamp_pod") not in pod_options:
        st.session_state["revamp_pod"] = remembered_pod if remembered_pod in pod_options else ("All my pods" if auth_user.get("scope") == "field_nation" else accessible[0])
    filter_col, _, refresh_col = st.columns([1.4, 4.5, 1.5], vertical_alignment="bottom")
    with filter_col:
        pod_choice = st.selectbox("Pod", pod_options, key="revamp_pod", on_change=_remember_pod)
    selected_pods = accessible if pod_choice == "All my pods" else [pod_choice]
    refresh_clicked = False  # The small Routes control checks in its own fragment.

    # Contractor intake is separate from task extraction. The roster is in
    # Postgres, while new OnFleet workers used to be created only by the
    # retired Monday import. Keep this control in the Revamp app itself.
    auth_user = st.session_state.get("_auth_user") or {}
    if db_engine is not None and str(auth_user.get("pod", "")).upper() in ("ADMIN", "MANAGER", "ALL"):
        with st.expander("New ICs in OnFleet"):
            st.caption("Check recent eligible DCC contractors against OnFleet by phone and email.")
            if st.button("Check new ICs", key="revamp_ic_check"):
                try:
                    from migration.onfleet_contractor_reconcile import preview_new_contractors
                    st.session_state["revamp_ic_preview"] = preview_new_contractors(db_engine)
                except Exception as exc:
                    st.error(f"Contractor check failed: {exc}")
            checked = st.session_state.get("revamp_ic_preview")
            if checked is not None:
                missing = [ic for ic in checked if ic["outcome"] == "missing"]
                st.write(f"{len(checked)} recent ICs checked; {len(missing)} eligible workers missing from OnFleet.")
                st.dataframe([
                    {"Name": ic["name"], "Pod": ic["pod_color"],
                     "Status": ic["ic_list"], "OnFleet": ic["outcome"],
                     "Added": str(ic["created_at"])[:16]}
                    for ic in checked
                ], hide_index=True, use_container_width=True)
                if missing:
                    choices = {f"{ic['name']} · {ic['pod_color']} · #{ic['id']}": ic["id"] for ic in missing}
                    selected = st.multiselect(
                        "Add these missing workers", list(choices),
                        default=list(choices), key="revamp_ic_selected",
                    )
                    if st.button("Add selected to OnFleet", key="revamp_ic_create",
                                 disabled=not selected):
                        from migration.onfleet_contractor_reconcile import create_missing_contractors
                        try:
                            with st.spinner("Matching and adding selected ICs..."):
                                results = create_missing_contractors(db_engine,
                                    selected_ids={choices[label] for label in selected})
                            st.dataframe(results, hide_index=True, use_container_width=True)
                            st.session_state.pop("revamp_ic_preview", None)
                        except Exception as exc:
                            st.error(f"Contractor sync stopped: {exc}")
    # A new signed-in session loads its selected pod automatically. Switching
    # pods loads only pods this session has not yet built; the refresh button
    # explicitly rebuilds the selected pod(s) against fresh OnFleet data.
    def _cluster_key(pod):
        return "global_digital_clusters" if pod == "Digital" else f"clusters_{pod}"

    def _route_pod_key(pod):
        return "Global_Digital" if pod == "Digital" else pod

    pending_pods = [] if fn_only else [pod for pod in selected_pods if
                    _cluster_key(pod) not in st.session_state and
                    not st.session_state.get(f"_revamp_load_attempted_{pod}")]
    if refresh_clicked or pending_pods:
        fetch_sent_records_from_sheet.clear()
        for index, pod in enumerate(selected_pods if refresh_clicked else pending_pods):
            st.session_state[f"_revamp_load_attempted_{pod}"] = True
            started = time.monotonic()
            print(f"[revamp/sync] waiting for {pod} load", flush=True)

            # Digital has its own authoritative pool builder and session-state
            # store. Treat it like every other Revamp pod in the UI, but route
            # its load through the existing Digital pipeline instead of
            # process_pod(), which only supports the five geographic pods.
            if pod == "Digital":
                if process_digital_pool is None:
                    st.error("Digital route loading is unavailable.")
                    continue
                try:
                    with st.spinner("Loading Digital routes..."):
                        process_digital_pool()
                except Exception as exc:
                    print(f"[revamp/sync] Digital load failed: {type(exc).__name__}: {exc}", flush=True)
                    st.session_state[f"_revamp_load_attempted_{pod}"] = False
                    st.error("Digital task extraction failed. Click the Routes refresh icon to retry.")
                    continue
            else:
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
                                if time.monotonic() - started > 40:
                                    st.session_state[f"_revamp_load_attempted_{pod}"] = False
                                    st.info("Route building is still running in the background. The page is no longer blocked; click the Routes refresh icon after a moment to display the results.")
                                    return
                    except Exception as exc:
                        print(f"[revamp/sync] background build failed for {pod}: {type(exc).__name__}: {exc}", flush=True)
                        ready = False
                    finally:
                        indicator.empty()
                    if not ready:
                        st.error("OnFleet task extraction failed. Click the Routes refresh icon to retry.")
                        continue
                with st.spinner(f"Finishing {pod} routes..."):
                    with _pod_load_locks()[pod]:
                        print(f"[revamp/sync] starting {pod}", flush=True)
                        if cluster_store is None and refresh_clicked and index == 0:
                            process_pod(pod, refresh_tasks=True)
                        else:
                            process_pod(pod)

            loaded_count = len(st.session_state.get(_cluster_key(pod), []))
            print(f"[revamp/sync] finished {pod}: {loaded_count} routes in {time.monotonic() - started:.1f}s", flush=True)
        st.session_state["_last_sync_ts"] = datetime.now()

    # The original pod tabs populate these session keys during their own
    # render. This workspace runs before those tabs, so hydrate the same
    # Postgres-backed state here for routed and accepted cards.
    # On user interaction, invalidate saved-status data when a portal event
    # arrives. This reads one indexed scalar; it does not rebuild task pools
    # or introduce automatic page refreshes.
    if db_engine is not None:
        import sqlalchemy as sa
        try:
            with db_engine.connect() as conn:
                version = conn.execute(sa.text("SELECT COALESCE(MAX(id), 0) FROM route_events")).scalar()
            if st.session_state.get("_revamp_saved_event_version") != version:
                fetch_sent_records_from_sheet.clear()
                st.session_state["_revamp_saved_event_version"] = version
        except Exception as exc:
            print(f"[revamp/status] event check failed: {type(exc).__name__}", flush=True)
    sent_db, ghost_db, archived_wos, history_db = fetch_sent_records_from_sheet()
    st.session_state["sent_db"] = sent_db
    st.session_state["ghost_db"] = ghost_db
    st.session_state["archived_wos"] = archived_wos
    st.session_state["_history_db"] = history_db

    loaded = list(selected_pods) if fn_only else [pod for pod in selected_pods if _cluster_key(pod) in st.session_state]
    if not loaded:
        def retry_initial_load():
            for pod in selected_pods:
                st.session_state[f'_revamp_load_attempted_{pod}'] = False
        with st.container(key='revamp_routes_heading'):
            title_col, refresh_col, notice_col = st.columns([58, 20, 300], vertical_alignment="center")
            with title_col:
                st.markdown('<div class="revamp-panel-title">Routes</div>', unsafe_allow_html=True)
            with refresh_col:
                with st.container(key='revamp_quiet_control'):
                    st.button('↻', key='revamp_quiet_refresh',
                              on_click=retry_initial_load, use_container_width=True)
            with notice_col:
                st.caption('Routes could not be loaded. Click the circle to retry.')
        _routes_refresh_style()
        return
    missing = [pod for pod in selected_pods if pod not in loaded]
    if missing:
        st.caption("Could not load: " + ", ".join(missing) + ". Click the Routes refresh icon to retry.")

    label_sets = st.session_state.setdefault('_revamp_bundle_labels', {})
    for pod in selected_pods:
        route_pod = _route_pod_key(pod)
        label_sets[route_pod] = _load_bundle_labels(db_engine, route_pod)
    eligible_ics = [] if fn_only else _eligible_ics(st.session_state.get("ic_df"), mapbox_geocode)
    all_routes = []
    seen_saved_routes = set()
    ghosts_by_pod = {
        pod: (merge_same_wo_ghosts((ghost_db or {}).get(_route_pod_key(pod), []))
              if merge_same_wo_ghosts else (ghost_db or {}).get(_route_pod_key(pod), []))
        for pod in loaded
    }
    saved_by_hash = {
        (pod, str(ghost.get("hash") or "")): ghost
        for pod, ghosts in ghosts_by_pod.items() for ghost in ghosts
        if ghost.get("hash") and str(ghost.get("status") or "").lower() != "finalized"
    }
    for pod in loaded:
        route_pod = _route_pod_key(pod)
        for route in ([] if fn_only else st.session_state.get(_cluster_key(pod), [])):
            if pod != "Digital" and route.get("is_digital"):
                continue
            if pod == "Digital" and not route.get("is_digital"):
                continue
            nearest = _nearest_ic(route, eligible_ics, haversine)
            route_hash = _route_hash(route)
            display_route = dict(route)
            route_state = _route_status(route, sent_db, nearest[1] if nearest else None, route_pod)
            if route_state == "Field Nation":
                # FN membership is derived from individual task reservations.
                # A live task cluster can have a NEW hash / partial task set,
                # resulting in a fake FN card with "no linked work order".
                # The persisted FN ghosts below are authoritative and show the
                # whole saved work order even after an OnFleet reassignment.
                continue
            saved = saved_by_hash.get((pod, route_hash))
            if saved:
                display_route["_ghost_record"] = saved
                live_ids = {str(task.get("id") or "").strip() for task in route.get("data", [])}
                saved_ids = {str(task_id).strip() for task_id in saved.get("task_ids", [])}
                if saved_ids and saved_ids != live_ids and route_state in ("Sent", "Accepted", "Finalized"):
                    display_route["_is_ghost"] = True
                    display_route["data"] = []
            elif route_state in ("Sent", "Accepted", "Declined", "Finalized"):
                record = next((sent_db.get(str(task.get("id") or "").strip())
                               for task in route.get("data", [])
                               if str(task.get("id") or "").strip() in sent_db), None)
                if record:
                    display_route.update(contractor_name=record.get("name") or "Unknown",
                                         wo=record.get("wo") or "",
                                         comp=record.get("comp", 0),
                                         due=record.get("due") or "N/A",
                                         route_ts=record.get("time") or "",
                                         route_ts_iso=record.get("status_ts_iso") or "",
                                         db_status=record.get("status") or "")
            if route_state != "Routed":
                if route_state in ("Sent", "Accepted", "Declined", "Finalized"):
                    seen_saved_routes.add((route_pod, route_hash,
                                           str(_saved_route_fields(display_route)["wo"] or "")))
                all_routes.append((route_pod, display_route, route_state,
                                   route_hash, nearest))

    # Accepted routes often leave OnFleet's unassigned feed; include the
    # persisted ghost records so they remain visible in this workspace.
    for pod in loaded:
        route_pod = _route_pod_key(pod)
        for ghost in ghosts_by_pod[pod]:
            route_hash = str(ghost.get("hash") or "")
            saved_identity = (route_pod, route_hash, str(ghost.get("wo") or ""))
            if (not route_hash or saved_identity in seen_saved_routes or
                    st.session_state.get(f"reverted_{route_hash}", False)):
                continue
            seen_saved_routes.add(saved_identity)
            ghost_status = str(ghost.get("status", "")).lower()
            state = ("Finalized" if ghost_status == "finalized" else
                     "Accepted" if ghost_status == "accepted" else
                     "Field Nation" if ghost_status in ("field_nation", "posted") else
                     "Sent" if ghost_status == "sent" else
                     "Declined" if ghost_status == "declined" else "Routed")
            if state == "Field Nation" and not _has_linked_fn_work_order(ghost):
                # Never show a phantom FN card without a persistent work order.
                # Do not delete or unassign any OnFleet task here.
                continue
            rebuilt_fn_data = _fn_ghost_tasks(ghost) if state == "Field Nation" else []
            route = {
                "_is_ghost": True, "wo": ghost.get("wo", ""),
                "_ghost_record": ghost,
                "city": ghost.get("city", "Unknown"),
                "state": ghost.get("state", ""),
                "stops": ghost.get("stops", ghost.get("lCnt", 0)),
                "data": rebuilt_fn_data,
            }
            if state != "Routed":
                all_routes.append((route_pod, route, state, route_hash, None))
    # Multiple live clusters can contain the same task IDs. Their hashes (and
    # Streamlit widget keys) coincide; keep one route before counts, selection,
    # bulk actions, and the visible list are derived.
    all_routes = _dedupe_route_entries(all_routes)
    all_routes = [(pod, {**route, '_bundle_label': _bundle_label(route, _bundle_sets_for_pod(pod))}, state, route_hash, nearest)
                  for pod, route, state, route_hash, nearest in all_routes]
    if fn_only:
        all_routes = [entry for entry in all_routes if entry[2] == "Field Nation"]
        # FN assignments leave this queue for Accepted. Keep a read-only copy
        # of every assigned FN order from Postgres, across all associates.
        if db_engine is not None:
            try:
                from migration import data_access as fn_data
                all_routes.extend(_fn_assigned_history_entries(
                    fn_data.get_fn_assigned_orders(db_engine),
                    selected_pods, pod_configs))
            except Exception as exc:
                print(f"[revamp/fn-assigned-history] {type(exc).__name__}: {exc}", flush=True)
                st.warning("Field Nation Assigned history is temporarily unavailable.")
        else:
            st.warning("Field Nation Assigned history requires the Railway database.")
        all_routes = _dedupe_route_entries(all_routes)
    else:
        _render_workspace_summary(all_routes, pod_choice, selected_pods)

    for key in st.session_state.pop("_revamp_fn_clear_next", []):
        st.session_state[f"revamp_fn_{key}"] = False
    remembered_status = st.query_params.get("view")
    if remembered_status in ("Ready", "Flagged", "Over 50 mi", "Selected"):
        remembered_status = "Routes"
    if st.session_state.get("revamp_status") in ("Ready", "Flagged", "Over 50 mi", "Selected"):
        st.session_state["revamp_status"] = "Routes"
    allowed_statuses = _workspace_statuses(auth_user)
    # Dedicated FN Associate History: do not change the other workspaces.
    if fn_only and auth_user.get("scope") == "field_nation":
        allowed_statuses = ("Field Nation", "History")
    if st.session_state.get("revamp_status") not in allowed_statuses:
        st.session_state["revamp_status"] = remembered_status if remembered_status in allowed_statuses else allowed_statuses[0]
    if st.session_state.pop("_revamp_show_fn_next", False):
        st.session_state["revamp_status"] = "Field Nation"
        _remember_view()
    if st.session_state.pop("_revamp_show_routes_next", False) and not fn_only:
        st.session_state["revamp_status"] = "Routes"
        _remember_view()
    if st.session_state.pop("_revamp_show_cvs_next", False) and not fn_only:
        st.session_state["revamp_status"] = "CVS Removal"
        _remember_view()
    if st.session_state.pop("_revamp_show_accepted_next", False) and not fn_only:
        st.session_state["revamp_status"] = "Accepted"
        _remember_view()
    status = st.radio("Route status", allowed_statuses, horizontal=True,
                      label_visibility="collapsed", key="revamp_status", on_change=_remember_view)
    notice = st.session_state.pop("_revamp_notice", None)
    if notice:
        (st.warning if notice[0] == "warning" else st.success)(notice[1])
    if fn_only and auth_user.get("scope") == "field_nation" and status == "History":
        from revamp_fn_history import render_history
        render_history(db_engine, auth_user, selected_pods, pod_configs,
                       fetch_sent_records_from_sheet, search=search)
        return
    matching = [entry for entry in all_routes if _entry_matches(entry, status, search)]
    if status in ('Ready', 'Flagged') and not matching:
        print(f"[revamp/queue] pods={selected_pods} view={status} "
              f"loaded={sum(len(st.session_state.get(_cluster_key(p), [])) for p in loaded)} "
              f"states={ {s: sum(e[2] == s for e in all_routes) for s in STATUSES[1:]} } "
              f"search_active={bool(search)}", flush=True)
    fn_posted = (ghost_db or {}).get("_fn_posted", {}) or {}
    fn_providers = (ghost_db or {}).get("_fn_provider", {}) or {}
    if status in ("Sent", "Accepted", "Declined", "Finalized"):
        matching.sort(
            key=lambda entry: (
                _saved_status_sort_key(entry[1]),
                str(entry[1].get("city") or "").lower(),
            ),
            reverse=True,
        )
    else:
        matching.sort(key=lambda entry: (
            entry[0] if status == "Field Nation" else "",
            str(entry[1].get("state") or "").upper(),
            {"Pending": 0, "Posted": 1, "Assigned": 2}[_fn_entry_stage(entry, fn_posted, fn_providers)] if status == "Field Nation" else 0,
            str(entry[1].get("city") or "").lower(), entry[0]))
    # Saved-route selections belong to the left-hand route cards, as in Field Nation.
    reroute_status = status in ("Sent", "Accepted", "Declined")
    reroute_entries = [entry for entry in matching if entry[2] == status] if reroute_status else []
    reroute_keys = [f"_reroute_chosen_{_route_entry_key(*entry[:4])}" for entry in reroute_entries]
    if reroute_status:
        visible = set(reroute_keys)
        # Do not keep stale selections across changes to the status/search/pod view.
        for old_key in list(st.session_state):
            if old_key.startswith("_reroute_chosen_") and old_key not in visible:
                st.session_state.pop(old_key, None)
        with st.container(key="revamp_reroute_toolbar"):
            select_col, clear_col, total_col = st.columns([1, 1, 2])
            with select_col:
                if st.button("Select all", key=f"revamp_reroute_select_{status}",
                             disabled=not reroute_keys, use_container_width=True):
                    for key in reroute_keys:
                        st.session_state[key] = True
                    st.rerun()
            with clear_col:
                if st.button("Clear", key=f"revamp_reroute_clear_{status}",
                             disabled=not reroute_keys, use_container_width=True):
                    for key in reroute_keys:
                        st.session_state[key] = False
                    st.rerun()
            with total_col:
                st.caption(f"{len(reroute_keys)} {status.lower()} routes available for selection")
    selection_prefix = "revamp_fn_" if status == "Field Nation" else "revamp_bulk_"
    visible_keys = [f"{entry[0]}:{entry[3]}" for entry in matching
                    if (entry[2] == "Field Nation" and not entry[1].get("_fn_assigned_history")) or
                    (status != "Field Nation" and entry[2] in ("Ready", "Flagged"))]
    def select_visible():
        # Field Nation bulk selection is intentionally Pending-only. Posted
        # and Assigned routes must be selected individually if needed.
        keys_to_select = (st.session_state.get('_revamp_quiet_visible_keys', visible_keys)
                          if status != 'Field Nation' else visible_keys)
        if status == "Field Nation":
            keys_to_select = [
                f"{entry[0]}:{entry[3]}" for entry in matching
                if entry[2] == "Field Nation"
                and _fn_entry_stage(entry, fn_posted, fn_providers) == "Pending"
                and (entry[1].get("_ghost_record") or {}).get("fn_handoff_state")
                    not in ("queued", "running", "error")
            ]
        for key in keys_to_select:
            st.session_state[f"{selection_prefix}{key}"] = True
    def select_flagged():
        # Use the latest manual-check results, including collapsed state groups.
        current = st.session_state.get('_revamp_quiet_all_pending')
        candidates = current[1] if current and current[0] == tuple(selected_pods) else all_routes
        for entry in candidates:
            if entry[2] in ("Ready", "Flagged"):
                st.session_state[f"revamp_bulk_{entry[0]}:{entry[3]}"] = (
                    entry[2] == "Flagged" and _entry_matches(entry, status, search)
                )
    def clear_visible():
        keys = (st.session_state.get('_revamp_quiet_visible_keys', visible_keys)
                if status != 'Field Nation' else visible_keys)
        for key in keys:
            st.session_state[f"{selection_prefix}{key}"] = False
    if status != "Field Nation":
        with st.container(key="revamp_action_bar"):
            count_col, select_col, flagged_col, clear_col = st.columns([3.2, 1.35, 1.8, 1.15], vertical_alignment="center")
            with count_col:
                st.caption(f"{len(matching)} route{'s' if len(matching) != 1 else ''} shown")
            with select_col:
                st.button("Select all", key="revamp_select_visible",
                          on_click=select_visible, disabled=not visible_keys,
                          use_container_width=True)
            with flagged_col:
                st.button("Select all Flagged", key="revamp_select_flagged",
                          on_click=select_flagged,
                          disabled=not any(entry[2] == "Flagged" for entry in matching),
                          use_container_width=True)
            with clear_col:
                st.button("Clear", key="revamp_clear_selection", on_click=clear_visible,
                          disabled=not visible_keys, use_container_width=True)
        if not fn_only:
            _render_dispatch_bot(matching, all_routes, dispatch_bot_helpers, haversine,
                                 sent_db, db_engine, default_due_days,
                                 assign_tasks_to_fn_team, fetch_sent_records_from_sheet,
                                 status=status, move_to_dispatch=move_to_dispatch)
    if status == "Field Nation":
        from fn_utils import generate_combined_fn_upload
        from migration import data_access as fn_data
        # Counts always reflect every matching FN route, regardless of the
        # Associate's currently selected workflow stage.
        stage_counts = {stage: sum(_fn_entry_stage(e, fn_posted, fn_providers) == stage
                                   for e in matching) for stage in ("Pending", "Posted", "Assigned")}
        if fn_only:
            # The guest/Field Nation account uses these cards as real navigation
            # controls. Previously they were inert HTML and filtered nothing.
            stage_key = "revamp_fn_stage_filter"
            if st.session_state.get(stage_key) not in ("Pending", "Posted", "Assigned"):
                st.session_state[stage_key] = "Pending"
            active_stage = st.session_state[stage_key]
            stage_columns = st.columns(3, gap="small")
            for col, stage_name in zip(stage_columns, ("Pending", "Posted", "Assigned")):
                with col:
                    if st.button(
                        f"{stage_name.upper()}  \n**{stage_counts[stage_name]}**",
                        key=f"revamp_fn_stage_{stage_name.lower()}",
                        type="primary" if active_stage == stage_name else "secondary",
                        use_container_width=True,
                        help=f"Show {stage_name.lower()} Field Nation routes",
                    ):
                        st.session_state[stage_key] = stage_name
                        st.session_state.pop("revamp_selected_route", None)
                        st.rerun()
            # Filter the route inbox AND the associated detail pane; selections
            # outside the visible stage cannot be bulk-posted accidentally.
            matching = [entry for entry in matching
                        if _fn_entry_stage(entry, fn_posted, fn_providers) == active_stage]
            visible_keys = [f"{entry[0]}:{entry[3]}" for entry in matching
                            if not entry[1].get("_fn_assigned_history")]
        else:
            # Keep the existing full-queue dispatcher presentation unchanged.
            st.markdown(
                '<div class="fn-stage-row">'
                f'<div class="fn-stage-card pending"><div class="k">Pending</div><div class="v">{stage_counts["Pending"]}</div></div>'
                f'<div class="fn-stage-card posted"><div class="k">Posted</div><div class="v">{stage_counts["Posted"]}</div></div>'
                f'<div class="fn-stage-card assigned"><div class="k">Assigned</div><div class="v">{stage_counts["Assigned"]}</div></div>'
                '</div>',
                unsafe_allow_html=True,
            )
        fn_selected = [entry for entry in (matching if fn_only else all_routes)
                       if entry[2] == "Field Nation"
                       and not entry[1].get("_fn_assigned_history")
                       and st.session_state.get(f"revamp_fn_{entry[0]}:{entry[3]}")]
        pending = [entry for entry in fn_selected
                   if _fn_entry_stage(entry, fn_posted, fn_providers) == "Pending"
                   and (entry[1].get("_ghost_record") or {}).get("fn_handoff_state")
                       not in ("queued", "running", "error")]
        csv_routes = [_fn_csv_route(entry[1], entry[3], fn_ghost_to_cluster)
                      for entry in fn_selected
                      if (entry[1].get("_ghost_record") or {}).get("fn_handoff_state")
                         not in ("queued", "running", "error")]
        csv_routes = [route for route in csv_routes if route]
        csv_data = None
        stop_count = 0
        csv_included_hashes = []
        bulk_return_clicked = False
        if csv_routes:
            try:
                csv_data, stop_count, csv_included_hashes = generate_combined_fn_upload(csv_routes)
            except Exception as exc:
                st.error(f"Could not build Field Nation CSV: {exc}")
        # Match CSV membership back to exact saved WOs. Checking Download on
        # a batch must not turn unrelated selected/Posted cards green.
        csv_hashes = set(csv_included_hashes or [])
        csv_pending = [
            {"work_order": str((entry[1].get("_ghost_record") or {}).get("wo")
                               or entry[1].get("wo") or "").strip(),
             "cluster_hash": str(entry[3])}
            for entry in pending if str(entry[3]) in csv_hashes
        ]
        # Every selected Pending route must contribute to this CSV before
        # recording a successful batch download.
        csv_complete = (not pending or len(csv_pending) == len(pending))
        csv_bytes = csv_data.getvalue() if csv_data else b""
        csv_digest = hashlib.sha256(csv_bytes).hexdigest() if csv_bytes else ""
        csv_available = bool(csv_bytes and stop_count > 0 and csv_complete)
        pending_csv_checked = _fn_csv_pending_checked(pending)
        with st.container(key="revamp_fn_toolbar"):
            sel_col, select_col, csv_col, posted_col, return_col, link_col, clear_col = st.columns(
                [1.0, .85, 1.35, 1.15, 1.25, 1.0, .65], vertical_alignment="center"
            )
            with sel_col:
                st.caption(f"{len(fn_selected)} selected · {stop_count} stops")
            with select_col:
                _pending_selectable = (sum(
                    _fn_entry_stage(entry, fn_posted, fn_providers) == "Pending"
                    and (entry[1].get("_ghost_record") or {}).get("fn_handoff_state")
                        not in ("queued", "running", "error")
                    for entry in matching) if fn_only else stage_counts["Pending"])
                st.button("Select all pending", key="revamp_fn_select_all",
                          on_click=select_visible, disabled=not _pending_selectable,
                          use_container_width=True)
            with csv_col:
                st.download_button(
                    f"Download CSV ({len(csv_routes)})",
                    data=csv_bytes,
                    file_name=f"FN_Combined_{date.today():%Y%m%d}.csv",
                    mime="text/csv", disabled=not csv_available or (bool(pending) and db_engine is None),
                    use_container_width=True, key="revamp_fn_csv",
                    on_click=_fn_csv_download_callback if csv_pending else "rerun",
                    args=(db_engine, tuple(csv_pending), csv_digest,
                          str(st.session_state.get("_auth_user", {}).get("username")
                              or "Field Nation Dispatch"),
                          fetch_sent_records_from_sheet) if csv_pending else None,
                )
            with posted_col:
                posted_clicked = st.button(
                    f"Mark Posted ({len(pending)})",
                    disabled=not pending_csv_checked or db_engine is None,
                    use_container_width=True, key="revamp_fn_posted",
                    help=("Every selected Pending route must show ✅ CSV downloaded "
                          "before it can be marked Posted."),
                )
            bulk_return_clicked = False
            if not fn_only:
                with return_col:
                    bulk_return_clicked = st.button(
                        f"Return selected ({len(fn_selected)})",
                        key="revamp_fn_return_selected",
                        disabled=fn_only or not fn_selected or db_engine is None,
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
        if pending and not pending_csv_checked:
            not_checked = sum(not bool((entry[1].get("_ghost_record") or {}).get("fn_csv_downloaded"))
                              for entry in pending)
            st.caption(f"Download CSV to get the green ✅ checkmark for {not_checked} "
                       "selected Pending route(s) before Mark Posted.")

        if bulk_return_clicked:
            st.session_state["_revamp_fn_bulk_return_confirm"] = True

        if not fn_only and st.session_state.get("_revamp_fn_bulk_return_confirm"):
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
            # UI gate AND transactional server gate: a stale browser cannot
            # force an unexported or changed work order to Posted.
            if not _fn_csv_pending_checked(pending):
                st.error("Download the CSV and get a green checkmark on every selected "
                         "Pending route before Mark Posted.")
            else:
                failures = []
                successes = 0
                for pod, route, _, route_hash, _ in pending:
                    try:
                        work_order = str((route.get("_ghost_record") or {}).get("wo")
                                         or route.get("wo") or "").strip()
                        if not work_order:
                            raise ValueError("No saved Field Nation work order for this card.")
                        # Use the exact work-order identity, not a potentially
                        # reused cluster hash. The database independently
                        # requires this WO's verified downloaded CSV.
                        fn_data.mark_fn_posted(db_engine, work_order)
                        successes += 1
                        st.session_state.setdefault("_revamp_fn_clear_next", []).append(f"{pod}:{route_hash}")
                    except Exception as exc:
                        failures.append(f"{route.get('city', 'Route')}: {exc}")
                if successes:
                    fetch_sent_records_from_sheet.clear()
                    st.session_state.pop("_revamp_fn_checked_entries", None)
                    st.session_state.pop("_revamp_saved_event_version", None)
                if failures:
                    if successes:
                        st.session_state["_revamp_notice"] = (
                            "warning", f"Marked {successes} checked route(s) Posted; "
                            f"{len(failures)} could not be Posted: " + "; ".join(failures[:3]))
                        st.rerun()
                    else:
                        st.error("Could not mark Posted: " + "; ".join(failures[:3]))
                else:
                    st.rerun()
    chosen = [entry for entry in all_routes if entry[2] in ("Ready", "Flagged")
              and st.session_state.get(f"revamp_bulk_{entry[0]}:{entry[3]}", False)]
    fn_team_id = st.session_state.get("_fn_team_id")
    fn_worker_id = st.session_state.get("_fn_worker_id")
    assign_clicked = False
    fn_due = date.today() + timedelta(days=default_due_days)
    # Bulk Field Nation controls are contextual: keep them out of the workspace
    # until the dispatcher has actually selected one or more Ready/Flagged routes.
    if status != "Field Nation" and chosen:
        with st.container(key="revamp_bulk_fn_actions"):
            bulk_label_col, due_col, action_col = st.columns([2.2, 1.25, 1.55], vertical_alignment="bottom")
            with bulk_label_col:
                st.caption(f"{len(chosen)} route{'s' if len(chosen) != 1 else ''} selected")
            with due_col:
                fn_due = st.date_input(
                    "Due date",
                    value=date.today() + timedelta(days=default_due_days),
                    key="revamp_fn_due",
                )
            with action_col:
                assign_clicked = st.button(
                    f"Send {len(chosen)} to Field Nation",
                    key="revamp_assign_fn",
                    type="primary",
                    disabled=db_engine is None,
                    use_container_width=True,
                )
            if db_engine is None:
                st.caption("Field Nation assignment needs the Railway database connection.")
    if assign_clicked:
        if _queue_fn_entries(chosen, fn_due, db_engine, assign_tasks_to_fn_team,
                             fetch_sent_records_from_sheet):
            st.rerun()

    # Bulk re-route uses the same underlying handler as the individual detail
    # pane action; the confirmation is for the entire batch, not per route.
    if status in ("Sent", "Accepted", "Declined") and move_to_dispatch:
        selected_routes = [entry for entry in matching if entry[2] == status
                           and st.session_state.get(
                               f"_reroute_chosen_{_route_entry_key(*entry[:4])}", False)]
        with st.container(key="revamp_reroute_actions"):
            # A popover opens immediately on the same click; the old two-step
            # app rerun made the confirmation appear unresponsive on mobile.
            with st.popover(f"Re-route selected ({len(selected_routes)})",
                            disabled=not selected_routes, use_container_width=True):
                st.warning(f"Re-route {len(selected_routes)} selected {status.lower()} routes? "
                           "This removes them from their current contractors.")
                yes_col = st.container()
                with yes_col:
                    if st.button("Confirm re-route", key=f"revamp_reroute_yes_{status}",
                                 type="primary", use_container_width=True):
                        started, failed = [], []
                        for pod, route, state, route_hash, nearest in list(selected_routes):
                            key = f"_reroute_chosen_{_route_entry_key(pod, route, state, route_hash)}"
                            name = str(route.get("wo") or route.get("contractor_name") or route_hash)
                            ic = str(route.get("contractor_name") or "Unknown")
                            ghost = route.get("_ghost_record") or {}
                            try:
                                hashes = (ghost.get("_merged_hashes") if status == "Sent" else None) or [route_hash]
                                for h in hashes:
                                    kwargs = dict(cluster_hash=h, ic_name=ic, pod_name=pod,
                                                  cluster_data=ghost if ghost else route,
                                                  check_onfleet=True)
                                    if status == "Sent":
                                        kwargs["action_label"] = "Re-Routed"
                                    if status == "Accepted":
                                        kwargs["check_completed"] = True
                                    if ghost and status == "Accepted":
                                        kwargs["action_label"] = "Ghost Archived"
                                    move_to_dispatch(**kwargs)
                                started.append(name)
                                st.session_state[key] = False
                            except Exception as exc:
                                failed.append(f"{name}: {exc}")
                        st.session_state["_revamp_notice"] = (
                            "warning" if failed else "success",
                            f"Re-route started for {len(started)} route(s); "
                            f"{len(failed)} could not start. Background OnFleet completion "
                            "is not yet verified." + (
                                " Failures: " + "; ".join(failed[:5]) if failed else ""
                            ),
                        )
                        st.rerun()

    left, right = st.columns([1.75, 3.25], gap="large")
    with left:
        quiet_context = ((selected_pods, process_pod, process_digital_pool, cluster_store,
                          fetch_open_tasks, eligible_ics, haversine, search, fetch_sent_records_from_sheet)
                         if not fn_only and cluster_store is not None and fetch_open_tasks is not None else None)
        st.session_state.pop('_revamp_quiet_visible_keys', None)
        st.session_state.pop('_revamp_quiet_pending', None)
        st.session_state.pop('_revamp_quiet_all_pending', None)
        st.session_state['_revamp_quiet_list_order'] = [_route_entry_key(*entry[:4]) for entry in matching
                                                      if entry[2] in ('Ready', 'Flagged')]
        st.session_state.pop("_revamp_fn_checked_entries", None)
        fn_refresh_context = (fetch_sent_records_from_sheet, selected_pods, search,
                              merge_same_wo_ghosts, db_engine, pod_configs) if fn_only else None
        _render_route_list(matching, status, fn_posted, fn_providers, quiet_context, fn_refresh_context)

    with right:
        # Search should bring the first matching route into focus, rather than
        # leaving the detail pane on a previously selected unrelated route.
        _previous_search = st.session_state.get("_revamp_last_detail_search")
        if _previous_search != search:
            st.session_state["_revamp_last_detail_search"] = search
            if search and matching:
                st.session_state["revamp_selected_route"] = _route_entry_key(*matching[0][:4])
        selection = st.session_state.get("revamp_selected_route")
        current = next((entry for entry in matching if
                        _route_entry_key(*entry[:4]) == selection),
                       matching[0] if matching else None)
        if current is None:
            st.info("Select a route from the list.")
            return
        pod, route, state, route_hash, nearest = current
        title = f"{route.get('city', 'Route')}, {route.get('state', '')}"
        saved_fields = _saved_route_fields(route) if state in ("Sent", "Accepted", "Declined", "Finalized") else None
        if saved_fields:
            _display_name = _display_contractor_name(route, state)
            _status_time = _route_status_timestamp(route, state)
            st.markdown(f"### {html.escape(str(saved_fields['wo'] or title))}")
            _due_part = f" · Due {html.escape(str(saved_fields['due']))}" if state in ("Sent", "Accepted") else ""
            st.caption(
                f"{title} · {state} {_status_time} · {('Digital' if pod == 'Global_Digital' else pod)} pod · "
                f"{saved_fields['stops']} stops · {saved_fields['tasks']} tasks{_due_part}"
            )
            st.caption(f"Assigned to: {_display_name}")
        else:
            st.markdown(f"### {html.escape(title)}  ·  {html.escape(state)}")
            _ghost = route.get("_ghost_record") or {}
            _detail_tasks = (len(route.get("data", []))
                             or len(_ghost.get("task_ids") or [])
                             or int(_ghost.get("tasks") or _ghost.get("tCnt") or 0))
            st.caption(f"{('Digital' if pod == 'Global_Digital' else pod)} pod · {route.get('stops', 0)} stops · "
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
            stage = _fn_entry_stage((pod, route, state, route_hash, nearest), fn_posted, fn_providers)
            ghost = route.get("_ghost_record") or {}
            wo = str(ghost.get("wo") or route.get("wo") or "").strip()
            if not wo or wo.lower() in ("none", "unknown"):
                wo = "Field Nation route (no linked work order)"
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
            if route.get("_fn_assigned_history"):
                first_label, first_date = "Assigned", str(route.get("_fn_assigned_at") or "—")
                second_label, second_value = "Workflow", "Accepted"
            else:
                first_label, first_date = "Sent to FN", created_date
                second_label, second_value = "Posted", posted_date
            st.markdown(
                '<div class="fn-date-row">'
                f'<div class="fn-date"><div class="k">{first_label}</div><div class="v">{html.escape(first_date)}</div></div>'
                f'<div class="fn-date"><div class="k">{second_label}</div><div class="v">{html.escape(second_value)}</div></div>'
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
                st.caption("Stops · Venue IDs and Kiosk IDs")
                def show_fn_stops(stops, start_number=1):
                    rows = []
                    for idx, stop in enumerate(stops, start_number):
                        if not isinstance(stop, dict):
                            continue
                        addr = address_with_zip(stop.get("addr"), stop.get("zip"))
                        rows.append(_fn_stop_markup(stop, idx, addr))
                    if rows:
                        st.markdown("".join(rows), unsafe_allow_html=True)
                show_fn_stops(stop_data[:12])
                if len(stop_data) > 12:
                    with st.expander(f"Show {len(stop_data) - 12} more stops"):
                        show_fn_stops(stop_data[12:], 13)

            if not fn_only:
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
                            outcome = _return_fn_route_to_regular(
                                route, route_hash, pod, db_engine,
                                move_to_dispatch, fetch_sent_records_from_sheet,
                            )
                            if outcome == "stale_cleared":
                                st.session_state["_revamp_notice"] = (
                                    "warning",
                                    f"{title}: cleared a stale Field Nation label. "
                                    "No database records or OnFleet task assignments were changed.",
                                )
                            else:
                                st.session_state["_revamp_notice"] = (
                                    "success",
                                    f"{title} returned to regular dispatch.",
                                )
                            st.session_state["_revamp_show_routes_next"] = True
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not return route: {exc}")

            handoff_state = str(ghost.get("fn_handoff_state") or "")
            if handoff_state in ("queued", "running"):
                st.info("OnFleet sync is in progress. The Field Nation route is saved; "
                        "its OnFleet Route Plan is not confirmed yet.")
            elif handoff_state == "error":
                st.error("OnFleet handoff needs attention: " +
                         str(ghost.get("fn_handoff_error") or "Route Plan was not confirmed."))
                if not fn_only and db_engine is not None:
                    from revamp_fn_queue import retry_handoff
                    if st.button("Retry OnFleet handoff",
                                 key=f"revamp_fn_retry_{pod}_{route_hash}",
                                 use_container_width=True):
                        if retry_handoff(db_engine, wo, assign_tasks_to_fn_team):
                            st.success("Retry queued. Refresh to check the result.")
                        else:
                            st.info("This route was already queued or completed.")
            if route.get("_fn_assigned_history"):
                st.success("Assigned in Field Nation. This route is now in the Accepted workflow.")
                st.caption(f"Original FN work order: {route.get('_fn_original_wo') or '—'}")
                st.caption(f"Provider: {route.get('_fn_history_provider') or 'Not recorded'}"
                           f" · Assigned: {route.get('_fn_assigned_at') or 'Date unavailable'}")
            elif stage == "Pending":
                if handoff_state not in ("queued", "running", "error"):
                    st.info("Select this route on the left, download the FN CSV, post it in Field Nation, then click Mark Posted.")
            else:
                provider = st.text_input("Assigned Field Nation rep",
                                         value=str(fn_providers.get(route_hash) or ""),
                                         key=f"revamp_provider_{pod}_{route_hash}",
                                         placeholder="Type the rep's name")
                if st.button("Assign", key=f"revamp_mark_assigned_{pod}_{route_hash}",
                             type="primary",
                             disabled=db_engine is None or not provider.strip(),
                             use_container_width=True):
                    try:
                        saved = fn_data.mirror_set_fn_provider_by_cluster_hash(db_engine, route_hash, provider.strip())
                        if not saved.get("success"):
                            raise RuntimeError(saved.get("skipped") or saved.get("error") or "Order not found")
                        result = fn_data.mark_fn_assigned(db_engine, saved["work_order"])
                        if not result.get("success"):
                            raise RuntimeError(result.get("error") or
                                               "OnFleet rename was not confirmed; route remains Posted.")
                        fetch_sent_records_from_sheet.clear()
                        st.session_state.pop(f"route_state_{route_hash}", None)
                        st.session_state[f"revamp_fn_{pod}:{route_hash}"] = False
                        if fn_only:
                            st.session_state["revamp_fn_stage_filter"] = "Assigned"
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
        elif state in ("Sent", "Accepted", "Declined", "Finalized"):
            if not saved_route_helpers:
                st.error("Saved route details are unavailable. Refresh the page.")
            else:
                _render_saved_route_card(
                    route, state, route_hash, pod, route.get("_ghost_record"),
                    **saved_route_helpers,
                )
        else:
            st.caption(f"Route {route.get('wo') or route_hash}")
