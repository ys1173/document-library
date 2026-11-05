-- One-time setup for running doclib on an existing Postgres cluster. Run it as a DBA, against the target database:
--
--   psql "postgresql://dba@main-cluster:5432/platform" -v ON_ERROR_STOP=1 \
--        -v owner_password="$OWNER" -v worker_password="$WORKER" -v api_password="$API" \
--        -f deploy/postgres/existing-cluster.sql
--
-- Safe to run again. Existing roles keep their passwords. Nothing here touches other schemas or data.
-- Roles are cluster-wide; the extension and schema belong to the database you connect to.
--
-- Roles created:
--   doclib_owner   Runs `doclib migrate` and owns the doclib schema. Not a superuser.
--   doclib_worker  Scanner, workers and admin API. Reads and writes table rows only (through doclib_rw).
--   doclib_api     Agent-facing API. Reads only, plus doclib.request_reindex() (through doclib_ro).

-- Needs superuser, or a pgvector build marked trusted. Requires pgvector 0.5 or newer; 0.8 or newer is recommended.
-- If your cluster keeps extensions elsewhere (CREATE EXTENSION vector SCHEMA extensions), that works too, but
-- also GRANT USAGE on that schema to doclib_owner, doclib_rw and doclib_ro.
create extension if not exists vector;

select 'create role doclib_rw nologin'
 where not exists (select from pg_roles where rolname = 'doclib_rw') \gexec
select 'create role doclib_ro nologin'
 where not exists (select from pg_roles where rolname = 'doclib_ro') \gexec

select format('create role doclib_owner login nosuperuser nocreatedb nocreaterole password %L', :'owner_password')
 where not exists (select from pg_roles where rolname = 'doclib_owner') \gexec
select format('create role doclib_worker login nosuperuser nocreatedb nocreaterole password %L', :'worker_password')
 where not exists (select from pg_roles where rolname = 'doclib_worker') \gexec
select format('create role doclib_api login nosuperuser nocreatedb nocreaterole password %L', :'api_password')
 where not exists (select from pg_roles where rolname = 'doclib_api') \gexec

grant doclib_rw to doclib_worker;
grant doclib_ro to doclib_api;

grant connect on database :"DBNAME" to doclib_owner, doclib_worker, doclib_api;

create schema if not exists doclib authorization doclib_owner;
