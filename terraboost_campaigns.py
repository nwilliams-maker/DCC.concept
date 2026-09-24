"""
terraboost_campaigns.py
=======================
READ-ONLY lookup against manage.terraboost.com's GraphQL API to populate
the SIO + art-file URL fields on the packing slip.

Endpoint: POST https://be-terraboost-v3.terraboost.com/graphql
Auth:     login(input: {email, password}) → token, then `Authorization: Bearer <token>`

Safety contract (enforced in code):
- ONE auth mutation is hardcoded (`_LOGIN_MUTATION`) — used only to get a token.
- All data calls go through `_query()`, which REFUSES any operation that doesn't
  start with the literal token "query " — making campaign edits, deletes,
  and art-file changes physically impossible from this module.

Public API:
    fetch_campaign_index(kids_tuple: tuple[str, ...]) -> dict[str, dict]

    Returns: {KID → {
        # campaign / art
        "sio", "campaign_id", "campaign_name",
        "top_file_url", "bottom_file_url",
        "collection_name", "collection_label",
        # venue
        "venue_name", "venue_address", "venue_state",
        # kiosk
        "kiosk_loc", "kiosk_type", "is_digital",
        # placement / boost
        "boosted", "ad_placement",
    }}
    For each KID, picks the campaignKiosk where today is within reservation dates.
    Cached for 1 hour via @st.cache_data so DCC doesn't hammer Terraboost.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime
from typing import Iterable

import requests

try:
    import streamlit as st
    _HAS_STREAMLIT = True
except ImportError:
    _HAS_STREAMLIT = False

GQL_URL = "https://be-terraboost-v3.terraboost.com/graphql"
HTTP_TIMEOUT = 20


# -- Hardcoded auth mutation --------------------------------------------------
# This is the ONLY mutation this module ever sends. It does not write business
# data — it exchanges credentials for a session token.
_LOGIN_MUTATION = """mutation Login($email: String!, $password: String!) {
  login(input: {email: $email, password: $password}) {
    token
    refreshToken
  }
}"""


# -- Read-only data query -----------------------------------------------------
_KIDS_LOOKUP_QUERY = """query KidLookup($kids: [String!]) {
  kiosks(where: {importKioskId: {in: $kids}}) {
    importKioskId
    venueId
    kioskLocationId
    isDigital
    kioskLocation { id typeName }
    kioskType { id typeName }
    venue {
      id
      venueName
      address1
      address2
      city
      state
      zip
    }
    campaignKiosks {
      reservationStart
      reservationEnd
      boosted
      kioskAdPlacement { typeName }
      printCollection {
        id
        collectionName
        topFileUrl
        bottomFileUrl
      }
      campaign {
        id
        name
        orderNumber
        statusId
      }
    }
  }
}"""


# -- Token cache (process-wide, shared across Streamlit reruns) ---------------
if _HAS_STREAMLIT:
    @st.cache_resource(show_spinner=False)
    def _token_cache() -> dict:
        return {"token": None, "issued_ts": 0.0}
else:
    _LOCAL_CACHE = {"token": None, "issued_ts": 0.0}
    def _token_cache() -> dict:
        return _LOCAL_CACHE


def _login() -> str | None:
    """
    Calls the login mutation with credentials from env vars.
    Returns a bearer token string or None on failure.
    """
    email = os.environ.get("TERRABOOST_EMAIL")
    password = os.environ.get("TERRABOOST_PASSWORD")
    if not email or not password:
        print("[terraboost_campaigns._login] TERRABOOST_EMAIL/TERRABOOST_PASSWORD "
              "not set — cannot authenticate.", file=sys.stderr, flush=True)
        return None
    try:
        r = requests.post(
            GQL_URL,
            headers={"content-type": "application/json"},
            json={"query": _LOGIN_MUTATION, "variables": {"email": email, "password": password}},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[terraboost_campaigns._login] login HTTP {r.status_code}",
                  file=sys.stderr, flush=True)
            return None
        body = r.json()
        if body.get("errors"):
            print(f"[terraboost_campaigns._login] login GraphQL errors: "
                  f"{body.get('errors')}", file=sys.stderr, flush=True)
            return None
        return ((body.get("data") or {}).get("login") or {}).get("token")
    except Exception as e:
        print(f"[terraboost_campaigns._login] {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return None


def _get_token() -> str | None:
    """
    Returns a cached token, re-logging in every ~50 minutes (tokens are
    typically valid for 1hr, so we refresh slightly before expiry).
    """
    cache = _token_cache()
    if not cache.get("token") or (time.time() - cache.get("issued_ts", 0)) > 3000:
        new_token = _login()
        if new_token:
            cache["token"] = new_token
            cache["issued_ts"] = time.time()
    return cache.get("token")


def _query(query_str: str, variables: dict | None = None) -> dict:
    """
    READ-ONLY GraphQL call. Asserts the operation is a `query` block —
    refuses anything else. This is the safety net.
    """
    stripped = query_str.lstrip()
    if not stripped.startswith("query"):
        raise RuntimeError(
            "terraboost_campaigns is read-only — only `query` blocks allowed. "
            f"Got: {stripped[:40]!r}"
        )
    token = _get_token()
    if not token:
        print("[terraboost_campaigns._query] no auth token — returning empty result.",
              file=sys.stderr, flush=True)
        return {}
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
    }
    try:
        r = requests.post(
            GQL_URL,
            headers=headers,
            json={"query": query_str, "variables": variables or {}},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 401:
            # Token expired — wipe cache, re-login once, retry
            cache = _token_cache()
            cache["token"] = None
            cache["issued_ts"] = 0.0
            token = _get_token()
            if not token:
                print("[terraboost_campaigns._query] re-login after 401 failed.",
                      file=sys.stderr, flush=True)
                return {}
            headers["authorization"] = f"Bearer {token}"
            r = requests.post(GQL_URL, headers=headers,
                              json={"query": query_str, "variables": variables or {}},
                              timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            print(f"[terraboost_campaigns._query] HTTP {r.status_code}",
                  file=sys.stderr, flush=True)
            return {}
        body = r.json()
        if isinstance(body, dict) and body.get("errors"):
            # GraphQL errors arrive inside an HTTP 200 — surface them, but still
            # return the body so any partial `data` remains usable downstream.
            print(f"[terraboost_campaigns._query] GraphQL errors: "
                  f"{body.get('errors')}", file=sys.stderr, flush=True)
        return body
    except Exception as e:
        print(f"[terraboost_campaigns._query] {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return {}


import re as _re
import os as _os
import urllib.parse as _urlparse

_UUID_PREFIX_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-",
    _re.IGNORECASE,
)
_EXT_RE = _re.compile(r"\.[a-z0-9]+$", _re.IGNORECASE)
_TB_SUFFIX_RE = _re.compile(r"_[TB]$", _re.IGNORECASE)


def _extract_collection_label(url: str) -> str:
    """Turn an Azure blob URL into the clean collection identifier.

    Example:
        https://.../storage/52/655f20b6-...-Alta_Dena_April_2026_T.pdf
        -> "Alta_Dena_April_2026"
    """
    if not url:
        return ""
    try:
        path = _urlparse.urlparse(url).path
    except Exception:
        return ""
    name = _os.path.basename(path)
    # Strip leading UUID-dash, the file extension, and the trailing _T or _B.
    name = _UUID_PREFIX_RE.sub("", name)
    name = _EXT_RE.sub("", name)
    name = _TB_SUFFIX_RE.sub("", name)
    return name.strip()


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _pick_active(campaign_kiosks: list, today: date) -> dict | None:
    """Return the most-relevant campaignKiosk for this kiosk.

    L13 (QA audit 2026-05-21) — BY DESIGN: this function intentionally
    IGNORES `reservationEnd`. It never checks whether `today` falls before
    the reservation end date, so a kiosk between campaigns will surface its
    most recent (possibly expired) campaign rather than nothing. This matches
    the dispatcher spec below ("whatever the portal shows as latest"). Do not
    add an end-date guard without product-owner sign-off.

    Per dispatcher spec: no date filtering — whatever the Terraboost portal
    shows as the kiosk's current/latest assignment is what the packing slip
    pulls. Tie-breakers when multiple entries share the same reservationStart:

      1. Latest reservationStart wins.
      2. Entry with a populated printCollection beats one without (a real
         campaign with art beats a RESERVED placeholder dupe).
      3. Campaign name NOT starting with "RESERVED" beats one that does.

    This handles the common Terraboost data pattern where a kiosk has both
    "Scorch LLC - ..." (real, with art) and "RESERVED - Scorch LLC - ..."
    (placeholder, no art) on the same start date.
    """
    def _score(ck: dict) -> tuple:
        start = _parse_date(ck.get("reservationStart")) or date(1900, 1, 1)
        has_collection = bool((ck.get("printCollection") or {}).get("collectionName"))
        camp_name = ((ck.get("campaign") or {}).get("name") or "").strip()
        is_reserved = camp_name.upper().startswith("RESERVED")
        # Higher tuple sorts later (we pick max). Tie-breakers in priority order:
        #   start date, has-collection, NOT reserved
        return (start, 1 if has_collection else 0, 0 if is_reserved else 1)

    best = None
    best_score = None
    for ck in campaign_kiosks or []:
        if not _parse_date(ck.get("reservationStart")):
            continue
        s = _score(ck)
        if best_score is None or s > best_score:
            best = ck
            best_score = s
    return best


def _do_fetch(kids: tuple) -> dict:
    """Inner fetch — pulled out so we can wrap it in @st.cache_data conditionally."""
    if not kids:
        return {}
    # Dedupe + drop blanks
    clean_kids = sorted({(k or "").strip() for k in kids if k and str(k).strip()})
    if not clean_kids:
        return {}
    resp = _query(_KIDS_LOOKUP_QUERY, {"kids": clean_kids})
    out: dict = {}
    today = date.today()
    for k in (resp.get("data") or {}).get("kiosks") or []:
        kid = k.get("importKioskId") or ""
        if not kid:
            continue
        active = _pick_active(k.get("campaignKiosks") or [], today)
        if not active:
            continue
        pc = active.get("printCollection") or {}
        cmp_ = active.get("campaign") or {}
        top_url = pc.get("topFileUrl") or ""
        bot_url = pc.get("bottomFileUrl") or ""
        # Clean human-readable label derived from the file name
        # (preferred over the free-form `collectionName` because the
        # filename uses underscores and is what production looks for).
        label = (
            _extract_collection_label(top_url)
            or _extract_collection_label(bot_url)
            or (pc.get("collectionName") or "")
        )
        # Compose a single-line full address from venue parts
        ven = k.get("venue") or {}
        addr_parts = []
        if ven.get("address1"):
            addr_parts.append(ven["address1"])
        if ven.get("address2"):
            addr_parts.append(ven["address2"])
        city_state_zip = ", ".join(p for p in [
            ven.get("city") or "",
            (ven.get("state") or "") + (" " + ven["zip"] if ven.get("zip") else ""),
        ] if p.strip(", "))
        if city_state_zip.strip(", "):
            addr_parts.append(city_state_zip)
        full_address = ", ".join(addr_parts)

        # Kiosk type prefers explicit Premium/Luxury/Digital naming.
        ktype_name = ((k.get("kioskType") or {}).get("typeName") or "").strip()
        if k.get("isDigital") and not ktype_name:
            ktype_name = "Digital"

        # boosted (the workflow flag) on the active campaignKiosk — boolean.
        boosted_bool = bool(active.get("boosted"))
        ad_placement = ((active.get("kioskAdPlacement") or {}).get("typeName") or "").strip()

        out[kid] = {
            # campaign / art
            "sio": str(cmp_.get("orderNumber") or ""),
            "campaign_id": cmp_.get("id"),
            "campaign_name": cmp_.get("name") or "",
            "top_file_url": top_url,
            "bottom_file_url": bot_url,
            "collection_name": pc.get("collectionName") or "",
            "collection_label": label,
            # venue
            "venue_name": ven.get("venueName") or "",
            "venue_address": full_address,
            "venue_state": (ven.get("state") or "").strip().upper(),
            # kiosk
            "kiosk_loc": ((k.get("kioskLocation") or {}).get("typeName") or "").strip(),
            "kiosk_type": ktype_name,
            "is_digital": bool(k.get("isDigital")),
            # placement / boost
            "boosted": boosted_bool,
            "ad_placement": ad_placement,
        }
    return out


# -- Venue-based lookup (fallback when OnFleet has no kioskid customField) ---
_VENUE_LOOKUP_QUERY = """query VenueLookup($vids: [Int!]) {
  kiosks(where: {venueId: {in: $vids}}) {
    importKioskId
    venueId
    kioskLocationId
    isDigital
    kioskLocation { id typeName }
    kioskType { id typeName }
    venue {
      id
      venueName
      address1
      address2
      city
      state
      zip
    }
    campaignKiosks {
      reservationStart
      reservationEnd
      boosted
      kioskAdPlacement { typeName }
      printCollection {
        id
        collectionName
        topFileUrl
        bottomFileUrl
      }
      campaign {
        id
        name
        orderNumber
        statusId
      }
    }
  }
}"""


def _do_fetch_venues(venue_ids: tuple) -> dict:
    """Returns {venue_id (int): [kiosk_entry, ...]} where each kiosk_entry is
    the same shape produced by _do_fetch() (sio, campaign_name, venue_*, etc.),
    plus an "importKioskId" field so the caller can identify which kiosk it is.
    """
    if not venue_ids:
        return {}
    clean = []
    for v in venue_ids:
        try:
            clean.append(int(v))
        except (TypeError, ValueError):
            continue
    if not clean:
        return {}
    resp = _query(_VENUE_LOOKUP_QUERY, {"vids": sorted(set(clean))})
    out: dict = {}
    today = date.today()
    for k in (resp.get("data") or {}).get("kiosks") or []:
        vid = k.get("venueId")
        if vid is None:
            continue
        active = _pick_active(k.get("campaignKiosks") or [], today)
        if not active:
            # Still include the kiosk so callers know it exists; just no campaign data.
            entry = {"importKioskId": k.get("importKioskId") or "", "_no_active_campaign": True}
        else:
            pc = active.get("printCollection") or {}
            cmp_ = active.get("campaign") or {}
            top_url = pc.get("topFileUrl") or ""
            bot_url = pc.get("bottomFileUrl") or ""
            label = (
                _extract_collection_label(top_url)
                or _extract_collection_label(bot_url)
                or (pc.get("collectionName") or "")
            )
            ven = k.get("venue") or {}
            addr_parts = []
            if ven.get("address1"):
                addr_parts.append(ven["address1"])
            if ven.get("address2"):
                addr_parts.append(ven["address2"])
            csz = ", ".join(p for p in [
                ven.get("city") or "",
                (ven.get("state") or "") + (" " + ven["zip"] if ven.get("zip") else ""),
            ] if p.strip(", "))
            if csz.strip(", "):
                addr_parts.append(csz)
            full_address = ", ".join(addr_parts)
            ktype_name = ((k.get("kioskType") or {}).get("typeName") or "").strip()
            if k.get("isDigital") and not ktype_name:
                ktype_name = "Digital"
            entry = {
                "importKioskId": k.get("importKioskId") or "",
                "sio": str(cmp_.get("orderNumber") or ""),
                "campaign_id": cmp_.get("id"),
                "campaign_name": cmp_.get("name") or "",
                "top_file_url": top_url,
                "bottom_file_url": bot_url,
                "collection_name": pc.get("collectionName") or "",
                "collection_label": label,
                "venue_name": ven.get("venueName") or "",
                "venue_address": full_address,
                "venue_state": (ven.get("state") or "").strip().upper(),
                "kiosk_loc": ((k.get("kioskLocation") or {}).get("typeName") or "").strip(),
                "kiosk_type": ktype_name,
                "is_digital": bool(k.get("isDigital")),
                "boosted": bool(active.get("boosted")),
                "ad_placement": ((active.get("kioskAdPlacement") or {}).get("typeName") or "").strip(),
            }
        out.setdefault(vid, []).append(entry)
    # ---- Back-fill missing collection names ----
    # When Terraboost has printCollection set on some kiosks but not others on
    # the SAME campaign, propagate the collection to the missing kiosks. Two
    # passes — wide-scope first, narrow-scope fallback:
    #
    #   1. Per-campaign GLOBAL: gather every distinct non-blank collection_name
    #      across ALL kiosks on the campaign. If they all share ONE name,
    #      that's the campaign's canonical art — propagate to every blank
    #      kiosk on the campaign, even across venues. (Scorch LLC at Save Mart
    #      and at Foods Co. share the same PG&E art, etc.)
    #
    #   2. Per-(campaign, venue) FALLBACK: when a campaign has multiple
    #      distinct collection names across venues (genuine regional variants),
    #      we DON'T cross venue boundaries — only kiosks at the same venue
    #      share that venue's specific variant.
    #
    # A kiosk that already has its own non-blank collection is never
    # overwritten — own value always wins.
    from collections import defaultdict as _dd
    by_camp_names = _dd(set)        # cid -> set of distinct collection_names
    by_camp_global = {}             # cid -> {name, label, top, bot} canonical
    by_camp_venue = {}              # (cid, vid) -> {name, label, top, bot}

    for vid, entries in out.items():
        for e in entries:
            cid = e.get("campaign_id")
            cn = e.get("collection_name")
            if not cid or not cn:
                continue
            by_camp_names[cid].add(cn)
            # Stash a canonical bundle keyed globally and per-venue
            bundle = {
                "collection_name": cn,
                "collection_label": e.get("collection_label") or "",
                "top_file_url": e.get("top_file_url") or "",
                "bottom_file_url": e.get("bottom_file_url") or "",
            }
            by_camp_global.setdefault(cid, bundle)
            by_camp_venue.setdefault((cid, vid), bundle)

    for vid, entries in out.items():
        for e in entries:
            cid = e.get("campaign_id")
            if not cid:
                continue
            if e.get("collection_name"):
                continue  # own value wins, never overwritten
            # Pick the global bundle if the campaign has exactly one canonical
            # collection across all venues; else fall back to same-venue.
            distinct = by_camp_names.get(cid) or set()
            sibling = (by_camp_global.get(cid)
                       if len(distinct) == 1
                       else by_camp_venue.get((cid, vid)))
            if not sibling:
                continue
            for k in ("collection_name", "collection_label",
                      "top_file_url", "bottom_file_url"):
                if not e.get(k) and sibling.get(k):
                    e[k] = sibling[k]
    return out


if _HAS_STREAMLIT:
    @st.cache_data(ttl=3600, show_spinner=False)
    def fetch_campaign_index(kids_tuple: tuple) -> dict:
        """
        For a tuple of OnFleet KIDs, returns the active-campaign metadata
        for each. Cached for 1 hour, shared across all dispatchers.
        """
        return _do_fetch(kids_tuple)

    @st.cache_data(ttl=3600, show_spinner=False)
    def fetch_venue_index(venue_ids_tuple: tuple) -> dict:
        """
        For a tuple of OnFleet venue IDs (VIDs), returns
        {vid: [list of kiosk entries at that venue with current-campaign data]}.
        Used as a fallback when OnFleet's kioskid customField is missing —
        the caller uses venue + campaign-name match to identify which kiosk.
        """
        return _do_fetch_venues(venue_ids_tuple)
else:
    def fetch_campaign_index(kids_tuple: tuple) -> dict:
        return _do_fetch(kids_tuple)

    def fetch_venue_index(venue_ids_tuple: tuple) -> dict:
        return _do_fetch_venues(venue_ids_tuple)


# -- Smoke test (run this module directly: `python terraboost_campaigns.py`) -
if __name__ == "__main__":
    import json
    test_kids = ("91304B", "91304A", "95164A")
    print(f"Testing with KIDs: {test_kids}")
    if not os.environ.get("TERRABOOST_EMAIL") or not os.environ.get("TERRABOOST_PASSWORD"):
        print("Set TERRABOOST_EMAIL and TERRABOOST_PASSWORD env vars first.")
    else:
        result = fetch_campaign_index(test_kids)
        print(json.dumps(result, indent=2))
