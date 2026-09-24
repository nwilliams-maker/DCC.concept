"""OnFleet + Monday.com side effects for the three GAS actions that
`data_access.py` deliberately did NOT replicate (see that module's
docstring and migration/README.md's "Step 4/5, in practice" /
"Decision (2026-09-18)" sections):

  - processDecision : OnFleet auto-assign + ordered route creation on accept.
  - markFNAssigned   : OnFleet routePlan rename + per-task metadata/worker
                        re-PUT, plus a Monday.com Route Planning board sync
                        (address-matched), when a dispatcher confirms a
                        Field Nation work order is assigned to a real
                        installer.
  - saveToFieldNation: a Monday.com placeholder push (installer="Field
                        Nation") the moment a route is posted to FN, before
                        a real provider has accepted.

This is a line-by-line, function-by-function port of the live Apps Script
(`Code.gs` in the "DCC" Apps Script project, script.google.com/d/
1iyG4dr7iyoAD1sDkyySCh78baDOFJWBHt-_j9sS82oASl-dWInb50wOI, pulled 2026-09-19
-- the actual GAS source was NOT available in this repo before then, which is
why migration/README.md's Step 7 could only say "port them ... using the
actual GAS source as reference" rather than actually doing it). Every
retry/backoff constant, allow-listed team name, Monday column default, and
the "board-corruption guard" (MONDAY_GROUP_FILTER) are carried over exactly
so behavior doesn't silently drift from what's live today.

Config -- set these as Railway env vars on the app service (GAS used Script
Properties of the same names, minus the ONFLEET_KEY substitution noted below):
  ONFLEET_KEY                     -- already set; reused (GAS used a separate
                                      ONFLEET_API_KEY Script Property, but the
                                      Python app has used ONFLEET_KEY for its
                                      existing OnFleet calls since before this
                                      migration -- same Onfleet account/key,
                                      just one env var instead of two).
  MONDAY_API_TOKEN                -- required for any Monday.com sync. If
                                      unset, Monday syncing is skipped (logged,
                                      not raised) exactly like GAS did.
  MONDAY_BOARD_ID                 -- default '7374880245'
  MONDAY_INSTALLER_COL            -- default 'text1__1'
  MONDAY_WO_COL                   -- default 'text14'
  MONDAY_ADDR_COL                 -- default 'text63'
  MONDAY_GROUP_FILTER             -- comma-separated Monday group IDs to
                                      restrict writes to. '*' disables the
                                      filter but ONLY takes effect together
                                      with MONDAY_GROUP_FILTER_ALLOW_WILDCARD
                                      (see _monday_group_filter below) -- this
                                      is the "board-corruption guard" Nick's
                                      GAS comments call out repeatedly; do not
                                      relax it without reading those comments.
  MONDAY_GROUP_FILTER_ALLOW_WILDCARD -- must be exactly 'yes' to let
                                      MONDAY_GROUP_FILTER='*' take effect.

NOT ported here, and out of scope for this module: the sheet-row bookkeeping
(moving a row between tabs) -- that's a Postgres status/table change instead,
handled in data_access.py's callers. See this repo's migration/README.md
"Field Nation acceptance -> Postgres" note for the one behavioral gap found
while porting this (markFNAssigned relocates the Sheet row into "Accepted
routes", which data_access.mark_fn_assigned's original stub did not
replicate -- fixed alongside this module, see that function).

None of the functions in this module talk to Postgres -- they are pure
side-effect functions (Onfleet/Monday HTTP calls in, small result dicts out)
so they can be unit-tested by mocking `requests` (see
migration/tests/test_fn_side_effects.py) without a live API key or a
database. data_access.py wires them into the actual write path.
"""
from __future__ import annotations

import base64
import json as _json
import math
import os
import re
import time
from typing import Any

import requests

ONFLEET_BASE = "https://onfleet.com/api/v2"
MONDAY_URL = "https://api.monday.com/v2"

