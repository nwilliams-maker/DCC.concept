"""Dispatch Bot: proposes an IC (or Field Nation) and a rate for every live route.

Nick's rules (Oct 15 2026):
  * Give each route to the CLOSEST eligible IC within reason who is not
    already overloaded. Do not reach farther just to use a new IC.
  * New ICs (0 OnFleet tasks and no Route Request waiting on them) are
    preferred when they are essentially as close as the closest IC.
  * Rate: 1-5 stops -> calculated rate kept between $20.00 and $24.50/stop.
          6+ stops  -> $20.00/stop.
  * 1-5 stop routes whose closest IC is more than 25 mi away go to
    Field Nation instead of an IC.

This module never sends anything. It only builds a plan for the dispatcher
to review; revamp_workspace applies an approved plan through the existing
Route Request and Field Nation code paths.
"""

import re
from dataclasses import dataclass, field

ELIGIBLE = ("ACTIVE", "IN TRAINING", "NEED INSURANCE")
SMALL_ROUTE_MAX_STOPS = 5
SMALL_ROUTE_RATE_MIN = 20.00
SMALL_ROUTE_RATE_MAX = 24.50
LARGE_ROUTE_RATE = 20.00
HOURLY = 25.0


@dataclass
class BotSettings:
    fn_distance_small_routes: float = 25.0   # 1-5 stops: closest IC farther than this -> Field Nation
    max_ic_distance: float = 60.0            # "within reason"; matches DCC's long-distance flag
    max_open_tasks: int = 25                 # OnFleet tasks already on the IC (🔵N badge)
    max_waiting_routes: int = 2              # Route Requests sent but not yet answered
    new_ic_edge_miles: float = 5.0           # a 0-task IC wins if within this of the closest IC
    reoffer_max_miles: float = 40.0          # after a decline: keep offering out to this, then FN
    removal_max_miles: float = 30.0          # CVS Removals: any eligible IC within this, else FN


@dataclass
class Proposal:
    key: str                  # "<pod>:<route_hash>" (same as bulk checkbox keys)
    pod: str
    route_hash: str
    city: str
    state: str
    stops: int
    tasks: int
    action: str               # "ic" | "fn" | "skip"
    reason: str
    ic_name: str = ""
    ic_identity: str = ""
    ic_miles: float = 0.0
    ic_open_tasks: object = None
    is_new_ic: bool = False
    calc_rate: float = 0.0
    rate: float = 0.0
    pay: float = 0.0
    closest_miles: object = None
    extra: dict = field(default_factory=dict)


def stop_count(route):
    seen = {str(t.get("full") or "") for t in (route.get("data") or [])}
    seen.discard("")
    try:
        declared = int(route.get("stops") or 0)
    except (TypeError, ValueError):
        declared = 0
    # The dispatch card divides pay by route["stops"]; use the same count.
    return declared if declared > 0 else max(len(seen), 1)


def rate_for_route(stops, calc_rate):
    """Nick's rate rule. calc_rate = route hours x $25 / stops from the IC's home."""
    if stops > SMALL_ROUTE_MAX_STOPS:
        return LARGE_ROUTE_RATE
    try:
        calc = float(calc_rate or 0)
    except (TypeError, ValueError):
        calc = 0.0
    return round(min(max(calc, SMALL_ROUTE_RATE_MIN), SMALL_ROUTE_RATE_MAX), 2)


def phone_last10(raw):
    raw = "" if raw is None else str(raw).strip()
    if raw in ("", "nan", "None"):
        return ""
    try:
        raw = str(int(float(raw)))
    except (TypeError, ValueError):
        pass
    return re.sub(r"\D", "", raw)[-10:]


def waiting_routes_by_ic(sent_db):
    """Distinct Route Requests still waiting on a reply, per IC name (casefolded)."""
    waiting = {}
    for record in (sent_db or {}).values():
        if str(record.get("status") or "").lower() != "sent":
            continue
        name = str(record.get("name") or "").strip().casefold()
        wo = str(record.get("wo") or "").strip()
        if name and wo:
            waiting.setdefault(name, set()).add(wo)
    return {name: len(wos) for name, wos in waiting.items()}


def candidate_ics(ic_df, is_fa_employee=None):
    """Eligible IC rows with usable coordinates (dicts with the original row)."""
    if ic_df is None or getattr(ic_df, "empty", True):
        return []
    cols = {str(c).strip().lower(): c for c in ic_df.columns}
    if "ic list" not in cols or "lat" not in cols or "lng" not in cols:
        return []
    out = []
    for _, row in ic_df.iterrows():
        if str(row.get(cols["ic list"], "") or "").strip().upper() not in ELIGIBLE:
            continue
        if row.astype(str).str.contains("Field Agent", case=False, na=False).any():
            continue
        if is_fa_employee and is_fa_employee(row):
            continue  # salaried FA employees are not paid per route
        try:
            lat, lng = float(row[cols["lat"]]), float(row[cols["lng"]])
        except (TypeError, ValueError):
            continue
        if lat != lat or lng != lng or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        out.append({
            "row": row,
            "name": str(row.get(cols.get("name"), "") or "Unknown").strip(),
            "lat": lat, "lng": lng,
            "phone": phone_last10(row.get(cols["phone"])) if "phone" in cols else "",
        })
    return out


