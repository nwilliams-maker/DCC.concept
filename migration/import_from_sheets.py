"""
One-time import: pull every tab of the DCC Google Sheet and load it into the
new Postgres database (see schema.sql). Safe to re-run -- every insert is an
upsert keyed on the same natural key the app already treats as unique
(contractor email, route WO, Field Nation work order).

Usage:
    export DATABASE_URL="postgresql://user:pass@host:port/dbname"
    export IC_SHEET_URL="https://docs.google.com/spreadsheets/d/XXXX/edit"
    pip install -r migration/requirements.txt
    python migration/import_from_sheets.py

Run this against a STAGING database first. Spot-check row counts and a
handful of records against the live Sheet before pointing the real app at
the result (see migration/README.md, step 3).
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd
import sqlalchemy as sa

IC_SHEET_URL = os.environ.get("IC_SHEET_URL", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not IC_SHEET_URL or not DATABASE_URL:
    sys.exit(
        "Set both IC_SHEET_URL and DATABASE_URL as environment variables "
        "before running this script (see the docstring above)."
    )

BASE_EXPORT_URL = f"{IC_SHEET_URL.split('/edit')[0]}/export?format=csv&gid="

CONTRACTORS_GID = "0"
ROUTE_TABS = [
    ("1477617688", "sent"),
    ("934075207", "accepted"),
    ("600909788", "declined"),
    ("1907347870", "finalized"),
]
FIELD_NATION_GID = "1396320527"
ARCHIVE_GID = "1841508981"

# Names from the app's hardcoded _UNRESTRICTED_ICS list -- ported here as
# data (contractors.unrestricted) instead of code. Matched the same way the
# app does today: case-insensitive substring match against the contractor's
# name. Confirm this list against the current app before running for real.
UNRESTRICTED_NAME_FRAGMENTS = ["biscardi", "ledbetter", "erlandson", "o'connor", "oconnor"]

# Known-bad fields on specific contractor rows in the Sheet (confirmed by
# hand). Applied after the normal row build so the correct value always wins,
# regardless of which of that email's duplicate rows the Sheet happens to
# list last. Keyed by normalized (lowercased, stripped) email.
CONTRACTOR_FIELD_OVERRIDES: dict[str, dict[str, str]] = {
    "robert@niekotech.com": {"phone": "18186324368"},
}


def fetch_csv(gid: str) -> pd.DataFrame:
    df = pd.read_csv(BASE_EXPORT_URL + gid)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def _to_float(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_str(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s or None


def import_contractors(engine: sa.Engine) -> None:
    df = fetch_csv(CONTRACTORS_GID)
    rows = []
    for _, r in df.iterrows():
        email = (_clean_str(r.get("email")) or "").lower()
        if not email:
            continue
        name = _clean_str(r.get("name")) or ""
        is_unrestricted = any(frag in name.lower() for frag in UNRESTRICTED_NAME_FRAGMENTS)
        row = {
            "email": email,
            "name": name,
            "location": _clean_str(r.get("location")),
            "phone": _clean_str(r.get("phone")),
            "ic_list": _clean_str(r.get("ic list")),
            "lat": _to_float(r.get("lat")),
            "lng": _to_float(r.get("lng")),
            "pod_color": _clean_str(r.get("pod color")),
            "digital_certified": (_clean_str(r.get("digital certified")) or "").upper() == "YES",
            "unrestricted": is_unrestricted,
        }
        row.update(CONTRACTOR_FIELD_OVERRIDES.get(email, {}))
        rows.append(row)
    with engine.begin() as conn:
        for row in rows:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO contractors
                        (email, name, location, phone, ic_list, lat, lng, pod_color, digital_certified, unrestricted)
                    VALUES
                        (:email, :name, :location, :phone, :ic_list, :lat, :lng, :pod_color, :digital_certified, :unrestricted)
                    ON CONFLICT (email) DO UPDATE SET
                        name = EXCLUDED.name, location = EXCLUDED.location, phone = EXCLUDED.phone,
                        ic_list = EXCLUDED.ic_list, lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                        pod_color = EXCLUDED.pod_color, digital_certified = EXCLUDED.digital_certified,
                        unrestricted = EXCLUDED.unrestricted, updated_at = now()
                    """
                ),
                row,
            )
    print(f"Imported {len(rows)} contractors.")


