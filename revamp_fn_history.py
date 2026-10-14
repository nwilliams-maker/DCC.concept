"""History and guarded recovery for the dedicated Field Nation Associate view.

No import-time database work and no OnFleet, Monday, email or FN API calls.
The shared field_nation_orders storage status stays 'posted' on recovery;
the absence of payload.fn_posted_ts is what makes the UI queue Pending.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa

CENTRAL = ZoneInfo("America/Chicago")
PODS = ("Blue", "Green", "Orange", "Purple", "Red", "Digital")


def associate_access(user):
    return str((user or {}).get("scope") or "").lower() == "field_nation"


def _payload(value):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("Invalid Field Nation record payload")
    return parsed


def _timestamp(value):
    if not value:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def central_time(value):
    parsed = _timestamp(value)
    return parsed.astimezone(CENTRAL).strftime("%Y-%m-%d %I:%M:%S %p %Z") if parsed else str(value or "")


def task_ids(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").replace("|", ",").split(",") if v.strip()]


def record_pod(row, payload, configs):
    raw = str(payload.get("pod") or payload.get("pod_color") or "").strip()
    if raw.lower() in ("global_digital", "digital"):
        return "Digital"
    for pod in PODS:
        if raw.lower() == pod.lower():
            return pod
    state = str(payload.get("state") or "").upper().strip()
    if not state:
        match = re.search(r"\b([A-Z]{2})-\d+$", str(row.get("work_order") or ""))
        state = match.group(1) if match else ""
    for pod, settings in (configs or {}).items():
        if state and state in settings.get("states", []):
            return pod
    return "Unmapped"


def classify(row, payload):
    if (str(row.get("status") or "").lower() == "assigned"
            or str(row.get("provider") or payload.get("fn_provider") or "").strip()
            or payload.get("fn_assigned_ts")):
        return "Assigned"
    return "Posted" if payload.get("fn_posted_ts") else "Pending"


def load_records(engine, selected_pods, configs):
    """Read all saved FN records, not the current OnFleet task feed."""
    with engine.connect() as conn:
        rows = conn.execute(sa.text("""
            SELECT work_order, status::text AS status, provider, payload,
                   created_at, updated_at
            FROM field_nation_orders
            WHERE status IN ('posted', 'assigned')
            ORDER BY updated_at DESC, work_order
        """)).mappings().all()
    result = []
    include_unmapped = set(PODS).issubset(set(selected_pods))
    for row in rows:
        p = _payload(row["payload"])
        pod = record_pod(row, p, configs)
        if pod not in selected_pods and not (pod == "Unmapped" and include_unmapped):
            continue
        status = classify(row, p)
        posted = str(p.get("fn_posted_ts") or "")
        events = list(p.get("fn_history") or [])
        result.append({
            "Work Order": row["work_order"], "Status": status, "Pod": pod,
            "Provider": str(row.get("provider") or p.get("fn_provider") or ""),
            "Created (Central)": central_time(row.get("created_at")),
            "Posted (Central)": central_time(posted),
            "Assigned (Central)": central_time(p.get("fn_assigned_ts")),
            "Updated (Central)": central_time(row.get("updated_at")),
            "Tasks": len(task_ids(p.get("taskIds") or p.get("task_ids"))),
            "_posted": posted, "_events": events,
            "_created": row.get("created_at"), "_assigned": p.get("fn_assigned_ts"),
        })
    return result


def restore_posted(engine, expected, user, selected_pods, configs):
    """Exact WOs + observed posting timestamps; row-lock and recheck on write.

    Reads and writes remain in one transaction. Concurrent assignments or
    re-posts are skipped. Other payload fields and task assignments are intact.
    """
    if not associate_access(user):
        raise PermissionError("Field Nation Associate access is required")
    if not isinstance(expected, dict) or not 1 <= len(expected) <= 100:
        raise ValueError("Select between 1 and 100 Posted work orders")
    actor = str(user.get("username") or user.get("name") or "Field Nation Associate")
    now = datetime.now(timezone.utc).isoformat()
    reverted, skipped = [], []
    with engine.begin() as conn:
        for wo in sorted(expected):
            row = conn.execute(sa.text("""
                SELECT work_order, status::text AS status, provider, payload
                FROM field_nation_orders WHERE work_order = :wo FOR UPDATE
            """), {"wo": wo}).mappings().first()
            if not row:
                skipped.append({"Work Order": wo, "Reason": "Record no longer exists"})
                continue
            p = _payload(row["payload"])
            pod = record_pod(row, p, configs)
            scope_ok = pod in selected_pods or (pod == "Unmapped" and set(PODS).issubset(set(selected_pods)))
            if not scope_ok:
                skipped.append({"Work Order": wo, "Reason": "Outside selected pods"})
                continue
            if str(row["status"]) != "posted" or classify(row, p) != "Posted":
                skipped.append({"Work Order": wo, "Reason": "No longer Posted or already has a provider"})
                continue
            old_posted = str(p.get("fn_posted_ts") or "")
            if not expected[wo] or old_posted != str(expected[wo]):
                skipped.append({"Work Order": wo, "Reason": "Posting timestamp changed; refresh and review"})
                continue
            events = list(p.get("fn_history") or [])
            events.append({"action": "restore_pending", "at": now, "actor": actor,
                           "from": "Posted", "to": "Pending", "posted_at": old_posted})
            p["fn_history"] = events
            p.pop("fn_posted_ts", None)
            conn.execute(sa.text("""
                UPDATE field_nation_orders
                SET payload = CAST(:payload AS jsonb), updated_at = now()
                WHERE work_order = :wo AND status = 'posted'
            """), {"wo": wo, "payload": json.dumps(p)})
            tids = sorted(task_ids(p.get("taskIds") or p.get("task_ids")))
            route_hash = str(p.get("cluster_hash") or (hashlib.md5("".join(tids).encode()).hexdigest() if tids else ""))
            reverted.append({"work_order": wo, "cluster_hash": route_hash})
        if reverted:
            conn.execute(sa.text("""
                INSERT INTO route_events (route_id, action, payload)
                VALUES (NULL, 'bulkRevertFnState', CAST(:payload AS jsonb))
            """), {"payload": json.dumps({"actor": actor, "to": "Pending", "at": now,
                                          "work_orders": [r["work_order"] for r in reverted]})})
    return {"reverted": reverted, "skipped": skipped}


def render_history(engine, user, selected_pods, configs, refresh_saved, *, search=""):
    import pandas as pd
    import streamlit as st

    if not associate_access(user):
        st.error("History is available in the Field Nation Associate workspace only.")
        return
    st.subheader("History")
    st.caption("Saved Field Nation records across Pending, Posted and Assigned. All times are Central.")
    if engine is None:
        st.warning("Postgres is unavailable. History and recovery are disabled.")
        return
    feedback = st.session_state.pop("_fn_history_feedback", None)
    if feedback:
        st.success(feedback)
    if st.session_state.pop("_fn_history_reset_widgets", False):
        st.session_state.pop("fn_history_selection", None)
        for key in list(st.session_state):
            if str(key).startswith("fn_history_confirm_"):
                st.session_state.pop(key, None)
    try:
        records = load_records(engine, selected_pods, configs)
    except Exception as exc:
        print(f"[fn-history/read] {type(exc).__name__}: {exc}", flush=True)
        st.error("History could not be loaded. No records were changed.")
        return
    stage = st.radio("History category", ("All", "Pending", "Posted", "Assigned"),
                     horizontal=True, key="fn_history_category")
    query = st.text_input("Search history", key="fn_history_query", placeholder="Work order, provider or pod")
    filters = [str(term).strip().lower() for term in (search, query) if str(term).strip()]
    rows = [r for r in records if (stage == "All" or r["Status"] == stage)
            and all(term in " ".join(str(v) for k, v in r.items() if not k.startswith("_")).lower() for term in filters)]
    st.caption(f"{len(rows)} of {len(records)} saved records in the selected pods. Newest activity first.")
    if rows:
        st.dataframe(pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]),
                     hide_index=True, use_container_width=True)
    else:
        st.info("No matching history records.")
    eligible = {r["Work Order"]: r for r in rows if r["Status"] == "Posted" and r["_posted"]}
    current = st.session_state.get("fn_history_selection", [])
    st.session_state["fn_history_selection"] = [wo for wo in current if wo in eligible]
    selected = st.multiselect("Select Posted work orders to return to Pending", list(eligible),
                              key="fn_history_selection",
                              format_func=lambda wo: f"{wo} | Posted {eligible[wo]['Posted (Central)']}")
    st.caption("Assigned records are read-only. Restoring does not delete work orders, unassign OnFleet tasks, or change Field Nation itself.")
    snapshot = {wo: eligible[wo]["_posted"] for wo in selected}
    signature = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]
    confirmed = st.checkbox(f"Confirm returning {len(selected)} selected work order(s) to Pending",
                            key=f"fn_history_confirm_{signature}", disabled=not selected)
    if st.button(f"Return to Pending ({len(selected)})", type="primary", key="fn_history_restore",
                 disabled=not (selected and confirmed)):
        try:
            outcome = restore_posted(engine, snapshot, user, selected_pods, configs)
        except Exception as exc:
            print(f"[fn-history/restore] {type(exc).__name__}: {exc}", flush=True)
            st.error("Recovery failed. The transaction was rolled back; no partial recovery was committed.")
            return
        for record in outcome["reverted"]:
            route_hash = record["cluster_hash"]
            for cache in ("_fn_posted", "_fn_exported"):
                st.session_state.get(cache, {}).pop(route_hash, None)
        refresh_saved.clear()
        st.session_state.pop("_revamp_saved_event_version", None)
        st.session_state["_fn_history_reset_widgets"] = True
        st.session_state["_fn_history_feedback"] = (
            f"Returned {len(outcome['reverted'])} work order(s) to Pending. "
            f"Skipped {len(outcome['skipped'])} that changed or are protected.")
        st.session_state["_fn_history_skipped"] = outcome["skipped"]
        st.rerun()
    skipped = st.session_state.pop("_fn_history_skipped", [])
    if skipped:
        st.dataframe(pd.DataFrame(skipped), hide_index=True, use_container_width=True)
    with st.expander("Recorded status changes"):
        events = []
        for row in rows:
            base = {"Work Order": row["Work Order"], "Pod": row["Pod"]}
            for category, at in (("Pending", row["_created"]), ("Posted", row["_posted"]), ("Assigned", row["_assigned"])):
                if at:
                    events.append({**base, "Status": category, "Time (Central)": central_time(at), "Actor": "", "_at": str(at)})
            for event in row["_events"]:
                if not isinstance(event, dict):
                    continue
                if event.get("posted_at"):
                    events.append({**base, "Status": "Posted", "Time (Central)": central_time(event["posted_at"]), "Actor": "", "_at": event["posted_at"]})
                events.append({**base, "Status": event.get("to", "Pending"), "Time (Central)": central_time(event.get("at")), "Actor": event.get("actor", ""), "_at": event.get("at", "")})
        events.sort(key=lambda e: _timestamp(e["_at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        if events:
            st.dataframe(pd.DataFrame([{k: v for k, v in e.items() if k != "_at"} for e in events]).drop_duplicates(),
                         hide_index=True, use_container_width=True)
        st.caption("This uses recorded timestamps. Older changes without an audit record cannot be reconstructed.")
