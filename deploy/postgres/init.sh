#!/bin/sh
# Creates the login roles. Their permissions come from the doclib_rw and doclib_ro groups in the schema migration.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
create role doclib_rw nologin;
create role doclib_ro nologin;
create role doclib_worker login password '$(cat /run/secrets/doclib_worker_password)' in role doclib_rw;
create role doclib_api login password '$(cat /run/secrets/doclib_api_password)' in role doclib_ro;
SQL
