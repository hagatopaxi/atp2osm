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
# Run the app for a worktree (port derived from the name, .env symlinked from the main checkout)
./dev.sh up [-d] [name]     # start (-d = detached)
./dev.sh down [name]        # stop
./dev.sh logs [name] [-f]   # show the logs

# Install dependencies
uv sync

# Run the Flask server (development)
uv run --env-file .env flask --app ./src/app.py run --debug

# Production: app runs via gunicorn inside a container (see Containerfile)
# Deploy is triggered by git push to the server (deploy/run hook)

# Run tests
uv run pytest
uv run pytest tests/test_compute_diff.py            # single file
uv run pytest tests/test_compute_diff.py::test_apply_on_node_default  # single test

# Start infrastructure (PostGIS database)
podman-compose up -d

# Import OSM PBF data into PostGIS (local dev, via container)
podman-compose run osm2pgsql osm2pgsql --output flex -S /osm2pgsql/generic.lua -d o2p -U o2p -H 127.0.0.1 -P 5432 /data/osm/<file>.osm.pbf

# Refresh all data (ATP + OSM) — runs daily via systemd timer in production
# Manual trigger on server:
#   systemctl --user start atp2osm-gwenael-leger-fr-refresh.service
# Manual trigger locally:
#   OSM_DB_NAME=o2p OSM_DB_USER=o2p OSM_DB_PASSWORD=... OSM_DB_HOST=127.0.0.1 OSM_DB_PORT=5432 ./run-pipeline.sh

# Import a fraction of the country instead of the nine extracts (dev only)
ATP2OSM_GEOFABRIK_PATHS=europe/france/provence-alpes-cote-d-azur,europe/france/martinique
```

## Architecture

**Data pipeline** (runs outside the web server, via `run-pipeline.sh` and `src/pipeline/`):
1. `run-pipeline.sh` — Entry point of the daily refresh: runs `src/pipeline` inside the container via podman. Copied into the project directory on every deploy. Triggered by a systemd timer (04:00 Europe/Paris). A branch no-ops when nothing it depends on has moved — *including its own code*: see **Rebuild guards** below.
2. `src/pipeline/` — Python module orchestrating the whole pipeline: OSM PBF download from Geofabrik, osm2pgsql import, ATP parquet download, load into `atp_fr` through DuckDB, materialized view refresh.
3. `osm2pgsql/generic.lua` — Flex output style that imports OSM PBF into `points`, `polygons` and `subdivisions` tables in PostGIS (SRID 4326). Administrative boundaries take a separate path, before the POI filters, down to `ATP2OSM_ADMIN_LEVEL` (6), the finest level the attachment reads. Two filters run on the POIs: objects that are definitely not places (roads, boundaries, transport…) and objects carrying none of the attributes a match can key on — no name, brand, email, phone or website. The second one drops ~95% of the objects.

**Rebuild guards** — a step never decides on the freshness of its source alone. Editing `generic.lua`, `atp.py` or the NSI constants changes what the tables contain while the upstream timestamp stays put, so a date-only guard holds the change back until the source happens to publish. Production ran that way once: code expecting a `subdivisions` table, and a database that had none.

The deployed revision answers it — `_version.app_version()`, which is `get_version()`, the commit `deploy/run` passes as `GIT_COMMIT` and that `src/config.py` refuses to start without in production. Two independent triggers, either of which rebuilds: **new data** or **new revision**. Coarser than digesting hand-picked sources, and more reliable — it also covers `MATCHED_POI_SQL`, `ndgeojson_to_parquet.py` and everything else a per-import digest would miss.

Where it is compared: stamped on `points` and gating the PBF download; recorded as the ATP import's comment; folded into the NSI stamp next to the published version. Downstream, `_matview.signature()` takes it beside the freshness of each datasource — a reimport the deploy triggered moves no date, so a view guarded on dates alone would keep its stale rows. Nothing else needs to go in: the view's own SQL and the bodies of the SQL functions it calls are code, so the revision already covers them. Whatever an object *reads*, pass it there.

In development the version is a constant, so nothing rebuilds on its own: rerun the step by hand (`python -m src.pipeline step osm-import`).

**Deploy** (`deploy/run` — git hook `post-receive`):
- Builds the container image, writes the `atp2osm.container` Quadlet, writes the `refresh.service` + `refresh.timer` systemd units from the `deploy/` templates, then runs `daemon-reload` + `restart` + `enable timer` directly.
- One-time server-side provisioning: `loginctl enable-linger $USER` (keeps the services running without an open session).

**Web application** (`src/app.py`, Flask):
- Uses PostGIS with psycopg3, connection per-request via Flask `g`
- OSM OAuth2 authentication; the token lives in the Flask session cookie, signed with `secret_key` (nothing server-side to share between workers)
- Templates in `website/templates/`, static assets in `static/`
- SQL migrations in `migrations/` auto-run at startup (`src/migrate.py`), tracked in `schema_migrations` table

**Core modules:**
- `src/matching.py` — Spatial join queries between `mv_places` and `atp_fr` (`MATCHED_POI_SQL`, shared with the `mv_places_brand` view and never duplicated), tag diffing logic (`apply_on_node`), batch composition (`pack_subdivisions`, `select_batch`), cooldown SQL, stats aggregation
- `src/upload.py` — `BulkUpload` class that creates OSM changesets grouped by department, uploads via `osmapi`
- `src/migrate.py` — Simple sequential SQL migration runner

**Key database objects:**
- `points`, `polygons` — Raw OSM data (from osm2pgsql)
- `mv_places` — Materialized view joining both with normalized columns, restricted to objects a match can key on (same filter as `generic.lua`, kept as a safety net)
- `subdivisions` — OSM administrative boundaries (from osm2pgsql). Each ATP POI is attached to the finest one containing it, walking down from `ADMIN_LEVEL` to the country
- `mv_places_brand` — Match count per (brand, subdivision); `get_all` sums the subdivisions that are not under cooldown
- `atp_fr` — ATP data filtered to metropolitan France
- `import_history` — One row per human integration action
- `import_subdivisions` — One row per changeset: subdivision code and name, count, status. Carries the per-subdivision blocking and the history detail

## Specs

Functional specs live in `specs/`, prefixed with a two-digit id in creation
order (`01_`, `02_`…). They state the intended behaviour, not the history of
the decisions that led to it.

## Configuration

Everything that is not a secret lives in one JSON file, named by
`ATP2OSM_CONFIG` — no default, so an instance that provides none refuses to
start rather than quietly serving France. No country file ships with the
product, not even the French one.

**`config.schema.json` is the documentation**: every setting is described where
it is declared, and the file is validated against it at startup. Read it before
asking what a key does, and add a `description` when you add a key. Only what a
schema cannot express stays in `src/config.py`: a language Babel knows, a real
IANA timezone, `admin_level_max` above `admin_level`.

A complete example lives in the schema's own `examples`, so there is one file
to keep in step instead of two — copy it out with
`jq '.examples[0]' config.schema.json`. A test loads it, so it cannot drift.

Secrets stay in the environment, `.env` today and sops tomorrow:
`OSM_DB_PASSWORD`, `OSM_OAUTH_CLIENT_ID`, `OSM_OAUTH_CLIENT_SECRET`,
`SECRET_KEY`. So does `GIT_COMMIT`, which the build computes.

In development, `config.json` sits in the main checkout, gitignored, and
`dev.sh` symlinks it into every worktree next to `.env`.

## Testing

Tests use pytest with `--import-mode=importlib` and pythonpath set to `.` (see `pyproject.toml`). The test file currently imports from `src.compute_diff` which corresponds to functions now in `src.matching`.
