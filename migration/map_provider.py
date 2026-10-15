"""Free map provider: US Census geocoding + OpenRouteService routing.

Switched on with the Railway variable MAP_PROVIDER=free (default: mapbox, so
nothing changes until that variable is set). Mapbox code is left untouched.

  Geocoding  : US Census Geocoder (no key, no cap, US street addresses only,
               returns address-level matches only -- never a city centroid).
               Falls back to ORS geocoding, accepting address/venue hits only.
  Routing    : ORS Optimization (VROOM) for stop order, ORS Directions for
               road miles/hours. Public OSRM /trip is the no-key fallback.
  Persistence: geocodes and successful routes are saved in Postgres
               (map_geocode_cache / map_route_cache) so a Railway redeploy no
               longer wipes them and re-spends API quota on every cluster.

Coordinates are (lng, lat) throughout, matching the existing Mapbox helpers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from typing import Any, Callable

import requests

ORS_BASE = "https://api.openrouteservice.org"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
OSRM_BASE = "https://router.project-osrm.org"

# ORS free-tier request limits (documented): Directions 50 waypoints,
# Optimization 50 jobs per request. Stay under both.
ORS_MAX_DIRECTION_POINTS = 50
ORS_MAX_OPT_JOBS = 48
OSRM_MAX_POINTS = 90

US_BOUNDS = (-179.9, -66.0, 18.0, 72.0)  # lng_min, lng_max, lat_min, lat_max
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI "
    "WY PR".split())


class MapProviderError(RuntimeError):
    pass


def provider() -> str:
    return (os.environ.get("MAP_PROVIDER") or "mapbox").strip().lower()


def use_free() -> bool:
    return provider() == "free"


def ors_key() -> str:
    return (os.environ.get("ORS_API_KEY") or "").strip()


def _in_us(lng: float, lat: float) -> bool:
    return (US_BOUNDS[0] <= lng <= US_BOUNDS[1]
            and US_BOUNDS[2] <= lat <= US_BOUNDS[3])


def _query_state_zip(address: str) -> tuple[str | None, str | None]:
    text = str(address or "").upper()
    zip_m = re.search(r"\b(\d{5})(?:-\d{4})?\s*(?:,?\s*(?:USA|US|UNITED STATES))?\s*$", text)
    state_m = re.findall(r"(?:^|[,\s])([A-Z]{2})(?=[\s,]+\d{5}|\s*,?\s*(?:USA|US)?\s*$)", text)
    states = [s for s in state_m if s in US_STATES]
    return (states[-1] if states else None), (zip_m.group(1) if zip_m else None)


# --------------------------------------------------------------------------
# Geocoding
# --------------------------------------------------------------------------

def census_geocode(address: str, request_get: Callable = requests.get,
                   strict: bool = True) -> tuple[float, float] | None:
    """(lng, lat) for a US street address, or None. The Census geocoder only
    returns street-address matches, so a miss is a miss (fails closed). With
    strict=True a result whose state/ZIP contradicts the query is rejected."""
    if not address or not str(address).strip():
        return None
    try:
        body = request_get(CENSUS_URL, params={
            "address": str(address).strip(),
            "benchmark": "Public_AR_Current",
            "format": "json",
        }, timeout=15).json()
    except Exception:
        return None
    matches = ((body or {}).get("result") or {}).get("addressMatches") or []
    q_state, q_zip = _query_state_zip(address)
    for match in matches:
        try:
            lng = float(match["coordinates"]["x"])
            lat = float(match["coordinates"]["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _in_us(lng, lat):
            continue
        if strict:
            comps = match.get("addressComponents") or {}
            r_state = str(comps.get("state") or "").upper() or None
            r_zip = str(comps.get("zip") or "")[:5] or None
            if q_state and r_state and q_state != r_state:
                continue
            if q_zip and r_zip and q_zip != r_zip:
                continue
        return (lng, lat)
    return None


def ors_geocode(address: str, key: str | None = None,
                request_get: Callable = requests.get,
                strict: bool = True) -> tuple[float, float] | None:
    """Fallback for addresses Census can't match (new builds, venue names).
    Only address- or venue-level results are accepted, never street/city."""
    key = key if key is not None else ors_key()
    if not key or not address:
        return None
    try:
        body = request_get(f"{ORS_BASE}/geocode/search", params={
            "api_key": key, "text": str(address).strip(),
            "boundary.country": "US", "size": 3,
        }, timeout=15).json()
    except Exception:
        return None
    q_state, q_zip = _query_state_zip(address)
    for feat in (body or {}).get("features") or []:
        props = feat.get("properties") or {}
        if props.get("layer") not in ("address", "venue"):
            continue
        try:
            if float(props.get("confidence") or 0) < 0.8:
                continue
            lng, lat = (float(v) for v in feat["geometry"]["coordinates"][:2])
        except (KeyError, TypeError, ValueError):
            continue
        if not _in_us(lng, lat):
            continue
        if strict:
            r_state = str(props.get("region_a") or "").upper() or None
            r_zip = str(props.get("postalcode") or "")[:5] or None
            if q_state and r_state and q_state != r_state:
                continue
            if q_zip and r_zip and q_zip != r_zip:
                continue
        return (lng, lat)
    return None


def free_geocode(address: str, request_get: Callable = requests.get,
                 strict: bool = True) -> tuple[float, float] | None:
    return (census_geocode(address, request_get, strict)
            or ors_geocode(address, None, request_get, strict))


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

def _ors_headers(key: str) -> dict[str, str]:
    return {"Authorization": key, "Content-Type": "application/json"}


def _ors_optimize(home, stops, key, request_post) -> list[int]:
    """Visiting order (indices into stops) for one home->stops->home trip."""
    body = {
        "jobs": [{"id": i + 1, "location": list(s)} for i, s in enumerate(stops)],
        "vehicles": [{"id": 1, "profile": "driving-car",
                      "start": list(home), "end": list(home)}],
    }
    resp = request_post(f"{ORS_BASE}/optimization", json=body,
                        headers=_ors_headers(key), timeout=30).json()
    if not isinstance(resp, dict) or resp.get("code") not in (0, None) or resp.get("unassigned"):
        raise MapProviderError(f"ORS optimization failed: {str(resp)[:160]}")
    routes = resp.get("routes") or []
    if len(routes) != 1:
        raise MapProviderError("ORS optimization returned no single route")
    order = [step["job"] - 1 for step in routes[0].get("steps", [])
             if step.get("type") == "job"]
    if sorted(order) != list(range(len(stops))):
        raise MapProviderError("ORS optimization skipped or repeated a stop")
    return order


def _ors_measure(points, key, request_post) -> tuple[float, float]:
    """Road miles and driving hours along points, in <=50-point chunks that
    share an endpoint (so the measured path is continuous)."""
    step = ORS_MAX_DIRECTION_POINTS - 1
    miles = hours = 0.0
    for i in range(0, len(points) - 1, step):
        chunk = points[i:i + ORS_MAX_DIRECTION_POINTS]
        resp = request_post(f"{ORS_BASE}/v2/directions/driving-car",
                            json={"coordinates": [list(p) for p in chunk],
                                  "instructions": False},
                            headers=_ors_headers(key), timeout=30).json()
        try:
            summary = resp["routes"][0]["summary"]
            miles += float(summary.get("distance", 0)) * 0.000621371
            hours += float(summary.get("duration", 0)) / 3600
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MapProviderError(f"ORS directions failed: {str(resp)[:160]}") from exc
    if not (math.isfinite(miles) and math.isfinite(hours)):
        raise MapProviderError("ORS directions returned invalid totals")
    return miles, hours


def _osrm_trip(home, stops, request_get) -> tuple[float, float, list[int]]:
    """No-key fallback: OSRM /trip with home fixed as the start of a round trip."""
    if len(stops) + 1 > OSRM_MAX_POINTS:
        raise MapProviderError("Too many stops for OSRM fallback")
    coords = ";".join(f"{lng},{lat}" for lng, lat in [home, *stops])
    resp = request_get(f"{OSRM_BASE}/trip/v1/driving/{coords}",
                       params={"source": "first", "roundtrip": "true",
                               "overview": "false"}, timeout=30).json()
    if resp.get("code") != "Ok" or not resp.get("trips"):
        raise MapProviderError(f"OSRM trip failed: {resp.get('code')}")
    wps = resp.get("waypoints") or []
    if len(wps) != len(stops) + 1:
        raise MapProviderError("OSRM trip dropped a stop")
    ranks = [wp["waypoint_index"] for wp in wps[1:]]
    order = sorted(range(len(stops)), key=lambda i: ranks[i])
    trip = resp["trips"][0]
    return float(trip["distance"]) * 0.000621371, float(trip["duration"]) / 3600, order


def plan_round_trip_free(home, stops, *, key: str | None = None,
                         request_get: Callable = requests.get,
                         request_post: Callable = requests.post,
                         seed_order: Callable | None = None):
    """Same contract as route_planning.plan_round_trip:
    returns (road_miles, driving_hours, order) for home -> every stop -> home.
    One optimization + one directions call per route (vs. up to ~15 Mapbox
    calls), falling back to OSRM when ORS is unavailable."""
    if not stops:
        return 0.0, 0.0, []
    key = key if key is not None else ors_key()
    errors = []
    if key:
        try:
            if len(stops) <= ORS_MAX_OPT_JOBS:
                order = _ors_optimize(home, stops, key, request_post)
            elif seed_order is not None:
                order = list(seed_order(home, stops))
            else:
                raise MapProviderError("Too many stops for ORS optimization")
            pts = [home, *[stops[i] for i in order], home]
            miles, hours = _ors_measure(pts, key, request_post)
            return miles, hours, order
        except Exception as exc:
            errors.append(f"ORS: {exc}")
            print(f"[map_provider] ORS fallback to OSRM: {exc}", flush=True)
    try:
        return _osrm_trip(home, stops, request_get)
    except Exception as exc:
        errors.append(f"OSRM: {exc}")
    raise MapProviderError("; ".join(errors) or "No routing provider available")


# --------------------------------------------------------------------------
# Persistent cache (Postgres) -- survives Railway redeploys
# --------------------------------------------------------------------------

_DDL = (
    "CREATE TABLE IF NOT EXISTS map_geocode_cache ("
    " address TEXT PRIMARY KEY, lng DOUBLE PRECISION NOT NULL,"
    " lat DOUBLE PRECISION NOT NULL, source TEXT,"
    " updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS map_route_cache ("
    " route_key TEXT PRIMARY KEY, result TEXT NOT NULL,"
    " saved_at DOUBLE PRECISION NOT NULL)",
)


class PersistentMapCache:
    """Write-through Postgres store. Every method swallows DB errors: the
    cache must never be the reason a route fails."""

    def __init__(self, engine):
        self.engine = engine
        self.url = str(engine.url) if engine is not None else None
        self.ready = False
        if engine is None:
            return
        try:
            import sqlalchemy as sa
            with engine.begin() as conn:
                for stmt in _DDL:
                    conn.execute(sa.text(stmt))
            self.ready = True
        except Exception as exc:
            print(f"[map_provider] persistent cache disabled: {exc}", flush=True)

    def load_geocodes(self) -> dict[str, tuple[float, float]]:
        if not self.ready:
            return {}
        try:
            import sqlalchemy as sa
            with self.engine.connect() as conn:
                rows = conn.execute(sa.text(
                    "SELECT address, lng, lat FROM map_geocode_cache")).fetchall()
            return {r[0]: (float(r[1]), float(r[2])) for r in rows}
        except Exception as exc:
            print(f"[map_provider] load_geocodes: {exc}", flush=True)
            return {}

    def save_geocode(self, address: str, coords, source: str) -> None:
        if not self.ready or not coords:
            return
        try:
            import sqlalchemy as sa
            with self.engine.begin() as conn:
                conn.execute(sa.text(
                    "INSERT INTO map_geocode_cache (address, lng, lat, source)"
                    " VALUES (:a, :lng, :lat, :s) ON CONFLICT (address) DO UPDATE"
                    " SET lng = EXCLUDED.lng, lat = EXCLUDED.lat,"
                    " source = EXCLUDED.source, updated_at = CURRENT_TIMESTAMP"),
                    {"a": address, "lng": coords[0], "lat": coords[1], "s": source})
        except Exception as exc:
            print(f"[map_provider] save_geocode: {exc}", flush=True)

    @staticmethod
    def route_key(key: Any) -> str:
        return hashlib.sha256(repr(key).encode()).hexdigest()

    def get_route(self, key: Any, max_age: float = 86400):
        if not self.ready:
            return None
        try:
            import sqlalchemy as sa
            with self.engine.connect() as conn:
                row = conn.execute(sa.text(
                    "SELECT result, saved_at FROM map_route_cache WHERE route_key = :k"),
                    {"k": self.route_key(key)}).fetchone()
            if row and time.time() - float(row[1]) < max_age:
                return tuple(json.loads(row[0])), float(row[1])
        except Exception as exc:
            print(f"[map_provider] get_route: {exc}", flush=True)
        return None

    def save_route(self, key: Any, result, saved_at: float) -> None:
        if not self.ready:
            return
        try:
            import sqlalchemy as sa
            with self.engine.begin() as conn:
                conn.execute(sa.text(
                    "INSERT INTO map_route_cache (route_key, result, saved_at)"
                    " VALUES (:k, :r, :t) ON CONFLICT (route_key) DO UPDATE"
                    " SET result = EXCLUDED.result, saved_at = EXCLUDED.saved_at"),
                    {"k": self.route_key(key), "r": json.dumps(list(result)), "t": saved_at})
                conn.execute(sa.text(
                    "DELETE FROM map_route_cache WHERE saved_at < :old"),
                    {"old": saved_at - 7 * 86400})
        except Exception as exc:
            print(f"[map_provider] save_route: {exc}", flush=True)


_PERSIST_SINGLETON: PersistentMapCache | None = None
_PERSIST_LOCK = threading.Lock()


def get_persistent_cache(engine) -> PersistentMapCache:
    """One PersistentMapCache per database per process. Streamlit re-executes
    the main script (and re-creates its engine) on every rerun, so key on the
    database URL rather than the engine object."""
    global _PERSIST_SINGLETON
    url = str(engine.url) if engine is not None else None
    cur = _PERSIST_SINGLETON
    if cur is None or cur.url != url:
        with _PERSIST_LOCK:
            cur = _PERSIST_SINGLETON
            if cur is None or cur.url != url:
                _PERSIST_SINGLETON = cur = PersistentMapCache(engine)
    return cur