def plan_routes(entries, ics, haversine, route_center, route_hours,
                worker_counts, waiting_counts, selection_key,
                settings=None, mode="routes", declined_by=None, states=None):
    """Build a Proposal for each Ready/Flagged route entry.

    entries: (pod, route, state, route_hash, nearest) tuples from the workspace.
    route_hours(ic_row, route) -> driving+stop hours for the full round trip.
    worker_counts: {phone_last10: open OnFleet tasks}; empty dict = unknown.
    mode: "routes"   - Routes tab rules (closest open IC, new-IC edge, FN for far small routes)
          "removals" - CVS Removal tab: any eligible IC within removal_max_miles,
                       spread so nobody gets two while someone else nearby has none
          "reoffer"  - a declined route: next closest open IC within reoffer_max_miles
                       who has not declined it; Field Nation once nobody is left
    declined_by: {entry key: {casefolded IC names who declined it}} - always skipped.
    states: which route states to plan (default Ready/Flagged; Declined for "reoffer").
    """
    s = settings or BotSettings()
    declined_by = declined_by or {}
    live_states = states or (("Declined",) if mode == "reoffer" else ("Ready", "Flagged"))
    counts_known = bool(worker_counts)
    waiting = dict(waiting_counts or {})
    given_this_run = {}
    proposals = []

    work = []
    for pod, route, state, route_hash, _nearest in entries:
        if state not in live_states:
            continue
        base = dict(key=f"{pod}:{route_hash}", pod=pod, route_hash=route_hash,
                    city=str(route.get("city") or ""), state=str(route.get("state") or ""),
                    stops=stop_count(route), tasks=len(route.get("data") or []))
        if pod in ("Global_Digital", "Digital") or route.get("is_digital"):
            proposals.append(Proposal(**base, action="skip",
                                      reason="Digital route — needs a digital-certified IC; left for you"))
            continue
        center = route_center(route)
        if not center:
            proposals.append(Proposal(**base, action="skip", reason="Route has no map location"))
            continue
        refused = {str(n).casefold() for n in declined_by.get(f"{pod}:{route_hash}", ())}
        dists = []
        for ic in ics:
            if ic["name"].casefold() in refused:
                continue
            d = haversine(center[0], center[1], ic["lat"], ic["lng"])
            if d is not None and d == d and d != float("inf"):
                dists.append((d, ic))
        dists.sort(key=lambda pair: pair[0])
        work.append((dists[0][0] if dists else float("inf"), base, route, dists))

    # Tightest matches first, so a nearby IC isn't used up by a far route.
    work.sort(key=lambda item: item[0])

    for closest, base, route, dists in work:
        stops = base["stops"]
        closest_val = None if closest == float("inf") else round(closest, 1)
        refused = declined_by.get(base["key"]) or ()
        declined_note = f"; skipped {len(refused)} IC(s) who declined" if refused else ""

        if mode == "removals":
            proposals.append(_plan_removal(base, route, dists, closest_val, s, waiting,
                                           given_this_run, route_hours, selection_key,
                                           declined_note))
            continue

        limit = s.reoffer_max_miles if mode == "reoffer" else s.max_ic_distance
        if mode == "routes" and stops <= SMALL_ROUTE_MAX_STOPS and closest > s.fn_distance_small_routes:
            where = "no IC on file" if closest_val is None else f"closest IC is {closest_val} mi"
            proposals.append(Proposal(**base, action="fn", closest_miles=closest_val,
                                      reason=f"{stops} stop(s) and {where} (> {s.fn_distance_small_routes:g} mi)"))
            continue

        def load(ic):
            open_tasks = worker_counts.get(ic["phone"], 0) if (counts_known and ic["phone"]) else None
            pending = waiting.get(ic["name"].casefold(), 0) + given_this_run.get(ic["name"].casefold(), 0)
            return open_tasks, pending

        available, busy = [], 0
        for d, ic in dists:
            if d > limit:
                break
            open_tasks, pending = load(ic)
            if (open_tasks is not None and open_tasks >= s.max_open_tasks) or pending >= s.max_waiting_routes:
                busy += 1
                continue
            available.append((d, ic, open_tasks, pending))

        if not available and mode == "reoffer":
            why = (f"all {busy} other IC(s) within {limit:g} mi are loaded up" if busy else
                   f"no other IC within {limit:g} mi")
            proposals.append(Proposal(**base, action="fn", closest_miles=closest_val,
                                      reason=why + declined_note))
            continue
        if not available:
            if closest_val is None or closest > s.max_ic_distance:
                why = f"closest IC is {closest_val} mi" if closest_val is not None else "no IC on file"
                reason = f"{why} (> {s.max_ic_distance:g} mi) — left for you"
            else:
                reason = f"all {busy} IC(s) within {s.max_ic_distance:g} mi are already loaded up — left for you"
            proposals.append(Proposal(**base, action="skip", closest_miles=closest_val, reason=reason))
            continue

        if (mode == "routes" and stops <= SMALL_ROUTE_MAX_STOPS
                and available[0][0] > s.fn_distance_small_routes):
            # Nearby ICs are all loaded up; a 1-5 stop route never reaches past
            # the Field Nation distance just to find someone free.
            proposals.append(Proposal(
                **base, action="fn", closest_miles=closest_val,
                reason=(f"{stops} stop(s); ICs within {s.fn_distance_small_routes:g} mi are "
                        f"loaded up and the next free IC is {round(available[0][0], 1)} mi")
                + declined_note))
            continue

        best_d = available[0][0]
        new_ics = [a for a in available
                   if a[2] == 0 and a[3] == 0 and a[0] <= best_d + s.new_ic_edge_miles]
        pick = new_ics[0] if new_ics else available[0]
        d, ic, open_tasks, pending = pick
        is_new = pick in new_ics

        try:
            hours = float(route_hours(ic["row"], route) or 0)
        except Exception:
            hours = 0.0
        calc_rate = round(hours * HOURLY / stops, 2) if hours > 0 else 0.0
        rate = rate_for_route(stops, calc_rate)
        if is_new and pick is not available[0]:
            reason = f"New IC (0 tasks), {round(d - best_d, 1)} mi past the closest open IC"
        elif is_new:
            reason = "Closest open IC — new (0 tasks)"
        else:
            reason = "Closest open IC"
        if busy:
            reason += f"; skipped {busy} busier IC(s)"
        reason += declined_note
        given_this_run[ic["name"].casefold()] = given_this_run.get(ic["name"].casefold(), 0) + 1
        proposals.append(Proposal(
            **base, action="ic", reason=reason, ic_name=ic["name"],
            ic_identity=selection_key(ic["row"]), ic_miles=round(d, 1),
            ic_open_tasks=open_tasks, is_new_ic=is_new, calc_rate=calc_rate,
            rate=rate, pay=round(rate * stops, 2), closest_miles=closest_val))

    order = {"ic": 0, "fn": 1, "skip": 2}
    proposals.sort(key=lambda p: (order[p.action], p.pod, p.state, p.city))
    return proposals


