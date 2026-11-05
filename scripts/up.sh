#!/bin/sh
# Starts everything on this machine: the service, Postgres and the embedding model.
#   scripts/up.sh            start
#   scripts/up.sh --vision   also start the vision model on CPU (needs at least 12 GB of Docker memory)
# Use your own documents with:  DOCLIB_SHARE=/path/to/documents scripts/up.sh
set -eu
cd "$(dirname "$0")/.."
[ -f deploy/secrets/worker_dsn ] || scripts/make-secrets.sh
files="-f deploy/docker-compose.yml -f deploy/compose.postgres.yml -f deploy/compose.embed.yml"
if [ "${1:-}" = "--vision" ]; then files="$files -f deploy/compose.vision.yml"; fi
docker volume inspect doclib-embed-models >/dev/null 2>&1 || {
  echo "The embedding model isn't downloaded yet. Run: scripts/fetch-models.sh" >&2; exit 1; }
# shellcheck disable=SC2086
docker compose $files up -d --build
echo
echo "Starting up. Check progress with:  curl -s http://localhost:8080/v1/list_sources"
