#!/bin/sh
# Runs all tests inside Docker, on a network with no internet access.
set -eu
cd "$(dirname "$0")/.."
compose="docker compose -f deploy/test/compose.yml"
$compose build -q
status=0
$compose run --rm tests "$@" || status=$?
$compose down -v --remove-orphans >/dev/null 2>&1
exit "$status"
