#!/bin/sh
# Generates local secrets for docker compose. Never commit deploy/secrets.
#
# Bundled Postgres (deploy/compose.postgres.yml): run this and you're done.
# Existing cluster: write deploy/secrets/admin_dsn, worker_dsn and api_dsn yourself, using the roles from
# deploy/postgres/existing-cluster.sql. This script never overwrites a DSN file that already exists.
set -eu
dir="$(dirname "$0")/../deploy/secrets"
mkdir -p "$dir"
chmod 700 "$dir"
for role in admin worker api; do
  [ -f "$dir/${role}_password" ] || openssl rand -hex 24 > "$dir/${role}_password"
done
[ -f "$dir/admin_dsn" ] || printf 'postgresql://postgres:%s@postgres:5432/doclib\n' "$(cat "$dir/admin_password")" > "$dir/admin_dsn"
[ -f "$dir/worker_dsn" ] || printf 'postgresql://doclib_worker:%s@postgres:5432/doclib\n' "$(cat "$dir/worker_password")" > "$dir/worker_dsn"
[ -f "$dir/api_dsn" ] || printf 'postgresql://doclib_api:%s@postgres:5432/doclib\n' "$(cat "$dir/api_password")" > "$dir/api_dsn"
[ -f "$dir/admin_token_secret" ] || openssl rand -hex 32 > "$dir/admin_token_secret"  # Signs tokens for the admin API.
chmod 644 "$dir"/*   # Containers read these as a different user; the directory itself is private.
echo "secrets written to $dir"