def _plan_removal(base, route, dists, closest_val, s, waiting, given_this_run,
                  route_hours, selection_key, declined_note=""):
    """CVS Removal: whoever is within removal_max_miles. Nobody gets a second
    removal while another IC in range has none (this run or still waiting)."""
    stops = base["stops"]
    in_range = [(d, ic) for d, ic in dists if d <= s.removal_max_miles]
    if not in_range:
        where = "no IC on file" if closest_val is None else f"closest IC is {closest_val} mi"
        return Proposal(**base, action="fn", closest_miles=closest_val,
                        reason=f"CVS Removal; {where} (> {s.removal_max_miles:g} mi){declined_note}")

    def doubled(ic):
        name = ic["name"].casefold()
        return given_this_run.get(name, 0) + waiting.get(name, 0)

    d, ic = min(in_range, key=lambda pair: (doubled(pair[1]), pair[0]))
    name = ic["name"].casefold()
    others_free = doubled(ic) == 0
    given_this_run[name] = given_this_run.get(name, 0) + 1
    try:
        hours = float(route_hours(ic["row"], route) or 0)
    except Exception:
        hours = 0.0
    calc_rate = round(hours * HOURLY / stops, 2) if hours > 0 else 0.0
    rate = rate_for_route(stops, calc_rate)
    reason = ("CVS Removal; closest IC without one" if others_free else
              f"CVS Removal; everyone within {s.removal_max_miles:g} mi already has one")
    return Proposal(**base, action="ic", reason=reason + declined_note, ic_name=ic["name"],
                    ic_identity=selection_key(ic["row"]), ic_miles=round(d, 1),
                    calc_rate=calc_rate, rate=rate, pay=round(rate * stops, 2),
                    closest_miles=closest_val)


def decliners_by_key(entries, sent_db, history_rows=()):
    """{entry key: {casefolded IC names who declined any of its tasks}}.

    sent_db gives the current decline; history_rows ((contractor_name, task_ids),
    from every past declined Route Request) covers earlier rounds after the
    route was re-offered and got a new WO.
    """
    by_task = {}
    for task_id, record in (sent_db or {}).items():
        if str(record.get("status") or "").lower() == "declined" and record.get("name"):
            by_task.setdefault(str(task_id).strip(), set()).add(str(record["name"]).strip().casefold())
    for name, task_ids in history_rows or ():
        if not name:
            continue
        for tid in task_ids or ():
            by_task.setdefault(str(tid).strip(), set()).add(str(name).strip().casefold())
    out = {}
    for pod, route, _state, route_hash, _nearest in entries:
        names = set()
        for task in route.get("data") or []:
            names |= by_task.get(str(task.get("id") or "").strip(), set())
        if names:
            out[f"{pod}:{route_hash}"] = names
    return out
