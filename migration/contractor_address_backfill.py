"""One-time repair of contractors.location / lat / lng in Postgres.

Why: before the contractor_sync fixes, (1) the geocoder's street-number /
state / ZIP checks never matched, so vague city-level Mapbox hits were saved
as exact IC coordinates; (2) the Monday "location" column was picked at
random among Address / Location / Service Area / City-State; (3) a changed
address could keep the old coordinates. The regular sync only looks at
Monday items edited in the last 48h, so existing rows never self-heal.

This script re-reads EVERY item on the Monday IC board, re-geocodes every
matched contractor with the fixed rules, and reports old vs. new.

    python -m migration.contractor_address_backfill                 # dry run, writes CSV
    python -m migration.contractor_address_backfill --apply         # write fixes
    python -m migration.contractor_address_backfill --apply --clear-unverified

Needs DATABASE_URL, MONDAY_API_TOKEN, MAPBOX_TOKEN. Touches only the
contractors table's location/lat/lng columns - no OnFleet, no other fields.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from typing import Any, Callable

import sqlalchemy as sa

try:
    from .contractor_sync import (
        _clean_text, _fetch_board_rows, _geocode, _item_to_source,
        normalize_email, normalize_phone,
    )
except ImportError:  # pragma: no cover - run as a plain script
    from contractor_sync import (  # type: ignore
        _clean_text, _fetch_board_rows, _geocode, _item_to_source,
        normalize_email, normalize_phone,
    )

MOVE_THRESHOLD_MILES = 0.25


def _miles(a_lat, a_lng, b_lat, b_lng) -> float | None:
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    r = 3958.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def plan_fixes(
    contractors: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    geocode: Callable[[str], tuple[float | None, float | None]] = _geocode,
) -> list[dict[str, Any]]:
    """Pure planning step: one row per contractor describing what would change."""
    by_email = {s["email"]: s for s in sources if s.get("email")}
    by_phone: dict[str, dict[str, Any]] = {}
    for s in sources:
        ph = normalize_phone(s.get("phone"))
        if ph and len(ph) == 10:
            by_phone.setdefault(ph, s)

    plan = []
    for c in contractors:
        src = by_email.get(normalize_email(c.get("email")) or "")
        if src is None:
            ph = normalize_phone(c.get("phone"))
            src = by_phone.get(ph) if ph else None
        row = {
            "id": c["id"], "name": c.get("name"), "email": c.get("email"),
            "old_location": c.get("location"), "old_lat": c.get("lat"), "old_lng": c.get("lng"),
            "new_location": c.get("location"), "new_lat": c.get("lat"), "new_lng": c.get("lng"),
            "moved_miles": None, "action": "unchanged",
        }
        if src is None:
            row["action"] = "no_monday_match"
            plan.append(row)
            continue

        new_loc = _clean_text(src.get("location")) or c.get("location")
        row["new_location"] = new_loc
        lat, lng = geocode(new_loc) if new_loc else (None, None)
        loc_changed = new_loc != c.get("location")

        if lat is None or lng is None:
            row["new_lat"], row["new_lng"] = None, None
            if loc_changed:
                row["action"] = "address_changed_ungeocodable"
            elif c.get("lat") is not None:
                row["action"] = "coords_unverified"  # old coords fail the strict check
                row["new_lat"], row["new_lng"] = c.get("lat"), c.get("lng")
            else:
                row["action"] = "no_coords"
            plan.append(row)
            continue

        row["new_lat"], row["new_lng"] = lat, lng
        moved = _miles(c.get("lat"), c.get("lng"), lat, lng)
        row["moved_miles"] = round(moved, 2) if moved is not None else None
        if loc_changed:
            row["action"] = "address_changed"
        elif moved is None:
            row["action"] = "coords_filled"
        elif moved > MOVE_THRESHOLD_MILES:
            row["action"] = "coords_moved"
        plan.append(row)
    return plan


WRITE_ACTIONS = {"address_changed", "address_changed_ungeocodable", "coords_filled", "coords_moved"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the fixes (default: dry run)")
    ap.add_argument("--clear-unverified", action="store_true",
                    help="with --apply, also clear coords that no longer pass the strict geocode check")
    ap.add_argument("--csv", default="contractor_address_backfill.csv")
    args = ap.parse_args(argv)

    if not (os.environ.get("MAPBOX_TOKEN") or "").strip():
        print("MAPBOX_TOKEN is not set - refusing to run (every row would look ungeocodable).", file=sys.stderr)
        return 2
    engine = sa.create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)

    mapping, items = _fetch_board_rows()
    print(f"Monday location column -> {mapping.get('location')!r}; {len(items)} board items", file=sys.stderr)
    sources = [_item_to_source(i, mapping) for i in items]

    with engine.connect() as conn:
        contractors = [dict(r) for r in conn.execute(sa.text(
            "SELECT id, name, email, phone, location, lat, lng FROM contractors ORDER BY name"
        )).mappings()]

    plan = plan_fixes(contractors, sources)
    with open(args.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(plan[0].keys()) if plan else ["id"])
        w.writeheader()
        w.writerows(plan)

    counts: dict[str, int] = {}
    for p in plan:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    print("Summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), file=sys.stderr)
    print(f"Full old-vs-new report: {args.csv}", file=sys.stderr)

    todo = [p for p in plan if p["action"] in WRITE_ACTIONS
            or (args.clear_unverified and p["action"] == "coords_unverified")]
    if not args.apply:
        print(f"DRY RUN - {len(todo)} row(s) would be updated. Re-run with --apply to write.", file=sys.stderr)
        return 0

    with engine.begin() as conn:
        for p in todo:
            clear = p["action"] == "coords_unverified"
            conn.execute(sa.text(
                "UPDATE contractors SET location = :loc, lat = :lat, lng = :lng, updated_at = now() WHERE id = :id"
            ), {"id": p["id"], "loc": p["new_location"],
                "lat": None if clear else p["new_lat"], "lng": None if clear else p["new_lng"]})
    print(f"APPLIED - updated {len(todo)} contractor row(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
