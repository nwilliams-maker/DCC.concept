# DCC Revamp

This branch copies the current DCC files into the separate `DCC.concept`
repository. The original DCC repository and its Railway project remain
independent.

## Interface

Set `DCC_REVAMP_UI=1` in the revamp Railway service. The default **Dispatch**
workspace follows the list/detail reference: search, pod selector, status
filters, a scrolling route list, and one selected route with the original
dispatch controls. **Full tools** opens the copied DCC interface, including
its Field Nation, Digital, sent/accepted, maps, bundling, and other operations.
Only one route's dispatch widgets are rendered in the new workspace at a time.

The **Over 50 mi** view uses the closest eligible contractor with valid
latitude/longitude from the contractor table. Eligible means Active, In
Training, or Need Insurance. A route whose closest eligible contractor is
more than 50 miles away appears in Flagged and Over 50 mi. Missing contractor
coordinates remain unknown rather than being mislabeled as far away.

Select eligible Ready/Flagged routes and choose **Assign to Field Nation** to
save each route using the existing Postgres function and send its tasks to the
Field Nation Onfleet team/worker. Repeated clicks skip previously saved route
hashes. This action is disabled until the database, FN team, and FN worker
are present. Test this with nonproduction services before enabling real
external credentials; this action changes work orders and Onfleet tasks.

## Isolated Railway setup

The separate Railway project is **DCC Revamp**. Deploy this repository from
the `revamp/reference-layout` branch and configure it independently. Required
for a functional dispatcher session: `ONFLEET_KEY`, `MAPBOX_TOKEN`,
`DATABASE_URL`, `STAY_SALT`, `DCC_SHARED_SECRET`, and any existing app login
variables. Set `DCC_REVAMP_UI=1`. Do not copy credentials into GitHub. The
connected Railway integration exposes names but withholds existing secret
values, so they must be supplied in Railway's secure Variables screen.

The first deployment can verify packaging and startup, but live route
operations require those variables and a deliberately chosen database.