# Same allow-list as GAS's createOnfleetRoute -- a worker not on one of these
# Onfleet teams can't have a route created for them.
_ALLOWED_ONFLEET_TEAM_NAMES = [
    "POD: Blue", "POD: Green", "POD: Orange", "POD: Purple", "POD: Red",
    "ZZZ - Field Agents", "ZZZ IC: David Region",
]

# Default Monday group allow-list (Field Nation / Escalations / Primary
# Route) -- see MONDAY_GROUP_FILTER above. Ported verbatim from GAS.
_MONDAY_DEFAULT_GROUPS = ["group_mkzk8sha", "group_mkq9dp76", "1731336780_all_unposted_kiosks__1"]


# ---------------------------------------------------------------------------
# Onfleet: auth + retrying fetch (replaces onfleetFetchWithBackoff)
# ---------------------------------------------------------------------------

def _onfleet_auth_header() -> dict[str, str]:
    key = os.environ.get("ONFLEET_KEY") or ""
    b64 = base64.b64encode(f"{key}:".encode()).decode()
    return {"Authorization": f"Basic {b64}"}


def onfleet_fetch_with_backoff(method: str, url: str, *, json_body: Any = None, max_retries: int = 5) -> requests.Response:
    """Port of onfleetFetchWithBackoff. Retries on 429, the documented
    "bandwidth quota exceeded" 429 body, a 401 whose body mentions
    rate/quota/limit/unauthorized (OnFleet's soft-throttle 401, per the Jul 2
    2026 GAS comment), and 502/503/504. Exponential backoff 250ms -> 4000ms
    cap, same as GAS. Network-level exceptions get the same backoff treatment
    and re-raise on the final attempt."""
    headers = _onfleet_auth_header()
    delay = 0.25
    resp: requests.Response | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=20)
        except requests.RequestException:
            if attempt == max_retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
            continue

        code = resp.status_code
        body_lower = (resp.text or "").lower()
        is_quota = (
            code == 429
            or "bandwidth quota exceeded" in body_lower
            or (code == 401 and any(tok in body_lower for tok in ("rate", "quota", "limit", "unauthorized")))
            or code in (502, 503, 504)
        )
        if not is_quota:
            return resp
        if attempt == max_retries:
            return resp
        time.sleep(delay)
        delay = min(delay * 2, 4.0)
    return resp  # pragma: no cover -- loop always returns/raises above


# ---------------------------------------------------------------------------
# Onfleet: create route + assign tasks (replaces createOnfleetRoute /
# assignTasksToWorker -- called by processDecision on accept)
# ---------------------------------------------------------------------------

def create_onfleet_route(wo_name: str, ordered_task_ids: list[str], worker_id: str) -> dict[str, Any]:
    """Port of createOnfleetRoute. Restricts to the same allow-listed Onfleet
    teams so a route is never created for a worker outside the dispatch
    pods/field-agent teams."""
    headers = _onfleet_auth_header()
    try:
        worker_resp = requests.get(f"{ONFLEET_BASE}/workers/{worker_id}", headers=headers, timeout=15)
        team_id = None
        if worker_resp.status_code == 200:
            worker_data = worker_resp.json()
            worker_teams = worker_data.get("teams") or []
            if worker_teams:
                teams_resp = requests.get(f"{ONFLEET_BASE}/teams", headers=headers, timeout=15)
                if teams_resp.status_code == 200:
                    all_teams = teams_resp.json()
                    allowed_ids = {t["id"] for t in all_teams if t.get("name") in _ALLOWED_ONFLEET_TEAM_NAMES}
                    for tid in worker_teams:
                        if tid in allowed_ids:
                            team_id = tid
                            break
        if not team_id:
            return {
                "success": False,
                "error": (
                    "Worker is not on an allowed Onfleet team (POD: Blue/Green/Orange/Purple/Red, "
                    "ZZZ - Field Agents, or ZZZ IC: David Region)"
                ),
            }

        start_time = int((time.time() + 2 * 86400) * 1000)  # +2 days, matches GAS's setHours(16,0,0,0) intent closely enough for route ordering
        route_payload = {
            "name": wo_name,
            "color": "#90EE90",
            "startTime": start_time,
            "timezone": "America/Chicago",
            "worker": worker_id,
            "team": team_id,
            "tasks": ordered_task_ids,
        }
        resp = requests.post(f"{ONFLEET_BASE}/routePlans", headers=headers, json=route_payload, timeout=20)
        if resp.status_code in (200, 201):
            created = resp.json()
            return {"success": True, "routeId": created.get("id")}
        return {"success": False, "error": f"Onfleet route failed ({resp.status_code}): {resp.text}"}
    except Exception as exc:  # noqa: BLE001 -- mirrors GAS's catch-all + Logger.log
        return {"success": False, "error": f"Route creation exception: {exc}"}


