# DCC database migration

Moves DCC off the Google Sheet (`IC_SHEET_URL`) + Apps Script (`GAS_WEB_APP_URL`)
backend onto a real Postgres database. Full context, the current
architecture, and the open questions are in the migration plan doc; this
folder is the code half of that plan.

Decision on file: **hard cutover, no parallel-run window** (no dual-write
phase) — so the staging import in step 2 and the verification pass in step 4
are the only safety net before flipping the switch. Budget real time for
both.

## Files

- `schema.sql` — the six tables that replace the seven Sheet tabs.
- `import_from_sheets.py` — one-time import from the live Sheet into the new database. Safe to re-run (every insert is an upsert). Does **not** import the Archive tab's history — `route_events` starts empty and fills in going forward as the app runs (decision on file: don't carry over old archive history).
- `data_access.py` — the new data-access layer: one function per Sheet-read / GAS-write the app does today. Meant to be dropped into `tactical_workspace_master_rw.py` in place of the CSV-fetch and `requests.post(GAS_WEB_APP_URL, ...)` call sites.
- `requirements.txt` — `sqlalchemy` + `psycopg2-binary`, on top of the app's existing `requirements.txt`.

## Steps

1. **Provision Postgres.** Add a Postgres instance in the same Railway project as the app (private networking to it is automatic). Set `DATABASE_URL` in both your local shell (for the steps below) and the app's Railway environment variables.
2. **Create the schema.**

psql "$DATABASE_URL" -f migration/schema.sql


3. **Import from the Sheet, against staging first.** Point `DATABASE_URL` at a throwaway/staging database, then:

pip install -r requirements.txt -r migration/requirements.txt

export IC_SHEET_URL="<the Sheet's edit URL>"

python migration/import_from_sheets.py


   Compare row counts (`SELECT COUNT(*) FROM contractors`, etc.) against the live Sheet, and spot-check a handful of records by hand, especially route `payload` JSON. Re-run against the real `DATABASE_URL` once you're satisfied.
4. **Wire `data_access.py` into the app.** Swap each read/write call site in `tactical_workspace_master_rw.py` for the matching function here — see the migration doc's "Code changes required" section for the full action-to-function mapping. Keep the existing `@st.cache_data` decorators wrapping the new read functions so caching behavior doesn't change. **Read this alongside "Step 4/5, in practice" below first** — it's a bigger job than a mechanical find-and-replace, and three of the write actions have hidden OnFleet/Monday.com side effects that need a decision before they're safe to swap. Do this against a running staging instance, not statically.
5. **Build the portal endpoint.** `docs/portal-dcc-rw.html` posts `processDecision` straight to GAS today and can't hold a database credential (it's public static HTML). It needs a small API endpoint in front of `data_access.process_decision()` — this is the one new piece of infrastructure in this migration, not a straight port. **Also see "Step 4/5, in practice"**: the live `processDecision` triggers an OnFleet auto-assign that `data_access.process_decision()` doesn't replicate — don't build this endpoint as a bare wrapper around it until that's resolved.
6. **Repoint the portal and the Field Nation browser extension** at the new endpoint(s), and confirm with whoever owns the extension that its three bulk-provider actions are updated too.
7. **Decide on the OnFleet/Monday.com orchestration** hidden in three GAS actions today: `markFNAssigned` (route-plan rename, worker updates, Monday.com mutations), `processDecision` (OnFleet auto-assign on accept), and `saveToFieldNation` (a Monday.com placeholder push). None of these are replicated by their `data_access.py` counterparts, which only touch the database row. Port them into Python or keep them as a small separate service called from `data_access.py` — see `data_access.py`'s module docstring and "Step 4/5, in practice" below.
8. **Cut over.** Since this is a hard cutover: do the final verification pass against staging (row counts, spot-checked records, a live test session pointed at the new database) as the last gate, then switch the app's env vars and redeploy. Retire `GAS_WEB_APP_URL`, `IC_SHEET_URL`, and `DCC_SHARED_SECRET` once everything is confirmed live on the new backend.

## Dry-run verification (2026-09-18, local sandbox — not staging)

Ran `schema.sql` + the real, unmodified `import_from_sheets.py` end-to-end
against a throwaway local Postgres, feeding it the live Sheet's actual data
(already fetched once this session for an unrelated data-quality check) via
a one-line patch of `fetch_csv` instead of a network call — every other line
of the import script ran as-is. This is **not** a substitute for step 3
against real staging — it never touched `DATABASE_URL`/`IC_SHEET_URL` or any
live infrastructure — but it does confirm the schema and script are correct
before anyone spends time on a real staging run:

- `schema.sql` applies cleanly, no errors.
- Import completed with no exceptions: 564 contractor rows → 562 distinct
  (the 2 known duplicate emails correctly collapsed via upsert), 193 route
  rows across sent/accepted/declined/finalized → 190 distinct routes (the
  known in-tab duplicates and the Saved→Accepted lifecycle transition both
  resolved exactly as expected, no data loss), 74 Field Nation orders (no
  dupes, matches the earlier audit), `route_events` stayed at 0 (confirms
  the Archive skip).
- Robert Nieman's row confirmed in the database with `phone = '18186324368'` —
  the override works in practice, not just in theory.
- Spot-checked: `unrestricted = true` on exactly the 4 contractors matching
  the name-fragment list; `routes.payload`/`locs`/`stop_data` are valid
  JSON/JSONB on every sampled row; no NULL `wo` or blank `contractor_name`
  in `routes`.

Bottom line: the import side of this migration (schema + `import_from_sheets.py`,
including both data fixes above) is verified correct. What's still unverified
is everything real infrastructure touches — provisioning, the actual staging
`DATABASE_URL`, and the app-wiring work below.

## Step 4/5, in practice: why they're not a mechanical swap

Attempted step 4 (wiring `data_access.py` into the app) and step 5 (the
portal endpoint) directly, without a staging `DATABASE_URL` available to test
against. Stopped short of touching `tactical_workspace_master_rw.py` after
finding the following — recording it here since it changes the actual scope
of both steps:

