# AGENTS.md

This file provides guidance to coding agents when working with code in this repository. `CLAUDE.md` is a symlink to it.

## Project Overview

atp2osm-import is a tool for importing [All The Places](https://alltheplaces.xyz) (ATP) data into OpenStreetMap (OSM). It focuses on French (metropolitan) POIs, matching ATP entries to existing OSM nodes/relations by spatial proximity (500m) and attribute similarity (brand, name, email, phone, website). A Flask web UI lets authenticated OSM users review, validate, and bulk-upload tag changes.

## Language

Code is written **in English**: comments, docstrings, variable, function and test names. No French in code, ever. This file too.

Log messages and the output of the `scripts/` maintenance tools count as code, and are English too.

User-facing text is written in English too, and translated through gettext.
A label in a template is `{{ _('Brands to integrate') }}`; a sentence with a
variable, a plural or inline markup is a `{% trans %}` block. French is one
catalog among others, in `website/translations/fr/`.

Nothing translatable lives in a module constant: a string there is read before
any request exists, so it has no locale to resolve against. The keys stay in
Python — they are data — and the labels go to the template that displays them
(`RANGES`, `ERROR_REASONS`, `PUBLIC_PAGES` all work that way).

The scripts get their strings from `_js_strings.html`, rendered as a JSON block
and read by `t()` in `static/js/i18n.js`. Babel does not read `.js`.

`./scripts/i18n.sh` extracts, updates and compiles the catalogs — run it after
touching a translatable string, and fill the empty `msgstr` it leaves behind.
The `.mo` files are build artefacts: gitignored, compiled in the image.

The OSM changeset comment follows the contributor's language, which `LOCALES`
constrains to the languages of the country served.

## Commits

Commit messages are written in English too, in the imperative ("Translate…",
"Add…", "Fix…"), one subject line and, when the change deserves it, a body
explaining the why. No `Co-Authored-By` trailer, ever.

## Worktrees

Git worktrees **always** live in `.worktrees/<name>` at the project root, never anywhere else (and definitely not under `.claude/`). `dev.sh` resolves worktree names from that directory.

## Commands

```bash
# Run the app for a worktree (port derived from the name, .env and config.json
# symlinked from the main checkout)
./dev.sh up [-d] [name]     # start (-d = detached)
./dev.sh down [name]        # stop
./dev.sh logs [name] [-f]   # show the logs

# Install dependencies
uv sync

# Run the Flask server (development)
ATP2OSM_CONFIG=./config.json uv run --env-file .env flask --app ./src/app.py run --debug

# Production: app runs via gunicorn inside a container (see Containerfile)
# Deploy is triggered by git push to the server (deploy/run hook)

# Run tests — --env-file is what gives them OSM_DB_PASSWORD, without which the
# throwaway test database cannot be created and every database test errors out
uv run --env-file .env pytest
uv run --env-file .env pytest tests/test_compute_diff.py            # single file
uv run --env-file .env pytest tests/test_compute_diff.py::test_apply_on_node_default  # single test

# Start infrastructure (PostGIS database)
podman-compose up -d

# Import OSM PBF data into PostGIS (local dev, via container)
podman-compose run osm2pgsql osm2pgsql --output flex -S /osm2pgsql/generic.lua -d o2p -U o2p -H 127.0.0.1 -P 5432 /data/osm/<file>.osm.pbf

# Refresh all data (ATP + OSM) — runs daily from supercronic inside the
# refresh container in production (python -m src.pipeline setup)
# Manual trigger on server:
#   ./run-pipeline.sh
# Manual trigger locally:
#   ATP2OSM_CONFIG=./config.json OSM_DB_PASSWORD=... ./run-pipeline.sh
# Rebuild everything, ignoring what data_imports and the table stamps say
# was already done (a file the code now reads differently, a doubt):
#   ATP2OSM_FORCE=1 ./run-pipeline.sh

# Import a fraction of the country instead of the nine extracts: shorten the
# `geofabrik` list of a configuration file of your own, and point
# ATP2OSM_CONFIG at it — nothing in the code knows about a dev shortcut.
```

## Architecture

**Data pipeline** (runs outside the web server, via `run-pipeline.sh` and `src/pipeline/`):
1. `run-pipeline.sh` — Entry point of the daily refresh: runs `src/pipeline` inside the container via podman, for a manual run. Copied into the project directory on every deploy. The daily run is scheduled by `python -m src.pipeline setup` (supercronic) in the long-lived `refresh` container, at `app.refresh_schedule` in the country's timezone; a restart during a run waits for it to finish. A branch no-ops when nothing it depends on has moved — *including its own code*: see **Rebuild guards** below.
2. `src/pipeline/` — Python module orchestrating the whole pipeline: OSM PBF download from Geofabrik, osm2pgsql import, ATP parquet download, load into `atp_places` through DuckDB, materialized view refresh.
3. `osm2pgsql/generic.lua` — Flex output style that imports OSM PBF into `points`, `polygons` and `subdivisions` tables in PostGIS (SRID 4326). Administrative boundaries take a separate path, before the POI filters, down to `ATP2OSM_ADMIN_LEVEL_MAX` (`country.admin_level_max`, 8 in France) — deeper than `country.admin_level`, the level the attachment reads, so lowering that one is a SQL filter rather than a reimport. Two filters run on the POIs: objects that are definitely not places (roads, boundaries, transport…) and objects carrying none of the attributes a match can key on — no name, brand, email, phone or website. The second one drops ~95% of the objects.

**Rebuild guards** — a step never decides on the freshness of its source alone. Editing `generic.lua`, `atp.py` or the NSI constants changes what the tables contain while the upstream timestamp stays put, so a date-only guard holds the change back until the source happens to publish. Production ran that way once: code expecting a `subdivisions` table, and a database that had none.

The deployed revision answers it — `_version.app_version()`, which is `get_version()`, the commit `deploy/run` passes as `GIT_COMMIT` and that `src/config.py` refuses to start without in production. Two independent triggers, either of which rebuilds: **new data** or **new revision**. Coarser than digesting hand-picked sources, and more reliable — it also covers `MATCHED_POI_SQL`, `ndgeojson_to_parquet.py` and everything else a per-import digest would miss.

Where it is compared: stamped on `points` and gating the PBF download; recorded as the ATP import's comment; folded into the NSI stamp next to the published version. Downstream, `_matview.signature()` takes it beside the freshness of each datasource — a reimport the deploy triggered moves no date, so a view guarded on dates alone would keep its stale rows. Nothing else needs to go in: the view's own SQL and the bodies of the SQL functions it calls are code, so the revision already covers them. Whatever an object *reads*, pass it there.

In development the version is a constant, so nothing rebuilds on its own: rerun the step by hand (`python -m src.pipeline step osm-import`).

**Swaps** — a rebuilding step never drops what the site reads. It builds the object beside the live one (`mv_places_new`, `atp_places_new`, the `osm_import` schema osm2pgsql writes into) and `_matview.swap()` renames it in at the end, in the transaction that records the import: the exclusive lock is held for a rename, and a failed build leaves the live object as it was. The live one retires as `<name>_old` rather than being dropped — `mv_places_brand` is materialized on `mv_places`, `mv_places` on `points`, and each keeps serving until its own swap. `mv-brand` disposes of the retired chain once it has swapped the brand view, and only what nothing depends on any more. So the site serves throughout a refresh; the `pending` row of `data_imports` is a status the home page shows, not a maintenance flag.

**Deploy** (`deploy/run` — git hook `post-receive`):
- Builds the container image, writes the `atp2osm.container` and `refresh.container` Quadlets from the `deploy/` templates, then runs `daemon-reload` + `restart` directly. Everyone else deploys through the root `compose.yml`, which runs the same three containers.
- One-time server-side provisioning: `loginctl enable-linger $USER` (keeps the services running without an open session).

**Web application** (`src/app.py`, Flask):
- Uses PostGIS with psycopg3, connection per-request via Flask `g`
- OSM OAuth2 authentication; the token lives in the Flask session cookie, signed with `secret_key` (nothing server-side to share between workers)
- Templates in `website/templates/`, static assets in `static/`
- SQL migrations in `migrations/` auto-run at startup (`src/migrate.py`), tracked in `schema_migrations` table

**Core modules:**
- `src/matching.py` — Spatial join queries between `mv_places` and `atp_places` (`MATCHED_POI_SQL`, shared with the `mv_places_brand` view and never duplicated), tag diffing logic (`apply_on_node`), batch composition (`pack_subdivisions`, `select_batch`), the `WAVES` list and the cooldown SQL it keys, stats aggregation
- `src/osm_history.py` — Wave 2's guard: dates the current value of each tag a diff would overwrite, through the OSM API, and leaves alone what a human posted within `app.recent_edit_weeks`
- `src/upload.py` — `BulkUpload` class that creates OSM changesets grouped by subdivision, uploads via `osmapi`
- `src/migrate.py` — Simple sequential SQL migration runner

**Pipeline tables and the site (read before touching a pipeline table).**
`points`, `atp_places`, `atp_spiders`, `subdivisions`, `mv_places*` are built
by the pipeline, not by migrations — so a deploy that adds a column the site
reads breaks production until the next refresh rebuilds the table (once a
night at best). Every column the web app reads from a pipeline table ships
with a migration that adds it to the live table when it is missing, with a
value the SQL already tolerates:
`ALTER TABLE IF EXISTS atp_spiders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;`
(`migrations/027`). Same for a renamed or dropped column: the migration
bridges, the pipeline then overwrites. No exception for "it will be rebuilt
tonight".

**Key database objects:**
- `points`, `polygons` — Raw OSM data (from osm2pgsql)
- `mv_places` — Materialized view joining both with normalized columns, restricted to objects a match can key on (same filter as `generic.lua`, kept as a safety net)
- `subdivisions` — OSM administrative boundaries (from osm2pgsql). Each ATP POI is attached to the finest one containing it, walking down from `country.admin_level` to the country
- `mv_places_brand` — Match count per (brand, subdivision, wave); `get_all` sums the subdivisions that are not under cooldown, on the brand's current wave
- `atp_places` — ATP data filtered to the country served
- `import_history` — One row per human integration action, with the `wave` it belonged to (which is also what confines its cooldown)
- `import_subdivisions` — One row per changeset: subdivision code and name, count, status, and the `tag_counts` frozen at integration time. Carries the per-subdivision blocking and the history detail

## Specs

Functional specs live in `specs/`, prefixed with a two-digit id in creation
order (`01_`, `02_`…). They state the intended behaviour, not the history of
the decisions that led to it.

## Configuration

Everything that is not a secret lives in one JSON file, named by
`ATP2OSM_CONFIG` — no default, so an instance that provides none refuses to
start rather than quietly serving France. No country file ships with the
product, not even the French one.

The name-suggestion-index scope follows from it too: `nsi_locations` holds the
country, its mainland code, its territories and the codes that contain it
(001, 150, eu). An NSI item applies when its locationSet includes one of them
and excludes none. `nsi_writable_tags` is configuration for the same reason: the list is produced
by measuring, tag by tag, how often NSI agrees with the country's own OSM
objects — `scripts/calibrate_nsi_tags.py` writes it, nobody edits it by hand,
and it is never copied from another country.

`calling_codes` and `trunk_prefix` are what lets the international and the
national writing of a number meet: a country answers to several codes, and the
first one is its mainland's — the special-rate and short-number rules key on
that one, since those numbering plans are the mainland's.

**`config.schema.json` is the documentation**: every setting is described where
it is declared, and the file is validated against it at startup. Read it before
asking what a key does, and add a `description` when you add a key. Only what a
schema cannot express stays in `src/config.py`: a language Babel knows, a real
IANA timezone, `admin_level_max` above `admin_level`.

A complete example lives in the schema's own `examples`, so there is one file
to keep in step instead of two — copy it out with
`jq '.examples[0]' config.schema.json`. A test loads it, so it cannot drift.

The image ships the catalogs of `website/translations/`, which are maintained
with the templates they come from. `app.translations_dir` points at catalogs
the deployment adds: they are merged over the shipped ones and win on the
strings they both hold, so a language the product does not ship needs no fork.

Secrets stay in the environment, `.env` today and sops tomorrow:
`OSM_DB_PASSWORD`, `OSM_OAUTH_CLIENT_ID`, `OSM_OAUTH_CLIENT_SECRET`,
`SECRET_KEY`. So does `GIT_COMMIT`, which the build computes.

In development, `config.json` sits in the main checkout, gitignored, and
`dev.sh` symlinks it into every worktree next to `.env`.

## Testing

Tests use pytest with `--import-mode=importlib` and pythonpath set to `.` (see `pyproject.toml`). The test file currently imports from `src.compute_diff` which corresponds to functions now in `src.matching`.

A test never reads the development database: its content is nobody's
guarantee — an interrupted pipeline leaves rows behind, and a test reading
them fails for reasons of its own. `conftest.py` builds a throwaway
`atp2osm_test_<pid>` database instead — one per process, so two worktrees can
run their suites at once — dropped when the session ends, and the tests
take one of its two fixtures: `migrated_conn` for the real schema (migrations
applied, tables emptied before each test), `db_kwargs` for a test that builds
a schema of its own — a partial migration history, a table shaped like
osm2pgsql's.

A database it cannot build is an **error**, never a skip: a test that does not
run controls nothing, and a suite reporting green on a third of its tests is
worse than a red one. `podman-compose up -d` is a prerequisite of `pytest`,
and so is `--env-file .env`: the database password is a secret, and the suite
has none of its own.

A route is tested through `web_app` — the site's blueprints, real templates,
filters and globals, a connection per request on the throwaway database —
and `contributor`, a client signed in on it. Never through `src.app`:
importing it runs the migrations against the development database. What a
test controls is what feeds the route (a staged `brand_matches`, a fake
`mv_places_brand`), what it asserts is the rows written and the page
rendered, read through Flask's `template_rendered` signal.

No test reaches the network: `conftest.py` refuses every `requests` call,
and a test that needs an answer stages it on the function that would have
asked. A pipeline step runs for real (`tests/test_rebuild_guards.py`), with
osm2pgsql replaced by a function that writes the tables it would, and both
`connect` and `get_database` of the step's module pointed at the test
database — DuckDB and osm2pgsql are handed the settings, not a connection.

The OSM API is never called: in development `BulkUpload` drives
`_FakeOsmApi`, which records the calls and writes their OSC instead of
sending them. So the upload tests exercise the very code path production
takes — there is no branch that only fires under test. A test that wants a
failure stages it on that fake, and turns the OSC writing off so it litters
no checkout.
