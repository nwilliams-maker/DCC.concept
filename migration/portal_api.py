"""
Small FastAPI service that gives docs/portal-dcc-rw.html something to talk to
once Phase 2 (routes / field_nation_orders in Postgres) is live -- this is
Step 5 in migration/README.md, the one new piece of infrastructure in this
migration, not a straight port.

Deliberately narrow: two routes, mirroring GAS's doGet/doPost action-dispatch
style byte-for-byte in the request/response shapes portal-dcc-rw.html already
speaks --

  GET  /?action=getRoute&routeId=<wo>   -> {"payload": {...}} or {"error": "..."}
  POST / {"action": "processDecision", "routeId": <wo>, "decision": "Accepted"|"Declined", ...}
                                         -> {"onfleetSuccess": ..., "onfleetMsg": ..., "routeSuccess": ..., "routeMsg": ...}
                                            or {"error": "..."}

so that Step 6 ("repoint the portal") is a one-line change to portal-dcc-rw.html
(the `webAppUrl` constant) rather than a portal rewrite -- see that file's
window.onload and submitFinalResponse for the exact shapes this mirrors.

New links use `wo` as the routeId (the routes table's natural key). Older
emailed R-... route IDs are resolved through legacy_route_links. This is the SAME security posture
portal-dcc-rw.html's own [M24] comment already documents as a known,
deliberately-not-fixed-here limitation: the only thing standing between
"anyone with this link" and "a valid accept/decline" is how guessable the ID
in the URL is, and a work-order-shaped ID is no more (and no less) guessable
than whatever opaque ID GAS's routeId scheme uses today. A real fix (an
unguessable per-link HMAC token, checked here) is the same M24 finding and is
intentionally NOT addressed by this port -- swapping in this endpoint does
not resolve M24.

Run locally:
    export DATABASE_URL="postgresql://localhost/dcc_test"
    pip install -r requirements.txt -r migration/requirements.txt
    uvicorn migration.portal_api:app --reload --port 8000

Deploy: see migration/README.md, "Step 5, in practice: deploying
portal_api.py" for the Railway steps -- this needs its OWN Railway service,
it is not part of the Streamlit app's process.
"""
from __future__ import annotations

import json
import os
import hmac
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Support both `from migration import portal_api` (how the Streamlit app
# would import this if it ever needed to) and running this file as its own
# top-level module (`uvicorn migration.portal_api:app`, or the tests) --
# same dual-import pattern data_access.py already uses for fn_side_effects.
try:
    from . import data_access as da
except ImportError:
    import data_access as da

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
ROUTE_SYNC_TOKEN = (os.environ.get("ROUTE_SYNC_TOKEN") or "").strip()
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL must be set -- this must point at the SAME Postgres "
        "database the Streamlit app's DATABASE_URL points at, not a "
        "separate one, since both read/write the same `routes` table."
    )
engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)

# The portal is static HTML served from GitHub Pages -- a different origin
# than wherever this API is deployed -- so the browser needs an explicit CORS
# allow before it will hand the page's JS the response body. Restrict to the
# actual portal origin(s) rather than "*": unlike a read-only API, a POST
# here can flip a route to accepted/declined, so it shouldn't quietly answer
# every origin on the internet.
_ALLOWED_ORIGINS = [
    o.strip()
    for o in (os.environ.get("PORTAL_ALLOWED_ORIGINS") or "https://nwilliams-maker.github.io").split(",")
    if o.strip()
]

app = FastAPI(title="DCC portal API")


@app.post("/internal/routes/sync")
async def internal_route_sync(request: Request):
    """Authenticated cross-project route-state replication endpoint."""
    if not ROUTE_SYNC_TOKEN:
        return JSONResponse({"error": "Route sync not configured."}, status_code=503)
    auth = request.headers.get("authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {ROUTE_SYNC_TOKEN}"):
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    body = await request.json()
    route = body.get("route") if isinstance(body, dict) else None
    if not isinstance(route, dict):
        return JSONResponse({"error": "Missing route."}, status_code=400)
    wo = str(route.get("wo") or "").strip()
    status = str(route.get("status") or "").strip().lower()
    if not wo or status not in {"sent", "accepted", "declined", "finalized", "archived"}:
        return JSONResponse({"error": "Invalid route."}, status_code=400)

    payload = route.get("payload")
    locs = route.get("locs")
    stop_data = route.get("stop_data")
    if not isinstance(payload, str):
        payload = json.dumps(payload or {})
    if locs is not None and not isinstance(locs, str):
        locs = json.dumps(locs)
    if stop_data is not None and not isinstance(stop_data, str):
        stop_data = json.dumps(stop_data)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO routes
                    (wo, contractor_id, contractor_name, status, comp, due, locs,
                     stop_data, cluster_hash, payload, created_at, updated_at)
                VALUES
                    (:wo,
                     (SELECT id FROM contractors
                      WHERE lower(email) = lower(COALESCE(CAST(:email AS text), ''))
                      LIMIT 1),
                     :contractor_name, CAST(:status AS route_status), :comp, :due,
                     CAST(:locs AS jsonb), CAST(:stop_data AS jsonb), :cluster_hash,
                     CAST(:payload AS jsonb),
                     COALESCE(CAST(:created_at AS timestamptz), now()),
                     COALESCE(CAST(:updated_at AS timestamptz), now()))
                ON CONFLICT (wo) DO UPDATE SET
                    contractor_name = EXCLUDED.contractor_name,
                    status = EXCLUDED.status,
                    comp = EXCLUDED.comp,
                    due = EXCLUDED.due,
                    locs = EXCLUDED.locs,
                    stop_data = EXCLUDED.stop_data,
                    cluster_hash = EXCLUDED.cluster_hash,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "wo": wo,
                "email": (json.loads(payload).get("ice") if payload else None),
                "contractor_name": route.get("contractor_name"),
                "status": status,
                "comp": route.get("comp"),
                "due": route.get("due"),
                "locs": locs,
                "stop_data": stop_data,
                "cluster_hash": route.get("cluster_hash"),
                "payload": payload,
                "created_at": route.get("created_at"),
                "updated_at": route.get("updated_at"),
            },
        )
    return {"success": True, "wo": wo, "status": status}


