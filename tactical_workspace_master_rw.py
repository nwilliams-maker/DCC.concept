import streamlit as st
import requests
import base64
import math
import pandas as pd
import time
import hashlib
import json
from datetime import datetime, timedelta
from streamlit_folium import st_folium
import folium
import threading
from concurrent.futures import ThreadPoolExecutor
import os
import re
import html as _html

# --- CONFIG & CREDENTIALS ---
# We check the Environment (Railway) FIRST to avoid the Streamlit Secrets crash.
# Google Cloud Services were dropped May 1 2026 — Mapbox handles geocoding +
# route distance + waypoint optimization, Geocodio handles address lookups in
# the GAS backend. GOOGLE_MAPS_KEY is no longer required for startup.
ONFLEET_KEY = os.environ.get("ONFLEET_KEY")
# Mapbox is the active routing / geocoding key for the live app.
#   Set in Railway: MAPBOX_TOKEN=pk.eyJ1...   (public token from Mapbox dashboard)
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN")
# GOOGLE_MAPS_KEY kept as a soft fallback (some embedded JS components still
# reference it for static maps preview). NOT required for app startup.
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY")

# If Railway didn't have them, ONLY THEN do we try st.secrets (and we catch the error)
if not ONFLEET_KEY or not MAPBOX_TOKEN:
    try:
        ONFLEET_KEY = ONFLEET_KEY or st.secrets.get("ONFLEET_KEY")
        MAPBOX_TOKEN = MAPBOX_TOKEN or st.secrets.get("MAPBOX_TOKEN")
    except Exception:
        # If we get here, it means no secrets file exists AND Railway variables are missing
        pass
    try:
        GOOGLE_MAPS_KEY = GOOGLE_MAPS_KEY or st.secrets.get("GOOGLE_MAPS_KEY")
    except Exception:
        pass

# Final check — only the keys the app actually needs to function.
if not ONFLEET_KEY or not MAPBOX_TOKEN:
    st.error("🔑 **API Keys Missing!**")
    st.info("I couldn't find your keys in Railway's 'Variables' tab. Please double-check that you added ONFLEET_KEY and MAPBOX_TOKEN there.")
    st.stop()

PORTAL_BASE_URL = os.environ.get("PORTAL_BASE_URL") or "https://nwilliams-maker.github.io/DCC/TerraboostRouteRequest.html"
# Legacy Google backend variables are retained only so old helper code can
# import cleanly during the Railway/Postgres cutover. Production route state
# no longer depends on Google Sheets or Apps Script.
GAS_WEB_APP_URL = ""
GAS_AUTH = ""
IC_SHEET_URL = ""

# --- TUNABLES (hoisted from inline magic numbers) ---
DEFAULT_DUE_DAYS = 14   # default deadline offset from today when dispatcher hasn't picked one
RATE_CRITICAL = 24.00   # $/stop — red status
RATE_WARNING = 21.00    # $/stop — orange status

# Security audit M29 caps. Total Comp and Rate/Stop are two views of the same
# number (rate = comp / stops), so any write to one master key must keep BOTH
# within their widget's [0, cap] range — otherwise a legitimately-capped Total
# Comp on a low-stop-count route (e.g. $2,225.94 on a 1-stop route) produces an
# out-of-range Rate/Stop that Streamlit refuses to render, crashing that route's
# card with StreamlitValueAboveMaxError. Every site that sets _pay_master_key /
# _rate_master_key must clamp through PAY_CAP / RATE_CAP.
PAY_CAP = 10000.00      # $ — matches the Total Comp widget's max_value
RATE_CAP = 2000.00      # $/stop — matches the Rate/Stop widget's max_value
HIGH_RATE_FLAG_THRESHOLD = 25.00

# 🌟 FA (Field Agent) employees — Nick, Sep 19 2026: "For FAs they don't need
# any pay rate, they are employees" (Biscardi is an IC and keeps normal
# per-route pay — confirmed separately: "Biscardi is fine"). These are the
# same three Field Agents named in the _UNRESTRICTED_ICS note below ("FA
# Freddy Ledbetter, Michael Erlandson, Kevin O'Connor"). Matched on last name
# only, same convention as _UNRESTRICTED_ICS, so sheet formatting differences
# still match. Defined at module scope (not inside render_dispatch's ic_df
# branch) so it's always available at the point `ic` gets checked, even on a
# call where that branch didn't run.
FA_EMPLOYEE_NAMES = ['ledbetter', 'erlandson', "o'connor", 'oconnor']

def _is_fa_employee(ic_row):
    """True if `ic_row` (a pandas Series or dict-like with a 'name' field) is
    one of the salaried FA employees, who get no per-route pay rate."""
    try:
        _name = str(ic_row.get('name', '') or '')
    except Exception:
        _name = ''
    _nl = _name.lower()
    return any(_fa in _nl for _fa in FA_EMPLOYEE_NAMES)

# Lightweight stderr logger — replaces silent `except: pass` so failures are visible in Railway logs.
def _log_err(context, exc):
    try:
        print(f"[{context}] {type(exc).__name__}: {exc}", flush=True)
    except Exception:
        pass

# --- OPTIONAL POSTGRES BACKEND (see migration/README.md) ---
# Dormant by default: DATABASE_URL is not set in Railway yet, so DB_ENGINE
# stays None and every call site wired to it below falls through to its
# existing Sheet/GAS code path, completely unchanged. Setting DATABASE_URL
# (only after the staging verification pass in migration/README.md step 8)
# is what turns this on -- nothing here changes today's behavior on its own.
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
DB_ENGINE = None
if DATABASE_URL:
    try:
        import sqlalchemy as _sa
        from migration import data_access as _da
        DB_ENGINE = _sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    except Exception as _db_init_e:
        _log_err("db_engine_init", _db_init_e)
        DB_ENGINE = None


def _ic_df_from_db(engine):
    """Adapts data_access.get_contractors()'s SQL-shaped DataFrame to the
    exact legacy column names/casing the rest of this app expects from the
    Sheet CSV (space-separated multi-word headers -- 'ic list', 'pod color',
    'digital certified' -- everything else already matches). Values need no
    further conversion: 'digital certified' stays a Python bool and
    str(True/False).upper() already satisfies the existing
    `cert_val in ['YES','Y','TRUE','1','1.0']` check downstream, and `phone`
    comes through as text (no pandas ".0" float artifact to strip, unlike the
    CSV path)."""
    df = _da.get_contractors(engine)
    return df.rename(columns={
        "ic_list": "ic list",
        "pod_color": "pod color",
        "digital_certified": "digital certified",
    })

# Security audit H3 - HTML-escape semi-trusted (Onfleet / Google Sheet) text
# before interpolating into any unsafe_allow_html block. Stops stored XSS via
# poisoned venue/campaign/client/contractor/WO values and stray markup chars.
def esc(value):
    try:
        return _html.escape(str(value if value is not None else ""), quote=True)
    except Exception:
        return ""

# Security audit M11 - pagination for the Awaiting Confirmation panels.
# A busy pod can hold ~89 route cards, each a large HTML block plus a
# nested fragment - enough to freeze the browser. This renders ~20 cards
# per page with prev/next controls. `page_key` must be unique per panel
# per pod (run_pod_tab runs once per pod for admin/manager). Returns
# (page_slice, start_index) so callers keep stable global indices.
def _paginate_panel(items, page_key, per_page=20):
    try:
        items = list(items or [])
        total = len(items)
        if True:  # pagination removed May 2026 — state-group collapse handles long lists
            return items, 0
        n_pages = (total + per_page - 1) // per_page
        _sk = f"_panel_page_{page_key}"
        cur = int(st.session_state.get(_sk, 0) or 0)
        if cur < 0:
            cur = 0
        if cur > n_pages - 1:
            cur = n_pages - 1
        st.markdown(
            """
            <style>
            div[class*="st-key-_pg"] button {
                background: #faf9fd !important;
                border: 1px solid #d9d4ec !important;
                border-radius: 8px !important;
                box-shadow: none !important;
                min-height: 2rem !important;
                height: 2rem !important;
                padding: 0.1rem 0.4rem !important;
            }
            div[class*="st-key-_pg"] button p {
                font-size: 0.78rem !important;
                font-weight: 500 !important;
                color: #7a72a8 !important;
            }
            div[class*="st-key-_pg"] button:disabled { opacity: 0.4 !important; }
            </style>
            """,
            unsafe_allow_html=True,
        )
        _spl, _pc1, _pc2, _pc3, _spr = st.columns([1, 1.3, 3, 1.3, 1])
        with _pc1:
            if st.button("‹ Prev", key=f"_pgprev_{page_key}",
                         disabled=(cur <= 0), use_container_width=True):
                st.session_state[_sk] = max(0, cur - 1)
                st.rerun()
        with _pc2:
            _start = cur * per_page
            _end = min(total, _start + per_page)
            st.markdown(
                f"<div style='text-align:center; font-size:0.74rem; color:#94a3b8; padding-top:0.5rem; white-space:nowrap;'>{_start + 1}-{_end} of {total} &middot; page {cur + 1}/{n_pages}</div>",
                unsafe_allow_html=True,
            )
        with _pc3:
            if st.button("Next ›", key=f"_pgnext_{page_key}",
                         disabled=(cur >= n_pages - 1), use_container_width=True):
                st.session_state[_sk] = min(n_pages - 1, cur + 1)
                st.rerun()
        _s = cur * per_page
        return items[_s:_s + per_page], _s
    except Exception as _pg_e:
        _log_err("paginate_panel", _pg_e)
        return list(items or []), 0
# Security audit H17 / M7 - routes whose background archive write failed all
# retries. The re-route handler checks this set on the next render and shows
# the dispatcher a visible warning instead of silently leaving a ghost.
_RECONCILE_FAILURES = set()

# Security audit M12 - pods with a route-gmaps warm thread currently running,
# so a repeated process_pod() call does not stack redundant warm threads.
_GMAPS_WARM_INFLIGHT = set()

SAVED_ROUTES_GID = "1477617688"
ACCEPTED_ROUTES_GID = "934075207"
DECLINED_ROUTES_GID = "600909788"
FIELD_NATION_GID = "1396320527"
FINALIZED_ROUTES_GID = "1907347870"
ARCHIVE_GID = "1841508981"  # Read-only side-channel: only used to harvest archived_wo for the WO counter; never used for clustering.

# Routes created before this date are skipped when reading the route database. This
# was added to cut over the pre-launch migration; rows older than the cutoff should
# eventually be moved to the Archive tab so this constant can go away.
MIGRATION_CUTOFF_DATE = "2026-04-20"

# Terraboost Media Brand Palette
TB_PURPLE = "#633094"
TB_GREEN = "#76bc21"
TB_APP_BG = "#f1f5f9"
TB_HOVER_GRAY = "#e2e8f0"

# Status Fills
TB_GREEN_FILL = "#dcfce7" # Ready
TB_BLUE_FILL = "#dbeafe"  # Sent
TB_RED_FILL = "#fee2e2"   # Flagged
TB_YELLOW_FILL = "#FEF9C3"     # Field Nation
TB_STATIC_FILL = "#f1f5f9"
TB_DIGITAL_FILL = "#ccfbf1"
TB_DIGITAL_BORDER = "#99f6e4" # Teal border

# Standardized Dark Text (for readability)
TB_GREEN_TEXT = "#166534"
TB_RED_TEXT = "#991b1b"
TB_STATIC_TEXT = "#475569"
TB_DIGITAL_TEXT = "#0f766e"

POD_CONFIGS = {
    # Sep 2026 (Nick) — OR/WA/NV moved from Orange to Blue.
    "Blue": {"states": {"AL", "AR", "FL", "IL", "IA", "LA", "MI", "MN", "MS", "MO", "NC", "SC", "WI", "OR", "WA", "NV"}},
    "Green": {"states": {"CO", "DC", "GA", "IN", "KY", "MD", "NJ", "OH", "UT"}},
    "Orange": {"states": {"AK", "AZ", "CA", "HI", "ID"}},
    "Purple": {"states": {"KS", "MT", "NE", "NM", "ND", "OK", "SD", "TN", "TX", "WY"}},
    "Red": {"states": {"CT", "DE", "ME", "MA", "NH", "NY", "PA", "RI", "VT", "VA", "WV"}}
}

STATE_MAP = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC"
}

headers = {"Authorization": f"Basic {base64.b64encode(f'{ONFLEET_KEY}:'.encode()).decode()}"}


# ─────────────────────────────────────────────────────────────────────────────
# 🌍 SHARED ONFLEET PULL — process-scoped cache (60s TTL)
# ─────────────────────────────────────────────────────────────────────────────
# With 5 Dispatchers + 1+ Associates on the same Railway instance, every pod
# Initialize / Sync click was independently paginating tasks/all?state=0 — so
# 5-7 identical pulls would burn through the Onfleet rate budget and trigger
# "Bandwidth quota exceeded" errors. This shared cache means only the first
# caller within a 60-second window pays the network cost; everyone else gets
# the in-memory result instantly.
#
# st.cache_data is process-scoped (not session-scoped) so it's shared across
# all dispatchers on the same Railway worker. Concurrent calls block on the
# first call's result — no thundering-herd to Onfleet.
#
# Failures (non-200, non-429) raise so they don't poison the cache. The 60s
# TTL is the staleness ceiling — if a Dispatcher's view is more than a minute
# old they can hit Sync Routes; if it's less, the cached result is fresh
# enough for dispatching decisions.
@st.cache_data(ttl=60, show_spinner=False)
def _fetch_onfleet_open_tasks_cached():
    """Returns dict with 'tasks' (deduped list of task dicts), 'target_team_ids',
    'esc_team_ids', 'cvs_remov_team_ids', '_page_count', '_hit_cap'.
    Raises on hard error so the failure isn't cached."""
    APPROVED_TEAMS = [
        "a - escalation", "b - boosted campaigns", "b - local campaigns",
        "c - priority nationals", "cvs kiosk removal", "digital routes",
        "n - national campaigns",
        # May 18 2026 — added per-pod teams in OnFleet ("POD: Blue", "POD: Green",
        # "POD: Orange", "POD: Purple", "POD: Red"). Tasks in these teams were
        # silently dropped at the approved-team filter, vanishing from
        # Ready/Flagged buckets. Match is case-insensitive substring.
        "pod: blue", "pod: green", "pod: orange", "pod: purple", "pod: red",
    ]
    # Onfleet's /teams normally returns a list of team dicts. On auth failure,
    # rate limit, or transient error it can return a dict like {"message": "...",
    # "code": "..."} — iterating a dict yields string keys, which then explode
    # downstream on t.get(), surfacing as "Onfleet API Error: 'str' object has
    # no attribute 'get'". Coerce defensively + skip non-dict items so the call
    # site can never hit that traceback. (May 15 2026.)
    _teams_raw = requests.get("https://onfleet.com/api/v2/teams", headers=headers, timeout=15).json()
    if not isinstance(_teams_raw, list):
        # Common alternate shapes: {"teams": [...]} wrapper, or an error envelope.
        if isinstance(_teams_raw, dict) and isinstance(_teams_raw.get('teams'), list):
            _teams_raw = _teams_raw['teams']
        else:
            raise RuntimeError(f"Onfleet /teams returned non-list payload: {str(_teams_raw)[:200]}")
    teams_res = [t for t in _teams_raw if isinstance(t, dict)]
    target_team_ids = [t['id'] for t in teams_res if any(appr in str(t.get('name', '')).lower() for appr in APPROVED_TEAMS)]
    esc_team_ids = [t['id'] for t in teams_res if 'escalation' in str(t.get('name', '')).lower()]
    cvs_remov_team_ids = [t['id'] for t in teams_res if 'cvs kiosk remov' in str(t.get('name', '')).lower()]
    fn_team_ids = [t['id'] for t in teams_res if 'field nation' in str(t.get('name', '')).lower()]
    # Jul 2026 (Nick): any task in the "D - Digital Routes" team stays in the
    # Digital tab regardless of its task type. Overrides REGULAR_EXEMPTIONS
    # and DIGITAL_WHITELIST — team membership wins.
    digital_team_ids = [t['id'] for t in teams_res if 'digital routes' in str(t.get('name', '')).lower()]
    # 🚫 Excluded teams — never include tasks in these team pools in any pod's
    # Ready/Flagged. Match is case-insensitive.
    # _EXCLUDED_TEAM_SUBSTRINGS: literal substrings (full phrases).
    # _EXCLUDED_TEAM_WORD_REGEX: word-boundary match on "test" (Jun 18 2026,
    # Nick) — catches any team with "test" as its own word ("QA Test Team",
    # "Test Pod") without false-positive on "Greatest", "Tested", etc.
    # 'sandbox' (Sep 15 2026, Nick: "do not include any team with SANDBOX in
    # it") — plain substring match is correct here since Nick asked for any
    # team with "SANDBOX" anywhere in its name (e.g. "Sandbox", "QA Sandbox",
    # "Sandbox - Pod Blue"), not just a whole-word match.
    _EXCLUDED_TEAM_SUBSTRINGS = ['zzz test team', 'sandbox']
    _EXCLUDED_TEAM_WORD_REGEX = re.compile(r'(?:^|[^a-z])test(?:[^a-z]|$)', re.IGNORECASE)
    def _is_excluded_team_name(_name):
        _n = str(_name or '')
        _nl = _n.lower()
        if any(ex in _nl for ex in _EXCLUDED_TEAM_SUBSTRINGS):
            return True
        if _EXCLUDED_TEAM_WORD_REGEX.search(_n):
            return True
        return False
    excluded_team_ids = [t['id'] for t in teams_res if _is_excluded_team_name(t.get('name', ''))]

    # 🌐 Field Nation placeholder worker — looked up by phone (last 10 digits).
    # Tasks PUT with this worker_id flip from state=0 to state=1, dropping out
    # of the unassigned pool. Created in Onfleet by Nick on 2026-04-30.
    FN_WORKER_PHONE = "6302869764"
    fn_worker_id = None
    _fn_worker_lookup_failed = False  # security audit M9
    _w_lastid = None
    _w_seen = set()
    for _wp in range(10):
        _wurl = "https://onfleet.com/api/v2/workers" + (f"?lastId={_w_lastid}" if _w_lastid else "")
        try:
            _wresp = requests.get(_wurl, headers=headers, timeout=10)
            if _wresp.status_code != 200:
                # Security audit M9 - a non-200 here means the FN worker
                # lookup is incomplete; flag it so the None worker is not
                # silently propagated (FN tasks would stay in state=0).
                _fn_worker_lookup_failed = True
                break
            _wdata = _wresp.json()
            _wlist = _wdata if isinstance(_wdata, list) else _wdata.get('workers', [])
            if not _wlist:
                break
            _new_count = 0
            for _w in _wlist:
                _wid = _w.get('id')
                if not _wid or _wid in _w_seen:
                    continue
                _w_seen.add(_wid)
                _new_count += 1
                _wphone_digits = ''.join(c for c in str(_w.get('phone', '') or '') if c.isdigit())[-10:]
                if _wphone_digits == FN_WORKER_PHONE:
                    fn_worker_id = _wid
                    break
            if fn_worker_id or _new_count == 0:
                break
            _w_lastid = _wlist[-1].get('id')
        except Exception:
            break

    all_tasks_raw = []
    time_window = int(time.time() * 1000) - (45 * 24 * 3600 * 1000)
    url = f"https://onfleet.com/api/v2/tasks/all?state=0&from={time_window}"

    _MAX_PAGES = 200
    _page = 0
    _seen_last_ids = set()
    _retry_429 = 0
    while url and _page < _MAX_PAGES:
        _page += 1
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 429:
            # Security audit M9 - exponential backoff for rate limits so a
            # transient 429 does not burn pages at full speed. Capped at 6
            # retries (~63s worst case) then raises so the cache discards.
            _retry_429 += 1
            if _retry_429 > 6:
                raise RuntimeError("Onfleet 429 rate-limit persisted after 6 backoff retries")
            time.sleep(min(60, 2 ** _retry_429))
            _page -= 1
            continue
        _retry_429 = 0
        if response.status_code != 200:
            # Raise so st.cache_data discards this attempt — the next caller
            # will retry instead of inheriting a half-populated list.
            raise RuntimeError(f"Onfleet API error {response.status_code}: {response.text[:200]}")
        res_json = response.json()
        all_tasks_raw.extend(res_json.get('tasks', []))
        _next_id = res_json.get('lastId')
        if _next_id and _next_id in _seen_last_ids:
            break
        if _next_id:
            _seen_last_ids.add(_next_id)
        url = f"https://onfleet.com/api/v2/tasks/all?state=0&from={time_window}&lastId={_next_id}" if _next_id else None

    unique_tasks = list({t['id']: t for t in all_tasks_raw}.values())
    # ROUTE-PLAN EXCLUSION (May 30 2026, Nick): any OnFleet route plan whose
    # name contains 'hold' or 'pause' is treated as a parking lot -- tasks
    # inside it never enter the dispatchable pool. Case-insensitive substring.
    # Filtering at this boundary so every downstream consumer inherits the
    # pre-filtered list with no per-loop change.
    # Match ONLY when 'hold' or 'pause' appears as its own word (not as a substring
    # of innocuous names like 'Household' or 'placeholder'). Word boundary regex.
    _EXCLUDED_PLAN_REGEX = re.compile(r'(?:^|[^a-z])(hold|pause)(?:[^a-z]|$)', re.IGNORECASE)
    _excluded_plan_ids = set()
    _excluded_plan_names = []
    try:
        _plans_raw = requests.get(
            f"https://onfleet.com/api/v2/routePlans?from={time_window}",
            headers=headers, timeout=15,
        ).json()
        if isinstance(_plans_raw, dict):
            _plans_list = _plans_raw.get('routePlans') or _plans_raw.get('plans') or _plans_raw.get('data') or []
        elif isinstance(_plans_raw, list):
            _plans_list = _plans_raw
        else:
            _plans_list = []
        for _p in _plans_list:
            if not isinstance(_p, dict):
                continue
            _pname_raw = str(_p.get('name', ''))
            if _EXCLUDED_PLAN_REGEX.search(_pname_raw):
                _pid = _p.get('id') or _p.get('_id')
                if _pid:
                    _excluded_plan_ids.add(_pid)
                    _excluded_plan_names.append(_pname_raw)
    except Exception as _rp_e:
        _log_err("_fetch_onfleet_open_tasks_cached/routePlans", _rp_e)
    _pre_filter_count = len(unique_tasks)
    if _excluded_plan_ids:
        unique_tasks = [
            t for t in unique_tasks
            if (t.get('routePlan') or t.get('routePlanId') or '') not in _excluded_plan_ids
        ]
    _post_filter_count = len(unique_tasks)
    # Stash debug info so a dispatcher (or ?debug=1) can see what got eaten.
    try:
        st.session_state['_hold_filter_debug'] = {
            'excluded_plans': _excluded_plan_names,
            'tasks_before': _pre_filter_count,
            'tasks_after': _post_filter_count,
            'tasks_removed': _pre_filter_count - _post_filter_count,
        }
    except Exception:
        pass
    return {
        'tasks': unique_tasks,
        'target_team_ids': target_team_ids,
        'esc_team_ids': esc_team_ids,
        'cvs_remov_team_ids': cvs_remov_team_ids,
        'digital_team_ids': digital_team_ids,
        'fn_team_id': (fn_team_ids[0] if fn_team_ids else None),
        'excluded_team_ids': excluded_team_ids,
        'fn_worker_id': fn_worker_id,
        '_fn_worker_lookup_failed': bool(_fn_worker_lookup_failed and fn_worker_id is None),
        '_page_count': _page,
        '_hit_cap': _page >= _MAX_PAGES,
    }


st.set_page_config(
    page_title="Terraboost Media: Dispatch Command Center", layout="wide",
    initial_sidebar_state="collapsed" if os.environ.get("DCC_REVAMP_UI") == "1" else "auto",
)

# ── COMPACT-BUTTON CSS (global — May 16 2026) ────────────────────────────────
# Streamlit 1.39 has no `st.button(size="small")`. The default 38px-tall buttons
# eat too much vertical space, especially in the FN tab's bulk-action row and
# in every popover's confirm/cancel. This compacts all .stButton, stDownloadButton,
# and stLinkButton across the app to ~26px tall with 12.5px font. No JS — pure
# CSS, applied once per page load.
st.markdown("""
<style>
.stButton > button,
.stDownloadButton > button,
.stLinkButton > a {
    padding: 0.2rem 0.6rem !important;
    font-size: 0.78rem !important;
    min-height: 28px !important;
    line-height: 1.15 !important;
    font-weight: 600 !important;
}
/* Multiselect chip rows can shrink a touch too so the FN selection list
   doesn't dominate the panel after a bulk-select. */
.stMultiSelect [data-baseweb="tag"] {
    font-size: 0.72rem !important;
    padding: 0.05rem 0.4rem !important;
    height: 22px !important;
}
</style>
""", unsafe_allow_html=True)

# ============================================================================
# 🔄 DEPLOY-RECOVERY WATCHER — silent auto-recovery from Railway redeploys
# ============================================================================
# When DCC redeploys, browser tabs that were open before the deploy can hit
# Streamlit "Bad message format" / "SessionInfo" / "fragment id" errors when
# the user next interacts. This watcher prevents that from ever showing:
#
#   • A unique INSTANCE_ID is generated when the Python process starts.
#     It's rendered into the DOM as a hidden data attribute.
#   • Browser-side JS captures the instance ID on first load.
#   • Every 30s it compares — if the server's instance ID changed (Python
#     process restarted), it shows a small "App was updated" banner with a
#     "Refresh now" button. No popup, no countdown, no forced reload.
#   • If the user has been idle 10+ min when the change is detected, the
#     reload happens silently in the background.
#   • Streamlit's own error events (Bad message / setIn / fragment id) are
#     caught by a global error listener — if the user is idle (>5s no input)
#     the page reloads silently. Otherwise the banner appears instead of
#     the red error toast.
#
# Net: dispatchers never see a Streamlit error popup, never get yanked
# mid-typing, and the latest code reaches them on their schedule.
# ============================================================================
import uuid as _uuid
import streamlit.components.v1 as _components
import os as _os

# 🌟 STATE-GROUP COLLAPSE (client-side, zero Streamlit reruns) — pairs with
# the .dcc-state-header class emitted by each state header markdown below.
# Clicks toggle sibling visibility in the DOM; sessionStorage remembers
# which groups the dispatcher has opened so choices survive Streamlit
# reruns and full page reloads. All state headers start collapsed.
_components.html(
    """
<script>
(function () {
  var STORAGE_KEY = 'dcc_state_collapsed_v1';
  var doc = window.parent.document;
  // On FRESH page loads start collapsed. window.parent persists across
  // Streamlit reruns (same window) but resets on hard reload / tab close —
  // so this flag distinguishes "fresh page" from "in-session rerun".
  if (!window.parent._dccCollapseInitialized) {
    window.parent._dccCollapseInitialized = true;
    try { window.parent.sessionStorage.removeItem(STORAGE_KEY); } catch (_e) {}
  }
  function getStored() {
    try { return JSON.parse(window.parent.sessionStorage.getItem(STORAGE_KEY) || '{}'); }
    catch (_e) { return {}; }
  }
  function setStored(m) {
    try { window.parent.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(m)); }
    catch (_e) {}
  }
  function containerOf(el) {
    return el.closest('[data-testid="stElementContainer"]') || el.parentElement;
  }
  function applyState(hdr, expanded) {
    hdr.classList.toggle('dcc-expanded', expanded);
    hdr.classList.toggle('dcc-collapsed', !expanded);
    var c = containerOf(hdr);
    if (!c) return;
    var next = c.nextElementSibling;
    while (next) {
      if (next.querySelector && (next.querySelector('.dcc-state-header') || next.querySelector('.dcc-collapse-boundary'))) break;
      next.classList.toggle('dcc-state-hidden', !expanded);
      next = next.nextElementSibling;
    }
  }
  function setup() {
    var stored = getStored();
    var headers = doc.querySelectorAll('.dcc-state-header');
    headers.forEach(function (hdr) {
      var key = hdr.getAttribute('data-state-key') || hdr.textContent.trim();
      if (!hdr.hasAttribute('data-dcc-wired')) {
        hdr.setAttribute('data-dcc-wired', '1');
        hdr.addEventListener('click', function () {
          var willExpand = hdr.classList.contains('dcc-collapsed');
          applyState(hdr, willExpand);
          var s = getStored();
          if (willExpand) s[key] = 1; else delete s[key];
          setStored(s);
        });
        // Apply the persisted choice only ONCE, at wire time (Sep 2026 --
        // fixes "flash open then snap shut" reported live on CA/WA group
        // headers). The old code re-ran this getStored()-derived apply for
        // EVERY header on EVERY MutationObserver tick -- and this observer
        // fires on ANY childList change anywhere on the page (Streamlit
        // reflows constantly: sync-age captions, fragment reruns, etc), not
        // just on this header's own click. If any such unrelated tick fired
        // between a click's applyState() and its setStored() -- or just read
        // a slower/partial sessionStorage state -- it forced the header back
        // to whatever storage said a moment earlier, undoing the click that
        // had just opened it. Storage is now only consulted here, at wire
        // time, to restore what the dispatcher chose earlier in the tab's
        // life; every later re-tick trusts the DOM below instead.
        applyState(hdr, !!stored[key]);
      } else {
        // Already wired. Don't re-derive from storage -- that's what raced.
        // Just cascade THIS header's own current (already-correct) expand
        // state onto any sibling cards that appeared after it was wired
        // (lazy pagination, a fresh sync adding routes), so they inherit
        // visibility instead of defaulting to shown.
        applyState(hdr, hdr.classList.contains('dcc-expanded'));
      }
    });
  }
  var obs = new MutationObserver(setup);
  if (doc.body) obs.observe(doc.body, { childList: true, subtree: true });
  setup();
})();
</script>
    """,
    height=0,
)
# 🔑 DETERMINISTIC INSTANCE_ID (Jun 18 2026 — Nick: phantom "App was updated"
# banners firing without any deploy).
# Old fallback chain: RAILWAY_DEPLOYMENT_ID → RAILWAY_GIT_COMMIT_SHA → uuid4().
# When neither Railway env var is set (varies by plan/build mode), the uuid4
# fallback regenerates on EVERY container restart — OOM, idle scale-to-zero,
# healthcheck blip, all looked like a fresh deploy to the watcher → banner spam.
# New fallback: an MD5 of the running script's contents. That's stable across
# restarts of the same code, only changes when the code actually changes — so
# the banner now fires ONLY on a real deploy, never on a transparent restart.
def _compute_instance_id():
    # Jul 2026 (Nick): "App was updated" was firing without a real deploy.
    # RAILWAY_DEPLOYMENT_ID / _GIT_COMMIT_SHA can churn on container restarts,
    # worker reshuffles, or Railway-internal events even when the code didn't
    # change. Use the file-content MD5 as the primary source — it only differs
    # when the code actually differs, so the banner only fires on real deploys.
    try:
        import hashlib as _hashlib
        _self_path = _os.path.abspath(__file__)
        with open(_self_path, 'rb') as _fh:
            return _hashlib.md5(_fh.read()).hexdigest()
    except Exception:
        # File-read failed (rare — permissions, missing __file__, etc.). Fall
        # back to Railway env vars if present, then to cwd+python-version hash.
        _rid = _os.environ.get('RAILWAY_DEPLOYMENT_ID') or _os.environ.get('RAILWAY_GIT_COMMIT_SHA')
        if _rid:
            return _rid
        try:
            import sys as _sys, hashlib as _hashlib
            return _hashlib.md5(
                (_os.getcwd() + '|' + _sys.version).encode()
            ).hexdigest()
        except Exception:
            return 'dcc-instance-fallback'

INSTANCE_ID = _compute_instance_id()

# /healthz endpoint — visit with ?healthz=1 to get a tiny status payload.
# Useful for external uptime monitors (Railway, UptimeRobot, etc.) and for
# verifying which deploy is currently serving traffic without DOM scraping.
# Returns INSTANCE_ID + Railway commit SHA (when set). Bypasses login since
# it's just a build-state readback, not user data.
try:
    if st.query_params.get("healthz") == "1":
        # Security audit L1 - return ONLY ok=true. Instance id and the full
        # git commit SHA were dropped: they let an unauthenticated caller
        # fingerprint the exact running build.
        st.markdown(
            "<div style='font-family:monospace;font-size:13px;color:#0f172a;padding:20px;'>"
            "<strong>DCC healthz</strong><br>ok=true</div>",
            unsafe_allow_html=True,
        )
        st.stop()
except Exception:
    pass

# Render the hidden instance-id + banner shell into the parent doc via st.markdown
# (these are static HTML with no scripts — Streamlit will render them fine).
st.markdown(
    f"""
    <div id="dcc-instance-id" data-id="{INSTANCE_ID}" style="display:none;"></div>
    <div id="dcc-update-banner" style="display:none;position:fixed;top:0;left:0;right:0;background:#fef3c7;border-bottom:1px solid #f59e0b;color:#78350f;padding:10px 16px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13px;z-index:99999;justify-content:space-between;align-items:center;box-shadow:0 1px 3px rgba(0,0,0,.08);">
      <span>📦 App was updated. Reload when you\'re ready.</span>
      <span>
        <button id="dcc-refresh-btn" style="background:#f59e0b;color:white;border:0;padding:6px 14px;border-radius:6px;cursor:pointer;font-weight:600;margin-right:8px;">Reload now</button>
        <button id="dcc-dismiss-btn" style="background:transparent;color:#78350f;border:0;cursor:pointer;font-weight:600;padding:6px;">Dismiss</button>
      </span>
    </div>
    """,
    unsafe_allow_html=True,
)

# Watcher v9 — loaded from static/dcc_watcher.js via Streamlit's static file
# server (enableStaticServing must be true in .streamlit/config.toml). The script
# tag is rendered via st.markdown(unsafe_allow_html=True). Streamlit normally
# strips <script> tags, but a script with src= on an external URL passes through.
# We append ?v=INSTANCE_ID for cache-busting per deploy.
# Browsers refuse to execute scripts inserted via innerHTML (which is what
# st.markdown uses). To get an external script to actually run, we use a tiny
# components.v1.html iframe that uses document.createElement() + appendChild()
# in the parent doc — that DOES trigger script execution. The iframe itself is
# only ~30 lines of bootstrap; all real logic is in the static dcc_watcher.js.
_components.html(
    f"""
    <script>
    (function() {{
      try {{
        var parentWin = window.parent;
        if (parentWin._dccWatcherLoaded) return;
        parentWin._dccWatcherLoaded = true;
        // Fetch the watcher source and eval it in the parent window context.
        // Dynamically-inserted <script src=> tags don't reliably execute in some
        // browser/iframe combinations; fetch+eval bypasses that.
        fetch('/app/static/dcc_watcher.js?v={INSTANCE_ID}', {{cache: 'no-cache'}})
          .then(function(r) {{ return r.text(); }})
          .then(function(code) {{
            try {{
              // Use indirect eval so the code runs in global scope of parent window.
              parentWin.eval(code);
            }} catch (e) {{
              console.error('[dcc-watcher] eval failed:', e);
            }}
          }})
          .catch(function(e) {{ console.error('[dcc-watcher] fetch failed:', e); }});
      }} catch(e) {{}}
    }})();
    </script>
    """,
    height=0,
)

# ============================================================================
# 🔁 STALE-DEPLOY GUARD
# ============================================================================
# When Railway swaps containers, browsers with the old session in memory hit
# a brief window where the new server is up but their session/asset hashes
# are gone. Streamlit handles this with a yellow "App was updated — auto-
# refreshing in 30s" banner, but in practice the dispatcher clicks something
# during those 30s and gets a "Bad message format: SessionInfo" or "Connection
# error: 404" modal. Both are the SAME bug — stale tab vs. fresh server.
#
# Mitigation: force a hard reload as soon as we see either signal, instead
# of letting the dispatcher sit on a broken page. Reload-loop guard: if we
# trigger more than 3 times in 60 seconds, stop reloading and surface the
# original error — that means it's NOT a redeploy and reloading won't fix it.
_components.html(
    """
    <script>
    (function() {
      try {
        var w = window.parent;
        var d = w.document;

        // 🌟 DEPLOY OVERLAY (May 2026):
        // After a hard reload triggered by the stale-guard, Streamlit shows its
        // bare gray skeleton placeholders while Python boots up (5-15s). This
        // looks alarming — Nick reported "this pops up on my app." Replace the
        // skeleton period with a branded "Updating DCC..." overlay. Driven by
        // a sessionStorage flag set right before the reload fires.
        try {
          var _deployFlag = w.sessionStorage.getItem('_dcc_deploy_reload_ts');
          if (_deployFlag) {
            var _deployAge = Date.now() - parseInt(_deployFlag, 10);
            if (_deployAge >= 0 && _deployAge < 90000 && !d.getElementById('dcc-deploy-overlay')) {
              var _ov = d.createElement('div');
              _ov.id = 'dcc-deploy-overlay';
              _ov.style.cssText = 'position:fixed;inset:0;background:linear-gradient(135deg,#faf5ff 0%,#f3e8ff 100%);z-index:2147483646;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#633094;transition:opacity 0.4s ease;';
              _ov.innerHTML = '<style>@keyframes dccSpin{0%{transform:rotate(0)}100%{transform:rotate(360deg)}}</style>' +
                '<div style="width:64px;height:64px;border:6px solid #e9d5ff;border-top-color:#633094;border-radius:50%;animation:dccSpin 0.9s linear infinite;"></div>' +
                '<div style="margin-top:24px;font-size:20px;font-weight:800;letter-spacing:0.02em;">📦 Updating DCC...</div>' +
                '<div style="margin-top:8px;font-size:13px;color:#7e22ce;font-weight:600;">Loading the latest version, hang tight.</div>';
              d.body.appendChild(_ov);
              // Remove the overlay once the real Terraboost header is present,
              // or after a 30s hard timeout (don't trap dispatchers on a broken
              // load forever). MutationObserver checks every DOM mutation for
              // the title element; cheaper than polling.
              var _removeOverlay = function() {
                var _o = d.getElementById('dcc-deploy-overlay');
                if (!_o) return;
                _o.style.opacity = '0';
                setTimeout(function() { try { _o.parentNode.removeChild(_o); } catch (e) {} }, 450);
                w.sessionStorage.removeItem('_dcc_deploy_reload_ts');
              };
              var _seenHeader = function() {
                // Title text "Terraboost Media: Dispatch Command Center" or the
                // login screen heading "Terraboost Media" both count as "real
                // content has rendered."
                var _texts = d.body ? d.body.textContent || '' : '';
                return _texts.indexOf('Dispatch Command Center') >= 0 || _texts.indexOf('SIGN IN') >= 0;
              };
              if (_seenHeader()) {
                // Already rendered before observer attached.
                setTimeout(_removeOverlay, 250);
              } else {
                var _obs = new MutationObserver(function() {
                  if (_seenHeader()) { _obs.disconnect(); _removeOverlay(); }
                });
                _obs.observe(d.body, { childList: true, subtree: true });
                // Hard timeout safety net.
                setTimeout(function() { try { _obs.disconnect(); } catch(e){} _removeOverlay(); }, 30000);
              }
            } else if (_deployAge >= 90000) {
              // Stale flag — clear it so the next non-deploy load doesn't trip.
              w.sessionStorage.removeItem('_dcc_deploy_reload_ts');
            }
          }
        } catch (e) { console.warn('[dcc-deploy-overlay]', e); }

        if (w._dccStaleGuardLoaded) return;
        w._dccStaleGuardLoaded = true;

        function _shouldReload() {
          // Reload-loop circuit breaker. Stored on parent window so it
          // survives across this iframe's lifecycle but not across actual
          // hard reloads (which clear sessionStorage anyway, but we also
          // explicitly check timestamps).
          try {
            var raw = w.sessionStorage.getItem('_dcc_stale_reloads') || '[]';
            var hits = JSON.parse(raw).filter(function(t) {
              return (Date.now() - t) < 60000;
            });
            if (hits.length >= 3) return false;
            hits.push(Date.now());
            w.sessionStorage.setItem('_dcc_stale_reloads', JSON.stringify(hits));
            return true;
          } catch (e) { return true; }
        }

        function _doReload(reason) {
          // Set the deploy-overlay flag right before reload so the next page
          // load picks it up and renders the branded overlay over Streamlit's
          // bare skeleton.
          try { w.sessionStorage.setItem('_dcc_deploy_reload_ts', Date.now().toString()); } catch (e) {}
          console.log('[dcc-stale] hard reload:', reason);
          var u = new URL(w.location.href);
          u.searchParams.set('_r', Date.now().toString());
          w.location.replace(u.toString());
        }

        function _hardReload(reason) {
          if (!_shouldReload()) {
            console.warn('[dcc-stale] reload loop suppressed:', reason);
            return;
          }
          // Pre-warm the new deploy with a tiny /healthz hit before reloading
          // so the new server's Python process is already hot by the time the
          // tab navigates. Shaves several seconds off the Streamlit skeleton
          // phase. Fire-and-forget with a short timeout — never block the
          // reload itself if /healthz is slow or 404s.
          try {
            var _ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
            var _t = setTimeout(function() { try { if (_ctl) _ctl.abort(); } catch(e){} _doReload(reason); }, 2500);
            fetch(w.location.origin + '/?healthz=1', {
              cache: 'no-cache',
              signal: _ctl ? _ctl.signal : undefined,
            }).then(function() {
              clearTimeout(_t);
              _doReload(reason);
            }).catch(function() {
              clearTimeout(_t);
              _doReload(reason);
            });
          } catch (e) { _doReload(reason); }
        }

        // (1) "App was updated" banner — auto-click Reload now.
        // (2) Connection error / Bad message format dialogs — same treatment.
        function _scan() {
          try {
            var doc = w.document;

            // Banner detection — text-content match instead of testid/class.
            // Streamlit hashes its CSS class names and shifts testids between
            // versions; the prior selector list missed the 1.39 native "App
            // was updated — auto-refreshing in 30s" toast entirely, leaving
            // the auto-reload path dead. Mirror the modal-header approach
            // below: walk small leaf-ish elements and match by content.
            // We require BOTH "app was updated" AND "auto-refreshing" so we
            // don't false-positive on DCC's own hidden custom banner
            // (#dcc-update-banner) which carries only the first phrase.
            var _scan_leaves = doc.body ? doc.body.querySelectorAll('span, div, p') : [];
            for (var i = 0; i < _scan_leaves.length; i++) {
              var _bn = _scan_leaves[i];
              if (_bn.children && _bn.children.length > 3) continue;
              var t = (_bn.textContent || '').toLowerCase();
              if (t.indexOf('app was updated') >= 0 && t.indexOf('auto-refreshing') >= 0) {
                if (_bn.closest && _bn.closest('#dcc-update-banner')) continue;
                // Jul 2 2026 — Nick: banner should only fire on REAL code deploys.
                // Streamlit's built-in banner also fires on container restarts (idle wake,
                // healthcheck, OOM), which spammed the dispatcher. Hide it here; our
                // custom banner (INSTANCE_ID-driven, code-hash based) is the sole notifier.
                var _hideTgt = _bn.closest('[data-testid="stAppDeployedNotification"]')
                            || _bn.closest('div[role="alert"]')
                            || _bn.parentElement
                            || _bn;
                try { if (_hideTgt && _hideTgt.style) _hideTgt.style.display = 'none'; } catch(e) {}
                continue;
              }
            }

            // Streamlit's error modals render in a portal; match by the
            // distinctive header text instead of class names (which are
            // hashed and change between Streamlit versions).
            // Option A (Jul 2026): show banner instead of hard-reloading —
            // dispatcher decides when to reload. Uses window._dccShowBanner
            // exposed by dcc_watcher.js when it has loaded; falls back to a
            // no-op if the watcher isn't up yet.
            var headers = doc.querySelectorAll('h1, h2, h3, [class*="StyledModalHeader"]');
            for (var j = 0; j < headers.length; j++) {
              var ht = (headers[j].textContent || '').toLowerCase();
              if (ht.indexOf('bad message format') >= 0 ||
                  ht.indexOf('connection error') >= 0) {
                try {
                  if (typeof w._dccShowBanner === 'function') {
                    w._dccShowBanner("📦 Reconnecting — click Reload now when ready, or Dismiss to stay.");
                  }
                } catch (_) {}
                return;
              }
            }
          } catch (e) {}
        }

        // Poll every 500ms — light, runs once per dispatcher tab. We can't
        // use MutationObserver alone because Streamlit's portal modals are
        // appended to body asynchronously and sometimes after our observer
        // is set up.
        setInterval(_scan, 500);
        _scan();  // also run immediately
      } catch (e) {}
    })();
    </script>
    """,
    height=0,
)
# ============================================================================
# 🔐 LOGIN — per-user authentication
# ============================================================================
# Each entry below is a user account.
#   - username (the dict key, lowercase) is what they type to sign in
#   - password_hash is a salted PBKDF2-HMAC-SHA256 digest (see _hash_password)
#   - pod controls which tabs they can see:
#       'ADMIN'                                -> Global + every pod + Digital (full access)
#       'MANAGER'                              -> Global + every pod + Digital (full access, alias of ADMIN)
#       'Blue'/'Green'/'Orange'/'Purple'/'Red' -> ONLY that single pod tab. No Global, no Digital, no other pods.
#   Global and Digital are gated to ADMIN/MANAGER ONLY by policy. Pod dispatchers
#   land directly on their pod tab and can't see overview / digital views.
#
# To add or rotate a user (Nick, edit this in github.dev):
#   1. Run the helper:  python3 gen_password_hash.py "THEIR_PASSWORD"
#      It prints a fresh "salt" + "password_hash" pair.
#   2. Paste both into that user's entry below (or add a new entry).
#   3. Commit + push -> Railway redeploys -> they can sign in.
USERS = {
    "admin": {
        "name": "Nick Williams",
        "salt": "41658aea82936e38ebd617d6c0058e5a",
        "password_hash": "56bd4d0957a83d203e31d877c3c895693ee56343318436a23927bb94d185b98a",
        "email": "nicholas.byron.williams@gmail.com",
        "pod": "ADMIN",
        "tier": "admin",
    },
    "manager": {
        "name": "Pod Manager",
        "salt": "6939cffef79bd801d8419773f2a9f3a3",
        "password_hash": "43f3e62e61a841a254d2cecfb7468b146534359f9a488e24aa31beeff7ae8cd5",
        "email": "nwilliams@terraboost.biz",
        "pod": "MANAGER",
        "role": "Manager",
        "tier": "manager",
    },
    "digital_user": {
        "name": "Digital User",
        "salt": "744a9204a1b8456ab389229cb81c1e69",
        "password_hash": "2b1898b8b4a3407a6b655b53296bcda0a0951b8d92361035e170c149d5e8fc0c",
        "email": "kheiden@terraboost.biz",
        "pod": "Digital",
        "role": "Dispatcher",
        "tier": "user",
    },
    "bluedispatch1811": {
        "name": "Blue Dispatcher",
        "salt": "46e95e4699615d387e68298d35561439",
        "password_hash": "4fd6c3d9c39ecdd8b9bfaac21d2ba35cab58b876644c5d47cf8275ca83b50989",
        "email": "dcazares@terraboost.biz",
        "pod": "Blue",
        "role": "Dispatcher",
        "tier": "user",
    },
    "greendispatch1811": {
        "name": "Green Dispatcher",
        "salt": "8d8b541f3b87c5dafa9bfa55c74a1eca",
        "password_hash": "cfa2785552a950b615d208140444cb85a3499577818ab3ae6944ad7b89ab01bb",
        "email": "aengelhardt@terraboost.biz",
        "pod": "Green",
        "role": "Dispatcher",
        "tier": "user",
    },
    "orangedispatch1811": {
        "name": "Orange Dispatcher",
        "salt": "9e36cee9f516304b6d52c0a454605041",
        "password_hash": "a4404c082f1a4702bc27e65248fc769fe07bfab4f29d020d994b7a007cf691a2",
        "email": "nwilliams@terraboost.biz",
        "pod": "Orange",
        "role": "Dispatcher",
        "tier": "user",
    },
    "purpledispatch1811": {
        "name": "Purple Dispatcher",
        "salt": "ac9b24e906d7ed7023ca05a38363cf64",
        "password_hash": "6896a20ffa9e25431d5f031541fe7be3499b94505f8adf27f929b1272bc927c4",
        "email": "kgallardo@terraboost.biz",
        "pod": "Purple",
        "role": "Dispatcher",
        "tier": "user",
    },
    "reddispatch1811": {
        "name": "Red Dispatcher",
        "salt": "47dd3a91429348b6bc6ed09c50dc2ef7",
        "password_hash": "3ec6d2168b85be00699c2ba269694066671c6444940452586cbb7483835f0177",
        "email": "mespinoza@terraboost.biz",
        "pod": "Red",
        "role": "Dispatcher",
        "tier": "user",
    },
    "blue_assoc": {
        "name": "Blue Dispatch Associate",
        "salt": "1d30d368b6490b38b945b7d3a45c0483",
        "password_hash": "bb01647f74a33e708cf57d2f25862e0a23c052780d7b2f7581e7b86e2e0953e2",
        "email": "eburger@terraboost.biz",
        "pod": "Blue",
        "role": "Associate",
        "tier": "guest",
    },
    "green_assoc": {
        "name": "Green Dispatch Associate",
        "salt": "21478b35c7869c227a92553445d32c35",
        "password_hash": "ca5841d98f8f0183716a46791afc54cb72a70ab1b3c77defb078475eae394999",
        "email": "reabetswe@terraboost.biz",
        "pod": "Green",
        "role": "Associate",
        "tier": "guest",
    },
    "orange_assoc": {
        "name": "Orange Dispatch Associate",
        "salt": "5db25814784f2963aca0e0403bba1686",
        "password_hash": "eab58a00423e76045e20593e56ef1cbc9bb7554c6313d19fc1931bbe0227a742",
        "email": "bmakaya@terraboost.biz",
        "pod": "Orange",
        "role": "Associate",
        "tier": "guest",
    },
    "purple_assoc": {
        "name": "Purple Dispatch Associate",
        "salt": "3c524a86ae0e5c49e0d72276f7f483e5",
        "password_hash": "0a7d8a5eec5a693dbcbef8ad8d7b81692a0ad50f43844d8b07a2f28eeed465c7",
        "email": "sferreira@terraboost.biz",
        "pod": "Purple",
        "role": "Associate",
        "tier": "guest",
    },
    "red_assoc": {
        "name": "Red Dispatch Associate",
        "salt": "38f491d91e13c087402f90243da7b26a",
        "password_hash": "b3c3d9d6b320ea2c4f25f90f6c1dbf7c1e7c671c61724e07f2047015d35aeb9d",
        "email": "ladams@terraboost.biz",
        "pod": "Red",
        "role": "Associate",
        "tier": "guest",
    },
}

import hashlib as _login_hashlib
import hmac as _login_hmac

# \U0001F512 Password storage — salted PBKDF2-HMAC-SHA256 (security audit H2).
# Each USERS entry carries a per-user random `salt`; `password_hash` is the
# PBKDF2 digest. Use gen_password_hash.py to make new salt+hash pairs.
_PBKDF2_ITERS = 200000

def _hash_password(password: str, salt_hex: str) -> str:
    # Salted PBKDF2-HMAC-SHA256 digest, returned as hex.
    try:
        _salt = bytes.fromhex(str(salt_hex or ""))
    except Exception:
        return ""
    if not _salt:
        return ""
    return _login_hashlib.pbkdf2_hmac(
        "sha256", str(password or "").encode("utf-8"), _salt, _PBKDF2_ITERS
    ).hex()

def _check_password(username: str, password: str):
    # Returns the user record dict on match, else None. Constant-time compare.
    rec = USERS.get(str(username or "").lower().strip())
    if not rec:
        return None
    _salt = rec.get("salt", "")
    _expected = rec.get("password_hash", "")
    if not _salt or not _expected:
        return None
    _candidate = _hash_password(password, _salt)
    if _candidate and _login_hmac.compare_digest(_candidate, _expected):
        return rec
    return None

# ─────────────────────────────────────────────────────────────────────────────
# 🍪 STAY-SIGNED-IN — localStorage-backed persistence across browser sessions
# ─────────────────────────────────────────────────────────────────────────────
# How it works:
#   1. User signs in. Right after, we ask "Stay signed in on this device?".
#   2. If yes, we generate a deterministic token (SHA-256 of username + salt)
#      and write it to the browser's localStorage via an injected JS snippet.
#      We also append it to the URL as ?auth=TOKEN so the current tab stays
#      signed in across hard refreshes.
#   3. On every page load, a tiny JS snippet checks localStorage; if a token is
#      there but not in the URL, it appends ?auth=TOKEN and reloads. The Python
#      side then validates the token and auto-restores the auth user.
#   4. Sign out clears both the URL param AND the localStorage entry.
#
# Security (audit C1): STAY_SALT is a SERVER-ONLY secret read from the
# environment — it is NOT stored in source. The persistent-login token is
# sha256(username | password_hash | STAY_SALT): it cannot be forged without
# the server salt AND the user's stored password hash, and it auto-invalidates
# whenever the password changes. If STAY_SALT is unset, stay-signed-in is
# disabled (fail-closed) but the app still works — users sign in each session.
# Set STAY_SALT in Railway -> Variables to a long random string.
STAY_SALT = (os.environ.get("STAY_SALT") or "").strip()

def _stay_token_for(username: str) -> str:
    # Unforgeable persistent-login token, or '' if stay-signed-in is disabled.
    _uname = str(username or "").lower().strip()
    rec = USERS.get(_uname)
    if not rec or not STAY_SALT:
        return ""
    return _login_hashlib.sha256(
        f"{_uname}|{rec.get('password_hash','')}|{STAY_SALT}".encode("utf-8")
    ).hexdigest()

def _validate_stay_token(token: str):
    # Returns (username, record) if token matches a known user, else None.
    if not token or not STAY_SALT:
        return None
    for u, rec in USERS.items():
        _expected = _stay_token_for(u)
        if _expected and _login_hmac.compare_digest(_expected, str(token)):
            return u, rec
    return None

def _user_role() -> str:
    """Return the current user's role string (e.g. 'Dispatcher', 'Associate', 'Manager') or '' if not logged in / no role set."""
    return str(st.session_state.get('_auth_user', {}).get('role', '')).strip()


def _user_tier() -> str:
    """Return the current user's tier — 'admin', 'manager', 'user', or 'guest'.
    Canonical permission model:
      admin   -> full access (Nick)
      manager -> full access (separate login)
      user    -> Dispatcher: pod-locked, sees full pod tab
      guest   -> Dispatch Associate: pod-locked, FN sub-tab only, no revoke buttons
    Falls back to inferring tier from pod/role for legacy records that don't
    yet have an explicit `tier` field."""
    user = st.session_state.get('_auth_user', {})
    explicit = str(user.get('tier', '')).strip().lower()
    if explicit:
        return explicit
    pod_u = str(user.get('pod', '')).strip().upper()
    role_u = str(user.get('role', '')).strip().lower()
    if pod_u in ('ADMIN', 'ALL'):
        return 'admin'
    if pod_u == 'MANAGER':
        return 'manager'
    if role_u == 'associate':
        return 'guest'
    return 'user'


def _is_dispatch_associate() -> bool:
    """True if the signed-in user is a guest tier (Dispatch Associate). Limited UI:
    FN-only sub-tab, no revoke buttons, can use Shopify."""
    return _user_tier() == 'guest'


def _is_admin_or_manager() -> bool:
    """True for admin and manager tiers — full UI surface, see everything."""
    return _user_tier() in ('admin', 'manager')


def _user_pod_raw() -> str:
    """Return the user's pod assignment string as-stored (e.g. 'Blue', 'ADMIN')."""
    return str(st.session_state.get('_auth_user', {}).get('pod', '')).strip()


def _can_access_tab(tab_pod: str) -> bool:
    """tab_pod ∈ {'Global','Blue','Green','Orange','Purple','Red','Digital'}.
    ADMIN/MANAGER see every tab. Pod dispatchers (Blue/Green/...) see ONLY their
    pod tab — Global and Digital are gated off limits to them by policy."""
    user = st.session_state.get('_auth_user')
    if not user:
        return False
    user_pod = str(user.get('pod', '')).upper()
    # ADMIN and MANAGER both get full access. ALL is kept as a legacy alias.
    if user_pod in ('ADMIN', 'MANAGER', 'ALL'):
        return True
    # Global stays admin-only. Pod-locked users (incl. Digital) see only their
    # assigned tab — Blue/Green/Orange/Purple/Red dispatchers see their pod;
    # Digital users see the Digital tab.
    if tab_pod == 'Global':
        return False
    return str(user.get('pod', '')) == tab_pod

def _render_login_form():
    """Renders the login form and stops the script if not authenticated."""
    # Login title block — pulled up into the upper third of the viewport.
    # Streamlit's natural padding alone pushes it to mid-page; we trim that
    # padding modestly (not all the way to the top — that looked cramped).
    st.markdown(
        """
        <style>
          /* Trim Streamlit's main container top padding so the login starts higher */
          [data-testid="stMainBlockContainer"],
          .main .block-container {
            padding-top: 16px !important;
          }
          .dcc-login-wrap {
            text-align: center;
            width: 100%;
            margin: 0 auto 12px auto;
          }
          .dcc-login-wrap h1 {
            color: #633094;
            margin: 0 0 6px 0;
            font-weight: 800;
            font-size: 32px;
          }
          .dcc-login-wrap p {
            color: #64748b;
            margin: 0 0 16px 0;
            font-size: 14px;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            font-weight: 700;
          }
        </style>
        <div class="dcc-login-wrap">
          <h1>Terraboost Media</h1>
          <p>Dispatch Command Center · Sign in</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _spacer_l, col, _spacer_r = st.columns([1, 2, 1])
    with col:
        with st.form("_login_form", clear_on_submit=False):
            _username = st.text_input("Username", key="_login_user_input", autocomplete="username")
            _password = st.text_input("Password", type="password", key="_login_pw_input", autocomplete="current-password")
            _submitted = st.form_submit_button("Sign in", use_container_width=True, type="primary")
            if _submitted:
                # \U0001F512 Login throttling (security audit M2) — exponential
                # backoff after repeated failures, tracked per session.
                _now_ts = time.time()
                _lock_until = float(st.session_state.get('_login_lock_until', 0) or 0)
                if _now_ts < _lock_until:
                    _wait = int(_lock_until - _now_ts) + 1
                    st.error(f"Too many failed attempts. Please wait {_wait} second(s) before trying again.")
                else:
                    _rec = _check_password(_username, _password)
                    if _rec:
                        st.session_state['_login_fails'] = 0
                        st.session_state['_login_lock_until'] = 0
                        _u_clean = str(_username).lower().strip()
                        st.session_state['_auth_user'] = {**_rec, 'username': _u_clean}
                        # ?auth=TOKEN lets this tab survive a Railway redeploy.
                        # Skipped silently if stay-signed-in is disabled (no STAY_SALT).
                        try:
                            _tok = _stay_token_for(_u_clean)
                            if _tok:
                                st.query_params['auth'] = _tok
                        except Exception:
                            pass
                        st.rerun()
                    else:
                        _fails = int(st.session_state.get('_login_fails', 0) or 0) + 1
                        st.session_state['_login_fails'] = _fails
                        if _fails >= 5:
                            _delay = min(600, 30 * (2 ** (_fails - 5)))
                            st.session_state['_login_lock_until'] = _now_ts + _delay
                            st.error(f"Too many failed attempts. Locked for {_delay} second(s).")
                        else:
                            st.error(f"Invalid username or password. {5 - _fails} attempt(s) left before lockout.")

# --- PINNED TOP-LEFT LOGO ---
# Function to convert the local image into web-safe code
def get_base64_image(image_path):
    try:
        with open(image_path, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode()
    except Exception as e:
        return ""

# Make sure "terraboost_logo.png" perfectly matches your saved file name!
logo_base64 = get_base64_image("terraboost_logo.png")

if logo_base64:
    st.markdown(f"""
        <div style="position: fixed; top: 15px; left: 20px; z-index: 999999;">
            <img src="data:image/png;base64,{logo_base64}" style="width: 140px;"> 
        </div>
    """, unsafe_allow_html=True)
else:
    st.sidebar.error("Logo file not found! Check the file name.")

# --- UI STYLING ---
# 🛡️ SessionInfo / Bad-message auto-refresh — DISABLED Jul 2026 (Nick, Option A).
# Replaced by banner-only behavior in dcc_watcher.js. When a Streamlit error
# modal appears, dcc_watcher.js hides it and shows the DCC banner; the
# dispatcher clicks "Reload now" when they're ready. No silent reloads.

st.components.v1.html("""
<script>
(function() {
    var SCROLL_KEY = 'tbm_scroll_pos';

    // Save scroll position on every scroll
    window.parent.document.addEventListener('scroll', function() {
        sessionStorage.setItem(SCROLL_KEY, window.parent.scrollY);
    }, { passive: true });

    // Restore scroll position whenever Streamlit rerenders
    var observer = new MutationObserver(function() {
        var saved = sessionStorage.getItem(SCROLL_KEY);
        if (saved && parseInt(saved) > 50) {
            window.parent.scrollTo({ top: parseInt(saved), behavior: 'instant' });
        }
    });
    observer.observe(window.parent.document.body, { childList: true, subtree: false });
})();
</script>
""", height=0)

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap'); .dcc-state-header {{ font-size: 12px !important; font-weight: 800 !important; color: #94a3b8 !important; margin-top: 15px !important; margin-bottom: 5px !important; border-bottom: 1px solid #e2e8f0 !important; padding-bottom: 2px !important; text-transform: uppercase !important; letter-spacing: 1px !important; cursor: pointer !important; user-select: none !important; display: flex !important; align-items: center !important; gap: 6px !important; }} .dcc-state-chevron {{ display: inline-block; font-size: 11px; color: #94a3b8; transition: transform 0.15s ease; }} .dcc-state-header.dcc-expanded .dcc-state-chevron {{ transform: rotate(90deg); }} .dcc-state-hidden {{ display: none !important; }}
.stApp {{ background-color: {TB_APP_BG} !important; color: #000000 !important; font-family: 'Inter', sans-serif !important; }}
/* Streamlit injects a fixed-position header bar by default with a dark/black background.
   Recolor it to match the page so the logo doesn't sit on a black strip. Header still
   exists (Streamlit needs it for menu/toolbar), it's just visually invisible now. */
/* Kill Streamlit's sticky top header bar so the title/tabs sit right at the viewport top.
   We don't use Streamlit's toolbar (deploy/share/share menu) so zero footprint is correct. */
header[data-testid="stHeader"] {{ height: 0 !important; min-height: 0 !important; padding: 0 !important; background: transparent !important; }}
header[data-testid="stHeader"] [data-testid="stToolbar"] {{ display: none !important; }}
header[data-testid="stHeader"]::before {{ background-color: {TB_APP_BG} !important; }}
div[data-testid="stToolbar"] {{ display: none !important; }}
.main .block-container,
[data-testid="stMainBlockContainer"] {{ max-width: 1400px !important; padding-top: 8px !important; padding-left: 1.5rem !important; padding-right: 1.5rem !important; }}

/* =========================================
   PHANTOM-WHITESPACE FIX (#1) — collapse stElementContainer wrappers
   that only hold a height=0 iframe. Without this, every
   st.components.v1.html(..., height=0) at the top of the script
   (scroll-saver, deploy watcher, SessionInfo watcher, etc.) ends up
   contributing ~42px of vertical space (26px container + 16px flex gap)
   because the parent stVerticalBlock applies its gap to every flex
   child regardless of content height. Result: ~230px of empty space
   above the page title, which gets worse after popover interaction
   because Streamlit re-mounts the iframes during cleanup.
   Targeting [height="0"] is precise — non-zero iframes (maps, packing
   slip button, etc.) keep their layout.
   ========================================= */
.stElementContainer:has(> iframe.stIFrame[height="0"]) {{ display: none !important; }}

/* =========================================
   DESTRUCTIVE BUTTON RED (#6) — buttons whose label starts with "🚨 "
   (Yes, Re-Route / Yes, Remove / Yes, Revoke FN) live inside revoke
   popovers. Streamlit's type="primary" theme renders them green, which
   contradicts the danger signal. Override to red so the visual matches
   the action.
   ========================================= */
.stPopover button[kind="primary"],
[data-baseweb="popover"] button[kind="primary"] {{
    background-color: #dc2626 !important;
    border-color: #b91c1c !important;
    color: #ffffff !important;
}}
.stPopover button[kind="primary"]:hover,
[data-baseweb="popover"] button[kind="primary"]:hover {{
    background-color: #b91c1c !important;
    border-color: #991b1b !important;
}}

/* =========================================
   WIDGET & INPUT STANDARDIZATION (Fixes the White Box Glitch)
   ========================================= */
/* Force clean white backgrounds on all inputs */
div[data-baseweb="select"] > div,
div[data-baseweb="input"],
div[data-baseweb="input"] > div {{
    background-color: #ffffff !important;
    border-color: #cbd5e1 !important;
}}

/* Ensure text inside inputs is dark and legible */
input[type="text"], 
input[type="number"], 
div[data-baseweb="select"] div {{
    color: #0f172a !important;
    -webkit-text-fill-color: #0f172a !important;
    font-weight: 600 !important;
}}

/* Number Input — match date input outline style */
div[data-testid="stNumberInputContainer"] {{
    border-radius: 8px !important;
    border: 1px solid #cbd5e1 !important;
    background-color: #ffffff !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
    overflow: hidden !important;
}}

div[data-testid="stNumberInputContainer"]:focus-within {{
    border-color: #633094 !important;
    box-shadow: 0 0 0 2px rgba(99,48,148,0.15) !important;
}}

/* Kills the white box by forcing transparency on the button wrapper */
div[data-testid="stNumberInputContainer"] div[data-baseweb="input"] > div:nth-child(2) {{
    background-color: transparent !important;
}}

/* Style the + / - icons to match the theme */
div[data-testid="stNumberInputContainer"] button, 
div[data-testid="stNumberInputContainer"] svg {{
    color: #64748b !important;
    fill: #64748b !important;
    background-color: transparent !important;
}}

/* Email Content Preview (st.text_area) — visible border around the preview */
div[data-testid="stTextArea"] textarea,
div[data-testid="stTextAreaRootElement"] textarea,
div[data-baseweb="textarea"] {{
    border: 1px solid #cbd5e1 !important;
    border-radius: 8px !important;
    background-color: #ffffff !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
}}

div[data-testid="stTextArea"] textarea:focus,
div[data-baseweb="textarea"]:focus-within {{
    border-color: #633094 !important;
    box-shadow: 0 0 0 2px rgba(99,48,148,0.15) !important;
}}

/* GLOBAL TABS CONTAINER - Clean & Floating with Bottom Line */
.stTabs [data-baseweb="tab-list"] {{ 
    justify-content: center; 
    gap: 12px; 
    background: transparent !important; /* Removes the gray box background */
    padding: 15px 15px 20px 15px !important; /* Adds extra padding on the bottom so pills don't touch the line */
    border-bottom: 2px solid #cbd5e1 !important; /* 🌟 THIS IS THE HORIZONTAL LINE 🌟 */
    margin-bottom: 15px !important; /* Pushes the dashboard content down slightly for breathing room */
}}

/* CENTERED PURPLE HEADERS */
h1, h2, h3, h4, h5, h6 {{ 
    font-weight: 800 !important; 
    text-align: center !important; 
    width: 100%;
}}

/* MODERN CONDENSED REFRESH BUTTON - FAR RIGHT */
div.refresh-btn-container {{
    display: flex;
    justify-content: flex-end;
    width: 100%;
}}

div.refresh-btn-container > div > button {{
    height: 32px !important; /* Slightly taller for breathing room */
    padding: 0 16px !important;
    font-size: 13px !important;
    border-radius: 20px !important;
    border: 1.2px solid #633094 !important;
    background-color: transparent !important;
    color: #633094 !important;
    font-weight: 700 !important;
    transition: all 0.2s ease-in-out !important;
    
    /* THE FIX: Forces icon and text onto one line perfectly centered */
    white-space: nowrap !important; 
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
}}

/* Ensures Streamlit's internal text wrapper doesn't force a line break */
div.refresh-btn-container > div > button div,
div.refresh-btn-container > div > button p {{
    white-space: nowrap !important;
    margin: 0 !important;
    padding: 0 !important;
}}
div.refresh-btn-container > div > button:hover {{
    background-color: #633094 !important;
    color: white !important;
    box-shadow: 0 2px 8px rgba(99, 48, 148, 0.3) !important;
}}

/* GLOBAL TABS STYLING */
.stTabs [data-baseweb="tab-list"] {{ justify-content: center; gap: 8px; background: rgba(255,255,255,0.6); padding: 10px; border-radius: 15px; }}

/* PERMANENT POD TAB OUTLINES & DARK TEXT */
.stTabs [data-baseweb="tab"] {{
    border-top: 1px solid #cbd5e1 !important;
    border-left: 1px solid #cbd5e1 !important;
    border-right: 1px solid #cbd5e1 !important;
    margin: 0 4px !important;
    transition: all 0.2s ease !important;
    font-weight: 800 !important;
    border-radius: 10px 10px 0 0 !important;
    padding: 10px 20px !important;
}}

/* GLOBAL TABS CONTAINER - Clean & Floating */
.stTabs [data-baseweb="tab-list"] {{ 
    justify-content: center; 
    gap: 12px; 
    background: transparent !important; /* Removes the gray box background */
    padding: 15px; 
}}

/* KILL THE DEFAULT UNDERLINE (The "Cutoff" source) */
.stTabs [data-baseweb="tab-highlight"] {{
    background-color: transparent !important;
}}

/* PERMANENT FLOATING PILLS - No flat bottoms */
.stTabs [data-baseweb="tab"] {{
    border-radius: 30px !important; /* Full rounded pill */
    margin: 0 5px !important;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
    font-weight: 800 !important;
    padding: 8px 25px !important;
    border: 2px solid transparent !important; /* Invisible border until set below */
}}

/* Global Tab */
.stTabs [data-baseweb="tab"]:nth-of-type(1) {{ border: 2px solid #633094 !important; color: #3b1d58 !important; background: white !important; }}
/* Blue Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(2) {{ border: 2px solid #3b82f6 !important; background-color: #f0f7ff !important; color: #1e3a8a !important; }}
/* Green Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(3) {{ border: 2px solid #22c55e !important; background-color: #f0fdf4 !important; color: #064e3b !important; }}
/* Orange Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(4) {{ border: 2px solid #f97316 !important; background-color: #fffaf5 !important; color: #7c2d12 !important; }}
/* Purple Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(5) {{ border: 2px solid #a855f7 !important; background-color: #faf5ff !important; color: #4c1d95 !important; }}
/* Red Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(6) {{ border: 2px solid #ef4444 !important; background-color: #fef2f2 !important; color: #7f1d1d !important; }}
/* Digital Pool Tab */
.stTabs [data-baseweb="tab"]:nth-of-type(7) {{ border: 2px solid #0f766e !important; color: #0f766e !important; background: #ccfbf1 !important; }}

/* ACTIVE STATE - The "Full Glow" (No flat bottom border) */
.stTabs [aria-selected="true"] {{ 
    background-color: #ffffff !important;
    transform: translateY(-4px) !important; /* Removed the scale(1.05) so it matches cards perfectly */
    box-shadow: 0 10px 20px rgba(99, 48, 148, 0.25) !important; 
}}

/* TAB ACTION BUTTONS (Top Right - Initialize / Sync) */
div.tab-action-btn {{
    display: flex;
    justify-content: flex-end;
    width: 100%;
    margin-top: 0px !important;
}}
div.tab-action-btn > div > button {{
    height: 32px !important;
    padding: 0 24px !important; /* Slightly wider as requested */
    font-size: 13px !important;
    border-radius: 20px !important;
    border: 1.2px solid #633094 !important;
    background-color: transparent !important;
    color: #633094 !important;
    font-weight: 700 !important;
    transition: all 0.2s ease-in-out !important;
    white-space: nowrap !important; 
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
}}
div.tab-action-btn > div > button:hover {{
    background-color: #633094 !important;
    color: white !important;
    box-shadow: 0 2px 8px rgba(99, 48, 148, 0.3) !important;
}}

/* PRIMARY & SECONDARY BUTTONS */
button[kind="primary"] {{
    background-color: #76bc21 !important;
    color: white !important;
    height: 3.5rem !important;
    font-size: 1.2rem !important;
    font-weight: 800 !important;
    border: none !important;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1) !important;
    transition: all 0.2s ease !important;
}}

button[kind="secondary"] {{
    background-color: #ffffff !important;
    color: {TB_PURPLE} !important;
    border: 2px solid {TB_PURPLE} !important;
    height: 42px !important;
    font-size: 0.9rem !important;
    font-weight: 800 !important;
    border-radius: 8px !important;
    box-shadow: 0 2px 4px rgba(0,0,0,0.05) !important;
    transition: all 0.2s ease !important;
}}

/* =========================================
   1. SCISSORS BUTTON (INSIDE EXPANDER)
   ========================================= */
div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(2) button {{
    margin-top: 2px !important;
    transform: scale(1.1) !important;
    transform-origin: center right !important;
    padding: 0 6px !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
    color: #ef4444 !important;
    font-weight: 900 !important;
    font-size: 26px !important;
    line-height: 1 !important;
}}

/* =========================================
   2. REVOKE / RE-ROUTE BUTTON — small pill
   ========================================= */
/* 📌 Revoke/re-route popover — the ↩️ emoji IS the button.
   Streamlit's actual DOM (verified live, Apr 27 2026):
     stPopover > div(unnamed) > button[stPopoverButton] > div(flex) > [label][caret]
   So we use `button[data-testid="stPopoverButton"]` (descendant, not direct-child) and
   target the caret via [data-testid="stIconMaterial"]. The :has() guard keeps the
   styling scoped to the btn_col next to an expander column — leaves the standalone
   "Confirm Field Nation Revocation" popover untouched. */

/* Strip the popover wrapper + its unnamed inner div of any chrome/spacing. */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) div[data-testid="stPopover"],
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) div[data-testid="stPopover"] > div {{
    padding: 0 !important;
    margin: 0 !important;
    background: transparent !important;
    border: none !important;
    width: auto !important;
    box-shadow: none !important;
}}

/* The button itself: wipe Streamlit defaults, size to the emoji glyph. */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"] {{
    all: unset !important;
    cursor: pointer !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    width: auto !important;
    height: auto !important;
    min-width: 0 !important;
    min-height: 0 !important;
    padding: 0 !important;
    margin: 4px 0 0 0 !important;
    font-size: 18px !important;
    line-height: 1 !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    transition: transform 0.15s ease !important;
}}

/* Strip every nested div/span/p inside the button — these carry padding/gap. */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"] div,
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"] span,
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"] p {{
    padding: 0 !important;
    margin: 0 !important;
    gap: 0 !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    line-height: 1 !important;
}}

/* Kill the dropdown caret (Streamlit renders it as <span data-testid="stIconMaterial">expand_more</span>). */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"] [data-testid="stIconMaterial"],
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"] svg {{
    display: none !important;
}}
/* And collapse the empty caret-container div so it doesn't reserve gap space. */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"] > div > div:last-child {{
    display: none !important;
}}

/* Hover: scale only — no pill, no border, no shadow, no purple. */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"]:hover,
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"]:focus,
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) button[data-testid="stPopoverButton"]:active {{
    transform: scale(1.25) !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: inherit !important;
    outline: none !important;
}}

/* Main Expander Container */
div[data-testid="stExpander"] {{ 
    border: 1px solid #cbd5e1 !important; 
    border-radius: 10px !important; 
    box-shadow: 0 2px 4px rgba(0,0,0,0.02) !important;
    margin-bottom: 8px !important;
    background-color: #ffffff !important;
    overflow: hidden !important;
}}

/* Header text color */
div[data-testid="stExpander"] details summary p {{ 
    color: #000000 !important; 
    font-weight: 800 !important; 
    font-size: 0.85rem !important;
}}

/* 🚀 FIX: STOP THE DARK HOVER & BLACK CLICK FILL */
div[data-testid="stExpander"] details summary {{
    background-color: #ffffff !important; /* Force base color */
    transition: background-color 0.2s ease !important;
}}

div[data-testid="stExpander"] details summary:hover {{
    background-color: #fcfaff !important; /* Very light purple on hover */
}}

/* This targets the exact moment you click it */
div[data-testid="stExpander"] details summary:active {{
    background-color: #ffffff !important; 
}}

/* This removes the "Black/Gray Box" focus state that stays after clicking */
div[data-testid="stExpander"] details summary:focus, 
div[data-testid="stExpander"] details summary:focus-visible {{
    background-color: #ffffff !important;
    outline: none !important;
    box-shadow: none !important;
}}

/* Ensure the text stays visible during the click */
div[data-testid="stExpander"] details summary:hover p,
div[data-testid="stExpander"] details summary:active p,
div[data-testid="stExpander"] details summary:focus p {{
    color: #633094 !important;
}}

label, div[data-testid="stWidgetLabel"] p {{ color: #000000 !important; font-weight: 600 !important; }}

/* MAP & FOLIUM */
iframe[title="streamlit_folium.st_folium"] {{
    border-radius: 15px !important;
    box-shadow: 0 4px 6px rgba(0,0,0,0.05) !important;
    max-height: 420px !important;
    height: 400px !important;
}}
.stFolium {{ background: transparent !important; }}

/* =========================================
   UNIFIED HOVER & CLICK EFFECTS
   ========================================= */

/* 1. BUTTONS: Lift + Purple Glow */
button[kind="primary"]:hover,
button[kind="secondary"]:hover,
div.refresh-btn-container > div > button:hover {{
    transform: translateY(-4px) !important;
    box-shadow: 0 12px 28px rgba(99, 48, 148, 0.35) !important;
    border-color: #633094 !important;
    z-index: 10;
}}

/* 2. CARDS, TABS & EXPANDERS: Lift + Neutral Drop Shadow (No Purple) */
div[data-testid="stExpander"]:hover,
.pod-card-pill:hover,
.dashboard-supercard:hover,
.stTabs [data-baseweb="tab"]:hover {{
    transform: translateY(-4px) !important;
    box-shadow: 0 12px 24px rgba(0, 0, 0, 0.08) !important; 
    z-index: 10;
}}

/* 3. STRICT CLICK ANIMATION (Kills the "Push In" effect) */
/* Forces all elements to just drop back to baseline smoothly when clicked */
button[kind="primary"]:active,
button[kind="secondary"]:active,
div.refresh-btn-container > div > button:active,
div[data-testid="stExpander"] details summary:active,
.pod-card-pill:active,
.dashboard-supercard:active,
.stTabs [data-baseweb="tab"]:active {{
    transform: translateY(0px) scale(1) !important; 
    box-shadow: 0 2px 4px rgba(0,0,0,0.05) !important;
}}

/* Smooth transitions for everything */
div[data-testid="stExpander"],
div[data-testid="stExpander"] details summary,
.pod-card-pill,
.dashboard-supercard,
button[kind="primary"],
button[kind="secondary"],
div.refresh-btn-container > div > button,
.stTabs [data-baseweb="tab"] {{
    transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1) !important;
}}

/* =========================================
   SUB-TAB PILL STYLING (Column-Targeting Method)
   ========================================= */

/* 🌟 SIZE OVERRIDES for both Dispatch + Awaiting columns.
   Default Streamlit tabs were too padded to fit 4 pills inside a half-page column at
   100% zoom — text overflow forced horizontal scroll arrows on the sides. Tighter
   padding, smaller font, no-wrap, and hidden scroll arrows put them all on one line. */
div[data-testid="stColumn"] div[data-testid="stTabs"] [data-baseweb="tab-list"] {{
    overflow-x: hidden !important;
    flex-wrap: nowrap !important;
    gap: 6px !important;
    padding: 8px !important;
}}
div[data-testid="stColumn"] div[data-testid="stTabs"] [data-baseweb="tab"] {{
    padding: 6px 16px !important;
    min-width: 0 !important;
    flex-shrink: 1 !important;
    flex-basis: auto !important;
}}
div[data-testid="stColumn"] div[data-testid="stTabs"] [data-baseweb="tab"] p {{
    font-size: 13px !important;
    white-space: nowrap !important;
    margin: 0 !important;
}}
/* Hide the < > scroll buttons that show up when content overflows */
div[data-testid="stColumn"] div[data-testid="stTabs"] button[role="button"][aria-label*="scroll"],
div[data-testid="stColumn"] div[data-testid="stTabs"] [data-baseweb="tab-border"] + div > button {{
    display: none !important;
}}

/* --- LEFT COLUMN: Dispatch Tabs --- */
/* 1. Ready (Green) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) {{
    background-color: #dcfce7 !important;
    border: 2.5px solid #166534 !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) p {{
    color: #166534 !important; 
}}

/* 2. Flagged (Red) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) {{
    background-color: #fee2e2 !important;
    border: 2.5px solid #991b1b !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) p {{
    color: #991b1b !important; 
}}

/* 3. Field Nation (Light Yellow BG / Dark Yellow Text) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) {{
    background-color: #fef9c3 !important;
    border: 2.5px solid #854d0e !important;
    border-radius: 30px !important;
    margin: 0 5px !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) p {{
    color: #854d0e !important;
    font-weight: 800 !important;
}}

/* 4. Digital (Teal - Left Column) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) {{
    background-color: #ccfbf1 !important;
    border: 2.5px solid #0f766e !important;
    border-radius: 30px !important;
    margin: 0 5px !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) p {{
    color: #0f766e !important;
    font-weight: 800 !important;
}}

/* --- RIGHT COLUMN: Awaiting Tabs --- */
/* Force the gap, center the pills, and stop stretching */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab-list"] {{
    gap: 12px !important;
    justify-content: center !important; 
}}

/* 🌟 RESTORE PILL SIZE: Inherit global sizing but prevent stretching */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"] {{
    flex-grow: 0 !important; /* Kills the stretching bloat */
}}

div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"] p {{
    white-space: nowrap !important;
    font-weight: 800 !important; /* Matches left column boldness */
}}

/* 1. Sent (Purple/Blue) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) {{
    background-color: #f3e8ff !important;
    border: 2.5px solid #633094 !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) p {{
    color: #633094 !important; 
}}

/* 2. Accepted (Green) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) {{
    background-color: #dcfce7 !important;
    border: 2.5px solid #166534 !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) p {{
    color: #166534 !important; 
}}

/* 3. Declined (Red) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) {{
    background-color: #fee2e2 !important;
    border: 2.5px solid #991b1b !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) p {{
    color: #991b1b !important; 
}}

/* 4. Finalized (Orange) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) {{
    background-color: #fffaf5 !important;
    border: 2.5px solid #f97316 !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) p {{
    color: #7c2d12 !important; 
}}

/* ALIGN COLUMNS AT THE TOP (Fixes the giant gap on the left) */
div[data-testid="stHorizontalBlock"] {{ align-items: flex-start !important; }}

/* TIGHTEN GAPS BETWEEN CARDS */
div[data-testid="stVerticalBlock"] {{ gap: 1rem !important; }}

/* Stop remover multiselect — compact, same size as expansion rows */
div[data-testid="stMultiSelect"] {{
    font-size: 11px !important;
}}
div[data-testid="stMultiSelect"] [data-baseweb="select"] > div {{
    min-height: 32px !important;
    font-size: 11px !important;
    padding: 2px 6px !important;
}}
div[data-testid="stMultiSelect"] [data-baseweb="tag"] {{
    font-size: 10px !important;
    height: 20px !important;
    padding: 0 6px !important;
}}



/* Collapse gap between consecutive stop row columns inside expanders */
div[data-testid="stExpander"] div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"] + div[data-testid="stHorizontalBlock"] {{
    margin-top: -14px !important;
}}

div[data-testid="stExpander"] {{ margin-top: 0px !important; margin-bottom: 2px !important; }}

/* Compact button inside revoke/re-route popover. The popover panel is portaled to a
   body-level [data-baseweb="popover"] layer, so we target that. Keeps the green button
   short instead of towering over the two-line prompt. */
[data-baseweb="popover"] button[kind="primary"] {{
    height: 34px !important;
    min-height: 34px !important;
    padding: 4px 12px !important;
    font-size: 13px !important;
    line-height: 1 !important;
    border-radius: 8px !important;
}}

/* MINI REVOKE BUTTON (Single Line, Right Aligned) */
div.mini-btn button {{
    height: 30px !important;
    min-height: 30px !important;
    padding: 0 8px !important;
    font-size: 11px !important;
    white-space: nowrap !important; /* CRITICAL: Stops "Revoke" from dropping to a second line */
    float: right !important;
    margin-top: 4px !important;
    border-radius: 4px !important;
}}


/* =========================================
   MULTISELECT — hide empty "No results" dropdown
   =========================================
   When every option has been selected, Streamlit's multiselect dropdown shows
   an empty popup with "No results" centered text and stays open until the user
   clicks elsewhere. Hide that empty popup. Selector: a baseweb popover that
   CONTAINS a listbox that has zero real options. The `:has([role=listbox])`
   guard scopes this to multiselect dropdowns specifically — st.popover
   containers (revoke confirmation prompts, etc.) don't contain a listbox so
   they're untouched. Also covers the case where the listbox renders as <ul>
   or <div> — both forms accepted. */
div[data-baseweb="popover"]:has([role="listbox"]):not(:has([role="option"])) {{
    display: none !important;
}}
[role="listbox"]:not(:has([role="option"])) {{
    display: none !important;
}}

@media (max-width: 768px) {{
    div[data-testid="stHorizontalBlock"] {{ flex-direction: column !important; }}
    div[data-testid="stColumn"] {{ width: 100% !important; min-width: 100% !important; flex: 1 1 100% !important; }}
    .main .block-container {{ padding-left: 0.5rem !important; padding-right: 0.5rem !important; padding-top: 0.5rem !important; }}
    .stTabs [data-baseweb="tab"] {{ padding: 6px 10px !important; font-size: 11px !important; border-radius: 20px !important; }}
    iframe[title="streamlit_folium.st_folium"] {{ height: 250px !important; }}
    .dashboard-supercard {{ height: auto !important; margin-bottom: 8px !important; }}
    div[data-testid="stExpander"] details summary p {{ font-size: 0.75rem !important; }}
    h1 {{ font-size: 1.3rem !important; }}
    h2 {{ font-size: 1.1rem !important; }}
}}
</style>
""", unsafe_allow_html=True)

# --- 1. BACKGROUND THREAD WORKER ---
def background_sheet_move(cluster_hash, payload_json, task_ids=None, action_label="Revoked", ic_name="", wo=None):
    """Archive route state in Railway/Postgres, then scrub OnFleet."""
    _archive_ok = False
    if DB_ENGINE is not None and wo:
        try:
            _da.archive_route(DB_ENGINE, wo, {"action_label": action_label, "ic_name": ic_name})
            _archive_ok = True
        except Exception as _dw_e:
            _log_err("postgres_archive_route", _dw_e)
    if not _archive_ok:
        try:
            _RECONCILE_FAILURES.add(str(cluster_hash))
        except Exception:
            pass
    # Onfleet scrub: actually UNASSIGN the worker now (was a no-op GET previously).
    # Sets worker=null and clears WO/PAY metadata so the task returns to the team pool.
    if task_ids:
        try:
            auth = {"Authorization": f"Basic {base64.b64encode(f'{ONFLEET_KEY}:'.encode()).decode()}"}
            scrub_payload = json.dumps({"worker": None, "metadata": []})
            # Apr 27 2026 — bookend the scrub loop with start/end log lines so we can
            # tell if the loop is running at all (silent no-PUT vs. PUTs returning 200).
            _scrub_total = len(task_ids)
            _scrub_ok = 0
            _log_err("background_sheet_move/scrub-start", f"scrubbing {_scrub_total} tasks for cluster {cluster_hash}")
            def _do_scrub(tid):
                try:
                    _r = requests.put(
                        f"https://onfleet.com/api/v2/tasks/{tid}",
                        headers={**auth, "Content-Type": "application/json"},
                        data=scrub_payload,
                        timeout=10,
                    )
                    if _r.status_code != 200:
                        _body = ""
                        try: _body = _r.text[:300]
                        except Exception: _body = ""
                        _log_err(f"background_sheet_move/scrub task={tid}", f"HTTP {_r.status_code}: {_body}")
                    return _r.status_code == 200
                except Exception as e:
                    _log_err(f"background_sheet_move/scrub task={tid}", e)
                    return False
            with ThreadPoolExecutor(max_workers=min(10, len(task_ids))) as _ex:
                _scrub_ok = sum(_ex.map(_do_scrub, task_ids))
            _log_err("background_sheet_move/scrub-end",
                     f"scrubbed {_scrub_ok}/{_scrub_total} successfully for cluster {cluster_hash}")
        except Exception as e:
            _log_err("background_sheet_move/scrub-outer", e)
        
# --- 2. INSTANT REVOKE LOGIC ---
def background_fn_revoke(cluster_hash):
    """Remove a Field Nation route from Railway/Postgres."""
    if DB_ENGINE is not None:
        try:
            _da.mirror_remove_field_nation_by_cluster_hash(DB_ENGINE, cluster_hash)
        except Exception as _dw_e:
            _log_err("postgres_remove_field_nation", _dw_e)

def _onfleet_get_state(tid, auth_header):
    """GET an Onfleet task and return (tid, is_completed). Defaults to NOT completed
    on any error so we err on the side of unassigning (safer for the dispatcher)."""
    try:
        r = requests.get(f"https://onfleet.com/api/v2/tasks/{tid}", headers=auth_header, timeout=4)
        if r.status_code == 200:
            t = r.json()
            # Onfleet task state: 0=Unassigned, 1=Assigned, 2=Active, 3=Completed
            is_done = (t.get('state') == 3) or bool((t.get('completionDetails') or {}).get('success'))
            return (tid, is_done)
    except Exception as e:
        _log_err(f"_onfleet_get_state task={tid}", e)
    return (tid, False)


def move_to_dispatch(cluster_hash, ic_name, pod_name, action_label="Revoked", check_onfleet=True, cluster_data=None, check_completed=False):
    """Moves route to Dispatch column instantly. Sheet update + Onfleet scrub run in background.

    Apr 27 2026 — completion-aware unassign (gated by check_completed=True):
      Sent/Declined tasks are still in Onfleet state=0 (unassigned), so the old behavior
      is correct for them — pass check_completed=False (default) and every task_id is
      unassigned, with a simple toast.
      Accepted/Finalized routes have tasks actively assigned to a contractor that may
      be partially complete. Pass check_completed=True from those buttons; we then GET
      each task in parallel and skip any with state=3 / completionDetails.success.
      Completed work stays attributed to the contractor; only outstanding tasks are
      returned to the pool. The toast reports the outstanding count + city/state."""

    # Security audit H23 - re-route double-submit guard. Every re-route /
    # revoke button funnels through move_to_dispatch; if this route is
    # already flagged reverted_ (a prior click is in flight or done), a
    # second click would re-fire the archive + Onfleet unassign. Bail.
    if st.session_state.get(f"reverted_{cluster_hash}", False):
        return

    # 1. Parse all task IDs from cluster_data (str CSV or list of dicts).
    all_task_ids = []
    if check_onfleet and cluster_data:
        try:
            # Three input shapes we have to handle:
            #   - 'taskIds'  (str CSV)         — built by background_sheet_move
            #   - 'data'     (list of dicts)   — live cluster from main fetch
            #   - 'task_ids' (list of strs)    — ghost rows from sheet (FN tab,
            #                                    rebuilt Sent/Finalized cards)
            # Without the third path, every ghost-revoke was a UI-only move that
            # never told Onfleet to unassign — worker kept seeing the route.
            raw = (
                cluster_data.get('taskIds', '')
                or cluster_data.get('data', [])
                or cluster_data.get('task_ids', [])
            )
            if isinstance(raw, str):
                all_task_ids = [t.strip() for t in raw.split(',') if t.strip()]
            elif isinstance(raw, list):
                all_task_ids = []
                for t in raw:
                    if isinstance(t, dict) and t.get('id'):
                        all_task_ids.append(str(t['id']).strip())
                    elif isinstance(t, str) and t.strip():
                        all_task_ids.append(t.strip())
        except Exception as e:
            _log_err("move_to_dispatch/task_ids-parse", e)
            all_task_ids = []

    # 2. Pick which task_ids get unassigned.
    #    - check_completed=False → unassign all (Sent/Declined behavior).
    #    - check_completed=True  → 🌟 May 2026 revoke-speedup: was doing a
    #      parallel GET per task to filter out state=3 (completed) — but that
    #      only fed the "M completed kept" toast text. Onfleet's PUT on a
    #      completed task is either a harmless no-op or a 4xx we already log;
    #      the actual re-route behavior is identical either way. Skip the GET
    #      to save ~1s per revoke and give the dispatcher a snappier button.
    outstanding_ids = list(all_task_ids)
    completed_count = 0

    # 🚀 INSTANT UI MOVE — set the reverted flag + clear per-route state +
    # record the action FIRST, synchronously, before any network I/O. The
    # bucket-sorting logic keys on reverted_{hash}, so the route leaves Sent on
    # the very next rerun: the button-press feels instant.
    st.session_state[f"reverted_{cluster_hash}"] = True
    st.session_state.pop(f"route_state_{cluster_hash}", None)
    st.session_state.pop(f"sent_ts_{cluster_hash}", None)
    st.session_state.pop(f"contractor_{cluster_hash}", None)
    st.session_state.pop(f"sync_{cluster_hash}", None)
    st.session_state.pop(f"scrub_timer_{cluster_hash}", None)

    # 🔄 POST-REVOKE AUTO-RESYNC (May 18 2026 — Nick: "tasks don't flow back").
    # When auto-rerun was killed, the natural path that brought re-routed tasks
    # back to Ready was severed. The OnFleet cache is 60s TTL and pull only
    # happens on Check New Tasks click, so the dispatcher saw the route vanish
    # from Awaiting but never reappear in Ready. Two countermeasures:
    #   (1) Bust the OnFleet 60s cache NOW so the next pull is fresh.
    #   (2) Set a per-pod flag that triggers a JS-deferred auto-click of the
    #       Check New Tasks button 12 seconds later (gives the bg thread time
    #       to unassign the tasks back to state=0 before we re-fetch).
    try:
        _fetch_onfleet_open_tasks_cached.clear()
    except Exception:
        pass
    if pod_name:
        st.session_state[f"_post_revoke_resync_pending_{pod_name}"] = True

    # 📜 Record this action so the dispatch card can show "Revoked / Re-Routed"
    # context the next time this same route surfaces in the Ready pool.
    try:
        from datetime import datetime as _dt
        _now_str = _dt.now().strftime('%m/%d %I:%M %p')
        _key = f'_actions_{cluster_hash}'
        st.session_state.setdefault(_key, []).append({
            'action': action_label,
            'ic': ic_name,
            'time': _now_str,
            'ts': _dt.now(),
        })
    except Exception as _e:
        _log_err('move_to_dispatch/action-record', _e)

    # 🚀 BACKGROUND RECONCILE — the slow part (Onfleet unassign PUTs + the GAS
    # archiveRoute POST, both multi-second HTTP) runs off the main thread so the
    # button-press returns instantly. The reverted_ flag above already moved the
    # route in the UI; this thread only reconciles Onfleet + the sheet. It touches
    # nothing but `requests` and `_log_err` (a print) — no Streamlit APIs — so it
    # has no ScriptRunContext dependency and completes reliably while the process
    # stays alive. (This is what makes "Sheet update + Onfleet scrub run in
    # background" in the docstring actually true again.)
    # Security audit H7 - the background thread reads cluster_data for several
    # seconds while the main thread keeps mutating the same dict on later
    # reruns. Snapshot it now so the archive payload is stable and a
    # mutate-while-iterate cannot raise inside (and silently lose) the
    # archive write.
    import copy as _copy_h7
    _cluster_snapshot = _copy_h7.deepcopy(cluster_data) if cluster_data is not None else None
    # Phase 2 migration — capture the WO on the main thread (st.session_state
    # isn't safe to read from the background thread below) so
    # background_sheet_move can best-effort mirror this archive into Postgres.
    _wo_for_pg_mirror = st.session_state.get(f"wo_{cluster_hash}")
    def _bg_reconcile():
        # 🌟 PARALLEL RECONCILE (May 2026 revoke-speedup): the Onfleet PUTs and
        # the GAS archiveRoute POST have no dependency on each other — running
        # them sequentially cost 3-7s per revoke; running them in parallel
        # drops revoke wall-clock to max(PUTs, GAS) ≈ 2-5s. Still inline (no
        # daemon), still reliable, just parallel across the two network calls.
        def _do_onfleet_unassigns():
            if not (outstanding_ids and check_completed):
                return
            try:
                _put_auth = {"Authorization": f"Basic {base64.b64encode(f'{ONFLEET_KEY}:'.encode()).decode()}", "Content-Type": "application/json"}
                _put_payload = json.dumps({"worker": None, "metadata": []})
                def _do_put(tid):
                    try:
                        r = requests.put(f"https://onfleet.com/api/v2/tasks/{tid}", headers=_put_auth, data=_put_payload, timeout=8)
                        if r.status_code != 200:
                            _log_err(f"move_to_dispatch/unassign task={tid}", f"HTTP {r.status_code}: {r.text[:200]}")
                        return (tid, r.status_code)
                    except Exception as e:
                        _log_err(f"move_to_dispatch/unassign task={tid}", e)
                        return (tid, -1)
                with ThreadPoolExecutor(max_workers=min(10, len(outstanding_ids))) as _ex:
                    list(_ex.map(_do_put, outstanding_ids))
            except Exception as e:
                _log_err("move_to_dispatch/unassign-outer", e)

        def _do_sheet_archive():
            try:
                background_sheet_move(cluster_hash, _cluster_snapshot, None, action_label, ic_name, wo=_wo_for_pg_mirror)
                # 🌟 Sep 2026 (Nick: "reroute isn't pulling back into dispatch") —
                # bust the 5-minute sheet cache right after the archive write
                # lands. Without this, the post-revoke 12s auto-resync (or even
                # a manual Check New Tasks click) could still read a STALE
                # fresh_sent_db that shows this route's tasks as sent/accepted/
                # finalized. The is_reverted flag lets a stale read skip the
                # dedup guard, but it does NOT override the cluster's own
                # `status` field — that's set from fresh_sent_db at
                # process_pod's classification step, so a stale read still
                # rebuilds the cluster with status='Sent'/'Accepted'/etc.
                # instead of 'Ready', landing it in Flagged (or nowhere the
                # dispatcher was looking) instead of Ready. The Field Nation
                # revoke handler already did this (see revoke_field_nation's
                # caller); every other re-route path was missing it.
                try:
                    fetch_sent_records_from_sheet.clear()
                except Exception as _clr_e:
                    _log_err("move_to_dispatch/sheet_cache_clear", _clr_e)
            except Exception as _bsm_e:
                _log_err("move_to_dispatch/sheet_move_bg", _bsm_e)

        # Sep 2026 — this .result(timeout=...) does NOT cap how long the button
        # click blocks: ThreadPoolExecutor.__exit__ below calls shutdown(wait=True),
        # so the `with` block doesn't return until both submitted futures truly
        # finish no matter what timeout is passed here — a short timeout just makes
        # this log a spurious "timed out" error while the thread keeps running
        # underneath it. Set to 55s (worst case: 2 attempts x 25s archiveRoute +
        # 1s backoff) so a normal-but-slow archive doesn't get misreported as a
        # failure while it's actually still in flight.
        with ThreadPoolExecutor(max_workers=2) as _rec_ex:
            _f1 = _rec_ex.submit(_do_onfleet_unassigns)
            _f2 = _rec_ex.submit(_do_sheet_archive)
            for _f in (_f1, _f2):
                try: _f.result(timeout=55)
                except Exception as _fe: _log_err("_bg_reconcile/future", _fe)

    # 🛑 INLINE, NOT A DAEMON THREAD (May 2026 fix — Accepted-revoke Onfleet bug):
    # The daemon-thread version fails SILENTLY under Streamlit on Railway. The
    # thread .start()s without error (so the except-fallback never fires), but
    # the call site fires st.rerun() the instant move_to_dispatch returns —
    # RerunException tears down the script-run context and the daemon thread is
    # orphaned mid-PUT. Net effect: the reverted_ flag moves the route in the UI
    # (looks done) but Onfleet never gets the unassign — Accepted-route tasks
    # stay assigned to the contractor. This exact failure was documented and
    # fixed once before ("SYNCHRONOUS parallel PUTs on the main thread") and got
    # re-introduced. Run inline: the reverted_ flag set ABOVE already moved the
    # route in the UI so the click still feels responsive — this just makes the
    # Onfleet + sheet reconcile actually happen before the rerun.
    _bg_reconcile()

    # Toast — completion-aware variant only when check_completed=True.
    if check_completed:
        _city = (cluster_data.get('city') if cluster_data else '') or ''
        _state = (cluster_data.get('state') if cluster_data else '') or ''
        _header = (f"{_city} {_state}").strip() or "Dispatch"
        n_out = len(outstanding_ids)
        if n_out > 0:
            _plural = 's' if n_out != 1 else ''
            _kept = f" ({completed_count} completed kept)" if completed_count else ""
            st.toast(f"✅ {n_out} Task{_plural} Unassigned from \"{ic_name}\": {_header}{_kept}")
        elif completed_count > 0:
            st.toast(f"✅ Route removed from \"{ic_name}\" — all {completed_count} tasks already completed.")
        else:
            st.toast(f"✅ {action_label}! Route moved back to Dispatch.")
    else:
        st.toast(f"✅ {action_label}! Route moved back to Dispatch.")
    # No st.rerun() — calling it inside an on_click callback is a no-op in this
    # Streamlit version (emits a warning, does nothing). The on_click flow already
    # auto-reruns the page; popover open/closed state is client-side only and
    # not controllable from server. Closing the revoke prompt cleanly would
    # require restructuring the call sites away from on_click=move_to_dispatch
    # to `if st.button(...): move_to_dispatch(...); st.rerun()` — that path runs
    # outside a callback context and rerun works normally.

def render_sync_age():
    """🌟 Opt #9 — small 'Updated Xs ago' caption. Reads st.session_state['_last_sync_ts']
    which is stamped by auto_sync_checker (every 60s) and by the init/sync callbacks.
    Shows nothing if the data hasn't been pulled yet.

    ⚠️ NOT a run_every fragment. It used to be @st.fragment(run_every=10), but a SECOND
    self-ticking run_every fragment alongside auto_sync_checker (run_every=60) starves
    auto_sync_checker's auto-rerun loop — which is the ONLY thing that makes accept/decline
    reflect in real time. Keep auto_sync_checker as the single run_every fragment in the app.
    This caption now just renders on each normal rerun (every auto_sync_checker poll stamps
    a fresh ts and reruns the page, so it stays accurate)."""
    last_ts = st.session_state.get('_last_sync_ts')
    if not last_ts:
        return
    try:
        elapsed = (datetime.now() - last_ts).total_seconds()
    except Exception:
        return
    if elapsed < 60:
        age = f"{int(elapsed)}s ago"
    elif elapsed < 3600:
        age = f"{int(elapsed/60)}m ago"
    else:
        age = f"{int(elapsed/3600)}h ago"
    st.markdown(
        f"<div style='font-size:10px; color:#94a3b8; text-align:center; margin-top:4px; font-style:italic;'>Updated {age}</div>",
        unsafe_allow_html=True,
    )


@st.fragment
def auto_sync_checker(pod_name):
    # 🛑 NO `run_every` (May 18 2026 — Nick's hard rule "stop refreshing").
    # Previously this was @st.fragment(run_every=60) — the 60s tick was the
    # last surviving source of visible "running" indicator activity on an
    # otherwise idle dashboard. The fragment now executes only on its
    # parent's first run; thereafter it re-runs only via user interaction.
    # sent_db patches are still applied on-demand whenever the page reruns.
    """Polls every 60s. Sync_version short-circuit means the heavy sheet fetch only runs when something ACTUALLY changed — between events, polls are tiny version probes. Uses GAS push-style change events to patch sent_db in-memory — no sheet fetch when accept/decline/archive happens. Falls back to full fetch only when the change buffer is empty or stale. Refreshes the sheet cache and triggers a rerun whenever
    any sheet content changed — not just Accepted/Declined status flips. Previously
    new routes appearing in Saved_Routes (or comp/due updates) wouldn't reflect
    until a user interaction (zooming the map, clicking somewhere, etc.) forced
    a rerender."""
    # 🛡️ INIT-IN-PROGRESS GUARD (May 18 2026). Don't fire st.rerun(scope="app")
    # while any pod is still initializing — admin view loads 5 pods sequentially,
    # and a mid-init poll that triggers an app rerun restarts the whole page
    # mid-stream. The loading overlay sets `_loading_overlay` / `_loading_pod`
    # while process_pod / smart_sync_pod are running; bail out cleanly if either
    # is set. The next poll (60s later) catches up.
    if st.session_state.get('_loading_overlay') is not None or st.session_state.get('_loading_pod'):
        return
    # 🛡️ POST-INIT COOLDOWN (May 18 2026). After ANY init/sync, give the
    # dispatcher 90 seconds of quiet page time before the auto-sync is
    # allowed to fire an app-scope rerun again. This kills the "page keeps
    # flickering / reverting" issue that admin testers were hitting — when
    # init completes and clears the overlay, the very next 60s tick was
    # firing st.rerun(scope="app") immediately and tearing the page apart.
    # The patched sent_db state is still updated in memory below; the only
    # thing the cooldown skips is the visual rerun.
    _last_app_rerun_ts = st.session_state.get('_auto_sync_last_app_rerun_ts')
    _cooldown_active = False
    if _last_app_rerun_ts is not None:
        try:
            _elapsed = (datetime.now() - _last_app_rerun_ts).total_seconds()
            if _elapsed < 90:
                _cooldown_active = True
        except Exception:
            pass
    pod_clusters = st.session_state.get(f"clusters_{pod_name}", [])
    if not pod_clusters:
        return
    # 🌟 Opt #9 — stamp last-sync ts every poll. The 'Updated Xs ago' pill in
    # the pod header reads this. Polls are 60s apart, so worst-case staleness
    # the dispatcher sees is ~60s.
    st.session_state['_last_sync_ts'] = datetime.now()

    pod_tid_set = set()
    for c in pod_clusters:
        for t in c.get('data', []):
            pod_tid_set.add(str(t['id']).strip())

    if not pod_tid_set:
        return

    try:
        last_ver_key = '_sync_version_last'
        last_ver = st.session_state.get(last_ver_key, None)
        # 🚀 Push-style sync: GAS returns version + any change events recorded
        # since our last seen version. We patch st.session_state.sent_db
        # directly with those changes — no sheet fetch needed for a poll
        # cycle that only saw accept/decline/archive events.
        cur_ver, changes = fetch_sync_status(since=int(last_ver or 0))

        # Endpoint missing / errored → bail out, leave existing state alone.
        # The next poll retries.
        if cur_ver == -1:
            return

        # First poll of this session — record baseline, no rerun.
        if last_ver is None:
            st.session_state[last_ver_key] = cur_ver
            return

        # Version unchanged → no mutations anywhere → nothing to do.
        if cur_ver == last_ver:
            return

        # Version advanced. Try to apply the change events directly to sent_db.
        # If `changes` is empty (e.g., we lost the buffer or fell behind by
        # more than 50 events), do the slow path: clear the cache and fetch
        # the sheet. Otherwise patch in-memory and skip the fetch entirely.
        sent_db_local = dict(st.session_state.get('sent_db', {}) or {})
        affected_this_pod = False
        if changes:
            for chg in changes:
                tids_csv = str(chg.get('t', '') or '')
                if not tids_csv:
                    continue
                action = str(chg.get('a', '') or '').strip()
                decision = str(chg.get('d', '') or '').strip().lower()
                # Map action+decision to a sent_db status string. The bucket
                # logic in run_pod_tab keys on raw_status === 'accepted' /
                # 'declined' / 'sent' / 'finalized' etc.
                if action == 'processDecision':
                    new_status = decision  # 'accepted' or 'declined'
                elif action == 'finalizeRoute':
                    new_status = 'finalized'
                elif action == 'archiveRoute':
                    # 🌟 BUG FIX (May 2026 — revoke/re-route stuck in Sent):
                    # Was: new_status = 'archived', falling through to the patch loop
                    # which set sent_db_local[tid]={status:'archived'}. Broke revoke 2 ways:
                    #   1) cleanup loop ~10 lines down sees tid in sent_db_local and
                    #      clears reverted_{cluster_hash}, undoing move_to_dispatch step 5.
                    #   2) run_pod_tab bucket logic (~line 5634) has no 'archived' case
                    #      → falls to `else: sent.append(c)` → cluster snaps back to Sent.
                    # Mirror the slow-path: remove the tid from sent_db_local instead.
                    for tid in tids_csv.split(','):
                        tid = tid.strip()
                        if not tid:
                            continue
                        if tid in pod_tid_set:
                            affected_this_pod = True
                        sent_db_local.pop(tid, None)
                    continue  # skip the generic patch loop below
                elif action == 'markFNAssigned':
                    new_status = 'accepted'
                elif action in (
                    'setFnProvider',
                    'bulkSetFnProviders',
                    'bulkSetFnProvidersByAddress',
                    'bulkSetFnProvidersByVenueId',
                    'bulkRevertFnState',
                ):
                    # 🌐 FN-state mutation. Provider writes (text input + Chrome
                    # extension scraping FN.com) and bulk reverts (Pending/Posted/
                    # Assigned → Pending) all rewrite the FN sheet row's JSON
                    # payload — fn_provider, fn_posted_ts, assigned_to_fn, etc.
                    # The change record carries no taskIds (these writes hit many
                    # routes), so we can't pod-filter — invalidate the sheet
                    # cache and force a rerun in EVERY pod tab so the new state
                    # shows up without a hard refresh. (May 16 2026.)
                    fetch_sent_records_from_sheet.clear()
                    affected_this_pod = True
                    continue
                elif action == 'saveRoute':
                    new_status = 'sent'
                else:
                    continue
                for tid in tids_csv.split(','):
                    tid = tid.strip()
                    if not tid:
                        continue
                    # saveRoute = the dispatcher's own dispatch action; the
                    # session already reflects it via route_state, so it must
                    # NOT trigger a rerun (was yanking the page on return from
                    # the Outlook tab). Patch sent_db silently below regardless.
                    if action != 'saveRoute' and tid in pod_tid_set:
                        affected_this_pod = True
                    rec = dict(sent_db_local.get(tid, {}))
                    rec['status'] = new_status
                    if chg.get('r'):
                        rec.setdefault('wo', str(chg.get('r')))
                    sent_db_local[tid] = rec
            st.session_state['sent_db'] = sent_db_local
            st.session_state[last_ver_key] = cur_ver
            # 🛑 REMOVED (May 14 2026) — this loop cleared reverted_<hash> for ANY
            # pod cluster still present in sent_db_local, not just the clusters this
            # sync actually touched. That raced with re-route: a dispatcher re-routes
            # a route (sets reverted_=True, fires st.rerun), the background archive
            # write is still in flight, an unrelated sync poll lands, and this loop
            # wiped the flag because the route was "still in the db" — snapping it
            # right back into Sent/Accepted/Declined. The run_pod_tab merge loop
            # already clears reverted_ when a patch genuinely changes a route's
            # status, and the dispatch flow clears it on re-send, so this block was
            # both redundant and harmful.
            # 🛑 NO APP-RERUN (May 18 2026 — final fix per Nick).
            # The page must stay stable. sent_db is already patched in memory
            # above — accept/decline events will surface on the dispatcher's
            # next interaction with the page (any click, fragment tick, etc.).
            # The previously-firing st.rerun(scope="app") was tearing pages
            # apart whenever any change landed, which is unacceptable in admin
            # multi-pod view. Each route card / awaiting panel is already its
            # own @st.fragment; they re-render on user click, not on poll.
            return

        # Slow path fallback: change buffer empty (older GAS, lost properties,
        # or we fell behind > 50 events). Do the original heavy fetch.
        st.session_state[last_ver_key] = cur_ver
        fetch_sent_records_from_sheet.clear()
        fresh_sent_db, _, _archived_wos, _history_db = fetch_sent_records_from_sheet()
        st.session_state['_history_db'] = _history_db

        # Build a fingerprint of the fields the cards actually display, restricted to
        # tasks in THIS pod. If the fingerprint differs from last poll, rerun the page.
        # This catches new routes, comp updates, due-date changes, status flips — any
        # sheet edit that would change what the dispatcher sees.
        def _fp(db):
            parts = []
            for tid in sorted(pod_tid_set):
                rec = db.get(tid)
                if not rec:
                    continue
                # 📌 Removed `time` from fingerprint: it ticks on every sheet write
                # (even no-op edits), causing spurious reruns. Status/WO/comp/due are the
                # only fields the cards display, so changes outside those don't matter.
                parts.append(f"{tid}|{rec.get('status','')}|{rec.get('wo','')}|{rec.get('comp','')}|{rec.get('due','')}")
            return hashlib.md5("\n".join(parts).encode()).hexdigest()

        new_fp = _fp(fresh_sent_db)
        last_fp_key = f"_auto_sync_fp_{pod_name}"
        last_fp = st.session_state.get(last_fp_key)

        # First poll: just record the baseline, don't rerun (avoids a rerun storm on init).
        if last_fp is None:
            st.session_state[last_fp_key] = new_fp
            st.session_state.sent_db = fresh_sent_db
            st.session_state['archived_wos'] = _archived_wos
            return

        if new_fp != last_fp:
            st.session_state[last_fp_key] = new_fp
            st.session_state.sent_db = fresh_sent_db
            st.session_state['archived_wos'] = _archived_wos
            # 🛑 NO APP-RERUN (May 18 2026 — final fix per Nick).
            # The fresh sent_db is now in session_state; route cards and
            # Awaiting panels are individual @st.fragment instances that
            # re-render on user interaction. App-level rerun was tearing
            # pages apart — explicitly removed here.

    except Exception as e:
        _log_err("auto_sync_checker", e)

@st.fragment
def render_finalization_checklist(cluster_hash, pod_name, prefix="chk", is_fn=False, has_kiosks=False):
    """Isolates checkbox reruns so the whole page doesn\'t reload, making checks instant.

    is_fn=True (Field Nation routes assigned to an FN rep) renders only 2 checklist
    items — \"Work Order Created\" and \"Packing list created\". The OnFleet
    optimization step doesn\'t apply because Field Nation handles their own routing."""
    st.markdown("<p style='font-size: 13px; font-weight: 600;'>Finalization Checklist:</p>", unsafe_allow_html=True)
    # Stack checkboxes vertically — Streamlit 1.39 enforces 1-level column
    # nesting; the checklist already lives inside Awaiting > expander, so
    # st.columns() here would exceed the limit and raise StreamlitAPIException.
    if is_fn:
        # FN routes skip the OnFleet route planning step.
        chk1 = st.checkbox("Work Order Created.", key=f"{prefix}_fnd_{cluster_hash}_{pod_name}")
        chk2 = st.checkbox("Attach Packing Slip to Production Line.", key=f"{prefix}_fnp_{cluster_hash}_{pod_name}")
        if has_kiosks:
            chk_k = st.checkbox("Ordered Kiosk(s).", key=f"{prefix}_fnk_{cluster_hash}_{pod_name}")
            _all_checked = chk1 and chk2 and chk_k
        else:
            _all_checked = chk1 and chk2
    else:
        chk1 = st.checkbox("Optimized Route in OnFleet.", key=f"{prefix}1_{cluster_hash}_{pod_name}")
        chk2 = st.checkbox("Work Order Created.", key=f"{prefix}2_{cluster_hash}_{pod_name}")
        chk3 = st.checkbox("Attach Packing Slip to Production Line.", key=f"{prefix}3_{cluster_hash}_{pod_name}")
        if has_kiosks:
            chk4 = st.checkbox("Ordered Kiosk(s).", key=f"{prefix}4_{cluster_hash}_{pod_name}")
            _all_checked = chk1 and chk2 and chk3 and chk4
        else:
            _all_checked = chk1 and chk2 and chk3

    if _all_checked:
        if st.button("🏁 Finalize Route", key=f"finbtn_{prefix}_{cluster_hash}_{pod_name}", type="primary", use_container_width=True):
            # 1. Railway/Postgres is the source of truth.
            try:
                if DB_ENGINE is None:
                    raise RuntimeError("Railway database is unavailable")
                _da.finalize_route(DB_ENGINE, st.session_state.get(f"wo_{cluster_hash}", ""))
            except Exception as e:
                st.error(f"Failed to finalize route in Railway: {e}")
                st.stop()

            # 2. 🧠 INSTANT UI OVERRIDE
            st.session_state[f"route_state_{cluster_hash}"] = "finalized"
            st.session_state[f"reverted_{cluster_hash}"] = True

            st.toast("🏁 Route Finalized! Moving to Finalized tab...")
            st.rerun(scope="app")
        

    
def instant_revoke_handler(cluster_hash, ic_name, payload_json, pod_name):
    # We now enable Onfleet scrubbing (State 0 check) immediately
    move_to_dispatch(cluster_hash, ic_name, pod_name, action_label="Revoked", check_onfleet=True, cluster_data=payload_json)
    
def assign_tasks_to_fn_team(task_ids, fn_team_id, fn_worker_id=None, wo_name="", due_date="", cluster_hash=""):
    """Move OnFleet tasks to the Field Nation surface in two steps:

    1. Append into the FN team container (organizational grouping).
    2. PUT each task with worker=fn_worker_id + WO/DUE metadata so its state
       flips 0→1 and it drops out of Onfleet's unassigned pool.

    Step 2 is skipped if no FN worker was found (logs a warning); Step 1 still
    runs so the dispatcher's filter (target_team_ids) keeps them out of view.
    Throttle of 70ms between task PUTs matches the GAS assignTasksToWorker
    pattern to stay under Onfleet's ~20 req/sec limit.
    """
    if not task_ids:
        _log_err("assign_tasks_to_fn_team/skip", "no task_ids")
        return
    try:
        auth = {"Authorization": f"Basic {base64.b64encode(f'{ONFLEET_KEY}:'.encode()).decode()}",
                "Content-Type": "application/json"}
        # 1) Move into FN team container
        if fn_team_id:
            try:
                r = requests.put(
                    f"https://onfleet.com/api/v2/containers/teams/{fn_team_id}",
                    headers=auth,
                    data=json.dumps({"tasks": [1] + list(task_ids)}),  # 1 = append
                    timeout=15,
                )
                if r.status_code != 200:
                    _log_err("assign_tasks_to_fn_team/team",
                             f"HTTP {r.status_code}: {r.text[:300]}")
            except Exception as _te:
                _log_err("assign_tasks_to_fn_team/team", _te)
        else:
            _log_err("assign_tasks_to_fn_team/team_skip", "no fn_team_id")

        # 2) Assign each task to the FN worker (state 0 → 1)
        if fn_worker_id:
            assign_payload = json.dumps({
                "worker": fn_worker_id,
                "metadata": [
                    {"name": "WO_NAME", "value": str(wo_name or ""), "type": "string", "visibility": ["api", "dashboard"]},
                    {"name": "DUE_DATE", "value": str(due_date or ""), "type": "string", "visibility": ["api", "dashboard"]},
                    {"name": "ASSIGNED_VIA", "value": "DCC_FN", "type": "string", "visibility": ["api", "dashboard"]},
                ],
            })
            def _do_assign(tid):
                try:
                    r = requests.put(
                        f"https://onfleet.com/api/v2/tasks/{tid}",
                        headers=auth, data=assign_payload, timeout=10,
                    )
                    if r.status_code != 200:
                        _log_err("assign_tasks_to_fn_team/worker",
                                 f"task={tid} HTTP {r.status_code}: {r.text[:200]}")
                    return r.status_code == 200
                except Exception as _we:
                    _log_err(f"assign_tasks_to_fn_team/worker task={tid}", _we)
                    return False
            
            with ThreadPoolExecutor(max_workers=min(10, len(task_ids))) as _ex:
                _results = list(_ex.map(_do_assign, task_ids))
            _ok = sum(_results)
            _log_err("assign_tasks_to_fn_team/done",
                     f"team_move OK; worker assigned {_ok}/{len(task_ids)}")
        else:
            _log_err("assign_tasks_to_fn_team/worker_skip",
                     "no fn_worker_id — tasks moved to team but state=0 (still in unassigned pool)")

        # 3) Create an OnFleet route plan named after the WO so the FN tasks
        #    show up as a grouped, named route in OnFleet under the Field
        #    Nation team. Mirrors GAS createOnfleetRoute pattern.
        if fn_worker_id and wo_name and task_ids:
            try:
                _start_dt = datetime.now() + timedelta(days=2)
                _start_dt = _start_dt.replace(hour=16, minute=0, second=0, microsecond=0)
                _start_ms = int(_start_dt.timestamp() * 1000)
                _route_payload = {
                    "name": str(wo_name),
                    "color": "#FACC15",
                    "startTime": _start_ms,
                    "timezone": "America/Los_Angeles",
                    "worker": fn_worker_id,
                    "tasks": list(task_ids),
                }
                if fn_team_id:
                    _route_payload["team"] = fn_team_id
                _rp = requests.post(
                    "https://onfleet.com/api/v2/routePlans",
                    headers=auth,
                    data=json.dumps(_route_payload),
                    timeout=15,
                )
                if _rp.status_code in (200, 201):
                    try:
                        _rid = _rp.json().get("id", "?")
                    except Exception:
                        _rid = "?"
                    _log_err("assign_tasks_to_fn_team/route_plan",
                             f"created route plan '{wo_name}' id={_rid} for {len(task_ids)} tasks")
                    # 🟪 Persist routePlanId onto the FN Postgres row so
                    # markFNAssigned can later PUT-rename it by ID. Without
                    # this, the rename path silently no-ops (OnFleet's list
                    # endpoint ignores date filters so name-based lookup is
                    # unreliable). May 16 2026.
                    if _rid and _rid != "?" and cluster_hash:
                        if DB_ENGINE is not None:
                            try:
                                _da.mirror_set_fn_route_plan_id_by_cluster_hash(DB_ENGINE, cluster_hash, _rid)
                            except Exception as _dw_e:
                                _log_err("pg_dual_write_set_fn_route_plan_id", _dw_e)
                else:
                    _log_err("assign_tasks_to_fn_team/route_plan",
                             f"HTTP {_rp.status_code}: {_rp.text[:300]}")
            except Exception as _re:
                _log_err("assign_tasks_to_fn_team/route_plan", _re)
        else:
            _log_err("assign_tasks_to_fn_team/route_plan_skip",
                     f"missing fn_worker_id={bool(fn_worker_id)} or wo_name={bool(wo_name)}")
    except Exception as e:
        _log_err("assign_tasks_to_fn_team", e)
        
def revoke_field_nation(cluster_hash, pod_name, cluster_data=None):
    """Removes route from Field Nation sheet tab AND resets UI state.

    Fires two GAS calls intentionally:
      1. removeFieldNation — explicit FN tab cleanup (handler in GAS)
      2. archiveRoute (via move_to_dispatch) — moves the row to Archive
    The archiveRoute call would already remove the FN row, but the explicit
    removeFieldNation makes intent legible if you ever wire up FN-only flows
    that shouldn't archive.

    Both calls run inline (was daemon=True, which dropped silently under
    Streamlit's thread context — same pattern that bit the assign path and
    the move_to_dispatch Onfleet PUTs before it).

    🌟 cluster_data + check_completed=True (May 2026 fix — "FN checkbox doesn't
    push back to pool"): FN tasks are at Onfleet state=1 under the FN worker
    (assign_tasks_to_fn_team put them there). To return them to state=0 so
    DCC's main pull sees them again, the Onfleet PUT in _bg_reconcile MUST
    fire — that PUT is gated on `outstanding_ids AND check_completed`. Pass
    cluster_data so step 1 can extract the task IDs, and check_completed=True
    so the gate opens. Without this, the FN sheet row was removed but the
    tasks stayed locked at state=1 forever, so the route was invisible
    everywhere — gone from FN, never came back to Ready.
    """
    try:
        background_fn_revoke(cluster_hash)
    except Exception as _e:
        _log_err("revoke_field_nation/fn_revoke", _e)
    move_to_dispatch(
        cluster_hash, "Field Nation", pod_name,
        action_label="Field Nation Revoked",
        check_onfleet=True,
        cluster_data=cluster_data,
        check_completed=True,
    )

# --- FIELD NATION MASS UPLOAD GENERATOR ---

from fn_utils import FN_STATE_MANAGER, generate_fn_upload, generate_combined_fn_upload, save_fn_provider, extract_fn_provider, format_fn_card_title



def _fn_ghost_to_cluster(g, skip_geocode=False):
    """Reconstruct a live-cluster-shaped dict from an FN sheet ghost.

    Why this exists: when "Assign to Field Nation" pushes OnFleet tasks to
    state=1 under the FN placeholder worker, DCC's main pull (state=0 only)
    can no longer see them — the FN tab would render empty. To recover, we
    read FN-status rows from the Google Sheet (already happening in
    fetch_sent_records_from_sheet now that 'field_nation' is in the ghost-
    capture list) and rebuild them into cluster-shaped dicts here. The FN
    tab's existing rendering code (which calls render_dispatch + expects
    cluster shape: data[], city, state, stops, center, …) then Just Works.

    The cluster_hash invariant matters: caller code recomputes the hash via
    md5(sorted(task_ids)) on cluster['data']. To make that match the hash
    already on the FN sheet row (so markFNAssigned can find it server-side
    and so route_state_{hash} session-state keys line up), the synthetic
    rows MUST carry the original OnFleet task IDs from g['task_ids'] —
    nothing else. Order is irrelevant since the hash sorts internally; what
    matters is that every real ID appears exactly once in cluster['data'].

    Returns None if the ghost has no task IDs (degenerate sheet row).
    """
    real_ids = [str(t).strip() for t in (g.get('task_ids') or []) if str(t).strip()]
    if not real_ids:
        return None

    # Pop real IDs onto each synthetic row as we walk stop_data. Order
    # doesn't matter for the hash — the cluster_hash recompute path sorts.
    id_queue = list(real_ids)
    synthetic = []
    addrs_seen = []

    for sd in (g.get('stop_data') or []):
        addr = sd.get('addr', '') or ''
        if not addr:
            continue
        addrs_seen.append(addr)
        venue = sd.get('venue', '') or ''
        _kiosk_id     = str(sd.get('kioskId', '') or '').strip()
        _venue_id     = str(sd.get('venueId', '') or '').strip()
        _loc_in_venue = str(sd.get('locationInVenue', '') or '').strip()
        # 🌟 FN-campaign-fanout fix (May 2026):
        # Previously this used only the FIRST campaign's name and stamped it
        # onto every synthetic task at the stop, so multi-campaign stops
        # collapsed to a single name in the venue accordion. Round-robin the
        # campaigns list across the per-stop tasks so every campaign name
        # surfaces in make_venue_details' grouped output. Also carry each
        # campaign's `esc` / `bs` flags onto the task it's assigned to so the
        # ❗/🔥/⭐ pills reflect the actual campaign mix rather than only the
        # stop-level boostedStandard fallback.
        _camps_raw = sd.get('campaigns') or []
        _camps = [c for c in _camps_raw if isinstance(c, dict) and (c.get('name') or '').strip()]
        _stop_boosted = str(sd.get('boostedStandard', '') or '').strip()
        if not _camps:
            # No campaign list at all — synthesize a single placeholder so the
            # rest of the loop doesn't have to special-case an empty list.
            # Empty name + stop-level boostedStandard preserves the old behavior
            # for legacy sheet rows (pre-2026-05-04) that never carried campaigns.
            _camps = [{'name': '', 'esc': False, 'bs': _stop_boosted}]
        _customer_type    = str(sd.get('customerType', '') or '').strip()
        _art_file         = str(sd.get('artFile', '') or '').strip()
        _sio              = str(sd.get('sio', '') or '').strip()
        _parts = [p.strip() for p in addr.split(',')]
        _state = (_parts[2] if len(_parts) > 2 else (g.get('state') or '')).strip().upper()[:2]
        _zip   = (_parts[3] if len(_parts) > 3 else '').strip()

        # Per-stop task counter for round-robin campaign assignment.
        _stop_task_idx = 0

        # Fan out per task-type count; consume one real ID per row.
        for label, key in (
            ('Kiosk Install',  'inst'),
            ('Kiosk Removal',  'remov'),
            ('New Ad',         'n_ad'),
            ('Continuity',     'c_ad'),
            ('Service',        'd_ad'),
        ):
            try:
                n = int(sd.get(key, 0) or 0)
            except (TypeError, ValueError):
                n = 0
            for _ in range(n):
                if not id_queue:
                    break
                _cmp_entry = _camps[_stop_task_idx % len(_camps)]
                _stop_task_idx += 1
                synthetic.append({
                    'id':              id_queue.pop(0),
                    'full':            addr,
                    'state':           _state,
                    'zip':             (str(sd.get('zip','') or '').strip() or _zip),
                    'venue_name':      venue,
                    'venue_id':        _venue_id,
                    'kiosk_id':        _kiosk_id,
                    'location_in_venue': _loc_in_venue,
                    'task_type':       label,
                    'client_company':  str(_cmp_entry.get('name', '') or ''),
                    'is_digital':      label == 'Service',
                    'boosted_standard': str(_cmp_entry.get('bs', '') or '') or _stop_boosted,
                    'art_file':        _art_file,
                    'customer_type':   _customer_type,
                    'escalated':       bool(_cmp_entry.get('esc', False)),
                })

    # Tail: any real IDs left after stop_data exhaustion get ROUND-ROBINED
    # across every known address — not dumped onto a single first-address as
    # we used to. Older FN sheet rows (pre-2026-05-04) often saved stop_data
    # with addresses but zero per-type counts (inst/remov/n_ad/c_ad/d_ad all
    # 0), so the fanout loop above consumes zero IDs and the entire queue
    # spills here. Dumping all 20 IDs onto addrs_seen[0] collapsed an N-stop
    # route into a 1-stop card with "20 Tasks" pinned to the first venue —
    # the exact bug Nick caught on a Folsom FN ghost (May 17 2026).
    #
    # Build the canonical stop list from BOTH stop_data addresses and the
    # locs pipe-string (which always reflects the original dispatch order +
    # count, with the IC home pin at index 0 and N-1).
    _raw_locs = [s.strip() for s in str(g.get('locs', '')).split('|') if s.strip()]
    _stops_from_locs = _raw_locs[1:-1] if len(_raw_locs) >= 3 else _raw_locs
    _all_stops = []
    for _a in (addrs_seen + _stops_from_locs):
        _a = (_a or '').strip()
        if _a and _a not in _all_stops:
            _all_stops.append(_a)
    if not _all_stops:
        _all_stops = [(g.get('city', '') or '')]
    # Pre-build a lookup so fallback rows can still grab venue/IDs from the
    # matching stop_data entry when available.
    _sd_by_addr = {}
    for _sd in (g.get('stop_data') or []):
        _a = (_sd.get('addr', '') or '').strip()
        if _a: _sd_by_addr[_a] = _sd
    # Per-stop campaign cycle counter so multiple tasks at the same address
    # round-robin through that stop's campaigns (matches the main fanout
    # loop's behavior at line 2430 — keeps multi-campaign stops surfacing
    # every name, not just the first).
    _fb_cycle = {}
    _rr_idx = 0
    while id_queue:
        _addr = _all_stops[_rr_idx % len(_all_stops)]
        _rr_idx += 1
        _sd = _sd_by_addr.get(_addr, {}) or {}
        # Pull this stop's campaigns and pick the next one (round-robin).
        _stop_camps = [
            _cp for _cp in (_sd.get('campaigns') or [])
            if isinstance(_cp, dict) and (_cp.get('name') or '').strip()
        ]
        if _stop_camps:
            _ci = _fb_cycle.get(_addr, 0)
            _fb_cycle[_addr] = _ci + 1
            _cmp_entry = _stop_camps[_ci % len(_stop_camps)]
            _cmp_name  = str(_cmp_entry.get('name', '') or '').strip()
            _cmp_esc   = bool(_cmp_entry.get('esc', False))
            _cmp_bs    = str(_cmp_entry.get('bs', '') or '').strip()
        else:
            _cmp_name  = ''
            _cmp_esc   = False
            _cmp_bs    = str(_sd.get('boostedStandard', '') or '').strip()
        synthetic.append({
            'id':              id_queue.pop(0),
            'full':            _addr,
            'state':           str(g.get('state', '') or '').strip().upper()[:2],
            'zip':             str(_sd.get('zip','') or '').strip(),
            'venue_name':      _sd.get('venue', '') or '',
            'venue_id':        str(_sd.get('venueId', '') or '').strip(),
            'kiosk_id':        str(_sd.get('kioskId', '') or '').strip(),
            'location_in_venue': str(_sd.get('locationInVenue', '') or '').strip(),
            'task_type':       '',
            'client_company':  _cmp_name,
            'is_digital':      False,
            'boosted_standard': _cmp_bs,
            'art_file':        str(_sd.get('artFile', '') or '').strip(),
            'customer_type':   str(_sd.get('customerType', '') or '').strip(),
            'escalated':       _cmp_esc,
        })

    inst_count  = sum(1 for t in synthetic if str(t.get('task_type','')).lower() == 'kiosk install')
    remov_count = sum(1 for t in synthetic if str(t.get('task_type','')).lower() == 'kiosk removal')

    # Center: best-effort. stop_data doesn't include lat/lng, so try to
    # geocode the first address. _mapbox_geocode is process-wide cached
    # (kiosks don't move), so the per-render cost is negligible after first
    # call. If geocoding fails (no MAPBOX_TOKEN, network blip, etc.) the
    # center stays at (0, 0); the map iteration in run_pod_tab guards on
    # _is_fn_ghost_pseudo to skip those markers rather than dropping a pin
    # in the Atlantic.
    center = (0.0, 0.0)
    _has_real_center = False
    if addrs_seen and not skip_geocode:
        try:
            _coords = _mapbox_geocode(addrs_seen[0])
            if _coords:
                # Mapbox returns (lng, lat); folium expects (lat, lng).
                center = (_coords[1], _coords[0])
                _has_real_center = True
        except Exception:
            pass

    return {
        'data':            synthetic,
        'city':            g.get('city', 'Unknown'),
        'state':           g.get('state', 'UNKNOWN'),
        'stops':           g.get('stops') or len(set(t['full'] for t in synthetic if t.get('full'))),
        'center':          center,
        'inst_count':      inst_count,
        'remov_count':     remov_count,
        'esc_count':       0,
        'is_removal':      False,
        'is_digital':      False,
        'boosted_tag':     '',
        'status':          'Ready',
        'contractor_name': g.get('contractor_name', 'Field Nation'),
        'wo':              g.get('wo', ''),
        'comp':            g.get('pay', 0),
        'due':             g.get('due', 'N/A'),
        'route_ts':        g.get('route_ts', ''),
        # Marker for downstream code that wants to distinguish a reconstructed
        # FN ghost from a normal live cluster — currently only the map-marker
        # iteration uses this (skips when _has_real_center is False).
        '_is_fn_ghost_pseudo': True,
        '_has_real_center':    _has_real_center,
    }



# --- UTILITIES ---
def haversine(lat1, lon1, lat2, lon2):
    # Sep 2026 — Nick: "Error initializing Orange: unsupported operand
    # type(s) for -: 'str' and 'float'". Root cause: one of the 12+ call
    # sites feeds this a lat/lon pulled straight from a Google Sheet column
    # (pandas can hand back text for a cell with stray whitespace/formatting)
    # or an Onfleet field that wasn't the plain float we expect, and the bare
    # subtraction below threw — which, uncaught, aborted the ENTIRE pod
    # init (no clusters saved, dispatcher stuck on "Initialize Data" forever).
    # Coerce defensively and fail soft: on bad input, return "infinitely far"
    # instead of crashing, so nearest-neighbor/radius logic just never
    # matches that one pair — a chance-mispriced route, never a dead pod.
    R = 3958.8
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError) as _hv_e:
        _log_err("haversine", f"non-numeric coordinate(s): lat1={lat1!r} lon1={lon1!r} lat2={lat2!r} lon2={lon2!r} ({_hv_e})")
        return float('inf')
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _ic_home_loc(ic, fallback=None):
    """Resolve an IC row to its best-known home location for routing.

    The IC sheet has both:
      - lat / lng numeric columns (used for the dropdown distance — trusted)
      - location text column (often a stale street address — UNTRUSTED)

    Earlier code passed `ic.get('location')` straight to get_gmaps, which then
    forward-geocoded the text. When the lat/lng said "Page, AZ" but the text
    column said "Phoenix, AZ", the dropdown would show "4.4 mi" but the route
    calc would use the Phoenix coords — producing a 333-mile / 7-hour route
    for a one-stop trip (the original bug that surfaced this).

    Always prefer lat/lng. _mapbox_geocode now short-circuits "lat,lng" strings
    to the same (lng, lat) tuple it would produce for an address — so passing
    the coord pair here gives correct routing without touching the API.

    Sep 2026 — Nick: a contractor (Jillane Barbee / Sellinja LLC) whose real
    address, 687 South Airport Way, Manteca, CA, sits right inside the
    route's own cluster somehow produced an 84-hour / 5525-mile round trip
    for an 8-stop LOCAL loop (Tracy/Manteca/Stockton/Livermore/Brentwood —
    all within ~40mi of each other). Root cause: this function only ever
    checked that the IC sheet's lat/lng was a numerically VALID coordinate
    (anywhere in -90..90 / -180..180) — never that it was actually anywhere
    NEAR the contractor. A stale/mistyped lat/lng for this one contractor
    silently anchored her round trip somewhere far away while her own
    address text was correct the whole time.
    Every caller already passes the cluster's own center as `fallback` —
    use it as a sanity check: if the "trusted" lat/lng is wildly far
    (>150mi — generous headroom above the 100-mile contractor-filter radius
    used elsewhere in the dispatcher) from where the route actually is,
    don't trust it. Fall through to the free-text address instead (still
    geocoded downstream by get_gmaps) rather than silently anchoring a real
    dispatcher's route on bad data.
    """
    try:
        _lat = float(ic.get('lat'))
        _lng = float(ic.get('lng'))
        if -90.0 <= _lat <= 90.0 and -180.0 <= _lng <= 180.0:
            _trusted = f"{_lat},{_lng}"
            if fallback:
                try:
                    _fb_lat_s, _fb_lng_s = str(fallback).split(',', 1)
                    _dist = haversine(_lat, _lng, float(_fb_lat_s), float(_fb_lng_s))
                    if _dist > 150:
                        _log_err(
                            "_ic_home_loc",
                            f"rejected implausible IC coords {_trusted} for "
                            f"{ic.get('name','?') if hasattr(ic, 'get') else '?'} — "
                            f"{round(_dist)}mi from cluster center {fallback}; "
                            f"falling back to text address instead."
                        )
                    else:
                        return _trusted
                except (TypeError, ValueError):
                    # fallback isn't a parseable "lat,lng" — can't sanity-check
                    # it, so trust the coordinate as before rather than change
                    # behavior for a shape of input this function never saw.
                    return _trusted
            else:
                return _trusted
    except (TypeError, ValueError):
        pass
    _loc = ic.get('location') if hasattr(ic, 'get') else None
    if _loc and str(_loc).strip():
        return str(_loc).strip()
    return fallback

def normalize_state(st_str):
    if not st_str: return "UNKNOWN"
    clean = str(st_str).strip().upper()
    return STATE_MAP.get(clean, clean)


# 🎨 Art-file extraction from OnFleet task notes.
# The "ArtFile" string sits inside the free-form Task Details / notes box on
# each OnFleet task — typically a token like "Dexcom_Premium_2026Q2_v3.pdf"
# embedded among normal driver-instruction prose. The Streamlit app surfaces
# this on the packing slip (dimmed sub-line on the National Summary card +
# ALLOCATED ART column on the Locals table).
#
# Heuristic ported from the existing portal's extractArtFile() in
# index_45.html — keeps lines that contain an underscore and do NOT end in
# sentence punctuation (.!?), then joins distinct values with " • ".
# That filter rules out prose like "Notify the manager." and keeps tokens
# like "Firkus_Plumbing_Q2_2026.pdf" that look like art file names.
def extract_art_file(notes: str) -> str:
    if not notes:
        return ""
    lines = [l.strip() for l in str(notes).splitlines() if l.strip()]
    keep = [l for l in lines if "_" in l and not re.search(r"[.!?]\s*$", l)]
    return " • ".join(keep)


def fetch_sync_status(since: int = 0):
    """Railway/Postgres change poll. Returns (version, changes)."""
    if DB_ENGINE is None:
        return (-1, [])
    try:
        rows = _da.get_changes_since(DB_ENGINE, int(since or 0))
        changes = []
        version = int(since or 0)
        for row in rows:
            version = max(version, int(row.get("version") or 0))
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            decision = str(payload.get("decision") or "")
            changes.append({
                "v": row.get("version"),
                "a": row.get("action"),
                "r": row.get("route_wo"),
                "t": str(payload.get("taskIds") or payload.get("task_ids") or ""),
                "d": "accepted" if decision == "accept" else ("declined" if decision == "decline" else decision),
                "ts": row.get("created_at"),
            })
        return (version, changes)
    except Exception as _ss_e:
        _log_err("fetch_sync_status/postgres", _ss_e)
        return (-1, [])


def _csv_with_retry(url, attempts=3):
    """pd.read_csv with brief linear-backoff retries. Google Sheets CSV export
    occasionally returns a transient error when several tabs are fetched
    back-to-back; a single miss used to surface a yellow "Could not load the
    \'<tab>\' route tab" banner until the user manually refreshed. Three
    attempts with 0.6s / 1.2s / 1.8s gaps clears nearly all of those without
    visibly slowing the cache miss. (Jun 3 2026 -- Nick.)"""
    _last = None
    for _i in range(attempts):
        try:
            return pd.read_csv(url)
        except Exception as _e:
            _last = _e
            time.sleep(0.6 * (_i + 1))
    raise _last


def _fetch_gids_parallel(base_url, gids):
    """Fetch multiple Google Sheets tabs (by gid) concurrently instead of one
    at a time. _cached_fetch_sent_records_from_sheet used to pull 6 tabs
    (sent/field_nation/declined/accepted/finalized/archive) in strict
    sequence — 6 full network round trips (each with its own up-to-3-attempt
    retry) before any row processing could even start. That was the
    dominant cost behind the "Ready loads, everything else takes 2-3
    minutes" symptom, and behind the 1-minute pause on any interaction
    (e.g. FN Assigned) that calls .clear() to force a resync.

    Returns {gid: DataFrame} on success or {gid: Exception} on per-tab
    failure, exactly the outcomes each caller already unwraps individually
    below — this just makes the network waiting happen in parallel instead
    of back-to-back. Same ThreadPoolExecutor pattern already used elsewhere
    in this file (e.g. the bulk markFNAssigned GAS calls).
    """
    results = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(gids)))) as _ex:
        _futures = {_ex.submit(_csv_with_retry, base_url + str(_gid)): _gid for _gid in gids}
        for _fut in _futures:
            _gid = _futures[_fut]
            try:
                results[_gid] = _fut.result()
            except Exception as _e:
                results[_gid] = _e
    return results

@st.cache_data(ttl=300, show_spinner=False)
def _cached_fetch_sent_records_from_sheet():
    """
    Returns: (sent_dict, ghost_routes, archived_wos)

    archived_wos was previously written to st.session_state from inside this cached
    function. That meant on cache hits the function body was skipped and archived_wos
    never refreshed even though the cached return looked fresh. Now returned in the
    tuple so callers must explicitly assign it after each call.
    """
    try:
        # Use safe extraction to prevent KeyError/AttributeError
        if not isinstance(IC_SHEET_URL, str):
            raise ValueError("IC_SHEET_URL must be a string. Check for trailing commas!")
            
        base_url = f"{IC_SHEET_URL.split('/edit')[0]}/export?format=csv&gid="
        
        sheets_to_fetch = [
            (SAVED_ROUTES_GID, "sent"),
            (FIELD_NATION_GID, "field_nation"),
            (DECLINED_ROUTES_GID, "declined"),
            (ACCEPTED_ROUTES_GID, "accepted"),
            (FINALIZED_ROUTES_GID, "finalized"),
        ]

        # 🚀 PERF: fetch all 6 tabs (the 5 above + the archive tab pulled
        # later in this function) concurrently instead of one-by-one. This
        # was 6 sequential network round trips before any row processing
        # could start — the dominant cost of a cache miss. See
        # _fetch_gids_parallel for the full story.
        _all_gids = [g for g, _ in sheets_to_fetch] + [ARCHIVE_GID]
        _prefetched = _fetch_gids_parallel(base_url, _all_gids)


        sent_dict = {}
        # 🌟 THE FIX: Add Global_Digital to the dictionary!
        ghost_routes = {"Blue": [], "Green": [], "Orange": [], "Purple": [], "Red": [], "Global_Digital": [], "UNKNOWN": []}
        # 📤 FN-Posted hydration: cluster_hash → display timestamp for any FN
        # row whose JSON payload has fn_posted_ts (set by GAS markFNPosted).
        # Merged into st.session_state['_fn_posted'] at end-of-function so any
        # local additions made between this read and the next GAS sync survive.
        fn_posted_dict = {}
        # 🌐 FN-Provider hydration: cluster_hash → Assigned Provider name (Associate-
        # entered or extension-pushed) so the card title can render "🌐 FN: <name>"
        # instead of bare "🌐 FN". Same lifecycle as fn_posted_dict.
        fn_provider_dict = {}
        # 📜 HISTORY DB: per-task event list, in chronological order across every
        # sheet tab a task ever appeared in. Used by render_dispatch to show "declined by X",
        # "removed from Field Nation on Y", etc. on Ready cards so dispatchers see the
        # route's prior journey instead of a blank slate.
        history_db = {}  # tid -> list of {status, name, time, wo, raw_ts}
        
        for gid, status_label in sheets_to_fetch:
            try:
                # Already fetched concurrently above — re-raise here so the
                # existing per-sheet except block below still handles a
                # failed tab exactly as before (log + warn + skip that tab).
                df = _prefetched.get(gid)
                if isinstance(df, Exception):
                    raise df
                df.columns = [str(c).strip().lower() for c in df.columns]

                if 'json payload' in df.columns:
                    # 🚀 PERF: drop rows the per-row loop below would just
                    # `continue` past anyway — empty/blank dates, and dates
                    # older than MIGRATION_CUTOFF_DATE — BEFORE the expensive
                    # iterrows()+json.loads() pass. These sheets only grow
                    # over time, so this is the difference between parsing
                    # every historical row on every cache miss and parsing
                    # only the recent ones. Rows with a non-empty but
                    # UNPARSEABLE date are deliberately kept (matches the
                    # original try/except fallback below, which still wants
                    # to process them with dt_obj=None).
                    if 'date created' in df.columns:
                        _raw_dates = df['date created']
                        _has_raw = _raw_dates.notna() & (_raw_dates.astype(str).str.strip() != '')
                        _parsed_dates = pd.to_datetime(_raw_dates, errors='coerce')
                        _cutoff = pd.to_datetime(MIGRATION_CUTOFF_DATE)
                        _too_old = _has_raw & _parsed_dates.notna() & (_parsed_dates < _cutoff)
                        df = df[_has_raw & ~_too_old]
                    for _, row in df.iterrows():
                        try:
                            p = json.loads(row['json payload'])
                            tids = str(p.get('taskIds', '')).replace('|', ',').split(',')
                            c_name = str(row.get('contractor', 'Unknown Contractor'))
                            
                            raw_ts = row.get('date created', '')
                            ts_display = ""
                            dt_obj = None  # 📌 reset per row so the history append never
                                            # picks up a stale timestamp from a previous iteration.
                            
                            # Filter out routes older than the migration cutoff (see top-of-file constant).
                            if pd.notna(raw_ts) and str(raw_ts).strip():
                                try:
                                    dt_obj = pd.to_datetime(raw_ts)
                                    if dt_obj < pd.to_datetime(MIGRATION_CUTOFF_DATE):
                                        continue 
                                    ts_display = dt_obj.strftime('%m/%d %I:%M %p')
                                except:
                                    ts_display = str(raw_ts)
                            else:
                                continue # Skip empty date rows just to be safe
                            
                            # 📤 Harvest fn_posted_ts so the FN tab's Sent group
                            # survives reloads. Only relevant for the Field Nation tab.
                            if status_label == "field_nation":
                                _fp_ts_raw = p.get('fn_posted_ts')
                                _fp_hash = p.get('cluster_hash')
                                if _fp_ts_raw and _fp_hash:
                                    try:
                                        _fp_dt = pd.to_datetime(_fp_ts_raw)
                                        fn_posted_dict[_fp_hash] = _fp_dt.strftime('%m/%d %I:%M %p')
                                    except Exception:
                                        fn_posted_dict[_fp_hash] = str(_fp_ts_raw)
                                # 🌐 Also harvest fn_provider (Assigned Provider name).
                                # Survives reload + travels to Accepted via markFNAssigned.
                                _fp_provider = extract_fn_provider(p)
                                if _fp_hash and _fp_provider:
                                    fn_provider_dict[_fp_hash] = _fp_provider

                            # 1. Live Task Matching
                            for tid in tids:
                                tid = tid.strip()
                                if tid:
                                    # 1. Enforce Contractor name for FN routes
                                    display_name = "Field Nation" if status_label == "field_nation" else c_name
                                    sent_dict[tid] = {
                                        "name": display_name, 
                                        "status": status_label,
                                        "time": ts_display,
                                        "wo": p.get('wo', display_name),
                                        "comp": p.get('comp', 0),     
                                        "due": p.get('due', 'N/A'),
                                        # raw_ts: sheet-row creation time ~ when the contractor accepted.
                                        # Used by the return-to-Dispatch grace-window check in the bucket loop.
                                        "raw_ts": dt_obj,
                                    }
                                    # 📜 Record this row as a history event for the task.
                                    # `dt_obj` (computed above) is the raw pandas Timestamp we can sort on later.
                                    history_db.setdefault(tid, []).append({
                                        "status": status_label,
                                        "name": display_name,
                                        "time": ts_display,
                                        "wo": p.get('wo', display_name),
                                        "raw_ts": dt_obj,
                                    })
                            
                            # 🌐 Accepted FN routes (post-markFNAssigned) — harvest provider
                            # from the new sheet row too so the Accepted tab title can render
                            # "🌐 FN: <name>". The payload's assigned_to_fn flag is the marker.
                            try:
                                if p.get('assigned_to_fn'):
                                    _afp_provider = extract_fn_provider(p)
                                    _afp_hash = p.get('cluster_hash')
                                    if _afp_hash and _afp_provider:
                                        fn_provider_dict[_afp_hash] = _afp_provider
                            except Exception:
                                pass

                            # 🌟 THE FIX: Omni-Ghost Engine - Capture Sent routes too!
                            # May 4 2026 — added 'field_nation' so FN-sheet rows also produce
                            # ghost entries. Required because once "Assign to Field Nation" pushes
                            # OnFleet tasks to state=1 under the FN placeholder worker, the main
                            # OnFleet pull (state=0 only) can't see them anymore. Without this,
                            # the FN tab is empty for any route that's actually been assigned to FN.
                            if status_label in ['accepted', 'finalized', 'sent', 'field_nation']:
                                locs_str = str(p.get('locs', ''))
                                state_guess, city_guess = "UNKNOWN", "Unknown"
                                stops_list = [s.strip() for s in locs_str.split('|') if s.strip()]
                                
                                # 🌟 THE FIX: Prioritize direct payload extraction, fallback to string splitting
                                state_guess = str(p.get('state', 'UNKNOWN'))
                                city_guess = str(p.get('city', 'Unknown'))
                                
                                if state_guess == "UNKNOWN" or city_guess == "Unknown":
                                    if len(stops_list) > 1:
                                        addr_parts = stops_list[1].split(',')
                                        if len(addr_parts) >= 2:
                                            state_raw = addr_parts[-1].strip().upper()
                                            state_guess = state_raw.split(' ')[0] 
                                            city_guess = addr_parts[-2].strip()
                                    elif len(stops_list) == 1:
                                        addr_parts = stops_list[0].split(',')
                                        if len(addr_parts) >= 2:
                                            state_raw = addr_parts[-1].strip().upper()
                                            state_guess = state_raw.split(' ')[0] 
                                            city_guess = addr_parts[-2].strip()
                                
                                # 🌟 THE FIX: ALWAYS define norm_state outside the if/else block!
                                norm_state = STATE_MAP.get(state_guess, state_guess)
                                
                                is_digital_ghost = False
                                if tids and tids[0].strip() in sent_dict:
                                    is_digital_ghost = sent_dict[tids[0].strip()].get('is_digital', False)
                                    
                                if not is_digital_ghost:
                                    job_only = str(p.get('jobOnly', ''))
                                    is_digital_ghost = any(trigger in job_only.lower() for trigger in ['🔌', '🔧', '⚙️', '📵', 'service', 'offline', 'ins/rem'])
                                
                                pod_name = "UNKNOWN"
                                if is_digital_ghost:
                                    pod_name = "Global_Digital"
                                else:
                                    for p_name, p_config in POD_CONFIGS.items():
                                        if norm_state in p_config['states']:
                                            pod_name = p_name
                                            break
                                
                                if pod_name != "UNKNOWN":
                                    ghost_hash = p.get("cluster_hash")
                                    if not ghost_hash:
                                        clean_tids = [str(t).strip() for t in tids if str(t).strip()]
                                        ghost_hash = hashlib.md5("".join(sorted(clean_tids)).encode()).hexdigest()
                                    # Parse rich stop data if available
                                    try:
                                        stop_data = json.loads(p.get("stopData", "[]"))
                                    except:
                                        stop_data = []

                                    # 🛠️ KIOSK COUNT — saveRoute/saveToFieldNation now stamp 'kCnt'
                                    # into the payload, but older sheet rows predate that. Fallback:
                                    # walk stop_data and count any task whose task_type contains
                                    # 'install' (matches "Kiosk Install" + variants). Without this the
                                    # Accepted/Sent/Finalized ghost cards have no Kiosk icon and the
                                    # Shopify "Order Kiosks" button never renders.
                                    _k_cnt_payload = p.get('kCnt', None)
                                    if _k_cnt_payload in (None, 0):
                                        _k_fallback = 0
                                        for _sd in (stop_data or []):
                                            try:
                                                for _tk in (_sd.get('tasks', []) or []):
                                                    _tt = str(_tk.get('task_type', '') or _tk.get('taskType', '')).lower()
                                                    if 'install' in _tt:
                                                        _k_fallback += 1
                                            except Exception:
                                                continue
                                        _k_cnt_final = _k_fallback
                                    else:
                                        _k_cnt_final = int(_k_cnt_payload or 0)
                                    ghost_routes[pod_name].append({
                                        "contractor_name": c_name,
                                        "route_ts": ts_display,
                                        "city": city_guess,
                                        "state": norm_state,
                                        "stops": p.get('lCnt', 0),
                                        "tasks": p.get('tCnt', len(tids)),
                                        "pay": p.get('comp', 0),
                                        "wo": p.get('wo', c_name),
                                        "due": p.get('due', 'N/A'),
                                        "status": status_label,
                                        "hash": ghost_hash,
                                        "locs": p.get('locs', ''),
                                        "stop_data": stop_data,
                                        "task_ids": [str(t).strip() for t in tids if str(t).strip()],
                                        "kCnt": _k_cnt_final,
                                    })
                        except Exception as _row_e:
                            # Security audit M6 - log malformed-row parse
                            # failures instead of silently dropping them.
                            _log_err(f"fetch_sent_records_from_sheet/row/{status_label}", _row_e)
                            continue
            except Exception as _sheet_e:
                # Security audit M6 - a whole tab failing to load is a real
                # incident (routes vanish, collision detection misses); log
                # loudly and surface a warning rather than going silent-empty.
                _log_err(f"fetch_sent_records_from_sheet/sheet/{status_label}", _sheet_e)
                try:
                    st.warning(
                        f"\u26a0\ufe0f Could not load the '{status_label}' route tab "
                        "- some routes may be missing from this view. Refresh to retry."
                    )
                except Exception:
                    pass
                continue
            
        # 🌟 ARCHIVE SIDE-CHANNEL: harvest WOs of archived routes so the WO counter
        # can move past suffixes that were "freed" when a route was archived. We also
        # harvest revoke/re-route HISTORY events so the Ready-card history banner survives
        # session resets — was previously session-only via _actions_*. Tasks/status are
        # still NEVER read into clustering — only into history_db.
        _archived_wos = set()
        try:
            # Already fetched concurrently above alongside the other 5 tabs.
            _archive_df = _prefetched.get(ARCHIVE_GID)
            if isinstance(_archive_df, Exception):
                raise _archive_df
            _archive_df.columns = [str(c).strip().lower() for c in _archive_df.columns]
            if 'json payload' in _archive_df.columns:
                for _, _arow in _archive_df.iterrows():
                    try:
                        _ap = json.loads(_arow['json payload'])
                        _orig = str(_ap.get('archived_wo', '') or '').strip()
                        if _orig and _orig.lower() != 'archived':
                            _archived_wos.add(_orig)
                        # History harvest: Revoked / Re-Routed / Ghost Archived events.
                        # Timestamp handling: GAS writes archive_ts via new Date().toISOString()
                        # which ends in "Z" (tz-aware UTC). pandas comparisons against the rest
                        # of history_db (tz-naive sheet "Date Created") would fail with
                        # "Cannot compare tz-naive and tz-aware timestamps" — so we explicitly
                        # parse with utc=True then tz_localize(None) to strip the tz.
                        _act = str(_ap.get('archive_action', '') or '').strip()
                        if _act in ('Revoked', 'Re-Routed', 'Ghost Archived'):
                            _ic = str(_ap.get('archive_ic', '') or _ap.get('icn', '') or '')
                            _ats = _ap.get('archive_ts', '')
                            _dt = pd.Timestamp.min
                            try:
                                if _ats:
                                    _parsed = pd.to_datetime(_ats, errors='coerce', utc=True)
                                    if pd.notna(_parsed):
                                        try:
                                            _dt = _parsed.tz_localize(None)
                                        except (TypeError, AttributeError):
                                            _dt = _parsed
                            except Exception:
                                _dt = pd.Timestamp.min
                            _ts_display = ''
                            try:
                                if pd.notna(_dt) and _dt is not pd.Timestamp.min:
                                    _ts_display = _dt.strftime('%m/%d %I:%M %p')
                            except Exception:
                                _ts_display = ''
                            _hist_status = 'revoked' if _act == 'Revoked' else ('re-routed' if _act == 'Re-Routed' else 'ghost-archived')
                            _tids_str = str(_ap.get('taskIds', ''))
                            for _tid in _tids_str.replace('|', ',').split(','):
                                _tid = _tid.strip()
                                if not _tid:
                                    continue
                                history_db.setdefault(_tid, []).append({
                                    'status': _hist_status,
                                    'name': _ic,
                                    'time': _ts_display,
                                    'wo': _orig,
                                    'raw_ts': _dt,
                                })
                    except Exception:
                        pass
        except Exception as _ae:
            # Security audit M5 - flag a hard archive-read failure so the WO
            # counter does not silently treat consumed suffixes as free.
            _log_err("fetch_sent_records_from_sheet/archive_harvest", _ae)
            try:
                ghost_routes['_archive_read_failed'] = True
            except Exception:
                pass

        # Sort each task's events chronologically (oldest first). Defensive key: strip
        # any accidental tz from raw_ts so a single tz-aware value can\'t poison the
        # sort and bubble up to the outer "Failed to fetch portal records" catch.
        def _sort_key(e):
            ts = e.get('raw_ts') or pd.Timestamp.min
            try:
                if pd.notna(ts) and getattr(ts, 'tzinfo', None) is not None:
                    return ts.tz_localize(None)
            except Exception:
                pass
            return ts if pd.notna(ts) else pd.Timestamp.min
        for _tid, _evts in history_db.items():
            try:
                _evts.sort(key=_sort_key)
            except Exception as _se:
                _log_err(f"history_db sort tid={_tid}", _se)
        # 📤 Park the FN-posted timestamp dict on ghost_routes so cache hits
        # still carry it. Earlier this merged directly into st.session_state
        # here, but @st.cache_data short-circuits the function body on a hit —
        # which meant after the 5-minute cache warmed, every reload bypassed
        # the merge and the FN tab forgot which routes had been Posted.
        # Caller-side merge fixes that: see the fetch_sent_records_from_sheet
        # call sites where `_fn_posted` is read off ghost_routes and merged
        # into session_state on every call (cached or not).
        try:
            ghost_routes['_fn_posted'] = fn_posted_dict
        except Exception as _fpe:
            _log_err("fn_posted_attach", _fpe)
        try:
            ghost_routes['_fn_provider'] = fn_provider_dict
        except Exception as _fpv:
            _log_err("fn_provider_attach", _fpv)
        return sent_dict, ghost_routes, _archived_wos, history_db
    except Exception as e:
        st.error(f"Failed to fetch portal records: {e}")
        return {}, {}, set(), {}


@st.cache_data(ttl=60, show_spinner=False)
def _cached_fetch_sent_records_from_db():
    if DB_ENGINE is None:
        return {}, {}, set(), {}
    return _da.get_sent_records_from_db(
        DB_ENGINE, POD_CONFIGS, STATE_MAP, cutoff_date=MIGRATION_CUTOFF_DATE
    )


def fetch_sent_records_from_sheet():
    """Compatibility name; production source is Railway Postgres."""
    sent_dict, ghost_routes, archived_wos, history_db = _cached_fetch_sent_records_from_db()
    try:
        _fp_from_db = (ghost_routes or {}).get('_fn_posted', {}) or {}
        if _fp_from_db:
            _ss_fp = st.session_state.get('_fn_posted', {}) or {}
            for _h, _ts in _fp_from_db.items():
                _ss_fp.setdefault(_h, _ts)
            st.session_state['_fn_posted'] = _ss_fp
    except Exception as _fpe:
        _log_err("fn_posted_hydrate_db", _fpe)
    try:
        _fpv_from_db = (ghost_routes or {}).get('_fn_provider', {}) or {}
        if _fpv_from_db:
            _ss_fpv = st.session_state.get('_fn_provider', {}) or {}
            for _h, _name in _fpv_from_db.items():
                _ss_fpv[_h] = _name
            st.session_state['_fn_provider'] = _ss_fpv
    except Exception as _fpv:
        _log_err("fn_provider_hydrate_db", _fpv)
    st.session_state['_archive_read_failed'] = False
    return sent_dict, ghost_routes, archived_wos, history_db



# Forward .clear() so existing `fetch_sent_records_from_sheet.clear()` call
# sites still invalidate the underlying cache without having to be edited.
fetch_sent_records_from_sheet.clear = _cached_fetch_sent_records_from_db.clear

# ─── Routing engine: Mapbox Optimization API + persistent shared cache ───
# Two-stage cache:
#   1. Geocode (address → lng,lat) — ~unbounded distinct addresses, but routes
#      revisit the same kiosks for months, so a few hundred warm cache entries
#      cover everything. Free tier: 100k Mapbox Geocoding calls/month.
#   2. Optimization (home + waypoints → distance, time, optimized order) —
#      keyed on (home, ordered tuple of waypoints). Same route bundle viewed
#      multiple times = single API call across ALL dispatchers for 24h.
# Both caches are @st.cache_resource (process-wide dict) so they survive
# Streamlit reruns and are shared across every connected session — was the
# main cost driver: previous st.session_state cache only helped within one
# tab and got wiped on every reconnect.

@st.cache_resource(show_spinner=False)
def _mapbox_geocode_cache():
    """Process-wide dict: address (str) → (lng, lat) tuple. Lasts until
    Railway redeploys. Geocoding is dirt cheap to retry on miss but free to
    serve from cache, so this is overwhelmingly net-positive."""
    return {}


def _mapbox_geocode(address):
    """Resolve an address to (lng, lat) via Mapbox Geocoding API. Returns
    None on miss. Cached forever for the address — kiosks don't move.

    Special case: if the input is a "<float>,<float>" coordinate pair (which
    is how IC home locations and cluster centers get passed around), skip
    the geocoding API entirely and return the pair directly. This dodges a
    long-standing bug where the IC sheet's 'location' text column can be
    out-of-date or land on the wrong city, while the lat/lng columns (used
    for the dropdown distance) are trustworthy. Convention: input is
    "lat,lng" (matches f-strings like f"{cluster['center'][0]},{cluster['center'][1]}"),
    return is (lng, lat) to match Mapbox's coordinate order.
    """
    if not MAPBOX_TOKEN or not address:
        return None
    addr_key = str(address).strip()
    if not addr_key:
        return None
    cache = _mapbox_geocode_cache()
    if addr_key in cache:
        return cache[addr_key]
    # Coordinate-pair short-circuit. Strict "lat,lng" form so we don't
    # accidentally swallow a street address that happens to start with
    # two numbers.
    _coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', addr_key)
    if _coord_match:
        try:
            _lat = float(_coord_match.group(1))
            _lng = float(_coord_match.group(2))
            if -90.0 <= _lat <= 90.0 and -180.0 <= _lng <= 180.0:
                _result = (_lng, _lat)
                cache[addr_key] = _result
                return _result
        except (ValueError, TypeError):
            pass
    from urllib.parse import quote
    enc = quote(addr_key, safe='')
    url = (
        f"https://api.mapbox.com/geocoding/v5/mapbox.places/{enc}.json"
        f"?limit=1&access_token={MAPBOX_TOKEN}"
    )
    try:
        r = requests.get(url, timeout=10).json()
        feats = r.get('features') or []
        if feats and feats[0].get('center'):
            lng, lat = feats[0]['center']
            cache[addr_key] = (lng, lat)
            return (lng, lat)
    except Exception as e:
        _log_err("_mapbox_geocode", e)
    return None


@st.cache_resource(show_spinner=False)
def _gmaps_route_cache():
    """Process-wide route cache: (home, waypoints_tuple) → (mi, hrs, str, order, ts).
    Stores SUCCESSES ONLY — failures fall through and are retried on next call."""
    return {}


def get_gmaps(home, waypoints):
    """Drop-in replacement for the previous Google Directions optimize:true call.
    Returns the same (miles, total_hours, "Xh Ym", waypoint_order) tuple shape
    so call sites need no changes. Internally uses Mapbox Optimization API V1.

    Mapbox V1 has a hard cap of 12 coordinates per request — and BOTH the
    source AND destination home pins count toward that cap. To support
    bundled routes that consistently run >10 stops, we chunk the waypoints
    into batches of ≤10 stops (12 total coords - 2 home pins), optimize each
    batch independently, and sum the totals. Order is preserved across
    batches; the optimizer still runs within each batch so per-chunk routing
    remains efficient.

    Per-request cost: $0 within Mapbox's 100k/month free tier, $2/1000 after.
    A 30-stop bundled route makes 3 chunked calls (10 + 10 + 10), all cached
    together under the original (home, waypoints) key for 24 hours.
    """
    _wp_tuple = tuple(waypoints) if waypoints else ()
    _key = (home, _wp_tuple)
    _now = time.time()
    _cache = _gmaps_route_cache()
    _entry = _cache.get(_key)
    if _entry is not None and (_now - _entry[1] < 86400):
        return _entry[0]

    if not MAPBOX_TOKEN:
        _log_err("get_gmaps", "MAPBOX_TOKEN not set in env — can't compute route")
        return 0, 0, "0h 0m", []
    if not waypoints:
        return 0, 0, "0h 0m", []

    # Geocode home + each waypoint to (lng, lat). Each is cached forever.
    home_ll = _mapbox_geocode(home)
    if not home_ll:
        return 0, 0, "0h 0m", []
    wp_lls = [_mapbox_geocode(w) for w in waypoints]
    if any(c is None for c in wp_lls):
        return 0, 0, "0h 0m", []

    # Helper: optimize one ≤11-stop chunk against `home`. Returns
    # (miles, drive_hours, ordered_global_indices) or None on Mapbox error.
    # `chunk_indices` is the list of indices into the original `waypoints`
    # list — used to translate Mapbox's local optimization back to global
    # positions for the combined waypoint_order result.
    def _optimize_chunk(chunk_indices):
        chunk_lls = [wp_lls[i] for i in chunk_indices]
        # Submit as [home, wp1, wp2, ..., wpN, home] then ask Mapbox to optimize
        # the middle waypoints (source=first, destination=last fixes both ends).
        coord_list = [home_ll] + chunk_lls + [home_ll]
        coords_str = ";".join(f"{lng},{lat}" for lng, lat in coord_list)
        url = (
            f"https://api.mapbox.com/optimized-trips/v1/mapbox/driving-traffic/{coords_str}"
            f"?source=first&destination=last&roundtrip=false"
            f"&access_token={MAPBOX_TOKEN}"
        )
        try:
            res = requests.get(url, timeout=15).json()
            if res.get('code') == 'Ok' and res.get('trips'):
                trip = res['trips'][0]
                _mi = trip['distance'] * 0.000621371
                _hrs = trip['duration'] / 3600
                # Mapbox returns each input waypoint with a `waypoint_index` field —
                # the position it ended up in the optimized trip. Indexes 0 and last
                # are the fixed home pinned via source=first / destination=last.
                wps = res.get('waypoints', [])
                try:
                    middle = wps[1:-1]
                    ordered_pairs = sorted(
                        enumerate(middle),
                        key=lambda p: p[1].get('waypoint_index', p[0])
                    )
                    # Translate Mapbox's local indices back to global indices.
                    chunk_order = [chunk_indices[local_idx] for local_idx, _ in ordered_pairs]
                except Exception:
                    chunk_order = list(chunk_indices)
                return _mi, _hrs, chunk_order
            else:
                _log_err(
                    "get_gmaps",
                    f"Mapbox code: {res.get('code')} / msg: {res.get('message','')[:200]}",
                )
        except Exception as e:
            _log_err("get_gmaps", e)
        return None

    # Mapbox V1 cap: 12 coords per request, period. The request body is
    # [home, wp1, ..., wpN, home] — both home pins count toward the cap, so
    # the maximum N is 12 - 2 = 10. Earlier off-by-one had this at 11, which
    # produced 13-coord requests that Mapbox rejected with "Too many
    # coordinates" — silently failing every chunk for any bundle ≥12 stops.
    CHUNK = 10
    total_mi = 0.0
    total_drive_hrs = 0.0  # service hours added once at the end after all chunks
    waypoint_order = []
    all_indices = list(range(len(waypoints)))

    for i in range(0, len(all_indices), CHUNK):
        chunk = all_indices[i:i + CHUNK]
        result = _optimize_chunk(chunk)
        # Any chunk failure aborts the whole call — we'd rather show "—" than
        # a partially-summed drive time that's wrong by minutes-to-hours.
        if result is None:
            return 0, 0, "0h 0m", []
        _mi, _hrs, _order = result
        total_mi += _mi
        total_drive_hrs += _hrs
        waypoint_order.extend(_order)

    service_hrs = len(waypoints) * (10 / 60)
    total_hrs = total_drive_hrs + service_hrs
    _result = (
        round(total_mi, 1),
        total_hrs,
        f"{int(total_hrs)}h {int((total_hrs * 60) % 60)}m",
        waypoint_order,
    )
    _cache[_key] = (_result, _now)  # cache success only
    # Security audit L3 - bound the process-wide route cache. It is keyed on
    # (home, waypoints) and never evicted, so over a long container uptime it
    # creeps. When it exceeds the cap, drop the oldest ~20% by timestamp.
    try:
        _GMAPS_CACHE_CAP = 2000
        if len(_cache) > _GMAPS_CACHE_CAP:
            _evict = sorted(_cache.items(), key=lambda kv: kv[1][1])
            for _ek, _ in _evict[:max(1, len(_cache) // 5)]:
                _cache.pop(_ek, None)
    except Exception as _gc_e:
        _log_err("gmaps_cache_evict", _gc_e)
    return _result

@st.cache_data(ttl=120, show_spinner=False)
def fetch_worker_task_counts():
    """Per-worker assigned-task count keyed by last-10 digits of phone. Cached 2 min.

    Jun 18 2026 — Nick: 🔵N badge was 0 for every IC.
    Old implementation depended on /workers?analytics=true returning a 'tasks'
    array, which Onfleet has been inconsistent about. New strategy:
      1. Try the legacy 'tasks' array on /workers?analytics=true (fast path).
      2. Also accept 'taskCount' / 'numberOfTasks' / 'activeTasks' if present.
      3. If everything resolves to zero, fall back to counting state=1+state=2
         tasks from /tasks/all and grouping by worker id, then mapping back to
         phone via the workers list. Heavier (2-3 extra HTTP calls) but it's the
         only authoritative count when the analytics field is missing.
    """
    try:
        # ---------- Step 1: workers list (with analytics) ----------
        res = requests.get("https://onfleet.com/api/v2/workers?analytics=true", headers=headers, timeout=10)
        if res.status_code != 200:
            _log_err("fetch_worker_task_counts/workers", f"HTTP {res.status_code}")
            return {}
        workers = res.json() or []

        counts = {}
        id_to_phone = {}
        any_nonzero = False
        for w in workers:
            _ph = re.sub(r'\D', '', str(w.get('phone', '')))[-10:]
            if not _ph:
                continue
            if w.get('id'):
                id_to_phone[w['id']] = _ph
            # Try several shapes Onfleet has used over the years.
            _tasks_field = w.get('tasks')
            if isinstance(_tasks_field, list):
                _cnt = len(_tasks_field)
            elif isinstance(_tasks_field, int):
                _cnt = _tasks_field
            else:
                _cnt = (
                    w.get('taskCount')
                    or w.get('numberOfTasks')
                    or w.get('activeTasks')
                    or (w.get('analytics') or {}).get('numberOfTasks')
                    or 0
                )
                try:
                    _cnt = int(_cnt)
                except Exception:
                    _cnt = 0
            counts[_ph] = _cnt
            if _cnt > 0:
                any_nonzero = True

        # ---------- Step 2: fast path success → return ----------
        if any_nonzero:
            return counts

        # ---------- Step 3: fallback — count state=1 + state=2 tasks ----------
        # Onfleet docs: state=1 (assigned), state=2 (active/in-progress).
        # Bound the window to 30 days back to keep pagination short.
        from time import time as _now
        _since_ms = int((_now() - 30 * 86400) * 1000)
        fallback_counts = {}
        for _state in (1, 2):
            _last_id = None
            for _pg in range(50):  # 50-page cap × 100/page = 5000 tasks/state max
                _url = f"https://onfleet.com/api/v2/tasks/all?state={_state}&from={_since_ms}"
                if _last_id:
                    _url += f"&lastId={_last_id}"
                try:
                    _tr = requests.get(_url, headers=headers, timeout=15)
                except Exception as _e:
                    _log_err("fetch_worker_task_counts/tasks-fetch", _e)
                    break
                if _tr.status_code != 200:
                    _log_err("fetch_worker_task_counts/tasks", f"HTTP {_tr.status_code} state={_state}")
                    break
                _body = _tr.json()
                _tasks = _body.get('tasks', []) if isinstance(_body, dict) else (_body or [])
                for _t in _tasks:
                    _wid = _t.get('worker')
                    if not _wid:
                        continue
                    _ph = id_to_phone.get(_wid)
                    if not _ph:
                        continue
                    fallback_counts[_ph] = fallback_counts.get(_ph, 0) + 1
                _last_id = _body.get('lastId') if isinstance(_body, dict) else None
                if not _last_id:
                    break

        # Merge fallback into counts (preserves phones with 0 from step 1 too).
        for _ph, _cnt in fallback_counts.items():
            counts[_ph] = _cnt
        return counts
    except Exception as e:
        _log_err("fetch_worker_task_counts", e)
        return {}

@st.cache_data(ttl=600)
def load_ic_database(sheet_url):
    try:
        export_url = f"{sheet_url.split('/edit')[0]}/export?format=csv&gid=0"
        return pd.read_csv(export_url)
    except Exception as e:
        _log_err("load_ic_database", e)
        return None

@st.cache_data(ttl=600, show_spinner=False)
def _warm_load_ic_df():
    """IC database for the headless startup warm-up. Replicates the dispatcher
    path EXACTLY (pd.read_csv + lowercased column headers) so the warm-computed
    cluster signature AND the IC-matching inside the cluster loop line up with a
    normal dispatcher load. load_ic_database() does NOT lowercase headers, so the
    warm path used to build a differently-cased ic_df and poison the shared
    cluster cache. Returns an empty DataFrame (never None) on failure."""
    try:
        if DB_ENGINE is not None:
            return _ic_df_from_db(DB_ENGINE)
        _url = f"{IC_SHEET_URL.split('/edit')[0]}/export?format=csv&gid=0"
        _df = pd.read_csv(_url)
        _df.columns = [str(_c).strip().lower() for _c in _df.columns]
        return _df
    except Exception as _e:
        _log_err("warm_load_ic_df", _e)
        return pd.DataFrame()

def process_digital_pool(master_bar=None):
    prog_bar = master_bar if master_bar else st.progress(0)
    prog_bar.progress(0.1, text="📥 Fetching National Tasks from Onfleet...")
    # Tick digital overlay timer
    _ov = st.session_state.get('_loading_overlay')
    _st = st.session_state.get('_loading_start')
    if _ov and _st:
        import time as _t2
        _el = int(_t2.time() - _st); _m = _el // 60; _s = _el % 60
        _ov.markdown(f"""<style>@keyframes spin{{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
.dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:36px 32px;text-align:center;margin:20px 0;}}
.dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;border-top:4px solid #0f766e;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
.dcc-pill{{display:inline-block;font-size:13px;font-weight:700;color:#0f766e;background:#ccfbf1;border-radius:20px;padding:4px 14px;margin-top:12px;}}</style>
<div class='dcc-card'><div class='dcc-spin'></div>
<p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Initializing Digital Pool</p>
<div class='dcc-pill'>⏱ {_m}:{_s:02d}</div><div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: ~30s (pulls live digital task pool).</div></div>""", unsafe_allow_html=True)
    
    # 🌍 SHARED ONFLEET PULL — see _fetch_onfleet_open_tasks_cached() near the
    # top of this file. If a Dispatcher already pulled within the last 60s, this
    # returns instantly. Digital tab + every pod tab now share one pull.
    try:
        _onfleet_data = _fetch_onfleet_open_tasks_cached()
    except Exception as _e:
        st.error(f"Onfleet API Error: {_e}")
        _log_err("process_digital_pool", f"shared pull failed: {type(_e).__name__}: {_e}")
        return
    target_team_ids = _onfleet_data['target_team_ids']
    esc_team_ids    = _onfleet_data['esc_team_ids']
    st.session_state['_fn_team_id'] = _onfleet_data.get('fn_team_id')
    st.session_state['_fn_worker_id'] = _onfleet_data.get('fn_worker_id')
    all_tasks_raw   = _onfleet_data['tasks']
    if _onfleet_data.get('_hit_cap'):
        _log_err("process_digital_pool", f"hit pagination cap (200 pages)")
        st.warning(f"⚠️ Hit pagination cap of 200 pages while fetching Onfleet tasks. Some tasks may be missing.")
    prog_bar.progress(0.39, text=f"📡 Got {len(all_tasks_raw)} tasks from Onfleet...")
        
    prog_bar.progress(0.4, text="🔍 Isolating Digital Service Calls...")
    # Tick digital overlay timer
    _ov = st.session_state.get('_loading_overlay')
    _st = st.session_state.get('_loading_start')
    if _ov and _st:
        import time as _t2
        _el = int(_t2.time() - _st); _m = _el // 60; _s = _el % 60
        _ov.markdown(f"""<style>@keyframes spin{{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
.dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:36px 32px;text-align:center;margin:20px 0;}}
.dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;border-top:4px solid #0f766e;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
.dcc-pill{{display:inline-block;font-size:13px;font-weight:700;color:#0f766e;background:#ccfbf1;border-radius:20px;padding:4px 14px;margin-top:12px;}}</style>
<div class='dcc-card'><div class='dcc-spin'></div>
<p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Initializing Digital Pool</p>
<div class='dcc-pill'>⏱ {_m}:{_s:02d}</div><div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: ~30s (pulls live digital task pool).</div></div>""", unsafe_allow_html=True)
    
    # 🌟 STRICT DIGITAL FILTER
    # --- 🌟 STRICT DIGITAL FILTER ---
    # 🌟 Site Survey added to digital classification (May 2026) — Onfleet tasks whose
    # taskType contains "site survey" now flow into the Digital pool alongside Service /
    # Ins-Rem / Offline. Same treatment as the other digital types (grouped into Digital tab).
    DIGITAL_WHITELIST = ["service", "ins/rem", "offline", "site survey", "digital install"]
    fresh_sent_db, _, _archived_wos, _history_db = fetch_sent_records_from_sheet()
    st.session_state['_history_db'] = _history_db
    st.session_state.sent_db = fresh_sent_db
    st.session_state['archived_wos'] = _archived_wos

    pool = []
    unique_tasks_dict = {t['id']: t for t in all_tasks_raw}
    
    for t in unique_tasks_dict.values():
        # 🚫 DRIVER-HOME PSEUDO-TASK GUARD: Onfleet auto-generates
        # "Start at driver address" / "End at driver's address" tasks for native
        # Route Plans, bound to a contractor's home address. Real kiosk tasks
        # always carry a `state` custom field; the pseudo-tasks don't. Require it
        # so the pseudo-tasks never land in the dispatchable pool.
        _has_state_cf = any(
            (str(_f.get('name', '')).strip().lower() == 'state'
             or str(_f.get('key', '')).strip().lower() == 'state')
            and str(_f.get('value', '')).strip()
            for _f in (t.get('customFields') or [])
        )
        # 🌟 May 2026 — TX task rescue: previously dropped ANY task without a
        # state customField, silently kicking real TX tasks that came in via
        # manual Onfleet entry / alt integrations. Fall back to
        # destination.address.state — if the address has a US state the task
        # is bucketable, customField or not. The customField still wins as
        # authoritative dispatch tagging (normalize_state below reads addr).
        _addr_state_probe = ''
        try: _addr_state_probe = str((t.get('destination') or {}).get('address', {}).get('state', '') or '').strip()
        except Exception: _addr_state_probe = ''
        if not _has_state_cf and not _addr_state_probe:
            continue

        container = t.get('container', {})
        c_type = str(container.get('type', '')).upper()
        # 🛡️ DOUBLE-ROUTING GUARD: skip tasks already assigned to a worker.
        if c_type == 'WORKER' or t.get('worker'):
            continue
        if c_type == 'TEAM' and container.get('team') not in target_team_ids: continue

        addr = t.get('destination', {}).get('address', {})
        stt = normalize_state(addr.get('state', ''))
        is_esc = (c_type == 'TEAM' and container.get('team') in esc_team_ids)
        
        # --- 🔍 STRICT CLASSIFICATION ENGINE (v4 - Final) ---
        native_details = str(t.get('taskDetails', '')).strip()
        custom_fields = t.get('customFields') or []
        
        # 1. EXTRACT OFFICIAL CUSTOM FIELDS
        custom_task_type = ""
        custom_boosted = ""
        venue_name = ""
        venue_id = ""
        kiosk_id = ""
        sio = ""
        client_company = ""
        campaign_name = ""
        location_in_venue = ""
        customer_type = ""
        # 🎨 Pull art file token(s) from the OnFleet Task Details / notes field
        # (free-text). See extract_art_file() up top for the heuristic.
        art_file = extract_art_file(t.get('taskDetails', '') or t.get('notes', ''))
        
        # Default UI display to native details unless a custom field overwrites it
        tt_val = native_details 
        
        for f in custom_fields:
            f_name = str(f.get('name', '')).strip().lower()
            f_key = str(f.get('key', '')).strip().lower()
            f_val = str(f.get('value', '')).strip()
            f_val_lower = f_val.lower()
            
            # Capture Official 'Task Type' Custom Field
            if f_name in ['task type', 'tasktype'] or f_key in ['tasktype', 'task_type']:
                custom_task_type = f_val_lower
                tt_val = f_val # 🌟 UI Display is now officially the Custom Field
                
            # Capture 'Boosted Standard' Custom Field
            if f_name in ['boosted standard', 'boostedstandard'] or f_key in ['boostedstandard', 'boosted_standard']:
                custom_boosted = f_val_lower
                
            # Capture Escalation
            if 'escalation' in f_name or 'escalation' in f_key:
                if f_val_lower in ['1', '1.0', 'true', 'yes'] or 'escalation' in f_val_lower:
                    is_esc = True

            # 🌟 Capture Field Nation metadata fields
            if f_name in ['venuename', 'venue name'] or f_key in ['venuename', 'venue_name']:
                venue_name = f_val
            if f_name in ['venueid', 'venue id'] or f_key in ['venueid', 'venue_id']:
                venue_id = f_val
            # 🔢 SIO — Single-Item Order from OnFleet's `sio` custom field.
            # Locals carry a real numeric SIO; Defaults carry literal "Default".
            # Drives slip's SIO column + Locals grouping/sort. Captured at
            # ingest so it flows through to ghost slips (via stopData).
            if f_name == 'sio' or f_key == 'sio':
                sio = f_val
            if f_name in ['kioskid', 'kiosk id', 'kiosk_id'] or f_key in ['kioskid', 'kiosk_id']:
                kiosk_id = f_val
            if f_name in ['clientcompany', 'client company'] or f_key in ['clientcompany', 'client_company']:
                client_company = f_val
            if f_name in ['locationinvenue', 'location in venue'] or f_key in ['locationinvenue', 'location_in_venue']:
                location_in_venue = f_val
            if f_name in ['campaignname', 'campaign name'] or f_key in ['campaignname', 'campaign_name']:
                campaign_name = f_val  # 🌟 Captured separately so Client Company can't overwrite it
            # 🌟 Customer Type — drives National vs Regional/Local bucketing on the packing slip.
            if f_name in ['customer type', 'customertype'] or f_key in ['customertype', 'customer_type']:
                customer_type = f_val

        # 🌟 Campaign Name always wins over Client Company for FN Customer Name
        client_company = campaign_name or client_company

        # 2. CHECK REGULAR (STATIC) EXEMPTIONS FIRST
        # Expanded to include "escalation" to prevent crossing over
        search_string = f"{native_details} {custom_task_type}".lower()
        REGULAR_EXEMPTIONS = ["photo", "magnet", "continuity", "new ad", "pull down", "kiosk install", "kiosk removal", "escalation"]
        is_exempt = any(ex in search_string for ex in REGULAR_EXEMPTIONS)
        
        # 3. STRICT DIGITAL CHECK
        is_digital_task = False

        if not is_exempt:
            # Rule A: Task Type contains service, ins/rem, offline, or site survey
            if any(trigger in custom_task_type for trigger in ["service", "ins/rem", "offline", "site survey"]):
                is_digital_task = True
            # 🌟 Rule B: Boosted Standard contains the word 'digital' (Matches 'Premium_Digital')
            elif "digital" in custom_boosted:
                is_digital_task = True
        
        # 🌟 SPEED FIX: Skip routing math entirely if it's not strictly digital
        if not is_digital_task: 
            continue
            
        # --- 4. ASSIGN STATUS & POOL ---
        t_status = fresh_sent_db.get(t['id'], {}).get('status', 'ready').lower() if t['id'] in fresh_sent_db else 'ready'
        t_wo = fresh_sent_db.get(t['id'], {}).get('wo', 'none') if t['id'] in fresh_sent_db else 'none'
        
        pool.append({
            "id": t['id'], "city": addr.get('city', 'Unknown'), "state": stt,
            "full": f"{addr.get('number','')} {addr.get('street','')}, {addr.get('city','')}, {stt}",
            "zip": addr.get('postalCode', ''),
            "lat": t['destination']['location'][1], "lon": t['destination']['location'][0],
            "escalated": is_esc, "task_type": tt_val, "is_digital": True, "db_status": t_status, "wo": t_wo,
            "boosted_standard": custom_boosted,
            "venue_name": venue_name,
            "venue_id": venue_id,
            "kiosk_id": kiosk_id,
            "sio": sio,
            "client_company": client_company,
            "location_in_venue": location_in_venue,
            "art_file": art_file,
            "customer_type": customer_type,
        })

    prog_bar.progress(0.6, text=f"🗺️ Routing {len(pool)} Digital Tasks...")
    # Tick digital overlay timer
    _ov = st.session_state.get('_loading_overlay')
    _st = st.session_state.get('_loading_start')
    if _ov and _st:
        import time as _t2
        _el = int(_t2.time() - _st); _m = _el // 60; _s = _el % 60
        _ov.markdown(f"""<style>@keyframes spin{{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
.dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:36px 32px;text-align:center;margin:20px 0;}}
.dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;border-top:4px solid #0f766e;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
.dcc-pill{{display:inline-block;font-size:13px;font-weight:700;color:#0f766e;background:#ccfbf1;border-radius:20px;padding:4px 14px;margin-top:12px;}}</style>
<div class='dcc-card'><div class='dcc-spin'></div>
<p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Initializing Digital Pool</p>
<div class='dcc-pill'>⏱ {_m}:{_s:02d}</div><div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: ~30s (pulls live digital task pool).</div></div>""", unsafe_allow_html=True)
    
    # 3. Route ONLY the Digital Tasks
    ic_df = st.session_state.get('ic_df', pd.DataFrame())
    lat_col = next((col for col in ic_df.columns if 'lat' in str(col).lower()), 'lat')
    lng_col = next((col for col in ic_df.columns if 'lng' in str(col).lower()), 'lng')
    v_ics_base = ic_df[~ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)].dropna(subset=[lat_col, lng_col]).copy() if (lat_col in ic_df.columns and lng_col in ic_df.columns) else pd.DataFrame()

    clusters = []
    route_radius = 20 # Strict 20-mile radius for digital (May 18 2026 — was 25)
    
    while pool:
        anc = pool.pop(0)
        candidates = []
        rem = []
        
        for t in pool:
            if anc['db_status'] in ['sent', 'accepted', 'field_nation']:
                if t['db_status'] == anc['db_status'] and t['wo'] == anc['wo']: candidates.append((0, t))
                else: rem.append(t)
            elif anc['db_status'] in ['ready', 'declined']:
                if t['db_status'] in ['ready', 'declined']:
                    d = haversine(anc['lat'], anc['lon'], t['lat'], t['lon'])
                    if d <= route_radius: candidates.append((d, t))
                    else: rem.append(t)
                else: rem.append(t)
        
        candidates.sort(key=lambda x: x[0])
        
        group = [anc]
        unique_stops = {anc['full']}
        spillover = []
        for _, t in candidates:
            if len(unique_stops) < 20 or t['full'] in unique_stops:
                group.append(t); unique_stops.add(t['full'])
            else: spillover.append(t)
        rem.extend(spillover)
        
        has_ic = False
        ic_dist = 0
        if not v_ics_base.empty:
            dists = [haversine(anc['lat'], anc['lon'], lat, lng) for lat, lng in zip(v_ics_base[lat_col], v_ics_base[lng_col])]
            valid_ics = v_ics_base.copy()
            valid_ics['d'] = dists
            valid_ics = valid_ics[valid_ics['d'] <= 100]
            if not valid_ics.empty:
                best_ic = valid_ics.sort_values('d').iloc[0]
                has_ic = True
                ic_dist = best_ic['d']

        status = "Ready" if anc['db_status'] not in ['sent', 'accepted', 'finalized'] else anc['db_status'].capitalize()

        # 🌟 DIGITAL FLAGGING: No IC, IC >40mi, or rate >$50/stop → Flagged
        if status == "Ready":
            if not has_ic or ic_dist > 40:
                status = "Flagged"
            else:
                ic_loc_d = f"{anc['lat']},{anc['lon']}"
                _, d_hrs, _, _ = get_gmaps(ic_loc_d, tuple(list(unique_stops)[:25]))
                d_pay = round(d_hrs * 25.0, 2)
                d_rate = round(d_pay / len(unique_stops), 2) if unique_stops else 0
                if d_rate >= HIGH_RATE_FLAG_THRESHOLD:
                    status = "Flagged"

        # Any-match boosted-tier (see process_pod for rationale).
        _d_boosted_vals = [str(x.get('boosted_standard', '')).lower() for x in group if x.get('boosted_standard')]
        if any('local plus' in v for v in _d_boosted_vals):
            _d_boosted_tag = 'local plus'
        elif any('boosted' in v for v in _d_boosted_vals):
            _d_boosted_tag = 'boosted'
        else:
            _d_boosted_tag = ''
        clusters.append({
            "data": group, "center": [anc['lat'], anc['lon']], "stops": len(unique_stops), 
            "city": anc['city'], "state": anc['state'], "status": status, "has_ic": has_ic,
            "esc_count": sum(1 for x in group if x.get('escalated')),
            "is_digital": True,
            "boosted_tag": _d_boosted_tag,
            "inst_count": sum(1 for x in group if "install" in str(x.get('task_type', '')).lower()),
            "remov_count": sum(1 for x in group if "remove" in str(x.get('task_type', '')).lower()),
            "wo": anc['wo']
        })
        pool = rem

    # Save to dedicated Global Digital State
    st.session_state['global_digital_clusters'] = clusters
    # Re-apply Global_Digital bundles after the rebuild.
    _replay_bundles("Global_Digital")
    prog_bar.empty()

# --- CORE LOGIC ---
@st.cache_resource(show_spinner=False)
def _pod_cluster_store():
    """Process-wide shared cache of computed pod clusters \u2014 one entry per pod,
    keyed internally by a content signature. Lets the FIRST dispatcher to
    initialize a pod absorb the heavy classify+route cost; every other session
    within the same data window deepcopies the result instead of re-routing
    800+ tasks. Main relief for GIL contention when all 5 dispatchers
    initialize at once. See the SHARED CLUSTER CACHE block in process_pod()."""
    return {}


def process_pod(pod_name, master_bar=None, pod_idx=0, total_pods=1, warm_only=False,
                refresh_tasks=False):
    config = POD_CONFIGS[pod_name]
    
    # Logic to handle if we are doing a single pod or a global pull
    pod_weight = 1.0 / total_pods
    start_pct = pod_idx * pod_weight
    
    # Use the master bar if provided, otherwise create a local one
    prog_bar = None if warm_only else (master_bar if master_bar else st.progress(0))
    _revamp_last_progress_log = [0.0]
    
    def update_prog(rel_val, msg):
        if warm_only: return  # headless startup warm-up: no progress UI
        if os.environ.get("DCC_REVAMP_UI") == "1":
            _now_log = time.monotonic()
            if rel_val in (0.0, 0.4) or _now_log - _revamp_last_progress_log[0] >= 30:
                print(f"[revamp/sync] {pod_name}: {msg}", flush=True)
                _revamp_last_progress_log[0] = _now_log
        global_val = min(start_pct + (rel_val * pod_weight), 0.99)
        prog_bar.progress(global_val, text=f"[{pod_name}] {msg}")
        # 🌟 Tick the loading overlay timer if it exists
        _ov = st.session_state.get('_loading_overlay')
        _st = st.session_state.get('_loading_start')
        _pn = st.session_state.get('_loading_pod')
        if _ov and _st and _pn:
            import time as _t
            elapsed = int(_t.time() - _st)
            m = elapsed // 60; s = elapsed % 60
            _ov.markdown(f"""
                <style>
                    @keyframes spin {{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
                    .dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;
                        padding:36px 32px;text-align:center;margin:20px 0;}}
                    .dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;
                        border-top:4px solid #633094;border-radius:50%;
                        animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
                    .dcc-pill{{display:inline-block;font-size:13px;font-weight:700;
                        color:#633094;background:#f3e8ff;border-radius:20px;
                        padding:4px 14px;margin-top:12px;}}
                </style>
                <div class='dcc-card'>
                    <div class='dcc-spin'></div>
                    <p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Initializing {_pn} Pod</p>
                    <p style='font-size:13px;color:#64748b;margin:0 0 8px 0;'>{msg}</p>
                    <div class='dcc-pill'>⏱ {m}:{s:02d}</div>
                <div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: 2–4 min for full pod fleet (Red Pod is heaviest).</div>
                </div>
            """, unsafe_allow_html=True)

    try:
        update_prog(0.0, "📥 Extracting tasks...")

        # 🌍 SHARED ONFLEET PULL — see _fetch_onfleet_open_tasks_cached() near the
        # top of this file. First caller within the 60s TTL window pays the network
        # cost; every subsequent caller (including the other 4 pods in the same
        # Initialize-All-Pods loop) gets the cached result for free. This is the
        # main fix for "Bandwidth quota exceeded" errors when multiple dispatchers
        # are using the app concurrently.
        try:
            # The manual Check new tasks action must bypass the 60-second
            # process-wide Onfleet cache. Clear once before a multi-pod run;
            # subsequent pods reuse this fresh result.
            if refresh_tasks:
                _fetch_onfleet_open_tasks_cached.clear()
            _onfleet_data = _fetch_onfleet_open_tasks_cached()
        except Exception as _e:
            if not warm_only: st.error(f"Onfleet API Error: {_e}")
            _log_err("process_pod", f"shared pull failed: {type(_e).__name__}: {_e}")
            return
        target_team_ids    = _onfleet_data['target_team_ids']
        esc_team_ids       = _onfleet_data['esc_team_ids']
        cvs_remov_team_ids = _onfleet_data['cvs_remov_team_ids']
        if not warm_only: st.session_state['_fn_team_id'] = _onfleet_data.get('fn_team_id')
        if not warm_only: st.session_state['_fn_worker_id'] = _onfleet_data.get('fn_worker_id')
        # Security audit M9 - warn loudly if the FN placeholder worker could
        # not be resolved; FN assignment would otherwise half-complete.
        if not warm_only and _onfleet_data.get('_fn_worker_lookup_failed'):
            _log_err("process_pod", "FN placeholder worker lookup failed (None propagated)")
            try:
                st.warning(
                    "\u26a0\ufe0f Could not resolve the Field Nation placeholder "
                    "worker - FN assignments may not fully sync. Retry after a refresh."
                )
            except Exception:
                pass
        all_tasks          = _onfleet_data['tasks']
        # Stash fresh state=0 IDs for STATE=0 IS AUTHORITATIVE reclaim in run_pod_tab.
        if not warm_only:
            st.session_state[f'_state0_ids_{pod_name}'] = {str(_t.get('id', '')).strip() for _t in all_tasks}
        if _onfleet_data.get('_hit_cap'):
            _log_err("process_pod", f"hit pagination cap (200 pages)")
            if not warm_only: st.warning(f"\u26a0\ufe0f Hit pagination cap of 200 pages while fetching Onfleet tasks for {pod_name}. Some tasks may be missing.")
        update_prog(0.4, f"📡 Got {len(all_tasks)} tasks — routing...")

        # PERFORMANCE FIX: Fetch Google Sheets data once before the loop
        fresh_sent_db, _, _archived_wos, _history_db = fetch_sent_records_from_sheet()
        if not warm_only: st.session_state['_history_db'] = _history_db
        if not warm_only: st.session_state.sent_db = fresh_sent_db
        if not warm_only: st.session_state['archived_wos'] = _archived_wos

        # \U0001f680 SHARED CLUSTER CACHE \u2014 the classify+route block below produces an
        # identical result for every dispatcher viewing the same pod within the
        # same data window. Compute it once; other sessions deepcopy the result
        # instead of re-routing 800+ tasks. This is the main relief for GIL
        # contention when all 5 dispatchers initialize at once. On ANY miss it
        # falls through to the original code path (the `else`) unchanged \u2014 so a
        # cache bug can only cost the speedup, never correctness.
        import copy as _copy
        _ic_df_peek = (_warm_load_ic_df() if warm_only else st.session_state.get('ic_df', pd.DataFrame()))
        # Security audit H8 - hash the actual IC lat/lng content into the
        # cache signature. Previously only len(_ic_df_peek) was used, so an
        # IC whose home address was corrected (row count unchanged) kept
        # serving stale cached clusters / distances until a redeploy.
        try:
            _lat_col = next((c for c in _ic_df_peek.columns if 'lat' in str(c).lower()), None)
            _lng_col = next((c for c in _ic_df_peek.columns if 'lng' in str(c).lower()
                             or 'lon' in str(c).lower()), None)
            if _lat_col is not None and _lng_col is not None:
                _ic_geo_sig = hash(tuple(
                    zip(_ic_df_peek[_lat_col].astype(str),
                        _ic_df_peek[_lng_col].astype(str))
                ))
            else:
                _ic_geo_sig = len(_ic_df_peek)
        except Exception:
            _ic_geo_sig = len(_ic_df_peek)
        _clu_sig = (
            pod_name,
            len(all_tasks),
            hash(frozenset(_t.get('id', '') for _t in all_tasks)),
            len(fresh_sent_db),
            hash(frozenset(
                (_k, str(_v.get('status', '')), str(_v.get('wo', '')))
                for _k, _v in fresh_sent_db.items()
            )),
            len(_ic_df_peek),
            _ic_geo_sig,
        )
        _clu_store = _pod_cluster_store()
        _clu_hit = _clu_store.get(pod_name)
        if _clu_hit is not None and _clu_hit.get('sig') == _clu_sig:
            clusters                   = _copy.deepcopy(_clu_hit['clusters'])
            total_pool                 = _clu_hit['total_pool']
            _skipped_no_state_cf       = _clu_hit['skipped_no_state_cf']
            _skipped_assigned          = _clu_hit['skipped_assigned']
            _skipped_wrong_team        = _clu_hit['skipped_wrong_team']
            _skipped_out_of_pod_states = _clu_hit['skipped_out_of_pod_states']
        else:
            pool = []
            _skipped_assigned = 0
            # 📊 ATTRITION COUNTERS: track tasks dropped at each filter gate
            _skipped_no_state_cf = 0
            _skipped_wrong_team = 0
            _skipped_out_of_pod_states = 0
            # 🌐 Excluded teams — FN team (handled via ghost path so showing in
            # Ready/Flagged would double-render) + explicitly-blacklisted teams
            # like "ZZZ Test Team" (substring matched in _fetch_onfleet_open_tasks_cached).
            _fn_team_id_local = _onfleet_data.get('fn_team_id')
            _excluded_team_set = set(_onfleet_data.get('excluded_team_ids') or [])
            if _fn_team_id_local:
                _excluded_team_set.add(_fn_team_id_local)
            # 🐛 DIAGNOSTIC — break down what's being dropped at the team gate.
            # Maps team_id -> count of dropped tasks. Surfaced in the attrition
            # expander so we can identify any team we still need to add or exclude.
            _dropped_by_team = {}
            for t in all_tasks:
                # 🚫 DRIVER-HOME PSEUDO-TASK GUARD: Onfleet auto-generates
                # "Start at driver address" / "End at driver's address" tasks for native
                # Route Plans, bound to a contractor's home address. Real kiosk tasks
                # always carry a `state` custom field; the pseudo-tasks don't. Require it
                # so the pseudo-tasks never land in the dispatchable pool.
                _has_state_cf = any(
                    (str(_f.get('name', '')).strip().lower() == 'state'
                     or str(_f.get('key', '')).strip().lower() == 'state')
                    and str(_f.get('value', '')).strip()
                    for _f in (t.get('customFields') or [])
                )
                # 🌟 May 2026 — TX task rescue (see comment at other gate site).
                _addr_state_probe = ''
                try: _addr_state_probe = str((t.get('destination') or {}).get('address', {}).get('state', '') or '').strip()
                except Exception: _addr_state_probe = ''
                if not _has_state_cf and not _addr_state_probe:
                    _skipped_no_state_cf += 1
                    continue

                container = t.get('container', {})
                c_type = str(container.get('type', '')).upper()

                # 🛡️ DOUBLE-ROUTING GUARD — conditional version (May 18 2026 v3).
                # Two competing requirements collided today:
                #   (a) Pure-strict (skip on `c_type=='WORKER' or t.get('worker')`)
                #       hides 170+ unassigned-in-OnFleet tasks that have stale
                #       worker-id refs (deleted workers, FN placeholder leftovers,
                #       post-revoke orphans). Dispatcher loses access to real work.
                #   (b) Pure-loose (skip only on `c_type=='WORKER'`) leaks
                #       just-dispatched tasks back into Ready as duplicates of
                #       their Awaiting/FN cluster.
                # The fix: skip on `c_type=='WORKER'` always. ALSO skip if the
                # task has a worker reference AND fresh_sent_db confirms it as
                # dispatched (status in sent/accepted/declined/finalized/field_nation).
                # If the worker ref is stale (task not in sent_db), let it through —
                # the dispatcher needs to see it. The downstream `_awaiting_tids`
                # dedup guard catches any leakage the conditional misses.
                _t_id = str(t.get('id', '')).strip()
                if c_type == 'WORKER':
                    _skipped_assigned += 1
                    continue
                if t.get('worker') and _t_id in fresh_sent_db:
                    _sb_stat = str(fresh_sent_db[_t_id].get('status', '')).lower()
                    if _sb_stat in ('sent', 'accepted', 'declined', 'finalized', 'field_nation'):
                        _skipped_assigned += 1
                        continue

                # 🌐 ORG-container ("Unassigned" in OnFleet UI) → always include.
                # TEAM-container → include unless team is in _excluded_team_set
                # (currently FN team + "ZZZ Test Team").
                # May 18 2026 — switched from APPROVED_TEAMS allow-list to a
                # deny-list so the Unassigned area + arbitrary team pools flow
                # through; only explicitly-blacklisted teams get dropped.
                if c_type == 'TEAM' and container.get('team') in _excluded_team_set:
                    _skipped_wrong_team += 1
                    _dt_id = container.get('team')
                    _dropped_by_team[_dt_id] = _dropped_by_team.get(_dt_id, 0) + 1
                    continue

                addr = t.get('destination', {}).get('address', {})
                stt = normalize_state(addr.get('state', ''))
                is_esc = (c_type == 'TEAM' and container.get('team') in esc_team_ids)
            
                # --- 🔍 STRICT CLASSIFICATION ENGINE (v5) ---
                native_details = str(t.get('taskDetails', '')).strip()
                custom_fields = t.get('customFields') or []
            
                # 1. EXTRACT OFFICIAL CUSTOM FIELDS
                custom_task_type = ""
                custom_boosted = ""
                tt_val = native_details # Fallback UI display
                venue_name = ""
                venue_id = ""
                kiosk_id = ""
                sio = ""
                client_company = ""
                campaign_name = ""
                location_in_venue = ""
                customer_type = ""
                # 🎨 Pull art file token(s) from the OnFleet Task Details / notes field
                # (free-text). See extract_art_file() up top for the heuristic.
                art_file = extract_art_file(t.get('taskDetails', '') or t.get('notes', ''))
            
                for f in custom_fields:
                    f_name = str(f.get('name', '')).strip().lower()
                    f_key = str(f.get('key', '')).strip().lower()
                    f_val = str(f.get('value', '')).strip()
                    f_val_lower = f_val.lower()
                
                    # Capture Official 'Task Type'
                    if f_name in ['task type', 'tasktype'] or f_key in ['tasktype', 'task_type']:
                        custom_task_type = f_val_lower
                        tt_val = f_val # Set the UI badge text
                    
                    # Capture Official 'Boosted Standard'
                    if f_name in ['boosted standard', 'boostedstandard'] or f_key in ['boostedstandard', 'boosted_standard']:
                        custom_boosted = f_val_lower
                    
                    # Capture Escalation (Adds the ⭐)
                    if 'escalation' in f_name or 'escalation' in f_key:
                        if f_val_lower in ['1', '1.0', 'true', 'yes'] or 'escalation' in f_val_lower:
                            is_esc = True

                    # 🌟 Capture Field Nation metadata fields
                    if f_name in ['venuename', 'venue name'] or f_key in ['venuename', 'venue_name']:
                        venue_name = f_val
                    if f_name in ['venueid', 'venue id'] or f_key in ['venueid', 'venue_id']:
                        venue_id = f_val
                    # 🔢 SIO — see site-1 comment.
                    if f_name == 'sio' or f_key == 'sio':
                        sio = f_val
                    if f_name in ['kioskid', 'kiosk id', 'kiosk_id'] or f_key in ['kioskid', 'kiosk_id']:
                        kiosk_id = f_val
                    if f_name in ['clientcompany', 'client company'] or f_key in ['clientcompany', 'client_company']:
                        client_company = f_val
                    if f_name in ['locationinvenue', 'location in venue'] or f_key in ['locationinvenue', 'location_in_venue']:
                        location_in_venue = f_val
                    if f_name in ['campaignname', 'campaign name'] or f_key in ['campaignname', 'campaign_name']:
                        campaign_name = f_val  # 🌟 Captured separately so Client Company can't overwrite it
                    # 🌟 Customer Type — drives National vs Regional/Local bucketing on the packing slip.
                    if f_name in ['customer type', 'customertype'] or f_key in ['customertype', 'customer_type']:
                        customer_type = f_val

                # 🌟 Campaign Name always wins over Client Company for FN Customer Name
                client_company = campaign_name or client_company
                # 2. CHECK REGULAR (STATIC) EXEMPTIONS FIRST
                # Combines native and custom type to ensure "Magnet" or "Photo" are never missed
                search_string = f"{native_details} {custom_task_type}".lower()
                REGULAR_EXEMPTIONS = ["photo", "magnet", "continuity", "new ad", "pull down", "kiosk", "escalation"]
                is_exempt = any(ex in search_string for ex in REGULAR_EXEMPTIONS)

                # 3. APPLY DIGITAL RULES
                # Locked strictly to the triggers you defined
                # 🌟 May 2026 — Site Survey added; see comment at first DIGITAL_WHITELIST.
                DIGITAL_WHITELIST = ["service", "ins/rem", "offline", "site survey", "digital install"]
                is_digital_task = False
                # Jul 2026 (Nick): "Digital Install" custom task type wins over the
                # kiosk exemption. Otherwise notes like "install kiosk here" trip
                # the "kiosk" exemption above and the task lands in static.
                if "digital install" in custom_task_type:
                    is_digital_task = False
                # Jul 2026 (Nick): tasks in the "D - Digital Routes" OnFleet
                # team override the whitelist/exemption path — team membership
                # wins so the task always lands in the Digital tab.
                _digital_team_ids_local = _onfleet_data.get('digital_team_ids', [])
                if c_type == 'TEAM' and container.get('team') in _digital_team_ids_local:
                    is_digital_task = True
                elif not is_exempt:
                    # Rule A: Official Task Type matches whitelist
                    if any(trigger in custom_task_type for trigger in DIGITAL_WHITELIST):
                        is_digital_task = True
                    # 🌟 Rule B: Boosted Standard contains the word 'digital' (matches 'Premium_Digital')
                    elif "digital" in custom_boosted:
                        is_digital_task = True
                # --- 3. ASSIGN STATUS & POOL ---
                t_status = 'ready'
                t_wo = 'none'
                if t['id'] in fresh_sent_db:
                    t_status = fresh_sent_db[t['id']].get('status', 'ready').lower()
                    t_wo = fresh_sent_db[t['id']].get('wo', 'none')
            
                if stt not in config['states']:
                    _skipped_out_of_pod_states += 1
                    continue
                if stt in config['states']:
                    _remov_keywords = ["kiosk removal", "remove kiosk"]
                    _is_cvs_team = (c_type == 'TEAM' and container.get('team') in cvs_remov_team_ids)
                    _is_removal = _is_cvs_team and any(kw in f"{native_details} {custom_task_type}".lower() for kw in _remov_keywords)
                    pool.append({
                        "id": t['id'], 
                        "city": addr.get('city', 'Unknown'), 
                        "state": stt,
                        "full": f"{addr.get('number','')} {addr.get('street','')}, {addr.get('city','')}, {stt}",
                        "zip": addr.get('postalCode', ''),
                        "lat": t['destination']['location'][1], 
                        "lon": t['destination']['location'][0],
                        "escalated": is_esc, 
                        "task_type": tt_val,
                        "is_digital": is_digital_task,
                        "is_removal": _is_removal,
                        "boosted_standard": custom_boosted,
                        "db_status": t_status, 
                        "wo": t_wo,
                        "venue_name": venue_name,
                        "venue_id": venue_id,
                        "kiosk_id": kiosk_id,
                        "sio": sio,
                        "client_company": client_company,
                        "location_in_venue": location_in_venue,
                        "art_file": art_file,
                        "customer_type": customer_type,
                    })
                
            clusters = []
            total_pool = len(pool)
            ic_df = (_warm_load_ic_df() if warm_only else st.session_state.get('ic_df', pd.DataFrame()))
        
            # 🌟 CRITICAL FIX: Safe extraction using standardized headers
            lat_col = next((col for col in ic_df.columns if 'lat' in str(col).lower()), 'lat')
            lng_col = next((col for col in ic_df.columns if 'lng' in str(col).lower()), 'lng')
        
            if lat_col in ic_df.columns and lng_col in ic_df.columns:
                v_ics_base = ic_df[~ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)].dropna(subset=[lat_col, lng_col]).copy()
            else:
                v_ics_base = pd.DataFrame()

            while pool:
                # Routing progress calculation
                rel_prog = 0.4 + (0.6 * (1 - (len(pool) / total_pool if total_pool > 0 else 1)))
                update_prog(rel_prog, f"🗺️ Routing {len(pool)} remaining tasks...")
            
                anc = pool.pop(0)
            
                # --- NEW: Strict Digital Separation & Dynamic Radius ---
                anc_tt = str(anc.get('task_type', '')).lower()
                anc_is_digital = anc.get('is_digital', False)
                anc_is_removal = anc.get('is_removal', False)
                anc_status = anc.get('db_status', 'ready')
                anc_wo = anc.get('wo', 'none')
            
                # Set radius strictly based on type.
                # May 18 2026 — bumped down 35 → 25 → 20 for static routes after
                # Nick tightened twice. Bay Area cross-bridge merges fully separated
                # at 20mi. Matches the digital radius AND CLUSTER_RADIUS below so
                # cluster geometry stays consistent across all three paths.
                route_radius = 20
            
                candidates = []; rem = []
                for t in pool:
                    t_tt = str(t.get('task_type', '')).lower()
                    t_is_digital = t.get('is_digital', False)
                    t_is_removal = t.get('is_removal', False)
                    t_status = t.get('db_status', 'ready')
                    t_wo = t.get('wo', 'none')
                
                    # Rule 1: Digital, Removal, and Standard never mix
                    if anc_is_digital == t_is_digital and anc_is_removal == t_is_removal:
                    
                        # Rule 2: Sent and Accepted are FROZEN
                        # 🌟 FIX 1: Add 'field_nation' so these routes stay grouped together!
                        if anc_status in ['sent', 'accepted', 'field_nation']:
                            # Bypasses distance! ONLY groups if the Work Order matches perfectly.
                            if t_status == anc_status and t_wo == anc_wo:
                                candidates.append((0, t)) 
                            else:
                                rem.append(t)
                            
                        # Rule 3: Ready and Declined are LIQUID (They can mix!)
                        elif anc_status in ['ready', 'declined']:
                            if t_status in ['ready', 'declined']:
                                d = haversine(anc['lat'], anc['lon'], t['lat'], t['lon'])
                                if d <= route_radius: 
                                    candidates.append((d, t))
                                else: 
                                    rem.append(t)
                            else:
                                rem.append(t)
                    else:
                        rem.append(t)
            
                candidates.sort(key=lambda x: x[0])
            
                # --- STOP LIMIT: 10 for CVS Removal, 20 for all others ---
                stop_limit = 10 if anc_is_removal else 20
                group = [anc]
                unique_stops = {anc['full']}
                spillover = []
            
                for _, t in candidates:
                    if len(unique_stops) < stop_limit or t['full'] in unique_stops:
                        group.append(t)
                        unique_stops.add(t['full'])
                    else:
                        spillover.append(t)
            
                # 🌟 BRIDGE: Put spillover back and fix the 'd' column error
                rem.extend(spillover)
            
                # --- 📡 1. IC SEARCH & DISTANCE CHECK (OPTIMIZED) ---
                has_ic = False
                ic_dist = 0
                closest_ic_loc = f"{anc['lat']},{anc['lon']}" 
            
                if not v_ics_base.empty:
                    # 🚀 OPTIMIZATION: Use list comprehension instead of pandas .apply(). It is ~100x faster.
                    dists = [
                        haversine(anc['lat'], anc['lon'], lat, lng) 
                        for lat, lng in zip(v_ics_base[lat_col], v_ics_base[lng_col])
                    ]
                
                    valid_ics = v_ics_base.copy()
                    valid_ics['d'] = dists
                    valid_ics = valid_ics[valid_ics['d'] <= 100]
                
                    if not valid_ics.empty:
                        best_ic = valid_ics.sort_values('d').iloc[0]
                        has_ic = True
                        ic_dist = best_ic['d']
                        closest_ic_loc = _ic_home_loc(best_ic, closest_ic_loc)

                def check_viability(grp):
                    seen = set(); u_locs = []
                    for x in grp:
                        if x['full'] not in seen: seen.add(x['full']); u_locs.append(x['full'])
                    if not u_locs: return 0, 0
                
                    # 🚀 OPTIMIZATION: Reverted back to real Google Maps!
                    # Wrapping u_locs[:25] in a tuple() makes Streamlit's cache process it instantly.
                    _, hrs, _, _ = get_gmaps(closest_ic_loc, tuple(u_locs[:25]))
                    pay = round(hrs * 25.0, 2) # 🌟 STRICTLY HOURLY ($25/hr)
                    return round(pay / len(u_locs), 2), len(u_locs)
            
                gate_avg, _ = check_viability(group)
            
                # --- 🚦 2. UPDATED FLAGGING LOGIC ---
                if anc_status in ['sent', 'accepted', 'finalized']:
                    status = anc_status.capitalize()
                else:
                    status = "Ready" # Default status
                
                    # Flag Criteria A: High Rate (> $23/stop)
                    if gate_avg > 23.00:
                        if len(group) > 1:
                            removed = group.pop()
                            new_avg, _ = check_viability(group)
                            if new_avg <= 23.00:
                                rem.append(removed)
                            else:
                                group.append(removed)
                                status = "Flagged"
                        else:
                            status = "Flagged"
                
                    # Flag Criteria B: Long Distance (> 60 miles) or No Contractor
                    if not has_ic or ic_dist > 60:
                        status = "Flagged"

                # --- 📊 3. COUNTERS & SAVE TO SESSION ---
                g_data = group

                # 🌟 CLEANUP: No need to loop again; the anchor already knows!
                route_is_digital = anc_is_digital
            
                # Route is tagged boosted/local-plus if ANY task in the cluster has that tier.
                # The header pill ensures the dispatcher sees boosted routes immediately,
                # even when the boosted task is a single one inside a mostly-pulldown cluster.
                # Drill-down to the specific stop + campaign happens via make_venue_details,
                # which counts boosted tasks per location and per campaign row.
                _boosted_vals = [str(x.get('boosted_standard', '')).lower() for x in g_data if x.get('boosted_standard')]
                if any('local plus' in v for v in _boosted_vals):
                    _boosted_tag = 'local plus'
                elif any('boosted' in v for v in _boosted_vals):
                    _boosted_tag = 'boosted'
                else:
                    _boosted_tag = ''

                clusters.append({
                    "data": g_data, 
                    "center": [anc['lat'], anc['lon']], 
                    "stops": len(set(x['full'] for x in g_data)), 
                    "city": anc['city'], "state": anc['state'],
                    "status": status,
                    "has_ic": has_ic,
                    "esc_count": sum(1 for x in g_data if x.get('escalated')),
                    "is_digital": route_is_digital,
                    "is_removal": anc_is_removal,
                    "boosted_tag": _boosted_tag,
                    "inst_count": sum(1 for x in g_data if "install" in str(x.get('task_type', '')).lower()),
                    "remov_count": sum(1 for x in g_data if str(x.get('task_type', '')).lower() in ["kiosk removal", "remove kiosk"]),
                    "wo": anc_wo,
                    "_default_ic_loc": closest_ic_loc
                })
                pool = rem

            _clu_store[pod_name] = {
                'sig': _clu_sig,
                'clusters': _copy.deepcopy(clusters),
                'total_pool': total_pool,
                'skipped_no_state_cf': _skipped_no_state_cf,
                'skipped_assigned': _skipped_assigned,
                'skipped_wrong_team': _skipped_wrong_team,
                'skipped_out_of_pod_states': _skipped_out_of_pod_states,
            }
        if not warm_only: st.session_state[f"clusters_{pod_name}"] = clusters
        # 🚀 PRE-WARM ROUTE GMAPS — render_dispatch calls get_gmaps(default_ic_loc,
        # stops) for every Ready card on first render, which is what makes the
        # Dispatch (left) column slow to populate. Each cluster now carries its
        # default IC location; warm the shared _gmaps_route_cache for those exact
        # keys off the main thread so render_dispatch hits a warm cache instead of
        # making ~50 live Mapbox calls. Pure I/O + cache reads, wrapped in try/except
        # — degrades gracefully: if it fails, render_dispatch just computes live.
        try:
            _warm_pairs = []
            if os.environ.get("DCC_REVAMP_UI") != "1":
                for _wc in clusters:
                    _wloc = _wc.get('_default_ic_loc')
                    if not _wloc:
                        continue
                    _wstops = tuple(dict.fromkeys(_t['full'] for _t in _wc.get('data', []) if _t.get('full')))
                    if _wstops:
                        _warm_pairs.append((_wloc, _wstops))
            def _warm_route_gmaps(_pairs):
                for _wloc, _wstops in _pairs:
                    try:
                        get_gmaps(_wloc, _wstops)
                    except Exception as _we:
                        _log_err("warm_route_gmaps", _we)
            # Security audit M12 - skip the warm spawn if a warm pass for
            # this pod is already running, so re-initializing a pod does not
            # stack threads each making ~50 Mapbox calls.
            if _warm_pairs and pod_name not in _GMAPS_WARM_INFLIGHT:
                _GMAPS_WARM_INFLIGHT.add(pod_name)
                def _warm_route_gmaps_guarded(_pairs, _pn):
                    try:
                        _warm_route_gmaps(_pairs)
                    finally:
                        _GMAPS_WARM_INFLIGHT.discard(_pn)
                threading.Thread(
                    target=_warm_route_gmaps_guarded,
                    args=(_warm_pairs, pod_name), daemon=True,
                ).start()
        except Exception as _wt_e:
            _log_err("warm_route_gmaps/setup", _wt_e)
        if warm_only:
            return  # headless startup warm-up: cluster + gmaps caches populated; skip session-scoped writes below
        # Re-apply any bundles the dispatcher previously confirmed for this pod, so a
        # full re-init via Initialize Data doesn\'t silently undo them.
        _replay_bundles(pod_name)
        if _skipped_assigned > 0:
            _log_err(f"process_pod/{pod_name}", f"skipped {_skipped_assigned} already-assigned tasks (state=0 leak)")

        # 📊 Save attrition funnel to session state for the supercard expander.
        # NOTE: raw_fetched == after_dedup now because the shared cached helper
        # (_fetch_onfleet_open_tasks_cached) dedupes internally before returning.
        # The pre-dedup count is no longer accessible here.
        st.session_state[f'_attrition_{pod_name}'] = {
            'raw_fetched': len(all_tasks),
            'after_dedup': len(all_tasks),
            'skipped_no_state_cf': _skipped_no_state_cf,
            'skipped_assigned_worker': _skipped_assigned,
            'skipped_wrong_team': _skipped_wrong_team,
            'skipped_out_of_pod_states': _skipped_out_of_pod_states,
            # Use the pre-routing pool size, not the post-routing one. The
            # `while pool:` loop consumes the list during cluster-building, so
            # by the time we reach this save line, `len(pool)` is always 0 —
            # which is what showed up as the buggy "Count: 0" on row 6 of the
            # attrition table. total_pool was captured BEFORE routing began.
            'final_pool': total_pool,
        }
        if not master_bar: 
            prog_bar.empty()

        # The revamp fetches worker counts only when a route detail is opened.
        # Doing this after extraction kept the entire route list behind one
        # additional paginated Onfleet request.
        if os.environ.get("DCC_REVAMP_UI") != "1":
            st.session_state['_worker_counts'] = fetch_worker_task_counts()

    except Exception as e:
        if not warm_only: st.error(f"Error initializing {pod_name}: {str(e)}")
        else: _log_err(f"process_pod/warm/{pod_name}", e)

# 🌅 SELF-WARM ON STARTUP — fires once per process (cold start). A background
# thread runs process_pod(warm_only=True) for all 5 colored pods, populating the
# shared cluster cache + Mapbox cache before any dispatcher logs in. warm_only=True
# makes process_pod do the compute + cache writes but skip every session-scoped /
# UI call, so it is safe to run headless in a thread. The @st.cache_resource guard
# guarantees the body runs exactly once per process; every later rerun is a no-op.
@st.cache_resource(show_spinner=False)
def _startup_warm_guard():
    def _do_startup_warm():
        # Security audit M8 - sleep between pods so this CPU-heavy warm-up
        # yields the GIL to the first dispatcher's render thread instead of
        # starving the very user it is meant to help.
        for _wp in list(POD_CONFIGS.keys()):
            try:
                process_pod(_wp, warm_only=True)
            except Exception as _swe:
                _log_err(f"startup_warm/{_wp}", _swe)
            time.sleep(1.0)
    try:
        threading.Thread(target=_do_startup_warm, daemon=True).start()
    except Exception as _sw_e:
        _log_err("startup_warm/thread-start", _sw_e)
    return True

if os.environ.get("DCC_REVAMP_UI") != "1":
    _startup_warm_guard()

# 🌟 NEW HELPER: Standardized Digital Badges
def get_digi_badges(cluster_data):
    """Labeled task-type breakdown for Digital tab accordion headers.

    Old behavior: returned concatenated unique icons (e.g., "📵🔧"),
    leaving dispatchers to consult the legend to decode the icons.
    New behavior: returns counts + labels (e.g., "📵 1 Offline · 🔧 3 Ins/Rem")
    so the task mix is readable at a glance.

    Categories follow the same substring rules used elsewhere in the file
    (see lines ~4485-4486 and the digital sub-tab pill). Order is fixed
    Offline → Ins/Rem → Service so the layout stays consistent across routes.
    """
    off_n = 0
    ins_n = 0
    svc_n = 0
    for t in cluster_data:
        if not t.get('is_digital'):
            continue
        tt = str(t.get('task_type', '')).lower()
        if 'offline' in tt:
            off_n += 1
        elif 'ins/re' in tt:
            ins_n += 1
        else:
            svc_n += 1
    parts = []
    if off_n > 0: parts.append(f"📵 {off_n} Offline")
    if ins_n > 0: parts.append(f"🔧 {ins_n} Ins/Rem")
    if svc_n > 0: parts.append(f"⚙️ {svc_n} Service")
    # 🌟 Trailing '|' separates the badge block from the city/state field in
    # the caller's f-string (`{badges} {city}`). Empty when no digital tasks
    # so a non-digital cluster's title doesn't get a stray pipe.
    return (" · ".join(parts) + " |") if parts else ""


# 🔗 Bundled tag for route-card expander headers. Dim non-bold gray, far-right of the
# label, only shown when the cluster has absorbed other routes via the Bundle Routes
# preview-and-confirm flow. cluster.bundle_count is set by _replay_bundles() below.
def _bundle_pill(cluster):
    _n = cluster.get('bundle_count', 0) if isinstance(cluster, dict) else 0
    return "  ·  :gray[🔗 Bundled]" if _n and _n > 0 else ""


# 🔗 BUNDLE PERSISTENCE — survives process_pod / smart_sync_pod / process_digital_pool
# rebuilds. Each entry is a SET of Onfleet task IDs that the dispatcher decided should
# end up in the same cluster. After any cluster rebuild we call _replay_bundles() and
# it re-merges the relevant clusters. Keyed by pod_name (or "Global_Digital").
def _bundle_clusters_store(pod_name):
    """Return (read_key, write_key) for the cluster store this pod uses. Global_Digital
    has its own bucket; everything else uses clusters_{pod_name}."""
    if pod_name == "Global_Digital":
        return "global_digital_clusters"
    return f"clusters_{pod_name}"

# 🔗 BUNDLE PERSISTENCE (May 2026): _bundle_map lived only in st.session_state,
# so a browser refresh (or a stale-deploy reload) wiped every bundle the
# dispatcher had committed. These helpers push the bundle map to GAS Script
# Properties keyed by pod, and pull it back on first visit each session so
# bundles survive reloads. Requires GAS handlers `saveBundleMap` and
# `loadBundleMap` — see companion diff in the GAS file.
# Bundle map is pod-wide shared state today (every dispatcher in a pod sees
# the same bundles), not per-dispatcher, even though bundle_maps' schema has
# room for a real dispatcher_id. Using this fixed sentinel for every caller
# reproduces that exact pod-wide sharing under the DB backend -- switching to
# a real per-user dispatcher_id would be a behavior change, not a pure
# backend swap, so it's deliberately not done here.
_BUNDLE_MAP_SHARED_DISPATCHER_ID = "_shared"


def _save_bundle_map_to_gas(pod_name):
    """Fire-and-forget persist of the current session's bundle map for pod_name."""
    try:
        _bm = st.session_state.get('_bundle_map', {}) or {}
        _entries = _bm.get(pod_name, []) or []
        _payload = [",".join(sorted(list(_e))) for _e in _entries if _e]
        if DB_ENGINE is not None:
            _da.save_bundle_map(DB_ENGINE, pod_name, _BUNDLE_MAP_SHARED_DISPATCHER_ID, _payload)
            return
        requests.post(GAS_WEB_APP_URL, json={
            "action": "saveBundleMap",
            "auth_secret": GAS_AUTH,
            "pod": pod_name,
            "bundles": _payload,
        }, timeout=8)
    except Exception as _e:
        _log_err(f"_save_bundle_map_to_gas/{pod_name}", _e)


def _load_bundle_map_from_gas(pod_name):
    """Fetch persisted bundle map for pod_name. Returns list of sets
    of task IDs (same shape as st.session_state['_bundle_map'][pod_name]).
    Returns [] on any error so the caller falls back cleanly."""
    try:
        if DB_ENGINE is not None:
            _raw = _da.load_bundle_map(DB_ENGINE, pod_name, _BUNDLE_MAP_SHARED_DISPATCHER_ID) or []
        else:
            _r = requests.post(GAS_WEB_APP_URL, json={
                "action": "loadBundleMap",
                "auth_secret": GAS_AUTH,
                "pod": pod_name,
            }, timeout=8)
            _data = _r.json() if _r.status_code == 200 else {}
            _raw = _data.get('bundles', []) or []
        _out = []
        for _b in _raw:
            _ids = set(str(_s).strip() for _s in str(_b).split(',') if str(_s).strip())
            if _ids:
                _out.append(_ids)
        return _out
    except Exception as _e:
        _log_err(f"_load_bundle_map_from_gas/{pod_name}", _e)
        return []



def _hydrate_bundle_map_once(pod_name):
    """Called from run_pod_tab on first visit per session per pod. Loads the
    persisted bundle map from GAS into st.session_state['_bundle_map'][pod_name]
    exactly once, gated by a session flag. Idempotent + safe under Streamlit
    reruns (the flag survives)."""
    _flag_key = f"_bundle_map_hydrated_{pod_name}"
    if st.session_state.get(_flag_key):
        return
    _loaded = _load_bundle_map_from_gas(pod_name)
    if _loaded:
        _bm = st.session_state.setdefault('_bundle_map', {})
        # Merge, not overwrite — preserves any bundles committed this session
        # before the load completed (shouldn't happen in practice, but safe).
        _existing = _bm.get(pod_name, []) or []
        _combined = list(_existing)
        for _new in _loaded:
            if not any(_new == _e for _e in _combined):
                _combined.append(_new)
        _bm[pod_name] = _combined
        st.session_state['_bundle_map'] = _bm
    st.session_state[_flag_key] = True


def _unbundle_cluster(pod_name, cluster_task_ids):
    """Reverse a bundle: remove any _bundle_map entries that overlap with this
    cluster's task_ids so the constituent routes reappear as separate clusters.
    Also clears the pod's cached cluster list so process_pod rebuilds fresh.
    Returns True if anything was removed, False if there was no bundle to undo.

    Jun 18 2026 — Nick: added "revert bundled work orders back to original routes"
    button. Bundles were previously one-way after Confirm; the only workaround was
    to sign out or restart the Python process."""
    _target_set = {str(_t).strip() for _t in cluster_task_ids if str(_t).strip()}
    if not _target_set:
        return False
    bm = st.session_state.get('_bundle_map', {}) or {}
    pm = bm.get(pod_name, []) or []
    if not pm:
        return False
    _kept = [entry for entry in pm if not (entry & _target_set)]
    if len(_kept) == len(pm):
        return False  # nothing overlapped
    bm[pod_name] = _kept
    st.session_state['_bundle_map'] = bm
    # Force re-cluster on the next render so the merged cluster splits back.
    _store_key = _bundle_clusters_store(pod_name)
    st.session_state.pop(_store_key, None)
    # Wipe stale bundle_count so the pill disappears immediately.
    return True


def _commit_bundle(pod_name, target_task_ids, source_task_ids):
    """Record that target_task_ids and source_task_ids should be in the same cluster.
    Idempotent — overlapping entries get merged into one set so a route bundled three
    times shows up as a single entry, not three."""
    combined = set(target_task_ids) | set(source_task_ids)
    bm = st.session_state.setdefault('_bundle_map', {})
    pm = bm.setdefault(pod_name, [])
    keep = []
    for entry in pm:
        if entry & combined:
            combined |= entry
        else:
            keep.append(entry)
    keep.append(combined)
    bm[pod_name] = keep
    # 🔗 Persist the just-committed bundle map for this pod so a page reload
    # doesn't wipe it. Fire-and-forget; failure is logged, doesn't block UI.
    _save_bundle_map_to_gas(pod_name)

def _replay_bundles(pod_name):
    """Re-merge any clusters that the dispatcher previously bundled. Safe to call after
    every cluster rebuild — if the bundle has already been applied (single cluster
    contains all the listed task IDs) this is a no-op for that entry."""
    bm = st.session_state.get('_bundle_map', {})
    entries = bm.get(pod_name, []) or []
    if not entries:
        return
    _key = _bundle_clusters_store(pod_name)
    cls = st.session_state.get(_key, [])
    if not cls:
        return
    _changed = False
    for entry in entries:
        # Find every cluster index whose data overlaps with this bundle\'s task-ID set.
        members = []
        for ci, c in enumerate(cls):
            tids = {str(t['id']).strip() for t in c.get('data', [])}
            if tids & entry:
                members.append(ci)
        if len(members) < 2:
            # Bundle intent still exists — mark the surviving cluster as bundled
            # so the pill persists after process_pod naturally re-merges the
            # source routes (common when they're within cluster radius).
            if len(members) == 1:
                _tgt = cls[members[0]]
                _tgt_tids = {str(t['id']).strip() for t in _tgt.get('data', [])}
                if entry.issubset(_tgt_tids) and not _tgt.get('bundle_count'):
                    _tgt['bundle_count'] = 1
                    _changed = True
            continue  # already merged, or members no longer present
        target_idx = members[0]
        source_indices = members[1:]
        target = cls[target_idx]
        existing_ids = {str(t['id']).strip() for t in target.get('data', [])}
        for si in source_indices:
            for t in cls[si].get('data', []):
                tid = str(t['id']).strip()
                if tid not in existing_ids:
                    target['data'].append(t)
                    existing_ids.add(tid)
        target['stops'] = len(set(x['full'] for x in target['data']))
        target['inst_count'] = sum(1 for x in target['data'] if 'install' in str(x.get('task_type','')).lower())
        target['remov_count'] = sum(1 for x in target['data'] if str(x.get('task_type','')).lower() in ('kiosk removal','remove kiosk'))
        target['esc_count'] = sum(1 for x in target['data'] if x.get('escalated'))
        target['bundle_count'] = target.get('bundle_count', 0) + len(source_indices)
        # Drop merged sources (highest indices first to preserve earlier ones).
        for si in sorted(source_indices, reverse=True):
            cls.pop(si)
        _changed = True
    if _changed:
        st.session_state[_key] = cls


# 🌟 Reusable pill helper for route card titles (Sent/Accepted/Declined/Finalized expanders
# and the Dispatch column big card). Returns a string like " | 🛠️ 5 Kiosk" for static
# routes or " | 🔧 3 Ins/2 Rem" for digital ones — empty string when no relevant tasks.
# Color is enforced via the surrounding markdown's CSS where needed.
def get_task_pill(cluster_data):
    if not cluster_data:
        return ""
    is_digi = any(t.get('is_digital') for t in cluster_data)
    if is_digi:
        ins_n = sum(1 for t in cluster_data if t.get('is_digital') and 'install' in str(t.get('task_type','')).lower() and 'ins/re' not in str(t.get('task_type','')).lower())
        rem_n = sum(1 for t in cluster_data if t.get('is_digital') and ('remov' in str(t.get('task_type','')).lower() or 'remove' in str(t.get('task_type','')).lower()))
        insrem_n = sum(1 for t in cluster_data if t.get('is_digital') and 'ins/re' in str(t.get('task_type','')).lower())
        parts = []
        if ins_n > 0: parts.append(f"{ins_n} Ins")
        if rem_n > 0: parts.append(f"{rem_n} Rem")
        if insrem_n > 0: parts.append(f"{insrem_n} Ins/Rem")
        return f" | 🔧 {' / '.join(parts)}" if parts else ""
    # Static kiosk path: count install tasks
    k_n = sum(1 for t in cluster_data if 'install' in str(t.get('task_type','')).lower())
    return f" | 🛠️ {k_n} Kiosk" if k_n > 0 else ""


# 🌟 NEW HELPER: Groups clusters by State, then sorts them by geographical proximity
def group_and_sort_by_proximity(bucket):
    if not bucket: return []
    grouped = {}
    for c in bucket:
        stt = c.get('state', 'UNKNOWN')
        if stt not in grouped: grouped[stt] = []
        grouped[stt].append(c)
    
    final_list = []
    for stt in sorted(grouped.keys()):
        state_cls = grouped[stt]
        if not state_cls: continue
        
        # Start with the first cluster and chain the nearest neighbors
        sorted_st_cls = [state_cls.pop(0)]
        while state_cls:
            last_center = sorted_st_cls[-1]['center']
            # Find the closest remaining cluster in this state
            closest_idx, min_d = 0, float('inf')
            for idx, x in enumerate(state_cls):
                d = haversine(last_center[0], last_center[1], x['center'][0], x['center'][1])
                if d < min_d:
                    min_d, closest_idx = d, idx
            sorted_st_cls.append(state_cls.pop(closest_idx))
        
        final_list.extend(sorted_st_cls)
    return final_list
# 🌟 NEW HELPER: Groups Awaiting routes by Date Sent, unifying Live and Ghost routes
def _merge_same_wo_ghosts(ghost_routes):
    """Jun 18 2026 — Nick: bundled route showed as two separate cards in Sent
    because saveRoute produced two sheet rows for the same WO. Merge ghosts
    that share a WO + status into one display ghost so the dispatcher sees the
    bundle they made, not the artifact of how it got written.

    Merge rules:
      - Group by (wo, status). Different statuses (sent vs accepted) stay
        separate so we don't hide a transition.
      - Combine task_ids, stops, tasks, pay. Sum pay across rows (each row's
        comp came from one dispatch; if both were $60 the bundle is $120).
      - Use earliest route_ts so sort order is stable and history makes sense.
      - Keep first hash for popover/reroute identification — the revoke button
        uses cluster_hash which the GAS archiveRoute accepts as a partial
        match anyway.
    """
    if not ghost_routes:
        return ghost_routes
    by_key = {}
    order = []
    for g in ghost_routes:
        _wo = str(g.get('wo', '') or '').strip()
        _status = str(g.get('status', '') or '').strip().lower()
        if not _wo:
            # Routes without a WO can't be merged safely — keep as-is.
            _key = ('__no_wo__', id(g))
        else:
            _key = (_wo, _status)
        if _key in by_key:
            _existing = by_key[_key]
            # Combine task_ids (de-dup, preserve order)
            _seen = set(str(t).strip() for t in _existing.get('task_ids', []))
            for _t in g.get('task_ids', []) or []:
                _t = str(_t).strip()
                if _t and _t not in _seen:
                    _existing.setdefault('task_ids', []).append(_t)
                    _seen.add(_t)
            # Sum scalar metrics
            try:
                _existing['stops'] = int(_existing.get('stops', 0)) + int(g.get('stops', 0) or 0)
            except Exception:
                pass
            try:
                _existing['tasks'] = int(_existing.get('tasks', 0)) + int(g.get('tasks', 0) or 0)
            except Exception:
                pass
            try:
                _existing['pay'] = float(_existing.get('pay', 0) or 0) + float(g.get('pay', 0) or 0)
            except Exception:
                pass
            try:
                _existing['kCnt'] = int(_existing.get('kCnt', 0) or 0) + int(g.get('kCnt', 0) or 0)
            except Exception:
                pass
            # Combine locs strings (already pipe-delimited)
            _e_locs = (_existing.get('locs', '') or '').strip()
            _g_locs = (g.get('locs', '') or '').strip()
            if _g_locs and _g_locs not in _e_locs:
                _existing['locs'] = (_e_locs + ' | ' + _g_locs).strip(' |') if _e_locs else _g_locs
            # Combine stop_data lists
            _existing.setdefault('stop_data', []).extend(g.get('stop_data', []) or [])
            # Keep earliest route_ts (sort field is route_ts; oldest wins so the
            # bundle stays at its original date in the column).
            _e_ts = str(_existing.get('route_ts', '') or '')
            _g_ts = str(g.get('route_ts', '') or '')
            if _g_ts and (not _e_ts or _g_ts < _e_ts):
                _existing['route_ts'] = _g_ts
            # Flag so the card can show a tiny "Bundled" badge if rendering supports it.
            _existing['_merged_count'] = int(_existing.get('_merged_count', 1) or 1) + 1
            _existing.setdefault('_merged_hashes', [_existing.get('hash')])
            if g.get('hash') and g.get('hash') not in _existing['_merged_hashes']:
                _existing['_merged_hashes'].append(g.get('hash'))
        else:
            # Clone so we don't mutate the original dict
            by_key[_key] = dict(g)
            by_key[_key]['_merged_count'] = 1
            order.append(_key)
    return [by_key[k] for k in order]


def unify_and_sort_by_date(live_routes, ghost_routes, live_hashes):
    """Combine live (in-progress) clusters with ghost (sheet-only) routes for
    display in Sent / Accepted / Finalized tabs.

    Jul 23 2026 — added session_state memoization. Was iterating hundreds of
    ghosts on every fragment rerun; now runs once per unique input hash. Cache
    key = hash(live task_ids | ghost task_ids | live_hashes) so any real change
    invalidates. Cheap to compute vs the full merge + sort.
    """
    # Fast cache-key hash — join task IDs (sorted) into a single string, hash once
    try:
        _lr_key = "|".join(sorted(
            ",".join(sorted(str(t.get('id','')) for t in (c.get('data') or [])))
            for c in (live_routes or [])
        ))
        _gr_key = "|".join(sorted(
            (str(g.get('hash','')) + ":" + ",".join(sorted(str(t) for t in (g.get('task_ids') or []))))
            for g in (ghost_routes or [])
        ))
        _lh_key = ",".join(sorted(str(h) for h in (live_hashes or set())))
        _cache_key = hashlib.md5(f"{_lr_key}||{_gr_key}||{_lh_key}".encode()).hexdigest()
        _cache = st.session_state.setdefault('_unify_cache', {})
        if _cache_key in _cache:
            return _cache[_cache_key]
    except Exception:
        _cache_key = None

    ghost_routes = _merge_same_wo_ghosts(ghost_routes or [])
    unified = []
    live_clusters_with_tids = []
    for c in live_routes:
        tids = {str(t.get('id', '')).strip() for t in c.get('data', []) if t.get('id')}
        live_clusters_with_tids.append((c, tids))
    live_task_ids = set().union(*(s for _, s in live_clusters_with_tids)) if live_clusters_with_tids else set()

    suppressed_live_ids = set()
    fragmented_ghosts = set()
    for g in ghost_routes:
        g_tids = [str(_t).strip() for _t in g.get('task_ids', []) if str(_t).strip()]
        if not g_tids:
            continue
        if g.get('hash') in live_hashes:
            continue
        matching_live = [c for c, tids in live_clusters_with_tids if tids & set(g_tids)]
        if len(matching_live) >= 2:
            covered_ids = set().union(*(set(str(t.get('id','')).strip() for t in c.get('data', [])) for c in matching_live))
            if all(tid in covered_ids for tid in g_tids):
                for c in matching_live:
                    suppressed_live_ids.add(id(c))
                fragmented_ghosts.add(g.get('hash'))

    for c in live_routes:
        if id(c) in suppressed_live_ids:
            continue
        c_copy = c.copy()
        c_copy['is_ghost'] = False
        ts = c_copy.get('route_ts', '')
        c_copy['sort_date'] = str(ts).split(' ')[0] if ts else 'Unknown Date'
        unified.append(c_copy)

    for g in ghost_routes:
        if g.get('hash') in fragmented_ghosts:
            pass
        else:
            if g.get('hash') in live_hashes:
                continue
            g_tids = [str(_t).strip() for _t in g.get('task_ids', []) if str(_t).strip()]
            if g_tids and all(tid in live_task_ids for tid in g_tids):
                continue
        g_copy = g.copy()
        g_copy['is_ghost'] = True
        ts = g_copy.get('route_ts', '')
        g_copy['sort_date'] = str(ts).split(' ')[0] if ts else 'Unknown Date'
        unified.append(g_copy)

    unified.sort(key=lambda x: x['sort_date'], reverse=True)

    if _cache_key:
        _c = st.session_state.setdefault('_unify_cache', {})
        _c[_cache_key] = unified
        # Bound cache size to prevent memory bloat (4 panels × 5 pods = 20 max)
        if len(_c) > 24:
            _oldest = next(iter(_c))
            _c.pop(_oldest, None)
    return unified
    
# --- DISPATCH RENDERING ---
# render_dispatch is a fragment — st.rerun() inside (Streamlit 1.39) defaults
# to fragment scope, so clicks re-render only this one route card, not the
# whole pod tab. State changes (session_state, sent_db, etc.) persist across
# fragments and propagate to supercards on the next auto_sync_checker tick
# (every 60s) or any other app-scoped interaction. Calls that genuinely
# need a full-page rerun use st.rerun(scope="app") explicitly.
@st.fragment
def render_dispatch(i, cluster, pod_name, is_sent=False, is_declined=False):
    # Capture current state identifiers (cluster_hash is computed from the ORIGINAL data
    # and stays constant through preview — keeps session-state continuity intact).
    task_ids = [str(t['id']).strip() for t in cluster['data']]
    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()

    # 🔗 BUNDLE PREVIEW — multiselect at the bottom of this fragment can flag nearby
    # clusters for preview. While in preview, this render shows the route card AS IF
    # those clusters were merged. Deselecting reverts the card. The actual merge into
    # session_state only happens when the dispatcher clicks "Confirm Bundle".
    _bundle_select_key = f"bundle_select_{pod_name}_{cluster_hash}"
    # Deferred-clear consumer: Streamlit forbids writing to a widget\'s session-state
    # key AFTER the widget is instantiated, so the Confirm/Clear button handlers below
    # set this intent flag and rerun. Here, BEFORE the multiselect renders, we honor
    # the flag by popping the multiselect\'s saved state (which IS allowed pre-render).
    _bundle_clear_intent_key = f"_bundle_clear_intent_{pod_name}_{cluster_hash}"
    if st.session_state.pop(_bundle_clear_intent_key, False):
        st.session_state.pop(_bundle_select_key, None)
    _preview_hashes = st.session_state.get(_bundle_select_key) or []
    _in_preview = False
    _preview_added_count = 0  # tasks added by preview (for display in Confirm button)
    if _preview_hashes:
        _pv_pool = st.session_state.get(f"clusters_{pod_name}", [])
        _pv_data = list(cluster['data'])
        _pv_seen = {str(_t['id']).strip() for _t in _pv_data}
        _matched_any = False
        for _ph in _preview_hashes:
            for _other in _pv_pool:
                _o_tids_pv = sorted([str(_t['id']).strip() for _t in _other.get('data', [])])
                _o_h_pv = hashlib.md5("".join(_o_tids_pv).encode()).hexdigest()
                if _o_h_pv == _ph:
                    _matched_any = True
                    for _t in _other.get('data', []):
                        _tid = str(_t['id']).strip()
                        if _tid not in _pv_seen:
                            _pv_data.append(_t)
                            _pv_seen.add(_tid)
                            _preview_added_count += 1
                    break
        if _matched_any and _preview_added_count > 0:
            cluster = dict(cluster)
            cluster['data'] = _pv_data
            cluster['stops'] = len(set(_t['full'] for _t in _pv_data))
            cluster['inst_count'] = sum(1 for _t in _pv_data if 'install' in str(_t.get('task_type','')).lower())
            cluster['remov_count'] = sum(1 for _t in _pv_data if str(_t.get('task_type','')).lower() in ('kiosk removal','remove kiosk'))
            cluster['esc_count'] = sum(1 for _t in _pv_data if _t.get('escalated'))
            task_ids = [str(_t['id']).strip() for _t in _pv_data]
            _in_preview = True

    # 📜 Route history banner — documents the 3 events that matter when a route
    # comes back to Ready: Declined (from the contractor portal, sourced from the sheet),
    # Revoked (dispatcher pulled it back), Re-Routed (dispatcher moved it to a new IC).
    # Apr 27 2026 — Revoked/Re-Routed are now ALSO sheet-backed via Archive, so the
    # banner survives session resets. Session-state actions are still merged in for
    # instant feedback before the GAS round-trip lands.
    if not is_sent and not is_declined:
        _events = []
        _seen = set()

        # 1. Declined / Revoked / Re-Routed events from sheet-backed history_db.
        _hist_db = st.session_state.get('_history_db', {})
        _STATUS_TO_KIND = {
            'declined':       'Declined',
            'revoked':        'Revoked',
            'ghost-archived': 'Revoked',  # archived ghost rows are conceptually revokes
            're-routed':      'Re-Routed',
        }
        for _tid in task_ids:
            for _h in _hist_db.get(_tid, []):
                _kind = _STATUS_TO_KIND.get(_h.get('status', ''))
                if not _kind:
                    continue
                _key = (_kind, _h.get('name',''), _h.get('time',''), _h.get('wo',''))
                if _key in _seen:
                    continue
                _seen.add(_key)
                _events.append({
                    'kind': _kind,
                    'ic':   _h.get('name',''),
                    'wo':   _h.get('wo',''),
                    'time': _h.get('time',''),
                    'ts':   _h.get('raw_ts'),
                })

        # 2. Revoked / Re-Routed actions from session state (instant feedback before
        # the GAS archiveRoute round-trip lands in the sheet).
        for _a in st.session_state.get(f'_actions_{cluster_hash}', []):
            _act = _a.get('action','')
            if _act in ('Revoked', 'Re-Routed'):
                _key = (_act, _a.get('ic',''), _a.get('time',''), '')
                if _key in _seen:
                    continue
                _seen.add(_key)
                _events.append({
                    'kind': _act,
                    'ic':   _a.get('ic',''),
                    'wo':   '',
                    'time': _a.get('time',''),
                    'ts':   _a.get('ts'),
                })

        # 🌟 If a Declined event is present, suppress any Revoked/Re-Routed events for
        # the same task set — the IC's decline is the meaningful action; the dispatcher
        # revoke/re-route that follows is just operational cleanup and clutters the
        # history banner. Surface only the Declined prompt in that case.
        if any(_e['kind'] == 'Declined' for _e in _events):
            _events = [_e for _e in _events if _e['kind'] == 'Declined']

        if _events:
            _STYLE = {
                'Declined':  ('❌', '#dc2626', 'Declined by'),
                'Revoked':   ('↩️', '#ca8a04', 'Revoked from'),
                'Re-Routed': ('🔄', '#7e22ce', 'Re-routed from'),
            }
            _events.sort(key=lambda e: (e.get('ts') or pd.Timestamp.min), reverse=True)
            _rows_html = []
            for _e in _events[:6]:  # cap to most-recent 6 to keep banner compact
                _icon, _color, _verb = _STYLE[_e['kind']]
                _line = f"<span style='color:{_color};font-weight:700;'>{_icon} {_verb} {_e['ic']}</span>"
                if _e.get('wo'):
                    _line += f" <span style='color:#94a3b8;'>· {_e['wo']}</span>"
                if _e.get('time'):
                    _line += f" <span style='color:#94a3b8;'>· {_e['time']}</span>"
                _rows_html.append(f"<div style='font-size:11px;padding:2px 0;'>{_line}</div>")
            st.markdown(
                "<div style='background:#fef3c7;border-left:3px solid #f59e0b;border-radius:6px;"
                "padding:8px 12px;margin:8px 0 12px 0;'>"
                "<div style='font-size:9px;font-weight:900;color:#92400e;text-transform:uppercase;"
                "letter-spacing:0.08em;margin-bottom:4px;'>📜 Route History</div>"
                + "".join(_rows_html)
                + "</div>",
                unsafe_allow_html=True,
            )

    sync_key = f"sync_{cluster_hash}"
    real_id = st.session_state.get(sync_key)
    link_id = real_id if real_id else "LINK_PENDING"

    # Scrub now runs silently in background_sheet_move — nothing blocks here

    # --- 1. STATE KEYS & INITIALIZATION (🌟 UNIQUE BY POD) ---
    pay_key = f"pay_val_{pod_name}_{cluster_hash}"
    rate_key = f"rate_val_{pod_name}_{cluster_hash}"
    sel_key = f"sel_{pod_name}_{cluster_hash}"
    last_sel_key = f"last_sel_{pod_name}_{cluster_hash}"

    # --- 2. STOP METRICS & PILLS (build dict — UI rendered after financials) ---
    stop_metrics = {}
    for t in cluster['data']:
        addr = t['full']
        if addr not in stop_metrics:
            stop_metrics[addr] = {
                't_count': 0, 'n_ad': 0, 'c_ad': 0, 'd_ad': 0,
                'inst': 0, 'remov': 0, 'digi_off': 0, 'digi_ins': 0, 'digi_srv': 0,
                'custom': {}, 'esc': False, 'is_new': False, 'venue_name': '',
                'boost_cnt': 0, 'lplus_cnt': 0,
            }
        stop_metrics[addr]['t_count'] += 1
        if t.get('escalated'): stop_metrics[addr]['esc'] = True
        # Per-stop boosted/local-plus task counts so the stop row can show 🔥 N / ⭐ N
        # pills alongside the kiosk and escalation indicators.
        _t_bs = str(t.get('boosted_standard','')).lower()
        if 'local plus' in _t_bs: stop_metrics[addr]['lplus_cnt'] += 1
        elif 'boosted' in _t_bs: stop_metrics[addr]['boost_cnt'] += 1
        if t.get('is_new'): stop_metrics[addr]['is_new'] = True
        if not stop_metrics[addr]['venue_name'] and t.get('venue_name'):
            stop_metrics[addr]['venue_name'] = t.get('venue_name', '')
            
        raw_tt = str(t.get('task_type', '')).strip()
        parts = [p.strip().lower() for p in raw_tt.split(',') if p.strip()]
        if "escalation" in parts:
            if len(parts) > 1: parts.remove("escalation") 
            else: parts = ["new ad"] 
        tt = ", ".join(parts)

        found_category = False
        
        # 🌟 Split Digital Tasks
        if t.get('is_digital'):
            if "offline" in tt: stop_metrics[addr]['digi_off'] += 1
            elif "ins/re" in tt: stop_metrics[addr]['digi_ins'] += 1
            else: stop_metrics[addr]['digi_srv'] += 1
            found_category = True
        else:
            if "install" in tt: 
                stop_metrics[addr]['inst'] += 1
                found_category = True
            if any(trigger in tt for trigger in ["kiosk removal", "remove kiosk"]):
                stop_metrics[addr]['remov'] += 1
                found_category = True
            if any(x in tt for x in ["continuity", "photo retake", "swap"]): 
                stop_metrics[addr]['c_ad'] += 1
                found_category = True
            if any(x in tt for x in ["default", "pull down"]): 
                stop_metrics[addr]['d_ad'] += 1
                found_category = True
        
        if any(x in tt for x in ["new ad", "art change", "top"]) or not tt:
            stop_metrics[addr]['n_ad'] += 1
        elif not found_category:
            # 🌟 THE FIX: Push exactly the remaining task type over
            display_tt = tt.title()
            if display_tt not in stop_metrics[addr]['custom']:
                stop_metrics[addr]['custom'][display_tt] = 0
            stop_metrics[addr]['custom'][display_tt] += 1
            
    # --- 3. CONTRACTOR FILTERING (100 MILES) ---
    ic_df = st.session_state.get('ic_df', pd.DataFrame())
    ic_opts = {}
    v_ics = pd.DataFrame()
    # 🔵 Lazy-init worker task counts so the badge works on first page load too.
    # Previously _worker_counts was only populated at the END of process_pod /
    # smart_sync_pod, so any render_dispatch call before those (or after a hard
    # reload that reset session_state) would silently fall back to {} and every
    # contractor would show 🔵0. The cached fetch (@st.cache_data ttl=120) means
    # this is a single ~200ms call once per dispatcher session, not per cluster.
    if '_worker_counts' not in st.session_state:
        st.session_state['_worker_counts'] = fetch_worker_task_counts()
    _worker_counts = st.session_state['_worker_counts']

    if not ic_df.empty:
        ic_df.columns = [str(c).strip().lower() for c in ic_df.columns]
        lat_col, lng_col = 'lat', 'lng'
        # 🔵 PHONE COLUMN AUTO-DETECT (Jun 18 2026 — Nick: 🔵0 for every IC).
        # IC sheet may have 'phone', 'cell', 'mobile', 'phone number', etc. The old
        # code hard-coded 'phone' so any other header silently returned empty and
        # every contractor showed 🔵0. Pick the first column that matches a
        # known phone alias.
        _PHONE_ALIASES = ('phone', 'cell', 'mobile', 'phone number', 'phonenumber',
                          'cell phone', 'cellphone', 'mobile number', 'cell number',
                          'contact phone', 'contact number', 'tel', 'telephone')
        _phone_col = next((a for a in _PHONE_ALIASES if a in ic_df.columns), None)
        if _phone_col is None:
            # Fallback: any column with 'phone'/'cell'/'mobile' substring
            _phone_col = next((c for c in ic_df.columns
                               if any(tok in str(c) for tok in ('phone', 'cell', 'mobile'))),
                              None)
        if lat_col in ic_df.columns and lng_col in ic_df.columns:
            # 🌟 UNRESTRICTED CONTRACTORS — contractors who must be selectable
            # on ANY route, period. This bypasses every filter below in
            # order: the "Field Agent" blanket text exclusion, the
            # missing-coordinates drop, AND the 100-mile distance cap.
            # Match is case-insensitive substring against the IC sheet's
            # 'name' column.
            #   - 'biscardi' — Sep 18 2026 (Nick: "I need to be able to put
            #     Biscardi on any work order").
            #   - 'ledbetter' / 'erlandson' / "o'connor" & 'oconnor' — Sep
            #     2026 (Nick: "lets add the Field Agents as well to be added
            #     in any route: FA Freddy Ledbetter, Michael Erlandson,
            #     Kevin O'Connor"). Matched on last name only (distinctive
            #     enough here) so sheet formatting differences like
            #     "Ledbetter, Freddy" still match, and both apostrophe forms
            #     of O'Connor are listed. These three ARE Field Agents,
            #     which is exactly what the old "Field Agent" text exclusion
            #     just below would otherwise drop before the distance filter
            #     even ran — the exemption has to happen first, not just at
            #     the 100-mile step.
            _UNRESTRICTED_ICS = ['biscardi', 'ledbetter', 'erlandson', "o'connor", 'oconnor']
            def _is_unrestricted_name(_name):
                _nl = str(_name or '').lower()
                return any(_u in _nl for _u in _UNRESTRICTED_ICS)
            _name_col = 'name' if 'name' in ic_df.columns else None

            _fa_mask = ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)
            _unrestricted_all = (ic_df[_name_col].astype(str).apply(_is_unrestricted_name)
                                 if _name_col else pd.Series(False, index=ic_df.index))
            # Field Agents must remain available in the route dropdown.
            # They are employees and already get separate pay handling later;
            # do not filter them out here.
            v_ics = ic_df.copy()

            # Contractor eligibility comes from the hourly Monday -> Postgres
            # sync, stored in the existing `ic list` field so no schema change
            # is needed. Only ACTIVE / IN TRAINING / NEED INSURANCE contractors
            # may appear in route dropdowns. INACTIVE / unknown are excluded,
            # even if they are otherwise unrestricted by distance.
            _elig_col = 'ic list' if 'ic list' in v_ics.columns else None
            if _elig_col:
                _elig_norm = v_ics[_elig_col].astype(str).str.strip().str.upper()
                v_ics = v_ics[_elig_norm.isin(['ACTIVE', 'IN TRAINING', 'NEED INSURANCE'])].copy()
            else:
                # Fail closed if status classification is unexpectedly absent.
                v_ics = v_ics.iloc[0:0].copy()

            # Resolve missing coordinates from the contractor's current
            # location text instead of silently dropping otherwise-eligible ICs.
            # This preserves the real 100-mile rule while avoiding false
            # exclusions caused by incomplete historical lat/lng backfills.
            _unrestricted_v = (v_ics[_name_col].astype(str).apply(_is_unrestricted_name)
                                if _name_col else pd.Series(False, index=v_ics.index))

            def _resolve_ic_coords(_row):
                try:
                    _lat = _row.get(lat_col)
                    _lng = _row.get(lng_col)
                    if pd.notna(_lat) and pd.notna(_lng):
                        return float(_lat), float(_lng)
                except Exception:
                    pass
                _loc = str(_row.get('location', '') or '').strip()
                if _loc:
                    try:
                        _coords = geocode(_loc)
                        if _coords:
                            # Existing geocode() returns (lng, lat).
                            return float(_coords[1]), float(_coords[0])
                    except Exception as _coord_e:
                        _log_err("contractor_dropdown_geocode", _coord_e)
                return None, None

            if not v_ics.empty:
                _resolved = v_ics.apply(_resolve_ic_coords, axis=1)
                v_ics['_resolved_lat'] = _resolved.apply(lambda p: p[0] if p else None)
                v_ics['_resolved_lng'] = _resolved.apply(lambda p: p[1] if p else None)
                v_ics['d'] = v_ics.apply(
                    lambda x: haversine(
                        cluster['center'][0], cluster['center'][1],
                        x.get('_resolved_lat'), x.get('_resolved_lng')
                    ),
                    axis=1
                )
                _is_unrestricted = (v_ics[_name_col].astype(str).apply(_is_unrestricted_name)
                                    if _name_col else pd.Series(False, index=v_ics.index))
                v_ics = v_ics[(v_ics['d'] <= 100) | _is_unrestricted].sort_values('d')
                for _, r in v_ics.iterrows():
                    cert_val = str(r.get('digital certified', '')).strip().upper()
                    cert_icon = " 🔌" if cert_val in ['YES', 'Y', 'TRUE', '1', '1.0'] else ""
                    ic_name = r.get('name', 'Unknown')
                    _phone_raw = str(r.get(_phone_col, '')).strip() if _phone_col else ''
                    # Strip pandas float artifact: numeric phone columns load as
                    # 18087220610.0 — the regex below kept the trailing 0 and
                    # shifted the last 10 digits by one (Vance Nouchi: real
                    # 8087220610 became 0872206100, never matched Onfleet).
                    # int(float()) handles plain floats AND scientific notation.
                    if _phone_raw and _phone_raw not in ('nan', 'None'):
                        try:
                            _phone_raw = str(int(float(_phone_raw)))
                        except (ValueError, TypeError):
                            pass
                    _ic_phone = re.sub(r'\D', '', _phone_raw)[-10:]
                    # Distinguish "fetcher returned no data at all" (empty dict)
                    # from "this worker genuinely has 0 active tasks" so the badge
                    # actually tells the dispatcher something useful when the API
                    # fetch failed silently.
                    if not _worker_counts:
                        _task_cnt = None
                        _cnt_tag = " 🔵?"
                    elif not _ic_phone:
                        _task_cnt = None
                        _cnt_tag = " 🔵—"
                    else:
                        _task_cnt = _worker_counts.get(_ic_phone, 0)
                        _cnt_tag = f" 🔵{_task_cnt}"
                    # An unrestricted contractor with no coordinates on file
                    # gets either haversine()'s fail-soft float('inf') (bad
                    # non-numeric input) or NaN (a genuinely empty lat/lng
                    # cell — float(nan) doesn't raise, so haversine's own
                    # try/except never fires and NaN just propagates through
                    # the arithmetic). Catch both rather than showing
                    # "(inf mi)" or "(nan mi)".
                    _dist_label = "distance unknown" if (pd.isna(r['d']) or r['d'] == float('inf')) else f"{round(r['d'], 1)} mi"
                    _elig = str(r.get('ic list', '') or '').strip().upper()
                    _is_field_agent_row = bool(
                        r.astype(str).str.contains('Field Agent', case=False, na=False).any()
                        if hasattr(r, 'astype') else False
                    )
                    if _is_field_agent_row:
                        _elig_tag = " 👤 FIELD AGENT"
                    elif _elig == 'ACTIVE':
                        _elig_tag = " 🟢 ACTIVE"
                    elif _elig == 'IN TRAINING':
                        _elig_tag = " 🟡 IN TRAINING"
                    elif _elig == 'NEED INSURANCE':
                        _elig_tag = " 🟠 NEED INSURANCE"
                    else:
                        continue
                    label = f"{ic_name}{_elig_tag}{cert_icon}{_cnt_tag} ({_dist_label})"
                    ic_opts[label] = r

    # --- DYNAMIC PRICING SYNC ---
    # Streamlit silently ignores st.session_state[widget_key] = X writes once the
    # user has touched the widget. Workaround: store the canonical pay/rate in
    # NON-widget master keys, and bump a version suffix in the widget keys
    # whenever sync happens — Streamlit then treats them as fresh widgets and
    # honors the `value=` parameter from the master.
    _pay_master_key = f"_pay_master_{pod_name}_{cluster_hash}"
    _rate_master_key = f"_rate_master_{pod_name}_{cluster_hash}"
    _pay_ver_key = f"_pay_ver_{pod_name}_{cluster_hash}"
    _stops_for_sync = cluster['stops'] if cluster['stops'] > 0 else 1

    if _pay_ver_key not in st.session_state:
        st.session_state[_pay_ver_key] = 0
    _pay_ver = st.session_state[_pay_ver_key]

    # Versioned widget keys — change on every sync so Streamlit re-instantiates
    pay_key = f"pay_val_{pod_name}_{cluster_hash}_v{_pay_ver}"
    rate_key = f"rate_val_{pod_name}_{cluster_hash}_v{_pay_ver}"

    def sync_on_total():
        v = st.session_state.get(pay_key)
        if v is not None:
            st.session_state[_pay_master_key] = min(float(v), PAY_CAP)
            st.session_state[_rate_master_key] = min(round(float(v) / _stops_for_sync, 2), RATE_CAP)
            st.session_state[_pay_ver_key] = _pay_ver + 1

    def sync_on_rate():
        v = st.session_state.get(rate_key)
        if v is not None:
            st.session_state[_rate_master_key] = min(float(v), RATE_CAP)
            st.session_state[_pay_master_key] = min(round(float(v) * _stops_for_sync, 2), PAY_CAP)
            st.session_state[_pay_ver_key] = _pay_ver + 1

    def update_for_new_contractor():
        selected_label = st.session_state.get(sel_key)
        if selected_label and selected_label != st.session_state.get(last_sel_key):
            ic_new = ic_opts[selected_label]
            _, h, _, _ = get_gmaps(_ic_home_loc(ic_new, f"{cluster['center'][0]},{cluster['center'][1]}"), tuple(stop_metrics.keys()))
            new_pay = float(round(h * 25.0, 2)) # 🌟 STRICTLY HOURLY
            # 🌟 BUGFIX: Apply same $20/stop floor used in init logic. Without this, when
            # gmaps fails (network error, bad IC location, etc.) the comp/rate fields zero out
            # and the dispatcher loses the displayed data even though the route is real.
            if new_pay == 0:
                new_pay = round(20.0 * cluster.get('stops', 1), 2)
            st.session_state[_pay_master_key] = min(new_pay, PAY_CAP)
            st.session_state[_rate_master_key] = min(round(new_pay / cluster['stops'], 2), RATE_CAP) if cluster['stops'] > 0 else 20.0
            st.session_state[_pay_ver_key] = _pay_ver + 1
            st.session_state[last_sel_key] = selected_label

    # --- 4. INITIAL SETUP (FIXED SAVING LOGIC) ---
    if _pay_master_key not in st.session_state:
        prev_name = cluster.get('contractor_name', 'Unknown')
        default_label = list(ic_opts.keys())[0] if ic_opts else None
        
        # Match previous contractor if possible
        if prev_name != 'Unknown' and ic_opts:
            for label, row in ic_opts.items():
                if row.get('name') == prev_name:
                    default_label = label; break

        # 🌟 THE FIX: Restore saved database pay first, OR calculate via Google Maps
        saved_comp = float(cluster.get('comp', 0))
        
        if saved_comp > 0:
            # Load the exact amount stored in Google Sheets
            initial_pay = saved_comp
            if default_label:
                st.session_state[sel_key] = default_label
                st.session_state[last_sel_key] = default_label
        elif default_label:
            # Calculate from the Contractor's Home
            ic_init = ic_opts[default_label]
            _, h, _, _ = get_gmaps(_ic_home_loc(ic_init, f"{cluster['center'][0]},{cluster['center'][1]}"), tuple(stop_metrics.keys()))
            initial_pay = float(round(h * 25.0, 2)) # 🌟 STRICTLY HOURLY
            st.session_state[sel_key] = default_label
            st.session_state[last_sel_key] = default_label
        else:
            # 🌟 THE FIX: If no IC is found, calculate the hourly rate from the cluster's center!
            _, h, _, _ = get_gmaps(f"{cluster['center'][0]},{cluster['center'][1]}", tuple(stop_metrics.keys()))
            initial_pay = float(round(h * 25.0, 2)) # 🌟 STRICTLY HOURLY

        # 🌟 Floor: if Maps returned 0 (fail/no IC), seed from $20/stop default
        if initial_pay == 0:
            initial_pay = round(20.0 * cluster.get('stops', 1), 2)
        # Security audit M29: saved_comp comes straight from the Sheet and can predate
        # these caps (or be a legacy/mistyped value) — clamp both derived fields so a
        # stale record can't crash this route's card (see PAY_CAP/RATE_CAP above).
        st.session_state[_pay_master_key] = min(initial_pay, PAY_CAP)
        st.session_state[_rate_master_key] = min(round(initial_pay / cluster['stops'], 2), RATE_CAP) if cluster['stops'] > 0 else 20.0
    
    # --- 4. UI RENDERING & BUTTON LOGIC ---
    route_state = st.session_state.get(f"route_state_{cluster_hash}")
    is_fn = (route_state == "field_nation")

    # Default ic for FN routes — overridden below if not is_fn
    ic = {"name": "Field Nation", "location": f"{cluster['center'][0]},{cluster['center'][1]}", "d": 0}
    mi, hrs, t_str = 0, 0, "N/A"  # defaults for FN routes
    is_unlocked = True

    if not is_fn:
        ic_location_tmp = f"{cluster['center'][0]},{cluster['center'][1]}"

        # ── CONTRACTOR ──────────────────────────────────────────────────
        if ic_opts:
            selected_label = st.selectbox("Contractor", list(ic_opts.keys()), key=sel_key, on_change=update_for_new_contractor, label_visibility="collapsed")
            ic = ic_opts[selected_label]
            # 🌟 Resolve to trusted lat/lng (not the free-text 'location'
            # column, which can drift from the IC's actual lat/lng — produced
            # the "Cassie 4.4mi shows 333mi" bug).
            ic_location_tmp = _ic_home_loc(ic, ic_location_tmp)
        else:
            ic = {"name": "Manual/FN", "location": ic_location_tmp, "d": 0}
            st.info("No ICs within 100mi.")

        # 🌟 FA employees (see FA_EMPLOYEE_NAMES above) are salaried, not paid
        # per route — Nick, Sep 19 2026. Skip the pay-rate inputs and never
        # let a rate/comp figure reach the financials card, the contractor
        # email, or the saveRoute payload for them.
        is_fa = _is_fa_employee(ic)

        ic_location = ic_location_tmp
        mi, hrs, t_str, _wp_order = get_gmaps(ic_location, tuple(stop_metrics.keys()))

        curr_rate = 0.0 if is_fa else st.session_state.get(_rate_master_key, 0.0)
        ic_dist = ic.get('d', 0)
        needs_unlock = (curr_rate >= HIGH_RATE_FLAG_THRESHOLD) or (ic_dist > 60) or (cluster['status'] == 'Flagged')
        # The selected contractor can cost more than the closest IC used by
        # the route builder. Move that card to Flagged on its first render.
        if (curr_rate >= HIGH_RATE_FLAG_THRESHOLD and not is_fa
                and cluster.get("status") == "Ready" and route_state not in ("email_sent", "field_nation")
                and not st.session_state.get(f"_high_rate_rerouted_{cluster_hash}", False)):
            st.session_state[f"_high_rate_rerouted_{cluster_hash}"] = True
            st.rerun(scope="app")
        is_unlocked = True

        if needs_unlock:
            reasons = []
            if curr_rate >= HIGH_RATE_FLAG_THRESHOLD: reasons.append(f"High Rate (${curr_rate})")
            if ic['d'] > 60: reasons.append(f"Distance ({round(ic['d'],1)}mi)")
            if cluster['status'] == 'Flagged': reasons.append("Flagged Route")
            st.markdown(f"""<div style="background:#fef2f2; border:1px solid #ef4444; padding:8px 10px; border-radius:8px; margin:6px 0;"><span style="color:#b91c1c; font-weight:800; font-size:11px;">🔒 ACTION REQUIRED:</span> <span style="color:#7f1d1d; font-size:11px;">{" & ".join(reasons)}</span></div>""", unsafe_allow_html=True)
            is_unlocked = st.checkbox("Authorize Premium Rate / Distance", key=f"lock_{pod_name}_{cluster_hash}")

        # ── INPUTS ──────────────────────────────────────────────────────
        if is_fa:
            st.markdown(
                "<div style='background:#f0fdf4; border:1px solid #86efac; padding:6px 10px; "
                "border-radius:8px; margin:6px 0; font-size:11px; color:#166534;'>"
                "👤 <b>Field Agent (employee)</b> — no per-route pay rate needed."
                "</div>",
                unsafe_allow_html=True,
            )
            _inp_c = st.container()
            with _inp_c:
                st.date_input("Deadline", datetime.now().date()+timedelta(DEFAULT_DUE_DAYS), key=f"dd_{pod_name}_{cluster_hash}", disabled=not is_unlocked)
        else:
            _inp_a, _inp_b, _inp_c = st.columns([1.5, 1.5, 1.5])
            with _inp_a:
                # Security audit M29 - cap comp so a fat-fingered value cannot
                # flow verbatim into the saveRoute payload and contractor email.
                # min(...) here is a last-resort guard: every write site above already
                # clamps to PAY_CAP, but this keeps the widget itself un-crashable even
                # if a value slips in from an older session or an unclamped write site.
                st.number_input("Total Comp ($)", min_value=0.0, max_value=PAY_CAP, step=5.0, format="%.2f", value=min(float(st.session_state.get(_pay_master_key, 0.0)), PAY_CAP), key=pay_key, on_change=sync_on_total, disabled=not is_unlocked)
            with _inp_b:
                # Security audit M29 - cap rate/stop for the same reason (see PAY_CAP/RATE_CAP note above).
                st.number_input("Rate/Stop ($)", min_value=0.0, max_value=RATE_CAP, step=1.0, format="%.2f", value=min(float(st.session_state.get(_rate_master_key, 0.0)), RATE_CAP), key=rate_key, on_change=sync_on_rate, disabled=not is_unlocked)
            with _inp_c:
                st.date_input("Deadline", datetime.now().date()+timedelta(DEFAULT_DUE_DAYS), key=f"dd_{pod_name}_{cluster_hash}", disabled=not is_unlocked)

        # ── FINANCIALS CARD ──────────────────────────────────────────────
        # FA employees never carry a comp/rate figure, regardless of whatever
        # stale value sits in the master keys from a previous non-FA selection.
        final_pay = 0.0 if is_fa else st.session_state.get(_pay_master_key, 0.0)
        final_rate = 0.0 if is_fa else st.session_state.get(_rate_master_key, 0.0)

        if final_rate >= RATE_CRITICAL: status_color = "#ef4444"
        elif final_rate >= RATE_WARNING: status_color = "#f97316"
        else: status_color = TB_GREEN

        # 🌟 BUGFIX: when get_gmaps returns 0 / "0h 0m" (network/API failure or empty IC
        # location), don't render literal zeros. Show "—" so dispatchers know the value
        # didn't come from a real measurement, and rely on the floor-seeded comp/rate
        # values for the dispatch decision instead.
        _t_display = t_str if t_str and t_str not in ("0h 0m", "N/A") else "—"
        _mi_display = f"{mi} mi" if mi and mi > 0 else "—"

        # FA employees: drop the comp/rate figures entirely rather than show a
        # misleading "$0.00 total | $0.0/stop" for someone who isn't paid per route.
        _comp_segment = (
            "<b style='color:#166534; font-size:13px;'>Employee</b> — no per-route pay"
            if is_fa else
            f"<b style='color:{status_color}; font-size:13px;'>${final_pay:,.2f}</b> total"
            f"&nbsp;&nbsp;<span style='color:#cbd5e1;'>|</span>&nbsp;&nbsp;${final_rate}/stop"
        )
        st.markdown(
            f"<div style='font-size:11px; color:#64748b; margin:4px 0 8px 0;'>"
            f"{_comp_segment}"
            f"&nbsp;&nbsp;<span style='color:#cbd5e1;'>|</span>&nbsp;&nbsp;{_t_display} drive"
            f"&nbsp;&nbsp;<span style='color:#cbd5e1;'>|</span>&nbsp;&nbsp;{_mi_display} round trip"
            f"</div>",
            unsafe_allow_html=True,
        )

        # ── ROUTE STOPS ─────────────────────────────────────────────────────
        hist = st.session_state.get(f"history_{cluster_hash}", [])
        if hist:
            st.markdown(f"<p style='color:#94a3b8; font-size:11px; margin-bottom:2px; font-weight:600;'>↩️ Previously sent to: {', '.join(hist)}</p>", unsafe_allow_html=True)

        # Build expandable stop rows with task pills in summary + campaign in expansion
        _dispatch_rows = []
        for addr, metrics in stop_metrics.items():
            # Icons only for address row summary (kiosk install gets its own green pill below
            # to match the right column's make_venue_details styling — was previously a plain
            # "🛠️ 1" inline icon with no color treatment).
            icon_parts = []
            if metrics['n_ad'] > 0: icon_parts.append("🆕")
            if metrics['c_ad'] > 0: icon_parts.append("🔄")
            if metrics['d_ad'] > 0: icon_parts.append("⚪")
            if metrics['remov'] > 0: icon_parts.append(f"🗑️ {metrics['remov']}")
            if metrics['custom']: icon_parts.append("📋")
            if metrics['digi_off'] > 0: icon_parts.append("📵")
            if metrics['digi_srv'] > 0: icon_parts.append("⚙️")
            pill_str = " ".join(icon_parts)
            # Green Kiosk pill — matches right column's make_venue_details treatment.
            k_tag_html = f" <span style='color:#16a34a;font-weight:800;font-size:10px;'>🛠️ {metrics['inst']} Kiosk</span>" if metrics['inst'] > 0 else ""
            # Digital Ins/Rem pill in matching teal — same visual rhythm as the kiosk pill.
            digi_ins_html = f" <span style='color:#0f766e;font-weight:800;font-size:10px;'>🔧 {metrics['digi_ins']} Ins/Rem</span>" if metrics['digi_ins'] > 0 else ""
            # Boosted / Local Plus pills — count of tasks at THIS stop with that tier.
            boost_html = f" <span style='color:#dc2626;font-weight:800;font-size:10px;'>🔥 {metrics['boost_cnt']}</span>" if metrics.get('boost_cnt', 0) > 0 else ""
            lplus_html = f" <span style='color:#ca8a04;font-weight:800;font-size:10px;'>⭐ {metrics['lplus_cnt']}</span>" if metrics.get('lplus_cnt', 0) > 0 else ""
            # Full icon+name for expansion
            expand_parts = []
            if metrics['n_ad'] > 0: expand_parts.append(f"🆕 {metrics['n_ad']} New Ad")
            if metrics['c_ad'] > 0: expand_parts.append(f"🔄 {metrics['c_ad']} Continuity")
            if metrics['d_ad'] > 0: expand_parts.append(f"⚪ {metrics['d_ad']} Default")
            if metrics['inst'] > 0: expand_parts.append(f"🛠️ {metrics['inst']} Install")
            if metrics['remov'] > 0: expand_parts.append(f"🗑️ {metrics['remov']} Removal")
            for cn, cnt in metrics['custom'].items(): expand_parts.append(f"📋 {cnt} {cn}")
            if metrics['digi_off'] > 0: expand_parts.append(f"📵 {metrics['digi_off']} Offline")
            if metrics['digi_ins'] > 0: expand_parts.append(f"🔧 {metrics['digi_ins']} Ins/Rem")
            if metrics['digi_srv'] > 0: expand_parts.append(f"⚙️ {metrics['digi_srv']} Service")
            expand_str = " | ".join(expand_parts)
            esc_count_stop = sum(1 for t in cluster['data'] if t.get('full') == addr and t.get('escalated'))
            esc_inline = f" <span style='color:#dc2626;font-weight:900;font-size:10px;'>❗ {esc_count_stop}</span>" if esc_count_stop > 0 else ""
            display_addr = f"+ {addr}" if metrics.get('is_new') else addr
            venue_prefix = f"<span style='color:#94a3b8;font-size:11px;font-weight:600;white-space:normal;'>{esc(metrics['venue_name'])} — </span>" if metrics.get('venue_name') else ""
            task_pill = f"<span style='color:#633094;background:#f3e8ff;padding:1px 5px;border-radius:8px;font-weight:800;font-size:10px;'>{metrics['t_count']} Tasks</span>"
            pill_html = f"<span style='font-size:11px;color:#94a3b8;'> — {pill_str}</span>" if pill_str else ""
            # Campaign expansion: aggregate by (campaign, task_type) with × N count
            # plus per-group escalation/boost/local-plus counters. Same shape as
            # make_venue_details so Sent/Accepted/Declined/Finalized/FN tabs match
            # what dispatchers see here in Ready/Flagged.
            from collections import defaultdict as _dd
            loc_tasks = [t for t in cluster['data'] if t.get('full') == addr]
            _camp_groups = _dd(lambda: {'count': 0, 'esc': 0, 'boost': 0, 'lplus': 0, 'tt_badge': '', 'cmp': ''})
            for t in loc_tasks:
                # 🌟 May 2026 — Blank client_company / campaign falls back to the
                # task type text so the accordion row still shows meaningful info
                # (previously the row was silently skipped, leaving the venue
                # accordion empty for tasks with no client_company data).
                cmp = (t.get('client_company','') or '').strip()
                tt = str(t.get('task_type','')).lower()
                if not cmp:
                    cmp = str(t.get('task_type','') or '').strip() or 'Unknown'
                if t.get('is_digital'):
                    if 'offline' in tt: tt_badge = "📵 Offline"
                    elif 'ins/re' in tt: tt_badge = "🔧 Ins/Rem"
                    elif 'site survey' in tt: tt_badge = "🔍 Site Survey"
                    else: tt_badge = "⚙️ Service"
                elif 'install' in tt: tt_badge = "🛠️ Install"
                elif any(x in tt for x in ['kiosk removal','remove kiosk']): tt_badge = "🗑️ Removal"
                elif any(x in tt for x in ['continuity','photo retake','swap']): tt_badge = "🔄 Continuity"
                elif any(x in tt for x in ['default','pull down']): tt_badge = "⚪ Default"
                elif any(x in tt for x in ['new ad','art change','top']) or not tt: tt_badge = "🆕 New Ad"
                else: tt_badge = f"📋 {tt.title()}"
                key = (cmp, tt_badge)
                grp = _camp_groups[key]
                grp['cmp'] = cmp
                grp['tt_badge'] = tt_badge
                grp['count'] += 1
                if t.get('escalated'): grp['esc'] += 1
                bs = str(t.get('boosted_standard','')).lower()
                if 'local plus' in bs: grp['lplus'] += 1
                elif 'boosted' in bs: grp['boost'] += 1
            camp_rows = []
            for (cmp, tt_badge), grp in _camp_groups.items():
                cnt = grp['count']
                count_suffix = f" <span style='color:#94a3b8;font-weight:600;'>× {cnt}</span>" if cnt > 1 else ""
                esc_pill   = f" <span style='color:#dc2626;font-weight:800;'>❗ {grp['esc']}</span>" if grp['esc'] > 0 else ""
                boost_pill = f" <span style='color:#dc2626;font-weight:800;'>🔥 {grp['boost']}</span>" if grp['boost'] > 0 else ""
                lplus_pill = f" <span style='color:#ca8a04;font-weight:800;'>⭐ {grp['lplus']}</span>" if grp['lplus'] > 0 else ""
                row = (
                    f"<div style='font-size:11px;color:#475569;padding:2px 4px;margin-top:3px;'>"
                    f"• <span style='color:#0f172a;font-weight:600;'>{cmp}</span>"
                    f"&nbsp;<span style='font-weight:700;color:#0f172a;'>{tt_badge}</span>"
                    f"{count_suffix}{esc_pill}{boost_pill}{lplus_pill}"
                    f"</div>"
                )
                camp_rows.append(row)
            camp_block = f"<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;'>{''.join(camp_rows)}</div>" if camp_rows else ""
            _icon_html = f"<span style='font-size:13px;margin-left:6px;'>{pill_str}</span>" if pill_str else ""
            _dispatch_rows.append(
                f"<details class='fn-loc-row'>"
                f"<summary class='fn-loc-summary'>"
                f"<span class='fn-chevron'>›</span>"
                f"{venue_prefix}<span style='font-weight:700;color:#0f172a;'>{display_addr}</span>{k_tag_html}{digi_ins_html}{boost_html}{lplus_html}{esc_inline} &nbsp;{task_pill}{_icon_html}"
                f"</summary>{camp_block}</details>"
            )

        st.markdown(f"{VENUE_SECTION_CSS}<div style='font-size:9px;font-weight:900;color:#94a3b8;text-transform:uppercase;letter-spacing:0.1em;margin:6px 0 3px 0;'>Route Stops &nbsp;&middot;&nbsp; {len(_dispatch_rows)}</div><div style='padding:0 2px;'>{''.join(_dispatch_rows)}</div>", unsafe_allow_html=True)

        # 🔗 BUNDLE ROUTES — Apr 27 2026 (preview-driven).
        # Find OTHER unsent clusters in this pod within 50mi and offer them as a
        # multiselect. Selecting a route triggers a preview at the top of this fragment
        # which re-renders the entire card AS IF those routes were merged. The merge is
        # only committed to session_state when the dispatcher clicks "Confirm Bundle".
        # Deselecting any candidate reverts the card to its original state. Generate Link
        # is disabled while in preview so dispatch stays consistent with what was committed.
        _bundle_blocked_states = ('email_sent', 'field_nation', 'finalized')
        _current_route_state = st.session_state.get(f"route_state_{cluster_hash}")
        if (not is_sent and not is_declined
                and _current_route_state not in _bundle_blocked_states):
            # 🔗 Bundle radius cap REMOVED (May 2026 — "ability to bundle any
            # route"): previously hard-capped to 50mi which hid every farther
            # candidate. Distance is already in each option's label (sorted
            # closest-first below), so the dispatcher decides whether the bundle
            # makes sense rather than the code deciding for them.
            _pod_clusters_for_bundle = st.session_state.get(f"clusters_{pod_name}", [])
            _nearby_bundles = []
            for _other in _pod_clusters_for_bundle:
                _o_tids = sorted([str(_t['id']).strip() for _t in _other.get('data', [])])
                if not _o_tids:
                    continue
                _o_hash = hashlib.md5("".join(_o_tids).encode()).hexdigest()
                if _o_hash == cluster_hash:
                    continue  # skip self
                # Don\'t suggest a cluster that\'s already been "absorbed" by the preview,
                # since the preview application above re-uses the same source-cluster IDs.
                _o_state = st.session_state.get(f"route_state_{_o_hash}")
                if _o_state in _bundle_blocked_states:
                    continue
                _o_sheet = st.session_state.get('sent_db', {}).get(
                    next((_tid for _tid in _o_tids if _tid in st.session_state.get('sent_db', {})), None)
                )
                if _o_sheet and not st.session_state.get(f"reverted_{_o_hash}", False):
                    _o_status = str(_o_sheet.get('status', '')).lower()
                    if _o_status in ('sent', 'accepted', 'declined', 'finalized', 'field_nation'):
                        continue
                if bool(_other.get('is_digital', False)) != bool(cluster.get('is_digital', False)):
                    continue
                if bool(_other.get('is_removal', False)) != bool(cluster.get('is_removal', False)):
                    continue
                _o_dist = haversine(cluster['center'][0], cluster['center'][1],
                                     _other['center'][0], _other['center'][1])
                _nearby_bundles.append((_o_dist, _o_hash, _other))
            _nearby_bundles.sort(key=lambda x: x[0])

            # If the preview has any selections that no longer appear as candidates
            # (e.g., the source cluster was just sent in another tab), prune them so
            # they don\'t silently linger in the multiselect\'s saved state.
            if _preview_hashes:
                _valid_now = {h for _, h, _ in _nearby_bundles}
                _stale = [h for h in _preview_hashes if h not in _valid_now]
                if _stale:
                    st.session_state[_bundle_select_key] = [h for h in _preview_hashes if h not in _stale]

            if _nearby_bundles or _in_preview:
                _opt_hashes = [h for _, h, _ in _nearby_bundles]
                _opt_label = {}
                for _d, _h, _o in _nearby_bundles:
                    _o_loc = f"{_o.get('city','')}, {_o.get('state','')}"
                    _o_stops = _o.get('stops', 0)
                    _o_tasks = len(_o.get('data', []))
                    _o_inst = _o.get('inst_count', 0)
                    _o_inst_pill = f" · 🛠️ {_o_inst}" if _o_inst > 0 else ""
                    _opt_label[_h] = f"{_o_loc} — {round(_d, 1)}mi · {_o_stops} stops · {_o_tasks} tasks{_o_inst_pill}"

                _banner_color = "#1e40af" if not _in_preview else "#b45309"
                _banner_bg    = "#eff6ff" if not _in_preview else "#fffbeb"
                _banner_border= "#3b82f6" if not _in_preview else "#f59e0b"
                _banner_text  = (f"🔗 Bundle Routes ({len(_nearby_bundles)} available, sorted by distance) — select to preview"
                                 if not _in_preview else
                                 f"🔍 PREVIEW — {len(_preview_hashes)} route(s) merged · {_preview_added_count} tasks added · click Confirm to commit, deselect to revert")
                st.markdown(
                    f"<div style='background:{_banner_bg};border-left:3px solid {_banner_border};border-radius:6px;"
                    f"padding:8px 12px;margin:8px 0 4px 0;'>"
                    f"<div style='font-size:9px;font-weight:900;color:{_banner_color};text-transform:uppercase;"
                    f"letter-spacing:0.08em;'>{_banner_text}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # Multiselect — its saved value IS the preview state.
                st.multiselect(
                    "Bundle nearby routes",
                    options=_opt_hashes,
                    format_func=lambda h: _opt_label.get(h, h),
                    key=_bundle_select_key,
                    label_visibility="collapsed",
                    placeholder="Select nearby routes to preview a merged version...",
                )

                # 🔁 UNBUNDLE — shown when this cluster is currently a bundle
                # (bundle_count > 0). Splits the merged cluster back into its original
                # pieces by dropping the bundle_map entries that share these task_ids.
                _bundle_n = int(cluster.get('bundle_count', 0) or 0)
                if _bundle_n > 0 and not _in_preview:
                    _unbundle_key = f"bundle_unbundle_{cluster_hash}"
                    if st.button(
                        f"🔁 Unbundle ({_bundle_n + 1} routes → split apart)",
                        key=_unbundle_key,
                        use_container_width=True,
                        help="Revert this bundle so each original route becomes its own card again.",
                    ):
                        _ub_ok = _unbundle_cluster(pod_name, task_ids)
                        if _ub_ok:
                            st.toast(f"🔁 Unbundled — {_bundle_n + 1} routes split back to originals")
                        else:
                            st.toast("Nothing to unbundle for this cluster.")
                        st.rerun()

                # Confirm button only shows while preview is active. No "Clear" button —
                # the multiselect chips have built-in × icons that achieve the same thing.
                if _in_preview:
                    if st.button(f"✅ Confirm Bundle ({len(_preview_hashes)} route{'s' if len(_preview_hashes)!=1 else ''}, +{_preview_added_count} tasks)",
                                 key=f"bundle_confirm_{cluster_hash}", use_container_width=True):
                        # Pull target + source task IDs from the live cluster store, commit
                        # the bundle to session_state[\'_bundle_map\'] (so it survives any
                        # future cluster rebuild — process_pod / smart_sync_pod / WS reset
                        # → re-init), then call _replay_bundles which does the actual merge.
                        # All merge logic lives in _replay_bundles — Confirm just records intent.
                        _store_key = _bundle_clusters_store(pod_name)
                        _live_clusters = st.session_state.get(_store_key, [])
                        _target_tids = set()
                        _source_tids = set()
                        _src_count = 0
                        for _cc in _live_clusters:
                            _cc_tids = sorted([str(_t['id']).strip() for _t in _cc.get('data', [])])
                            _cc_hash = hashlib.md5("".join(_cc_tids).encode()).hexdigest()
                            if _cc_hash == cluster_hash:
                                _target_tids = set(_cc_tids)
                            if _cc_hash in _preview_hashes:
                                _source_tids |= set(_cc_tids)
                                _src_count += 1
                        if _target_tids and _source_tids:
                            _commit_bundle(pod_name, _target_tids, _source_tids)
                            _replay_bundles(pod_name)
                            # cluster_hash changes on the next render (cluster.data changed),
                            # so pay_key/rate_key/sel_key/_bundle_select_key all become NEW
                            # keys with no saved state — Streamlit re-initializes the widgets
                            # fresh. No manual pop needed (post-render widget writes would
                            # crash anyway).
                            st.toast(f"🔗 Bundled {_src_count} route(s) — {_preview_added_count} tasks added")
                            st.rerun(scope="app")

        if not is_sent and not is_declined and len(stop_metrics) > 1:
            _all_addrs = list(stop_metrics.keys())
            _ms_key = f"multi_split_{pod_name}_{cluster_hash}_{i}_{hashlib.md5(str(list(stop_metrics.keys())).encode()).hexdigest()[:4]}"
            _selected = st.multiselect(
                "Remove stops",
                options=_all_addrs,
                format_func=lambda x: f"{stop_metrics[x].get('venue_name','') + ' — ' if stop_metrics[x].get('venue_name') else ''}{x}",
                key=_ms_key,
                label_visibility="collapsed",
                placeholder="Select stops to remove from route..."
            )
            if _selected:
                if st.button(f"✂️ Remove {len(_selected)} Stop{'s' if len(_selected) > 1 else ''}", key=f"btn_{_ms_key}"):
                    for _addr in _selected:
                        tasks_to_move = [t for t in cluster['data'] if t['full'] == _addr]
                        if not tasks_to_move: continue
                        new_fragment = {
                            "data": tasks_to_move, "center": [tasks_to_move[0]['lat'], tasks_to_move[0]['lon']],
                            "stops": 1, "city": tasks_to_move[0]['city'], "state": tasks_to_move[0]['state'],
                            "status": "Ready", "has_ic": cluster.get('has_ic', False),
                            "esc_count": sum(1 for x in tasks_to_move if x.get('escalated')),
                            "is_digital": any(x.get('is_digital') for x in tasks_to_move),
                            "inst_count": sum(1 for x in tasks_to_move if "install" in str(x.get('task_type','')).lower()),
                            "remov_count": sum(1 for x in tasks_to_move if "remove" in str(x.get('task_type','')).lower()),
                            "wo": "none"
                        }
                        cluster['data'] = [t for t in cluster['data'] if t['full'] != _addr]
                        target_pod = pod_name if pod_name != "Global_Digital" else next((p for p, cfg in POD_CONFIGS.items() if new_fragment['state'] in cfg['states']), "UNKNOWN")
                        if target_pod != "UNKNOWN" and f"clusters_{target_pod}" in st.session_state:
                            st.session_state[f"clusters_{target_pod}"].append(new_fragment)
                    cluster['stops'] = len(set(t['full'] for t in cluster['data']))
                    st.session_state.pop(pay_key, None)
                    st.session_state.pop(rate_key, None)
                    st.toast(f"✂️ {len(_selected)} stop(s) broken off into standalone routes!")
                    st.rerun()





        stops_text = ""
        
        if len(stop_metrics) > 2:
            stops_text += f"   ... and {len(stop_metrics) - 2} more stops.\n"

        loc_pills = {}
        for t in cluster['data']:
            addr = t.get('full', 'Unknown')
            if addr not in loc_pills: loc_pills[addr] = ""
            if t.get('escalated'): pass  # escalation shown in header only
        
            # 🌟 THE FIX: Split Digital Email Output
            if t.get('is_digital'):
                tt_lower = str(t.get('task_type','')).lower()
                if "offline" in tt_lower and "📵" not in loc_pills[addr]: loc_pills[addr] += "🔌"
                elif "ins/re" in tt_lower and "🔧" not in loc_pills[addr]: loc_pills[addr] += "🔧"
                elif ("offline" not in tt_lower and "ins/re" not in tt_lower) and "⚙️" not in loc_pills[addr]: 
                    loc_pills[addr] += "⚙️"
            else:
                if "install" in str(t.get('task_type','')).lower() and "🛠️" not in loc_pills[addr]: loc_pills[addr] += "🛠️"
                if str(t.get('task_type','')).lower() in ["kiosk removal", "remove kiosk"] and "🗑️" not in loc_pills[addr]: 
                    loc_pills[addr] += "🗑️"

        due = st.session_state.get(f"dd_{pod_name}_{cluster_hash}", datetime.now().date()+timedelta(DEFAULT_DUE_DAYS))
        is_already_sent = is_sent or is_declined or st.session_state.get(f"route_state_{cluster_hash}") == "email_sent"
    
        prev_ic_name = cluster.get('contractor_name', 'Unknown')
        ic_name = ic.get('name', 'Unknown Contractor') 
    
        # 🌟 STICKY WO RESERVATION (Jul 2026): once a WO is computed for this
        # (cluster, IC) pair in the current session, reuse it. Prevents same-IC
        # collisions between two not-yet-dispatched routes (sent_db can't see
        # the first one yet, so both would compute "-1" without this).
        _wo_sticky_key = f"_wo_sticky_{cluster_hash}_{ic_name}"
        _wo_sticky = st.session_state.get(_wo_sticky_key)

        if ic_name == prev_ic_name and cluster.get('wo', 'none') != 'none':
            wo_val = cluster['wo']
        elif _wo_sticky:
            wo_val = _wo_sticky
        else:
            _base_wo = f"{ic.get('name', 'Unknown')}-{datetime.now().strftime('%m%d%Y')}"
            # Compute next suffix from the max of (active sent WOs ∪ archived WOs) for this IC today.
            # Archived suffixes are "consumed" — we never recycle them, so a route created after an
            # archive will read -2 (or -N+1) instead of clobbering the archived -1.
            _local_sent_db = st.session_state.get('sent_db', {})
            _archived_wos_set = st.session_state.get('archived_wos', set()) or set()
            _candidate_wos = set()
            for _info in _local_sent_db.values():
                _w = str(_info.get('wo', '') or '')
                if _w.startswith(_base_wo):
                    _candidate_wos.add(_w)
            for _w in _archived_wos_set:
                if str(_w).startswith(_base_wo):
                    _candidate_wos.add(str(_w))
            # 🌟 IN-SESSION WO SCAN: also pick up WOs "reserved" for other
            # clusters this session (not yet in sent_db).
            # Jul 2026 — iterate a snapshot of keys instead of .items() because
            # Streamlit's session_state raises KeyError when unpacking widget
            # entries whose widget was destroyed mid-iteration (e.g., a bundle
            # multiselect from a prior render whose cluster_hash changed). Filter
            # first, then explicitly fetch each value with try/except.
            for _k in list(st.session_state.keys()):
                if not (isinstance(_k, str) and _k.startswith('_wo_sticky_') and _k != _wo_sticky_key):
                    continue
                try:
                    _v = st.session_state[_k]
                except KeyError:
                    continue
                _vs = str(_v or '')
                if _vs.startswith(_base_wo):
                    _candidate_wos.add(_vs)
            _max_suffix = 0
            for _w in _candidate_wos:
                try:
                    _suffix = int(str(_w).rsplit('-', 1)[-1])
                    if _suffix > _max_suffix:
                        _max_suffix = _suffix
                except Exception as _wo_e:
                    # Security audit M4 - a malformed suffix feeding this
                    # correctness-critical max() is logged, not swallowed:
                    # an undetected bad suffix can recycle a WO number.
                    _log_err("wo_suffix_parse", f"bad WO '{_w}': {_wo_e}")
            _wo_num = _max_suffix + 1
            wo_val = f"{_base_wo}-{_wo_num}"
            # Reserve this WO for the current (cluster, IC) so subsequent
            # same-IC cluster renders pick -N+1 instead of colliding.
            st.session_state[_wo_sticky_key] = wo_val
            # Security audit M5 - if the Archive sheet could not be read,
            # consumed WO suffixes are invisible and this number may collide.
            if st.session_state.get('_archive_read_failed'):
                st.warning(
                    "\u26a0\ufe0f Archive sheet could not be read - the new WO "
                    f"number ({wo_val}) may reuse an archived suffix. Verify it "
                    "before sending, or retry after a refresh."
                )
        # 🌟 NEW: Calculate route-level task breakdowns for the email preview
        route_task_counts = {}
        total_installs = 0
    
        for t in cluster['data']:
            raw_tt = str(t.get('task_type', '')).strip()
            clean_tt_lower = raw_tt.lower().replace("escalation", "").replace("  ", " ").strip(" ,-|:")
        
            # Default to New Ad if empty
            if not clean_tt_lower:
                clean_tt_lower = "new ad"
                display_tt = "New Ad"
            else:
                display_tt = clean_tt_lower.title()

            is_digi = t.get('is_digital')
            category = None
        
            # 🚦 Match exactly to the UI buckets
            if is_digi:
                if "offline" in clean_tt_lower: category = "📵 Offline"
                elif "ins/re" in clean_tt_lower: category = "🔧 Ins/Rem"
                else: category = "⚙️ Service"
            else:
                if "install" in clean_tt_lower: 
                    category = "🛠️ Kiosk Install"
                    total_installs += 1
                elif any(x in clean_tt_lower for x in ["kiosk removal", "remove kiosk"]): category = "🗑️ Kiosk Removal"
                elif any(x in clean_tt_lower for x in ["continuity", "photo retake", "swap"]): category = "🔄 Continuity"
                elif any(x in clean_tt_lower for x in ["default", "pull down"]): category = "⚪ Default"
                elif any(x in clean_tt_lower for x in ["new ad", "art change", "top"]): category = "🆕 New Ad"
                else: category = f"📋 {display_tt}" # Pass custom types straight through
            
            if category not in route_task_counts:
                route_task_counts[category] = 0
            route_task_counts[category] += 1

        # Format the breakdown list cleanly for the email
        task_breakdown_str = "\n".join([f"  {cat}: {count}" for cat, count in route_task_counts.items()]) + "\n"
    
        install_warning = f"\n⚠️ NOTE: This route contains Kiosk Installs. Please ensure you have adequate storage and vehicle space.\n" if total_installs > 0 else ""

        # 🩺 Quest Diagnostics hours warning (May 18 2026 — Nick's request).
        # Quest venues are only open 7am-4pm. Surface in the email body if any
        # stop on the route is a Quest so the contractor plans their day around
        # the window. We also propagate this into the portal HTML below.
        _quest_addrs = set()
        for _t in cluster['data']:
            _vn = str(_t.get('venue_name', '')).strip().lower()
            if 'quest' in _vn:
                _addr = _t.get('full', '')
                if _addr:
                    _quest_addrs.add(_addr)
        quest_warning = (
            f"\n⏰ QUEST DIAGNOSTICS HOURS: 7:00 AM – 4:00 PM only.\n"
            f"   {len(_quest_addrs)} stop(s) on this route are Quest Diagnostics — please arrive within the open window.\n"
        ) if _quest_addrs else ""

        # FA employees are salaried — don't quote them a per-route dollar
        # figure that doesn't apply to them (see is_fa above).
        _comp_line = "\n" if is_fa else f" Estimated Compensation: ${final_pay:.2f} \n\n"
        sig_preview = (
            f"Hello {ic.get('name', 'Contractor')},\n\n"
            f"We have a new route available for you to review.\n\n"
            f" Work Order: {wo_val}\n"
            f"📅 Due Date: {due.strftime('%A, %b %d, %Y')}\n"
            f" Total Stops: {cluster['stops']}\n"
            f"{_comp_line}"
            f" Task Breakdown:\n"
            f"{task_breakdown_str}"
            f"{install_warning}"
            f"{quest_warning}\n"
            f"To view the complete route details—including total stops, estimated mileage, and time—please click the secure link below to access your Route Summary.\n\n"
            f"⚠️ ACTION REQUIRED:\n"
            f"You must confirm by selecting 'Accept' or 'Decline' directly through the portal link.\n\n"
            f"Terraboost Route Request Link:\n"
            f"{PORTAL_BASE_URL}?route={link_id}&v2=true"
        )
    
        # 🌟 UNIQUE KEY
        last_data_key = f"last_data_{pod_name}_{cluster_hash}"
        version_key = f"tx_ver_{pod_name}_{cluster_hash}"
        current_data_fingerprint = (
            f"{ic.get('name', 'Unknown')}_"
            f"{ic.get('email', '')}_"
            f"{final_pay}_{final_rate}_"
            f"{due}_{wo_val}_"
            f"{cluster['stops']}_{len(cluster['data'])}"
        )
    
        if version_key not in st.session_state:
            st.session_state[version_key] = 1

        if st.session_state.get(last_data_key) != current_data_fingerprint:
            st.session_state[version_key] += 1
            st.session_state[last_data_key] = current_data_fingerprint
            st.session_state[f"tx_{pod_name}_{cluster_hash}_{st.session_state[version_key]}"] = sig_preview
    
        active_tx_key = f"tx_{pod_name}_{cluster_hash}_{st.session_state[version_key]}"

        if active_tx_key not in st.session_state:
            st.session_state[active_tx_key] = sig_preview
        elif real_id and "LINK_PENDING" in st.session_state[active_tx_key]:
            st.session_state[active_tx_key] = st.session_state[active_tx_key].replace("LINK_PENDING", real_id)
    
       # 🌟 UNIQUE KEY & PERFECT INDENTATION
        # Email Preview shows only for admin/manager. Pod accounts (Dispatchers
        # + Associates) see the email when Outlook opens on Generate Link, so the
        # in-app preview just eats vertical space for them. (May 23 2026.)
        if _is_admin_or_manager():
            email_body_content = st.text_area("Email Content Preview", value=sig_preview, height=120, key=f"txt_area_{pod_name}_{current_data_fingerprint}_{cluster_hash}", disabled=not is_unlocked)
        else:
            email_body_content = sig_preview

        # --- HIGH-SPEED DISPATCH BUTTON ---
        btn_label = "RESEND LINK & OPEN OUTLOOK" if is_already_sent else "GENERATE LINK & OPEN OUTLOOK"
        if is_fn:
            st.caption("📋 Email dispatch disabled — route is assigned to Field Nation.")

        # Generate Link button + FN checkbox paired into one row (May 23 2026).
        _disp_c1, _disp_c2 = st.columns([1.8, 1], vertical_alignment="center")
        with _disp_c1:
            _gen_clicked = st.button(btn_label, type="primary", key=f"gbtn_{pod_name}_{cluster_hash}", disabled=not is_unlocked or is_fn or _in_preview, use_container_width=True, help=("Confirm or clear the bundle preview before dispatching." if _in_preview else None))
        with _disp_c2:
            if route_state != "email_sent":
                fn_checked = st.checkbox("Assign to FN", value=is_fn, key=f"fn_check_{pod_name}_{cluster_hash}")
            else:
                fn_checked = is_fn
        if _gen_clicked:
            # 🛡️ STEP 0: POST-TIMEOUT RETRY SAFETY (Sep 2026 — Nick: "clicking
            # Generate Link again to retry makes it worse"). A saveRoute POST
            # that times out CLIENT-SIDE (25s) can still be running — or have
            # already finished — SERVER-SIDE in Apps Script; the row may well
            # have saved. The plain collision check below only looks at
            # session_state['sent_db'], a snapshot from before that timed-out
            # attempt, so it never saw the row the first click may have just
            # written, and a second click fired a second saveRoute — two rows
            # / two WO suffixes / two emails for the same route.
            # Fix: only when the LAST attempt for this exact cluster timed
            # out, force a fresh sheet read before doing anything else, so
            # the collision check below is checking reality, not a stale
            # snapshot. Gated on that flag (not every click) so a normal
            # first-time dispatch pays no extra round-trip.
            _timed_out_key = f"_dispatch_timed_out_{cluster_hash}"
            if st.session_state.pop(_timed_out_key, False):
                try:
                    fetch_sent_records_from_sheet.clear()
                    fetch_sent_records_from_sheet()
                except Exception as _refresh_e:
                    _log_err("gen_link/post_timeout_refresh", _refresh_e)

            # 🛡️ STEP 1: FAST COLLISION CHECK — only block active sent routes (not revoked/declined)
            local_sent_db = st.session_state.get('sent_db', {})
            _active_statuses = ('sent',)
            collision = next(
                (tid for tid in task_ids
                 if tid in local_sent_db
                 and local_sent_db[tid].get('status', '').lower() in _active_statuses
                 and not st.session_state.get(f"reverted_{cluster_hash}", False)),
                None
            )

            # A collision against OUR OWN prior attempt (same dispatcher, same
            # route) means the "timed out" saveRoute actually landed. Adopt
            # it as a success instead of firing a duplicate saveRoute.
            if collision and not is_already_sent and local_sent_db[collision].get('name') == ic.get('name', 'Unknown'):
                st.session_state[f"sent_ts_{cluster_hash}"] = local_sent_db[collision].get('time') or datetime.now().strftime('%m/%d %I:%M %p')
                st.session_state[f"contractor_{cluster_hash}"] = ic.get('name', 'Unknown')
                st.session_state[f"wo_{cluster_hash}"] = wo_val
                st.session_state[f"comp_{cluster_hash}"] = final_pay
                st.session_state[f"due_{cluster_hash}"] = str(due)
                st.session_state[f"route_state_{cluster_hash}"] = "email_sent"
                st.session_state[f"reverted_{cluster_hash}"] = False
                st.success("✅ Good news — the earlier attempt actually saved before it timed out. Marked as sent; use **Resend Link & Open Outlook** if you still need to email it.")
                st.rerun()
                return

            if collision and not is_already_sent:
                st.error(f"🚫 COLLISION: Dispatched by someone else ({local_sent_db[collision]['name']}).")
                st.rerun()
                return

            # Security audit H23 - in-flight guard. A double-click within ~1s
            # fires two saveRoute POSTs before route_state is set, producing
            # duplicate sheet rows / WO suffixes / emails. Bail if a dispatch
            # for this exact cluster is already in flight.
            _dispatch_inflight_key = f"_dispatching_{cluster_hash}"
            if st.session_state.get(_dispatch_inflight_key):
                st.warning("⏳ Dispatch already in progress for this route - please wait.")
                st.stop()
            st.session_state[_dispatch_inflight_key] = True

            # 🚀 STEP 2: PROCEED WITH DISPATCH
            _dispatch_result = {}
            with st.spinner("Generating link..."):
                home = _ic_home_loc(ic, f"{cluster['center'][0]},{cluster['center'][1]}")
                # Build ordered task IDs from Google Maps waypoint order
                _addr_list = list(stop_metrics.keys())
                _ordered_addrs = [_addr_list[i] for i in _wp_order] if _wp_order else _addr_list
                _stop_order_ids = []
                for _oa in _ordered_addrs:
                    for _t in cluster['data']:
                        if _t.get('full') == _oa:
                            _stop_order_ids.append(_t['id'])

                payload = {
                    "cluster_hash": cluster_hash,
                    "icn": ic.get('name', 'Unknown'),
                    "ice": ic.get('email', ''),
                    "wo": wo_val, 
                    "pod": pod_name,
                    "city": cluster.get('city', 'Unknown'),
                    "state": cluster.get('state', 'Unknown'),
                    "due": str(due), "comp": final_pay, "lCnt": cluster['stops'], "mi": mi, "time": t_str,
                    "phone": str(ic.get('phone', '')),
                    # Dispatcher email priority: user-saved (Settings ✉️ pill) wins over
                    # the login record's email (which today is hardcoded to Nick for
                    # every account, so unusable for CC'ing the actual sender). Falls
                    # back to empty so the portal skips the CC entirely rather than
                    # CC'ing Nick on his own primary email — Formspree dedupes those
                    # anyway, which is why nobody else was getting a copy.
                    "dispatcherEmail": str(
                        st.session_state.get('_auth_user', {}).get('email', '')
                        or ''
                    ).strip(),
                    "dispatcherName": str(st.session_state.get('_auth_user', {}).get('name', '') or '').strip(),
                    "locs": " | ".join([home] + list(stop_metrics.keys()) + [home]),
                    "taskIds": ",".join(task_ids),
                    # Apr 27 2026 — list of task IDs that are digital, used by GAS
                    # assignTasksToWorker to SKIP setting completeAfter for those tasks
                    # (digital tasks should keep their original Onfleet start time;
                    # only physical kiosk tasks get the today+2 @ 4pm bump).
                    "digitalTaskIds": ",".join(str(t['id']).strip() for t in cluster['data'] if t.get('is_digital')),
                    "tCnt": len(task_ids),
                    "kCnt": cluster.get('inst_count', 0),
                    "rCnt": cluster.get('remov_count', 0),
                    "dCnt": sum(1 for t in cluster['data'] if t.get('is_digital')),
                    "jobOnly": " | ".join([f"{addr} {pills}" for addr, pills in loc_pills.items()]),
                    "stopOrder": ",".join(str(tid) for tid in _stop_order_ids),
                    "stopData": json.dumps([{
                        "addr": addr,
                        "venue": metrics.get("venue_name", ""),
                        "t_count": metrics.get("t_count", 0),
                        "esc": metrics.get("esc", False),
                        "inst": metrics.get("inst", 0),
                        "remov": metrics.get("remov", 0),
                        "n_ad": metrics.get("n_ad", 0),
                        "c_ad": metrics.get("c_ad", 0),
                        "d_ad": metrics.get("d_ad", 0),
                        # Per-stop OnFleet customField values — first non-empty value
                        # wins when multiple tasks at the same address have differing
                        # values (rare). These flow into the packing slip when the
                        # route eventually becomes a ghost (sheet-only) row.
                        "kioskId": next((str(t.get("kiosk_id","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("kiosk_id","")).strip()), ""),
                        "venueId": next((str(t.get("venue_id","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("venue_id","")).strip()), ""),
                        "locationInVenue": next((str(t.get("location_in_venue","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("location_in_venue","")).strip()), ""),
                        "campaigns": list({
                            (t.get("client_company",""), t.get("escalated",False), str(t.get("boosted_standard","")).lower()):
                            {"name": t.get("client_company",""), "esc": t.get("escalated",False), "bs": str(t.get("boosted_standard","")).lower()}
                            for t in cluster["data"] if t.get("full") == addr and t.get("client_company")
                        }.values()),
                        # Per-stop fields needed for the packing slip when the route
                        # later becomes a ghost. First non-empty value wins when
                        # multiple tasks at the same address differ (which is rare
                        # but happens — e.g. two campaigns at one venue).
                        "customerType": next((str(t.get("customer_type","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("customer_type","")).strip()), ""),
                        "boostedStandard": next((str(t.get("boosted_standard","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("boosted_standard","")).strip()), ""),
                        "artFile": next((str(t.get("art_file","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("art_file","")).strip()), ""),
                        "zip": next((str(t.get("zip","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("zip","")).strip()), ""),
                        "sio": next((str(t.get("sio","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("sio","")).strip()), ""),
                    } for addr, metrics in stop_metrics.items()])
                }
                try:
                    if DB_ENGINE is None:
                        raise RuntimeError("Railway database is unavailable")
                    _da.save_route(DB_ENGINE, wo_val, ic.get('name', 'Unknown'), payload)
                    _dispatch_result = {"success": True, "routeId": wo_val}
                except Exception as e:
                    _dispatch_result = {"_error": str(e)}

            # Security audit H23 - the saveRoute POST has resolved; release
            # the in-flight guard so a later legitimate dispatch / resend works.
            st.session_state.pop(_dispatch_inflight_key, None)

            # Spinner now closed — handle result
            if _dispatch_result.get("_timeout"):
                # 🌟 Sep 2026 — flag this cluster as "last attempt timed out" so
                # the NEXT click (Step 0 above) forces a fresh sheet read
                # before doing anything else, instead of retrying blind
                # against a stale snapshot and risking a duplicate route.
                st.session_state[_timed_out_key] = True
                st.warning("⏱️ Google Sheets is taking too long. The earlier attempt may have actually saved — click **Generate Link** again and it'll check for that before creating a new one.")
            elif _dispatch_result.get("_error"):
                st.error(f"Connection Error: {_dispatch_result['_error']} — Please try again.")
            elif _dispatch_result.get("success"):
                final_route_id = _dispatch_result.get("routeId")
                st.session_state[sync_key] = final_route_id
                st.session_state[f"sent_ts_{cluster_hash}"] = datetime.now().strftime('%m/%d %I:%M %p')
                st.session_state[f"contractor_{cluster_hash}"] = ic.get('name', 'Unknown')
                st.session_state[f"wo_{cluster_hash}"] = wo_val
                # Stash comp + due locally so the post-dispatch card has correct values
                # even if the sheet readback is briefly stale (was previously rendering
                # $0 / N/A for ~15s while the cached fetch_sent_records_from_sheet expired).
                st.session_state[f"comp_{cluster_hash}"] = final_pay
                st.session_state[f"due_{cluster_hash}"] = str(due)
                st.session_state[f"route_state_{cluster_hash}"] = "email_sent"
                st.session_state[f"reverted_{cluster_hash}"] = False
                # Sep 2026 — Nick: "after the email is sent the refresh to
                # reflect the new change takes at least 2 minutes." Root
                # cause: this card doesn't need fresh sheet data to show up
                # in Sent — the bucket router (`elif route_state ==
                # "email_sent"` in the Ready/Sent routing loop) reads the
                # session-state overrides set just above and moves this
                # route into Sent immediately, with no sheet round-trip.
                # The `.clear()` that used to sit here bought nothing for
                # THIS route's visibility; all it did was force the very
                # next render's fetch_sent_records_from_sheet() call into a
                # cache miss, which pays for a full live resync of 6 Google
                # Sheets tabs synchronously — THAT was the 2-minute stall,
                # not the send. Left uncleared, sent_db serves its cached
                # value for up to its normal 5-minute TTL (or gets busted
                # sooner by any other dispatcher's own action), so
                # cross-dispatcher visibility of this row still catches up
                # on its own without blocking this render.
                final_sig = email_body_content.replace("LINK_PENDING", final_route_id)
                subject_line = requests.utils.quote(f"Route Request | {wo_val}")
                body_content = requests.utils.quote(final_sig)
                # Security audit M3 - the IC email is quote()-encoded like the
                # subject/body, and the whole URL is JSON-encoded before being
                # placed into the injected <script> so a poisoned email cell
                # cannot break out of the JS string literal.
                _to_enc = requests.utils.quote(str(ic.get('email', '') or ''))
                outlook_url = f"https://outlook.office.com/mail/deeplink/compose?to={_to_enc}&subject={subject_line}&body={body_content}"
                _link_ph = st.empty()
                _link_ph.success("✅ Link Live! Outlook opening — or use the button below.")
                # Best-effort automatic popup — ONE SHOT only. Store an
                # explicit intent flag and consume it immediately on this generate/
                # resend rerun so later actions (especially Move to Sent) can never
                # reopen Outlook.
                _popup_key = f"_outlook_popup_once_{cluster_hash}"
                st.session_state[_popup_key] = outlook_url
                _popup_url = st.session_state.pop(_popup_key, None)
                if _popup_url:
                    st.components.v1.html(
                        f"<script>if(window.screen.width>768){{try{{window.open({json.dumps(_popup_url)},'_blank');}}catch(e){{}}}}</script>",
                        height=0,
                    )
                st.session_state[f"_persisted_outlook_{cluster_hash}"] = outlook_url
                # Stash the mailto: URL so the "Default Mail" button persists across
                # reruns as a fallback for users who prefer their local mail client.
                _mailto = f"mailto:{ic.get('email','')}?subject={subject_line}&body={body_content}"
                st.session_state[f"_persisted_mailto_{cluster_hash}"] = _mailto
                # Keep this route card visible after generation so dispatchers can
                # click Open Outlook before the route moves to Sent. The explicit
                # "Move to Sent" button below triggers the app-level rerun.
                route_state = "email_sent"
                time.sleep(0.25)
                _link_ph.empty()
                st.rerun(scope="app")
            else:
                # Surface any other GAS response so silent failures (auth gate
                # returning {"error": "Unauthorized"} as HTTP 200, etc.) become
                # visible instead of looking like a no-op. (May 30 2026 - Nick.)
                _err_msg = (_dispatch_result.get("error") if isinstance(_dispatch_result, dict) else None) or "Unknown response from Google Apps Script (no success flag returned)."
                st.error(f"❌ Save failed: {_err_msg}")

    # 📨 PERSISTENT DEFAULT MAIL BUTTON
    # Renders below the GENERATE button whenever the route is in email_sent state
    # and we have a stashed mailto: URL. Sits outside the if-button click block so
    # it survives reruns — fixes the "second-generate doesn't show Default Mail"
    # bug where the button only appeared during the one rerun that processed the
    # click and then vanished. Disappears automatically if the route moves to
    # field_nation or is revoked back to pending (route_state changes).
    if route_state == "email_sent" and not is_fn:
        _persisted_outlook = st.session_state.get(f"_persisted_outlook_{cluster_hash}")
        _persisted_mailto = st.session_state.get(f"_persisted_mailto_{cluster_hash}")
        if _persisted_outlook:
            st.markdown(f"""<div style="display:flex;margin:6px 0;">
<a href="{_persisted_outlook}" target="_blank" rel="noopener noreferrer"
style="flex:1;text-align:center;background:#633094;color:#ffffff;border:1px solid #633094;
padding:12px;border-radius:10px;font-weight:800;font-size:14px;
text-decoration:none;">📬 Open Outlook</a>
</div>""", unsafe_allow_html=True)
        if _persisted_mailto:
            st.markdown(f"""<div style="display:flex;margin:6px 0;">
<a href="{_persisted_mailto}"
style="flex:1;text-align:center;background:#ffffff;color:#633094;border:1px solid #633094;
padding:12px;border-radius:10px;font-weight:800;font-size:14px;
text-decoration:none;">📨 Default Mail</a>
</div>""", unsafe_allow_html=True)

    # --- 🌐 FIELD NATION PERSISTENCE (CHECKBOX) ---

    if route_state != "email_sent":
        # Reuse the checkbox rendered above for assignment and revocation.
        if fn_checked and not is_fn:
            # Persist the FN route before changing the local UI state.
            home = _ic_home_loc(ic, f"{cluster['center'][0]},{cluster['center'][1]}")
            _fn_due = st.session_state.get(f"dd_{pod_name}_{cluster_hash}", datetime.now().date()+timedelta(DEFAULT_DUE_DAYS))
            # WO# format: FN{MMDDYYYY}-{City} {ST}-{N}
            #   Example: FN05112026-Ladera Ranch CA-1
            # N auto-increments for repeat routes to the same centroid on the same day.
            _fn_date_tag = datetime.now().strftime('%m%d%Y')
            _fn_city  = str(cluster.get('city', '') or 'Unknown').strip()
            _fn_state = str(cluster.get('state', '') or '').strip().upper()
            _fn_wo_prefix = f"FN{_fn_date_tag}-{_fn_city} {_fn_state}-"
            _fn_next_n = 1
            try:
                _, _gd_all, _, _ = fetch_sent_records_from_sheet()
                _existing_nums = []
                for _pod_ghosts in (_gd_all or {}).values():
                    if not isinstance(_pod_ghosts, list):
                        continue
                    for _g in _pod_ghosts:
                        _g_wo = str(_g.get('wo', '') or '')
                        if _g_wo.startswith(_fn_wo_prefix):
                            try:
                                _existing_nums.append(int(_g_wo[len(_fn_wo_prefix):]))
                            except (TypeError, ValueError):
                                pass
                if _existing_nums:
                    _fn_next_n = max(_existing_nums) + 1
            except Exception as _wo_e:
                _log_err("fn_wo_counter_lookup", _wo_e)
            _fn_wo_full = f"{_fn_wo_prefix}{_fn_next_n}"
            fn_payload = {
                "cluster_hash": cluster_hash,
                "icn": "Field Nation",
                "city": cluster.get('city', 'Unknown'),
                "state": cluster.get('state', 'Unknown'),
                "taskIds": ",".join(task_ids),
                # WO# unique per route. Was previously just FN-MMDDYYYY which gave every
                # route created on the same day the same WO — fetch_sent_records_from_sheet
                # then collapsed them into one ghost route on refresh. Suffix with the
                # first 6 chars of cluster_hash so each route gets a stable, unique WO.
                "wo": _fn_wo_full,
                "due": str(_fn_due),
                "lCnt": cluster['stops'],
                "tCnt": len(task_ids),
                "kCnt": cluster.get('inst_count', 0),
                "locs": " | ".join([home] + list(stop_metrics.keys()) + [home]),
                # 🌟 FN-stopData fix (May 2026):
                # Previously this payload had no stopData, so FN sheet rows landed
                # with empty stop_data. _fn_ghost_to_cluster then reconstructed every
                # synthetic task with client_company='' (no campaigns to read), the
                # venue accordion's camp_rows came out empty, and the dispatcher saw
                # only the task-type tally — no campaign names. Mirror the saveRoute
                # payload at line ~4733 so FN ghosts carry the same per-stop metadata
                # (campaigns / kioskId / customerType / boostedStandard / artFile / sio)
                # that normal Sent ghosts already have. Only affects FN routes assigned
                # AFTER this deploy; existing FN rows in the sheet still lack stopData
                # and will keep showing only the task-type tally until re-assigned.
                "stopData": json.dumps([{
                    "addr": addr,
                    "venue": metrics.get("venue_name", ""),
                    "t_count": metrics.get("t_count", 0),
                    "esc": metrics.get("esc", False),
                    "inst": metrics.get("inst", 0),
                    "remov": metrics.get("remov", 0),
                    "n_ad": metrics.get("n_ad", 0),
                    "c_ad": metrics.get("c_ad", 0),
                    "d_ad": metrics.get("d_ad", 0),
                    "kioskId": next((str(t.get("kiosk_id","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("kiosk_id","")).strip()), ""),
                    "venueId": next((str(t.get("venue_id","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("venue_id","")).strip()), ""),
                    "locationInVenue": next((str(t.get("location_in_venue","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("location_in_venue","")).strip()), ""),
                    "campaigns": list({
                        (t.get("client_company",""), t.get("escalated",False), str(t.get("boosted_standard","")).lower()):
                        {"name": t.get("client_company",""), "esc": t.get("escalated",False), "bs": str(t.get("boosted_standard","")).lower()}
                        for t in cluster["data"] if t.get("full") == addr and t.get("client_company")
                    }.values()),
                    "customerType": next((str(t.get("customer_type","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("customer_type","")).strip()), ""),
                    "boostedStandard": next((str(t.get("boosted_standard","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("boosted_standard","")).strip()), ""),
                    "artFile": next((str(t.get("art_file","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("art_file","")).strip()), ""),
                    "zip": next((str(t.get("zip","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("zip","")).strip()), ""),
                    "sio": next((str(t.get("sio","")).strip() for t in cluster["data"] if t.get("full")==addr and str(t.get("sio","")).strip()), ""),
                } for addr, metrics in stop_metrics.items()]),
            }

            # Commit to the source this app reads before moving the card.
            try:
                if DB_ENGINE is None:
                    raise RuntimeError("Railway database is unavailable")
                _fn_result = _da.save_to_field_nation(DB_ENGINE, _fn_wo_full, fn_payload)
                if not _fn_result.get("success"):
                    raise RuntimeError(str(_fn_result))
            except Exception as _fn_save_e:
                _log_err("save_to_field_nation", _fn_save_e)
                st.error(f"Could not move route to Field Nation: {_fn_save_e}")
                st.stop()
            fetch_sent_records_from_sheet.clear()
            st.session_state[f"route_state_{cluster_hash}"] = "field_nation"
            # Move OnFleet tasks into the Field Nation team after the DB save.
            # Wrapped + pre-checked so a missing team id or thread error can
            # never block the rerun and break the checkbox UX.
            try:
                _fn_tid = st.session_state.get('_fn_team_id')
                _fn_wid = st.session_state.get('_fn_worker_id')
                if task_ids and (_fn_tid or _fn_wid):
                    assign_tasks_to_fn_team(
                        list(task_ids), _fn_tid,
                        fn_worker_id=_fn_wid,
                        wo_name=fn_payload.get('wo', ''),
                        due_date=str(_fn_due),
                        cluster_hash=cluster_hash,
                    )
            except Exception as _fn_team_e:
                _log_err("fn_assign_sync", _fn_team_e)
            st.session_state[f"reverted_{cluster_hash}"] = False  # Postgres row is committed before rerender
            st.toast("✅ Saved to Field Nation Tab")
            st.rerun(scope="app")  # security audit L6 - bucket re-sort is app-scoped
        
        elif not fn_checked and is_fn:
            # 🌟 ADDED Safety check for Field Nation revocation
            with st.popover("🚨 Confirm Field Nation Revocation", use_container_width=True):
                st.error("Remove this route from Field Nation tracking?")
                # 🌟 THE FIX: Upgraded to a callback so it doesn't freeze the screen!
                if st.button("🚨 Yes, Revoke FN", key=f"fn_rev_confirm_{pod_name}_{cluster_hash}", type="primary", use_container_width=True):
                    # Pass the in-scope `cluster` so revoke_field_nation can hand
                    # task_ids to move_to_dispatch's Onfleet PUT (otherwise the PUT
                    # is skipped — task_ids list is empty without cluster_data).
                    revoke_field_nation(cluster_hash=cluster_hash, pod_name=pod_name, cluster_data=cluster)
                    fetch_sent_records_from_sheet.clear()   # bust the sheet cache so the rerun sees the post-revoke state
                    st.rerun(scope="app")  # security audit L6 - bucket re-sort is app-scoped
            st.stop()

    BG_COLOR = "#FEF9C3"
    TEXT_COLOR = "#854D0E"
    BORDER_COLOR = "#FACC15"

    if route_state == "field_nation":
        # 🐛 DIAGNOSTIC: when ?debug=fn is on the URL, dump the actual
        # cluster['data'] task dicts so we can SEE what client_company /
        # campaign / task-type fields the renderer is being handed. Solves
        # the "campaigns missing on FN cards but present in Ready" mystery
        # one row at a time. (May 17 2026.)
        if st.query_params.get("debug") == "fn" and _is_admin_or_manager():
            with st.expander(f"🐛 FN data debug — {cluster_hash[:8]}", expanded=False):
                _dbg_addrs = {}
                for _t in cluster.get('data', []):
                    _ad = _t.get('full', '')
                    _dbg_addrs.setdefault(_ad, []).append({
                        'id': _t.get('id', ''),
                        'client_company': _t.get('client_company', ''),
                        'task_type': _t.get('task_type', ''),
                        'venue_name': _t.get('venue_name', ''),
                        'boosted_standard': _t.get('boosted_standard', ''),
                        'escalated': _t.get('escalated', False),
                    })
                st.caption(f"Total tasks in cluster['data']: {len(cluster.get('data', []))}")
                st.caption(f"Cluster is FN ghost pseudo: {cluster.get('_is_fn_ghost_pseudo', False)}")
                for _a, _tasks in _dbg_addrs.items():
                    st.markdown(f"**{_a}** ({len(_tasks)} tasks)")
                    _n_with_cmp = sum(1 for _t in _tasks if _t['client_company'])
                    st.text(f"  with client_company: {_n_with_cmp}/{len(_tasks)}")
                    for _t in _tasks[:4]:
                        st.text(f"    id={_t['id']}  cmp='{_t['client_company']}'  tt='{_t['task_type']}'  venue='{_t['venue_name']}'")
                    if len(_tasks) > 4:
                        st.text(f"    … +{len(_tasks)-4} more")

        # ── ROUTE STOPS (FN routes) ─────────────────────────────────────
        # May 17 2026 — Nick: "I want the original look of the ready cards
        # with campaign names to transition to Field Nation tabs". Renders
        # the SAME per-stop accordion structure Ready/Flagged uses (including
        # campaign-row drill-down inside each <details>), so the FN tab card
        # shows venue / kiosk pill / task-type icons / boosted / esc pills /
        # tasks count, plus a campaign-list expansion identical to what
        # dispatchers see in Ready. Builds rows inline (mirrors render_dispatch
        # lines ~4737-4837) instead of calling make_venue_details so we get
        # the exact pill order + display_addr "+" prefix behavior.
        _fn_dispatch_rows = []
        for _addr, _metrics in stop_metrics.items():
            _ip = []
            if _metrics['n_ad']    > 0: _ip.append("🆕")
            if _metrics['c_ad']    > 0: _ip.append("🔄")
            if _metrics['d_ad']    > 0: _ip.append("⚪")
            if _metrics['remov']   > 0: _ip.append(f"🗑️ {_metrics['remov']}")
            if _metrics['custom']:      _ip.append("📋")
            if _metrics['digi_off']> 0: _ip.append("📵")
            if _metrics['digi_srv']> 0: _ip.append("⚙️")
            _pill_str = " ".join(_ip)
            _k_html  = f" <span style='color:#16a34a;font-weight:800;font-size:10px;'>🛠️ {_metrics['inst']} Kiosk</span>" if _metrics['inst'] > 0 else ""
            _di_html = f" <span style='color:#0f766e;font-weight:800;font-size:10px;'>🔧 {_metrics['digi_ins']} Ins/Rem</span>" if _metrics['digi_ins'] > 0 else ""
            _bo_html = f" <span style='color:#dc2626;font-weight:800;font-size:10px;'>🔥 {_metrics['boost_cnt']}</span>" if _metrics.get('boost_cnt', 0) > 0 else ""
            _lp_html = f" <span style='color:#ca8a04;font-weight:800;font-size:10px;'>⭐ {_metrics['lplus_cnt']}</span>" if _metrics.get('lplus_cnt', 0) > 0 else ""
            _esc_n   = sum(1 for _t in cluster['data'] if _t.get('full') == _addr and _t.get('escalated'))
            _esc_html= f" <span style='color:#dc2626;font-weight:900;font-size:10px;'>❗ {_esc_n}</span>" if _esc_n > 0 else ""
            _display_addr = f"+ {_addr}" if _metrics.get('is_new') else _addr
            _vp      = f"<span style='color:#94a3b8;font-size:11px;font-weight:600;white-space:normal;'>{_metrics['venue_name']} — </span>" if _metrics.get('venue_name') else ""
            _tp      = f"<span style='color:#633094;background:#f3e8ff;padding:1px 5px;border-radius:8px;font-weight:800;font-size:10px;'>{_metrics['t_count']} Tasks</span>"
            _ih      = f"<span style='font-size:13px;margin-left:6px;'>{_pill_str}</span>" if _pill_str else ""
            # Campaign rows in expansion — same aggregation Ready uses.
            from collections import defaultdict as _dd
            _loc_tasks = [_t for _t in cluster['data'] if _t.get('full') == _addr]
            _cg = _dd(lambda: {'count': 0, 'esc': 0, 'boost': 0, 'lplus': 0, 'tt_badge': '', 'cmp': ''})
            for _t in _loc_tasks:
                _cm = _t.get('client_company', '')
                if not _cm: continue
                _tt = str(_t.get('task_type', '')).lower()
                if _t.get('is_digital'):
                    if 'offline' in _tt: _tb = "📵 Offline"
                    elif 'ins/re' in _tt: _tb = "🔧 Ins/Rem"
                    else: _tb = "⚙️ Service"
                elif 'install' in _tt: _tb = "🛠️ Install"
                elif any(x in _tt for x in ['kiosk removal', 'remove kiosk']): _tb = "🗑️ Removal"
                elif any(x in _tt for x in ['continuity', 'photo retake', 'swap']): _tb = "🔄 Continuity"
                elif any(x in _tt for x in ['default', 'pull down']): _tb = "⚪ Default"
                elif any(x in _tt for x in ['new ad', 'art change', 'top']) or not _tt: _tb = "🆕 New Ad"
                else: _tb = f"📋 {_tt.title()}"
                _key = (_cm, _tb)
                _grp = _cg[_key]
                _grp['cmp'] = _cm
                _grp['tt_badge'] = _tb
                _grp['count'] += 1
                if _t.get('escalated'): _grp['esc'] += 1
                _bs = str(_t.get('boosted_standard', '')).lower()
                if 'local plus' in _bs: _grp['lplus'] += 1
                elif 'boosted' in _bs: _grp['boost'] += 1
            _crows = []
            for (_cm, _tb), _grp in _cg.items():
                _cnt = _grp['count']
                _cs = f" <span style='color:#94a3b8;font-weight:600;'>× {_cnt}</span>" if _cnt > 1 else ""
                _ep = f" <span style='color:#dc2626;font-weight:800;'>❗ {_grp['esc']}</span>" if _grp['esc'] > 0 else ""
                _bp = f" <span style='color:#dc2626;font-weight:800;'>🔥 {_grp['boost']}</span>" if _grp['boost'] > 0 else ""
                _lp = f" <span style='color:#ca8a04;font-weight:800;'>⭐ {_grp['lplus']}</span>" if _grp['lplus'] > 0 else ""
                _crows.append(
                    f"<div style='font-size:11px;color:#475569;padding:2px 4px;margin-top:3px;'>"
                    f"• <span style='color:#0f172a;font-weight:600;'>{_cm}</span>"
                    f"&nbsp;<span style='font-weight:700;color:#0f172a;'>{_tb}</span>"
                    f"{_cs}{_ep}{_bp}{_lp}</div>"
                )
            # Fallback to task-type tally when client_company is empty across
            # all tasks (older FN ghosts pre-2026-05-04).
            if _crows:
                _cb = f"<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;'>{''.join(_crows)}</div>"
            else:
                _ttl = []
                if _metrics['inst']:     _ttl.append(f"🛠️ Install &times; {_metrics['inst']}")
                if _metrics['remov']:    _ttl.append(f"🗑️ Removal &times; {_metrics['remov']}")
                if _metrics['n_ad']:     _ttl.append(f"🆕 New Ad &times; {_metrics['n_ad']}")
                if _metrics['c_ad']:     _ttl.append(f"🔄 Continuity &times; {_metrics['c_ad']}")
                if _metrics['d_ad']:     _ttl.append(f"⚪ Default &times; {_metrics['d_ad']}")
                if _metrics['digi_ins']: _ttl.append(f"🔧 Ins/Rem &times; {_metrics['digi_ins']}")
                if _metrics['digi_off']: _ttl.append(f"📵 Offline &times; {_metrics['digi_off']}")
                if _metrics['digi_srv']: _ttl.append(f"⚙️ Service &times; {_metrics['digi_srv']}")
                for _lbl, _n in _metrics['custom'].items():
                    _ttl.append(f"📋 {_lbl} &times; {_n}")
                _body = "".join(
                    f"<div style='font-size:11px;color:#475569;padding:2px 4px;margin-top:3px;'>• <span style='font-weight:700;color:#0f172a;'>{_ln}</span></div>"
                    for _ln in _ttl
                )
                _cb = f"<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;'>{_body}</div>" if _ttl else ""
            _fn_dispatch_rows.append(
                f"<details class='fn-loc-row'>"
                f"<summary class='fn-loc-summary'>"
                f"<span class='fn-chevron'>›</span>"
                f"{_vp}<span style='font-weight:700;color:#0f172a;'>{_display_addr}</span>{_k_html}{_di_html}{_bo_html}{_lp_html}{_esc_html} &nbsp;{_tp}{_ih}"
                f"</summary>{_cb}</details>"
            )
        st.markdown(
            f"{VENUE_SECTION_CSS}<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;margin-bottom:8px;'>"
            f"<div style='background:#f8fafc;border-bottom:1px solid #e2e8f0;padding:6px 12px;'>"
            f"<span style='font-size:9px;font-weight:900;color:#94a3b8;text-transform:uppercase;letter-spacing:0.1em;'>Route Stops</span></div>"
            f"<div style='padding:2px 8px 4px 8px;'>{''.join(_fn_dispatch_rows)}</div></div>",
            unsafe_allow_html=True,
        )



        # 🌟 FIELD NATION BUTTONS
        _due = st.session_state.get(f"dd_{pod_name}_{cluster_hash}", datetime.now().date() + timedelta(DEFAULT_DUE_DAYS))
        _pay = st.session_state.get(_pay_master_key, 0.0)
        fn_buf, _ = generate_fn_upload(stop_metrics, cluster, _due, _pay, cluster_hash)

        dl_col, link_col = st.columns(2)
        with dl_col:
            if fn_buf:
                st.download_button(
                    label="Download CSV",
                    data=fn_buf,
                    file_name=f"FN_Upload_{cluster.get('city', 'Route')}_{datetime.now().strftime('%m%d%Y')}.csv",
                    mime="text/csv",
                    key=f"fn_dl_{cluster_hash}",
                    use_container_width=True
                )
        with link_col:
            st.link_button(
                "Field Nation",
                url="https://app.fieldnation.com/projects",
                use_container_width=True
            )

        # 📤 PER-ROUTE POSTED! — mirrors the bulk Posted! at the top of the FN tab
        # but fires markFNPosted for just this cluster_hash. Once posted, the
        # button switches to a disabled "Posted ✓ {timestamp}" indicator.
        _fn_posted_dict = st.session_state.setdefault('_fn_posted', {})
        _already_posted_ts = _fn_posted_dict.get(cluster_hash)
        # Mark Posted + Assigned FN Rep paired into one 2-up row. (May 23 2026.)
        _fn_act_c1, _fn_act_c2 = st.columns(2)
        with _fn_act_c1:
            if _already_posted_ts:
                st.button(
                    f"Posted ✓ {_already_posted_ts}",
                    key=f"fn_posted_done_{pod_name}_{cluster_hash}",
                    use_container_width=True,
                    disabled=True,
                )
            else:
                if st.button(
                    "Posted",
                    key=f"fn_post_one_{pod_name}_{cluster_hash}",
                    use_container_width=True,
                ):
                    _ts = datetime.now().strftime('%m/%d %I:%M %p')
                    _fn_posted_dict[cluster_hash] = _ts
                    st.session_state['_fn_posted'] = _fn_posted_dict
                    try:
                        threading.Thread(
                            target=lambda: requests.post(
                                GAS_WEB_APP_URL,
                                json={"action": "markFNPosted", "auth_secret": GAS_AUTH, "cluster_hash": cluster_hash},
                                timeout=15,
                            ),
                            daemon=True,
                        ).start()
                    except Exception as _fpe:
                        _log_err("markFNPosted/per-route", _fpe)
                    # --- Phase 2 migration: best-effort Postgres mirror (2026-09-21) ---
                    if DB_ENGINE is not None:
                        try:
                            _da.mirror_mark_fn_posted_by_cluster_hash(DB_ENGINE, cluster_hash)
                        except Exception as _dw_e:
                            _log_err("pg_dual_write_mark_fn_posted", _dw_e)
                    st.toast("📤 Marked as Posted to Field Nation.")
                    st.rerun()
        with _fn_act_c2:
            if st.button("Assigned", key=f"fn_assigned_one_{pod_name}_{cluster_hash}", use_container_width=True):
                with st.spinner("Marking as Assigned to FN Rep — moving to Accepted..."):
                    try:
                        # 90s timeout: markFNAssigned does (1) OnFleet routePlan
                        # rename, (2) per-task metadata + worker re-PUT, (3) per-
                        # stop Monday lookups + WO/installer mutations. An 11-stop
                        # route can fire ~120+ HTTP calls server-side; the old 25s
                        # cap timed out on bigger routes. (May 17 2026.)
                        # Jul 27 2026 — Nick: "Route hash not found" errors when task set drifts.
                        # Send task_ids too; GAS can fall back to task-ID intersection when
                        # the exact cluster_hash no longer matches the stored payload's hash.
                        _mfa_tids = ",".join(str(t['id']).strip() for t in cluster.get('data', []))
                        res = requests.post(
                            GAS_WEB_APP_URL,
                            json={
                                "action": "markFNAssigned",
                                "auth_secret": GAS_AUTH,
                                "cluster_hash": cluster_hash,
                                "taskIds": _mfa_tids,
                            },
                            timeout=90,
                        ).json()
                        if res.get("success"):
                            # Local state: route moves to Accepted (treated as already accepted by the FN rep).
                            # contractor_name stays "Field Nation" so the Accepted-tab badge + shortened
                            # 2-item finalization checklist (no Onfleet step) kicks in on next render.
                            st.session_state[f"contractor_{cluster_hash}"] = "Field Nation"
                            st.session_state.pop(f"route_state_{cluster_hash}", None)
                            # NOT reverted (May 23 2026). markFNAssigned moves the route
                            # TO Accepted, not back to Dispatch. reverted_=True hid it
                            # from the Accepted tab (its ghost is filtered on reverted_)
                            # and dropped any still-live cluster into Ready. Clear it so
                            # the route lands in Accepted on this rerun.
                            st.session_state[f"reverted_{cluster_hash}"] = False
                            # --- Phase 2 migration: best-effort Postgres mirror (2026-09-21) ---
                            # mirror_mark_fn_assigned_by_cluster_hash never calls
                            # fn_side_effects (GAS already ran the real Onfleet
                            # rename/re-PUT above) -- no-ops harmlessly if this
                            # cluster_hash has no Postgres field_nation_orders row
                            # yet (e.g. posted to FN before the saveToFieldNation
                            # mirror existed).
                            if DB_ENGINE is not None:
                                try:
                                    _da.mirror_mark_fn_assigned_by_cluster_hash(DB_ENGINE, cluster_hash)
                                except Exception as _dw_e:
                                    _log_err("pg_dual_write_mark_fn_assigned", _dw_e)
                            # Force the next render to re-pull the sheet so the new Accepted row is visible.
                            fetch_sent_records_from_sheet.clear()
                            st.toast("✅ Assigned to FN Rep — moved to Accepted!")
                            st.rerun()
                        else:
                            st.error(f"Sheet Error: {res.get('error')}")
                    except Exception as e:
                        st.error(f"Connection Failed: {e}")

                    
def smart_sync_pod(pod_name):
    """
    Fetches only NEW tasks from Onfleet not already tracked in session state.
    - New tasks within radius of existing cluster → appended, inherit IC + pricing
    - New tasks with no nearby cluster → new standalone cluster for dispatcher
    - New task addresses flagged with is_new=True for UI badge
    """
    config = POD_CONFIGS[pod_name]
    existing_clusters = st.session_state.get(f"clusters_{pod_name}", [])

    # Build set of all task IDs already tracked
    known_ids = set()
    for c in existing_clusters:
        for t in c.get('data', []):
            known_ids.add(str(t['id']).strip())

    _bar = st.progress(0, text="🔍 Checking Onfleet for new tasks...")

    def _tick(pct, msg):
        """Update progress bar AND re-render the spin-card overlay so the timer
        actually counts up during smart sync (was previously frozen at 0:00)."""
        try: _bar.progress(pct, text=msg)
        except Exception: pass
        _ov = st.session_state.get('_loading_overlay')
        _st = st.session_state.get('_loading_start')
        _pn = st.session_state.get('_loading_pod') or pod_name
        if _ov and _st:
            import time as _t
            _el = int(_t.time() - _st); _m = _el // 60; _s = _el % 60
            _ov.markdown(f"""
                <style>
                    @keyframes spin {{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
                    .dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;
                        padding:36px 32px;text-align:center;margin:20px 0;}}
                    .dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;
                        border-top:4px solid #633094;border-radius:50%;
                        animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
                    .dcc-pill{{display:inline-block;font-size:13px;font-weight:700;
                        color:#633094;background:#f3e8ff;border-radius:20px;
                        padding:4px 14px;margin-top:12px;}}
                </style>
                <div class='dcc-card'>
                    <div class='dcc-spin'></div>
                    <p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Checking New Tasks — {_pn} Pod</p>
                    <p style='font-size:13px;color:#64748b;margin:0 0 8px 0;'>{msg}</p>
                    <div class='dcc-pill'>⏱ {_m}:{_s:02d}</div>
                <div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: 2–4 min for full pod fleet (Red Pod is heaviest).</div>
                </div>
            """, unsafe_allow_html=True)

    _tick(0, "🔍 Checking Onfleet for new tasks...")

    # 🌍 SHARED ONFLEET PULL — see _fetch_onfleet_open_tasks_cached() near the top
    # of this file. Smart Sync runs frequently (every "Check New Tasks" click), so
    # routing it through the shared cache eliminates the worst rate-budget burner
    # in the app.
    try:
        _onfleet_data = _fetch_onfleet_open_tasks_cached()
    except Exception as _e:
        st.error(f"Onfleet API Error: {_e}")
        _log_err("smart_sync_pod", f"shared pull failed: {type(_e).__name__}: {_e}")
        return
    target_team_ids    = _onfleet_data['target_team_ids']
    esc_team_ids       = _onfleet_data['esc_team_ids']
    cvs_remov_team_ids = _onfleet_data['cvs_remov_team_ids']
    _fn_team_id_local  = _onfleet_data.get('fn_team_id')
    _excluded_team_set = set(_onfleet_data.get('excluded_team_ids') or [])
    if _fn_team_id_local:
        _excluded_team_set.add(_fn_team_id_local)
    all_tasks_raw      = _onfleet_data['tasks']
    # Stash fresh state=0 IDs for STATE=0 IS AUTHORITATIVE reclaim in run_pod_tab.
    st.session_state[f'_state0_ids_{pod_name}'] = {str(_t.get('id', '')).strip() for _t in all_tasks_raw}
    if _onfleet_data.get('_hit_cap'):
        _log_err("smart_sync_pod", f"hit pagination cap (200 pages)")
        st.warning(f"⚠️ Hit pagination cap of 200 pages during Smart Sync. Some new tasks may be missing.")

    _tick(0.4, "🔎 Identifying new tasks...")

    # Filter to only NEW tasks for this pod
    fresh_sent_db, _, _archived_wos, _history_db = fetch_sent_records_from_sheet()
    st.session_state['_history_db'] = _history_db
    st.session_state['archived_wos'] = _archived_wos
    new_pool = []
    unique_tasks = {t['id']: t for t in all_tasks_raw}

    for t in unique_tasks.values():
        if str(t['id']).strip() in known_ids:
            continue

        # 🚫 DRIVER-HOME PSEUDO-TASK GUARD: Onfleet auto-generates
        # "Start at driver address" / "End at driver's address" tasks for native
        # Route Plans, bound to a contractor's home address. Real kiosk tasks
        # always carry a `state` custom field; the pseudo-tasks don't. Require it
        # so the pseudo-tasks never land in the dispatchable pool.
        _has_state_cf = any(
            (str(_f.get('name', '')).strip().lower() == 'state'
             or str(_f.get('key', '')).strip().lower() == 'state')
            and str(_f.get('value', '')).strip()
            for _f in (t.get('customFields') or [])
        )
        # 🌟 May 2026 — TX task rescue: previously dropped ANY task without a
        # state customField, silently kicking real TX tasks that came in via
        # manual Onfleet entry / alt integrations. Fall back to
        # destination.address.state — if the address has a US state the task
        # is bucketable, customField or not. The customField still wins as
        # authoritative dispatch tagging (normalize_state below reads addr).
        _addr_state_probe = ''
        try: _addr_state_probe = str((t.get('destination') or {}).get('address', {}).get('state', '') or '').strip()
        except Exception: _addr_state_probe = ''
        if not _has_state_cf and not _addr_state_probe:
            continue

        container = t.get('container', {})
        c_type = str(container.get('type', '')).upper()
        # 🛡️ DOUBLE-ROUTING GUARD — conditional version (May 18 2026 v3).
        # Matches process_pod's smart filter: hard-skip on container=WORKER,
        # also skip if worker ref exists AND fresh_sent_db confirms dispatch.
        # Let stale-worker-ref-but-no-sent_db-record tasks through (they're
        # the 170 unassigned tasks Nick was missing). The bucketing dedup
        # downstream catches any duplicates that slip in.
        _t_id_ss = str(t.get('id', '')).strip()
        if c_type == 'WORKER':
            continue
        if t.get('worker') and _t_id_ss in fresh_sent_db:
            _sb_stat_ss = str(fresh_sent_db[_t_id_ss].get('status', '')).lower()
            if _sb_stat_ss in ('sent', 'accepted', 'declined', 'finalized', 'field_nation'):
                continue
        # ORG-container ("Unassigned" in OnFleet UI) → include. TEAM-container
        # → include unless the team is in _excluded_team_set (FN team + any
        # explicitly-blacklisted teams like "ZZZ Test Team").
        # Matches the May 18 2026 deny-list change in process_pod.
        if c_type == 'TEAM' and container.get('team') in _excluded_team_set:
            continue

        addr = t.get('destination', {}).get('address', {})
        stt = normalize_state(addr.get('state', ''))
        if stt not in config['states']:
            continue

        is_esc = (c_type == 'TEAM' and container.get('team') in esc_team_ids)

        # Run classification engine
        native_details = str(t.get('taskDetails', '')).strip()
        custom_fields = t.get('customFields') or []
        custom_task_type = ""
        custom_boosted = ""
        tt_val = native_details
        venue_name = ""; venue_id = ""; kiosk_id = ""; sio = ""; client_company = ""; campaign_name = ""; location_in_venue = ""; customer_type = ""
        # 🎨 Pull art file token(s) from the OnFleet Task Details / notes field
        # (free-text). See extract_art_file() up top for the heuristic.
        art_file = extract_art_file(t.get('taskDetails', '') or t.get('notes', ''))

        for f in custom_fields:
            f_name = str(f.get('name', '')).strip().lower()
            f_key  = str(f.get('key', '')).strip().lower()
            f_val  = str(f.get('value', '')).strip()
            f_val_lower = f_val.lower()
            if f_name in ['task type', 'tasktype'] or f_key in ['tasktype', 'task_type']:
                custom_task_type = f_val_lower; tt_val = f_val
            if f_name in ['boosted standard', 'boostedstandard'] or f_key in ['boostedstandard', 'boosted_standard']:
                custom_boosted = f_val_lower
            if 'escalation' in f_name or 'escalation' in f_key:
                if f_val_lower in ['1', '1.0', 'true', 'yes'] or 'escalation' in f_val_lower:
                    is_esc = True
            if f_name in ['venuename', 'venue name'] or f_key in ['venuename', 'venue_name']:
                venue_name = f_val
            if f_name in ['venueid', 'venue id'] or f_key in ['venueid', 'venue_id']:
                venue_id = f_val
            # 🔢 SIO — Single-Item Order from OnFleet's `sio` custom field.
            # Locals carry a real numeric SIO; Defaults carry literal "Default".
            # Drives slip's SIO column + Locals grouping/sort. Captured at
            # ingest so it flows through to ghost slips (via stopData).
            if f_name == 'sio' or f_key == 'sio':
                sio = f_val
            if f_name in ['kioskid', 'kiosk id', 'kiosk_id'] or f_key in ['kioskid', 'kiosk_id']:
                kiosk_id = f_val
            if f_name in ['clientcompany', 'client company'] or f_key in ['clientcompany', 'client_company']:
                client_company = f_val
            if f_name in ['locationinvenue', 'location in venue'] or f_key in ['locationinvenue', 'location_in_venue']:
                location_in_venue = f_val
            if f_name in ['campaignname', 'campaign name'] or f_key in ['campaignname', 'campaign_name']:
                campaign_name = f_val  # 🌟 Captured separately so Client Company can't overwrite it
            # 🌟 Customer Type — drives National vs Regional/Local bucketing on the packing slip.
            if f_name in ['customer type', 'customertype'] or f_key in ['customertype', 'customer_type']:
                customer_type = f_val

        # 🌟 Campaign Name always wins over Client Company for FN Customer Name
        client_company = campaign_name or client_company

        search_string = f"{native_details} {custom_task_type}".lower()
        REGULAR_EXEMPTIONS = ["photo", "magnet", "continuity", "new ad", "pull down", "kiosk", "escalation"]
        is_exempt = any(ex in search_string for ex in REGULAR_EXEMPTIONS)
        DIGITAL_WHITELIST = ["service", "ins/rem", "offline", "site survey", "digital install"]
        is_digital_task = False
        if not is_exempt:
            if any(trigger in custom_task_type for trigger in DIGITAL_WHITELIST):
                is_digital_task = True
            elif "digital" in custom_boosted:
                is_digital_task = True

        t_status = fresh_sent_db.get(t['id'], {}).get('status', 'ready').lower() if t['id'] in fresh_sent_db else 'ready'
        t_wo = fresh_sent_db.get(t['id'], {}).get('wo', 'none') if t['id'] in fresh_sent_db else 'none'

        # Match process_pod's logic: a task is "removal" only if it's on the CVS Kiosk
        # Removal team AND its task type contains a removal keyword. Without this, CVS
        # removal tasks pulled in via Smart Sync would cluster with regular installs and
        # inherit the 20-stop limit instead of the 10-stop CVS limit.
        _remov_keywords = ["kiosk removal", "remove kiosk"]
        _is_cvs_team = (c_type == 'TEAM' and container.get('team') in cvs_remov_team_ids)
        _is_removal = _is_cvs_team and any(kw in f"{native_details} {custom_task_type}".lower() for kw in _remov_keywords)

        new_pool.append({
            "id": t['id'],
            "city": addr.get('city', 'Unknown'),
            "state": stt,
            "full": f"{addr.get('number','')} {addr.get('street','')}, {addr.get('city','')}, {stt}",
            "zip": addr.get('postalCode', ''),
            "lat": t['destination']['location'][1],
            "lon": t['destination']['location'][0],
            "escalated": is_esc,
            "task_type": tt_val,
            "is_digital": is_digital_task,
            "is_removal": _is_removal,
            "boosted_standard": custom_boosted,
            "db_status": t_status,
            "wo": t_wo,
            "venue_name": venue_name,
            "venue_id": venue_id,
            "kiosk_id": kiosk_id,
            "sio": sio,
            "client_company": client_company,
            "location_in_venue": location_in_venue,
            "art_file": art_file,
            "customer_type": customer_type,
            "is_new": True,  # 🌟 Flag for UI badge
        })

    if not new_pool:
        _bar.empty()
        st.toast("✅ No new tasks found.")
        return

    _tick(0.7, f"📦 Merging {len(new_pool)} new tasks...")

    CLUSTER_RADIUS = 20  # miles (May 18 2026 — kept in sync with route_radius)

    unmatched = []
    for new_task in new_pool:
        merged = False
        _new_is_digital = bool(new_task.get('is_digital', False))
        _new_is_removal = bool(new_task.get('is_removal', False))
        for cluster in existing_clusters:
            # Digital / CVS Removal / Standard never mix — mirror process_pod's Rule 1.
            # Without this guard, smart_sync merged new CVS removals and digital
            # tasks into whichever static cluster was within 20mi. (Jul 2026 — Nick.)
            if bool(cluster.get('is_digital', False)) != _new_is_digital: continue
            if bool(cluster.get('is_removal', False)) != _new_is_removal: continue
            dist = haversine(cluster['center'][0], cluster['center'][1], new_task['lat'], new_task['lon'])
            if dist <= CLUSTER_RADIUS:
                # Inherit cluster — append task
                cluster['data'].append(new_task)
                cluster['stops'] = len(set(x['full'] for x in cluster['data']))
                cluster['inst_count'] = sum(1 for x in cluster['data'] if "install" in str(x.get('task_type', '')).lower())
                cluster['remov_count'] = sum(1 for x in cluster['data'] if str(x.get('task_type', '')).lower() in ["kiosk removal", "remove kiosk"])
                cluster['esc_count'] = sum(1 for x in cluster['data'] if x.get('escalated'))
                merged = True
                break
        if not merged:
            unmatched.append(new_task)

    # Create new standalone clusters for unmatched tasks
    while unmatched:
        anc = unmatched.pop(0)
        _anc_is_digital = bool(anc.get('is_digital', False))
        _anc_is_removal = bool(anc.get('is_removal', False))
        group = [anc]
        remaining = []
        for t in unmatched:
            # Same type-mixing guard as the existing-cluster merge above.
            if bool(t.get('is_digital', False)) != _anc_is_digital:
                remaining.append(t)
                continue
            if bool(t.get('is_removal', False)) != _anc_is_removal:
                remaining.append(t)
                continue
            if haversine(anc['lat'], anc['lon'], t['lat'], t['lon']) <= CLUSTER_RADIUS:
                group.append(t)
            else:
                remaining.append(t)
        unmatched = remaining

        # Any-match boosted-tier (see process_pod for rationale).
        _ss_boosted_vals = [str(x.get('boosted_standard', '')).lower() for x in group if x.get('boosted_standard')]
        if any('local plus' in v for v in _ss_boosted_vals):
            _ss_boosted_tag = 'local plus'
        elif any('boosted' in v for v in _ss_boosted_vals):
            _ss_boosted_tag = 'boosted'
        else:
            _ss_boosted_tag = ''
        existing_clusters.append({
            "data": group,
            "center": [anc['lat'], anc['lon']],
            "stops": len(set(x['full'] for x in group)),
            "city": anc['city'], "state": anc['state'],
            "status": "Ready",
            "has_ic": False,
            "esc_count": sum(1 for x in group if x.get('escalated')),
            "is_digital": anc.get('is_digital', False),
            "is_removal": anc.get('is_removal', False),
            "boosted_tag": _ss_boosted_tag,
            "inst_count": sum(1 for x in group if "install" in str(x.get('task_type', '')).lower()),
            "remov_count": sum(1 for x in group if str(x.get('task_type', '')).lower() in ["kiosk removal", "remove kiosk"]),
            "wo": anc['wo']
        })

    st.session_state[f"clusters_{pod_name}"] = existing_clusters
    # Re-apply bundles in case Smart Sync introduced a new cluster that should have
    # been absorbed into an existing bundled route (or rebuilt sources of past bundles).
    _replay_bundles(pod_name)
    st.session_state['_worker_counts'] = fetch_worker_task_counts()
    _bar.empty()
    # Break down the merged tasks by bucket so the dispatcher knows WHERE the
    # new tasks landed — many of these are in Sent/Accepted (auto-fetched by
    # sync, not "new Ready work"). The bare "X merged" count was confusing:
    # dispatchers expected X new Ready routes but most were already-dispatched
    # tasks the sync caught up on. (May 18 2026.)
    if new_pool:
        _ready_n = 0
        _sent_n = 0
        for _t in new_pool:
            _rec = fresh_sent_db.get(str(_t['id']).strip())
            if _rec and str(_rec.get('status', '')).lower() in ('sent', 'accepted', 'declined', 'finalized'):
                _sent_n += 1
            else:
                _ready_n += 1
        _parts = []
        if _ready_n: _parts.append(f"{_ready_n} new Ready")
        if _sent_n:  _parts.append(f"{_sent_n} already dispatched")
        _label = ' + '.join(_parts) or f"{len(new_pool)} task(s)"
        st.toast(f"✅ {_label} synced to {pod_name} Pod.")
    else:
        st.toast(f"✅ {pod_name} Pod already in sync — no new tasks.")


def make_venue_details(data):
    """Build expandable venue location rows from cluster task data.

    Per-stop header pills (only shown when count > 0):
      🛠️ {N} Kiosk     — install count (green)
      🔧 {N} Ins/Rem   — digital ins/rem count (teal)
      🔥 {N}           — boosted standard task count (red)
      ⭐ {N}              — local plus task count (amber)
      ❗ {N}              — escalation count (red)
      {N} Tasks              — total task count (purple pill)

    Each campaign row: • {campaign}  {task-type badge}  {markers}
    Task-type badge is 🛠️ Install / 🔄 Continuity / ⚪ Default / 🗑️ Removal /
    🆕 New Ad / 🔧 Ins/Rem / 📵 Offline / ⚙️ Service / 📋 Custom.
    Markers are ❗ escalation, 🔥 boosted, ⭐ local plus.
    """
    u_locs = []
    for t in data:
        if t['full'] not in u_locs: u_locs.append(t['full'])
    rows = []
    for loc in u_locs:
        loc_tasks = [t for t in data if t['full'] == loc]
        venue = next((t.get('venue_name','') for t in loc_tasks if t.get('venue_name')), '')

        # Per-stop metrics — same buckets the dispatch view computes.
        n_ad = c_ad = d_ad = inst = remov = digi_ins = digi_off = digi_srv = 0
        boost_cnt = lplus_cnt = 0
        custom_types = {}
        for t in loc_tasks:
            tt = str(t.get('task_type','')).lower()
            if t.get('is_digital'):
                if 'offline' in tt: digi_off += 1
                elif 'ins/re' in tt: digi_ins += 1
                else: digi_srv += 1
            elif 'install' in tt: inst += 1
            elif any(x in tt for x in ['kiosk removal','remove kiosk']): remov += 1
            elif any(x in tt for x in ['continuity','photo retake','swap']): c_ad += 1
            elif any(x in tt for x in ['default','pull down']): d_ad += 1
            elif any(x in tt for x in ['new ad','art change','top']) or not tt: n_ad += 1
            else:
                _label = tt.title() or 'Other'
                custom_types[_label] = custom_types.get(_label, 0) + 1
            # Boosted Standard tier: 'local plus' is its own subtype, otherwise any
            # 'boosted'-containing value counts as boosted.
            _bs = str(t.get('boosted_standard','')).lower()
            if 'local plus' in _bs: lplus_cnt += 1
            elif 'boosted' in _bs: boost_cnt += 1
        t_count = len(loc_tasks)
        esc_cnt = sum(1 for t in loc_tasks if t.get('escalated'))

        # Header pills — match dispatch view layout, plus boost/local-plus counts.
        k_tag       = f" <span style='color:#16a34a;font-weight:800;font-size:10px;'>🛠️ {inst} Kiosk</span>" if inst > 0 else ""
        digi_ins_tag= f" <span style='color:#0f766e;font-weight:800;font-size:10px;'>🔧 {digi_ins} Ins/Rem</span>" if digi_ins > 0 else ""
        boost_tag   = f" <span style='color:#dc2626;font-weight:800;font-size:10px;'>🔥 {boost_cnt}</span>" if boost_cnt > 0 else ""
        lplus_tag   = f" <span style='color:#ca8a04;font-weight:800;font-size:10px;'>⭐ {lplus_cnt}</span>" if lplus_cnt > 0 else ""
        esc_tag     = f" <span style='color:#dc2626;font-weight:900;font-size:10px;'>❗ {esc_cnt}</span>" if esc_cnt > 0 else ""
        t_pill      = f" <span style='color:#633094;background:#f3e8ff;padding:1px 5px;border-radius:8px;font-weight:800;font-size:10px;'>{t_count} Tasks</span>" if t_count else ""

        # Task-type icon prefix — mirrors render_dispatch's inline summary so
        # Sent / Accepted / Declined / Finalized / FN cards show the SAME
        # at-a-glance breakdown as Ready / Flagged. Without these icons, the
        # FN/Accepted/Finalized stop summary was missing the 🆕 / 🔄 / ⚪ /
        # 🗑️ N / 📋 / 📵 / ⚙️ indicators that dispatchers rely on to scan a
        # card without expanding each stop. (May 17 2026 — Nick: "for each
        # Field Nation card it needs to show the full location list with the
        # entire campaign name breakdown including the emojis ... the data
        # added back that was originally in Ready/Flagged.")
        _icon_parts = []
        if n_ad     > 0: _icon_parts.append("🆕")
        if c_ad     > 0: _icon_parts.append("🔄")
        if d_ad     > 0: _icon_parts.append("⚪")
        if remov    > 0: _icon_parts.append(f"🗑️ {remov}")
        if custom_types: _icon_parts.append("📋")
        if digi_off > 0: _icon_parts.append("📵")
        if digi_srv > 0: _icon_parts.append("⚙️")
        _icon_html  = f" <span style='font-size:13px;margin-left:6px;'>{' '.join(_icon_parts)}</span>" if _icon_parts else ""

        venue_prefix = f"<span style='color:#94a3b8;font-size:11px;font-weight:600;'>{esc(venue)} — </span>" if venue else ""

        # Campaign expansion: aggregate by (campaign, task type) so multiple tasks for
        # the same client+type at one stop collapse into one row with COUNTS — and the
        # marker pills (escalation, boosted, local plus) carry their per-group counts
        # too, mirroring the header style.
        from collections import defaultdict
        camp_groups = defaultdict(lambda: {'count': 0, 'esc': 0, 'boost': 0, 'lplus': 0, 'tt_badge': '', 'cmp': ''})
        for t in loc_tasks:
            # 🌟 May 2026 — Blank client_company / campaign falls back to the
            # task type text so the accordion row still shows meaningful info.
            cmp = (t.get('client_company','') or '').strip()
            tt = str(t.get('task_type','')).lower()
            if not cmp:
                cmp = str(t.get('task_type','') or '').strip() or 'Unknown'
            if t.get('is_digital'):
                if 'offline' in tt: tt_badge = "📵 Offline"
                elif 'ins/re' in tt: tt_badge = "🔧 Ins/Rem"
                elif 'site survey' in tt: tt_badge = "🔍 Site Survey"
                else: tt_badge = "⚙️ Service"
            elif 'install' in tt: tt_badge = "🛠️ Install"
            elif any(x in tt for x in ['kiosk removal','remove kiosk']): tt_badge = "🗑️ Removal"
            elif any(x in tt for x in ['continuity','photo retake','swap']): tt_badge = "🔄 Continuity"
            elif any(x in tt for x in ['default','pull down']): tt_badge = "⚪ Default"
            elif any(x in tt for x in ['new ad','art change','top']) or not tt: tt_badge = "🆕 New Ad"
            else: tt_badge = f"📋 {tt.title()}"
            key = (cmp, tt_badge)
            grp = camp_groups[key]
            grp['cmp'] = cmp
            grp['tt_badge'] = tt_badge
            grp['count'] += 1
            if t.get('escalated'): grp['esc'] += 1
            bs = str(t.get('boosted_standard','')).lower()
            if 'local plus' in bs: grp['lplus'] += 1
            elif 'boosted' in bs: grp['boost'] += 1

        camp_rows = []
        for (cmp, tt_badge), grp in camp_groups.items():
            cnt = grp['count']
            count_suffix = f" <span style='color:#94a3b8;font-weight:600;'>× {cnt}</span>" if cnt > 1 else ""
            esc_pill   = f" <span style='color:#dc2626;font-weight:800;'>❗ {grp['esc']}</span>" if grp['esc'] > 0 else ""
            boost_pill = f" <span style='color:#dc2626;font-weight:800;'>🔥 {grp['boost']}</span>" if grp['boost'] > 0 else ""
            lplus_pill = f" <span style='color:#ca8a04;font-weight:800;'>⭐ {grp['lplus']}</span>" if grp['lplus'] > 0 else ""
            row = (
                f"<div style='font-size:11px;color:#475569;padding:2px 4px;margin-top:3px;'>"
                f"• <span style='color:#0f172a;font-weight:600;'>{esc(cmp)}</span>"
                f"&nbsp;<span style='font-weight:700;color:#0f172a;'>{esc(tt_badge)}</span>"
                f"{count_suffix}"
                f"{esc_pill}{boost_pill}{lplus_pill}"
                f"</div>"
            )
            camp_rows.append(row)
        # 🌟 FN-empty-accordion fix (May 2026):
        # When a cluster's tasks don't carry client_company (typical for FN
        # ghost clusters reconstructed from pre-2026-05-04 sheet rows whose
        # stop_data predates the `campaigns` field), camp_rows stays empty and
        # the <details> body renders blank — the accordion technically opens
        # but the dispatcher sees nothing change. Fall back to a task-type
        # tally so clicking a location ALWAYS reveals useful detail.
        if camp_rows:
            camp_block = f"<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;'>{''.join(camp_rows)}</div>"
        else:
            _tt_lines = []
            if inst:     _tt_lines.append(f"🛠️ Install &times; {inst}")
            if remov:    _tt_lines.append(f"🗑️ Removal &times; {remov}")
            if n_ad:     _tt_lines.append(f"🆕 New Ad &times; {n_ad}")
            if c_ad:     _tt_lines.append(f"🔄 Continuity &times; {c_ad}")
            if d_ad:     _tt_lines.append(f"⚪ Default &times; {d_ad}")
            if digi_ins: _tt_lines.append(f"🔧 Ins/Rem &times; {digi_ins}")
            if digi_off: _tt_lines.append(f"📵 Offline &times; {digi_off}")
            if digi_srv: _tt_lines.append(f"⚙️ Service &times; {digi_srv}")
            for _lbl, _n in custom_types.items():
                _tt_lines.append(f"📋 {_lbl} &times; {_n}")
            if _tt_lines:
                _body = "".join(
                    f"<div style='font-size:11px;color:#475569;padding:2px 4px;margin-top:3px;'>• <span style='font-weight:700;color:#0f172a;'>{_ln}</span></div>"
                    for _ln in _tt_lines
                )
                camp_block = f"<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;'>{_body}</div>"
            else:
                camp_block = "<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;font-size:10px;color:#94a3b8;'>No detail available for this stop.</div>"

        rows.append(
            f"<details class='fn-loc-row'>"
            f"<summary class='fn-loc-summary'>"
            f"<span class='fn-chevron'>›</span>"
            f"{venue_prefix}<span style='font-weight:700;color:#0f172a;'>{esc(loc)}</span>"
            f"{k_tag}{digi_ins_tag}{boost_tag}{lplus_tag}{esc_tag} &nbsp;{t_pill}{_icon_html}"
            f"</summary>{camp_block}</details>"
        )
    return "".join(rows)

def make_venue_details_ghost(locs_list, stop_data=None):
    """Expandable accordion rows for ghost routes. Uses rich stop_data if available.

    May 16 2026 — upgraded to mirror make_venue_details' per-stop header pills
    and campaign-row task-type badges so FN routes in Accepted/Declined/
    Finalized show the SAME breakdown dispatchers see on Ready/Flagged. The
    underlying stop_data captures campaigns + per-task-type counts at save
    time; we just have to render them out here.
    """
    sd_map = {}
    if stop_data:
        for sd in stop_data:
            sd_map[sd.get('addr', '')] = sd

    rows = []
    for loc in locs_list:
        if " — " in loc:
            parts = loc.split(" — ", 1)
            venue_prefix = f"<span style='color:#94a3b8;font-size:11px;font-weight:600;'>{esc(parts[0])} — </span>"
            addr = parts[1]
        else:
            venue_prefix = ""
            addr = loc

        sd = sd_map.get(addr) or sd_map.get(loc) or {}
        venue = sd.get('venue', '')
        if venue and not venue_prefix:
            venue_prefix = f"<span style='color:#94a3b8;font-size:11px;font-weight:600;'>{esc(venue)} — </span>"

        # Per-stop counts (defensive — older sheet rows may be missing keys).
        _inst    = int(sd.get('inst', 0) or 0)
        _remov   = int(sd.get('remov', 0) or 0)
        _n_ad    = int(sd.get('n_ad', 0) or 0)
        _c_ad    = int(sd.get('c_ad', 0) or 0)
        _d_ad    = int(sd.get('d_ad', 0) or 0)
        _t_count = int(sd.get('t_count', 0) or 0) or (_inst + _remov + _n_ad + _c_ad + _d_ad)
        _stop_bs = str(sd.get('boostedStandard', '') or '').lower()
        _boost   = 1 if ('boosted' in _stop_bs and 'local plus' not in _stop_bs) else 0
        _lplus   = 1 if ('local plus' in _stop_bs) else 0
        _esc     = 1 if sd.get('esc') else 0

        # Header pills — match the live version's layout.
        k_tag        = f" <span style='color:#16a34a;font-weight:800;font-size:10px;'>🛠️ {_inst} Kiosk</span>" if _inst > 0 else ""
        remov_tag    = f" <span style='color:#9333ea;font-weight:800;font-size:10px;'>🗑️ {_remov}</span>" if _remov > 0 else ""
        boost_tag    = f" <span style='color:#dc2626;font-weight:800;font-size:10px;'>🔥</span>" if _boost else ""
        lplus_tag    = f" <span style='color:#ca8a04;font-weight:800;font-size:10px;'>⭐</span>" if _lplus else ""
        esc_tag      = f" <span style='color:#dc2626;font-weight:900;font-size:10px;'>❗</span>" if _esc else ""
        t_pill       = f" <span style='color:#633094;background:#f3e8ff;padding:1px 5px;border-radius:8px;font-weight:800;font-size:10px;'>{_t_count} Tasks</span>" if _t_count else ""

        # Campaign rows: for each campaign at this stop, render a row with the
        # best available task-type badge. Since stop_data doesn't pair campaigns
        # with task types 1:1, we fall back to the stop-level dominant type.
        def _stop_tt_badge():
            if _n_ad: return "🆕 New Ad"
            if _c_ad: return "🔄 Continuity"
            if _d_ad: return "⚪ Default"
            if _inst: return "🛠️ Install"
            if _remov: return "🗑️ Removal"
            return ""

        camps = sd.get('campaigns', []) or []
        camp_rows = []
        seen = set()
        _default_badge = _stop_tt_badge()
        for cp in camps:
            cname = (cp.get('name', '') or '').strip()
            if not cname: continue
            tt_badge = _default_badge
            badges = ""
            if cp.get('esc'): badges += " <span style='color:#dc2626;font-weight:800;'>❗</span>"
            bs = (cp.get('bs', '') or '').lower()
            if 'local plus' in bs: badges += " <span style='color:#ca8a04;font-weight:800;'>⭐</span>"
            elif 'boosted' in bs: badges += " <span style='color:#dc2626;font-weight:800;'>🔥</span>"
            tt_html = f"&nbsp;<span style='font-weight:700;color:#0f172a;'>{tt_badge}</span>" if tt_badge else ""
            row = (
                f"<div style='font-size:11px;color:#475569;padding:2px 4px;margin-top:3px;'>"
                f"• <span style='color:#0f172a;font-weight:600;'>{esc(cname)}</span>"
                f"{tt_html}{badges}"
                f"</div>"
            )
            if row not in seen:
                seen.add(row)
                camp_rows.append(row)

        # When no campaigns survive (legacy FN rows w/o stopData campaigns),
        # fall back to a task-type tally — guarantees the accordion ALWAYS
        # reveals useful detail.
        if not camp_rows:
            tt_lines = []
            if _inst:  tt_lines.append(f"🛠️ Install &times; {_inst}")
            if _remov: tt_lines.append(f"🗑️ Removal &times; {_remov}")
            if _n_ad:  tt_lines.append(f"🆕 New Ad &times; {_n_ad}")
            if _c_ad:  tt_lines.append(f"🔄 Continuity &times; {_c_ad}")
            if _d_ad:  tt_lines.append(f"⚪ Default &times; {_d_ad}")
            if tt_lines:
                _body = "".join(
                    f"<div style='font-size:11px;color:#475569;padding:2px 4px;margin-top:3px;'>• <span style='font-weight:700;color:#0f172a;'>{_ln}</span></div>"
                    for _ln in tt_lines
                )
                camp_block = f"<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;'>{_body}</div>"
            else:
                camp_block = "<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;font-size:10px;color:#94a3b8;'>No detail available for this stop.</div>"
        else:
            camp_block = f"<div style='padding:6px 8px;background:#f8fafc;border-radius:6px;margin-top:4px;'>{''.join(camp_rows)}</div>"

        rows.append(
            f"<details class='fn-loc-row'>"
            f"<summary class='fn-loc-summary'>"
            f"<span class='fn-chevron'>›</span>"
            f"{venue_prefix}<span style='font-weight:700;color:#0f172a;'>{esc(addr)}</span>"
            f"{k_tag}{remov_tag}{boost_tag}{lplus_tag}{esc_tag} &nbsp;{t_pill}"
            f"</summary>{camp_block}</details>"
        )
    return "".join(rows)

VENUE_SECTION_CSS = """<style>
.fn-loc-row{border-bottom:1px solid #f1f5f9;}
.fn-loc-row:last-child{border-bottom:none;}
.fn-loc-summary{display:flex;align-items:flex-start;justify-content:flex-start;gap:6px;padding:7px 4px;font-size:12px;cursor:pointer;border-radius:6px;list-style:none;user-select:none;transition:background 0.15s ease;flex-wrap:wrap;}
.fn-loc-summary::-webkit-details-marker{display:none;}
.fn-loc-summary::marker{display:none;}
.fn-loc-summary:hover{background:#f8fafc;}
.fn-chevron{font-size:13px;color:#94a3b8;font-weight:300;transition:transform 0.2s ease;flex-shrink:0;margin-right:4px;}
details[open] .fn-chevron{transform:rotate(90deg);}
</style>"""

def venue_section(inner_html):
    """Wrap venue rows in the standard section container."""
    return f'{VENUE_SECTION_CSS}<div style="border-top:1px solid #e2e8f0;padding:6px 12px 8px 12px;"><div style="font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px;">Venue Locations</div>{inner_html}</div>'

def run_pod_tab(pod_name):

    # 🛡️ DCC error shield — hide raw Streamlit framework error UI (SessionInfo
    # evictions, fragment-id mismatches, setIn index drift, Python exception
    # panels) from dispatchers WITHOUT triggering a refresh. These errors fire
    # in Streamlit's WebSocket message-routing layer under contention (multi-
    # tab admin views, post-deploy stale tabs) and almost always self-heal in
    # a rerun cycle. Hiding the visual panel keeps the dispatcher in a clean
    # view while Streamlit's own retry resolves it transparently. Does NOT
    # hide info/success/warning alerts or toasts — only the red error UIs.
    # DIAGNOSTIC (temporary — Jul 2026): un-hide Streamlit's exception panels
    # so we can see what's crashing the render on bundled routes. Restore the
    # `display:none` line above once the root cause is identified and fixed.
    st.markdown(
        '<style>[data-testid="stException"]{display:block !important;}</style>',
        unsafe_allow_html=True,
    )
    _components.html(
        """
        <script>
        (function () {
          if (parent._dccErrorShieldInstalled) return;
          parent._dccErrorShieldInstalled = true;
          var ERR_STRINGS = [
            'Bad message format',
            'Tried to use SessionInfo before it was initialized',
            'Could not find fragment id',
            'Bad setIn index',
            'RerunData'
          ];
          function hideIfErrorText(node) {
            if (!node || node.nodeType !== 1) return;
            var text = node.innerText || '';
            for (var i = 0; i < ERR_STRINGS.length; i++) {
              if (text.indexOf(ERR_STRINGS[i]) !== -1) {
                node.style.display = 'none';
                return;
              }
            }
          }
          try {
            var pdoc = parent.document;
            pdoc.querySelectorAll('[role="alert"], [data-baseweb="modal"], [data-baseweb="toast"]').forEach(hideIfErrorText);
            var obs = new parent.MutationObserver(function (muts) {
              muts.forEach(function (m) {
                m.addedNodes.forEach(function (n) {
                  if (n.nodeType !== 1) return;
                  hideIfErrorText(n);
                  if (n.querySelectorAll) n.querySelectorAll('*').forEach(hideIfErrorText);
                });
              });
            });
            obs.observe(pdoc.body, { childList: true, subtree: true });
          } catch (e) { /* swallow — shield is best-effort */ }
        })();
        </script>
        """,
        height=0,
    )

    # Security audit M7 - surface any background archive-write failures so a
    # route that looks re-routed but never archived is not silently lost.
    try:
        if _RECONCILE_FAILURES:
            _failed_n = len(_RECONCILE_FAILURES)
            st.warning(
                f"\u26a0\ufe0f {_failed_n} re-route(s) could not be archived in the "
                f"backend after retries. Re-run the re-route, or check Railway logs."
            )
            _RECONCILE_FAILURES.clear()
    except Exception as _rf_e:
        _log_err("reconcile_failure_banner", _rf_e)

    # Show toast only if a route in THIS pod changed
    sent_db = st.session_state.get('sent_db', {})
    pod_clusters = st.session_state.get(f"clusters_{pod_name}", [])
    pod_task_ids = set()
    for c in pod_clusters:
        for t in c.get('data', []):
            pod_task_ids.add(str(t['id']).strip())




    # Security audit H9 - the JS sync poller that used to live here was dead
    # code: clickPulse() was defined but never called and the hidden
    # _sync_pulse_hidden button was never clicked, so it reflected nothing.
    # It has been removed (along with the hidden button and stale docstring)
    # so a future maintainer cannot 'fix it back' and reintroduce the
    # run-every starvation the team deliberately removed. accept/decline
    # surfaces on the next user interaction or Check New Tasks click.
    # Security audit M13 - run_pod_tab() runs once PER POD for admin/manager
    # (5x). auto_sync_checker patches the app-wide sent_db, so running it 5x
    # per render just multiplies sheet I/O. Gate it to the first pod of the
    # render; the per-render flag is reset once per script run near the tabs.
    if not st.session_state.get('_asc_ran_this_render'):
        st.session_state['_asc_ran_this_render'] = True
        auto_sync_checker(pod_name)  # patches sent_db in-memory on each rerun.

    # 🔗 Hydrate persisted bundle map for this pod (once per session per pod)
    # so dispatcher bundles survive page reloads / redeploy reloads. Gated by
    # a session flag inside the helper so subsequent renders are free.
    _hydrate_bundle_map_once(pod_name)

    # Grab the contractor database from session state
    ic_df = st.session_state.get('ic_df', pd.DataFrame())
    
    # Grab the matching "Midnight" text color for the current pod
    text_color = {
        "Blue": "#2563eb", "Green": "#16a34a", "Orange": "#ea580c",
        "Purple": "#9333ea", "Red": "#dc2626"
    }.get(pod_name, "#633094")
    
    # Check if data exists for this pod to determine button state
    is_initialized = f"clusters_{pod_name}" in st.session_state
    
    # 🌟 HEADER ROW: Title Centered, Dynamic Button Top Right
    h_col1, h_col2, h_col3 = st.columns([2, 6, 2])
    with h_col2:
        st.markdown(f"<h2 style='color: {text_color}; text-align:center; margin-top: 0;'>{pod_name} Pod Dashboard</h2>", unsafe_allow_html=True)
    with h_col3:
        st.markdown("<div class='tab-action-btn'>", unsafe_allow_html=True)
        if not is_initialized:
            # STATE 1: Not loaded yet
            init_clicked = st.button(f"🚀 Initialize Data", key=f"init_{pod_name}", use_container_width=True)
            sync_clicked = False
        else:
            # STATE 2: Loaded — smart sync for new tasks only
            init_clicked = False
            sync_clicked = st.button("🔄 Check New Tasks", key=f"reopt_{pod_name}", use_container_width=True)
            # 🌟 Opt #9 — last-sync indicator below the Sync button.
            render_sync_age()
        st.markdown("</div>", unsafe_allow_html=True)

    # 🛡️ SESSION-RESET SELF-HEAL
    # clusters_{pod} lives only in volatile st.session_state. When the
    # Streamlit session resets (WebSocket reconnect under load, redeploy,
    # memory eviction) the ?auth= URL token rehydrates the login — but the
    # pod's initialized clusters are NOT rehydrated, stranding the dispatcher
    # on the "Initialize Data" button mid-shift. Observed live: Red Pod
    # dropped clusters_Red after a dispatch under stress.
    # Fix: if clusters are missing and the dispatcher didn't click anything,
    # auto-fire the init flow. The GAS OnFleet pull is cached (sub-1s) so the
    # pod self-heals in seconds. The 2-attempt guard stops an infinite loop
    # if init genuinely fails — after that the dispatcher gets the manual
    # Initialize button as a fallback. Side benefit: pod-locked dispatchers
    # now auto-init on landing and never see the Initialize button at all.
    # 🔄 POST-REVOKE AUTO-RESYNC TRIGGER (May 18 2026).
    # When move_to_dispatch fires (Re-route button), it sets
    # _post_revoke_resync_pending_{pod}=True. The bg thread needs ~10s to
    # unassign the OnFleet tasks back to state=0. We inject a one-shot JS
    # snippet here that waits 12s after this rerun then clicks Check New
    # Tasks, which forces smart_sync_pod to fetch fresh OnFleet data and
    # surface the re-routed cluster in Ready. Fires once per re-route action
    # (flag is consumed below).
    _post_revoke_key = f"_post_revoke_resync_pending_{pod_name}"
    if st.session_state.pop(_post_revoke_key, False):
        # Security audit M10 - share a sessionStorage mutex with the
        # auto-sync JS. Both timers click the same reopt_{pod} button;
        # smart_sync_pod is not re-entrant, so a recent click suppresses
        # this one (and vice-versa) to avoid overlapping syncs.
        _components.html(
            f"""
            <script>
            (function() {{
              parent.setTimeout(function() {{
                try {{
                  var LK = '_dccSyncClickLock_{pod_name}';
                  var now = Date.now();
                  var last = parseInt(parent.sessionStorage.getItem(LK) || '0', 10);
                  if (now - last < 15000) return;  // another timer just clicked
                  var btn = parent.document.querySelector(
                    '[class*="st-key-reopt_{pod_name}"] button'
                  );
                  if (btn) {{
                    parent.sessionStorage.setItem(LK, String(now));
                    btn.click();
                  }}
                }} catch (_) {{}}
              }}, 12000);
            }})();
            </script>
            """,
            height=0,
        )

    _auto_init_key = f"_auto_init_attempts_{pod_name}"
    if is_initialized:
        st.session_state.pop(_auto_init_key, None)
    elif not init_clicked:
        _ai_attempts = st.session_state.get(_auto_init_key, 0)
        if _ai_attempts < 2:
            st.session_state[_auto_init_key] = _ai_attempts + 1
            init_clicked = True

    # 🛡️ AUTO-SYNC ON FIRST POD OPEN — DEFERRED VIA JS (May 18 2026 v2)
    # The earlier version of this block (sync_clicked=True synchronously) caused
    # a brutal UX flow: page sign-in → 60s blank → faded "Initialize Data" →
    # premature "No new tasks found" toast → eventually dashboard renders →
    # contradicting "63 new tasks merged" toast → page sometimes evaporates.
    # Root cause: forcing sync_clicked=True before any dashboard content has
    # been rendered means smart_sync_pod runs DURING the user's first paint,
    # blocking everything else.
    # New approach: render the dashboard normally on first load, then a tiny
    # JS snippet (injected near the end of run_pod_tab) clicks the "Check New
    # Tasks" button 8s after the dashboard is visible. The toast that fires
    # then arrives AFTER the user can see the real state, so its count is
    # actually meaningful instead of contradicting an earlier fire-too-early
    # message. Uses sessionStorage so it only fires once per browser session
    # per pod — manual Check New Tasks clicks ALSO set the flag, so the
    # auto-trigger never double-fires on top of a user click.
    # (The python-side _auto_sync_done_{pod} flag below is preserved as a
    # cheap idempotency guard so any leftover edge-case can't double-fire.)
    _auto_sync_key = f"_auto_sync_done_{pod_name}"
    if False:  # auto Check New Tasks on page load disabled — dispatcher clicks manually
        # Mark fired immediately so a Streamlit rerun cycle can't queue two
        # JS-clicks. The actual click is deferred to the JS snippet below.
        st.session_state[_auto_sync_key] = True
        _components.html(
            f"""
            <script>
            (function() {{
              var KEY = "_dccAutoSyncDone_{pod_name}";
              try {{
                if (parent.sessionStorage.getItem(KEY) === "1") return;
              }} catch(_) {{}}
              // Wait 8s so the dispatcher sees the dashboard's real state FIRST.
              // Then click the "Check New Tasks" button to fire smart_sync_pod.
              // The toast that follows will reflect what actually changed
              // (instead of arriving before the dashboard renders and saying
              // "No new tasks" against an empty in-memory cluster set).
              parent.setTimeout(function() {{
                try {{
                  // Look for the "Check New Tasks" button by its pod-scoped key.
                  // Key pattern: st-key-reopt_{pod_name}
                  // Security audit M10 - respect the shared sync-click mutex
                  // so this auto-sync click and the post-revoke click never
                  // overlap (smart_sync_pod is not re-entrant).
                  var LK = '_dccSyncClickLock_{pod_name}';
                  var now = Date.now();
                  var last = parseInt(parent.sessionStorage.getItem(LK) || '0', 10);
                  if (now - last < 15000) {{ try {{ parent.sessionStorage.setItem(KEY, "1"); }} catch(_) {{}} return; }}
                  var btn = parent.document.querySelector(
                    '[class*="st-key-reopt_{pod_name}"] button'
                  );
                  if (btn) {{
                    parent.sessionStorage.setItem(LK, String(now));
                    btn.click();
                    try {{ parent.sessionStorage.setItem(KEY, "1"); }} catch(_) {{}}
                  }}
                }} catch(_) {{ /* swallow — next session can retry */ }}
              }}, 8000);
            }})();
            </script>
            """,
            height=0,
        )

    # 🌟 Check New Tasks: same full-width overlay as Initialize so the dispatcher
    # sees the spin-card + timer instead of a bare progress bar while smart_sync_pod runs.
    if is_initialized and sync_clicked:
        # 🌟 Opt #9 — manual Check New Tasks gets immediate ts feedback.
        st.session_state['_last_sync_ts'] = datetime.now()
        import time as _time
        _start = _time.time()

        def _render_sync_card(overlay, pod, start):
            elapsed = int(_time.time() - start)
            m = elapsed // 60
            s = elapsed % 60
            overlay.markdown(f"""
                <style>
                    @keyframes spin {{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
                    .dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;
                        padding:36px 32px;text-align:center;margin:20px 0;}}
                    .dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;
                        border-top:4px solid #633094;border-radius:50%;
                        animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
                    .dcc-pill{{display:inline-block;font-size:13px;font-weight:700;
                        color:#633094;background:#f3e8ff;border-radius:20px;
                        padding:4px 14px;margin-top:12px;}}
                </style>
                <div class='dcc-card'>
                    <div class='dcc-spin'></div>
                    <p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Checking New Tasks — {pod} Pod</p>
                    <p style='font-size:13px;color:#64748b;margin:0 0 8px 0;'>Scanning Onfleet for tasks not yet tracked...</p>
                    <div class='dcc-pill'>⏱ {m}:{s:02d}</div>
                <div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: 2–4 min for full pod fleet (Red Pod is heaviest).</div>
                </div>
            """, unsafe_allow_html=True)

        sync_overlay = st.empty()
        _render_sync_card(sync_overlay, pod_name, _start)

        # Stash so smart_sync_pod could tick the timer if we wire it up later.
        st.session_state['_loading_overlay'] = sync_overlay
        st.session_state['_loading_start'] = _start
        st.session_state['_loading_pod'] = pod_name

        smart_sync_pod(pod_name)

        sync_overlay.empty()
        st.session_state.pop('_loading_overlay', None)
        st.session_state.pop('_loading_start', None)
        st.session_state.pop('_loading_pod', None)
        # Seed the auto_sync_checker cooldown so the next 90s of polls don't
        # fire an app-scope rerun that yanks the page out from under us.
        st.session_state['_auto_sync_last_app_rerun_ts'] = datetime.now()
        # st.rerun() RESTORED (May 18 2026 v2 — BUG-3 fix). Without it, the
        # "Check New Tasks" button and supercards were rendered earlier in the
        # run with the PRE-sync state and never refreshed — dispatchers saw a
        # stale header contradicting a freshly-populated dashboard. Sync
        # completion is a discrete event; a rerun here is correct. (The reruns
        # we permanently killed are the IDLE auto_sync_checker polls, not
        # user/flow-triggered ones like this.)
        st.rerun()

    # 🌟 FULL-WIDTH LOADING UI — outside columns so bar spans the page
    if not is_initialized and init_clicked:
        import time as _time
        _start = _time.time()

        def _render_card(overlay, pod, start):
            elapsed = int(_time.time() - start)
            m = elapsed // 60
            s = elapsed % 60
            overlay.markdown(f"""
                <style>
                    @keyframes spin {{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
                    .dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;
                        padding:36px 32px;text-align:center;margin:20px 0;}}
                    .dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;
                        border-top:4px solid #633094;border-radius:50%;
                        animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
                    .dcc-pill{{display:inline-block;font-size:13px;font-weight:700;
                        color:#633094;background:#f3e8ff;border-radius:20px;
                        padding:4px 14px;margin-top:12px;}}
                </style>
                <div class='dcc-card'>
                    <div class='dcc-spin'></div>
                    <p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Initializing {pod} Pod</p>
                    <p style='font-size:13px;color:#64748b;margin:0 0 8px 0;'>Fetching tasks from Onfleet and building routes...</p>
                    <div class='dcc-pill'>⏱ {m}:{s:02d}</div>
                <div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: 2–4 min for full pod fleet (Red Pod is heaviest).</div>
                </div>
            """, unsafe_allow_html=True)

        loading_overlay = st.empty()
        _render_card(loading_overlay, pod_name, _start)

        # Store start time and overlay in session state so process_pod can tick it
        st.session_state['_loading_overlay'] = loading_overlay
        st.session_state['_loading_start'] = _start
        st.session_state['_loading_pod'] = pod_name

        _bar = st.progress(0, text=f"🔌 Connecting to Onfleet...")
        _time.sleep(0.05)
        _bar.progress(0.03, text=f"⏳ Fetching {pod_name} tasks from Onfleet...")
        process_pod(pod_name, master_bar=_bar)

        loading_overlay.empty()
        _bar.empty()
        st.session_state.pop('_loading_overlay', None)
        st.session_state.pop('_loading_start', None)
        st.session_state.pop('_loading_pod', None)
        # Seed the auto_sync_checker cooldown (90s of quiet).
        st.session_state['_auto_sync_last_app_rerun_ts'] = datetime.now()
        # st.rerun() RESTORED (May 18 2026 v2 — BUG-1 + BUG-3 fix). Without it,
        # the header was rendered with is_initialized=False BEFORE process_pod
        # ran, so after a successful init the dispatcher saw a stale
        # "Initialize Data" button + "No tasks initialized" message sitting
        # above a fully-populated dashboard. The rerun lets the next pass
        # render the header against the now-true is_initialized state.
        # Admin multi-pod view does flicker once per pod as each finishes —
        # acceptable: it's a load sequence, not a random idle refresh, and a
        # broken-looking dispatcher page is the worse evil.
        st.rerun()

    # 🌟 THE FIX: Remove the early return and safely default to an empty list
    # Load cluster data safely so the Supercards can render 0's
    cls = st.session_state.get(f"clusters_{pod_name}", [])

    # 🛡️ RE-EVALUATE is_initialized (May 18 2026 — BUG-1/BUG-3 defensive fix).
    # The header's is_initialized was computed BEFORE the init/sync blocks ran.
    # If a process_pod just completed inline this pass, clusters_{pod} now
    # exists — recompute so the "No tasks initialized" message + any other
    # downstream is_initialized check reflect reality. The st.rerun() after
    # init normally makes this moot, but this keeps a single render pass
    # internally consistent if the rerun is ever skipped.
    is_initialized = f"clusters_{pod_name}" in st.session_state

    # --- KEEPING THE CLEAN AUTO-SYNC LOGIC ---
    sent_db, ghost_db, _archived_wos, _history_db = fetch_sent_records_from_sheet()
    st.session_state['_history_db'] = _history_db
    st.session_state['archived_wos'] = _archived_wos
    # Merge real-time status updates from auto_sync_checker (overrides stale cache)
    # Security audit H10 - full union. Previously this merge only handled
    # `_tid in sent_db`, so an in-memory tid added by auto_sync_checker while
    # the 5-minute sheet cache was still stale was silently dropped. Now an
    # in-memory tid absent from the fresh fetch is carried over verbatim.
    _ss_db = st.session_state.get('sent_db', {})
    for _tid, _info in _ss_db.items():
        if _tid in sent_db:
            if _info.get('status') != sent_db[_tid].get('status'):
                sent_db[_tid]['status'] = _info['status']
                # Clear reverted flag so traffic cop picks up the new status
                for _c in st.session_state.get(f"clusters_{pod_name}", []):
                    _c_tids = [str(t['id']).strip() for t in _c.get('data', [])]
                    if _tid in _c_tids:
                        _c_hash = hashlib.md5("".join(sorted(_c_tids)).encode()).hexdigest()
                        st.session_state[f"reverted_{_c_hash}"] = False
                        break
        else:
            # In-memory addition not yet visible in the sheet fetch - keep it.
            sent_db[_tid] = _info

    # 🌟 THE FIX: Omni-Ghost Sorter
    # May 4 2026 — fn_ghosts added so FN-status sheet rows have a separate bucket.
    # These get reconstructed into pseudo-clusters and merged into `field_nation`
    # below, since live OnFleet pull can no longer see FN-assigned tasks (state=1).
    pod_ghosts, finalized_ghosts, sent_ghosts, fn_ghosts = [], [], [], []
    seen_ghosts = set() # 🛡️ THE FIX: Streamlit Crash Shield
    
    for g in ghost_db.get(pod_name, []):
        g_hash = g.get('hash')
        
        # If the Google Sheet has duplicate rows, drop the clone instantly!
        if g_hash in seen_ghosts:
            continue
        seen_ghosts.add(g_hash)

        # 🌟 Bug fix: if the dispatcher just revoked/re-routed this route,
        # suppress the sheet ghost until the async sheet archive lands.
        # Without this skip, the route appears "stuck" in Sent/Accepted
        # for ~5-15s after revoke because the live cluster is filtered by
        # is_reverted but the ghost path bypassed that check.
        if g_hash and st.session_state.get(f"reverted_{g_hash}", False):
            continue

        g_stat = g.get("status", "")
        local_override = st.session_state.get(f"route_state_{g_hash}")
        if local_override == "finalized" or g_stat == "finalized": finalized_ghosts.append(g)
        elif g_stat == "sent": sent_ghosts.append(g)
        elif g_stat == "field_nation": fn_ghosts.append(g)
        else: pod_ghosts.append(g)

    # ═══════════════════════════════════════════════════════════════════════════
    # UNASSIGN RECLAIM — Jul 2026 (Nick)
    # ═══════════════════════════════════════════════════════════════════════════
    # Authoritative rule: if OnFleet's fresh state=0 fetch contains a task that
    # sent_db thinks is accepted/finalized, the task was manually unassigned.
    # It gets pulled out of the accepted route and back into Ready for redispatch.
    # Works for partial (some tasks) and full (all tasks) unassigns in one pass.
    # Self-contained — fetches state=0 directly, doesn't depend on any stashed
    # session state from process_pod or smart_sync_pod.
    # 🚀 THROTTLE — Jul 23 2026 — Nick: reclaim block was running on EVERY
    # widget click/rerun, causing lag for pods with 200+ clusters. 20s TTL per
    # pod keeps it responsive without burning CPU on every interaction.
    _reclaim_ts_key = f'_reclaim_last_ts_{pod_name}'
    _reclaim_now = time.time()
    _skip_reclaim = (_reclaim_now - st.session_state.get(_reclaim_ts_key, 0)) < 20
    _fresh_state0 = set()
    if not _skip_reclaim:
        st.session_state[_reclaim_ts_key] = _reclaim_now
        try:
            _reclaim_pull = _fetch_onfleet_open_tasks_cached()
            _fresh_state0 = {str(t.get('id', '')).strip() for t in _reclaim_pull.get('tasks', [])}
        except Exception as _rec_e:
            _log_err(f"unassign_reclaim/{pod_name}/fetch", _rec_e)
    if _fresh_state0:
        _split_cls = []
        _recovered_tids = set()
        _ghosts_to_suppress = set()
        _now_ts = pd.Timestamp.now()
        for _sc in cls:
            _sc_tids = [str(t['id']).strip() for t in _sc.get('data', [])]
            _sc_hash = hashlib.md5("".join(sorted(_sc_tids)).encode()).hexdigest()
            if st.session_state.get(f"reverted_{_sc_hash}", False):
                _split_cls.append(_sc)
                continue
            _sm_row = None
            for _tid in _sc_tids:
                if _tid in sent_db:
                    _sm_row = sent_db[_tid]
                    break
            if not _sm_row:
                _split_cls.append(_sc)
                continue
            _sm_status = str(_sm_row.get('status', '')).lower()
            if _sm_status not in ('accepted', 'finalized'):
                _split_cls.append(_sc)
                continue
            if str(_sm_row.get('name', '')).strip().lower() == 'field nation':
                _split_cls.append(_sc)
                continue
            # 90s propagation grace — OnFleet's state=0 cache is 60s TTL, so
            # a just-accepted route (worker-assign PUT still in flight) could
            # still show its tasks as state=0. Wait 90s before reclaiming.
            _sm_raw_ts = _sm_row.get('raw_ts')
            try:
                if _sm_raw_ts is not None:
                    _age = (_now_ts - _sm_raw_ts).total_seconds()
                    if _age < 90:
                        _split_cls.append(_sc)
                        continue
            except Exception:
                pass
            _un = [t for t in _sc['data'] if str(t['id']).strip() in _fresh_state0]
            _st = [t for t in _sc['data'] if str(t['id']).strip() not in _fresh_state0]
            if not _un:
                _split_cls.append(_sc)
                continue
            def _recompute(_data):
                _bs_vals = [str(x.get('boosted_standard', '')).lower() for x in _data if x.get('boosted_standard')]
                _btag = ('local plus' if any('local plus' in v for v in _bs_vals)
                         else ('boosted' if any('boosted' in v for v in _bs_vals) else ''))
                return {
                    'stops': len(set(x['full'] for x in _data)),
                    'inst_count': sum(1 for x in _data if 'install' in str(x.get('task_type','')).lower()),
                    'remov_count': sum(1 for x in _data if str(x.get('task_type','')).lower() in ('kiosk removal','remove kiosk')),
                    'esc_count': sum(1 for x in _data if x.get('escalated')),
                    'boosted_tag': _btag,
                }
            if _st:
                # Partial unassign: keep still-assigned in Accepted, split unassigned into Ready.
                _assigned_c = dict(_sc)
                _assigned_c['data'] = _st
                _assigned_c.update(_recompute(_st))
                _split_cls.append(_assigned_c)
            _recovered_c = dict(_sc)
            _recovered_c['data'] = _un
            _recovered_c.update(_recompute(_un))
            _recovered_c['status'] = 'Ready'
            _recovered_c['contractor_name'] = 'Unknown'
            _recovered_c['wo'] = ''
            _recovered_c['comp'] = 0
            _recovered_c['due'] = 'N/A'
            _recovered_c['route_ts'] = ''
            _split_cls.append(_recovered_c)
            for t in _un:
                _recovered_tids.add(str(t['id']).strip())
            _sc_tid_set = set(_sc_tids)
            for _g in pod_ghosts:
                _g_tids = set(str(_t).strip() for _t in _g.get('task_ids', []) if str(_t).strip())
                if _g_tids and _g_tids & _sc_tid_set:
                    if _g.get('hash'):
                        _ghosts_to_suppress.add(_g.get('hash'))
        cls = _split_cls
        for _tid in _recovered_tids:
            sent_db.pop(_tid, None)
        # Also reclaim tasks that are state=0 AND accepted/finalized in sent_db
        # but NOT in any current cluster. Happens when clusters got rebuilt
        # while the tasks were state=1 (accepted), and now they're state=0
        # again but haven't been re-clustered by smart_sync yet.
        _cls_all_tids = set()
        for _c_scan in cls:
            for _t_scan in _c_scan.get('data', []):
                _cls_all_tids.add(str(_t_scan.get('id', '')).strip())
        for _tid in list(sent_db.keys()):
            if _tid in _fresh_state0 and _tid not in _cls_all_tids:
                _rec = sent_db[_tid]
                _st_str = str(_rec.get('status', '')).lower()
                if _st_str in ('accepted', 'finalized'):
                    _name_str = str(_rec.get('name', '')).strip().lower()
                    if _name_str != 'field nation':
                        _raw_ts = _rec.get('raw_ts')
                        try:
                            if _raw_ts is not None:
                                _age2 = (_now_ts - _raw_ts).total_seconds()
                                if _age2 < 90:
                                    continue
                        except Exception:
                            pass
                        sent_db.pop(_tid, None)
                        # Suppress matching ghost too so it doesn't rehydrate.
                        for _g in pod_ghosts:
                            _g_tids = set(str(_t).strip() for _t in _g.get('task_ids', []) if str(_t).strip())
                            if _tid in _g_tids and _g.get('hash'):
                                _ghosts_to_suppress.add(_g.get('hash'))
        if _ghosts_to_suppress:
            pod_ghosts[:] = [g for g in pod_ghosts if g.get('hash') not in _ghosts_to_suppress]
            _gd_pod = ghost_db.get(pod_name, [])
            if isinstance(_gd_pod, list):
                ghost_db[pod_name] = [g for g in _gd_pod if g.get('hash') not in _ghosts_to_suppress]
    
    # STATE=0 IS AUTHORITATIVE block deleted Jul 2026 (Nick) — it was a duplicate
    # of UNASSIGN RECLAIM above. Running both produced duplicated route cards
    # (one in Sent/Accepted, one in Dispatch) because each block's mid-pass
    # mutations to sent_db raced with the other. UNASSIGN RECLAIM alone handles
    # all reclaim behavior (partial split, full bounce, orphan reclaim, ghost
    # suppression) with a 20s throttle to keep render latency low.
    
    # 1. 📂 DEFINE BUCKETS
    ready, review, sent, accepted, declined, finalized, field_nation, digital_ready = [], [], [], [], [], [], [], []
    live_hashes = set() # 🌟 Track live routes so we don't duplicate them!
    
    # 🛡️ AWAITING-CONFIRMATION DEDUP GUARD (May 18 2026 — v2 strengthened)
    # A task ID must never appear in BOTH the Awaiting column (Sent/Accepted/
    # Declined/Finalized) AND the main Dispatch column (Ready/Flagged) at the
    # same time. Build a set of every task_id that's currently represented in
    # Awaiting — from BOTH sources:
    #   (1) sent_db — live patch state from auto_sync_checker (accept/decline
    #       events, etc.) plus the read of Saved_Routes
    #   (2) ghost_db — task_ids embedded in the Saved_Routes JSON payload
    # The two sources can disagree briefly post-dispatch: a fresh sheet row
    # may make it into ghost_db's payload-parse before sent_db is rebuilt from
    # the row's task-ID column, or vice-versa. Without including ghost task
    # IDs, a freshly-dispatched route's Awaiting card renders correctly but
    # the same tasks ALSO re-appear in Ready (sheet_match=None lookup misses
    # the just-saved row). Confirmed live: "Anthony Smith-05182026-1" (3 stops
    # Arlington/Mukilteo WA) showed in both columns until this guard.
    # When the cluster loop below decides where to put a cluster, if the
    # cluster's task_ids overlap with _awaiting_tids AND the existing routing
    # rules would have dropped it into Ready or Review, we skip it instead.
    _awaiting_tids = set()
    # (1) sent_db — explicitly include field_nation status (added May 18 2026
    #     after FN routes showed up duplicated in Ready alongside the FN tab).
    for _tid, _rec in sent_db.items():
        if str(_rec.get('status', '')).lower() in ('sent', 'accepted', 'declined', 'finalized', 'field_nation'):
            _awaiting_tids.add(str(_tid).strip())
    # (2) ghost_db for THIS pod — task IDs from any non-revoked ghost route.
    for _g in ghost_db.get(pod_name, []):
        _g_hash = _g.get('hash') or ''
        if _g_hash and st.session_state.get(f"reverted_{_g_hash}", False):
            continue  # Dispatcher explicitly revoked — let the route flow back to Ready
        for _gt in (_g.get('task_ids') or []):
            _gt = str(_gt).strip()
            if _gt:
                _awaiting_tids.add(_gt)
    # Note (May 18 2026 — rolled back): both the cross-pod ghost iteration AND
    # the address-level dedup were tried and removed. They were too aggressive:
    # the cross-pod sweep caused false positives when unrelated pods happened
    # to share legacy task IDs, and the address sweep hid legitimate new
    # continuity batches at already-dispatched kiosks. Keep dedup scoped to
    # this pod's sent_db + ghost_db + same-rerun live clusters.
    # Also include task IDs from every LIVE cluster in `cls` whose session-state
    # route_state pins it to a non-Ready bucket (field_nation, email_sent,
    # finalized). These clusters didn't land in ghost_db (they're still live
    # in clusters_{pod}) but they ARE definitively committed to Awaiting/FN, so
    # any OTHER cluster with overlapping task IDs that ends up in Ready/Flagged
    # is a duplicate that must be suppressed. Fixes the case Nick reported: an
    # FN-tab route's tasks showing simultaneously in Ready after a re-initialize
    # rebuilt clusters_{pod} without preserving the route_state on the second
    # copy of the cluster.
    for _c in cls:
        _c_tids = [str(_t['id']).strip() for _t in _c.get('data', []) if _t.get('id')]
        if not _c_tids:
            continue
        _c_hash = hashlib.md5("".join(sorted(_c_tids)).encode()).hexdigest()
        if st.session_state.get(f"reverted_{_c_hash}", False):
            continue
        _c_route_state = st.session_state.get(f"route_state_{_c_hash}")
        if _c_route_state in ('field_nation', 'email_sent', 'finalized'):
            for _gt in _c_tids:
                _awaiting_tids.add(_gt)

    for c in cls:
        # 🌟 FIX: Skip empty routes that were trimmed to 0 stops
        if not c.get('data') or len(c.get('data')) == 0:
            continue
            
        task_ids = [str(t['id']).strip() for t in c['data']]
        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
        live_hashes.add(cluster_hash) # Save hash
        
        # 🔗 BUNDLE-AWARE sheet_match (Jun 12 2026, relaxed Jun 18 2026).
        # Original problem: bundling Ready + previously-sent route dumped the merged
        # cluster into Sent because any single orphan sheet row hijacked the routing.
        # First fix required ALL cluster tasks be in sent_db with one WO — too strict.
        # Live clusters legitimately pick up fresh OnFleet tasks (post-revoke returns,
        # smart_sync merges) that aren't in sent_db yet; those got rejected and routes
        # bounced back to Ready as if never dispatched. New rule:
        #   - Walk the cluster; collect WOs of any tasks that ARE in sent_db.
        #   - If we see >1 distinct WO → real multi-route bundle hijack → suppress match.
        #   - Otherwise use the first sent_db row found. Single-WO matches go through,
        #     even when only some of the cluster's tasks are in sent_db.
        sheet_match = None
        _first_match_row = None
        _unique_wos = set()
        for _tid in task_ids:
            if _tid in sent_db:
                _row = sent_db[_tid]
                _wo = str(_row.get('wo', '')).strip()
                if _wo:
                    _unique_wos.add(_wo)
                if _first_match_row is None:
                    _first_match_row = _row
        if _first_match_row and len(_unique_wos) <= 1:
            sheet_match = _first_match_row
        # If len(_unique_wos) >= 2 → multi-WO bundle, leave sheet_match=None so the
        # merged cluster falls to Ready instead of inheriting any one row's status.
        route_state = st.session_state.get(f"route_state_{cluster_hash}")
        local_ts = st.session_state.get(f"sent_ts_{cluster_hash}", "")
        local_contractor = st.session_state.get(f"contractor_{cluster_hash}", "Unknown")
        local_wo = st.session_state.get(f"wo_{cluster_hash}", local_contractor) # 🌟 Fetch WO
        local_comp = st.session_state.get(f"comp_{cluster_hash}", 0)
        local_due = st.session_state.get(f"due_{cluster_hash}", 'N/A')
        is_reverted = st.session_state.get(f"reverted_{cluster_hash}", False)

        # NOTE: the next block mutates `c` (the cluster dict in session state) directly
        # on every rerender — contractor_name / route_ts / wo / comp / due are refreshed
        # from the sheet each pass. Once the user types into pay_key/rate_key those
        # session-state-scoped values take over for input rendering, so the cluster-level
        # `comp` overwrite here doesn't fight with user edits. Don't rely on these fields
        # being immutable — they're effectively a sheet-driven cache.
        if sheet_match and not is_reverted:
            c['contractor_name'] = sheet_match.get('name', 'Unknown')
            c['route_ts'] = sheet_match.get('time', '') or local_ts
            c['wo'] = sheet_match.get('wo', c['contractor_name'])
            # Sheet has authoritative values, but fall back to local session state if the
            # sheet readback hasn't caught up yet (e.g., immediately post-dispatch). This
            # was previously rendering $0 / N/A on the just-dispatched card for ~15s.
            c['comp'] = sheet_match.get('comp') or local_comp or 0
            c['due'] = sheet_match.get('due') or local_due or 'N/A'
        else:
            # 🌟 Apply Fallbacks Instantly
            c['contractor_name'] = local_contractor
            c['wo'] = local_wo
            c['route_ts'] = local_ts
            c['comp'] = local_comp
            c['due'] = local_due
        
        # --- 🚦 DIGITAL OWNERSHIP RULE (Jun 18 2026, refined for FN flow) ---
        # Digital tasks live in the per-pod Digital tab UNLESS they're actively
        # parked in the Awaiting Confirmation column (sent / accepted / declined /
        # finalized / field_nation). Without this, revoked digital routes fall
        # through to Ready/Flagged at line 7575 — Nick: "digital cannot go to
        # ready/flagged". Subtleties:
        #   - route_state == "email_sent" or "field_nation": instant-UI signal
        #     immediately after Generate Link or "Assign to FN" click. Trust it
        #     even if sheet_match is stale and is_reverted is set as a guard.
        #   - sheet_match.status in any Awaiting state: persistent ownership.
        #   - is_reverted + no route_state: an explicit revoke → must go back
        #     to Digital (NOT Ready), so DO NOT honor stale sheet_match here.
        if c.get('is_digital'):
            _digital_in_awaiting = False
            if route_state in ("email_sent", "field_nation"):
                # Local instant-UI flag wins, regardless of is_reverted/sheet_match.
                _digital_in_awaiting = True
            elif not is_reverted and sheet_match:
                _sm_status = str(sheet_match.get('status', '')).lower()
                if _sm_status in ('sent', 'accepted', 'declined', 'finalized', 'field_nation'):
                    _digital_in_awaiting = True
            if not _digital_in_awaiting:
                # Dedup guard — only when not reverted (revoke WANTS this route
                # to leave Awaiting and reappear in Digital).
                if not is_reverted and any(tid in _awaiting_tids for tid in task_ids):
                    continue
                digital_ready.append(c)
                continue
            # else: fall through to the Awaiting bucket router below

        # --- PRIORITY: LIVE DATABASE OVERRIDES LOCAL STATE ---
        # 🌟 THE FIX: If we just clicked Finalize, override the Google Sheet instantly!
        if route_state == "finalized":
            finalized.append(c)
        elif sheet_match and not is_reverted:
            raw_status = str(sheet_match.get('status', '')).lower()
            # RETURN-TO-DISPATCH (May 23 2026 - Nick: previously-assigned
            # then unassigned tasks must pop back to Dispatch).
            # A live cluster only reaches this loop because its tasks are in
            # OnFleet's state=0 (unassigned) feed. A route's OnFleet tasks
            # get assigned to a worker only when a contractor ACCEPTS - so an
            # 'accepted'/'finalized' route whose tasks are sitting unassigned
            # was unassigned again (directly in OnFleet, or via a DCC revoke)
            # and must drop back to the Dispatch/Ready side. Skip the override
            # only inside a short grace window so a just-accepted route whose
            # worker-assignment PUT is still propagating cannot flicker into
            # Ready and get double-dispatched.
            _reassigned_back = False
            # 🌐 FN-assigned routes are managed by Field Nation, not DCC.
            # Their OnFleet tasks can legitimately sit in the state=0 feed (DCC
            # never worker-assigns an FN route), so RETURN-TO-DISPATCH must NOT
            # fire for them — otherwise every FN route bounces out of Accepted
            # into Ready. markFNAssigned always stamps the contractor as
            # "Field Nation"; a real contractor name never collides.
            _is_fn_route = str(sheet_match.get('name', '')).strip().lower() == 'field nation'
            if raw_status in ('accepted', 'finalized') and not _is_fn_route:
                # RETURN-TO-DISPATCH re-enabled Jul 2026 (Nick: "manually
                # unassigned in OnFleet needs to flow back to a pool").
                # The May-2026 version bounced on cached clusters and got
                # disabled. This one bounces ONLY when the most recent
                # OnFleet state=0 fetch actually contained every task in
                # the cluster AND the fetch happened AFTER the acceptance.
                # Both conditions together mean the tasks were assigned
                # (post-accept) and then unassigned back to state=0 — the
                # exact scenario we want to catch.
                _fresh_state0 = st.session_state.get(f'_state0_ids_{pod_name}', set())
                _fresh_fetch_ts = st.session_state.get(f'_state0_fetch_ts_{pod_name}')
                _acc_raw_ts = sheet_match.get('raw_ts')
                if _fresh_state0 and _fresh_fetch_ts is not None:
                    _fetch_after_accept = True
                    try:
                        if _acc_raw_ts is not None:
                            _fetch_after_accept = _fresh_fetch_ts > _acc_raw_ts
                    except Exception:
                        _fetch_after_accept = True
                    # RETURN-TO-DISPATCH now handled entirely by the UNASSIGN
                    # RECLAIM preprocessing above (Jul 2026). Nothing to do here.
                    pass
            if _reassigned_back:
                c['status'] = 'Ready'
                ready.append(c)
            elif raw_status == 'field_nation':
                # Restore session state so checkbox stays checked after reload
                if not st.session_state.get(f"route_state_{cluster_hash}"):
                    st.session_state[f"route_state_{cluster_hash}"] = "field_nation"
                field_nation.append(c)
            elif raw_status == 'declined': declined.append(c) #
            elif raw_status == 'accepted': accepted.append(c) #
            elif raw_status == 'finalized': finalized.append(c) #
            else: sent.append(c) #
        
        # 🌟 Handle Local Session State (Instant UI Moves)
        elif route_state == "email_sent" and not is_reverted:
            sent.append(c) #
        elif route_state == "field_nation":
            field_nation.append(c) #
        else:
            # 🛡️ AWAITING-CONFIRMATION DEDUP (May 18 2026)
            # A cluster's task_ids must not overlap with any task in the
            # Awaiting column (sent/accepted/declined/finalized) UNLESS the
            # dispatcher explicitly revoked it (is_reverted=True). If they
            # overlap AND we're falling through to Ready/Review, the
            # Awaiting cluster owns those tasks — skip rendering here so
            # the same kiosk doesn't appear in both columns.
            # Address-level dedup was tried but it hid LEGITIMATE new
            # continuity batches at already-dispatched kiosks. Reverted.
            if not is_reverted and any(tid in _awaiting_tids for tid in task_ids):
                continue
            # 🗑️ CVS Kiosk Removal personal toggle — see the checkbox rendered
            # above the Dispatch/Awaiting columns further down this function.
            # Only filters the undecided Ready/Flagged queue; already-
            # dispatched CVS routes are handled by the branches above this
            # fallback and are never touched here.
            if c.get('is_removal') and not st.session_state.get(f"show_cvs_kiosk_removal_{pod_name}", False):
                continue
            # Fallback to calculated status
            if (c.get('status') == 'Ready'
                    and float(st.session_state.get(f'_rate_master_{pod_name}_{cluster_hash}', 0) or 0) < HIGH_RATE_FLAG_THRESHOLD):
                ready.append(c)
            else:
                review.append(c)

    # 🌐 FN-GHOST RECONSTRUCTION — the actual fix for the empty-FN-tab bug.
    # FN-status sheet rows whose tasks have moved to OnFleet state=1 won't
    # appear in `cls` (the main pull is state=0 only) and so the live loop
    # above never sees them. Rebuild them into pseudo-clusters and append
    # them to `field_nation`. We dedupe against live_hashes — if the live
    # cluster still has the tasks (FN move happened but OnFleet hasn't
    # caught up yet, or the user just reverted), the live entry above wins.
    for _fng in fn_ghosts:
        _fng_hash = _fng.get('hash')
        if not _fng_hash or _fng_hash in live_hashes:
            continue
        _pseudo = _fn_ghost_to_cluster(_fng)
        if _pseudo is None:
            continue
        field_nation.append(_pseudo)
        # Make sure render_dispatch sees route_state == 'field_nation' so the
        # FN-action block (Download FN Upload, Post link, Accepted button)
        # fires for this pseudo-cluster. Don't clobber a deliberate override.
        if not st.session_state.get(f"route_state_{_fng_hash}"):
            st.session_state[f"route_state_{_fng_hash}"] = "field_nation"

    # --- 🐛 DEBUG: bucket routing visibility (collapsed by default, no behavior change) ---
    # Shows what each cluster's bucket decision was based on. Drop the URL-param check or
    # remove the whole block once we've confirmed auto-move works.
    if st.query_params.get("debug") == "1" and _is_admin_or_manager():
        with st.expander(f"🐛 Bucket debug — {pod_name}", expanded=False):
            _fp_now = st.session_state.get(f"_auto_sync_fp_{pod_name}", "(unset)")
            st.caption(f"Last fingerprint: `{_fp_now[:12]}...`  |  sent_db rows: {len(sent_db)}  |  pod_clusters: {len(cls)}")
            # Extra ghost diagnostics — figure out where the bundled-route ghosts are landing
            st.markdown("**Ghosts (raw from sheet)** for this pod:")
            for _g in ghost_db.get(pod_name, []):
                _g_status = str(_g.get('status', '')).lower()
                _g_wo = _g.get('wo', '?')
                _g_hash = (_g.get('hash') or '')[:8]
                _g_tids = _g.get('task_ids', [])
                _g_tasks = _g.get('tasks', '?')
                st.text(f"  {_g_hash} | {_g_status:10} | wo={_g_wo:35} | tasks={_g_tasks} | tids={len(_g_tids)}")
            st.markdown("**sent_ghosts** (after status routing) for this pod:")
            for _g in sent_ghosts:
                _g_wo = _g.get('wo', '?')
                _g_hash = (_g.get('hash') or '')[:8]
                _g_tasks = _g.get('tasks', '?')
                st.text(f"  {_g_hash} | wo={_g_wo:35} | tasks={_g_tasks}")
            st.markdown("**`_bundle_map`** for this pod:")
            _bm_pod = st.session_state.get('_bundle_map', {}).get(pod_name, [])
            if not _bm_pod:
                st.text("  (empty — no bundles recorded in this session)")
            for _i, _entry in enumerate(_bm_pod):
                _e_list = sorted(list(_entry))
                st.text(f"  bundle #{_i}: {len(_e_list)} task IDs → {_e_list[:3]}{'...' if len(_e_list) > 3 else ''}")
            st.markdown(f"**Live cluster sample for `{pod_name}`** (matching the two sent Alvarado entries):")
            for _bc in sent:
                _btids_dbg = sorted([str(_t['id']).strip() for _t in _bc.get('data', [])])
                st.text(f"  {_bc.get('contractor_name','?')} | {len(_btids_dbg)} tasks | first 3: {_btids_dbg[:3]}")
            _bucket_map = [("ready", ready), ("review", review), ("sent", sent), ("accepted", accepted),
                           ("declined", declined), ("finalized", finalized), ("field_nation", field_nation),
                           ("digital_ready", digital_ready)]
            for _bname, _blist in _bucket_map:
                if not _blist:
                    continue
                st.markdown(f"**{_bname}** ({len(_blist)})")
                for _bc in _blist:
                    _btids = [str(_t['id']).strip() for _t in _bc.get('data', [])]
                    _bhash = hashlib.md5("".join(sorted(_btids)).encode()).hexdigest()[:8]
                    _bsm = sent_db.get(next((tid for tid in _btids if tid in sent_db), None))
                    _brs = st.session_state.get(f"route_state_{hashlib.md5(''.join(sorted(_btids)).encode()).hexdigest()}")
                    _brev = st.session_state.get(f"reverted_{hashlib.md5(''.join(sorted(_btids)).encode()).hexdigest()}", False)
                    _bsm_status = _bsm.get('status') if _bsm else "(no sheet match)"
                    st.text(f"  {_bhash} | {_bc.get('contractor_name','?'):20} | sheet={_bsm_status:10} | route_state={_brs} | reverted={_brev}")

    # --- 📊 CATEGORIZED MATH ---
    # Routes
    ready_count = len(ready)
    flagged_count = len(review)

    # 🌟 May 16 2026 — SUPERCARD BUCKET RULES (Nick's spec):
    # • Static Workload TASKS/STOPS counts non-digital work in Ready + Flagged
    #   + Sent ONLY. Field Nation, Declined, Accepted, Finalized are excluded
    #   so the dispatcher's "in-flight static workload" number stays accurate.
    # • Digital Workload TASKS/STOPS counts digital work in those same active
    #   buckets, PLUS the Digital Ready dispatch pool.
    # • Field Nation gets its OWN supercard (routes + tasks) so its volume is
    #   tracked separately from the static dispatch funnel.
    _static_buckets = ready + review + sent

    tasks_static = sum(len(c['data']) for c in _static_buckets if not c.get('is_digital'))
    stops_static = sum(c['stops']      for c in _static_buckets if not c.get('is_digital'))
    tasks_digital = sum(len(c['data']) for c in _static_buckets if c.get('is_digital'))
    stops_digital = sum(c['stops']      for c in _static_buckets if c.get('is_digital'))

    # Digital Ready pool is its own bucket — fold it into Digital totals.
    tasks_digital += sum(len(c['data']) for c in digital_ready)
    stops_digital += sum(c['stops']      for c in digital_ready)

    # Field Nation — own supercard (routes + tasks).
    fn_routes_count = len(field_nation)
    fn_tasks = sum(len(c['data']) for c in field_nation)

    # 📊 Supercard-excluded buckets (visible in Awaiting Confirmation but not counted in TASKS card)
    _excluded_accepted = sum(len(c['data']) for c in accepted)
    _excluded_finalized = sum(len(c['data']) for c in finalized)
    _excluded_declined = sum(len(c['data']) for c in declined)
    
    # Sent Records
    accepted_count = len(accepted) + len(pod_ghosts)
    declined_count = len(declined)
    total_sent = len(sent) + accepted_count + declined_count + len(field_nation)

    # --- DASHBOARD SUPERCARDS (5-Card Layout — May 16 2026: added Field Nation) ---
    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 1])

    with c1:
        # CARD 1: ROUTE STATUS (Ready | Flagged)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Route Status</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_GREEN_TEXT};'>READY</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_GREEN_TEXT};'>{ready_count}</p>
                    </div>
                    <div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_RED_TEXT};'>FLAGGED</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_RED_TEXT};'>{flagged_count}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    with c2:
        # CARD 2: STATIC WORKLOAD (Tasks | Stops)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Static Workload</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>TASKS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{tasks_static}</p>
                    </div>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>STOPS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{stops_static}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    with c3:
        # CARD 3: DIGITAL WORKLOAD (Updated to Static Theme)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Digital Workload</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>TASKS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{tasks_digital}</p>
                    </div>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>STOPS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{stops_digital}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    with c4:
        # CARD 4: FIELD NATION (Routes | Tasks) — May 16 2026
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Field Nation</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_YELLOW_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:#854d0e;'>ROUTES</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:#854d0e;'>{fn_routes_count}</p>
                    </div>
                    <div style='background:{TB_YELLOW_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:#854d0e;'>TASKS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:#854d0e;'>{fn_tasks}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    with c5:
        # CARD 5: SENT RECORDS (Accepted | Declined)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>All Sent: {total_sent}</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_GREEN_TEXT};'>ACCEPTED</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_GREEN_TEXT};'>{accepted_count}</p>
                    </div>
                    <div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_RED_TEXT};'>DECLINED</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_RED_TEXT};'>{declined_count}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    # --- 📊 TASK ATTRITION EXPANDER (collapsed by default) ---
    # Only Admin / Manager see the attrition diagnostic. Dispatchers and
    # Associates don't need the funnel breakdown — it's a debugging surface.
    _attr = st.session_state.get(f'_attrition_{pod_name}')
    # Task attrition expander — visible to dispatchers, admins, and managers.
    # Hidden from FN Associates (tier='guest') since they only work the FN tab.
    # (May 18 2026 — Nick: "for all dispatchers only".)
    if _attr and not _is_dispatch_associate():
        with st.expander(f"📊 Task attrition — {pod_name} Pod", expanded=False):
            _raw = _attr.get('raw_fetched', 0)
            _ded = _attr.get('after_dedup', 0)
            _no_cf = _attr.get('skipped_no_state_cf', 0)
            _wrk = _attr.get('skipped_assigned_worker', 0)
            _team = _attr.get('skipped_wrong_team', 0)
            _oops = _attr.get('skipped_out_of_pod_states', 0)
            _pool = _attr.get('final_pool', 0)
            _sup_total = tasks_static + tasks_digital + fn_tasks  # Now includes FN tracked in its own card.
            st.markdown(f"""
**Raw → Pool funnel (this pod's slice of the Onfleet pull)**

| Step | Count | Drop |
|---|---:|---:|
| 1. Raw fetched from Onfleet (`tasks/all?state=0`, last 45d) | **{_raw}** | — |
| 2. After dedup by task ID | {_ded} | {max(0, _raw - _ded)} |
| 3. After driver-home filter (require `state` custom field) | {_ded - _no_cf} | {_no_cf} |
| 4. After assigned-worker leak filter | {_ded - _no_cf - _wrk} | {_wrk} |
| 5. After approved-team filter | {_ded - _no_cf - _wrk - _team} | {_team} |
| 6. After in-pod-states filter | **{_pool}** | {_oops} |

**Pool → Supercard math (across buckets)**

| Bucket | Tasks | Static card? | Digital card? | FN card? |
|---|---:|---|---|---|
| Ready | {sum(len(c['data']) for c in ready)} | ✅ (if non-digital) | ✅ (if digital) | — |
| Flagged (review) | {sum(len(c['data']) for c in review)} | ✅ (if non-digital) | ✅ (if digital) | — |
| Sent | {sum(len(c['data']) for c in sent)} | ✅ (if non-digital) | ✅ (if digital) | — |
| Digital Ready | {sum(len(c['data']) for c in digital_ready)} | — | ✅ | — |
| Field Nation | {sum(len(c['data']) for c in field_nation)} | ❌ | — | ✅ |
| Declined | {_excluded_declined} | ❌ excluded | ❌ excluded | — |
| Accepted | {_excluded_accepted} | ❌ excluded | ❌ excluded | — |
| Finalized | {_excluded_finalized} | ❌ excluded | ❌ excluded | — |

**Static TASKS: {tasks_static} · Digital TASKS: {tasks_digital} · Field Nation TASKS: {fn_tasks}** (FN tracked separately per May 16 2026 spec).
— excludes {_excluded_declined + _excluded_accepted + _excluded_finalized} declined+accepted+finalized tasks.
            """)

            # 📍 Stop-address breakdown for every excluded route so the dispatcher
            # can quickly cross-reference and re-route stale ones if needed.
            def _excluded_section(label, clusters):
                if not clusters:
                    return
                st.markdown(f"**{label} — {len(clusters)} route(s) | {sum(len(c['data']) for c in clusters)} tasks**")
                for _c in clusters:
                    _wo = _c.get('wo') or _c.get('contractor_name') or '(no WO)'
                    _ic = _c.get('contractor_name', '?')
                    _comp = _c.get('comp', 0)
                    _due = _c.get('due', 'N/A')
                    _addrs = []
                    for _t in _c.get('data', []):
                        _full = _t.get('full', '').strip()
                        if _full and _full not in _addrs:
                            _addrs.append(_full)
                    _addr_lines = '\n'.join(f"  - {a}" for a in _addrs) or "  (no addresses)"
                    st.markdown(
                        f"<div style='font-size:12px; padding:6px 10px; margin:4px 0; "
                        f"background:#f8fafc; border-left:3px solid #94a3b8; border-radius:4px;'>"
                        f"<b>{_wo}</b> &middot; {_ic} &middot; \${_comp} &middot; Due {_due} "
                        f"&middot; {len(_c.get('data', []))} tasks @ {len(_addrs)} stops"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    # Native HTML <details> — Streamlit disallows expanders
                    # nested inside expanders, and this block is rendered inside
                    # the Task Attrition expander above.
                    _addr_html_li = "".join(f"<li style='font-family:monospace; font-size:11px; color:#475569;'>{_a}</li>" for _a in _addrs) or "<li style='color:#94a3b8;'>(no addresses)</li>"
                    st.markdown(
                        f"<details style='margin:4px 0 8px 0; padding-left:8px;'>"
                        f"<summary style='cursor:pointer; font-size:11px; color:#475569; font-weight:600;'>Stops ({len(_addrs)})</summary>"
                        f"<ul style='margin:6px 0 0 0; padding-left:20px;'>{_addr_html_li}</ul>"
                        f"</details>",
                        unsafe_allow_html=True,
                    )

            _excluded_section("❌ Accepted (excluded)", accepted)
            _excluded_section("🏁 Finalized (excluded)", finalized)

    # 🌟 THE FIX: Force spacing before the Map
    st.markdown("<div style='margin-bottom: 25px;'></div>", unsafe_allow_html=True)
    
    # 🌟 Halt execution HERE, right after the cards render!
    if not is_initialized:
        st.info(f"No {pod_name} tasks initialized. Click '🚀 Initialize Data' at the top right.")
        return
        
    # 🌟 THE FIX: Don't hide the tab if there are pending sent routes!
    if not cls and not pod_ghosts and not sent_ghosts and not finalized_ghosts:
        st.info(f"No active tasks pending in the {pod_name} region.")
        return

    # 🚀 Opt B — Lazy-load the Folium map. Wrapping in a collapsed expander
    # defers the heavy Leaflet iframe + marker render until the dispatcher
    # clicks. Most operational interactions don't need the map, so this
    # removes ~100KB of HTML/JS from the default page weight.
    with st.expander("🗺️ Master Route Map", expanded=False):
        # 🌟 THE FIX: Prevent IndexError if there are Ghost routes but no Live routes!
        map_center = cls[0]['center'] if cls else [39.8283, -98.5795]
        m = folium.Map(location=map_center, zoom_start=6 if cls else 4, tiles="cartodbpositron")
        # 🗺️ Map shows only the operational buckets the dispatcher acts on:
        # Ready, Flagged (review), Field Nation, Sent. Accepted/Declined/Finalized
        # are deliberately excluded so the map stays a working surface, not a history view.
        # Digital is excluded too — it has its own dedicated map below.
        for c in ready:        folium.CircleMarker(c['center'], radius=8, color=TB_GREEN,   fill=True, opacity=0.8).add_to(m)
        for c in review:       folium.CircleMarker(c['center'], radius=8, color="#ef4444", fill=True, opacity=0.8).add_to(m)
        # FN-ghost pseudo-clusters whose Mapbox geocode failed have center=(0,0).
        # Skipping them keeps the marker out of the Atlantic when the only location
        # info we have is a saved address that didn't resolve.
        for c in field_nation:
            if c.get('_is_fn_ghost_pseudo') and not c.get('_has_real_center'):
                continue
            folium.CircleMarker(c['center'], radius=8, color="#ca8a04", fill=True, opacity=0.8).add_to(m)
        for c in sent:         folium.CircleMarker(c['center'], radius=8, color="#3b82f6", fill=True, opacity=0.8).add_to(m)
        # 📌 returned_objects=[] disables the map's rerun-on-interaction behavior.
        # Without this, every zoom/pan/click on the Leaflet map re-runs the entire
        # Streamlit script, causing the "page keeps refreshing" experience.
        st_folium(m, height=400, use_container_width=True, key=f"map_{pod_name}", returned_objects=[])
    
    st.markdown("""
<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:14px 20px; margin-bottom:20px; box-shadow:0 2px 4px rgba(0,0,0,0.04);">
    <div style="font-size:10px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em; margin-bottom:12px;">📖 Route Key</div>
    <div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr 1fr 1fr; gap:12px;">
        <div>
            <div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px;">Status</div>
            <div style="display:flex; flex-direction:column; gap:4px; font-size:12px; color:#334155;">
                <span title="Route is within distance limits and standard rate — ready to dispatch.">🟢 Ready</span>
                <span title="Rate is $25+/stop or IC is 60+ miles away. Unlock required before sending.">🔒 Action Required</span>
                <span title="Route was flagged for review — low density or pricing issue.">🔴 Flagged</span>
                <span title="Route has been assigned to Field Nation for external dispatch.">🌐 Field Nation</span>
            </div>
        </div>
        <div>
            <div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px;">Flags</div>
            <div style="display:flex; flex-direction:column; gap:4px; font-size:12px; color:#334155;">
                <span title="Closest available IC is 60+ miles from the route center.">📡 Long Distance</span>
                <span title="Route consists exclusively of CVS Kiosk Removal tasks — capped at 10 stops.">🗑️ CVS Removal</span>
            </div>
        </div>
        <div>
            <div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px;">Priority</div>
            <div style="display:flex; flex-direction:column; gap:4px; font-size:12px; color:#334155;">
                <span title="Route contains one or more escalated tasks requiring priority handling.">❗ Escalation</span>
                <span title="Local Plus campaign — higher value placements in targeted local markets.">⭐ Local Plus</span>
                <span title="Boosted campaign — premium national or regional campaign with elevated priority.">🔥 Boosted</span>
            </div>
        </div>
        <div>
            <div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px;">Task Types</div>
            <div style="display:flex; flex-direction:column; gap:4px; font-size:12px; color:#334155;">
                <span title="New Ad: Fresh creative installation at this location.">🆕 New Ad</span>
                <span title="Continuity: Replacing an existing ad with updated creative.">🔄 Continuity</span>
                <span title="Default: Pull-down or placeholder installation.">⚪ Default</span>
                <span title="Kiosk Install: Physical kiosk installation at this stop.">🛠️ Kiosk Install</span>
                <span title="Kiosk Removal: Physical kiosk removal — CVS routes only.">🗑️ Kiosk Removal</span>
                <span title="Custom task type defined in Onfleet outside of standard categories.">📋 Custom</span>
            </div>
        </div>
        <div>
            <div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px;">Contractor</div>
            <div style="display:flex; flex-direction:column; gap:4px; font-size:12px; color:#334155;">
                <span title="Number of tasks currently assigned to this contractor in Onfleet.">🔵 Current Tasks</span>
            </div>
        </div>
        <div>
            <div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px;">Digital</div>
            <div style="display:flex; flex-direction:column; gap:4px; font-size:12px; color:#334155;">
                <span title="Digital Offline: Screen at this location has been reported offline.">📵 Offline</span>
                <span title="Digital Ins/Rem: Installation or removal of a digital screen unit.">🔧 Ins/Rem</span>
                <span title="Digital Service: Routine maintenance or software service of a digital screen.">⚙️ Service</span>
                <span title="Digital route — IC must be digital-certified to receive this route.">🔌 Digital</span>
            </div>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

    st.markdown("---")

    # 🗑️ CVS KIOSK REMOVAL TOGGLE (Sep 2026 — Nick). Personal, per-pod-tab
    # filter: unchecking hides CVS Kiosk Removal routes from THIS
    # dispatcher's Dispatch/Ready+Flagged queue in THIS pod only. Nobody
    # else's view changes, and routes already in Sent/Accepted/Field
    # Nation/Finalized still show — this only declutters the undecided
    # queue. Keyed per pod_name (not a single global key) because
    # run_pod_tab renders once per pod (up to 5x for admin/manager), so a
    # shared key here would raise a duplicate-widget-id error. Defaults to
    # OFF (unchecked / hidden) on first load and resets to that default on
    # redeploy/restart — plain session_state, not persisted to a sheet.
    st.checkbox(
        "🗑️ Show CVS Kiosk Removal routes",
        value=st.session_state.get(f"show_cvs_kiosk_removal_{pod_name}", False),
        key=f"show_cvs_kiosk_removal_{pod_name}",
        help="Uncheck to hide CVS Kiosk Removal routes from this pod's Dispatch queue. "
             "This only affects your own view — other dispatchers still see them, and "
             "any you've already Sent/Accepted/Finalized/assigned to Field Nation stay visible.",
    )

    col_left, col_right = st.columns([5, 5])

    with col_left:
        st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_PURPLE}; text-align: center;'>🚀 Dispatch</div>", unsafe_allow_html=True)
        # Slim the per-route Generate Link button (May 23 2026) -- scoped to
        # its gbtn_ key so only the dispatch button is affected.
        st.markdown(
            """
            <style>
            div[class*="st-key-gbtn_"] button {
                min-height: 2rem !important;
                height: 2rem !important;
                padding-top: 0.05rem !important;
                padding-bottom: 0.05rem !important;
                line-height: 1.1 !important;
            }
            div[class*="st-key-gbtn_"] button p {
                font-size: 0.8rem !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        t_ready, t_flagged, t_fn, t_digital = st.tabs(["📥 Ready", "⚠️ Flagged", "🌐 Field Nation", "🔌 Digital"])

        with t_ready:
            if _is_dispatch_associate():
                st.info("🔒 Field Nation Associates have access to the Field Nation tab only.")
            elif not ready: st.info("No tasks ready for dispatch.")
            else:
                sorted_ready = group_and_sort_by_proximity(ready)
                _pg_items, _pg_start = _paginate_panel(sorted_ready, f"{pod_name}_ready", per_page=25)
                current_state = None
                for i, c in enumerate(_pg_items, start=_pg_start):
                    # 🌟 Insert State Header
                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div class='dcc-state-header' data-state-key='{current_state}'><span class='dcc-state-chevron'>▸</span>📍 {current_state}</div>", unsafe_allow_html=True)
                        
                    badges = ""
                    if not ic_df.empty:
                        lat_col = next((col for col in ic_df.columns if str(col).strip().lower() == 'lat'), 'Lat')
                        lng_col = next((col for col in ic_df.columns if str(col).strip().lower() == 'lng'), 'Lng')
                        loc_col = next((col for col in ic_df.columns if str(col).strip().lower() == 'location'), 'Location')
                        if lat_col in ic_df.columns and lng_col in ic_df.columns:
                            v_ics = ic_df[~ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)].dropna(subset=[lat_col, lng_col]).copy()
                            if not v_ics.empty:
                                v_ics['d'] = v_ics.apply(lambda x: haversine(c['center'][0], c['center'][1], x[lat_col], x[lng_col]), axis=1)
                                closest_ic = v_ics.sort_values('d').iloc[0]
                                _, hrs, _, _ = get_gmaps(_ic_home_loc(closest_ic, f"{c['center'][0]},{c['center'][1]}"), [t['full'] for t in c['data'][:25]])
                                est_pay = hrs * 25.0 # 🌟 STRICTLY HOURLY
                                est_rate = est_pay / c['stops'] if c['stops'] > 0 else 0
                                if closest_ic['d'] > 60: badges += " 📡"

                    esc_pill = f" | ❗ {c.get('esc_count', 0)}" if c.get('esc_count', 0) > 0 else ""
                    inst_pill = f" | 🛠️ {c.get('inst_count', 0)} Installs" if c.get('inst_count', 0) > 0 else "" 
                    remov_pill = f" | 🗑️ {c.get('remov_count', 0)} Removal" if (c.get('remov_count', 0) > 0 and not c.get('is_removal')) else ""
                    remov_tag = f" 🗑️ CVS Removal — {c.get('remov_count', 0)} Units" if c.get('is_removal') else ""
                    _BOOSTED_BADGES = {'local plus': '⭐ LOCAL PLUS', 'boosted': '🔥 BOOSTED'}
                    boosted_pill = f" | {next((v for k,v in _BOOSTED_BADGES.items() if k in c.get('boosted_tag','')), '')}" if c.get('boosted_tag') and any(k in c.get('boosted_tag','') for k in _BOOSTED_BADGES) else ""
                    with st.expander(f"{badges} 🟢 {c['city']}, {c['state']} | {c['stops']} Stops | 🗑️ CVS Kiosk Removal") if c.get('is_removal') else st.expander(f"{badges} 🟢 {c['city']}, {c['state']} | {c['stops']} Stops{inst_pill}{remov_pill}{boosted_pill}{esc_pill}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                        render_dispatch(i, c, pod_name)
                    
        with t_flagged:
            if _is_dispatch_associate():
                st.info("🔒 Field Nation Associates have access to the Field Nation tab only.")
            elif not review: st.info("No flagged tasks requiring review.")
            else:
                sorted_review = group_and_sort_by_proximity(review)
                _pg_items, _pg_start = _paginate_panel(sorted_review, f"{pod_name}_flagged", per_page=25)
                current_state = None
                for i, c in enumerate(_pg_items, start=_pg_start):
                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div class='dcc-state-header' data-state-key='{current_state}'><span class='dcc-state-chevron'>▸</span>📍 {current_state}</div>", unsafe_allow_html=True)
                    
                    esc_pill = f" | ❗ {c.get('esc_count', 0)}" if c.get('esc_count', 0) > 0 else ""
                    inst_pill = f" | 🛠️ {c.get('inst_count', 0)} Installs" if c.get('inst_count', 0) > 0 else ""
                    remov_pill = f" | 🗑️ {c.get('remov_count', 0)} Removal" if (c.get('remov_count', 0) > 0 and not c.get('is_removal')) else ""
                    remov_tag = f" 🗑️ CVS Removal — {c.get('remov_count', 0)} Units" if c.get('is_removal') else ""
                    _BOOSTED_BADGES = {'local plus': '⭐ LOCAL PLUS', 'boosted': '🔥 BOOSTED'}
                    boosted_pill = f" | {next((v for k,v in _BOOSTED_BADGES.items() if k in c.get('boosted_tag','')), '')}" if c.get('boosted_tag') and any(k in c.get('boosted_tag','') for k in _BOOSTED_BADGES) else ""
                    with st.expander(f"🔒 🔴 {c['city']}, {c['state']} | {c['stops']} Stops | 🗑️ CVS Kiosk Removal") if c.get('is_removal') else st.expander(f"🔒 🔴 {c['city']}, {c['state']} | {c['stops']} Stops{inst_pill}{remov_pill}{boosted_pill}{esc_pill}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                        render_dispatch(i+1000, c, pod_name)

        with t_fn:
            if not field_nation: st.info("No routes currently moved to Field Nation.")
            else:
                sorted_fn = group_and_sort_by_proximity(field_nation)
                # FN-tab button slimming (May 23 2026) -- scoped to fn_-keyed
                # widgets so it never affects buttons on other tabs.
                st.markdown(
                    """
                    <style>
                    div[class*="st-key-fn_"]:not([class*="st-key-fn_sel_"]):not([class*="st-key-fn_combined_select_"]) button,
                    div[data-testid="stLinkButton"] a {
                        background: #faf9fd !important;
                        border: 1px solid #d9d4ec !important;
                        border-radius: 8px !important;
                        box-shadow: none !important;
                        color: #7a72a8 !important;
                        min-height: 2rem !important;
                        height: 2rem !important;
                        padding: 0.1rem 0.5rem !important;
                        line-height: 1.1 !important;
                    }
                    div[class*="st-key-fn_"]:not([class*="st-key-fn_sel_"]):not([class*="st-key-fn_combined_select_"]) button p,
                    div[data-testid="stLinkButton"] a p {
                        font-size: 0.8rem !important;
                        font-weight: 500 !important;
                        color: #7a72a8 !important;
                    }
                    div[data-testid="stHorizontalBlock"]:has(div[class*="st-key-fn_sel_"]) {
                        gap: 0 !important;
                    }
                    div[class*="st-key-fn_sel_"] button {
                        border-radius: 0 !important;
                        border: 0.5px solid #c9c2e6 !important;
                        background: #ffffff !important;
                        color: #534AB7 !important;
                        box-shadow: none !important;
                        font-weight: 500 !important;
                        min-height: 1.85rem !important;
                        height: 1.85rem !important;
                    }
                    div[class*="st-key-fn_sel_"] button p { font-size: 0.72rem !important; }
                    div[class*="st-key-fn_sel_"] button:disabled {
                        color: #b3aecb !important;
                        background: #f7f6fb !important;
                    }
                    div[class*="st-key-fn_sel_all_"] button {
                        border-top-left-radius: 7px !important;
                        border-bottom-left-radius: 7px !important;
                    }
                    div[class*="st-key-fn_sel_clear_"] button {
                        border-top-right-radius: 7px !important;
                        border-bottom-right-radius: 7px !important;
                        color: #8b8898 !important;
                    }
                    </style>
                    """,
                    unsafe_allow_html=True,
                )

                # 🌐 FN TAB — 3-section model (May 16 2026):
                #   Pending  = no fn_posted_ts (not yet posted to FN)
                #   Posted   = fn_posted_ts set, but fn_provider not yet filled
                #   Assigned = fn_provider filled (ready to move to Accepted)
                # Selecting one-by-one was tedious, so we replaced the bare multiselect
                # with section-scoped "Select all" buttons that bulk-populate it.
                # All four bulk actions (CSV / Mark Posted / Assigned → Accepted /
                # Revert to Pending) operate on the same selection but each filters
                # to the routes whose state makes the action meaningful.
                _fn_exported = st.session_state.setdefault('_fn_exported', {})
                _fn_posted_dict = st.session_state.setdefault('_fn_posted', {})
                _fn_provider_dict = st.session_state.setdefault('_fn_provider', {})
                _fn_assigned_dict = st.session_state.setdefault('_fn_assigned', {})

                # Partition routes into Pending / Posted / Assigned
                _fn_route_lookup = {}    # hash -> cluster
                _fn_pending_hashes = []
                _fn_posted_hashes = []
                _fn_assigned_hashes = []
                for _fc in sorted_fn:
                    _fc_tids = sorted([str(_t['id']).strip() for _t in _fc.get('data', [])])
                    if not _fc_tids:
                        continue
                    _fc_hash = hashlib.md5("".join(_fc_tids).encode()).hexdigest()
                    _fn_route_lookup[_fc_hash] = _fc
                    _has_provider = bool((_fn_provider_dict.get(_fc_hash) or '').strip())
                    _is_posted = _fc_hash in _fn_posted_dict
                    if _has_provider:
                        _fn_assigned_hashes.append(_fc_hash)
                    elif _is_posted:
                        _fn_posted_hashes.append(_fc_hash)
                    else:
                        _fn_pending_hashes.append(_fc_hash)
                _fn_all_hashes = _fn_pending_hashes + _fn_posted_hashes + _fn_assigned_hashes

                _fn_select_key = f"fn_combined_select_{pod_name}"
                # 🐛 FIX (Sep 2026 — StreamlitAPIException: "cannot be modified
                # after the widget ... is instantiated"). The bulk "Posted" /
                # "Assigned" buttons below used to clear the selection with
                # `st.session_state[_fn_select_key] = []` directly in their
                # click handler -- but that runs AFTER the st.multiselect(key=
                # _fn_select_key) call further down has already instantiated
                # the widget for this script run, which Streamlit disallows.
                # Fix: those handlers now only set this "pending clear" flag
                # (an ordinary, non-widget key -- safe to set any time) and
                # call st.rerun(); the actual reset happens HERE, before the
                # multiselect widget is instantiated, on the fresh run that
                # follows.
                _fn_select_reset_key = f"_fn_select_reset_pending_{pod_name}"
                if st.session_state.pop(_fn_select_reset_key, False):
                    st.session_state[_fn_select_key] = []
                elif _fn_select_key not in st.session_state:
                    st.session_state[_fn_select_key] = []

                # Short per-route label for the multiselect chip list.
                def _fn_label_for(h):
                    _c = _fn_route_lookup.get(h, {})
                    _prov = (_fn_provider_dict.get(h) or '').strip()
                    if h in _fn_assigned_hashes:
                        _tag = '🌐'
                    elif h in _fn_posted_hashes:
                        _tag = '📤'
                    else:
                        _tag = '📋'
                    _prov_part = f" · {_prov}" if _prov else ''
                    return f"{_tag} {_c.get('city','?')}, {_c.get('state','?')} · {_c.get('stops',0)}st{_prov_part}"

                # ── COMPACT BULK-SELECT BAR ─────────────────────────────────
                # One-line title, no helper subtitle (it was bossy and ate vertical
                # real estate). The buttons below are self-explanatory.
                st.markdown(
                    "<div style='background:#fef9c3;border-left:3px solid #facc15;border-radius:6px;"
                    "padding:4px 10px;margin:6px 0;font-size:10px;font-weight:900;color:#854d0e;"
                    "text-transform:uppercase;letter-spacing:0.08em;'>📦 Bulk Select & Act</div>",
                    unsafe_allow_html=True,
                )

                # Section-scoped "Select all" callbacks (mutate widget state pre-render).
                def _fn_set_selection(_hashes):
                    st.session_state[_fn_select_key] = list(_hashes)

                # Compact section toggles. Shorter labels + slimmer Clear column so
                # nothing wraps. Format: "Label · N" so the count is on the same line.
                _sel_cols = st.columns([1, 1, 1, 1, 0.7])
                with _sel_cols[0]:
                    st.button(
                        f"All · {len(_fn_all_hashes)}",
                        key=f"fn_sel_all_{pod_name}",
                        use_container_width=True,
                        on_click=_fn_set_selection,
                        args=(_fn_all_hashes,),
                        disabled=not _fn_all_hashes,
                    )
                with _sel_cols[1]:
                    st.button(
                        f"Pending · {len(_fn_pending_hashes)}",
                        key=f"fn_sel_pending_{pod_name}",
                        use_container_width=True,
                        on_click=_fn_set_selection,
                        args=(_fn_pending_hashes,),
                        disabled=not _fn_pending_hashes,
                    )
                with _sel_cols[2]:
                    st.button(
                        f"Posted · {len(_fn_posted_hashes)}",
                        key=f"fn_sel_posted_{pod_name}",
                        use_container_width=True,
                        on_click=_fn_set_selection,
                        args=(_fn_posted_hashes,),
                        disabled=not _fn_posted_hashes,
                    )
                with _sel_cols[3]:
                    st.button(
                        f"Assigned · {len(_fn_assigned_hashes)}",
                        key=f"fn_sel_assigned_{pod_name}",
                        use_container_width=True,
                        on_click=_fn_set_selection,
                        args=(_fn_assigned_hashes,),
                        disabled=not _fn_assigned_hashes,
                    )
                with _sel_cols[4]:
                    st.button(
                        "Clear",
                        key=f"fn_sel_clear_{pod_name}",
                        use_container_width=True,
                        on_click=_fn_set_selection,
                        args=([],),
                    )

                st.multiselect(
                    "Selected FN routes",
                    options=_fn_all_hashes,
                    format_func=_fn_label_for,
                    key=_fn_select_key,
                    label_visibility="collapsed",
                    placeholder="Section buttons above bulk-select. Type here to fine-tune.",
                )
                _fn_selected = st.session_state.get(_fn_select_key, []) or []

                # Per-section subsets for context-aware bulk actions.
                _sel_pending = [h for h in _fn_selected if h in _fn_pending_hashes]
                _sel_posted = [h for h in _fn_selected if h in _fn_posted_hashes]
                _sel_assigned = [h for h in _fn_selected if h in _fn_assigned_hashes]
                _sel_revertable = _sel_posted + _sel_assigned

                st.caption(
                    f":gray[Selected **{len(_fn_selected)}** — "
                    f"📋 {len(_sel_pending)} · 📤 {len(_sel_posted)} · 🌐 {len(_sel_assigned)}]"
                )

                # ── BULK ACTION ROW 1: Combined CSV + Open FN link ──────────
                # Disabled buttons just show "Combined CSV" instead of a wordy
                # "(none selected)" suffix — the count IS the empty-state signal.
                _action_cols = st.columns(2)
                with _action_cols[0]:
                    if _fn_selected:
                        _fn_to_export = []
                        for _h in _fn_selected:
                            _cluster = _fn_route_lookup.get(_h)
                            if _cluster:
                                _ann = dict(_cluster)
                                _ann['_cluster_hash'] = _h
                                _fn_to_export.append(_ann)
                        try:
                            _fn_combined_buf, _fn_combined_stops, _fn_included_hashes = generate_combined_fn_upload(_fn_to_export)
                        except Exception as _fe:
                            _fn_combined_buf, _fn_combined_stops, _fn_included_hashes = None, 0, []
                            _log_err("fn_combined", _fe)
                        if _fn_combined_buf is None:
                            st.button(
                                "Combined CSV",
                                disabled=True,
                                use_container_width=True,
                                key=f"fn_dl_disabled_{pod_name}",
                            )
                        else:
                            if st.download_button(
                                label="Combined CSV",
                                data=_fn_combined_buf,
                                file_name=f"FN_Combined_{datetime.now().strftime('%m%d%Y_%H%M')}_{len(_fn_included_hashes)}routes.csv",
                                mime="text/csv",
                                key=f"fn_dl_combined_{pod_name}",
                                use_container_width=True,
                            ):
                                _ts = datetime.now().strftime('%m/%d %I:%M %p')
                                for _h in _fn_included_hashes:
                                    _fn_exported[_h] = _ts
                                st.session_state['_fn_exported'] = _fn_exported
                                st.toast(f"📥 Combined CSV: {len(_fn_included_hashes)} routes · {_fn_combined_stops} stops")
                    else:
                        st.button(
                            "Combined CSV",
                            disabled=True,
                            use_container_width=True,
                            key=f"fn_dl_empty_{pod_name}",
                        )
                with _action_cols[1]:
                    st.link_button(
                        "Field Nation",
                        url="https://app.fieldnation.com/projects",
                        use_container_width=True,
                    )

                # BULK FN ACTIONS -- Mark Posted + Move to Accepted paired into
                # one 2-up row to keep the bulk bar compact. (May 23 2026.)
                _fn_act_row = st.columns(2)
                with _fn_act_row[0]:
                    if _sel_pending:
                        if st.button(
                            "Posted",
                            key=f"fn_post_btn_{pod_name}",
                            use_container_width=True,
                            type="secondary",
                        ):
                            _ts = datetime.now().strftime('%m/%d %I:%M %p')
                            for _h in _sel_pending:
                                _fn_posted_dict[_h] = _ts
                            st.session_state['_fn_posted'] = _fn_posted_dict
                            try:
                                _hashes_csv = ",".join(_sel_pending)
                                threading.Thread(
                                    target=lambda: requests.post(
                                        GAS_WEB_APP_URL,
                                        json={"action": "markFNPosted", "auth_secret": GAS_AUTH, "cluster_hash": _hashes_csv},
                                        timeout=15,
                                    ),
                                    daemon=True,
                                ).start()
                            except Exception as _fpe:
                                _log_err("markFNPosted/pod", _fpe)
                            # --- Phase 2 migration: best-effort Postgres mirror (2026-09-21) ---
                            if DB_ENGINE is not None:
                                for _pmh in _sel_pending:
                                    try:
                                        _da.mirror_mark_fn_posted_by_cluster_hash(DB_ENGINE, _pmh)
                                    except Exception as _dw_e:
                                        _log_err(f"pg_dual_write_mark_fn_posted/{pod_name}", _dw_e)
                            st.session_state[_fn_select_reset_key] = True
                            st.toast(f"📤 Marked {len(_sel_pending)} pending route(s) as Posted to FN.")
                            st.rerun()
                    else:
                        st.button(
                            "Posted",
                            key=f"fn_post_btn_empty_{pod_name}",
                            use_container_width=True,
                            disabled=True,
                        )
                with _fn_act_row[1]:
                    if _sel_assigned:
                        if st.button(
                            "Assigned",
                            key=f"fn_assigned_bulk_{pod_name}",
                            use_container_width=True,
                        ):
                            _ts_now = datetime.now().strftime('%m/%d %I:%M %p')
                            def _fire_assigned(_h):
                                try:
                                    _r = requests.post(
                                        GAS_WEB_APP_URL,
                                        json={"action": "markFNAssigned", "auth_secret": GAS_AUTH, "cluster_hash": _h},
                                        timeout=90,  # see comment on per-route call above
                                    ).json()
                                    return (_h, bool(_r.get("success")), _r.get("error", ""))
                                except Exception as _ex:
                                    return (_h, False, str(_ex))
                            _ok = 0
                            _fail = 0
                            with st.spinner(f"Marking {len(_sel_assigned)} route(s) as Assigned FN Rep..."):
                                with ThreadPoolExecutor(max_workers=min(8, max(1, len(_sel_assigned)))) as _ex:
                                    _results = list(_ex.map(_fire_assigned, _sel_assigned))
                                for _h, _success, _err in _results:
                                    if _success:
                                        _ok += 1
                                        _fn_assigned_dict[_h] = _ts_now
                                        st.session_state[f"contractor_{_h}"] = "Field Nation"
                                        st.session_state.pop(f"route_state_{_h}", None)
                                        # NOT reverted — see per-route handler (May 23 2026).
                                        st.session_state[f"reverted_{_h}"] = False
                                        # Phase 2 migration: best-effort Postgres mirror (2026-09-21) — see per-route handler above.
                                        if DB_ENGINE is not None:
                                            try:
                                                _da.mirror_mark_fn_assigned_by_cluster_hash(DB_ENGINE, _h)
                                            except Exception as _dw_e:
                                                _log_err(f"pg_dual_write_mark_fn_assigned/{pod_name}", _dw_e)
                                    else:
                                        _fail += 1
                                        _log_err(f"bulk markFNAssigned/{pod_name} hash={_h}", _err)
                            st.session_state['_fn_assigned'] = _fn_assigned_dict
                            fetch_sent_records_from_sheet.clear()
                            st.session_state[_fn_select_reset_key] = True
                            if _fail == 0:
                                st.toast(f"✅ {_ok} route(s) moved to Accepted.")
                            elif _ok == 0:
                                st.error(f"All {_fail} GAS calls failed. Check logs.")
                            else:
                                st.toast(f"⚠️ {_ok} succeeded, {_fail} failed. Check logs for details.")
                            st.rerun()
                    else:
                        st.button(
                            "Assigned",
                            key=f"fn_assigned_bulk_empty_{pod_name}",
                            use_container_width=True,
                            disabled=True,
                        )

                st.divider()

                # Render the 3 buckets in order: Pending → Posted → Assigned.
                # Each gets its own banner; section transitions reset the state header.
                _fn_pending_list  = [_fn_route_lookup[h] for h in _fn_pending_hashes]
                _fn_posted_list   = [_fn_route_lookup[h] for h in _fn_posted_hashes]
                _fn_assigned_list = [_fn_route_lookup[h] for h in _fn_assigned_hashes]
                _fn_ordered = _fn_pending_list + _fn_posted_list + _fn_assigned_list

                def _fn_section_banner(_label, _count, _bg, _border, _fg, _emoji, _margin_top="6px"):
                    st.markdown(
                        f"<div class='dcc-collapse-boundary' style='background:{_bg}; border-left:3px solid {_border}; "
                        f"padding:6px 12px; margin:{_margin_top} 0 4px 0; border-radius:6px;'>"
                        f"<span style='font-size:11px; font-weight:900; color:{_fg}; "
                        f"text-transform:uppercase; letter-spacing:0.08em;'>{_emoji} {_label} · "
                        f"{_count} route(s)</span></div>",
                        unsafe_allow_html=True,
                    )

                if _fn_pending_list:
                    _fn_section_banner("Pending Post", len(_fn_pending_list), "#fffbeb", "#f59e0b", "#92400e", "📋")

                current_state = None
                _last_section = 'pending' if _fn_pending_list else ('posted' if _fn_posted_list else 'assigned')
                _seen_posted_banner = False
                _seen_assigned_banner = False
                for i, c in enumerate(_fn_ordered):
                    _fn_h_for_badge = hashlib.md5("".join(sorted([str(_t['id']).strip() for _t in c.get('data', [])])).encode()).hexdigest()
                    # Determine which section this route lives in.
                    if _fn_h_for_badge in _fn_assigned_hashes:
                        _this_section = 'assigned'
                    elif _fn_h_for_badge in _fn_posted_hashes:
                        _this_section = 'posted'
                    else:
                        _this_section = 'pending'

                    # Section banner transitions (fired at the first row of each new section).
                    if _this_section == 'posted' and not _seen_posted_banner:
                        _fn_section_banner("Posted to FN — awaiting Provider", len(_fn_posted_list), "#ecfdf5", "#10b981", "#065f46", "📤", _margin_top="14px")
                        _seen_posted_banner = True
                        current_state = None
                    elif _this_section == 'assigned' and not _seen_assigned_banner:
                        _fn_section_banner("Assigned — ready to move to Accepted", len(_fn_assigned_list), "#dbeafe", "#3b82f6", "#1e40af", "🌐", _margin_top="14px")
                        _seen_assigned_banner = True
                        current_state = None
                    _last_section = _this_section

                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div class='dcc-state-header' data-state-key='{current_state}'><span class='dcc-state-chevron'>▸</span>📍 {current_state}</div>", unsafe_allow_html=True)

                    esc_pill = f" | ❗ {c.get('esc_count', 0)}" if c.get('esc_count', 0) > 0 else ""
                    digi_pill = " 🔌" if c.get('is_digital') else ""
                    inst_pill = f" | 🛠️ {c.get('inst_count', 0)} Installs" if c.get('inst_count', 0) > 0 else ""
                    remov_pill = f" | 🗑️ {c.get('remov_count', 0)} Removal" if c.get('remov_count', 0) > 0 else ""
                    _BOOSTED_BADGES = {'local plus': '⭐ LOCAL PLUS', 'boosted': '🔥 BOOSTED'}
                    boosted_pill = f" | {next((v for k,v in _BOOSTED_BADGES.items() if k in c.get('boosted_tag','')), '')}" if c.get('boosted_tag') and any(k in c.get('boosted_tag','') for k in _BOOSTED_BADGES) else ""

                    _fn_exp_check = "✓ " if _fn_h_for_badge in st.session_state.get('_fn_exported', {}) else ""
                    _fn_sel_check = "☑️ " if _fn_h_for_badge in _fn_selected else ""
                    # 🌐 Inject Assigned Provider into title: "🌐 FN: Jane" or "🌐 FN"
                    _fn_prov_for_title = st.session_state.get('_fn_provider', {}).get(_fn_h_for_badge, '')
                    _fn_label = format_fn_card_title(_fn_prov_for_title)
                    with st.expander(_fn_sel_check + _fn_exp_check + f"🌐 {_fn_label}{digi_pill} | {c['city']}, {c['state']} | {c['stops']} Stops{inst_pill}{remov_pill}{boosted_pill}{esc_pill}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                        # 🌟 Guarantee route_state is set before render so FN card shows
                        _fn_task_ids = [str(t['id']).strip() for t in c['data']]
                        _fn_hash = hashlib.md5("".join(sorted(_fn_task_ids)).encode()).hexdigest()
                        if not st.session_state.get(f"route_state_{_fn_hash}"):
                            st.session_state[f"route_state_{_fn_hash}"] = "field_nation"

                        # NOTE (May 17 2026): render_dispatch now renders the
                        # full Route Stops card (per-stop accordions + campaign
                        # rows) for FN routes inside its is_fn branch, matching
                        # Ready/Flagged exactly. The old custom "Route Summary"
                        # card lived here — it's been removed so the FN card
                        # uses the same canonical breakdown as Ready.

                        # 🌐 ASSIGNED PROVIDER input — shown for Posted AND Assigned
                        # routes (Pending hasn't been posted yet so no provider yet).
                        # Editing here can also promote a Posted route to Assigned or
                        # clear an Assigned route back to Posted by emptying the field.
                        # Saves via background thread to GAS setFnProvider; survives
                        # reload and travels with the route into Accepted when
                        # markFNAssigned fires.
                        if _this_section in ('posted', 'assigned'):
                            _fnprov_curr = st.session_state.get('_fn_provider', {}).get(_fn_hash, '')
                            _fnprov_key = f"fn_prov_input_{pod_name}_{_fn_hash}"
                            def _on_fn_prov_change(_h=_fn_hash, _key=_fnprov_key):
                                _val = (st.session_state.get(_key, '') or '').strip()
                                _dict = st.session_state.setdefault('_fn_provider', {})
                                _dict[_h] = _val
                                save_fn_provider(GAS_WEB_APP_URL, _h, _val, st.session_state, db_engine=DB_ENGINE)
                            st.text_input(
                                "🌐 Assigned Provider (Field Nation)",
                                value=_fnprov_curr,
                                key=_fnprov_key,
                                on_change=_on_fn_prov_change,
                                placeholder="Type the FN provider's name once they accept",
                                help="Saves automatically. Appears as 'FN: <name>' in the card title and travels with the route to Accepted.",
                            )

                        render_dispatch(i+5000, c, pod_name)
                    
        with t_digital:
            if _is_dispatch_associate():
                st.info("🔒 Field Nation Associates have access to the Field Nation tab only.")
            elif not digital_ready: st.info("No digital service tasks pending.")
            else:
                sorted_digi = group_and_sort_by_proximity(digital_ready)
                _pg_items, _pg_start = _paginate_panel(sorted_digi, f"{pod_name}_digital", per_page=25)
                current_state = None
                for i, c in enumerate(_pg_items, start=_pg_start):
                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div class='dcc-state-header' data-state-key='{current_state}'><span class='dcc-state-chevron'>▸</span>📍 {current_state}</div>", unsafe_allow_html=True)
                    _DIG_BOOSTED = {'local plus': '⭐ LOCAL PLUS', 'boosted': '🔥 BOOSTED'}
                    _dig_boosted_pill = f" | {next((v for k,v in _DIG_BOOSTED.items() if k in c.get('boosted_tag','')), '')}" if c.get('boosted_tag') and any(k in c.get('boosted_tag','') for k in _DIG_BOOSTED) else ""
                    _dig_esc_pill = f" | ❗ {c.get('esc_count', 0)}" if c.get('esc_count', 0) > 0 else ""
                    # 🌟 Digital task-type breakdown — same classification used elsewhere
                    # in the file (lines ~4463-4464 + ~4485-4486). 'offline' beats 'service'
                    # because "Digital Offline" tasks contain both words, so check Offline
                    # before Service to avoid double-counting.
                    _dig_off_n = sum(1 for _t in c['data'] if 'offline' in str(_t.get('task_type','')).lower())
                    _dig_ins_n = sum(1 for _t in c['data'] if 'ins/re' in str(_t.get('task_type','')).lower())
                    _dig_svc_n = sum(1 for _t in c['data']
                                     if 'service' in str(_t.get('task_type','')).lower()
                                     and 'offline' not in str(_t.get('task_type','')).lower())
                    _dig_type_parts = []
                    if _dig_off_n > 0: _dig_type_parts.append(f"📵 {_dig_off_n} Offline")
                    if _dig_ins_n > 0: _dig_type_parts.append(f"🔧 {_dig_ins_n} Ins/Rem")
                    if _dig_svc_n > 0: _dig_type_parts.append(f"⚙️ {_dig_svc_n} Service")
                    _dig_type_pill = f" | {' · '.join(_dig_type_parts)}" if _dig_type_parts else ""
                    with st.expander(f"🔌{c['city']}, {c['state']} | {c['stops']} Stops{_dig_type_pill}{_dig_boosted_pill}{_dig_esc_pill}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                        render_dispatch(i+7000, c, pod_name)
                    
    with col_right:
        st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_GREEN}; margin-bottom: 5px; text-align: center;'>⏳ Awaiting Confirmation</div>", unsafe_allow_html=True)
        t_sent, t_acc, t_dec, t_fin = st.tabs(["✉️ Sent", "✅ Accepted", "❌ Declined", "🏁 Finalized"])
        
        @st.fragment
        def _render_sent_panel():
            # 🚀 INSTANT RE-ROUTE — the re-route buttons below fire
            # st.rerun(scope="fragment"), which re-runs ONLY this fragment, not the
            # whole pod tab — so the route leaves the Sent list the instant the
            # button is hit. But `sent` / `sent_ghosts` were built by the
            # (non-rerunning) outer bucket-sort, so on a fragment-only rerun they
            # still include anything just re-routed. Re-filter on the reverted_
            # flag here so the just-re-routed route drops straight out of the list.
            def _is_reverted_cluster(_c):
                _tids = [str(_t['id']).strip() for _t in _c.get('data', [])]
                _h = hashlib.md5("".join(sorted(_tids)).encode()).hexdigest()
                return st.session_state.get(f"reverted_{_h}", False)
            _live_sent = [c for c in sent if not _is_reverted_cluster(c)]
            _live_ghosts = [g for g in sent_ghosts if not st.session_state.get(f"reverted_{g.get('hash','')}", False)]
            unified_sent = unify_and_sort_by_date(_live_sent, _live_ghosts, live_hashes)
            # 🚪 Close the re-route popover after a route moves. Streamlit keeps a
            # popover open when you click a button inside it; once the route is gone the
            # popover is orphaned. The re-route handlers set this flag, then this fires
            # Escape + an outside-click on the parent document to dismiss it.
            if st.session_state.pop('_close_reroute_popover', False):
                _components.html(
                    "<script>try{var d=window.parent.document;"
                    "d.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,which:27,bubbles:true}));"
                    "d.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                    "d.body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));"
                    "}catch(e){}</script>",
                    height=0,
                )
            # 🔄 POST-REVOKE RESYNC — relocated into this fragment (May 23 2026).
            # The Sent re-route now fires st.rerun(scope="fragment"), so the
            # main-script resync trigger never runs on a re-route. Emit it here
            # so the route still gets pulled back into Ready ~12s after the
            # background OnFleet unassign lands.
            if st.session_state.pop(f"_post_revoke_resync_pending_{pod_name}", False):
                _components.html(
                    f"""
                    <script>
                    (function() {{
                      parent.setTimeout(function() {{
                        try {{
                          var LK = '_dccSyncClickLock_{pod_name}';
                          var now = Date.now();
                          var last = parseInt(parent.sessionStorage.getItem(LK) || '0', 10);
                          if (now - last < 15000) return;
                          var btn = parent.document.querySelector('[class*="st-key-reopt_{pod_name}"] button');
                          if (btn) {{ parent.sessionStorage.setItem(LK, String(now)); btn.click(); }}
                        }} catch (_) {{}}
                      }}, 12000);
                    }})();
                    </script>
                    """,
                    height=0,
                )
            if not unified_sent: st.info("No pending routes sent.")
            
            current_date = None
            _pg_items, _pg_start = _paginate_panel(unified_sent, f"{pod_name}_sent")
            for i, item in enumerate(_pg_items, start=_pg_start):
                date_str = item['sort_date']
                if date_str != current_date:
                    current_date = date_str
                    st.markdown(f"<div class='dcc-state-header' data-state-key='sent-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 SENT: {current_date}</div>", unsafe_allow_html=True)
                
                if not item['is_ghost']:
                    c = item
                    ic_name = c.get('contractor_name', 'Unknown')
                    task_ids = [str(tid['id']).strip() for tid in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    comp, due = c.get('comp', 0), c.get('due', 'N/A')
                    tasks_cnt, stops_cnt = len(c['data']), c['stops']
                    wo_display = c.get('wo', ic_name)
                    _pill_sent = get_task_pill(c.get('data', []))
                    
                    exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✉️ {wo_display} | ${comp} | Due: {due}{_pill_sent}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                            _venues_html = venue_section(make_venue_details(c['data']))
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;">
    <div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;">
        <span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span>
    </div>
    <div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;">
        <div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div>
        <div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div>
        <div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div>
        <div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div>
    </div>
    <div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;">
        <div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div>
        <div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div>
        <div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div>
        <div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div>
    </div>
    {_venues_html}
</div>""", unsafe_allow_html=True)

                            # Keep email controls accessible after Generate Link
                            # moves the route into the Sent bucket.
                            _sent_outlook = st.session_state.get(f"_persisted_outlook_{cluster_hash}")
                            _sent_mailto = st.session_state.get(f"_persisted_mailto_{cluster_hash}")
                            if _sent_outlook:
                                st.markdown(f"""<div style="display:flex;margin:8px 0 4px 0;">
<a href="{_sent_outlook}" target="_blank" rel="noopener noreferrer"
style="flex:1;text-align:center;background:#633094;color:#ffffff;border:1px solid #633094;
padding:10px;border-radius:8px;font-weight:800;font-size:13px;text-decoration:none;">📬 Open Outlook</a>
</div>""", unsafe_allow_html=True)
                            if _sent_mailto:
                                st.markdown(f"""<div style="display:flex;margin:4px 0 8px 0;">
<a href="{_sent_mailto}"
style="flex:1;text-align:center;background:#ffffff;color:#633094;border:1px solid #633094;
padding:10px;border-radius:8px;font-weight:800;font-size:13px;text-decoration:none;">📨 Default Mail</a>
</div>""", unsafe_allow_html=True)

                            # Restore the Sent-route finalization checklist.
                            # Kiosk routes get the extra "Ordered Kiosk(s)" item,
                            # matching the checklist behavior used in Accepted.
                            _sent_kiosk_total = sum(
                                1 for _t in c.get('data', [])
                                if 'kiosk' in str(_t.get('task_type', '') or '').lower()
                                and 'remov' not in str(_t.get('task_type', '') or '').lower()
                            )
                            render_finalization_checklist(
                                cluster_hash,
                                pod_name,
                                "sent_chk",
                                is_fn=(ic_name == "Field Nation"),
                                has_kiosks=(_sent_kiosk_total > 0),
                            )
                    with btn_col:
                        if not _is_dispatch_associate():
                            with st.popover("↩️"):
                                st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                                if st.button("🚨 Yes, Re-Route", key=f"rev_sent_live_{cluster_hash}_{pod_name}", type="primary", use_container_width=True):
                                    move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": c})
                                    st.session_state['_close_reroute_popover'] = True
                                    st.rerun(scope="fragment")  # snap out of Sent (May 23 2026)
                else:
                    g = item
                    g_ic_name = g.get('contractor_name', 'Unknown')
                    ghost_hash = g.get('hash', f"ghost_sent_{pod_name}_{i}")  # H14 pod-scoped
                    wo_display = g.get('wo', g_ic_name)
                    comp, due = g.get('pay', 0), g.get('due', 'N/A')
                    stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                    
                    exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✉️ {wo_display} | ${comp} | Due: {due}  ·  :gray[{tasks_cnt} tasks]"):
                            raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                            if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                            else: task_locs = raw_locs
                            u_locs = list(dict.fromkeys(task_locs))
                            _gvenues_html = venue_section(make_venue_details_ghost(u_locs, stop_data=g.get('stop_data', []))) if u_locs else ""
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;">
    <div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;">
        <span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span>
    </div>
    <div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;">
        <div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div>
        <div style="font-size:14px; font-weight:800; color:#0f172a;">{g_ic_name}</div></div>
        <div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div>
        <div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div>
    </div>
    <div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;">
        <div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div>
        <div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div>
        <div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div>
        <div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div>
    </div>
    {_gvenues_html}
</div>""", unsafe_allow_html=True)
                    with btn_col:
                        if not _is_dispatch_associate():
                            with st.popover("↩️"):
                                st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{g_ic_name}</b>?</p>", unsafe_allow_html=True)
                                if st.button("🚨 Yes, Re-Route", key=f"rev_ghost_sent_{ghost_hash}", type="primary", use_container_width=True):
                                    # Jul 2 2026 — Nick: bundled ghost revoke must archive
                                    # ALL rows in the bundle so the merged tasks return to
                                    # Dispatch as ONE cluster, not multiple. _replay_bundles
                                    # will re-merge in Ready via the existing _bundle_map.
                                    _all_hashes = g.get('_merged_hashes') or [ghost_hash]
                                    for _mh in _all_hashes:
                                        move_to_dispatch(**{"cluster_hash": _mh, "ic_name": g_ic_name, "pod_name": pod_name, "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": g})
                                    st.session_state['_close_reroute_popover'] = True
                                    st.rerun(scope="fragment")  # snap out of Sent (May 23 2026)
        with t_sent:
            _render_sent_panel()
        # 🚀 Shared re-route helpers for the Accepted/Declined/Finalized panels.
        # Each panel below is now its own @st.fragment, so a re-route is a
        # fragment-scoped rerun: the route drops out instantly, with no
        # full-page flicker and no auto_sync_checker re-trigger.
        def _is_reverted_cluster(_c):
            _tids = [str(_t['id']).strip() for _t in _c.get('data', [])]
            _h = hashlib.md5("".join(sorted(_tids)).encode()).hexdigest()
            return st.session_state.get(f"reverted_{_h}", False)
        def _close_popover_if_flagged():
            if st.session_state.pop('_close_reroute_popover', False):
                _components.html(
                    "<script>try{var d=window.parent.document;"
                    "d.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,which:27,bubbles:true}));"
                    "d.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                    "d.body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));"
                    "}catch(e){}</script>",
                    height=0,
                )
        @st.fragment
        def _render_acc_panel():
            _close_popover_if_flagged()
            _live_acc = [c for c in accepted if not _is_reverted_cluster(c)]
            _live_acc_ghosts = [g for g in pod_ghosts if not st.session_state.get(f"reverted_{g.get('hash','')}", False)]
            unified_acc = unify_and_sort_by_date(_live_acc, _live_acc_ghosts, live_hashes)
            if not unified_acc: st.info("Waiting for portal acceptances...")
            
            current_date = None
            _pg_items, _pg_start = _paginate_panel(unified_acc, f"{pod_name}_acc")
            for i, item in enumerate(_pg_items, start=_pg_start):
                date_str = item['sort_date']
                if date_str != current_date:
                    current_date = date_str
                    st.markdown(f"<div class='dcc-state-header' data-state-key='accepted-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 ACCEPTED: {current_date}</div>", unsafe_allow_html=True)
                
                if not item['is_ghost']:
                    c = item
                    ic_name = c.get('contractor_name', 'Unknown')
                    task_ids = [str(tid['id']).strip() for tid in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    comp, due = c.get('comp', 0), c.get('due', 'N/A')
                    tasks_cnt, stops_cnt = len(c['data']), c['stops']
                    
                    _k_by_addr = {}
                    for _tk in c['data']:
                        if any(kw in str(_tk.get('task_type','')).lower() for kw in ['kiosk install','install']):
                            _addr = _tk['full']
                            _venue = _tk.get('venue_name', '') or _addr
                            _k_by_addr[_venue] = _k_by_addr.get(_venue, 0) + 1
                    _k_total = sum(_k_by_addr.values())
                    _k_pill = f" | 🛠️ {_k_total} Kiosk" if _k_total > 0 else ""
                    exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                    with exp_col:
                        if ic_name == "Field Nation":
                            _acc_fn_prov = st.session_state.get('_fn_provider', {}).get(cluster_hash, '')
                            _acc_fn_badge = f"🌐 {format_fn_card_title(_acc_fn_prov)} | "
                        else:
                            _acc_fn_badge = ""
                        with st.expander(_acc_fn_badge + f"✅ {c.get('wo', ic_name)} | ${comp} | Due: {due}{_k_pill}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                            u_locs = []
                            for tk in c['data']:
                                if tk['full'] not in u_locs: u_locs.append(tk['full'])
                            loc_rows = []
                            for l in u_locs:
                                _venue_key = next((_tk.get('venue_name','') for _tk in c['data'] if _tk['full'] == l and _tk.get('venue_name')), '')
                                _k_cnt = sum(1 for _tk in c['data'] if _tk['full'] == l and 'install' in str(_tk.get('task_type','')).lower())
                                _k_tag = f" <span style='color:#16a34a; font-weight:800;'>🛠️ {_k_cnt} Kiosk</span>" if _k_cnt > 0 else ""
                                _v_prefix = f"<span style='color:#94a3b8; font-weight:600;'>{_venue_key} — </span>" if _venue_key else ""
                                loc_rows.append(f"<li>{_v_prefix}{l}{_k_tag}</li>")
                            _acc_venues_html = venue_section(make_venue_details(c['data']))
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_acc_venues_html}</div>""", unsafe_allow_html=True)
                            render_finalization_checklist(cluster_hash, pod_name, "chk", is_fn=(ic_name == "Field Nation"), has_kiosks=(_k_total > 0))
                            if _k_total > 0:
                                st.link_button("🛍️ Order Kiosks on Shopify", url="https://admin.shopify.com/store/terraboost/draft_orders/new", use_container_width=True)
                    with btn_col:
                        if not _is_dispatch_associate():
                            with st.popover("↩️"):
                                st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{c.get('wo', ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                if st.button("🚨 Yes, Remove", key=f"rev_acc_{cluster_hash}_{pod_name}", type="primary", use_container_width=True):
                                    move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "cluster_data": c, "check_completed": True})
                                    st.session_state['_close_reroute_popover'] = True
                                    st.rerun(scope="app")
                else:
                    g = item
                    g_ic_name = g.get('contractor_name', 'Unknown')
                    ghost_hash = g.get('hash', f"ghost_{pod_name}_{i}")  # H14 pod-scoped
                    comp, due = g.get('pay', 0), g.get('due', 'N/A')
                    stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                    
                    exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                    with exp_col:
                        _gk_total = g.get('kCnt', 0) or 0
                        _gk_pill = f" | 🛠️ {_gk_total} Kiosk" if _gk_total > 0 else ""
                        _gacc_fn_badge = "🌐 " if g_ic_name == "Field Nation" else ""
                        with st.expander(_gacc_fn_badge + f"✅ {g.get('wo', g_ic_name)} | ${comp} | Due: {due}{_gk_pill}  ·  :gray[{tasks_cnt} tasks]"):
                            raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                            if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                            else: task_locs = raw_locs
                            u_locs = list(dict.fromkeys(task_locs))
                            _gacc_venues = venue_section(make_venue_details_ghost(u_locs, stop_data=g.get('stop_data', []))) if u_locs else ""
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{g_ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_gacc_venues}</div>""", unsafe_allow_html=True)
                            render_finalization_checklist(ghost_hash, pod_name, "g_chk", is_fn=(g_ic_name == "Field Nation"), has_kiosks=(_gk_total > 0))
                            if _gk_total > 0:
                                st.link_button("🛍️ Order Kiosks on Shopify", url="https://admin.shopify.com/store/terraboost/draft_orders/new", use_container_width=True)
                    with btn_col:
                        if not _is_dispatch_associate():
                            with st.popover("↩️"):
                                st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{g_ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{g.get('wo', g_ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                if st.button("🚨 Yes, Remove", key=f"rev_ghost_{ghost_hash}_{i}", type="primary", use_container_width=True):
                                    move_to_dispatch(**{"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": pod_name, "action_label": "Ghost Archived", "check_onfleet": True, "cluster_data": g, "check_completed": True})
                                    st.session_state['_close_reroute_popover'] = True
                                    st.rerun(scope="app")
        @st.fragment
        def _render_dec_panel():
            _close_popover_if_flagged()
            _live_dec = [c for c in declined if not _is_reverted_cluster(c)]
            unified_dec = unify_and_sort_by_date(_live_dec, [], live_hashes)
            if not unified_dec: st.info("No declined routes.")
            
            current_date = None
            _pg_items, _pg_start = _paginate_panel(unified_dec, f"{pod_name}_dec")
            for i, item in enumerate(_pg_items, start=_pg_start):
                date_str = item['sort_date']
                if date_str != current_date:
                    current_date = date_str
                    st.markdown(f"<div class='dcc-state-header' data-state-key='declined-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 DECLINED: {current_date}</div>", unsafe_allow_html=True)
                
                c = item
                ic_name = c.get('contractor_name', 'Unknown')
                task_ids = [str(tid['id']).strip() for tid in c['data']]
                cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                with exp_col:
                    comp_dec = c.get('comp', 0)
                    due_dec = c.get('due', 'N/A')
                    stops_dec, tasks_dec = c['stops'], len(c['data'])
                    _pill_dec = get_task_pill(c.get('data', []))
                    with st.expander(f"❌ {c.get('wo', ic_name)} | ${comp_dec} | Due: {due_dec}{_pill_dec}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                        u_locs_dec = list(dict.fromkeys(t['full'] for t in c['data']))
                        _dec_venues = venue_section(make_venue_details(c['data']))
                        st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_dec} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_dec} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due_dec}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp_dec}</div></div></div>{_dec_venues}</div>""", unsafe_allow_html=True)
                with btn_col:
                    if not _is_dispatch_associate():
                        with st.popover("↩️"):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                            if st.button("🚨 Yes, Remove", key=f"rev_dec_{cluster_hash}_{pod_name}", type="primary", use_container_width=True):
                                move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "cluster_data": c})
                                st.session_state['_close_reroute_popover'] = True
                                st.rerun(scope="app")
        @st.fragment
        def _render_fin_panel():
            _close_popover_if_flagged()
            _live_fin = [c for c in finalized if not _is_reverted_cluster(c)]
            _live_fin_ghosts = [g for g in finalized_ghosts if not st.session_state.get(f"reverted_{g.get('hash','')}", False)]
            unified_fin = unify_and_sort_by_date(_live_fin, _live_fin_ghosts, live_hashes)
            if not unified_fin: st.info("No finalized routes.") 
            
            current_date = None
            _pg_items, _pg_start = _paginate_panel(unified_fin, f"{pod_name}_fin")
            for i, item in enumerate(_pg_items, start=_pg_start):
                date_str = item['sort_date']
                if date_str != current_date:
                    current_date = date_str
                    st.markdown(f"<div class='dcc-state-header' data-state-key='finalized-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 FINALIZED: {current_date}</div>", unsafe_allow_html=True)
                
                if not item['is_ghost']:
                    c = item
                    ic_name = c.get('contractor_name', 'Unknown')
                    task_ids = [str(tid['id']).strip() for tid in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    comp, due = c.get('comp', 0), c.get('due', 'N/A')
                    tasks_cnt, stops_cnt = len(c['data']), c['stops']
                    
                    _fk_by_addr = {}
                    for _tk in c['data']:
                        if any(kw in str(_tk.get('task_type','')).lower() for kw in ['kiosk install','install']):
                            _addr = _tk['full']
                            _venue = _tk.get('venue_name', '') or _addr
                            _fk_by_addr[_venue] = _fk_by_addr.get(_venue, 0) + 1
                    _fk_total = sum(_fk_by_addr.values())
                    _fk_pill = f" | 🛠️ {_fk_total} Kiosk" if _fk_total > 0 else ""
                    exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"🏁 {c.get('wo', ic_name)} | ${comp} | Due: {due}{_fk_pill}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                            u_locs = []
                            for tk in c['data']:
                                if tk['full'] not in u_locs: u_locs.append(tk['full'])
                            loc_rows = []
                            for l in u_locs:
                                _fvk = next((_tk.get('venue_name','') for _tk in c['data'] if _tk['full'] == l and _tk.get('venue_name')), '')
                                _k_cnt = sum(1 for _tk in c['data'] if _tk['full'] == l and 'install' in str(_tk.get('task_type','')).lower())
                                _k_tag = f" <span style='color:#16a34a; font-weight:800;'>🛠️ {_k_cnt} Kiosk</span>" if _k_cnt > 0 else ""
                                _fv_prefix = f"<span style='color:#94a3b8; font-weight:600;'>{_fvk} — </span>" if _fvk else ""
                                loc_rows.append(f"<li>{_fv_prefix}{l}{_k_tag}</li>")
                            _fin_venues = venue_section(make_venue_details(c['data']))
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_fin_venues}</div>""", unsafe_allow_html=True)
                    with btn_col:
                        if not _is_dispatch_associate():
                            with st.popover("↩️"):
                                st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{c.get('wo', ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                if st.button("🚨 Yes, Remove", key=f"rev_fin_{cluster_hash}_{pod_name}", type="primary", use_container_width=True):
                                    move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "cluster_data": c, "check_completed": True})
                                    st.session_state['_close_reroute_popover'] = True
                                    st.rerun(scope="app")
                else:
                    g = item
                    g_ic_name = g.get('contractor_name', 'Unknown')
                    ghost_hash = g.get('hash', f"ghost_fin_{pod_name}_{i}")  # H14 pod-scoped
                    wo_display = g.get('wo', g_ic_name)
                    comp, due = g.get('pay', 0), g.get('due', 'N/A')
                    stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                    
                    exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                    with exp_col:
                        _gfk_total = g.get('kCnt', 0) or 0
                        _gfk_pill = f" | 🛠️ {_gfk_total} Kiosk" if _gfk_total > 0 else ""
                        with st.expander(f"🏁 {wo_display} | ${comp} | Due: {due}{_gfk_pill}  ·  :gray[{tasks_cnt} tasks]"):
                            raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                            if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                            else: task_locs = raw_locs
                            u_locs = list(dict.fromkeys(task_locs))
                            _gfin_venues = venue_section(make_venue_details_ghost(u_locs, stop_data=g.get('stop_data', []))) if u_locs else ""
                            g_ic_name_fin = g.get('contractor_name', 'Unknown')
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{g_ic_name_fin}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_gfin_venues}</div>""", unsafe_allow_html=True)
                    with btn_col:
                        if not _is_dispatch_associate():
                            with st.popover("↩️"):
                                st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{g_ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{g.get('wo', g_ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                if st.button("🚨 Yes, Remove", key=f"rev_ghost_fin_{ghost_hash}_{i}", type="primary", use_container_width=True):
                                    move_to_dispatch(**{"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": pod_name, "action_label": "Ghost Archived", "check_onfleet": True, "cluster_data": g, "check_completed": True})
                                    st.session_state['_close_reroute_popover'] = True
                                    st.rerun(scope="app")
        with t_acc:
            _render_acc_panel()
        with t_dec:
            _render_dec_panel()
        with t_fin:
            _render_fin_panel()
# --- LOGOUT URL HANDLER ---
# The pinned-top-right Sign-out link sets ?logout=1 on the URL (so we can keep
# the logout control as pure HTML and pin it via fixed positioning, instead of
# using an unstyleable st.button). Catch it here, before the login gate.
# (Note: _components is imported once at the top of the file, line ~248.)
try:
    if st.query_params.get("logout") == "1":
        st.session_state.pop('_auth_user', None)
        st.session_state.pop('_stay_prompt_dismissed', None)
        # Wipe the persisted-login token from localStorage so the user doesn't
        # auto-sign-in on next page load.
        _components.html(
            "<script>try{localStorage.removeItem('dcc_stay_auth');}catch(e){}</script>",
            height=0,
        )
        for _k in ("logout", "auth"):
            try: del st.query_params[_k]
            except Exception: pass
        st.rerun()
except Exception:
    pass

# --- STAY-SIGNED-IN AUTO-RESTORE ---
# 1. If localStorage has a token but URL doesn't, the JS snippet below redirects
#    with ?auth=TOKEN appended.
# 2. If the URL has ?auth=TOKEN, we validate it and auto-restore the auth user.
if '_auth_user' not in st.session_state:
    _auth_param = st.query_params.get("auth")
    if _auth_param:
        _match = _validate_stay_token(_auth_param)
        if _match:
            _u, _rec = _match
            st.session_state['_auth_user'] = {**_rec, 'username': _u}
            # Mark the popup as dismissed since this is a returning auto-login.
            st.session_state['_stay_prompt_dismissed'] = True
    else:
        # No URL param — ask the browser if it has a saved token.
        _components.html(
            """
            <script>
                try {
                    var t = localStorage.getItem('dcc_stay_auth');
                    var here = window.parent.location;
                    if (t && !new URLSearchParams(here.search).has('auth')) {
                        var sep = here.search ? '&' : '?';
                        here.replace(here.pathname + here.search + sep + 'auth=' + t + here.hash);
                    }
                } catch (e) {}
            </script>
            """,
            height=0,
        )

# Temporary workbook migration on this isolated revamp service. The form is
# disabled unless both Railway variables are set; never exposes the DB URL.
if os.environ.get("DCC_WORKBOOK_IMPORT") == "1" and st.query_params.get("import_workbook") == "1":
    import hmac as _hmac
    st.title("DCC Revamp workbook import")
    with st.form("_one_time_import_form"):
        _import_token = st.text_input("Import token", type="password")
        _import_file = st.file_uploader("WO Request workbook or DCC snapshot", type=["xlsx", "zip"])
        _import_submit = st.form_submit_button("Import selected file")
    if _import_submit:
        _expected = os.environ.get("DCC_WORKBOOK_IMPORT_TOKEN") or ""
        if not _expected or not _hmac.compare_digest(_expected, _import_token):
            st.error("Invalid import token")
        elif _import_file is None:
            st.error("Select the workbook first")
        else:
            try:
                with st.spinner("Importing and verifying records"):
                    if _import_file.name.lower().endswith(".zip"):
                        from migration.import_snapshot import import_snapshot
                        _summary = import_snapshot(_import_file.getvalue(), DATABASE_URL)
                    else:
                        from migration.import_workbook import import_workbook
                        _summary = import_workbook(_import_file.getvalue(), DATABASE_URL)
                st.success("Import complete")
                st.json(_summary)
            except Exception as _import_error:
                st.error(f"Import failed: {type(_import_error).__name__}: {_import_error}")
    st.stop()

# --- LOGIN GATE ---
# Block all downstream rendering until the user signs in. Once authenticated,
# their record sits in st.session_state['_auth_user'] for the lifetime of the
# session (until they log out or close the tab).
if '_auth_user' not in st.session_state:
    _render_login_form()
    st.stop()

# --- STAY-SIGNED-IN (SILENT) ---
# The old "Stay signed in?" modal was removed. After a fresh username/password
# login, the session is now persisted silently and automatically (no prompt) for the user
# skips the login screen next time, with no prompt. Sign-out still clears the
# localStorage token (see the logout handler above). Auto-login via a ?auth=
# URL already sets _stay_prompt_dismissed, so this block runs exactly once,
# only after a fresh credential login.
if not st.session_state.get('_stay_prompt_dismissed'):
    st.session_state['_stay_prompt_dismissed'] = True
    _u_now = st.session_state['_auth_user'].get('username', '')
    _tok = _stay_token_for(_u_now)
    if _tok:
        # Persist to localStorage AND tag the current URL so reloads keep the session.
        _components.html(
            f"<script>try{{localStorage.setItem('dcc_stay_auth','{_tok}');}}catch(e){{}}</script>",
            height=0,
        )
        st.query_params['auth'] = _tok
        st.rerun()

# --- START ---
if "ic_df" not in st.session_state:
    try:
        if DB_ENGINE is not None:
            df = _ic_df_from_db(DB_ENGINE)
        else:
            url = f"{IC_SHEET_URL.split('/edit')[0]}/export?format=csv&gid=0"
            df = pd.read_csv(url)
            # 🌟 BULLETPROOF: Lowercase all headers the second the data is downloaded
            df.columns = [str(c).strip().lower() for c in df.columns]
        st.session_state.ic_df = df
    except: st.error("Database connection failed.")


# --- CONTRACTOR ROSTER AUTO-REFRESH ---
# Railway updates Postgres in the background. DCC only needs to refresh its
# in-memory contractor roster periodically so newly-added/updated ICs appear
# without a manual sync button or page restart.
if DB_ENGINE is not None:
    _ic_refresh_now = time.time()
    _ic_last_refresh = float(st.session_state.get("_ic_db_refresh_ts", 0) or 0)
    if (_ic_refresh_now - _ic_last_refresh) >= 300:
        try:
            st.session_state.ic_df = _ic_df_from_db(DB_ENGINE)
            st.session_state["_ic_db_refresh_ts"] = _ic_refresh_now
        except Exception as _ic_refresh_exc:
            _log_err("contractor_roster_auto_refresh", _ic_refresh_exc)

# 🔵 PAGE-LOAD LAZY-INIT for worker task counts. Populates st.session_state['_worker_counts']
# on the very first render of every fresh page load, BEFORE any tab/cluster renders. The
# previous in-render_dispatch placement only fired when a pod tab was active with at least
# one cluster — so dispatchers landing on the Global tab never saw real counts. The cached
# fetch (@st.cache_data ttl=120) means this is one shared API call per worker session.
if os.environ.get("DCC_REVAMP_UI") != "1" and '_worker_counts' not in st.session_state:
    st.session_state['_worker_counts'] = fetch_worker_task_counts()

# --- HEADER ROW ---
# Pin signed-in user info to the top-right corner (same fixed-position pattern
# as the Terraboost logo top-left). Sign out is a styled <a> that hits
# ?logout=1 → handled by the LOGOUT URL HANDLER block above.
_u = st.session_state.get('_auth_user', {})
# Role label: "Pod · Role" when a role is set (e.g. "Blue · Associate"),
# otherwise just the pod (e.g. "ADMIN").
_pod_label = _u.get('pod', '?')
_role_label = _u.get('role')
_signin_line = f"{_u.get('name','?')} · {_pod_label}" + (f" {_role_label}" if _role_label else "")
_user_email = str(_u.get('email', '') or '').strip()

# Header markdown — pinned info + Sign out pill. The dispatcher email used for
# IC accept/decline notifications now comes from each user's login record
# (USERS dict), so no manual entry / pill is needed.
_pill_base = ("display: inline-block; padding: 3px 10px; background: #ffffff;"
              " border: 1px solid #cbd5e1; border-radius: 6px; text-decoration: none;"
              " font-size: 11px; font-weight: 700; line-height: 1.4; cursor: pointer;")
_email_line_html = (f'<div style="font-size: 11px; color: #64748b; font-weight: 600; margin: 0 0 4px 0;">{_user_email}</div>'
                    if _user_email else '')
st.markdown(
    f"""
    <div style="position: fixed; top: 14px; right: 64px; z-index: 999999; text-align: right;
                font-family: 'Inter', sans-serif; line-height: 1.2;">
        <div style="font-size: 10px; color: #94a3b8; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;">Signed in as</div>
        <div style="font-size: 13px; color: #0f172a; font-weight: 800; margin: 1px 0 1px 0;">{_signin_line}</div>
        {_email_line_html}
        <span style="display: inline-flex; gap: 6px; align-items: center; justify-content: flex-end;">
          <a href="?logout=1" target="_self" style="{_pill_base} color: #475569;">Sign out</a>
        </span>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown("<h1 style='color: #633094; text-align: center; margin-top: 0;'>Terraboost Media: Dispatch Command Center</h1>", unsafe_allow_html=True)

# 🔍 DEBUG: Worker task-count diagnostics — only renders when ?debug=1 is in the URL.
# Shows the raw Onfleet /workers?analytics=true response shape so we can see why
# the contractor 🔵{N} task counts are coming back as 0 for everyone. Once the
# field name / response shape is confirmed, fetch_worker_task_counts() can be
# patched and this block can stay dormant (or be removed) — it's behind a query
# param so end-users never see it.
if st.query_params.get("debug") == "1":
    with st.expander("🔍 IC ↔ Worker Phone-Match Debug", expanded=True):
        if st.button("🔗 Cross-reference IC database with Onfleet workers", key="_dbg_ic_xref"):
            try:
                _r = requests.get("https://onfleet.com/api/v2/workers?analytics=true", headers=headers, timeout=10)
                if _r.status_code != 200:
                    st.error(f"Onfleet HTTP {_r.status_code}: {_r.text[:300]}")
                else:
                    _data = _r.json()
                    # Build worker phone → count map (same logic as fetch_worker_task_counts)
                    _wcounts = {}
                    for _w in _data:
                        _wphone = re.sub(r'\D', '', str(_w.get('phone', '')))[-10:]
                        if _wphone:
                            _wcounts[_wphone] = (_w.get('name', '?'), len(_w.get('tasks') or []))
                    st.write(f"**Onfleet workers indexed by phone**: {len(_wcounts)}")

                    # Inspect IC database
                    _ic_df = st.session_state.get('ic_df', pd.DataFrame())
                    st.write(f"**IC database row count**: {len(_ic_df)}")
                    st.write(f"**IC columns (lowercased)**: {list(_ic_df.columns)}")

                    # Try to find a phone-like column
                    _phone_cols = [c for c in _ic_df.columns if 'phone' in c.lower() or 'cell' in c.lower() or 'mobile' in c.lower()]
                    st.write(f"**Candidate phone columns**: {_phone_cols}")

                    if 'phone' in _ic_df.columns:
                        _used_col = 'phone'
                    elif _phone_cols:
                        _used_col = _phone_cols[0]
                        st.warning(f"⚠️ No column named 'phone' — code is looking for 'phone' but your IC sheet has '{_used_col}'. THAT'S THE BUG.")
                    else:
                        _used_col = None
                        st.error("❌ No phone-like column found in IC database. The 🔵 badge can't work without one.")

                    if _used_col:
                        # Sample raw + normalized phones from IC
                        _samples = []
                        _matched = 0
                        _empty = 0
                        for _, _row in _ic_df.head(50).iterrows():
                            _raw = str(_row.get(_used_col, '')).strip()
                            if _raw and _raw not in ('nan', 'None'):
                                try:
                                    _raw = str(int(float(_raw)))
                                except (ValueError, TypeError):
                                    pass
                            _norm = re.sub(r'\D', '', _raw)[-10:]
                            if not _norm:
                                _empty += 1
                            elif _norm in _wcounts:
                                _matched += 1
                            _samples.append((_row.get('name', '?'), _raw, _norm, _norm in _wcounts))
                        st.write(f"**Sampled first 50 IC rows from column '{_used_col}'**:")
                        st.write(f"  • Matched in Onfleet: **{_matched}/50**")
                        st.write(f"  • Empty after normalize: **{_empty}/50**")
                        st.write("**First 10 samples** (name | raw | normalized | matched?):")
                        for _n, _r_, _nm, _m in _samples[:10]:
                            _ico = "✅" if _m else "❌"
                            st.text(f"  {_ico} {_n[:25]:25} | {_r_[:20]:20} | {_nm:12} | {_m}")

                        # Show the full count of matches across the whole IC table
                        _full_matched = 0
                        _matched_with_tasks = 0
                        _ic_match_details = []
                        for _, _row in _ic_df.iterrows():
                            _raw_b = str(_row.get(_used_col, '')).strip()
                            if _raw_b and _raw_b not in ('nan', 'None'):
                                try:
                                    _raw_b = str(int(float(_raw_b)))
                                except (ValueError, TypeError):
                                    pass
                            _norm = re.sub(r'\D', '', _raw_b)[-10:]
                            if _norm and _norm in _wcounts:
                                _full_matched += 1
                                _wname, _wcnt = _wcounts[_norm]
                                if _wcnt > 0:
                                    _matched_with_tasks += 1
                                _ic_match_details.append((_row.get('name', '?'), _wname, _wcnt))
                        st.write(f"**Total IC rows matching an Onfleet worker by phone**: **{_full_matched} / {len(_ic_df)}**")
                        st.write(f"**Matched ICs WITH tasks > 0**: **{_matched_with_tasks} / {_full_matched}**")
                        st.write("**Top 15 matched ICs by task count**:")
                        for _icn, _wn, _wc in sorted(_ic_match_details, key=lambda x: -x[2])[:15]:
                            st.text(f"  {_icn[:25]:25} (Onfleet: {_wn[:25]:25}) → {_wc} tasks")
                        # Also: peek at session_state to see if _worker_counts is populated
                        _ss_wc = st.session_state.get('_worker_counts', None)
                        if _ss_wc is None:
                            st.warning("⚠️ st.session_state['_worker_counts'] is NOT SET. fetch_worker_task_counts() hasn't been called yet — that's why all badges show 🔵0.")
                        else:
                            _ss_nonzero = sum(1 for v in _ss_wc.values() if v > 0)
                            st.success(f"✅ st.session_state['_worker_counts'] IS populated: {len(_ss_wc)} entries, {_ss_nonzero} with tasks > 0")
            except Exception as _e:
                st.error(f"{type(_e).__name__}: {_e}")
        st.caption("Click to compare IC database phones against Onfleet worker phones. Shows the bug: column name mismatch, format mismatch, or empty cells.")
        if st.button("Probe /workers?analytics=true (raw)", key="_dbg_workers_probe"):
            try:
                _r = requests.get("https://onfleet.com/api/v2/workers?analytics=true", headers=headers, timeout=10)
                st.write(f"**HTTP**: {_r.status_code}")
                if _r.status_code == 200:
                    _data = _r.json()
                    st.write(f"**Worker count**: {len(_data)}")
                    if _data:
                        _w0 = _data[0]
                        st.write("**First worker keys**:", sorted(_w0.keys()))
                        st.write("**phone**:", _w0.get('phone'))
                        st.write("**tasks (raw)**:", _w0.get('tasks'))
                        st.write("**tasks type**:", type(_w0.get('tasks')).__name__)
                        _tasks = _w0.get('tasks') or []
                        st.write("**len(tasks)**:", len(_tasks) if isinstance(_tasks, list) else "(not a list)")
                        # Show analytics blob if it exists — Onfleet sometimes nests counts there
                        st.write("**analytics field**:", _w0.get('analytics'))
                        # Sample of task counts across all workers, so we can see if SOMEONE has tasks
                        _all_counts = [(w.get('name', '?'), len(w.get('tasks') or [])) for w in _data]
                        _nonzero = [(n, c) for n, c in _all_counts if c > 0]
                        st.write(f"**Workers with tasks > 0**: {len(_nonzero)} / {len(_data)}")
                        if _nonzero:
                            st.write("**Top 5 by count**:", sorted(_nonzero, key=lambda x: -x[1])[:5])
                else:
                    st.write("**Response body**:", _r.text[:500])
            except Exception as _e:
                st.error(f"{type(_e).__name__}: {_e}")
        st.caption("Add `?debug=1` to the URL to see this panel. Click Probe to inspect Onfleet's response shape — tells us which field holds the task count and whether phone matches our IC database.")
        # ── /tasks probe — diagnoses where the art file lives & how customer
        # type is encoded. Fetches the same /tasks/all feed the app normally
        # ingests (state=0 = unassigned, last 14 days), then dumps the first
        # task as JSON, the FULL list of `customFields` names/keys/values,
        # plus a flag for whether common art-file fields appear populated.
        st.markdown("---")
        if st.button("Probe /tasks (raw — diagnose art file + national)", key="_dbg_tasks_probe"):
            try:
                _from = int((time.time() - 14 * 86400) * 1000)
                _r = requests.get(
                    f"https://onfleet.com/api/v2/tasks/all?state=0&from={_from}",
                    headers=headers, timeout=15
                )
                st.write(f"**HTTP**: {_r.status_code}")
                if _r.status_code != 200:
                    st.write("**Response body**:", _r.text[:500])
                else:
                    _body = _r.json()
                    _tasks = _body.get('tasks', []) if isinstance(_body, dict) else []
                    st.write(f"**Task count**: {len(_tasks)}")
                    if not _tasks:
                        st.write("(no tasks returned — try a wider time window)")
                    else:
                        _t0 = _tasks[0]
                        st.write("**First task — top-level keys**:", sorted(_t0.keys()))
                        st.write("**`notes`**:", repr(_t0.get('notes'))[:300])
                        st.write("**`taskDetails`**:", repr(_t0.get('taskDetails'))[:300])
                        _cf = _t0.get('customFields') or _t0.get('container', {}).get('customFields') or []
                        st.write(f"**customFields count**: {len(_cf)}")
                        if _cf:
                            st.write("**customFields (name / key / value)**:")
                            for _f in _cf:
                                st.write(f"  - name='{_f.get('name','')}', key='{_f.get('key','')}' -> {repr(_f.get('value',''))[:200]}")
                        _national_t = None
                        for _t in _tasks:
                            _cf2 = _t.get('customFields') or _t.get('container', {}).get('customFields') or []
                            for _f in _cf2:
                                if 'national' in str(_f.get('value', '')).lower():
                                    _national_t = _t
                                    break
                            if _national_t:
                                break
                        st.markdown("---")
                        if _national_t:
                            st.write("**Found a likely-National task**: id =", _national_t.get('id') or _national_t.get('shortId'))
                            st.write("**`notes`**:", repr(_national_t.get('notes'))[:300])
                            st.write("**`taskDetails`**:", repr(_national_t.get('taskDetails'))[:300])
                            _cf_n = _national_t.get('customFields') or _national_t.get('container', {}).get('customFields') or []
                            st.write(f"**customFields count**: {len(_cf_n)}")
                            for _f in _cf_n:
                                st.write(f"  - name='{_f.get('name','')}', key='{_f.get('key','')}' -> {repr(_f.get('value',''))[:200]}")
                        else:
                            st.write("**No national task found in current pool** — every task's customFields lacked the word 'national'.")
            except Exception as _e:
                st.error(f"{type(_e).__name__}: {_e}")
        st.caption("Click to see what fields OnFleet actually returns on tasks. Tells us where the art file lives and whether National-vs-Local detection has anything to match against.")

# 🔬 DEBUG: Postgres read-side reconstruction preview (Step 8, 2026-09-22) —
# only renders when ?debug=readside is in the URL AND the viewer is
# ADMIN/MANAGER (this one touches real route data, unlike the Onfleet-only
# panel above, so it gets the stricter gate the FN/bucket debug panels use).
# Read-only and button-triggered: runs migration.data_access.get_sent_records_from_db()
# against Postgres and compares it with fetch_sent_records_from_sheet() (the
# live path) side-by-side. Never assigns into st.session_state beyond what
# fetch_sent_records_from_sheet() already does on every normal render, and
# never changes anything a dispatcher sees elsewhere on the page — purely a
# diagnostic, ahead of any real cutover decision. See migration/README.md
# "Step 8" for what this is checking and why it isn't wired to the live view.
if st.query_params.get("debug") == "readside" and _is_admin_or_manager():
    with st.expander("🔬 Read-side reconstruction preview — Postgres vs Sheets (Step 8)", expanded=True):
        if DB_ENGINE is None:
            st.info(
                "DATABASE_URL isn't set for this deploy, so there's no Postgres "
                "data to compare yet. This panel has nothing to show until "
                "DB_ENGINE is live."
            )
        else:
            st.caption(
                "Read-only comparison. Runs get_sent_records_from_db() against Postgres and "
                "fetch_sent_records_from_sheet() against the live Sheets, then diffs them. "
                "Doesn't change anything a dispatcher sees — purely diagnostic."
            )
            if st.button("Run comparison", key="_dbg_readside_run"):
                try:
                    sheet_sent, sheet_ghost, sheet_archived, sheet_hist = fetch_sent_records_from_sheet()
                    db_sent, db_ghost, db_archived, db_hist = _da.get_sent_records_from_db(
                        DB_ENGINE, POD_CONFIGS, STATE_MAP, cutoff_date=MIGRATION_CUTOFF_DATE
                    )

                    def _ghost_count(gd):
                        # Skip non-list side-channel keys (_fn_posted, _fn_provider,
                        # _archive_read_failed) — same convention both paths use to
                        # park extras on the ghost_routes dict.
                        return sum(len(v) for v in gd.values() if isinstance(v, list))

                    st.write("**Top-line counts (Sheets vs Postgres)**:")
                    _count_rows = [
                        ("sent_dict (tasks)", len(sheet_sent), len(db_sent)),
                        ("ghost_routes (routes)", _ghost_count(sheet_ghost), _ghost_count(db_ghost)),
                        ("archived_wos", len(sheet_archived), len(db_archived)),
                        ("history_db (tasks with history)", len(sheet_hist), len(db_hist)),
                    ]
                    st.table(pd.DataFrame(_count_rows, columns=["", "Sheets", "Postgres"]))
                    st.caption(
                        "Expect Postgres counts to run well below Sheets today — Postgres only has "
                        "what's been dual-written since Step 6/7 landed (2026-09-20 onward), plus "
                        "whatever's been backfilled by import_from_sheets.py. A gap alone isn't a "
                        "bug; it's what 'still accumulating, not yet backfilled' looks like."
                    )

                    # Cross-check the tasks BOTH paths already know about (both sides
                    # already filter to MIGRATION_CUTOFF_DATE, so this isn't a
                    # freshness question) — this is the part that would catch a real
                    # logic divergence between the two implementations, not just a
                    # data-coverage gap.
                    _sheet_tids = set(sheet_sent.keys())
                    _db_tids = set(db_sent.keys())
                    _only_sheet = _sheet_tids - _db_tids
                    _only_db = _db_tids - _sheet_tids
                    _both = _sheet_tids & _db_tids
                    st.write(
                        f"**Task IDs**: {len(_both)} in both, {len(_only_sheet)} Sheets-only, "
                        f"{len(_only_db)} Postgres-only."
                    )

                    _mismatches = []
                    for tid in _both:
                        s, d = sheet_sent[tid], db_sent[tid]
                        if s.get("status") != d.get("status") or s.get("wo") != d.get("wo"):
                            _mismatches.append({
                                "task_id": tid,
                                "sheet_status": s.get("status"), "db_status": d.get("status"),
                                "sheet_wo": s.get("wo"), "db_wo": d.get("wo"),
                            })
                    if _mismatches:
                        st.warning(f"⚠️ {len(_mismatches)} task(s) exist in both but disagree on status/WO — worth a look:")
                        st.dataframe(pd.DataFrame(_mismatches).head(50))
                    elif _both:
                        st.success(f"✅ All {len(_both)} tasks present in both paths agree on status and WO.")
                    else:
                        st.caption("No overlapping task IDs yet to cross-check.")

                    if st.checkbox("Show raw Postgres ghost_routes by pod", key="_dbg_readside_raw"):
                        for pod, entries in db_ghost.items():
                            if not isinstance(entries, list) or not entries:
                                continue
                            st.write(f"**{pod}** ({len(entries)}):")
                            st.dataframe(pd.DataFrame(entries).head(25).reindex(
                                columns=["wo", "status", "city", "state", "route_ts"]
                            ))
                except Exception as _e:
                    st.error(f"{type(_e).__name__}: {_e}")
            st.caption("Add `?debug=readside` to the URL (ADMIN/MANAGER only) to see this panel.")

# Security audit M13 - reset the once-per-render sync-check guard. This line
# runs exactly once per Streamlit script run (it is in the linear flow, not
# inside any loop/function), so auto_sync_checker fires once even for the
# admin/manager 5-pod view.
st.session_state['_asc_ran_this_render'] = False

# Isolated revamp deployment: show a single-route dispatch workspace by
# default, while keeping every existing DCC screen and action under Full tools.
# Production DCC never sets this flag and never imports the new UI module.
if os.environ.get("DCC_REVAMP_UI") == "1":
    from revamp_workspace import render_workspace
    render_workspace(_can_access_tab, process_pod, render_dispatch,
                     haversine, DB_ENGINE, assign_tasks_to_fn_team,
                     fetch_sent_records_from_sheet, DEFAULT_DUE_DAYS,
                     _fn_ghost_to_cluster)
    st.stop()

# Updated Main Tabs
# --- POD-LOCKED LANDING ---
# Single-pod dispatchers + Associates (Blue/Green/Orange/Purple/Red) only see
# their assigned pod tab — they land directly on it (no Global gate to click
# past). Admin/Manager AND Digital users fall through to the 7-tab UI below;
# the per-tab gate blocks every tab they can't access.
_locked_pod = _user_pod_raw()
if (_locked_pod.upper() not in ('ADMIN', 'MANAGER', 'ALL')
        and _locked_pod != 'Digital'):
    _locked_tabs = st.tabs([f"{_locked_pod} Pod"])
    with _locked_tabs[0]:
        run_pod_tab(_locked_pod)
    st.stop()

tabs = st.tabs(["Global", "Blue Pod", "Green Pod", "Orange Pod", "Purple Pod", "Red Pod", "Digital"])

# ── TAB PERSISTENCE: keep the user on whatever tab they were on across deploys.
# Streamlit's st.tabs() is stateless and resets to tab[0] on every fresh server
# start (which is what a Railway redeploy looks like — session_state wiped, but
# the URL ?auth= survives so the user stays signed in). We layer URL-param
# persistence on top: every click on a tab writes ?tab=<name> via history.replaceState
# (no navigation), and on first paint we click whichever tab matches ?tab=.
_persist_target = st.query_params.get("tab", "")
_components.html(
    f"""
    <script>
    (function() {{
      var parentDoc, parentWin;
      try {{ parentWin = window.parent; parentDoc = parentWin.document; }} catch(e) {{ return; }}
      var TARGET = {repr(str(_persist_target))};
      var LS_LAST_TAB = "dcc_last_tab";

      // Fallback: if URL has no ?tab= param, check localStorage for the last
      // tab the user clicked. This catches the case where they hard-reload
      // (Ctrl+R) before clicking any tab on a fresh sign-in.
      if (!TARGET) {{
        try {{
          var lsTab = parentWin.localStorage.getItem(LS_LAST_TAB);
          if (lsTab) {{
            TARGET = lsTab;
            // Also write it to the URL so subsequent navigation matches.
            try {{
              var u0 = new URL(parentWin.location.href);
              u0.searchParams.set('tab', lsTab);
              parentWin.history.replaceState({{}}, '', u0.toString());
            }} catch(_) {{}}
          }}
        }} catch(_) {{}}
      }}

      // Restore the last-active tab on page load.
      function restoreTab() {{
        if (!TARGET) return true;
        var btns = parentDoc.querySelectorAll('button[role="tab"]');
        for (var i = 0; i < btns.length; i++) {{
          var label = (btns[i].innerText || '').trim();
          if (label === TARGET) {{
            if (btns[i].getAttribute('aria-selected') !== 'true') {{
              try {{ btns[i].click(); }} catch(_) {{}}
            }}
            return true;
          }}
        }}
        return false;
      }}
      var attempts = 0;
      var iv = parentWin.setInterval(function() {{
        attempts++;
        if (restoreTab() || attempts > 25) parentWin.clearInterval(iv);
      }}, 120);

      // Persist tab clicks to URL so the next reload (deploy or otherwise) lands here.
      if (!parentWin._dccTabPersist) {{
        parentWin._dccTabPersist = true;
        parentDoc.addEventListener('click', function(e) {{
          var t = e.target && e.target.closest && e.target.closest('button[role="tab"]');
          if (!t) return;
          var name = (t.innerText || '').trim();
          if (!name) return;
          try {{
            var u = new URL(parentWin.location.href);
            u.searchParams.set('tab', name);
            parentWin.history.replaceState({{}}, '', u.toString());
            try {{ parentWin.localStorage.setItem("dcc_last_tab", name); }} catch(_) {{}}
          }} catch(_) {{}}
        }}, true);
      }}
    }})();
    </script>
    """,
    height=0,
)

def _render_global_tab_body():
    # Check if ANY pod is loaded to toggle button state
    has_global_data = any(f"clusters_{p}" in st.session_state for p in POD_CONFIGS.keys())
    
    # 🌟 NEW HEADER: Title Centered, Dynamic Button Top Right
    gh_col1, gh_col2, gh_col3 = st.columns([2, 6, 2])
    with gh_col2:
        st.markdown("<h2 style='color: #633094; text-align:center; margin-top: 0;'>🌍 Global Overview</h2>", unsafe_allow_html=True)
    with gh_col3:
        st.markdown("<div class='tab-action-btn'>", unsafe_allow_html=True)
        btn_label = "🚀 Sync Routes" if has_global_data else "🚀 Initialize All Pods"
        if st.button(btn_label, key="global_init_btn", use_container_width=True):
            st.session_state.sent_db, st.session_state.ghost_db, st.session_state['archived_wos'], st.session_state['_history_db'] = fetch_sent_records_from_sheet()
            st.session_state.trigger_pull = True
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("---")
    loading_placeholder = st.empty()
    bar_placeholder = st.empty()
    if not has_global_data:
        st.info("No operational data initialized. Click '🚀 Initialize All Pods' at the top right to fetch tasks for the 5 static pods. The Digital tab initializes separately.")

    if st.session_state.get("trigger_pull"):
        st.markdown("<style>.pod-card-pill { opacity: 0.35 !important; filter: grayscale(40%) !important; pointer-events: none !important; transition: opacity 0.3s ease !important; }</style>", unsafe_allow_html=True)

    cols = st.columns(len(POD_CONFIGS))
    pod_keys = list(POD_CONFIGS.keys())
    global_map = folium.Map(location=[39.8283, -98.5795], zoom_start=4, tiles="cartodbpositron")
    current_sent_db, ghost_db, _archived_wos, _history_db = fetch_sent_records_from_sheet()
    st.session_state['_history_db'] = _history_db
    st.session_state['archived_wos'] = _archived_wos

    for i, pod in enumerate(pod_keys):
        colors = {
            "Blue":   {"border": "#3b82f6", "bg": "#f0f7ff", "text": "#1e3a8a"},
            "Green":  {"border": "#22c55e", "bg": "#f0fdf4", "text": "#064e3b"},
            "Orange": {"border": "#f97316", "bg": "#fffaf5", "text": "#7c2d12"},
            "Purple": {"border": "#a855f7", "bg": "#faf5ff", "text": "#4c1d95"},
            "Red":    {"border": "#ef4444", "bg": "#fef2f2", "text": "#7f1d1d"}
        }.get(pod)
        
        with cols[i]:
            is_loading = st.session_state.get("current_loading_pod") == pod
            has_data = f"clusters_{pod}" in st.session_state
            
            if is_loading:
                card_content = f"<p class='loading-pulse' style='color:{colors['border']}; margin-top:25px;'>📡 SYNCING...</p>"
            elif has_data:
                # 🌟 GLOBAL TAB = STATIC ONLY: Digital clusters are tracked in the
                # dedicated Digital tab (Tab 6) and have their own ghost/sent/accepted
                # counters there. Filtering them out here keeps the supercard math
                # (Routes Sent / Accepted / Declined / Tasks / Stops) and the master
                # map markers focused on static-route operations only.
                pod_cls = [c for c in st.session_state[f"clusters_{pod}"] if not c.get('is_digital')]
                total_routes = len(pod_cls)
                total_tasks = sum(len(c['data']) for c in pod_cls)
                total_stops = sum(c['stops'] for c in pod_cls)
                
                # 🌟 THE FIX: Initialize all required lists for the Global summary
                sent, accepted, declined, field_nation, ready, review, finalized = [], [], [], [], [], [], []
                
                for c in pod_cls:
                    task_ids = [str(t['id']).strip() for t in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    sheet_match = current_sent_db.get(next((tid for tid in task_ids if tid in current_sent_db), None))
                    route_state = st.session_state.get(f"route_state_{cluster_hash}")
                    is_reverted = st.session_state.get(f"reverted_{cluster_hash}", False)
                    
                    # --- PRIORITY: LIVE DATABASE OVERRIDES LOCAL STATE ---
                    if sheet_match and not is_reverted:
                        raw_status = str(sheet_match.get('status', '')).lower()
                        if raw_status == 'field_nation':
                            if not st.session_state.get(f"route_state_{cluster_hash}"):
                                st.session_state[f"route_state_{cluster_hash}"] = "field_nation"
                            field_nation.append(c)
                        elif raw_status == 'declined':
                            declined.append(c)
                        elif raw_status == 'accepted':
                            accepted.append(c)
                        elif raw_status == 'finalized': 
                            finalized.append(c)
                        else:
                            sent.append(c)
                    # 🌟 Handle Local Session State
                    elif route_state == "email_sent" and not is_reverted:
                        sent.append(c)
                    elif route_state == "field_nation": 
                        field_nation.append(c)
                    elif route_state == "link_generated" and not is_reverted:
                        orig = st.session_state.get(f"orig_status_{cluster_hash}")
                        if orig == "declined":
                            declined.append(c)
                        else:
                            ready.append(c)
                    else:
                        if c.get('status') == 'Ready': 
                            ready.append(c)
                        else: 
                            review.append(c)
                
                pod_ghosts = ghost_db.get(pod, [])
                total_accepted = len(accepted) + len(pod_ghosts)
                true_sent_count = len(sent) + len(field_nation) + total_accepted + len(declined)
                visual_total_routes = len(pod_cls) + len(pod_ghosts)
                
                card_content = f"""
<p style='margin: 10px 0 0 0; font-size: 26px; font-weight: 800; color: {colors['text']};'>{true_sent_count} / {visual_total_routes}</p>
<p style='margin: -5px 0 0 0; font-size: 11px; font-weight: 700; color: {colors['text']}; opacity: 0.6; text-transform: uppercase;'>All Routes Sent</p>
<p style='margin: 2px 0 8px 0; font-size: 9px; font-weight: 700; color: {colors['text']}; opacity: 0.5;'>{total_accepted} ACCEPTED | {len(declined)} DECLINED</p>
<div style='display: flex; justify-content: space-around; border-top: 1px solid rgba(0,0,0,0.08); padding-top: 10px;'>
<div><p style='margin:0; font-size:9px; color: {colors['text']}; opacity: 0.8; font-weight: 800;'>TASKS</p><b style='color: {colors['text']};'>{total_tasks}</b></div>
<div style='border-left: 1px solid rgba(0,0,0,0.08); height: 20px;'></div>
<div><p style='margin:0; font-size:9px; color: {colors['text']}; opacity: 0.8; font-weight: 800;'>STOPS</p><b style='color: {colors['text']};'>{total_stops}</b></div>
</div>
"""
                for c in pod_cls: folium.CircleMarker(c['center'], radius=5, color=colors['border'], fill=True, fill_opacity=0.7).add_to(global_map)
            else:
                card_content = f"<p style='color: {colors['text']}; opacity: 0.3; font-weight: 800; margin-top: 30px;'>OFFLINE</p>"

            st.markdown(f"""
<div class="pod-card-pill" style="border: 2px solid {colors['border']}; border-radius: 30px; padding: 20px 10px; background-color: {colors['bg']}; text-align: center; height: 190px; box-shadow: 0 4px 10px rgba(0,0,0,0.03); display: flex; flex-direction: column; justify-content: center;">
<div style="margin: 0; color: {colors['text']}; font-weight: 800; font-size: 1.2rem;">{pod} Pod</div>
{card_content}
</div>
""", unsafe_allow_html=True)
            
    if st.session_state.get("trigger_pull"):
        import time as _time
        _g_start = _time.time()

        def _render_global_card(overlay, msg, start):
            elapsed = int(_time.time() - start)
            m = elapsed // 60; s = elapsed % 60
            overlay.markdown(f"""
                <style>
                    @keyframes spin {{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
                    .dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;
                        padding:36px 32px;text-align:center;margin:20px 0;}}
                    .dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;
                        border-top:4px solid #633094;border-radius:50%;
                        animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
                    .dcc-pill{{display:inline-block;font-size:13px;font-weight:700;
                        color:#633094;background:#f3e8ff;border-radius:20px;
                        padding:4px 14px;margin-top:12px;}}
                </style>
                <div class='dcc-card'>
                    <div class='dcc-spin'></div>
                    <p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Initializing All Pods</p>
                    <p style='font-size:13px;color:#64748b;margin:0 0 8px 0;'>{msg}</p>
                    <div class='dcc-pill'>⏱ {m}:{s:02d}</div>
                <div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: 2–4 min for full pod fleet (Red Pod is heaviest).</div>
                </div>
            """, unsafe_allow_html=True)

        _g_overlay = loading_placeholder.empty()
        st.session_state['_loading_overlay'] = _g_overlay
        st.session_state['_loading_start'] = _g_start
        st.session_state['_loading_pod'] = 'Global'

        _render_global_card(_g_overlay, "Loading route database...", _g_start)
        _time.sleep(0.05)
        p_bar = bar_placeholder.progress(0, text="📋 Loading route database from Google Sheets...")
        st.session_state.sent_db, st.session_state.ghost_db, st.session_state['archived_wos'], st.session_state['_history_db'] = fetch_sent_records_from_sheet()
        _render_global_card(_g_overlay, f"Fetching tasks across {len(pod_keys)} pods...", _g_start)
        p_bar.progress(0.03, text=f"⏳ Fetching tasks across {len(pod_keys)} pods...")
        for idx, p in enumerate(pod_keys):
            st.session_state.current_loading_pod = p
            process_pod(p, master_bar=p_bar, pod_idx=idx, total_pods=len(pod_keys))
        st.session_state.current_loading_pod = None
        _g_overlay.empty()
        bar_placeholder.empty()
        st.session_state.pop('_loading_overlay', None)
        st.session_state.pop('_loading_start', None)
        st.session_state.pop('_loading_pod', None)
        st.session_state.trigger_pull = False
        st.rerun()

    # 🌟 THE FIX: Inject the blue prompt right above the map if no data exists


    # 🚀 Opt B — Lazy-load the Global master map. The map was building all
    # 5 pods + ghosts on every Global view render; collapsed by default now.
    with st.expander("🗺️ Master Route Map", expanded=False):
        st_folium(global_map, height=500, use_container_width=True, key="global_master_map", returned_objects=[])



# --- TAB 0: GLOBAL CONTROL ---
with tabs[0]:
    if _can_access_tab('Global'):
        _render_global_tab_body()
    else:
        st.info(f"🔒 Global Overview is restricted to Admin and Manager roles. Your assigned pod is **{st.session_state.get('_auth_user', {}).get('pod', '?')}** — head to your pod tab to dispatch.")
# --- INDIVIDUAL POD TABS ---
# 🌟 FIX: Using 2 instead of 1 to account for the new Digital Pool tab!
# Each tab body is gated by _can_access_tab — pod-locked users see a friendly
# "no access" message instead of the dispatch UI for pods they don't own.
for i, pod in enumerate(["Blue", "Green", "Orange", "Purple", "Red"], 1):
    with tabs[i]:
        if _can_access_tab(pod):
            run_pod_tab(pod)
        else:
            st.info(f"🔒 You don't have access to the {pod} Pod. Your assigned pod is **{st.session_state.get('_auth_user', {}).get('pod', '?')}**.")

# --- TAB 6: DIGITAL POOL ---
with tabs[6]:
    if not _can_access_tab('Digital'):
        st.info(f"🔒 The Digital tab is restricted to Admin and Manager roles. Your assigned pod is **{st.session_state.get('_auth_user', {}).get('pod', '?')}**.")
        st.stop()
    # 1. 📊 GRAB DATA & INITIALIZE
    global_digital = st.session_state.get('global_digital_clusters', [])
    
    # 🌟 THE FIX: Omni-Ghost Sorter for Digital
    sent_db, ghost_db, _archived_wos, _history_db = fetch_sent_records_from_sheet()
    st.session_state['_history_db'] = _history_db
    st.session_state['archived_wos'] = _archived_wos
    digital_ghosts_list = ghost_db.get("Global_Digital", [])
    
    pod_ghosts, finalized_ghosts, sent_ghosts, fn_ghosts = [], [], [], []
    seen_ghosts = set() # 🛡️ THE FIX: Streamlit Crash Shield
    
    for g in digital_ghosts_list:
        g_hash = g.get('hash')
        
        # If the Google Sheet has duplicate rows, drop the clone instantly!
        if g_hash in seen_ghosts:
            continue
        seen_ghosts.add(g_hash)

        # 🌟 Bug fix: if the dispatcher just revoked/re-routed this route,
        # suppress the sheet ghost until the async sheet archive lands.
        # Without this skip, the route appears "stuck" in Sent/Accepted
        # for ~5-15s after revoke because the live cluster is filtered by
        # is_reverted but the ghost path bypassed that check.
        if g_hash and st.session_state.get(f"reverted_{g_hash}", False):
            continue

        g_stat = g.get("status", "")
        local_override = st.session_state.get(f"route_state_{g_hash}")
        if local_override == "finalized" or g_stat == "finalized": finalized_ghosts.append(g)
        elif g_stat == "sent": sent_ghosts.append(g)
        elif g_stat == "field_nation": fn_ghosts.append(g)
        else: pod_ghosts.append(g)
    
    # --- 🚦 TRAFFIC COP: BUCKET SORTING (Pulls WO from Sheet) ---
    d_ready, d_flagged, d_fn, d_sent, d_acc, d_dec, d_fin = [], [], [], [], [], [], []
    live_hashes = set() # 🌟 Track live routes so we don't duplicate them!
    
    for c in global_digital:
        task_ids = [str(t['id']).strip() for t in c['data']]
        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
        live_hashes.add(cluster_hash) # Save hash
        
        route_state = st.session_state.get(f"route_state_{cluster_hash}")
        is_reverted = st.session_state.get(f"reverted_{cluster_hash}", False)
        
        # 🌟 Fetch Local Memory
        local_ts = st.session_state.get(f"sent_ts_{cluster_hash}", "")
        local_contractor = st.session_state.get(f"contractor_{cluster_hash}", "Unknown")
        local_wo = st.session_state.get(f"wo_{cluster_hash}", local_contractor)
        
        # Match live sheet data to get the Contractor Name and WO
        sheet_match = sent_db.get(next((tid for tid in task_ids if tid in sent_db), None))
        if sheet_match and not is_reverted:
            c['contractor_name'] = sheet_match.get('name', 'Unknown')
            c['wo'] = sheet_match.get('wo', c['contractor_name'])
            c['route_ts'] = sheet_match.get('time', '') or local_ts
            c['comp'] = sheet_match.get('comp', 0)    # 🌟 NEW
            c['due'] = sheet_match.get('due', 'N/A')  # 🌟 NEW
            db_stat = sheet_match.get('status', 'sent').lower()
        else:
            # 🌟 Apply Fallbacks Instantly
            c['contractor_name'] = local_contractor
            c['wo'] = local_wo
            c['route_ts'] = local_ts
            db_stat = c.get('db_status', 'ready').lower()

        # 🌟 LOGIC GATE: Every .append() target MUST start with 'd_'
        if route_state == 'finalized': d_fin.append(c) # 🌟 THE FIX: Local Finalize Override
        elif db_stat in ['sent', 'email_sent'] and not is_reverted: d_sent.append(c) 
        elif db_stat == 'accepted' and not is_reverted: d_acc.append(c) 
        elif db_stat == 'declined' and not is_reverted: d_dec.append(c) 
        elif db_stat == 'finalized' and not is_reverted: d_fin.append(c)
        elif db_stat == 'field_nation' and not is_reverted: d_fn.append(c) 
        elif route_state == 'email_sent' and not is_reverted: d_sent.append(c) 
        elif route_state == 'field_nation' and not is_reverted: d_fn.append(c) 
        # 👇 Added this safeguard back in just in case!
        elif route_state == 'link_generated' and not is_reverted:
            orig = st.session_state.get(f"orig_status_{cluster_hash}")
            if orig == "declined": d_dec.append(c)
            else: d_ready.append(c)
        else:
            if (c.get('status') == 'Ready'
                    and float(st.session_state.get(f'_rate_master_Global_Digital_{cluster_hash}', 0) or 0) < HIGH_RATE_FLAG_THRESHOLD):
                d_ready.append(c)
            else:
                d_flagged.append(c)
                
    # 🌐 FN-GHOST RECONSTRUCTION (Digital pool) — mirrors the per-pod path.
    # Digital routes rarely go to FN, but when they do (e.g. a service ticket
    # batch) we want the same recovery behavior so they stay visible after the
    # OnFleet tasks transition to state=1.
    for _fng in fn_ghosts:
        _fng_hash = _fng.get('hash')
        if not _fng_hash or _fng_hash in live_hashes:
            continue
        _pseudo = _fn_ghost_to_cluster(_fng)
        if _pseudo is None:
            continue
        d_fn.append(_pseudo)
        if not st.session_state.get(f"route_state_{_fng_hash}"):
            st.session_state[f"route_state_{_fng_hash}"] = "field_nation"

    # Supercard Counts
    pool_ready = len(d_ready)
    pool_flagged = len(d_flagged)
    pool_total_sent = len(d_sent) + len(d_acc) + len(pod_ghosts) + len(d_dec) + len(d_fn)
    
    # 🌟 THE FIX: Combine active Digital buckets (Excludes Accepted & Finalized)
    active_d_cls = d_ready + d_flagged + d_fn + d_sent + d_dec
    tasks_total = sum(len(c['data']) for c in active_d_cls)
    unique_stops_total = len(set(t['full'] for c in active_d_cls for t in c['data']))
    
    # 2. ⚡ DIGITAL HEADER & DYNAMIC BUTTON
    dh_col1, dh_col2, dh_col3 = st.columns([2, 6, 2])
    with dh_col2:
        st.markdown(f"<div style='text-align:center; padding-bottom:15px;'><h2 style='color:{TB_DIGITAL_TEXT}; margin:0;'>🔌 Digital Services Dashboard</h2></div>", unsafe_allow_html=True)
    with dh_col3:
        st.markdown("<div class='tab-action-btn'>", unsafe_allow_html=True)
        btn_label = "🚀 Sync Routes" if global_digital else "🚀 Initialize Data"
        digital_init_clicked = st.button(btn_label, key="digital_init_btn", use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    # 🌟 FULL-WIDTH LOADING UI — outside columns
    if digital_init_clicked:
        import time as _time
        _d_start = _time.time()
        _d_overlay = st.empty()
        _d_bar = st.progress(0, text="🔌 Connecting to Onfleet...")

        def _render_digital_card(overlay, start):
            elapsed = int(_time.time() - start)
            m = elapsed // 60; s = elapsed % 60
            overlay.markdown(f"""
                <style>
                    @keyframes spin {{0%{{transform:rotate(0deg)}}100%{{transform:rotate(360deg)}}}}
                    .dcc-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;
                        padding:36px 32px;text-align:center;margin:20px 0;}}
                    .dcc-spin{{width:44px;height:44px;border:4px solid #e2e8f0;
                        border-top:4px solid #0f766e;border-radius:50%;
                        animation:spin 0.8s linear infinite;margin:0 auto 16px auto;}}
                    .dcc-pill{{display:inline-block;font-size:13px;font-weight:700;
                        color:#0f766e;background:#ccfbf1;border-radius:20px;
                        padding:4px 14px;margin-top:12px;}}
                </style>
                <div class='dcc-card'>
                    <div class='dcc-spin'></div>
                    <p style='font-size:16px;font-weight:800;color:#0f172a;margin:0 0 4px 0;'>Initializing Digital Pool</p>
                    <p style='font-size:13px;color:#64748b;margin:0 0 8px 0;'>Fetching Digital tasks from Onfleet...</p>
                    <div class='dcc-pill'>⏱ {m}:{s:02d}</div>
                <div style='font-size:10px; color:#94a3b8; margin-top:8px; font-style:italic;'>First load: ~30s (pulls live digital task pool).</div>
                </div>
            """, unsafe_allow_html=True)

        st.session_state['_loading_overlay'] = _d_overlay
        st.session_state['_loading_start'] = _d_start
        st.session_state['_loading_pod'] = 'Digital'
        _render_digital_card(_d_overlay, _d_start)
        _time.sleep(0.05)
        _d_bar.progress(0.03, text="⏳ Fetching Digital tasks from Onfleet...")
        process_digital_pool(master_bar=_d_bar)
        _d_overlay.empty()
        _d_bar.empty()
        st.session_state.pop('_loading_overlay', None)
        st.session_state.pop('_loading_start', None)
        st.session_state.pop('_loading_pod', None)
        st.rerun()

    # 3. 🃏 SUPERCARDS
    dc1, dc2, dc3 = st.columns([1, 1, 1])
    with dc1:
        st.markdown(f"<div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 110px;'><p style='margin:0 0 8px 0; font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Status</p><div style='display:flex; justify-content:space-around; gap:8px;'><div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_GREEN_TEXT};'>READY</p><p style='margin:0; font-size:22px; font-weight:800;'>{pool_ready}</p></div><div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_RED_TEXT};'>FLAGGED</p><p style='margin:0; font-size:22px; font-weight:800;'>{pool_flagged}</p></div></div></div>", unsafe_allow_html=True)
    with dc2:
        # 🌟 UPDATED: Uses tasks_total instead of len(pool)
        st.markdown(f"<div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 110px;'><p style='margin:0 0 8px 0; font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Workload</p><div style='display:flex; justify-content:space-around; gap:8px;'><div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_STATIC_TEXT};'>TASKS</p><p style='margin:0; font-size:22px; font-weight:800;'>{tasks_total}</p></div><div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_STATIC_TEXT};'>STOPS</p><p style='margin:0; font-size:22px; font-weight:800;'>{unique_stops_total}</p></div></div></div>", unsafe_allow_html=True)
    with dc3:
        st.markdown(f"<div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height:110px;'><p style='margin:0 0 8px 0; font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>All Sent: {pool_total_sent}</p><div style='display:flex; justify-content:space-around; gap:8px;'><div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_GREEN_TEXT};'>ACCEPTED</p><p style='margin:0; font-size:22px; font-weight:800;'>{len(d_acc)}</p></div><div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_RED_TEXT};'>DECLINED</p><p style='margin:0; font-size:22px; font-weight:800;'>{len(d_dec)}</p></div></div></div>", unsafe_allow_html=True)
    # 🌟 THE FIX: Force spacing after the cards
    st.markdown("<div style='margin-bottom: 25px;'></div>", unsafe_allow_html=True)
    
    # 🌟 THE FIX: Make sure the UI still loads if there are digital ghosts but no live digital routes
    if not global_digital and not pod_ghosts:
        st.info("Click '🚀 Initialize Data' at the top right to fetch data.")
    else:
        # 4. 🗺️ MAP & LEGEND
        # 🚀 Opt B — Lazy-load the Digital pool map.
        with st.expander("🗺️ Master Route Map", expanded=False):
            # 🌟 THE FIX: Safe coordinate extraction
            map_center_digi = global_digital[0]['center'] if global_digital else [39.8283, -98.5795]
            m_digi = folium.Map(location=map_center_digi, zoom_start=4, tiles="cartodbpositron")
            for c in global_digital: folium.CircleMarker(c['center'], radius=8, color="#0f766e", fill=True, opacity=0.8).add_to(m_digi)
            st_folium(m_digi, height=400, use_container_width=True, key="digital_pool_map", returned_objects=[])
        
        # 5. 🚀 TWO-COLUMN DISPATCH (Parity with Pods)
        st.markdown("""
<div style="display:flex; justify-content:center; flex-wrap:wrap; gap:8px 20px; background:#ffffff; padding:12px 20px; border-radius:12px; border:1px solid #99f6e4; margin-bottom:20px; box-shadow:0 2px 4px rgba(0,0,0,0.05);">
    <div style="font-size:11px; font-weight:900; color:#0f766e; text-transform:uppercase; letter-spacing:0.08em; align-self:center; margin-right:8px;">📖 Route Key</div>
    <span style="font-size:11px; color:#0f766e; font-weight:600; align-self:center; margin-right:4px; border-right:1px solid #99f6e4; padding-right:12px;">Status:</span>
    <span style="font-size:13px;" title="Ready to dispatch">🟢 Ready</span>
    <span style="font-size:13px;" title="Requires unlock">🔒 Action Req.</span>
    <span style="font-size:13px;" title="Flagged for review">🔴 Flagged</span>
    <span style="font-size:13px;" title="Field Nation">🌐 FN</span>
    <span style="font-size:11px; color:#0f766e; font-weight:600; align-self:center; margin-left:4px; margin-right:4px; border-right:1px solid #99f6e4; padding-right:12px;">Flags:</span>
    <span style="font-size:13px;" title="IC 40+ miles away">📡 Distance</span>
    <span style="font-size:13px;" title="Contains escalated tasks requiring priority handling">❗ Escalation</span>
    <span style="font-size:11px; color:#0f766e; font-weight:600; align-self:center; margin-left:4px; margin-right:4px; border-right:1px solid #99f6e4; padding-right:12px;">Tasks:</span>
    <span style="font-size:13px;" title="Screen offline">📵 Offline</span>
    <span style="font-size:13px;" title="Install / Removal">🔧 Ins/Rem</span>
    <span style="font-size:13px;" title="Digital maintenance">⚙️ Service</span>
    <span style="font-size:13px;" title="Certified digital IC">🔌 Digital</span>
</div>
""", unsafe_allow_html=True)
        st.markdown("---")
        col_left, col_right = st.columns([5, 5])
        
        with col_left:
            st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_DIGITAL_TEXT}; text-align: center;'>🚀 Dispatch</div>", unsafe_allow_html=True)
            t_ready, t_flagged, t_fn = st.tabs(["📥 Ready", "⚠️ Flagged", "🌐 Field Nation"])
            
            with t_ready:
                if not d_ready: st.info("No digital tasks ready for dispatch.")
                else:
                    sorted_d_ready = group_and_sort_by_proximity(d_ready)
                    current_state = None
                    for i, c in enumerate(sorted_d_ready):
                        if c['state'] != current_state:
                            current_state = c['state']
                            st.markdown(f"<div class='dcc-state-header' data-state-key='{current_state}'><span class='dcc-state-chevron'>▸</span>📍 {current_state}</div>", unsafe_allow_html=True)
                        _GD_BOOSTED = {'local plus': '⭐ LOCAL PLUS', 'boosted': '🔥 BOOSTED'}
                        _gd_boost = f" | {next((v for k,v in _GD_BOOSTED.items() if k in c.get('boosted_tag','')), '')}" if c.get('boosted_tag') and any(k in c.get('boosted_tag','') for k in _GD_BOOSTED) else ""
                        _gd_esc = f" | ❗ {c.get('esc_count', 0)}" if c.get('esc_count', 0) > 0 else ""
                        with st.expander(f"{get_digi_badges(c['data'])} {c['city']}, {c['state']} | {c['stops']} Stops{_gd_boost}{_gd_esc}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                            render_dispatch(i+8000, c, "Global_Digital")
                            
            with t_flagged:
                if not d_flagged: st.info("No flagged tasks requiring review.")
                else:
                    sorted_d_flagged = group_and_sort_by_proximity(d_flagged)
                    current_state = None
                    for i, c in enumerate(sorted_d_flagged):
                        if c['state'] != current_state:
                            current_state = c['state']
                            st.markdown(f"<div class='dcc-state-header' data-state-key='{current_state}'><span class='dcc-state-chevron'>▸</span>📍 {current_state}</div>", unsafe_allow_html=True)
                        _GDF_BOOSTED = {'local plus': '⭐ LOCAL PLUS', 'boosted': '🔥 BOOSTED'}
                        _gdf_boost = f" | {next((v for k,v in _GDF_BOOSTED.items() if k in c.get('boosted_tag','')), '')}" if c.get('boosted_tag') and any(k in c.get('boosted_tag','') for k in _GDF_BOOSTED) else ""
                        _gdf_esc = f" | ❗ {c.get('esc_count', 0)}" if c.get('esc_count', 0) > 0 else ""
                        with st.expander(f"🔴 {get_digi_badges(c['data'])} {c['city']}, {c['state']} | {c['stops']} Stops{_gdf_boost}{_gdf_esc}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                            render_dispatch(i+9000, c, "Global_Digital")
                            
            with t_fn:
                if not d_fn: st.info("No tasks in Field Nation.")
                else:
                    sorted_d_fn = group_and_sort_by_proximity(d_fn)

                    # 📦 Combined FN CSV — Digital pool. Same flow as the per-pod FN tab.
                    _fn_exported = st.session_state.setdefault('_fn_exported', {})
                    _fn_route_lookup = {}
                    _fn_options = []
                    for _fc in sorted_d_fn:
                        _fc_tids = sorted([str(_t['id']).strip() for _t in _fc.get('data', [])])
                        if not _fc_tids:
                            continue
                        _fc_hash = hashlib.md5("".join(_fc_tids).encode()).hexdigest()
                        _fn_route_lookup[_fc_hash] = _fc
                        _fn_options.append(_fc_hash)
                    _fn_select_key = "fn_combined_select_Global_Digital"
                    _fn_label_for = lambda h: (
                        f"{'✓ ' if h in _fn_exported else ''}{_fn_route_lookup[h].get('city','?')}, {_fn_route_lookup[h].get('state','?')} "
                        f"— {_fn_route_lookup[h].get('stops',0)} stops · {len(_fn_route_lookup[h].get('data',[]))} tasks"
                        + (f" · exported {_fn_exported[h]}" if h in _fn_exported else "")
                    )
                    st.markdown(
                        "<div style='background:#fef9c3;border-left:3px solid #facc15;border-radius:6px;"
                        "padding:8px 12px;margin:6px 0;'>"
                        "<div style='font-size:9px;font-weight:900;color:#854d0e;text-transform:uppercase;letter-spacing:0.08em;'>"
                        "📦 Combined FN CSV</div>"
                        "<div style='font-size:11px;color:#475569;'>Select multiple FN routes, download a single CSV with every stop. ✓ marks routes already exported.</div>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    st.multiselect(
                        "Select FN routes for combined CSV",
                        options=_fn_options,
                        format_func=_fn_label_for,
                        key=_fn_select_key,
                        label_visibility="collapsed",
                        placeholder="Select FN routes to include in the combined CSV...",
                    )
                    _fn_selected = st.session_state.get(_fn_select_key, []) or []
                    if True:
                        if _fn_selected:
                            _fn_to_export = []
                            for _h in _fn_selected:
                                _cluster = _fn_route_lookup.get(_h)
                                if _cluster:
                                    _ann = dict(_cluster)
                                    _ann['_cluster_hash'] = _h
                                    _fn_to_export.append(_ann)
                            try:
                                _fn_combined_buf, _fn_combined_stops, _fn_included_hashes = generate_combined_fn_upload(_fn_to_export)
                            except Exception as _fe:
                                _fn_combined_buf, _fn_combined_stops, _fn_included_hashes = None, 0, []
                                _log_err("fn_combined_digital", _fe)
                            if _fn_combined_buf is None:
                                st.button(f"📥 Download Combined CSV ({len(_fn_selected)} routes — no kiosk stops)", disabled=True, use_container_width=True, key="fn_dl_disabled_digital")
                            else:
                                if st.download_button(
                                    label=f"📥 Download Combined CSV ({len(_fn_selected)} routes · {_fn_combined_stops} stops)",
                                    data=_fn_combined_buf,
                                    file_name=f"FN_Combined_DIGITAL_{datetime.now().strftime('%m%d%Y_%H%M')}_{len(_fn_included_hashes)}routes.csv",
                                    mime="text/csv",
                                    key="fn_dl_combined_digital",
                                    type="primary",
                                    use_container_width=True,
                                ):
                                    _ts = datetime.now().strftime('%m/%d %I:%M %p')
                                    for _h in _fn_included_hashes:
                                        _fn_exported[_h] = _ts
                                    st.session_state['_fn_exported'] = _fn_exported
                                    st.toast(f"📥 Combined CSV: {len(_fn_included_hashes)} routes · {_fn_combined_stops} stops")
                        else:
                            st.button("📥 Download Combined CSV (none selected)", disabled=True, use_container_width=True, key="fn_dl_empty_digital")

                    # 📤 POSTED TO FIELD NATION — Digital tab. Same flow / state key as the
                    # per-pod FN tab so the Sent set is shared across pods (one Associate
                    # owns the whole FN view).
                    _fn_posted_dict = st.session_state.setdefault('_fn_posted', {})
                    _fn_post_left_d, _fn_post_right_d = st.columns(2)
                    with _fn_post_left_d:
                        st.link_button(
                            "Field Nation",
                            url="https://app.fieldnation.com/projects",
                            use_container_width=True,
                        )
                    with _fn_post_right_d:
                        if _fn_selected:
                            _newly_posting = [_h for _h in _fn_selected if _h not in _fn_posted_dict]
                            if st.button(
                                f"📤 Posted! ({len(_newly_posting)} new · {len(_fn_selected)} selected)",
                                key="fn_post_btn_digital",
                                use_container_width=True,
                                type="secondary",
                                disabled=(len(_newly_posting) == 0),
                            ):
                                _ts = datetime.now().strftime('%m/%d %I:%M %p')
                                for _h in _fn_selected:
                                    _fn_posted_dict[_h] = _ts
                                st.session_state['_fn_posted'] = _fn_posted_dict
                                # 📤 Persist via GAS so Sent group survives reload (see pod FN handler).
                                try:
                                    _hashes_csv = ",".join(_fn_selected)
                                    threading.Thread(
                                        target=lambda: requests.post(
                                            GAS_WEB_APP_URL,
                                            json={"action": "markFNPosted", "auth_secret": GAS_AUTH, "cluster_hash": _hashes_csv},
                                            timeout=15,
                                        ),
                                        daemon=True,
                                    ).start()
                                except Exception as _fpe:
                                    _log_err("markFNPosted/digital", _fpe)
                                # --- Phase 2 migration: best-effort Postgres mirror (2026-09-21) ---
                                if DB_ENGINE is not None:
                                    for _pmh in _fn_selected:
                                        try:
                                            _da.mirror_mark_fn_posted_by_cluster_hash(DB_ENGINE, _pmh)
                                        except Exception as _dw_e:
                                            _log_err("pg_dual_write_mark_fn_posted/digital", _dw_e)
                                st.toast(f"📤 Marked {len(_newly_posting)} digital route(s) as Posted to Field Nation.")
                                st.rerun()
                        else:
                            st.button(
                                "📤 Posted! (none selected)",
                                key="fn_post_btn_empty_digital",
                                use_container_width=True,
                                disabled=True,
                            )

                    # 📢 ASSIGNED FN REP (BULK) — digital, May 2026:
                    # Mirror of the pod FN bulk handler above. Shares the same
                    # _fn_assigned session-state dict so the badge / disabled state
                    # is consistent across pods + digital.
                    _fn_assigned_dict_d = st.session_state.setdefault('_fn_assigned', {})
                    if _fn_selected:
                        _newly_assigning_d = [_h for _h in _fn_selected if _h not in _fn_assigned_dict_d]
                        if st.button(
                            f"📢 Assigned FN Rep — Move to Accepted ({len(_newly_assigning_d)} new · {len(_fn_selected)} selected)",
                            key="fn_assigned_bulk_digital",
                            use_container_width=True,
                            type="primary",
                            disabled=(len(_newly_assigning_d) == 0),
                        ):
                            _ts_now_d = datetime.now().strftime('%m/%d %I:%M %p')
                            def _fire_assigned_d(_h):
                                try:
                                    _r = requests.post(
                                        GAS_WEB_APP_URL,
                                        json={"action": "markFNAssigned", "auth_secret": GAS_AUTH, "cluster_hash": _h},
                                        timeout=90,
                                    ).json()
                                    return (_h, bool(_r.get("success")), _r.get("error", ""))
                                except Exception as _ex:
                                    return (_h, False, str(_ex))
                            _ok_d = 0
                            _fail_d = 0
                            with st.spinner(f"Marking {len(_newly_assigning_d)} digital route(s) as Assigned FN Rep..."):
                                with ThreadPoolExecutor(max_workers=min(8, max(1, len(_newly_assigning_d)))) as _ex:
                                    _results_d = list(_ex.map(_fire_assigned_d, _newly_assigning_d))
                                for _h, _success, _err in _results_d:
                                    if _success:
                                        _ok_d += 1
                                        _fn_assigned_dict_d[_h] = _ts_now_d
                                        st.session_state[f"contractor_{_h}"] = "Field Nation"
                                        st.session_state.pop(f"route_state_{_h}", None)
                                        # NOT reverted — see per-route handler (May 23 2026).
                                        st.session_state[f"reverted_{_h}"] = False
                                        # Phase 2 migration: best-effort Postgres mirror (2026-09-21) — see per-route handler above.
                                        if DB_ENGINE is not None:
                                            try:
                                                _da.mirror_mark_fn_assigned_by_cluster_hash(DB_ENGINE, _h)
                                            except Exception as _dw_e:
                                                _log_err("pg_dual_write_mark_fn_assigned/digital", _dw_e)
                                    else:
                                        _fail_d += 1
                                        _log_err(f"bulk markFNAssigned/digital hash={_h}", _err)
                            st.session_state['_fn_assigned'] = _fn_assigned_dict_d
                            fetch_sent_records_from_sheet.clear()
                            if _fail_d == 0:
                                st.toast(f"✅ {_ok_d} digital route(s) marked Assigned FN Rep — moved to Accepted.")
                            elif _ok_d == 0:
                                st.error(f"All {_fail_d} GAS calls failed. Check logs.")
                            else:
                                st.toast(f"⚠️ {_ok_d} succeeded, {_fail_d} failed. Check logs for details.")
                            st.rerun()
                    else:
                        st.button(
                            "📢 Assigned FN Rep — Move to Accepted (none selected)",
                            key="fn_assigned_bulk_empty_digital",
                            use_container_width=True,
                            disabled=True,
                        )

                    st.divider()

                    # Partition into Pending Post + Sent (Posted to Field Nation).
                    _fn_pending_list, _fn_sent_list = [], []
                    for _c in sorted_d_fn:
                        _ch = hashlib.md5("".join(sorted([str(_t['id']).strip() for _t in _c.get('data', [])])).encode()).hexdigest()
                        (_fn_sent_list if _ch in _fn_posted_dict else _fn_pending_list).append(_c)
                    _fn_ordered = _fn_pending_list + _fn_sent_list

                    if _fn_pending_list:
                        st.markdown(
                            "<div style='background:#fffbeb; border-left:3px solid #f59e0b; "
                            "padding:6px 12px; margin:6px 0 4px 0; border-radius:6px;'>"
                            f"<span style='font-size:11px; font-weight:900; color:#92400e; "
                            "text-transform:uppercase; letter-spacing:0.08em;'>📋 Pending Post — "
                            f"{len(_fn_pending_list)} route(s)</span></div>",
                            unsafe_allow_html=True,
                        )

                    current_state = None
                    _entered_sent_zone = False
                    for i, c in enumerate(_fn_ordered):
                        if (not _entered_sent_zone) and i == len(_fn_pending_list) and _fn_sent_list:
                            st.markdown(
                                "<div style='background:#ecfdf5; border-left:3px solid #10b981; "
                                "padding:6px 12px; margin:14px 0 4px 0; border-radius:6px;'>"
                                f"<span style='font-size:11px; font-weight:900; color:#065f46; "
                                "text-transform:uppercase; letter-spacing:0.08em;'>📤 Posted! · "
                                f"{len(_fn_sent_list)} route(s)</span></div>",
                                unsafe_allow_html=True,
                            )
                            current_state = None
                            _entered_sent_zone = True
                        if c['state'] != current_state:
                            current_state = c['state']
                            st.markdown(f"<div class='dcc-state-header' data-state-key='{current_state}'><span class='dcc-state-chevron'>▸</span>📍 {current_state}</div>", unsafe_allow_html=True)
                        _GDFN_BOOSTED = {'local plus': '⭐ LOCAL PLUS', 'boosted': '🔥 BOOSTED'}
                        _gdfn_boost = f" | {next((v for k,v in _GDFN_BOOSTED.items() if k in c.get('boosted_tag','')), '')}" if c.get('boosted_tag') and any(k in c.get('boosted_tag','') for k in _GDFN_BOOSTED) else ""
                        _gdfn_esc = f" | ❗ {c.get('esc_count', 0)}" if c.get('esc_count', 0) > 0 else ""
                        _dfn_h_for_badge = hashlib.md5("".join(sorted([str(_t['id']).strip() for _t in c.get('data', [])])).encode()).hexdigest()
                        _dfn_exp_check = "✓ " if _dfn_h_for_badge in st.session_state.get('_fn_exported', {}) else ""
                        with st.expander(_dfn_exp_check + f"🌐 FN {get_digi_badges(c['data'])} {c['city']}, {c['state']} | {c['stops']} Stops{_gdfn_boost}{_gdfn_esc}  ·  :gray[{len(c['data'])} tasks]{_bundle_pill(c)}"):
                            # 🌟 FN-locations fix (May 2026):
                            # Digital FN tab previously jumped straight to render_dispatch
                            # with no venue section, so the dispatcher couldn't click into a
                            # location to see its tasks. Mirror the pod FN tab's route
                            # summary + venue accordion (line ~6256) before the dispatch
                            # block so clicking a location reveals task-type detail.
                            _dfn_task_ids = [str(_t['id']).strip() for _t in c.get('data', [])]
                            _dfn_hash = hashlib.md5("".join(sorted(_dfn_task_ids)).encode()).hexdigest()
                            if not st.session_state.get(f"route_state_{_dfn_hash}"):
                                st.session_state[f"route_state_{_dfn_hash}"] = "field_nation"
                            _dfn_stops, _dfn_tasks = len(set(_t['full'] for _t in c.get('data', []))), len(c.get('data', []))
                            _dfn_venues = venue_section(make_venue_details(c.get('data', [])))
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;">
    <div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;">
        <span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span>
    </div>
    <div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;">
        <div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div>
        <div style="font-size:14px; font-weight:800; color:#0f172a;">{{_dfn_stops}} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {{_dfn_tasks}} Tasks</span></div></div>
        <div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Status</div>
        <div style="font-size:13px; font-weight:700; color:#854d0e;">Field Nation</div></div>
    </div>
    {{_dfn_venues}}
</div>""".format(_dfn_stops=_dfn_stops, _dfn_tasks=_dfn_tasks, _dfn_venues=_dfn_venues), unsafe_allow_html=True)
                            render_dispatch(i+9500, c, "Global_Digital")

        with col_right:
            st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_GREEN}; text-align: center;'>⏳ Awaiting Confirmation</div>", unsafe_allow_html=True)
            t_sent, t_acc, t_dec, t_fin = st.tabs(["✉️ Sent", "✅ Accepted", "❌ Declined", "🏁 Finalized"])
            
            with t_sent:
                unified_sent = unify_and_sort_by_date(d_sent, sent_ghosts, live_hashes)
                if not unified_sent: st.info("No pending routes sent.")
                
                current_date = None
                _pg_items, _pg_start = _paginate_panel(unified_sent, "digital_sent")
                for i, item in enumerate(_pg_items, start=_pg_start):
                    date_str = item['sort_date']
                    if date_str != current_date:
                        current_date = date_str
                        st.markdown(f"<div class='dcc-state-header' data-state-key='sent-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 SENT: {current_date}</div>", unsafe_allow_html=True)
                    
                    if not item['is_ghost']:
                        c = item
                        task_ids = [str(t['id']).strip() for t in c['data']]
                        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                        ic_name = c.get('contractor_name', 'Unknown')
                        comp, due = c.get('comp', 0), c.get('due', 'N/A')
                        tasks_cnt, stops_cnt = len(c['data']), c['stops']
                        wo_display = c.get('wo', ic_name)
                        
                        exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                        with exp_col:
                            with st.expander(f"✉️ {wo_display} | ${comp} | Due: {due}  ·  :gray[{tasks_cnt} tasks]{_bundle_pill(c)}"):
                                u_locs, _dslv = [], []
                                for tk in c['data']:
                                    if tk['full'] not in u_locs:
                                        u_locs.append(tk['full'])
                                        _v = tk.get('venue_name', '')
                                        _dslv.append(f"{_v} — {tk['full']}" if _v else tk['full'])
                                _ds_venues = venue_section(make_venue_details(c['data']))
                                st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_ds_venues}</div>""", unsafe_allow_html=True)
                        with btn_col:
                            if not _is_dispatch_associate():
                                with st.popover("↩️"):
                                    st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                                    if st.button("🚨 Yes, Re-Route", key=f"rev_d_sent_live_{cluster_hash}", type="primary", use_container_width=True):
                                        move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": c})
                                        st.rerun()
                    else:
                        g = item
                        g_ic_name = g.get('contractor_name', 'Unknown')
                        ghost_hash = g.get('hash', f"ghost_d_sent_Global_Digital_{i}")  # H14 pod-scoped
                        wo_display = g.get('wo', g_ic_name)
                        comp, due = g.get('pay', 0), g.get('due', 'N/A')
                        stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                        
                        exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                        with exp_col:
                            with st.expander(f"✉️ {wo_display} | ${comp} | Due: {due}  ·  :gray[{tasks_cnt} tasks]"):
                                raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                                if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                                else: task_locs = raw_locs
                                u_locs = list(dict.fromkeys(task_locs))
                                _dsg_venues = venue_section(make_venue_details_ghost(u_locs, stop_data=g.get('stop_data', []))) if u_locs else ""
                                st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{g_ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_dsg_venues}</div>""", unsafe_allow_html=True)
                        with btn_col:
                            if not _is_dispatch_associate():
                                with st.popover("↩️"):
                                    st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{g_ic_name}</b>?</p>", unsafe_allow_html=True)
                                    if st.button("🚨 Yes, Re-Route", key=f"rev_ghost_d_sent_{ghost_hash}_{i}", type="primary", use_container_width=True):
                                        move_to_dispatch(**{"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": "Global_Digital", "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": g})
                                        st.rerun()
            with t_acc:
                unified_acc = unify_and_sort_by_date(d_acc, pod_ghosts, live_hashes)
                if not unified_acc: st.info("Waiting for portal acceptances...")
                
                current_date = None
                _pg_items, _pg_start = _paginate_panel(unified_acc, "digital_acc")
                for i, item in enumerate(_pg_items, start=_pg_start):
                    date_str = item['sort_date']
                    if date_str != current_date:
                        current_date = date_str
                        st.markdown(f"<div class='dcc-state-header' data-state-key='accepted-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 ACCEPTED: {current_date}</div>", unsafe_allow_html=True)
                    
                    if not item['is_ghost']:
                        c = item
                        task_ids = [str(t['id']).strip() for t in c['data']]
                        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                        ic_name = c.get('contractor_name', 'Unknown')
                        comp, due = c.get('comp', 0), c.get('due', 'N/A')
                        tasks_cnt, stops_cnt = len(c['data']), c['stops']
                        
                        _dins_cnt = sum(1 for tk in c['data'] if 'ins' in str(tk.get('task_type','')).lower() or 'rem' in str(tk.get('task_type','')).lower())
                        _dins_pill = f" | 🔧 {_dins_cnt} Ins/Rem" if _dins_cnt > 0 else ""
                        exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                        with exp_col:
                            _dacc_fn_badge = "🌐 " if ic_name == "Field Nation" else ""
                            with st.expander(_dacc_fn_badge + f"✅ {c.get('wo', ic_name)} | ${comp} | Due: {due}" + (f" | 🛠️ {sum(1 for tk in c['data'] if 'install' in str(tk.get('task_type','')).lower())}" if any('install' in str(tk.get('task_type','')).lower() for tk in c['data']) else "") + _bundle_pill(c)):
                                u_locs, _dalv = [], []
                                for tk in c['data']:
                                    if tk['full'] not in u_locs:
                                        u_locs.append(tk['full'])
                                        _v = tk.get('venue_name','')
                                        _dalv.append(f"{_v} — {tk['full']}" if _v else tk['full'])
                                _dal_venues = venue_section(make_venue_details(c['data']))
                                st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_dal_venues}</div>""", unsafe_allow_html=True)
                                render_finalization_checklist(cluster_hash, "Global_Digital", "d_chk", is_fn=(ic_name == "Field Nation"))
                        with btn_col:
                            if not _is_dispatch_associate():
                                with st.popover("↩️"):
                                    st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{c.get('wo', ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                    if st.button("🚨 Yes, Remove", key=f"rev_d_acc_{cluster_hash}", type="primary", use_container_width=True):
                                        move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "cluster_data": c})
                                        st.rerun()
                    else:
                        g = item
                        g_ic_name = g.get('contractor_name', 'Unknown')
                        ghost_hash = g.get('hash', f"ghost_digi_Global_Digital_{i}")  # H14 pod-scoped
                        comp, due = g.get('pay', 0), g.get('due', 'N/A')
                        stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                        
                        exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                        with exp_col:
                            _dgacc_fn_badge = "🌐 " if g_ic_name == "Field Nation" else ""
                            with st.expander(_dgacc_fn_badge + f"✅ {g.get('wo', g_ic_name)} | ${comp} | Due: {due}"):
                                raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                                if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                                else: task_locs = raw_locs
                                u_locs = list(dict.fromkeys(task_locs))
                                _dag_venues = venue_section(make_venue_details_ghost(u_locs, stop_data=g.get('stop_data', []))) if u_locs else ""
                                st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{g_ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_dag_venues}</div>""", unsafe_allow_html=True)
                                render_finalization_checklist(ghost_hash, "Global_Digital", "g_chk_d", is_fn=(g_ic_name == "Field Nation"))
                        with btn_col:
                            if not _is_dispatch_associate():
                                with st.popover("↩️"):
                                    st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{g_ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{g.get('wo', g_ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                    if st.button("🚨 Yes, Remove", key=f"rev_ghost_digi_{ghost_hash}_{i}", type="primary", use_container_width=True):
                                        move_to_dispatch(**{"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": "Global_Digital", "action_label": "Ghost Archived", "check_onfleet": True, "cluster_data": g})
                                        st.rerun()
            with t_dec:
                unified_dec = unify_and_sort_by_date(d_dec, [], live_hashes)
                if not unified_dec: st.info("No declined routes.")
                
                current_date = None
                _pg_items, _pg_start = _paginate_panel(unified_dec, "digital_dec")
                for i, item in enumerate(_pg_items, start=_pg_start):
                    date_str = item['sort_date']
                    if date_str != current_date:
                        current_date = date_str
                        st.markdown(f"<div class='dcc-state-header' data-state-key='declined-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 DECLINED: {current_date}</div>", unsafe_allow_html=True)
                    
                    c = item
                    task_ids = [str(t['id']).strip() for t in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    ic_name = c.get('contractor_name', 'Unknown')
                    exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                    with exp_col:
                        comp_ddec = c.get('comp', 0); due_ddec = c.get('due', 'N/A')
                        stops_ddec, tasks_ddec = c['stops'], len(c['data'])
                        with st.expander(f"❌ {c.get('wo', ic_name)} | ${comp_ddec} | Due: {due_ddec}{_bundle_pill(c)}"):
                            _ddec_venues = venue_section(make_venue_details(c['data']))
                            st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_ddec} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_ddec} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due_ddec}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp_ddec}</div></div></div>{_ddec_venues}</div>""", unsafe_allow_html=True)
                    with btn_col:
                        if not _is_dispatch_associate():
                            with st.popover("↩️"):
                                st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                                if st.button("🚨 Yes, Remove", key=f"rev_d_dec_{cluster_hash}", type="primary", use_container_width=True):
                                    move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "cluster_data": c})
                                    st.rerun()
            with t_fin:
                unified_fin = unify_and_sort_by_date(d_fin, finalized_ghosts, live_hashes)
                if not unified_fin: st.info("No finalized digital routes.") 
                
                current_date = None
                _pg_items, _pg_start = _paginate_panel(unified_fin, "digital_fin")
                for i, item in enumerate(_pg_items, start=_pg_start):
                    date_str = item['sort_date']
                    if date_str != current_date:
                        current_date = date_str
                        st.markdown(f"<div class='dcc-state-header' data-state-key='finalized-{current_date}'><span class='dcc-state-chevron'>▸</span>📅 FINALIZED: {current_date}</div>", unsafe_allow_html=True)
                    
                    if not item['is_ghost']:
                        c = item
                        task_ids = [str(t['id']).strip() for t in c['data']]
                        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                        ic_name = c.get('contractor_name', 'Unknown')
                        comp, due = c.get('comp', 0), c.get('due', 'N/A')
                        tasks_cnt, stops_cnt = len(c['data']), c['stops']
                        
                        _dfins_cnt = sum(1 for tk in c['data'] if 'ins' in str(tk.get('task_type','')).lower() or 'rem' in str(tk.get('task_type','')).lower())
                        _dfins_pill = f" | 🔧 {_dfins_cnt} Ins/Rem" if _dfins_cnt > 0 else ""
                        exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                        with exp_col:
                            with st.expander(f"🏁 {c.get('wo', ic_name)} | ${comp} | Due: {due}" + (f" | 🛠️ {sum(1 for tk in c['data'] if 'install' in str(tk.get('task_type','')).lower())}" if any('install' in str(tk.get('task_type','')).lower() for tk in c['data']) else "") + _bundle_pill(c)):
                                u_locs, _dflv = [], []
                                for tk in c['data']:
                                    if tk['full'] not in u_locs:
                                        u_locs.append(tk['full'])
                                        _v = tk.get('venue_name','')
                                        _dflv.append(f"{_v} — {tk['full']}" if _v else tk['full'])
                                _dfl_venues = venue_section(make_venue_details(c['data']))
                                st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_dfl_venues}</div>""", unsafe_allow_html=True)
                        with btn_col:
                            if not _is_dispatch_associate():
                                with st.popover("↩️"):
                                    st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{c.get('wo', ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                    if st.button("🚨 Yes, Remove", key=f"rev_d_fin_{cluster_hash}", type="primary", use_container_width=True):
                                        move_to_dispatch(**{"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "cluster_data": c, "check_completed": True})
                                        st.rerun()
                    else:
                        g = item
                        g_ic_name = g.get('contractor_name', 'Unknown')
                        ghost_hash = g.get('hash', f"ghost_d_fin_Global_Digital_{i}")  # H14 pod-scoped
                        wo_display = g.get('wo', g_ic_name)
                        comp, due = g.get('pay', 0), g.get('due', 'N/A')
                        stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                        _gdfk_total = g.get('kCnt', 0) or 0
                        _gdfk_pill = f" | 🛠️ {_gdfk_total} Kiosk" if _gdfk_total > 0 else ""
                        exp_col, btn_col = st.columns([9.5, 0.5], vertical_alignment="center")
                        with exp_col:
                            with st.expander(f"🏁 {wo_display} | ${comp} | Due: {due}{_gdfk_pill}  ·  :gray[{tasks_cnt} tasks]"):
                                raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                                if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                                else: task_locs = raw_locs
                                u_locs = list(dict.fromkeys(task_locs))
                                _gdfin_venues = venue_section(make_venue_details_ghost(u_locs, stop_data=g.get('stop_data', []))) if u_locs else ""
                                st.markdown(f"""<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; margin-bottom:10px;"><div style="background:#f8fafc; border-bottom:1px solid #e2e8f0; padding:8px 12px;"><span style="font-size:9px; font-weight:900; color:#94a3b8; text-transform:uppercase; letter-spacing:0.1em;">Route Summary</span></div><div style="padding:12px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Contractor</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{g_ic_name}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Stops / Tasks</div><div style="font-size:14px; font-weight:800; color:#0f172a;">{stops_cnt} <span style="color:#94a3b8; font-size:11px; font-weight:500;">Stops / {tasks_cnt} Tasks</span></div></div></div><div style="padding:10px 14px; display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px solid #f1f5f9;"><div><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Due Date</div><div style="font-size:13px; font-weight:700; color:#0f172a;">{due}</div></div><div style="text-align:right;"><div style="font-size:9px; font-weight:800; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:2px;">Total Compensation</div><div style="font-size:18px; font-weight:900; color:#16a34a;">${comp}</div></div></div>{_gdfin_venues}</div>""", unsafe_allow_html=True)
                        with btn_col:
                            if not _is_dispatch_associate():
                                with st.popover("↩️"):
                                    st.markdown(f"<p style='font-size:11px; text-align:center; margin:0 0 4px 0; line-height:1.3;'><span style='color:#475569; font-weight:700;'>Are you sure you want to remove this route from <b>{g_ic_name}</b>?</span><br><span style='color:#dc2626; font-size:10px; font-weight:500;'>All remaining tasks in <b>{g.get('wo', g_ic_name)}</b> will be removed from OnFleet.</span></p>", unsafe_allow_html=True)
                                    if st.button("🚨 Yes, Remove", key=f"rev_ghost_d_fin_{ghost_hash}_{i}", type="primary", use_container_width=True):
                                        move_to_dispatch(**{"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": "Global_Digital", "action_label": "Ghost Archived", "check_onfleet": True, "cluster_data": g, "check_completed": True})
                                        st.rerun()
# --- FOOTER ---
st.markdown("---")
st.markdown(
    """
    <div style="text-align: center; color: #94a3b8; font-size: 12px; padding: 20px;">
        Tactical Workspace Master • 2026 Digital Logistics Interface • <b>v2.4.0</b><br>
        <i>All digital and static route data is synced in real-time.</i>
    </div>
    """,
    unsafe_allow_html=True
)