# In-process phone -> workerId cache (replaces GAS's CacheService, 30 min TTL
# like the original `workers_phone_map_v1` cache entry). Deliberately a plain
# module-level dict with a manual TTL rather than st.cache_data -- this
# module has no Streamlit dependency so it can be unit-tested standalone.
_phone_map_cache: dict[str, Any] = {"map": None, "at": 0.0}
_PHONE_MAP_TTL_S = 1800


def _load_onfleet_phone_map(force: bool = False) -> dict[str, str]:
    now = time.time()
    if not force and _phone_map_cache["map"] is not None and (now - _phone_map_cache["at"]) < _PHONE_MAP_TTL_S:
        return _phone_map_cache["map"]

    headers = _onfleet_auth_header()
    phone_map: dict[str, str] = {}
    last_id = None
    seen_ids: set[str] = set()
    for _page in range(10):
        url = f"{ONFLEET_BASE}/workers" + (f"?lastId={last_id}" if last_id else "")
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            break
        try:
            parsed = resp.json()
        except ValueError:
            break
        page_workers = parsed if isinstance(parsed, list) else (parsed.get("workers") or [])
        if not page_workers:
            break
        new_count = 0
        for w in page_workers:
            wid = w.get("id")
            if wid in seen_ids:
                continue
            seen_ids.add(wid)
            digits = re.sub(r"\D", "", str(w.get("phone") or "").split(".")[0])
            if len(digits) >= 10:
                phone_map[digits[-10:]] = wid
            new_count += 1
        if new_count == 0:
            break
        last_id = page_workers[-1].get("id")

    _phone_map_cache["map"] = phone_map
    _phone_map_cache["at"] = now
    return phone_map


