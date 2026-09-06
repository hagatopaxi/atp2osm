# ATP 2 OSM Import

In this project, All The Places data are imported into OpenStreetMap.

## Starting the containers

```
podman-compose up -d

podman-compose run osm2pgsql osm2pgsql --output flex -S /osm2pgsql/generic.lua -d o2p -U o2p -H 127.0.0.1 -P 5432 /data/osm/your-file.osm.pbf
```

## Install dependencies

```
uv sync
```

## Configure the instance

Two files, and neither ships with the product:

```
jq '.examples[0]' config.schema.json > config.json   # everything but the secrets
cp .env.sample .env                                  # the secrets, and nothing else
```

`config.json` says which country the instance serves, which languages it
speaks, which Geofabrik extracts it imports and where its database lives.
`ATP2OSM_CONFIG` names it and has no default: an instance that provides none
refuses to start rather than quietly serving France.

**`config.schema.json` is the documentation.** Every setting is described where
it is declared, and the file is validated against it at startup — a key it does
not know is refused rather than ignored, so a typo cannot silently leave a
setting on its default.

`.env` keeps what must not sit in a file meant to be read, mounted and diffed:
the database password, the OSM OAuth credentials and the session key.

## Start the server

`dev.sh` is the recommended way to run the app:

```
./dev.sh up          # start and stream the logs
./dev.sh up -d       # start detached, prints the URL
./dev.sh down        # stop it
./dev.sh logs -f     # follow the logs
```

It symlinks `.env` and `config.json` from the main checkout, picks a stable port (one per git worktree) and installs
the versioned git hooks (pre-push runs the tests). It takes an optional worktree
name: `./dev.sh up my-feature` serves `.worktrees/my-feature`.

Under the hood it is just Flask, if you prefer running it yourself:

```
ATP2OSM_CONFIG=./config.json uv run --env-file .env flask --app ./src/app.py run --debug
```

## Refresh the data

The pipeline downloads the OSM extracts and the ATP export, imports them and
rebuilds the matches. It reads the same configuration as the app, so shortening
the `geofabrik` list of a configuration of your own imports a fraction of the
country instead of all of it.

```
ATP2OSM_CONFIG=./config.json uv run --env-file .env python -m src.pipeline
```

A fresh database gets its schema from the app, which runs the migrations at
startup: start the server once before the first pipeline run.

In production it runs daily, on a systemd timer whose hour and timezone come
from the configuration.
