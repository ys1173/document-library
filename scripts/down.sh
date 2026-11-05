#!/bin/sh
# Stops everything. Add --delete-index to also delete the database (it can always be rebuilt from your documents).
set -eu
cd "$(dirname "$0")/.."
files="-f deploy/docker-compose.yml -f deploy/compose.postgres.yml -f deploy/compose.embed.yml -f deploy/compose.vision.yml"
if [ "${1:-}" = "--delete-index" ]; then
  # shellcheck disable=SC2086
  docker compose $files down -v --remove-orphans
else
  # shellcheck disable=SC2086
  docker compose $files down --remove-orphans
fi