def assign_tasks_to_worker(
    worker_phone: str,
    task_ids_str: str,
    wo_name: str,
    total_comp: float,
    due_date: str,
    digital_task_ids_str: str = "",
) -> dict[str, Any]:
    """Port of assignTasksToWorker. Two PUTs per task (metadata, then
    worker-assignment) -- deliberately NOT collapsed into one PUT, because
    OnFleet's task.assigned webhook (which feeds Make.com -> Monday's Route
    Planning board) doesn't reliably include metadata set in the SAME PUT
    that also assigns the worker. Digital tasks skip the completeAfter bump
    so the IC can complete them immediately."""
    safe_phone = str(worker_phone or "").split(".")[0]
    safe_task_ids = str(task_ids_str or "")
    if not safe_phone or not safe_task_ids:
        return {"success": False, "error": "Missing phone or task IDs."}

    digital_task_set = {t.strip() for t in str(digital_task_ids_str or "").split(",") if t.strip()}
    headers = _onfleet_auth_header()

    raw_digits = re.sub(r"\D", "", safe_phone)
    if not raw_digits or len(raw_digits) < 10:
        return {"success": False, "error": f"Invalid phone format: '{safe_phone}'"}
    core10 = raw_digits[-10:]

    phone_map = _load_onfleet_phone_map()
    worker_id = phone_map.get(core10)
    if not worker_id:
        # Cache miss could mean a stale map (worker added since last refresh) -- refresh once.
        phone_map = _load_onfleet_phone_map(force=True)
        worker_id = phone_map.get(core10)
    if not worker_id:
        return {"success": False, "error": f"Contractor phone number not found in Onfleet. (Searched for: {core10})", "workerId": None}

    task_array = [t.strip() for t in safe_task_ids.split(",") if t.strip()]
    # Ported literally from GAS: Math.ceil(((totalComp||0)*10)/taskArray.length)/10
    per_task_rate = (math.ceil(((total_comp or 0) * 10) / len(task_array)) / 10.0) if task_array else 0.0

    assigned_count = 0
    assignment_errors: list[str] = []
    complete_after = int((time.time() + 2 * 86400) * 1000)

    for i, tid in enumerate(task_array):
        is_digital = tid in digital_task_set
        meta = [
            {"name": "WO_NAME", "value": wo_name or "", "type": "string", "visibility": ["api"]},
            {"name": "PAY_PER_TASK", "value": per_task_rate, "type": "number", "visibility": ["api"]},
            {"name": "DUE_DATE", "value": str(due_date or ""), "type": "string", "visibility": ["api"]},
        ]
        # PUT 1: metadata only, committed before the assignment webhook fires.
        try:
            onfleet_fetch_with_backoff("put", f"{ONFLEET_BASE}/tasks/{tid}", json_body={"metadata": meta})
        except Exception:  # noqa: BLE001 -- non-fatal, matches GAS's try/catch + Logger.log
            pass
        time.sleep(0.04)
        # PUT 2: worker assignment (re-sends metadata + completeAfter for non-digital).
        assign_body: dict[str, Any] = {"worker": worker_id, "metadata": meta}
        if not is_digital:
            assign_body["completeAfter"] = complete_after
        assign_resp = onfleet_fetch_with_backoff("put", f"{ONFLEET_BASE}/tasks/{tid}", json_body=assign_body)
        if assign_resp.status_code == 200:
            assigned_count += 1
        else:
            assignment_errors.append(f"Task {tid} failed: {assign_resp.text}")
        if i < len(task_array) - 1:
            time.sleep(0.07)

    if assigned_count == 0:
        return {"success": False, "error": f"Onfleet API rejected assignment. Details: {' | '.join(assignment_errors)}", "workerId": None}

    is_partial = assigned_count < len(task_array)
    return {
        "success": True,
        "partial": is_partial,
        "assignedCount": assigned_count,
        "totalCount": len(task_array),
        "assignmentErrors": assignment_errors if is_partial else [],
        "msg": f"Assigned {assigned_count} out of {len(task_array)} tasks to worker.",
        "workerId": worker_id,
    }


def apply_onfleet_decision(
    *,
    decision: str,
    task_ids: str,
    wo: str,
    phone: str,
    comp: float,
    due: str,
    digital_task_ids: str = "",
    stop_order: str | None = None,
) -> dict[str, Any]:
    """Port of the OnFleet half of the `processDecision` GAS action (the
    sheet move + email are handled by the caller -- see data_access.py's
    process_decision). Only runs the assign+route-create when decision is an
    accept with task IDs, exactly like GAS's `if (params.decision ===
    "Accepted" && params.taskIds ...)` guard."""
    if decision != "accept" or not (task_ids or "").strip():
        return {"onfleetSuccess": True, "onfleetMsg": "", "routeSuccess": False, "routeMsg": "", "partial": False}

    assign_result = assign_tasks_to_worker(phone, task_ids, wo, comp, due, digital_task_ids)
    onfleet_success = assign_result.get("success", False)
    onfleet_msg = (
        (assign_result.get("msg") or "Assigned (no detail).")
        if onfleet_success
        else (assign_result.get("error") or "Onfleet assignment failed (no error detail returned).")
    )

    route_success = False
    route_msg = ""
    if onfleet_success and assign_result.get("workerId"):
        ordered_ids = [
            t.strip() for t in (stop_order or task_ids).split(",") if t.strip()
        ]
        route_result = create_onfleet_route(wo or "Route", ordered_ids, assign_result["workerId"])
        route_success = route_result.get("success", False)
        route_msg = f"Route created: {route_result.get('routeId')}" if route_success else f"Route creation failed: {route_result.get('error')}"

    partial = bool(assign_result.get("partial"))
    route_incomplete = (not onfleet_success) or partial or (not route_success)
    return {
        "onfleetSuccess": onfleet_success,
        "onfleetMsg": onfleet_msg,
        "routeSuccess": route_success,
        "routeMsg": route_msg,
        "partial": partial,
        "route_incomplete": route_incomplete,
    }