- **`processDecision` has an OnFleet side effect that isn't in `data_access.py`.**
  `docs/portal-dcc-rw.html` posts `action: "processDecision"` to GAS and reads
  `result.onfleetSuccess` / `result.onfleetMsg` back to show the contractor
  (see its JS around the fetch call). That means the live Apps Script
  `processDecision` triggers an OnFleet auto-assign (and apparently route
  creation) as part of handling an accept — not just a status write.
  `data_access.process_decision()` only flips `routes.status`. This is a
  second, separate OnFleet dependency beyond the `markFNAssigned` one step 7
  already calls out — worth deciding alongside it, since a portal endpoint
  built strictly around `data_access.process_decision()` would silently drop
  OnFleet auto-assignment on every accepted route.
- **`saveToFieldNation` also pushes to Monday.com.** `fn_utils.save_fn_to_sheet()`'s
  docstring/comments say the GAS-side `saveToFieldNation` action does "an
  inline Monday placeholder push (find by address + 2 mutations per matched
  item)" on top of the sheet write. `data_access.save_to_field_nation()` is a
  bare insert — it doesn't replicate that. Same category of gap as the two
  above: needs a home before this call site can be safely swapped.
- **`_cached_fetch_sent_records_from_sheet()` (≈380 lines, `tactical_workspace_master_rw.py`)
  is not a drop-in read swap.** It does a concurrent 6-tab fetch, per-row JSON
  parsing, live-vs-ghost route reconstruction against currently-live OnFleet
  task IDs, a migration-cutoff date filter, FN-posted/FN-provider hydration,
  and builds the per-task history log — all coupled tightly to the exact
  Sheet row/column shapes. Pointing it at `data_access.get_routes()` means
  re-deriving all of that from the `payload` JSONB column, which needs to be
  built and clicked through against a real staging database, not written
  blind.

Net effect: three of the write actions (`processDecision`, `saveToFieldNation`,
`markFNAssigned`) all have side effects in OnFleet and/or Monday.com that live
only in Apps Script today and aren't in this repo. Recommend deciding where
all three go (ported into Python, or kept as a small separate service called
from `data_access.py`) as one decision, using the actual GAS source as
reference — before wiring any of the write call sites. The read-only call
sites (`get_contractors`, `get_routes`) have no such hidden side effects and
are lower-risk to wire first, but `_cached_fetch_sent_records_from_sheet`
specifically still needs the careful port described above, ideally done
against a running staging instance rather than statically.

## Decision (2026-09-18): defer saveRoute/archiveRoute/finalizeRoute/FN writes to a Phase 2

Looked at the actual `saveRoute` call site in `tactical_workspace_master_rw.py`
(the one GAS write with no OnFleet/Monday.com angle) to see if it, at least,
was safe to wire today. It isn't, for a different reason: it carries
production-tuned reliability logic that has no equivalent anywhere in
`data_access.py` yet —

- GAS-side dedupe via `CacheService` on a 10-minute window keyed on
  `cluster_hash`, so a retried `saveRoute` with the same cluster can never
  create a duplicate route; it just hands back the original route ID.
- A 3-attempt escalating-backoff retry (2s, then 4s) around the POST itself,
  added after two real incidents (Sep 2026) where GAS's web-app front end
  blipped for a few seconds and returned a non-JSON 404 even though the
  deployment was healthy.

Neither is replicated in `data_access.save_route()`, which is a plain
upsert. Swapping this call site over without first building and testing the
Postgres-side equivalent of that dedupe/retry behavior against a real
staging environment risks either duplicate routes or silently losing a
safety net that has already caught production issues.

Combined with the three OnFleet/Monday.com side effects documented above
(`processDecision`, `saveToFieldNation`, `markFNAssigned`), the decision is
to treat `saveRoute`, `archiveRoute`, `finalizeRoute`, `processDecision`,
`markFNAssigned`, and `saveToFieldNation` — everything keyed on
`cluster_hash` plus the three OnFleet/Monday.com actions — as a **Phase 2**,
deliberately not attempted yet. This doesn't reopen the "no dual-write"
decision at the top of this doc: Phase 1 (`contractors`, `bundle_maps`) is a
complete, real cutover for those two tables the moment `DATABASE_URL` is
set; Phase 2's tables (`routes`, `field_nation_orders`) simply haven't
started their cutover and keep running on Sheets/GAS exactly as today until
someone can test the replacement against a staging OnFleet/Monday.com
environment, not just read the code.

**What's actually live today:** the app now reads the IC/contractor list and
saves/loads pod bundle maps from Postgres whenever `DATABASE_URL` is set —
see `_ic_df_from_db()` and the `DB_ENGINE` branches in
`tactical_workspace_master_rw.py`. Everything else (routes, Field Nation
orders, the portal, the browser extension) is still 100% Sheets/GAS,
unaffected by setting `DATABASE_URL`.

## What's needed next (requires Railway/account access this repo doesn't have)

The remaining steps need your Railway dashboard and aren't something that
can be done from this environment:

1. Add a Postgres service to the DCC Railway project (Railway → New →
   Database → Postgres). This is the real, billable resource — nothing here
   provisions it for you.
2. Copy the generated `DATABASE_URL` from that Postgres service's Variables
   tab. Run the staging import against a **throwaway/local** Postgres first
   (see step 3 above), not the new Railway one directly, so a bad import
   never touches the database the app will actually read from.
3. Once the staging import checks out, run `import_from_sheets.py` again
   against the real Railway `DATABASE_URL`.