def import_route_tab(engine: sa.Engine, gid: str, status: str) -> None:
    df = fetch_csv(gid)
    inserted = 0
    with engine.begin() as conn:
        for _, r in df.iterrows():
            raw_payload = r.get("json payload")
            if raw_payload is None or (isinstance(raw_payload, float) and pd.isna(raw_payload)):
                continue
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError):
                continue
            wo = payload.get("wo") or _clean_str(r.get("wo"))
            if not wo:
                continue
            conn.execute(
                sa.text(
                    """
                    INSERT INTO routes
                        (wo, contractor_name, status, comp, due, locs, stop_data, cluster_hash, payload)
                    VALUES
                        (:wo, :contractor_name, :status, :comp, :due, :locs, :stop_data, :cluster_hash, :payload)
                    ON CONFLICT (wo) DO UPDATE SET
                        status = EXCLUDED.status, payload = EXCLUDED.payload, updated_at = now()
                    """
                ),
                {
                    "wo": wo,
                    "contractor_name": _clean_str(r.get("contractor")) or "",
                    "status": status,
                    "comp": payload.get("comp"),
                    "due": payload.get("due"),
                    "locs": json.dumps(payload.get("locs")) if payload.get("locs") is not None else None,
                    "stop_data": json.dumps(payload.get("stopData")) if payload.get("stopData") is not None else None,
                    "cluster_hash": payload.get("cluster_hash"),
                    "payload": json.dumps(payload),
                },
            )
            inserted += 1
    print(f"Imported {inserted} routes with status={status} (gid={gid}).")


def import_field_nation(engine: sa.Engine) -> None:
    df = fetch_csv(FIELD_NATION_GID)
    inserted = 0
    with engine.begin() as conn:
        for _, r in df.iterrows():
            wo = _clean_str(r.get("work order"))
            if not wo:
                continue
            raw_payload = r.get("json payload")
            try:
                payload = json.loads(raw_payload) if raw_payload and not pd.isna(raw_payload) else {}
            except (TypeError, ValueError):
                payload = {}
            status = "assigned" if "assign" in (_clean_str(r.get("status")) or "").lower() else "posted"
            conn.execute(
                sa.text(
                    """
                    INSERT INTO field_nation_orders (work_order, status, payload)
                    VALUES (:work_order, :status, :payload)
                    ON CONFLICT (work_order) DO UPDATE SET
                        status = EXCLUDED.status, payload = EXCLUDED.payload, updated_at = now()
                    """
                ),
                {"work_order": wo, "status": status, "payload": json.dumps(payload)},
            )
            inserted += 1
    print(f"Imported {inserted} Field Nation orders.")


def import_archive_events(engine: sa.Engine) -> None:
    """Archive rows become route_events, not a separate table -- see schema.sql.

    NOT called from main() by design: per decision, the old Sheet's Archive
    tab history is not carried over into Postgres. route_events starts empty
    and fills in going forward as the app writes archiveRoute / finalizeRoute
    / etc. events through data_access.py. Left here in case someone wants to
    backfill history later -- call it manually if so, but note the inserts
    below are plain INSERTs (no ON CONFLICT), so re-running it would duplicate
    rows.
    """
    df = fetch_csv(ARCHIVE_GID)
    inserted = 0
    with engine.begin() as conn:
        for _, r in df.iterrows():
            raw_payload = r.get("json payload")
            if raw_payload is None or (isinstance(raw_payload, float) and pd.isna(raw_payload)):
                continue
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError):
                continue
            wo = payload.get("archived_wo") or payload.get("wo")
            route_id = None
            if wo:
                row = conn.execute(sa.text("SELECT id FROM routes WHERE wo = :wo"), {"wo": wo}).fetchone()
                route_id = row[0] if row else None
            conn.execute(
                sa.text(
                    """
                    INSERT INTO route_events (route_id, action, payload)
                    VALUES (:route_id, :action, :payload)
                    """
                ),
                {
                    "route_id": route_id,
                    "action": payload.get("archive_action") or "archiveRoute",
                    "payload": json.dumps(payload),
                },
            )
            inserted += 1
    print(f"Imported {inserted} archive events.")


def main() -> None:
    engine = sa.create_engine(DATABASE_URL)
    import_contractors(engine)
    for gid, status in ROUTE_TABS:
        import_route_tab(engine, gid, status)
    import_field_nation(engine)
    # Archive tab history is intentionally NOT imported -- route_events starts
    # fresh and fills in from here going forward. See import_archive_events()
    # docstring if that decision ever changes.
    print("Done. Spot-check row counts and a sample of records against the live Sheet before cutover.")


if __name__ == "__main__":
    main()