# ---------------------------------------------------------------------------
# Shared street-address normalization (used by both the markFNAssigned and
# saveToFieldNation Monday syncs to match a route stop to a Monday item)
# ---------------------------------------------------------------------------

_STREET_ABBREV = [
    (r"\bavenue\b", "ave"), (r"\bstreet\b", "st"), (r"\bboulevard\b", "blvd"),
    (r"\broad\b", "rd"), (r"\bdrive\b", "dr"), (r"\blane\b", "ln"),
    (r"\bcourt\b", "ct"), (r"\bplace\b", "pl"), (r"\bhighway\b", "hwy"),
    (r"\bnorth\b", "n"), (r"\bsouth\b", "s"), (r"\beast\b", "e"), (r"\bwest\b", "w"),
]


def _normalize_street(s: str) -> str:
    out = str(s or "").lower()
    for pattern, repl in _STREET_ABBREV:
        out = re.sub(pattern, repl, out)
    return re.sub(r"[^a-z0-9]", "", out)


def _parse_stops_from_locs(locs: str) -> list[dict[str, str]]:
    """locs = "home | stop1 | stop2 | ... | home" -- first/last are always
    the contractor's home address, dropped so only venue stops are matched."""
    raw_all = str(locs or "").split("|")
    raw = raw_all[1:-1] if len(raw_all) >= 3 else []
    stops = []
    for entry in raw:
        stop = entry.strip()
        if not stop:
            continue
        head = stop.split(",")[0].strip()
        m = re.match(r"^(\d{2,})\s+(.+)$", head)
        if not m:
            continue
        stops.append({"num": m.group(1), "streetNorm": _normalize_street(m.group(2)), "full": head})
    return stops


# ---------------------------------------------------------------------------
# Monday.com config + the "board-corruption guard"
# ---------------------------------------------------------------------------

def _monday_config() -> dict[str, str]:
    return {
        "token": (os.environ.get("MONDAY_API_TOKEN") or "").strip(),
        "board": (os.environ.get("MONDAY_BOARD_ID") or "7374880245").strip(),
        "installer_col": (os.environ.get("MONDAY_INSTALLER_COL") or "text1__1").strip(),
        "wo_col": (os.environ.get("MONDAY_WO_COL") or "text14").strip(),
        "addr_col": (os.environ.get("MONDAY_ADDR_COL") or "text63").strip(),
    }


def _monday_group_filter() -> list[str] | None:
    """None means "no filter -- touch any group" (only reachable via the
    double-confirmed wildcard). Ported verbatim from GAS's PATCH M17 guard:
    MONDAY_GROUP_FILTER='*' alone is NOT enough, it also needs
    MONDAY_GROUP_FILTER_ALLOW_WILDCARD='yes', otherwise it silently falls
    back to the 3-group default. Do not simplify this -- it exists because a
    prior run without it corrupted unrelated Monday groups."""
    raw = (os.environ.get("MONDAY_GROUP_FILTER") or "").strip()
    if raw == "*":
        if (os.environ.get("MONDAY_GROUP_FILTER_ALLOW_WILDCARD") or "").strip().lower() == "yes":
            return None
        return list(_MONDAY_DEFAULT_GROUPS)
    if raw:
        return [g.strip() for g in raw.split(",") if g.strip()]
    return list(_MONDAY_DEFAULT_GROUPS)


