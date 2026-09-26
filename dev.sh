#!/usr/bin/env bash
# Serveur Flask isolé pour un worktree git.
#
#   ./dev.sh up [nom]       # démarre en streamant les logs (worktree courant, ou .worktrees/<nom>)
#   ./dev.sh up -d [nom]    # démarre détaché, affiche l'URL et rend la main
#   ./dev.sh down [nom]     # arrête ce serveur
#   ./dev.sh logs [nom]     # affiche son log (-f pour suivre)
#
#   DEV_PORT=5000 ./dev.sh up -d [nom]   # sur le port du checkout principal,
#                                        # le seul que le callback OSM connaît
#
# Le checkout principal garde :5000 ; le port d'un worktree est dérivé de son nom,
# donc l'URL est stable et le port sert d'identité du process (down sans pidfile).
# Chaque worktree écrit son propre $wt/.dev.log. Le .env et le config.json sont
# symlinkés depuis le checkout principal — jamais copiés, jamais lus.
set -euo pipefail

# Share the versioned hooks (git never installs them on clone).
git config core.hooksPath .githooks

case "${1:-}" in
  up|down|logs) cmd="$1"; shift ;;
  *)            sed -n '2,7p' "$0" | cut -c3-; exit 1 ;;
esac

detach=""
if [ "$cmd" = up ] && [ "${1:-}" = "-d" ]; then detach=1; shift; fi
name="${1:-}"

main="$(cd "$(dirname "$(git rev-parse --git-common-dir)")" && pwd)"

if [ "$name" ] && [ -d "$main/.worktrees/$name" ]; then
  wt="$main/.worktrees/$name"
else
  wt="$PWD"; name="$(basename "$wt")"
fi
cd "$wt"

if [ "$wt" = "$main" ]; then
  offset=0
else
  offset=$(( $(printf %s "$name" | cksum | cut -d' ' -f1) % 90 + 1 ))
fi
port=$((5000 + offset))
# 5060/5061 (SIP) font partie des ports que les navigateurs refusent d'ouvrir.
while [ $port = 5060 ] || [ $port = 5061 ]; do port=$((port + 2)); done
# DEV_PORT=5000 ./dev.sh up -d <nom> : servir un worktree là où le callback
# OAuth est enregistré, le temps de tester ce qui demande d'être connecté.
# `down` et `logs` veulent le même DEV_PORT — le port est l'identité du process.
port="${DEV_PORT:-$port}"
# Hostname propre à chaque worktree : les cookies de session ignorent le port,
# sinon tous les localhost:50xx partageraient la même session OSM. Sur le port
# du checkout principal c'est l'inverse qu'il faut : OSM renvoie sur
# localhost:5000, et une session posée sur <nom>.localhost n'y survivrait pas.
host="localhost"
[ "$wt" = "$main" ] || [ "$port" = 5000 ] || host="$name.localhost"
log="$wt/.dev.log"
# Une seule définition : le motif a déjà divergé de la commande une fois, et
# `down` annonçait alors un arrêt qui ne tuait rien.
flask_args="--app ./src/app.py run --debug --port $port"
pattern="flask $flask_args"

if [ "$cmd" = down ]; then
  pkill -f "$pattern" || true
  sleep 1  # laisser mourir, sinon un `up` enchaîné croit que ça tourne encore
  echo "arrêté : $name (:$port)"
  exit 0
fi

if [ "$cmd" = logs ]; then
  [[ " $* " == *" -f "* ]] && follow="-f" || follow=""
  exec tail ${follow:+-f} -n 40 "$log"
fi

if pgrep -f "$pattern" >/dev/null; then
  echo "déjà lancé : $name → http://$host:$port"
  exit 0
fi

[ -e .env ] || ln -s "$main/.env" .env
[ -e config.json ] || ln -s "$main/config.json" config.json

# Les .mo sont des artefacts de build, donc absents d'un worktree neuf : sans
# eux le site sert les msgid anglais sans rien dire.
uv run pybabel compile -d website/translations >/dev/null 2>&1 || true

# The worktrees share the dev PostGIS database (OSM_DB_* in .env), which is
# enough to test; override OSM_DB_NAME if their migrations conflict.
: > "$log"
setsid bash -c "cd '$wt' && ATP2OSM_CONFIG='$wt/config.json' exec uv run --env-file .env flask $flask_args" >>"$log" 2>&1 &

echo "worktree : $name"
echo "app      : http://$host:$port"

if [ "$detach" ]; then
  # Le port fait l'identité du process : sans le même DEV_PORT, `down` viserait
  # le port dérivé du nom et n'arrêterait rien.
  echo "logs     : ${DEV_PORT:+DEV_PORT=$DEV_PORT }./dev.sh logs $name    stop : ${DEV_PORT:+DEV_PORT=$DEV_PORT }./dev.sh down $name"
  exit 0
fi

# Attaché : on suit le log jusqu'à Ctrl-C, puis on coupe le serveur.
# setsid l'isole du SIGINT, c'est le trap qui fait le travail.
trap 'pkill -f "$pattern" || true
      echo; echo "arrêté : $name (:$port)"; exit 0' INT
tail -f -n +1 "$log"
