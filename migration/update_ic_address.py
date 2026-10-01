"""Deliberately update ONE existing IC's address (the only sanctioned way).

The current IC list was corrected on 2026-09-30 and syncs no longer touch
existing ICs' addresses. When a specific IC moves, update just that IC:

    python -m migration.update_ic_address --email jane@x.com \
        --address "123 Main St, Fresno, CA 93721, USA"            # dry run
    python -m migration.update_ic_address --email jane@x.com \
        --address "123 Main St, Fresno, CA 93721, USA" --apply

Optional --lat/--lng (e.g. Monday's map pin); otherwise a strict Mapbox
geocode is used (needs MAPBOX_TOKEN). Pod is re-derived from the new state.
Only this one row's location / lat / lng / pod_color change.
"""
from __future__ import annotations

import argparse
import os
import sys

import sqlalchemy as sa

try:
    from .contractor_sync import _geocode, normalize_email, resolved_pod, state_from_location
except ImportError:  # pragma: no cover
    from contractor_sync import _geocode, normalize_email, resolved_pod, state_from_location  # type: ignore


def plan_update(row: dict, address: str, lat=None, lng=None, geocode=_geocode) -> dict:
    address = " ".join((address or "").split())
    if not state_from_location(address):
        raise ValueError("Address must include a U.S. state (full address, e.g. '123 Main St, Fresno, CA 93721').")
    if lat is None or lng is None:
        lat, lng = geocode(address)
    return {"location": address, "lat": lat, "lng": lng,
            "pod_color": resolved_pod(address, row.get("pod_color"))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True)
    ap.add_argument("--address", required=True)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lng", type=float)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    email = normalize_email(a.email)
    eng = sa.create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with eng.connect() as c:
        rows = [dict(r) for r in c.execute(sa.text(
            "SELECT id, name, email, location, lat, lng, pod_color FROM contractors WHERE email = :e"),
            {"e": email}).mappings()]
    if len(rows) != 1:
        print(f"Expected exactly one IC with email {email}, found {len(rows)}. Nothing changed.", file=sys.stderr)
        return 2
    row = rows[0]
    try:
        new = plan_update(row, a.address, a.lat, a.lng)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"{row['name']} <{row['email']}>")
    for k in ("location", "lat", "lng", "pod_color"):
        print(f"  {k}: {row.get(k)!r} -> {new[k]!r}")
    if new["lat"] is None:
        print("  WARNING: no coordinates (pass --lat/--lng or check the address).")
    if not a.apply:
        print("DRY RUN - re-run with --apply to save.")
        return 0
    with eng.begin() as c:
        c.execute(sa.text("UPDATE contractors SET location=:location, lat=:lat, lng=:lng, "
                          "pod_color=:pod_color, updated_at=now() WHERE id=:id"), {**new, "id": row["id"]})
    print("SAVED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