def _monday_mutation_ok(resp: requests.Response) -> bool:
    """Port of _mondayMutationOk -- Monday's GraphQL API returns HTTP 200
    even when a mutation fails, with the failure in a top-level "errors"
    array. A status-only check would count a rejected write as applied."""
    try:
        if resp is None or resp.status_code != 200:
            return False
        parsed = resp.json()
        return not (parsed.get("errors"))
    except Exception:  # noqa: BLE001
        return False


def _monday_find_items_for_stop(cfg: dict[str, str], stop_num: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    query = (
        "query { boards(ids: %s) {"
        " items_page(query_params: { rules: [{ column_id: \"%s\", compare_value: [%s], operator: contains_text }] }, limit: 50) {"
        " items { id name group { id } column_values(ids: [\"%s\"]) { id text } } } } }"
    ) % (int(cfg["board"]), cfg["addr_col"], _json_str(stop_num), cfg["addr_col"])
    resp = requests.post(MONDAY_URL, headers=headers, json={"query": query}, timeout=20)
    if resp.status_code != 200:
        return []
    try:
        parsed = resp.json()
    except ValueError:
        return []
    boards = (parsed.get("data") or {}).get("boards") or []
    if not boards:
        return []
    return ((boards[0].get("items_page") or {}).get("items")) or []


def _json_str(s: str) -> str:
    return _json.dumps(s)


def sync_monday_for_stops(locs: str, wo: str, installer_name: str, debug_label: str = "") -> dict[str, Any]:
    """Shared address-matched Monday.com sync used by both saveToFieldNation
    (installer_name="Field Nation" placeholder, pushed the moment a route is
    posted to FN) and markFNAssigned (installer_name=the real provider, once
    a dispatcher confirms assignment). This consolidates the two GAS
    functions' near-identical ~150-line address-match blocks into one.

    Returns {"skipped": reason} if MONDAY_API_TOKEN isn't set or there's no
    WO/installer to push -- exactly like GAS logged and moved on rather than
    failing the caller.

    2026-09-21 -- Nick: Terraboost doesn't use Monday.com anymore. Hard-disabled
    here regardless of MONDAY_API_TOKEN, rather than relying on that env var
    staying unset, so this can't fire even if a token gets set again by
    accident later. The rest of this function (and the GAS-ported logic below
    it) is left in place, unreached, in case Monday sync is ever reinstated --
    delete it instead of re-enabling it blind if that day doesn't come."""
    debug: list[str] = []
    return {"skipped": "Monday.com sync disabled 2026-09-21 (Terraboost no longer uses Monday)", "debug": debug, "matches": 0, "instUpdates": 0, "woUpdates": 0}
    cfg = _monday_config()
    if not cfg["token"]:
        return {"skipped": "MONDAY_API_TOKEN not set", "debug": debug, "matches": 0, "instUpdates": 0, "woUpdates": 0}
    if not wo and not installer_name:
        return {"skipped": "no WO or provider to push", "debug": debug, "matches": 0, "instUpdates": 0, "woUpdates": 0}

    group_filter = _monday_group_filter()
    headers = {"Authorization": cfg["token"]}
    debug.append(f"board={cfg['board']} addrCol={cfg['addr_col']} woCol={cfg['wo_col']} instCol={cfg['installer_col']}")
    debug.append(f"groups={','.join(group_filter) if group_filter else '*'}")
    debug.append(f'wo="{wo}" prov="{installer_name}"' + (" [" + debug_label + "]" if debug_label else ""))

    stops = _parse_stops_from_locs(locs)
    debug.append(f"stops={len(stops)}")

    mutation = (
        "mutation ($b: ID!, $i: ID!, $c: String!, $v: String!) "
        "{ change_simple_column_value(board_id: $b, item_id: $i, column_id: $c, value: $v) { id } }"
    )
    seen_item_ids: set[str] = set()
    total_matches = 0
    total_inst = 0
    total_wo = 0

    for stop in stops:
        items_raw = _monday_find_items_for_stop(cfg, stop["num"], headers)
        if not items_raw:
            debug.append(f"stop \"{stop['full']}\" raw=0")
            continue

        items = []
        for item in items_raw:
            addr_text = ""
            for cv in item.get("column_values") or []:
                if cv.get("id") == cfg["addr_col"]:
                    addr_text = cv.get("text") or ""
                    break
            addr_norm = _normalize_street(addr_text)
            street_norm = stop["streetNorm"]
            ok = stop["num"] in addr_norm and (
                len(street_norm) < 3 or street_norm[: min(8, len(street_norm))] in addr_norm
            )
            if ok and group_filter is not None:
                gid = (item.get("group") or {}).get("id") or ""
                if gid not in group_filter:
                    ok = False
            if ok:
                items.append(item)
        debug.append(f"stop \"{stop['full']}\" raw={len(items_raw)} filtered={len(items)}")

        for item in items:
            item_id = str(item.get("id"))
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            total_matches += 1

            if installer_name:
                try:
                    resp = requests.post(
                        MONDAY_URL, headers=headers,
                        json={"query": mutation, "variables": {"b": cfg["board"], "i": item_id, "c": cfg["installer_col"], "v": installer_name}},
                        timeout=20,
                    )
                    if _monday_mutation_ok(resp):
                        total_inst += 1
                    else:
                        debug.append(f"item {item_id} INST FAIL HTTP {resp.status_code}")
                except Exception as exc:  # noqa: BLE001
                    debug.append(f"item {item_id} INST err: {exc}")

            if wo:
                try:
                    resp = requests.post(
                        MONDAY_URL, headers=headers,
                        json={"query": mutation, "variables": {"b": cfg["board"], "i": item_id, "c": cfg["wo_col"], "v": wo}},
                        timeout=20,
                    )
                    if _monday_mutation_ok(resp):
                        total_wo += 1
                    else:
                        debug.append(f"item {item_id} WO FAIL HTTP {resp.status_code}")
                except Exception as exc:  # noqa: BLE001
                    debug.append(f"item {item_id} WO err: {exc}")

            time.sleep(0.04)

    debug.append(f"TOTAL items={total_matches} inst={total_inst} wo={total_wo}")
    return {"debug": debug, "matches": total_matches, "instUpdates": total_inst, "woUpdates": total_wo}


# ---------------------------------------------------------------------------
# Onfleet sync for markFNAssigned (routePlan rename + per-task metadata/worker
# re-PUT) -- Part A + Part B of the GAS function, minus the sheet move.
# ---------------------------------------------------------------------------

_FA_BUDGET_S = 5 * 60  # 5 min, same headroom GAS left under its 6-min hard kill


def sync_onfleet_for_fn_assignment(
    task_ids: list[str], wo: str, provider: str, route_plan_id_hint: str | None = None,
) -> dict[str, Any]:
    """Port of markFNAssigned's Part A (discover-and-rename the OnFleet
    routePlan) + Part B (per-task metadata + worker re-PUT so the
    task.assigned webhook fires and Make/Monday pick up the new WO/installer).
    Returns the resolved routePlanId (if any) so the caller can persist it on
    the field_nation_orders row for the fast path next time."""
    start = time.time()
    partial = False
    partial_reason = ""
    resolved_route_plan_id = route_plan_id_hint

    if not task_ids:
        return {"partial": True, "partialReason": "No Onfleet task IDs available to locate the Route Plan Name", "routePlanId": resolved_route_plan_id}

    headers = _onfleet_auth_header()
    new_meta = [
        {"name": "WO_NAME", "value": wo, "type": "string", "visibility": ["api"]},
        {"name": "INSTALLER_NAME", "value": provider or "", "type": "string", "visibility": ["api"]},
    ]

    # Part A: discover-and-rename.
    rp_id = resolved_route_plan_id
    if not rp_id and task_ids:
        try:
            task_get = onfleet_fetch_with_backoff("get", f"{ONFLEET_BASE}/tasks/{task_ids[0]}")
            if task_get.status_code == 200:
                task_json = task_get.json()
                if task_json.get("routePlan"):
                    rp_id = str(task_json["routePlan"])
        except Exception:  # noqa: BLE001
            pass

    if rp_id:
        try:
            rename_resp = onfleet_fetch_with_backoff("put", f"{ONFLEET_BASE}/routePlans/{rp_id}", json_body={"name": wo})
            if rename_resp.status_code < 400:
                resolved_route_plan_id = rp_id
            else:
                partial = True
                partial_reason = f"Onfleet Route Plan Name update returned HTTP {rename_resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            partial = True
            partial_reason = f"Onfleet Route Plan Name update failed: {exc}"
    else:
        partial = True
        partial_reason = "Onfleet route plan was not found for this route"

    # Part B: per-task metadata + worker re-PUT.
    for i, tid in enumerate(task_ids):
        if time.time() - start > _FA_BUDGET_S:
            partial = True
            partial_reason = (partial_reason + "; " if partial_reason else "") + f"time budget reached during Onfleet sync ({i}/{len(task_ids)} tasks)"
            break

        current_worker = None
        try:
            task_resp = onfleet_fetch_with_backoff("get", f"{ONFLEET_BASE}/tasks/{tid}")
            if task_resp.status_code == 200:
                current_worker = task_resp.json().get("worker")
        except Exception:  # noqa: BLE001
            pass

        try:
            onfleet_fetch_with_backoff("put", f"{ONFLEET_BASE}/tasks/{tid}", json_body={"metadata": new_meta})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.04)

        if current_worker:
            try:
                onfleet_fetch_with_backoff("put", f"{ONFLEET_BASE}/tasks/{tid}", json_body={"worker": current_worker, "metadata": new_meta})
            except Exception:  # noqa: BLE001
                pass

        if i < len(task_ids) - 1:
            time.sleep(0.07)

    return {"partial": partial, "partialReason": partial_reason, "routePlanId": resolved_route_plan_id}