4. Only then, set `DATABASE_URL` in the **app's** Railway service variables
   (not just the Postgres service's) and redeploy. That's the switch that
   turns on `DB_ENGINE` and moves contractors + bundle maps to Postgres —
   watch the deploy logs and confirm the dispatcher tab loads the IC list
   and that a pod's bundle map still saves/loads correctly, since this is
   the first time that code path runs against anything but a local sandbox.
5. `routes` and `field_nation_orders` keep running on Sheets/GAS until
   Phase 2 above is unblocked — no action needed for those yet.

## Known data fixes applied during import

- `robert@niekotech.com` has two rows in the Sheet with different phone numbers; `import_from_sheets.py` forces the confirmed-correct one (`18186324368`) via `CONTRACTOR_FIELD_OVERRIDES` regardless of which row the Sheet lists last.
- Duplicate emails/work-order numbers caused by stray whitespace or genuine double-entry (e.g. `robert@niekotech.com` / `holmj777@yahoo.com` having a tab-prefixed twin, or `FN-Ruben Delgado-9/14` appearing twice in Accepted routes) are handled by the existing upsert-on-natural-key behavior — the later row in the Sheet wins and the duplicate is silently absorbed. Confirmed acceptable; no separate cleanup needed.

## Not covered here

- Normalizing `routes.locs` / `routes.stop_data` out of JSONB into their own tables — a reasonable later cleanup, not required for the migration itself.
- The unrestricted-IC allowlist: `import_from_sheets.py` ports the current hardcoded name list into `contractors.unrestricted` as a one-time seed, but the app's dispatch-filter code still needs updating to read that column instead of checking names in Python.

## Decision + port (2026-09-19): OnFleet/Monday.com side effects

Step 7 above asked for a decision on where `processDecision`, `markFNAssigned`,
and `saveToFieldNation`'s OnFleet/Monday.com side effects should live —
**ported into Python**, not a separate service. Reasoning: the app already
makes raw OnFleet API calls directly from `tactical_workspace_master_rw.py`
(worker/task/team lookups, route-plan creation) for its existing dispatch
flow — a separate service would be a second deployable with its own auth and
failure modes for logic that's a natural extension of code already living
here.

That port is done: **`migration/fn_side_effects.py`**, function-by-function
from the actual Apps Script source (pulled from the live "DCC" Apps Script
project on 2026-09-19 — this wasn't available in the repo before, which is
why Step 7 originally could only say to use it "as reference" rather than
actually doing so). It carries over every retry/backoff constant, the
Onfleet team allow-list, the Monday column defaults, and — importantly —
the **"board-corruption guard"**: `MONDAY_GROUP_FILTER` restricts Monday
writes to 3 specific board groups (Field Nation / Escalations / Primary
Route) by default, and a `MONDAY_GROUP_FILTER='*'` override requires a
*second* confirmation env var (`MONDAY_GROUP_FILTER_ALLOW_WILDCARD=yes`) to
take effect, exactly like GAS enforces it. Do not relax this without reading
the comments in that module — it exists because an earlier run without it
corrupted unrelated Monday board groups.

`data_access.py`'s `process_decision()`, `mark_fn_assigned()`, and
`save_to_field_nation()` now call into it:

- **`process_decision`** runs the OnFleet auto-assign + ordered route
  creation on accept (same as GAS), plus the idempotency guards GAS has —
  a duplicate accept is a no-op, and an already-accepted route can't flip
  back to declined through this path (use the app's revoke flow instead).
  Returns the same `onfleetSuccess`/`onfleetMsg`/`routeSuccess`/`routeMsg`/
  `partial`/`route_incomplete` shape `docs/portal-dcc-rw.html` already
  expects from GAS, for whenever the portal endpoint (Step 5, still not
  built) is wired up.
- **`mark_fn_assigned`** runs the OnFleet routePlan-rename + per-task
  metadata/worker re-PUT, and the Monday.com address-matched sync.
- **`save_to_field_nation`** runs the Monday.com placeholder push
  (installer = "Field Nation" until a real provider is confirmed).

**A real functional gap found (and fixed) while porting, not just a missing
side effect:** GAS's `markFNAssigned` doesn't just flip a status — it
*moves* the Field Nation sheet row into "Accepted routes", renaming the WO
to `FN-<Provider>-<MMDD>` along the way, so the order becomes a full
accepted route (shows in the Accepted bucket, goes through the finalization
checklist — everything else in the app that reads from `routes` expects
this). The original `mark_fn_assigned()` (written before the actual GAS
source was available) only updated `field_nation_orders` in place and never
created the corresponding `routes` row — so nothing would have ever
promoted an FN order into the app's normal accepted-route lifecycle. Fixed:
`mark_fn_assigned()` now inserts/updates that `routes` row too.
`set_fn_provider()` had the same shape of gap (GAS stamps `fn_provider` into
the JSON payload, not a separate column, and `mark_fn_assigned` reads it
from there) — also fixed, keeping both the payload field and the existing
`provider` column in sync.

`saveRoute`/`archiveRoute`/`finalizeRoute` (the other half of Step 7's
"decision on file") are intentionally NOT touched by this port — their
existing `save_route()`/`archive_route()`/`finalize_route()` upsert-on-`wo`
behavior already gives equivalent dedupe to GAS's 10-minute cluster_hash
cache (see that module's comments), so there's nothing to port there beyond
what's already written.

### What's verified, and what isn't

Every function in `fn_side_effects.py` is unit-tested against **mocked**
`requests` calls — `migration/tests/test_fn_side_effects.py` proves the
shape of every Onfleet/Monday request (endpoints, payloads, the retry
classifier, the board-corruption guard) matches the ported GAS source.
`migration/tests/test_process_decision_and_fn_flow.py` exercises the full
`data_access.py` call paths (`process_decision`, `mark_fn_assigned`,
`save_to_field_nation`) against a real (throwaway) Postgres with the same
HTTP mocking, and passes end to end, including the new `routes` row created
by `mark_fn_assigned`.

**None of this has been run against a real Onfleet or Monday.com sandbox.**
This sandbox has no such environment, and pointing this code at the *real*
production Onfleet/Monday accounts to "test" it would itself create the
exact side effects (real task assignments, real board writes) a test run
shouldn't cause. Before removing the GAS call sites in
`tactical_workspace_master_rw.py` — the actual cutover, per this doc's "hard
cutover, no dual-write" decision at the top — do one supervised manual
accept and one manual FN-assign against a staging `DATABASE_URL`, watched
live in both Onfleet and Monday, the same way Step 8's "final verification
pass" already calls for.

### New env vars needed before flipping this on

In addition to `DATABASE_URL` (Phase 1), the app's Railway service needs:
- `MONDAY_API_TOKEN` — a Monday.com personal API token. Without it, the
  Monday sync is skipped (logged, not raised) exactly like GAS does when its
  equivalent Script Property is unset.
- Optional, all have the same defaults GAS used: `MONDAY_BOARD_ID`
  (`7374880245`), `MONDAY_INSTALLER_COL` (`text1__1`), `MONDAY_WO_COL`
  (`text14`), `MONDAY_ADDR_COL` (`text63`), `MONDAY_GROUP_FILTER`,
  `MONDAY_GROUP_FILTER_ALLOW_WILDCARD`.
- `ONFLEET_KEY` is already set (the app's existing OnFleet integration uses
  it) and is reused here — GAS used a separate `ONFLEET_API_KEY` Script
  Property, but it's the same Onfleet account/key either way.

### Still not done

- The GAS call sites in `tactical_workspace_master_rw.py` still POST to
  `GAS_WEB_APP_URL` for all of this today — this port doesn't switch
  anything live. That switch is the actual cutover and should wait for the
  staging test above.

## Step 5, built (2026-09-20): the portal endpoint (`migration/portal_api.py`)

The portal endpoint step 5 asked for is done: **`migration/portal_api.py`**,
a small FastAPI service with exactly two routes, deliberately narrow rather
than a general REST API:

- `GET /?action=getRoute&routeId=<wo>` — mirrors GAS's `getRoute` action.
  Looks up `routes.payload` by `wo` and returns `{"payload": {...}}`, or
  `{"error": "..."}` if the WO isn't found. `routes.payload` already has
  every field `docs/portal-dcc-rw.html`'s `window.onload` reads off `d`
  (`icn`, `wo`, `due`, `lCnt`, `tCnt`, `kCnt`, `rCnt`, `dCnt`, `time`, `mi`,
  `comp`, `phone`, `taskIds`, `stopOrder`, `stopData`) — it's the exact same
  dict `tactical_workspace_master_rw.py` already builds and currently POSTs
  to GAS's `saveRoute`, so nothing needed adding there.
- `POST /` with `{"action": "processDecision", "routeId": <wo>, ...}` —
  mirrors GAS's `processDecision` action. Calls `data_access.process_decision()`
  (already built and tested — see the section above) and returns its result
  as-is, since that function already returns the exact
  `onfleetSuccess`/`onfleetMsg`/`routeSuccess`/`routeMsg`/`error` shape
  `submitFinalResponse()` in the portal already branches on.

Both routes match the GAS web app's request/response shapes byte-for-byte on
purpose, so Step 6 ("repoint the portal") is a **one-line change** to
`docs/portal-dcc-rw.html` — swap the `webAppUrl` constant — not a portal
rewrite.

**routeId is `wo`, not a new opaque token.** The routes table's natural key
is `wo`, so that's what's used in the link (`?route=<wo>`) and as the lookup
key here. This is the *same* security posture `docs/portal-dcc-rw.html`'s own
`[M24]` comment already documents as a known, deliberately-not-fixed-here
limitation: the only thing standing between "anyone with this link" and "a
valid accept/decline" is how guessable the ID in the URL is, and a
work-order-shaped ID is no more (and no less) guessable than whatever opaque
ID GAS's `routeId` scheme uses today. A real fix (an unguessable per-link
HMAC token, checked here) is the same `[M24]` finding and is intentionally
**not** addressed by this port — deploying this endpoint does not resolve
`[M24]`.

**Verified:** `migration/tests/test_portal_api.py` runs the FastAPI app
through Starlette's `TestClient` (the actual HTTP/ASGI layer, not just
calling the Python functions directly) against a real throwaway Postgres,
with Onfleet/Monday mocked the same way the other tests do it — checks the
`getRoute` 404-vs-hit shapes, the `processDecision` accept/decline paths, the
"unknown WO" error shape, and that the CORS preflight actually allows the
configured portal origin. Also manually run as a live `uvicorn` server in
this sandbox and hit with real HTTP requests (not just the test client) to
confirm the exact Railway start command works. Like the rest of Phase 2,
**none of this has touched a real Onfleet/Monday sandbox** — same caveat as
the "What's verified, and what isn't" section above.

### Deploying `portal_api.py`

This is its own process, not part of the Streamlit app — it needs its own
Railway **service** (same repo, different service):

1. In the Railway project, **New → GitHub Repo**, pick the same `DCC` repo
   again. This creates a second service alongside the existing Streamlit one.
2. In that new service's Settings, confirm it picked up the `api:` process
   type from the `Procfile` (Railway lets you pick which `Procfile` line a
   service runs when there's more than one — pick `api`, not `web`). If it
   defaults to `web` instead, set a custom start command:
   `uvicorn migration.portal_api:app --host 0.0.0.0 --port $PORT`.
3. Set that service's `DATABASE_URL` to the **same** value as the Streamlit
   app's — both read/write the same `routes` table, so this must be the
   real Railway Postgres, not a separate database.
4. Set `PORTAL_ALLOWED_ORIGINS` if the portal is ever served from somewhere
   other than `https://nwilliams-maker.github.io` (comma-separated for more
   than one origin). Defaults to that GitHub Pages origin if unset.
5. Set `ONFLEET_KEY` (and `MONDAY_API_TOKEN` etc. if using Monday) on this
   service too — `process_decision()` needs them exactly like the Streamlit
   app does; they don't carry over between services automatically.
6. Deploy, then hit `https://<this-service>.up.railway.app/?action=getRoute&routeId=anything`
   in a browser — a `{"error": "This route link has expired or the route
   was not found."}` response (not a 502/timeout) confirms it's up and can
   reach the database.
7. Update `docs/portal-dcc-rw.html`'s `webAppUrl` constant to that service's
   URL, commit, and it's live for whoever opens a route link next.


### Step 6 rollback (2026-09-20, same day): sequencing mistake

The real end-to-end test caught it immediately: Nick dispatched a route
(`R-94029-9BB7G`) through the live app and opened the actual portal link a
contractor would get. It showed **"This route link has expired or the route
was not found."**

Root cause: `data_access.save_route()` — the function that writes a route
into Postgres — **is never called anywhere in `tactical_workspace_master_rw.py`**,
only in this migration's own test files (confirmed by grep). The live
dispatch flow ("Generate Link") still exclusively `requests.post`s to
`GAS_WEB_APP_URL`, writing the route to the Google Sheet and nowhere else.
So Postgres's `routes` table only ever had the one-time historical snapshot
from `import_from_sheets.py` — nothing dispatched after that import exists
there, including every route dispatched today.

Step 6 (repointing the portal at `portal_api.py`) was done **before** Step 8
("Cut over" — switching the write side to Postgres too). That ordering
mistake meant every contractor who opened a route link after the repoint hit
a false "not found," even though their route was perfectly valid in the
Sheet — a live break for real people, not a test artifact.

**Fix applied immediately:** `docs/portal-dcc-rw.html`'s `webAppUrl` reverted
to the GAS URL. Contractor accept/decline is back on the original GAS/Sheets
path exactly as before Step 6.

**Do not repoint the portal again until:** `tactical_workspace_master_rw.py`'s
dispatch flow actually calls `data_access.save_route()` (dual-write
alongside the existing GAS POST, or a full cutover — see Step 8 above) and
that's been verified with a live dispatch → live portal-link round trip, the
same way this mistake was caught. Re-run this exact test (dispatch a real
route, open its real portal link, confirm it renders and accept/decline
works) before flipping `webAppUrl` back to `portal-api` a second time.

### Step 4, done as a dual-write (2026-09-20): what's wired and what's deliberately not

Nick asked for the rest of the migration to be built out now. Given the
scale of what's left (the full read-side rewrite of
`_cached_fetch_sent_records_from_sheet`, three GAS actions with real
Onfleet/Monday.com side effects) and the fact that today's Step 6 mistake
just showed exactly what happens when a piece is wired ahead of what it
depends on, this was done as a **dual-write**, not the doc's original
"hard cutover, no dual-write" plan: every write below still goes to GAS/the
Sheet exactly as before (that's still the only thing any part of the app
reads from), and a best-effort mirror also lands in Postgres. If the mirror
write fails or is skipped, nothing about the dispatcher's experience
changes — it's logged (`_log_err`) and swallowed, same pattern already
proven safe for the bundle-map dual-path. This directly fixes the root
cause of the Step 6 incident (Postgres never getting new routes) without
requiring the full read-side rewrite or the side-effect-duplication risk of
a true hard cutover.

**Wired (safe — plain DB writes, zero Onfleet/Monday side effects):**
- `saveRoute` → `data_access.save_route()`, called right after a successful
  GAS dispatch (same `wo_val`/`payload` already computed locally for the
  GAS POST — no adaptation needed). Upsert-safe against GAS's own retry
  loop (`ON CONFLICT (wo) DO NOTHING`), verified against a real local
  Postgres including the exact duplicate-POST-retry case.
- `archiveRoute` → `data_access.archive_route()`, called from
  `background_sheet_move()`'s background thread. The WO is captured on the
  **main thread** (`st.session_state.get(f"wo_{cluster_hash}")`) before the
  thread starts and passed in as an argument — the background thread itself
  still touches nothing but `requests`/`_log_err`, preserving the "no
  ScriptRunContext dependency" property the existing code comment calls out.
- `finalizeRoute` → `data_access.finalize_route()`, called synchronously
  right after GAS confirms the finalize, same button handler.
- All three verified end-to-end against a real local Postgres (not just
  imported/read): save → finalize → archive, event log order, and the
  no-op-on-unknown-wo case (a route dispatched before this code existed, or
  a save that failed to mirror) all confirmed safe.

**Deliberately NOT wired — the entire Field Nation family** (`saveToFieldNation`,
`removeFieldNation`, `markFNPosted`, `markFNAssigned`, `setFnProvider`,
`setFnRoutePlanId`, `bulkSetFnProvidersByAddress`) **and `processDecision`.**
Reasons, not an oversight:
- `saveToFieldNation` and `markFNAssigned` (via `data_access.py`, which
  calls into `fn_side_effects.py`) trigger **real** OnFleet routePlan
  renames/worker PUTs and **real** Monday.com board mutations as part of a
  normal call. GAS already performs these live today. Calling the
  `data_access.py` versions alongside GAS would duplicate those real-world
  side effects (double Monday writes, double Onfleet updates) — this is
  exactly the class of bug `fn_side_effects.py`'s own board-corruption-guard
  comment warns about from an earlier incident. A dual-write here needs a
  separate "mirror-only" write path that updates the DB row without
  re-triggering `_fx.*`, which does not exist yet and hasn't been built or
  tested.
- Everything else in the FN family (`removeFieldNation`, `markFNPosted`,
  `setFnProvider`, `setFnRoutePlanId`, `bulkSetFnProvidersByAddress`) reads
  or updates `field_nation_orders` rows that only ever get created by
  `saveToFieldNation` — since that one isn't mirrored, wiring the rest would
  just be updating rows that don't exist in Postgres. Wiring only some of
  the family isn't safe on its own merits either — better to leave the
  whole FN subsystem as GAS-only until `saveToFieldNation`'s mirror-only
  path is built, rather than ship partial, silently-no-op coverage.
- `processDecision` isn't called from `tactical_workspace_master_rw.py` at
  all — it's only ever posted by `docs/portal-dcc-rw.html`, and the portal
  is still pointed at GAS (see the Step 6 rollback above). No dispatch-flow
  change was needed or made for it.
- `getSyncVersion` / `fetch_sync_status()` stays GAS-only too — it already
  has its own graceful degrade-to-GAS-only behavior and switching it to read
  from Postgres before the FN/decision side effects are also mirrored would
  surface an inconsistent event log (Postgres would show route saves/
  finalizes/archives but never FN or decision events). Left for Step 8.

**Net effect:** the app's behavior is completely unchanged today — GAS/the
Sheet is still the only thing any read path uses, and every write still
goes there first. Postgres now additionally accumulates real, live
saveRoute/finalizeRoute/archiveRoute data going forward (best-effort,
non-blocking), which is what actually needs to be true before Step 6 (the
portal repoint) or Step 8 (the real cutover) can be attempted again safely.
**Not done and not attempted:** the FN/decision mirror-only paths, the
`_cached_fetch_sent_records_from_sheet` read-side rewrite, and any actual
cutover. Per the standing commitment to Nick, nothing here flips a switch —
it only starts populating Postgres safely in the background.

### Correction (2026-09-21): Monday.com is retired — disabled in `fn_side_effects.py`

Nick: Terraboost doesn't use Monday.com anymore. The Monday.com code in
`fn_side_effects.py` (`sync_monday_for_stops`, called by both
`push_fn_placeholder_to_monday` for `saveToFieldNation` and by
`apply_fn_assigned_side_effects` for `markFNAssigned`) was not invented by
this migration — it's a line-by-line port of the live "DCC" Apps Script
source as it existed on 2026-09-19 (see that module's docstring for the
exact script ID and pull date), which at that point still did an
address-matched sync to a Monday.com "Route Planning" board on both of
those actions. It was ported to keep the migration behavior-equivalent to
GAS in case Monday syncing was still relied on somewhere.

It isn't. **Fix:** `sync_monday_for_stops()` now unconditionally returns a
`skipped` result before touching the network, regardless of whether
`MONDAY_API_TOKEN` is set — this is a deliberate hard kill switch, not just
"leave the token unset," so a future env var accidentally getting set can't
silently revive Monday writes. The GAS-ported implementation is left in
place below the kill switch, unreached, rather than deleted, in case this
ever needs to be reversed — but treat that as reference/history, not as
something to re-enable without checking with Nick first.

**Not resolved by this change:** whether the live Apps Script (`Code.gs`,
not something this repo can edit — it lives at
script.google.com/d/1iyG4dr7iyoAD1sDkyySCh78baDOFJWBHt-_j9sS82oASl-
dWInb50wOI) still actually fires those same Monday API calls today on every
real `markFNAssigned`/`saveToFieldNation`. This migration module never sat
in that live path (see "Deliberately NOT wired" above — the FN family was
never dual-written), so disabling it here has zero effect on production
behavior either way. If the live GAS script is still calling Monday for
real, that's a separate cleanup in the Apps Script editor itself, outside
what this repo or this migration touches — worth confirming with Nick
whether that's also stale/wanted gone, especially given the board-corruption
incident the `MONDAY_GROUP_FILTER` guard exists because of (plausibly the
reason Monday was dropped in the first place, though that connection hasn't
been confirmed).

### Step 7, the FN mirror-only write path (2026-09-21): the whole Field Nation family can now dual-write

The Step 4 dual-write (above) deliberately left the entire Field Nation
family unwired, because `save_to_field_nation()` and `mark_fn_assigned()`
call into `fn_side_effects.py` for real Onfleet/Monday side effects that GAS
already performs live — dual-writing them as-is would have duplicated those
side effects. That required "a separate mirror-only write path that updates
the DB row without re-triggering `_fx.*`", which didn't exist yet. It does
now.

**What changed in `migration/data_access.py`:** `save_to_field_nation()` and
`mark_fn_assigned()` both gained a `mirror_only: bool = False` keyword-only
flag. When `True`, they run every DB step exactly as before (the same
`new_wo` computation, the same `field_nation_orders`/`routes` writes, the
same event log) but skip the `fn_side_effects` call entirely — no Onfleet
routePlan rename/re-PUT, no Monday sync. Three new wrapper functions are the
intended entry points, so nobody has to remember to pass the flag correctly:

- `mirror_save_to_field_nation(engine, work_order, payload)`
- `mirror_mark_fn_assigned(engine, work_order, route_plan_id=None)` — for a
  caller that already has the FN `work_order`.
- `mirror_mark_fn_assigned_by_cluster_hash(engine, cluster_hash, route_plan_id=None)` —
  for the live app's actual call sites, which only have `cluster_hash` in
  scope. Looks up the `work_order` via `field_nation_orders.payload ->>
  'cluster_hash'` (stamped there by `mirror_save_to_field_nation`'s own
  payload), then delegates. No-ops harmlessly — returns `{"success": False,
  "skipped": ...}`, never raises — if no matching row exists yet (e.g. a
  route posted to FN before this mirror existed).

All of this is covered by a new test file, `migration/tests/test_fn_mirror_writes.py`,
run against a real local Postgres: it proves via `unittest.mock.patch` +
call-count assertions (not just "didn't raise" — a bare `MagicMock` never
raises) that the mirror path never calls into `fn_side_effects`, that the
non-mirror path still does (a contrast check, so `mirror_only` can't
silently become the accidental default), and that the DB end state
(`field_nation_orders.status`, the new `routes` row, its `wo`/`status`/
`contractor_name`) matches what the live, non-mirror path produces.

**What changed in `fn_utils.py` and `tactical_workspace_master_rw.py` (the
actual wiring):**

- `fn_utils.save_fn_to_sheet()` (the `saveToFieldNation` call site) gained an
  optional `db_engine` parameter. When set, its background thread
  best-effort mirrors the save via `mirror_save_to_field_nation()` right
  after the real GAS POST, same exception-swallowed pattern as the Step 4
  mirrors. Its one call site (`tactical_workspace_master_rw.py`'s FN
  checkbox handler) now passes `db_engine=DB_ENGINE`.
- All three `markFNAssigned` call sites (single-route, bulk-pod,
  bulk-digital) now call `_da.mirror_mark_fn_assigned_by_cluster_hash(DB_ENGINE,
  cluster_hash)` best-effort, right after GAS confirms success, guarded by
  `if DB_ENGINE is not None` and wrapped in try/except like every other
  dual-write in this file. Wired at all three sites rather than just one —
  leaving two of three unmirrored would make the Postgres mirror
  inconsistent for the same action depending on which UI button fired it,
  which is worse than not mirroring it at all.

Verified locally end-to-end against a real Postgres (not just the unit
tests above): `fn_utils.save_fn_to_sheet(..., db_engine=engine)` with GAS's
`requests.post` mocked out actually lands a `field_nation_orders` row in the
background thread, and `db_engine=None` (today's default — `DATABASE_URL`
isn't set in Railway yet) still fires the real GAS POST with zero behavior
change, confirming this is exactly as dormant-by-default as every other
piece of this migration until `DATABASE_URL` is actually set.

**Still not wired at the time of writing:** `saveToFieldNation`'s Monday push
and `markFNAssigned`'s Onfleet/Monday side effects are *never* mirrored —
only their DB writes are. `processDecision` is still untouched (see Step 4
above — it's not called from this file at all).

### Step 7 continued (2026-09-21, same day): the rest of the Field Nation family wired

`setFnProvider`, `removeFieldNation`, `markFNPosted`, and `setFnRoutePlanId`
are now also dual-written. None of these four ever called into
`fn_side_effects.py` even in their live/non-mirror form — GAS's own
`removeFieldNation`/`markFNPosted`/`setFnProvider`/`setFnRoutePlanId`
actions have no Onfleet/Monday side effect — so there's no `mirror_only`
flag to add here, unlike `saveToFieldNation`/`markFNAssigned` above. The
only piece that was missing was the same `cluster_hash → work_order`
resolution `mirror_mark_fn_assigned_by_cluster_hash()` already does, since
every live call site for these four only has `cluster_hash` in scope, not
the Postgres `work_order`.

**What changed in `migration/data_access.py`:** four new
`mirror_*_by_cluster_hash()` wrapper functions
(`mirror_remove_field_nation_by_cluster_hash`,
`mirror_mark_fn_posted_by_cluster_hash`,
`mirror_set_fn_provider_by_cluster_hash`,
`mirror_set_fn_route_plan_id_by_cluster_hash`), each resolving `work_order`
via the same `field_nation_orders.payload ->> 'cluster_hash'` lookup and
delegating to the existing plain `remove_field_nation()` / `mark_fn_posted()`
/ `set_fn_provider()` / `set_fn_route_plan_id()`. Same no-op-never-raise
contract as the rest of the family — returns `{"success": False, "skipped":
...}` if no matching row exists yet.

**What changed in `fn_utils.py` and `tactical_workspace_master_rw.py` (the
actual wiring):**

- `fn_utils.save_fn_provider()` (the `setFnProvider` call site) gained an
  optional `db_engine` parameter, same pattern as `save_fn_to_sheet()`. Its
  one call site (the FN tab's "Assigned Provider" text input handler) now
  passes `db_engine=DB_ENGINE`.
- `background_fn_revoke()` (the `removeFieldNation` call site, called from
  `revoke_field_nation()`) now also calls
  `_da.mirror_remove_field_nation_by_cluster_hash()` best-effort, guarded by
  `if DB_ENGINE is not None`.
- All three `markFNPosted` call sites (per-route, bulk-pod, bulk-digital)
  now call `_da.mirror_mark_fn_posted_by_cluster_hash()` once per
  `cluster_hash` — GAS's `markFNPosted` accepts a comma-separated list for
  the two bulk sites, but the mirror function takes one hash at a time, so
  the bulk call sites loop over the same set of hashes they already send GAS
  (`_sel_pending` for bulk-pod, `_fn_selected` for bulk-digital).
- The `setFnRoutePlanId` call site (inside `assign_tasks_to_fn_team()`, right
  after a new OnFleet route plan is created) now also calls
  `_da.mirror_set_fn_route_plan_id_by_cluster_hash()` best-effort.

All four covered by a new test file,
`migration/tests/test_fn_remaining_mirrors.py`, against a real local
Postgres: proves the `cluster_hash → work_order` resolution is correct for
each, that every mirror no-ops cleanly (never raises) on an unknown
`cluster_hash`, and that the three per-field mirrors (`provider`,
`route_plan_id`, `status`) can all land on the same row without clobbering
each other. Also smoke-tested `fn_utils.save_fn_provider(..., db_engine=...)`
end-to-end (background thread, GAS POST mocked) — confirms the provider
lands in Postgres when `db_engine` is set, and that Postgres is left
untouched with the real GAS POST still firing when it's `None` (today's
Railway default).

**`bulkSetFnProvidersByAddress` is the one FN action still not wired, and
that's a scope boundary rather than an oversight:** `fn_utils.bulk_save_fn_providers_by_address()`
exists and `data_access.bulk_set_fn_providers_by_address()` already has a
matching mirror-ready implementation (it matches on `address` inside every
`posted` order's payload, no `cluster_hash` needed) — but `bulk_save_fn_providers_by_address()`
is never called anywhere in this repo today (confirmed by grep). Its
docstring says it's "designed for the browser-extension path" — the
Chrome extension that scrapes FN.com posts straight to GAS, bypassing this
Python app entirely, the same way `processDecision` bypasses it via the
portal. There is no call site in `tactical_workspace_master_rw.py` to hook
a dual-write onto, so none was added — same treatment `processDecision` got
in Step 4.

**Net effect:** the entire Field Nation family that this app's own UI
drives (`saveToFieldNation`, `markFNAssigned`, `setFnProvider`,
`removeFieldNation`, `markFNPosted`, `setFnRoutePlanId`) now dual-writes to
Postgres best-effort, alongside the still-authoritative GAS/Sheets path.
Only `bulkSetFnProvidersByAddress` (driven externally, not from this app)
and the Onfleet/Monday side effects inside `saveToFieldNation`/
`markFNAssigned` remain unmirrored.

### Step 8 (2026-09-22): read-side reconstruction built — NOT wired to the live app

The next piece flagged back in "Step 4/5, in practice" (top of this doc) as
needing to be "built and clicked through against a real staging database,
not written blind": `_cached_fetch_sent_records_from_sheet()`
(`tactical_workspace_master_rw.py`, ~380 lines) — the function every
dispatcher's Ready/Sent/Accepted/Field Nation view is built from. Now that
Step 4 and Step 7 have this app dual-writing `routes` and
`field_nation_orders` in the background, there's finally real Postgres data
to reconstruct that view from and test against.

**Built:** `data_access.get_sent_records_from_db(engine, pod_configs,
state_map)` — same four-tuple return shape as the sheet function
(`sent_dict, ghost_routes, archived_wos, history_db`), so a future swap of
the actual call sites is a substitution, not a rewrite of the surrounding
UI code (same design goal this whole module states in its top docstring).
`pod_configs` / `state_map` are passed in (the app's `POD_CONFIGS` /
`STATE_MAP` constants) rather than imported, so `data_access.py` stays free
of app/UI config and this new function is testable standalone like
everything else in this module.

**Deliberately a parallel implementation, not a refactor.** The sheet-CSV
loop's ~170-line per-row derivation logic (pod/state guessing, digital-ghost
detection, kiosk-count fallback, FN-provider/FN-posted hydration, ghost hash
construction) was duplicated into a new `_ingest_sent_record()` helper
rather than extracted out of `_cached_fetch_sent_records_from_sheet()` for
both paths to share. Extracting it would touch code that runs live in
production on every cache miss today — this migration has been careful to
keep every Phase 2 change purely additive (new function, new best-effort
dual-write) and never a modification of anything the dispatcher's current
view depends on, and there was no reason to break that pattern here. If the
two paths are ever proven equivalent on staging and the sheet path is
retired, unifying them then removes the duplication with only one live path
left to risk breaking.

**Three known gaps found while building this** (all documented in
`data_access.py`'s "Read-side reconstruction" section docstring, right
above `_normalize_ts`):

1. **Ghost-only revoke/re-route/ghost-archive history cannot be
   reconstructed from Postgres.** `archive_route()`'s dual-write does
   `UPDATE routes ... WHERE wo = :wo` and logs the event via `INSERT INTO
   route_events SELECT id, ... FROM routes WHERE wo = :wo` — for a "Ghost
   Archived" cluster that was never `saveRoute()`'d (a pure
   OnFleet-reconstructed ghost with no `routes` row), that `SELECT` matches
   zero rows, so *nothing* lands in `route_events`. GAS's Archive tab has no
   such requirement — it can archive a ghost regardless of whether it was
   ever "saved" — so the sheet path's `history_db` has entries this
   function structurally cannot produce. This is a write-side gap (or a
   deliberate design tradeoff — `route_events.route_id` is a real foreign
   key, on purpose, so every event is traceable to a route), not something
   fixable from the read side. Confirmed with a test
   (`migration/tests/test_get_sent_records_from_db.py`) rather than left as
   a theoretical concern.
2. **FN order contractor/IC name is assumed to be `payload["icn"]`.**
   `field_nation_orders` has no `contractor_name` column of its own (every
   row's real "contractor" is the FN vendor, not an IC — see the table's
   comment in `schema.sql`), and this app's own `fn_payload` always sets
   `icn: "Field Nation"` when posting. What the *actual* GAS sheet's
   "Contractor" column holds for these rows isn't visible from this repo
   (`Code.gs` lives outside it), so this is an assumption, not a confirmed
   match — worth checking against a real FN sheet row before trusting it
   for anything user-facing.
3. **A real bug this surfaced (fixed here, not just documented):**
   `mark_fn_assigned()` was building the new `routes` payload (`
   assigned_to_fn`, `fn_assigned_ts`, `wo`) without carrying `fn_provider`
   forward onto it — even though `set_fn_provider()` already stamps
   `fn_provider` onto the *`field_nation_orders`* row's payload
   specifically so `mark_fn_assigned()` can resolve it (see that function's
   own docstring). The resolved `provider` variable was being used to build
   the new WO string but never written back into the payload that actually
   lands on the promoted `routes` row, so any Postgres-only read of an
   FN-assigned route would show a bare "FN" instead of "FN: <name>"
   regardless of provider. Fixed by stamping `payload["fn_provider"] =
   provider` alongside the other new fields — a pure data-completeness
   addition to a dict already being constructed and saved, doesn't change
   any existing side effect. Covered by both
   `test_process_decision_and_fn_flow.py` (existing) and the new
   `test_get_sent_records_from_db.py`.

**Verified with `migration/tests/test_get_sent_records_from_db.py`**
against real local Postgres, seeded through the same write-path functions
the live app already dual-writes through (not hand-crafted rows): sent /
accepted / declined / finalized routes each land (or correctly don't land,
for declined) in the right pod's `ghost_routes`; an FN-posted order shows up
as `status_label="field_nation"` with `fn_posted_ts`/`fn_provider`
hydration; an FN-assigned order correctly disappears from the
`field_nation` set and reappears as an `accepted` route with its provider
intact (gap 3 above, before the fix, this assertion failed); Revoked /
Re-Routed / Ghost Archived archive events produce the right `history_db`
status strings and populate `archived_wos`; archived routes never leak into
`ghost_routes` (matches the sheet path, which only reads its Archive tab
for `archived_wos`/history, never for the live view); a jobOnly trigger
word routes to `Global_Digital` regardless of the address's actual state;
an unrecognized state produces no ghost entry anywhere but still populates
`sent_dict`/`history_db`; and `cutoff_date` filtering excludes routes
created before it. The gap-1 no-op is explicitly asserted too, not just
described in a comment.

**Not done, on purpose:** nothing in `tactical_workspace_master_rw.py` calls
this yet. Swapping `fetch_sent_records_from_sheet()`'s call sites over is a
separate, later decision — the same "no live cutover without a supervised
test" policy that already governs every write-side call site in this
migration (`saveToFieldNation`/`markFNAssigned`'s real OnFleet/Monday side
effects, `processDecision`'s OnFleet auto-assign). This function existing
and being tested against real local data is what makes that eventual
staging comparison possible — it isn't the comparison itself.

### Step 8 continued (2026-09-22, same day): a safe way to actually look at it

`get_sent_records_from_db()` had only ever been checked from a test file
run by hand in this sandbox — never from inside the running app, against
whatever real data Postgres actually has today. Built a preview so that
comparison can happen without it being, or turning into, a live cutover.

**Added to `tactical_workspace_master_rw.py`:** a new debug panel, gated
behind `?debug=readside` in the URL **and** `_is_admin_or_manager()` — the
stricter of the two gates this file's existing `?debug=` panels use, since
this one touches real route data rather than just Onfleet's API. Sits at
module level (same place as the other `?debug=` panels), renders nothing
unless both conditions are true, and does nothing at all until an
ADMIN/MANAGER clicks "Run comparison" inside it — no automatic query on
page load, no new background work for ordinary dispatchers.

**What it does:** runs `get_sent_records_from_db()` against Postgres and
`fetch_sent_records_from_sheet()` against the live Sheets (the exact same
call every other part of the app already makes) side by side, then shows:
top-line counts for all four return values; how many task IDs the two
paths agree even exist (`_both` / `_only_sheet` / `_only_db`); and, for the
tasks both paths agree exist, whether they agree on `status` and `wo` too
— that last check is the one that would actually catch a logic bug between
the two implementations, as opposed to just a "Postgres doesn't have this
route yet" coverage gap. A checkbox reveals the raw `ghost_routes` Postgres
produced, by pod, for a closer look.

**Explicitly not a cutover mechanism:** it never assigns anything back
into `st.session_state` beyond what `fetch_sent_records_from_sheet()`
already does on every normal render (the existing `_fn_posted`/
`_fn_provider` hydration), never writes to Postgres or Sheets, and doesn't
change what any dispatcher below it on the page sees. It's a way for Nick
and me to look at the comparison together, live, ahead of any real
decision — not a soft-launch of the read side.

**Verified:** re-seeded `dcc_test2` via `test_get_sent_records_from_db.py`,
then ran the panel's exact comparison/rendering logic (counts table,
overlap/mismatch cross-check including a forced-mismatch case, the
no-overlap case, and the per-pod raw dataframe view) standalone against
that real data — all paths run clean, no exceptions. Full Streamlit
end-to-end wasn't feasible from this sandbox (no Onfleet/Sheets
credentials here), so this is the same level of verification the rest of
Step 8 got: real Postgres, real write-path-seeded data, not a live
staging click-through.