@app.post("/internal/contractors/sync")
async def sync_recent_contractors(request: Request):
    """Receive the Monday IC/FA intake from DCC's existing hourly sync.

    This writes Revamp's own Postgres roster, which its contractor picker and
    bounded OnFleet worker reconciliation already read. The dedicated token
    is required even when the endpoint is reachable from the public domain.
    """
    expected = (os.environ.get("REVAMP_CONTRACTOR_SYNC_TOKEN") or "").strip()
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    rows = body.get("contractors") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) > 5000:
        return JSONResponse({"error": "Expected at most 5000 contractors"}, status_code=400)

    from .contractor_sync import normalize_email, normalize_phone, _clean_text
    prepared = []
    skipped = 0
    for source in rows:
        if not isinstance(source, dict):
            skipped += 1
            continue
        try:
            created = datetime.fromisoformat(str(source.get("monday_created_at") or "").replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            skipped += 1
            continue
        email = normalize_email(source.get("email"))
        name = _clean_text(source.get("name"))
        phone = normalize_phone(source.get("phone"))
        location = _clean_text(source.get("location"))
        status = (_clean_text(source.get("ic_list")) or "").upper()
        # Store the contractor record even when phone/location is incomplete.
        # Those fields affect route eligibility / OnFleet creation, but should
        # never prevent a valid Monday IC/FA row from existing in Postgres.
        if not email or not name:
            skipped += 1
            continue
        prepared.append({
            "monday_item_id": str(source.get("monday_item_id") or ""),
            "monday_created_at": created,
            "email": email, "name": name, "phone": "+1" + phone if phone and len(phone) == 10 else _clean_text(source.get("phone")),
            "location": location, "ic_list": status or None,
            "pod_color": _clean_text(source.get("pod_color")),
            "digital_certified": bool(source.get("digital_certified")),
            "unrestricted": bool(source.get("unrestricted")),
        })

    added = updated = conflicts = 0
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE IF NOT EXISTS contractor_monday_intake (
                email TEXT PRIMARY KEY,
                monday_item_id TEXT NOT NULL,
                monday_created_at TIMESTAMPTZ NOT NULL
            )
        """))
        for row in prepared:
            existing = conn.execute(sa.text("SELECT id, name, phone FROM contractors WHERE email = :email"), row).mappings().first()
            if existing:
                old_phone = normalize_phone(existing["phone"])
                new_phone = normalize_phone(row["phone"])
                if old_phone and new_phone and old_phone != new_phone and existing["name"].strip().casefold() != row["name"].casefold():
                    conflicts += 1
                    continue
                conn.execute(sa.text("""
                    UPDATE contractors SET name=:name, phone=:phone, location=:location,
                      ic_list=:ic_list, pod_color=COALESCE(:pod_color, pod_color),
                      digital_certified=:digital_certified, unrestricted=:unrestricted,
                      lat=CASE WHEN location IS DISTINCT FROM :location THEN NULL ELSE lat END,
                      lng=CASE WHEN location IS DISTINCT FROM :location THEN NULL ELSE lng END,
                      updated_at=now()
                    WHERE email=:email
                """), row)
                updated += 1
            else:
                conn.execute(sa.text("""
                    INSERT INTO contractors (email, name, phone, location, ic_list, pod_color,
                        digital_certified, unrestricted)
                    VALUES (:email, :name, :phone, :location, :ic_list, :pod_color,
                        :digital_certified, :unrestricted)
                """), row)
                added += 1
            conn.execute(sa.text("""
                INSERT INTO contractor_monday_intake (email, monday_item_id, monday_created_at)
                VALUES (:email, :monday_item_id, :monday_created_at)
                ON CONFLICT (email) DO UPDATE SET monday_item_id=EXCLUDED.monday_item_id,
                    monday_created_at=EXCLUDED.monday_created_at
            """), row)
    return {"received": len(rows), "added": added, "updated": updated,
            "skipped": skipped, "conflicts": conflicts}


@app.on_event("startup")
def _optional_one_time_recovery() -> None:
    """Explicit one-time Railway trigger; disabled during normal operation."""
    if os.environ.get("RECOVERY_RUN_ON_STARTUP") == "1":
        from .recover_legacy_routes import main
        main()
    # The portal API stays online even when no dispatcher has signed in to
    # Streamlit. Reconcile its shared Postgres roster from this process too.
    if os.environ.get("ONFLEET_KEY"):
        from .onfleet_contractor_reconcile import start_background_reconciliation
        start_background_reconciliation(engine)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _resolve_route(conn, route_id: str):
    """Resolve new WO links and pre-migration R- links without changing their URLs."""
    row = conn.execute(
        sa.text("SELECT wo, payload FROM routes WHERE wo = :identifier"),
        {"identifier": route_id},
    ).mappings().first()
    if row:
        return row["wo"], row["payload"], False
    # The recovery job creates this table. A normal WO link continues to work
    # during deployment even if the recovery job has not run yet.
    try:
        row = conn.execute(
            sa.text("""
                SELECT l.wo, l.payload, l.active
                FROM legacy_route_links l
                WHERE l.route_id = :identifier
            """), {"identifier": route_id}
        ).mappings().first()
    except sa.exc.ProgrammingError:
        return None
    if not row or not row["active"]:
        return None
    return row["wo"], row["payload"], True


@app.get("/")
def get_route(action: str = "", routeId: str = ""):
    """Mirrors GAS's `?action=getRoute&routeId=<wo>`. portal-dcc-rw.html's
    window.onload reads `res.error` (shows it and stops) or `res.payload`
    (renders the route summary) -- those are the only two shapes it checks
    for, so those are the only two shapes returned here."""
    if action != "getRoute":
        return JSONResponse({"error": "Unknown action."}, status_code=400)
    wo = (routeId or "").strip()
    if not wo:
        return JSONResponse({"error": "Missing routeId."}, status_code=400)

    with engine.connect() as conn:
        route = _resolve_route(conn, wo)
    if not route:
        # Matches the portal's existing error-banner path (see the [M22]/[H4]
        # comments in portal-dcc-rw.html) -- a plain res.error string it
        # already knows how to display.
        return {"error": "This route link has expired or the route was not found."}

    payload_value = route[1]
    payload = payload_value if isinstance(payload_value, dict) else json.loads(payload_value)
    return {"payload": payload}


@app.post("/")
async def post_decision(request: Request):
    """Mirrors GAS's doPost({action: "processDecision", ...}) -- the only
    action the portal ever POSTs (see submitFinalResponse's `gasPayload` in
    portal-dcc-rw.html). Calls data_access.process_decision(), which already
    returns the exact onfleetSuccess/onfleetMsg/routeSuccess/routeMsg/error
    shape the portal's success/error UI branches expect -- no translation
    needed here beyond the decision-string mapping below."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    if body.get("action") != "processDecision":
        return JSONResponse({"error": "Unknown action."}, status_code=400)

    # The portal's radio buttons send "Accepted"/"Declined" (see ic_decision
    # in portal-dcc-rw.html); process_decision() expects "accept"/"decline".
    decision = "accept" if str(body.get("decision", "")).strip().lower() == "accepted" else "decline"
    route_id = str(body.get("routeId") or body.get("wo") or "").strip()
    if not route_id:
        return JSONResponse({"error": "Missing routeId."}, status_code=400)

    with engine.connect() as conn:
        resolved = _resolve_route(conn, route_id)
        if not resolved:
            return {"error": "This route link has expired or the route was not found."}
        wo, linked_payload, is_legacy = resolved
        if is_legacy:
            current = conn.execute(sa.text("SELECT payload, status::text AS status FROM routes WHERE wo = :wo"), {"wo": wo}).mappings().first()
            if not current:
                return {"error": "This route link has expired or the route was not found."}
            if current["status"] in ("archived", "finalized"):
                return {"error": "This route is no longer open. Contact dispatch."}
            if current["status"] == "sent" and current["payload"] != linked_payload:
                return {"error": "This route was changed after this link was sent. Contact dispatch for the current link."}

    try:
        result = da.process_decision(
            engine,
            wo,
            decision=decision,
            signature=str(body.get("signature", "")),
            notes=str(body.get("notes", "")),
            phone=str(body.get("phone", "")),
            task_ids=str(body.get("taskIds", "")),
            stop_order=str(body.get("stopOrder", "")),
            comp=body.get("comp"),
        )
    except Exception as exc:  # noqa: BLE001 -- never let this 500 into a blank/frozen portal page; surface it in the JSON body the portal already parses (see submitFinalResponse's errMsg fallback chain)
        return {"error": f"Server error processing decision: {exc}"}

    return result