def apply_fn_assigned_side_effects(payload: dict[str, Any], wo: str, provider: str, route_plan_id_hint: str | None = None) -> dict[str, Any]:
    """Orchestrates the OnFleet + Monday side effects for markFNAssigned,
    given the already-renamed WO and the confirmed provider name. Wrapped so
    a failure in either sync never raises -- matches GAS's "Don't fail the
    action -- the sheet move already succeeded" comment."""
    task_ids = [t.strip() for t in str(payload.get("taskIds") or "").split(",") if t.strip()]

    onfleet_result = {"partial": False, "partialReason": "", "routePlanId": route_plan_id_hint}
    try:
        onfleet_result = sync_onfleet_for_fn_assignment(task_ids, wo, provider, route_plan_id_hint)
    except Exception as exc:  # noqa: BLE001
        onfleet_result["partialReason"] = f"Onfleet sync exception: {exc}"
        onfleet_result["partial"] = True

    monday_result: dict[str, Any] = {}
    try:
        monday_result = sync_monday_for_stops(payload.get("locs") or "", wo, provider, debug_label="markFNAssigned")
    except Exception as exc:  # noqa: BLE001
        monday_result = {"skipped": f"exception: {exc}"}

    return {
        "partial": onfleet_result.get("partial", False),
        "partialReason": onfleet_result.get("partialReason", ""),
        "routePlanId": onfleet_result.get("routePlanId"),
        "monday": monday_result,
    }


def push_fn_placeholder_to_monday(payload: dict[str, Any], wo: str) -> dict[str, Any]:
    """Port of saveToFieldNation's Monday placeholder push -- installer is
    always the literal string "Field Nation" until markFNAssigned overwrites
    it with the real provider."""
    return sync_monday_for_stops(payload.get("locs") or "", wo, "Field Nation", debug_label="saveToFieldNation")
