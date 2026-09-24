from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import sqlalchemy as sa

BOARD_ID = int(os.environ.get("MONDAY_CONTRACTOR_BOARD_ID", "5840676529"))
MONDAY_API_URL = "https://api.monday.com/v2"

COLUMN_ALIASES = {
    "email": {"email", "email address", "e-mail", "e mail"},
    "name": {"name", "contractor", "contractor name", "ic", "ic name", "independent contractor"},
    "phone": {"phone", "phone number", "mobile", "cell", "cell phone"},
    "location": {"location", "address", "home location", "service area", "city state", "city/state"},
    "ic_list": {"ic list", "ic_list", "list", "contractor list"},
    "pod_color": {"pod color", "pod", "pod_color", "pod colour"},
    "digital_certified": {"digital certified", "digital certification", "digital_certified", "digital cert"},
    "unrestricted": {"unrestricted", "unrestricted ic", "full access"},
    "ic_status": {"ic status", "status", "contractor status"},
    "inactive_reason": {"reason for inactive status", "inactive reason", "reason inactive"},
}
TRUE_VALUES = {"yes", "y", "true", "1", "checked"}
FALSE_VALUES = {"no", "n", "false", "0", "unchecked"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


ONFLEET_API_URL = "https://onfleet.com/api/v2"


def _onfleet_headers() -> dict[str, str] | None:
    key = (os.environ.get("ONFLEET_KEY") or "").strip()
    if not key:
        return None
    token = base64.b64encode(f"{key}:".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def _onfleet_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    headers = _onfleet_headers()
    if not headers:
        raise RuntimeError("ONFLEET_KEY is not configured.")
    resp = requests.request(
        method,
        ONFLEET_API_URL + path,
        headers=headers,
        timeout=20,
        **kwargs,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OnFleet {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
    return resp


def _onfleet_list_workers() -> list[dict[str, Any]]:
    workers: list[dict[str, Any]] = []
    last_id = None
    seen: set[str] = set()
    for _ in range(50):
        path = "/workers" + (f"?lastId={last_id}" if last_id else "")
        payload = _onfleet_request("GET", path).json()
        page = payload if isinstance(payload, list) else (payload.get("workers") or [])
        if not page:
            break
        new_count = 0
        for worker in page:
            wid = str(worker.get("id") or "")
            if wid and wid not in seen:
                seen.add(wid)
                workers.append(worker)
                new_count += 1
        if new_count == 0:
            break
        last_id = page[-1].get("id")
        if not last_id:
            break
    return workers


def _onfleet_sync_new_contractor(source: dict[str, Any]) -> dict[str, Any]:
    """Create a new Monday contractor as an OnFleet worker and add Pod team.

    This is intentionally called only for contractors newly inserted into DCC.
    Existing OnFleet workers are matched by normalized phone first, then email,
    making retries idempotent instead of creating duplicate drivers.
    """
    pod = _clean_text(source.get("pod_color"))
    if not pod:
        return {"status": "skipped", "reason": "missing pod color"}

    pod_norm = _norm_title(pod)
    expected_team = f"pod: {pod_norm}"
    teams_payload = _onfleet_request("GET", "/teams").json()
    teams = teams_payload if isinstance(teams_payload, list) else (teams_payload.get("teams") or [])
    team = next(
        (t for t in teams if _norm_title(t.get("name")) == expected_team),
        None,
    )
    if not team:
        return {"status": "failed", "reason": f"OnFleet team POD: {pod} not found"}

    email = normalize_email(source.get("email"))
    phone = normalize_phone(source.get("phone"))
    workers = _onfleet_list_workers()
    worker = None
    for candidate in workers:
        c_phone = normalize_phone(candidate.get("phone"))
        c_email = normalize_email(candidate.get("email"))
        if (phone and c_phone == phone) or (email and c_email == email):
            worker = candidate
            break

    if worker is None:
        if not phone:
            return {"status": "failed", "reason": "valid phone required to create OnFleet driver"}
        payload = {
            "name": _clean_text(source.get("name")),
            "phone": "+1" + phone if len(phone) == 10 else phone,
            "teams": [team.get("id")],
        }
        if email:
            payload["email"] = email
        address = _clean_text(source.get("location"))
        if address:
            payload["metadata"] = [
                {"name": "Address", "type": "string", "value": address}
            ]
        worker = _onfleet_request("POST", "/workers", json=payload).json()
        return {
            "status": "created",
            "worker_id": worker.get("id"),
            "team": team.get("name"),
        }

    worker_id = worker.get("id")
    if not worker_id:
        return {"status": "failed", "reason": "matched OnFleet driver has no id"}

    existing_team_ids = set(worker.get("teams") or [])
    target_team_id = team.get("id")
    update_payload: dict[str, Any] = {}
    if target_team_id and target_team_id not in existing_team_ids:
        existing_team_ids.add(target_team_id)
        update_payload["teams"] = list(existing_team_ids)

    address = _clean_text(source.get("location"))
    if address:
        existing_metadata = [
            m for m in (worker.get("metadata") or [])
            if _norm_title(m.get("name")) != "address"
        ]
        existing_metadata.append({"name": "Address", "type": "string", "value": address})
        update_payload["metadata"] = existing_metadata

    if update_payload:
        _onfleet_request("PUT", f"/workers/{worker_id}", json=update_payload)
        return {"status": "updated", "worker_id": worker_id, "team": team.get("name"), "address_added": bool(address)}

    return {"status": "already_present", "worker_id": worker_id, "team": team.get("name")}


def _norm_title(value: Any) -> str:
    s = str(value or "").strip().lower().replace("_", " ")
    # Monday boards often prefix required columns with "*" (for example
    # "*email", "*phone", "*location"). Treat that as display decoration,
    # not part of the semantic column title.
    s = re.sub(r"^[^a-z0-9]+", "", s)
    return re.sub(r"\s+", " ", s)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def normalize_email(value: Any) -> str | None:
    s = _clean_text(value)
    if not s:
        return None
    s = s.lower()
    return s if EMAIL_RE.match(s) else None


def normalize_phone(value: Any) -> str | None:
    s = _clean_text(value)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def parse_bool(value: Any) -> bool | None:
    s = _norm_title(value)
    if s in TRUE_VALUES:
        return True
    if s in FALSE_VALUES:
        return False
    return None


def _preserve_blank(existing: Any, incoming: Any) -> Any:
    if isinstance(incoming, str):
        return incoming.strip() or existing
    return existing if incoming is None else incoming


def _monday_request(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    token = (os.environ.get("MONDAY_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("MONDAY_API_TOKEN is not configured.")
    resp = requests.post(
        MONDAY_API_URL,
        headers={"Authorization": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError("Monday API error: " + "; ".join(str(e.get("message") or e) for e in payload["errors"]))
    return payload["data"]


def _discover_mapping(columns: list[dict[str, Any]]) -> dict[str, str]:
    by_title = {_norm_title(c.get("title")): str(c.get("id")) for c in columns}
    mapping: dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        matches = [by_title[a] for a in aliases if a in by_title]
        if matches:
            mapping[field] = matches[0]
    missing = [f for f in ("email",) if f not in mapping]
    if missing:
        raise RuntimeError(
            "Required Monday contractor columns could not be mapped by title: "
            + ", ".join(missing)
            + ". Available titles: "
            + ", ".join(sorted(by_title))
        )
    return mapping


def _fetch_board_rows() -> tuple[dict[str, str], list[dict[str, Any]]]:
    meta_q = """
    query($board:[ID!]!) {
      boards(ids:$board) {
        columns { id title type }
        items_page(limit:500) {
          cursor
          items { id name updated_at column_values { id text value } }
        }
      }
    }
    """
    data = _monday_request(meta_q, {"board": [BOARD_ID]})
    boards = data.get("boards") or []
    if not boards:
        raise RuntimeError(f"Monday board {BOARD_ID} was not found.")
    board = boards[0]
    mapping = _discover_mapping(board.get("columns") or [])
    page = board.get("items_page") or {}
    items = list(page.get("items") or [])
    cursor = page.get("cursor")
    next_q = """
    query($cursor:String!) {
      next_items_page(limit:500, cursor:$cursor) {
        cursor
        items { id name updated_at column_values { id text value } }
      }
    }
    """
    while cursor:
        nxt = _monday_request(next_q, {"cursor": cursor}).get("next_items_page") or {}
        items.extend(nxt.get("items") or [])
        cursor = nxt.get("cursor")
    return mapping, items


def _item_to_source(item: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    vals = {str(v.get("id")): v for v in item.get("column_values") or []}
    def txt(field: str) -> str | None:
        col_id = mapping.get(field)
        if not col_id:
            return None
        v = vals.get(col_id) or {}
        return _clean_text(v.get("text"))
    source = {
        "monday_item_id": str(item.get("id") or ""),
        "monday_updated_at": item.get("updated_at"),
        "email": normalize_email(txt("email")),
        "name": txt("name") or _clean_text(item.get("name")),
        "phone": txt("phone"),
        "location": txt("location"),
        "ic_list": txt("ic_list"),
        "pod_color": txt("pod_color"),
        "digital_certified": parse_bool(txt("digital_certified")),
        "unrestricted": parse_bool(txt("unrestricted")),
        "ic_status": txt("ic_status"),
        "inactive_reason": txt("inactive_reason"),
    }
    availability = _availability_class(source)
    if availability:
        source["ic_list"] = availability
    return source


def _availability_class(source: dict[str, Any], insurance_window_days: int = 90) -> str | None:
    status = _norm_title(source.get("ic_status"))
    reason = _norm_title(source.get("inactive_reason"))

    if status == "active":
        return "ACTIVE"
    if status in {"new", "in training", "training"}:
        return "IN TRAINING"

    raw = source.get("monday_updated_at")
    recent = False
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        recent = dt >= datetime.now(timezone.utc) - timedelta(days=insurance_window_days)
    except Exception:
        recent = False

    # Monday has used several insurance-related labels over time. Treat any
    # recent insurance hold as route-eligible NEED INSURANCE whether the word
    # insurance appears in the status itself or in the inactive-reason field.
    insurance_related = ("insur" in status) or ("insur" in reason)
    inactive_related = (
        status == "inactive"
        or "inactive" in status
        or insurance_related
    )
    if inactive_related:
        if insurance_related and recent:
            return "NEED INSURANCE"
        return "INACTIVE"

    # Unknown/missing status is intentionally not considered route-eligible.
    return None


def _extract_state_zip_from_query(location: str) -> tuple[str | None, str | None]:
    text = str(location or "").upper()
    state = None
    zip_code = None

    # Prefer explicit USPS abbreviation near a ZIP / comma boundary.
    m_state = re.search(r"(?:,|\\s)\\s*([A-Z]{2})(?:\\s|,|$)", text)
    if m_state:
        state = m_state.group(1)

    m_zip = re.search(r"\\b(\\d{5})(?:-\\d{4})?\\b", text)
    if m_zip:
        zip_code = m_zip.group(1)

    return state, zip_code


def _mapbox_result_state_zip(feature: dict[str, Any]) -> tuple[str | None, str | None]:
    state = None
    zip_code = None

    # State/postcode may appear either as the feature itself or in context.
    parts = [feature] + list(feature.get("context") or [])
    for part in parts:
        pid = str(part.get("id") or "")
        if pid.startswith("region."):
            short = str((part.get("properties") or {}).get("short_code") or part.get("short_code") or "").upper()
            if short.startswith("US-") and len(short) >= 5:
                state = short[-2:]
            elif len(short) == 2:
                state = short
        elif pid.startswith("postcode."):
            txt = str(part.get("text") or "").strip()
            m = re.search(r"\\b(\\d{5})\\b", txt)
            if m:
                zip_code = m.group(1)

    return state, zip_code


def _mapbox_match_is_acceptable(location: str, feature: dict[str, Any]) -> bool:
    try:
        relevance = float(feature.get("relevance", 0) or 0)
    except Exception:
        relevance = 0.0

    # Reject weak/fuzzy first results. A complete street address should be
    # essentially exact; city/state-only locations can be slightly less exact.
    has_street_number = bool(re.search(r"\\b\\d{1,6}\\b", str(location or "")))
    min_relevance = 0.90 if has_street_number else 0.80
    if relevance < min_relevance:
        return False

    place_types = {str(x).lower() for x in (feature.get("place_type") or [])}
    if has_street_number and not (
        "address" in place_types
        or _clean_text(feature.get("address"))
    ):
        return False

    query_state, query_zip = _extract_state_zip_from_query(location)
    result_state, result_zip = _mapbox_result_state_zip(feature)

    # If Monday gives us a state/ZIP, do not accept a result that contradicts it.
    if query_state and result_state and query_state != result_state:
        return False
    if query_zip and result_zip and query_zip != result_zip:
        return False

    center = feature.get("center") or []
    if len(center) != 2:
        return False
    try:
        lng, lat = float(center[0]), float(center[1])
    except Exception:
        return False

    # U.S. sanity bounds, including Alaska/Hawaii. This mainly guards corrupt
    # API payloads / coordinate-order mistakes rather than doing state policing.
    if not (-179.9 <= lng <= -66.0 and 18.0 <= lat <= 72.0):
        return False

    return True


def _geocode(location: str | None) -> tuple[float | None, float | None]:
    token = (os.environ.get("MAPBOX_TOKEN") or "").strip()
    if not token or not location:
        return None, None
    try:
        resp = requests.get(
            "https://api.mapbox.com/geocoding/v5/mapbox.places/"
            + requests.utils.quote(location, safe="")
            + ".json",
            params={
                "access_token": token,
                "limit": 3,
                "country": "US",
                "autocomplete": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
        features = (resp.json() or {}).get("features") or []
        for feature in features:
            if not _mapbox_match_is_acceptable(location, feature):
                continue
            center = feature.get("center") or []
            return float(center[1]), float(center[0])
        # Fail closed: do not save coordinates for an ambiguous/weak match.
        return None, None
    except Exception:
        return None, None


def _build_update(existing: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in ("name", "phone", "location", "ic_list", "pod_color"):
        incoming = _clean_text(source.get(field))
        if incoming and incoming != existing.get(field):
            out[field] = incoming
    for field in ("digital_certified", "unrestricted"):
        incoming = source.get(field)
        if incoming is not None and incoming != existing.get(field):
            out[field] = bool(incoming)
    return out


def _recent_source(source: dict[str, Any], lookback_hours: int) -> bool:
    raw = source.get("monday_updated_at")
    if not raw:
        return True
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        return dt >= cutoff
    except Exception:
        # If Monday changes timestamp formatting, fail open rather than miss an IC.
        return True


def sync_contractors_from_monday(engine: sa.Engine | None = None) -> dict[str, Any]:
    if engine is None:
        db_url = (os.environ.get("DATABASE_URL") or "").strip()
        if not db_url:
            raise RuntimeError("DATABASE_URL is not configured.")
        engine = sa.create_engine(db_url, pool_pre_ping=True)

    mapping, items = _fetch_board_rows()
    all_sources = [_item_to_source(item, mapping) for item in items]
    lookback_hours = max(1, int(os.environ.get("MONDAY_SYNC_LOOKBACK_HOURS", "48")))
    sources = [src for src in all_sources if _recent_source(src, lookback_hours)]
    if not all_sources:
        raise RuntimeError("Monday contractor board returned no items; no database changes were made.")

    result = {
        "checked": len(sources),
        "skipped_old": max(0, len(all_sources) - len(sources)),
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "needs_review": 0,
        "failed": 0,
        "onfleet_created": 0,
        "onfleet_updated": 0,
        "onfleet_failed": 0,
        "details": [],
        "mapped_columns": sorted(mapping),
    }

    with engine.begin() as conn:
        existing_rows = [
            dict(r)
            for r in conn.execute(
                sa.text(
                    """
                    SELECT id,email,name,location,phone,ic_list,lat,lng,pod_color,
                           digital_certified,unrestricted
                    FROM contractors
                    """
                )
            ).mappings().all()
        ]
        by_email = {normalize_email(r.get("email")): r for r in existing_rows if normalize_email(r.get("email"))}
        by_name_phone: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in existing_rows:
            key = (_norm_title(row.get("name")), normalize_phone(row.get("phone")) or "")
            if key[0] and key[1]:
                by_name_phone.setdefault(key, []).append(row)

        for source in sources:
            email = source.get("email")
            name = _clean_text(source.get("name"))
            if not email or not name:
                result["failed"] += 1
                result["details"].append({
                    "item_id": source.get("monday_item_id"),
                    "status": "failed",
                    "reason": "missing or invalid email/name",
                })
                continue

            existing = by_email.get(email)
            if existing is None:
                np_key = (_norm_title(name), normalize_phone(source.get("phone")) or "")
                candidates = by_name_phone.get(np_key, []) if np_key[1] else []
                if candidates and all(normalize_email(c.get("email")) != email for c in candidates):
                    result["needs_review"] += 1
                    result["details"].append({
                        "item_id": source.get("monday_item_id"),
                        "email": email,
                        "name": name,
                        "status": "needs_review",
                        "reason": "name+phone matches an existing contractor with a different email",
                    })
                    continue

                lat, lng = _geocode(source.get("location"))
                row = {
                    "email": email,
                    "name": name,
                    "location": _clean_text(source.get("location")),
                    "phone": _clean_text(source.get("phone")),
                    "ic_list": _clean_text(source.get("ic_list")),
                    "lat": lat,
                    "lng": lng,
                    "pod_color": _clean_text(source.get("pod_color")),
                    "digital_certified": bool(source.get("digital_certified")) if source.get("digital_certified") is not None else False,
                    "unrestricted": bool(source.get("unrestricted")) if source.get("unrestricted") is not None else False,
                }
                insert_res = conn.execute(
                    sa.text(
                        """
                        INSERT INTO contractors
                          (email,name,location,phone,ic_list,lat,lng,pod_color,digital_certified,unrestricted)
                        VALUES
                          (:email,:name,:location,:phone,:ic_list,:lat,:lng,:pod_color,:digital_certified,:unrestricted)
                        ON CONFLICT (email) DO NOTHING
                        """
                    ),
                    row,
                )
                if insert_res.rowcount:
                    result["added"] += 1
                    detail = {"email": email, "name": name, "status": "added"}
                    try:
                        of_result = _onfleet_sync_new_contractor(source)
                        detail["onfleet"] = of_result
                        if of_result.get("status") == "created":
                            result["onfleet_created"] += 1
                        elif of_result.get("status") == "updated":
                            result["onfleet_updated"] += 1
                        elif of_result.get("status") == "failed":
                            result["onfleet_failed"] += 1
                    except Exception as exc:
                        result["onfleet_failed"] += 1
                        detail["onfleet"] = {"status": "failed", "reason": str(exc)}
                    result["details"].append(detail)
                else:
                    result["unchanged"] += 1
                continue

            updates = _build_update(existing, source)
            if "location" in updates or (
                _clean_text(source.get("location"))
                and (existing.get("lat") is None or existing.get("lng") is None)
            ):
                lat, lng = _geocode(updates.get("location") or source.get("location"))
                if lat is not None and lng is not None:
                    updates["lat"] = lat
                    updates["lng"] = lng
            if not updates:
                result["unchanged"] += 1
                continue

            sets = ", ".join(f"{k} = :{k}" for k in updates)
            params = dict(updates)
            params["id"] = existing["id"]
            conn.execute(
                sa.text(f"UPDATE contractors SET {sets}, updated_at = now() WHERE id = :id"),
                params,
            )
            result["updated"] += 1
            result["details"].append({
                "email": email,
                "name": name,
                "status": "updated",
                "fields": sorted(updates),
            })

    return result


def main() -> None:
    try:
        result = sync_contractors_from_monday()
        print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
