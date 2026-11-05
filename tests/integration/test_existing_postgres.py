"""Running on an existing shared cluster: no superuser, with the DBA script as the only privileged step."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from doclib import db
from doclib.config import Settings
from doclib.db import MigrationError, create_pool, migrate

DSN = os.environ.get("DOCLIB_TEST_DSN")
SCRIPT = Path(__file__).parents[2] / "deploy" / "postgres" / "existing-cluster.sql"
PASSWORD = "existing-cluster-test"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not DSN, reason="DOCLIB_TEST_DSN is not set")]


def dsn_for(database: str, user: str | None = None, query: str = "") -> str:
    parts = urlsplit(DSN)
    netloc = parts.netloc if user is None else f"{user}:{PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, f"/{database}", query, ""))


def settings_for(dsn: str) -> Settings:
    return Settings.model_validate({"postgres": {"dsn": dsn}, "embeddings": {"endpoint": "http://unused/v1", "dimension": 1024}})


def run_dba_script(database: str) -> None:
    done = subprocess.run(
        ["psql", dsn_for(database), "-q", "-v", "ON_ERROR_STOP=1", "-v", f"owner_password={PASSWORD}",
         "-v", f"worker_password={PASSWORD}", "-v", f"api_password={PASSWORD}", "-f", str(SCRIPT)],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


@pytest_asyncio.fixture
async def database():
    """A fresh database per test, as a stand-in for an existing shared cluster."""
    name = f"shared_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'create database "{name}"')
    yield name
    await admin.execute(f'drop database "{name}" with (force)')
    await admin.close()


@pytest_asyncio.fixture
async def prepared(database):
    run_dba_script(database)
    return database


async def as_superuser(database: str, sql: str, *args):
    conn = await asyncpg.connect(dsn_for(database))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def make_unprivileged_role() -> str:
    name = f"lonely_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    try:
        await admin.execute(f"create role {name} login nosuperuser nocreatedb nocreaterole password '{PASSWORD}'")
    finally:
        await admin.close()
    return name


async def test_migrate_as_non_superuser_after_dba_script(prepared):
    owner = dsn_for(prepared, "doclib_owner")
    applied = await migrate(settings_for(owner), dsn=owner)
    assert "001_schema.sql" in applied
    assert await migrate(settings_for(owner), dsn=owner) == []  # Running it again changes nothing.
    rows = await as_superuser(
        prepared, "select distinct tableowner from pg_tables where schemaname = 'doclib'")
    assert [r["tableowner"] for r in rows] == ["doclib_owner"]
    role = await as_superuser(prepared, "select rolsuper, rolcreaterole, rolcreatedb from pg_roles where rolname = 'doclib_owner'")
    assert tuple(role[0]) == (False, False, False)


async def test_nothing_is_created_outside_the_doclib_schema(prepared):
    owner = dsn_for(prepared, "doclib_owner")
    await migrate(settings_for(owner), dsn=owner)
    outside = await as_superuser(
        prepared,
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where c.relname like 'doclib%' and n.nspname <> 'doclib'")
    assert outside == []


async def test_dba_script_is_safe_to_run_twice(prepared):
    run_dba_script(prepared)
    owner = dsn_for(prepared, "doclib_owner")
    assert await migrate(settings_for(owner), dsn=owner)


async def test_clear_error_when_pgvector_is_missing(database):
    role = await make_unprivileged_role()
    dsn = dsn_for(database, role)
    with pytest.raises(MigrationError, match=r"(?s)CREATE EXTENSION vector.*existing-cluster\.sql"):
        await migrate(settings_for(dsn), dsn=dsn)


async def test_clear_error_when_schema_is_missing(database):
    await as_superuser(database, "create extension vector")
    role = await make_unprivileged_role()
    dsn = dsn_for(database, role)
    with pytest.raises(MigrationError, match=r"(?s)CREATE SCHEMA doclib.*existing-cluster\.sql"):
        await migrate(settings_for(dsn), dsn=dsn)


async def test_clear_error_when_schema_exists_but_belongs_to_someone_else(database):
    await as_superuser(database, "create extension vector")
    await as_superuser(database, "create schema doclib")
    role = await make_unprivileged_role()
    dsn = dsn_for(database, role)
    with pytest.raises(MigrationError, match="ALTER SCHEMA doclib OWNER"):
        await migrate(settings_for(dsn), dsn=dsn)


async def test_clear_error_when_roles_are_missing(prepared, monkeypatch):
    monkeypatch.setattr(db, "REQUIRED_ROLES", ("doclib_rw", "doclib_ro", "doclib_role_nobody_created"))
    owner = dsn_for(prepared, "doclib_owner")
    with pytest.raises(MigrationError, match="CREATE ROLE doclib_role_nobody_created NOLOGIN"):
        await migrate(settings_for(owner), dsn=owner)


async def test_clear_error_when_pgvector_is_too_old(prepared, monkeypatch):
    monkeypatch.setattr(db, "MIN_PGVECTOR", (99, 0))
    owner = dsn_for(prepared, "doclib_owner")
    with pytest.raises(MigrationError, match="ALTER EXTENSION vector UPDATE"):
        await migrate(settings_for(owner), dsn=owner)


@pytest_asyncio.fixture
async def migrated(prepared):
    owner = dsn_for(prepared, "doclib_owner")
    await migrate(settings_for(owner), dsn=owner)
    return prepared


async def _seed_file(pool: asyncpg.Pool) -> str:
    file_id = str(uuid.uuid4())
    await pool.execute(
        "insert into doclib.sources (source_id, tenant_id, rclone_remote, root_path, config) "
        "values ('s', 't', 'local', '/x', $1)", {"id": "s"})
    await pool.execute(
        "insert into doclib.files (file_id, tenant_id, source_id, path, content_version, status) "
        "values ($1, 't', 's', 'a.pdf', 'v1', 'indexed')", file_id)
    return file_id


async def test_worker_role_can_write_rows_but_not_change_schema(migrated):
    dsn = dsn_for(migrated, "doclib_worker")
    pool = await create_pool(settings_for(dsn), dsn=dsn)
    try:
        file_id = await _seed_file(pool)
        await pool.execute("update doclib.files set path = 'b.pdf' where file_id = $1", file_id)
        vector = "[" + ",".join(["0.5"] * 1024) + "]"
        await pool.execute(
            "insert into doclib.chunks (chunk_id, file_id, tenant_id, content_version, ordinal, anchor, text, embedding) "
            "values ('c', $1, 't', 'v1', 0, 'page:1', 'hello world', $2::vector)", file_id, vector)
        found = await pool.fetchval("select chunk_id from doclib.chunks order by embedding <=> $1::vector limit 1", vector)
        assert found == "c"
        assert await pool.fetchval("select tsv::text like '%hello%' from doclib.chunks") is True
        await pool.execute("delete from doclib.chunks where chunk_id = 'c'")
        for statement in ("drop table doclib.chunks", "create table doclib.extra (x int)",
                          "alter table doclib.files add column extra int", "create table public.extra (x int)",
                          "delete from doclib.schema_migrations"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await pool.execute(statement)
    finally:
        await pool.close()


async def test_api_role_reads_and_requests_reindex_only(migrated):
    worker_dsn = dsn_for(migrated, "doclib_worker")
    worker = await create_pool(settings_for(worker_dsn), dsn=worker_dsn)
    api_dsn = dsn_for(migrated, "doclib_api")
    api = await create_pool(settings_for(api_dsn), dsn=api_dsn)
    try:
        file_id = await _seed_file(worker)
        assert await api.fetchval("select count(*) from doclib.files") == 1
        for statement in ("delete from doclib.chunks", "update doclib.files set status = 'failed'",
                          "insert into doclib.jobs (kind) values ('index')", "truncate doclib.files",
                          "drop table doclib.chunks", "select * from doclib.schema_migrations"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await api.execute(statement)

        owner = await as_superuser(
            migrated, "select pg_get_userbyid(proowner) o from pg_proc where proname = 'request_reindex'")
        assert owner[0]["o"] == "doclib_owner"  # A non-superuser owner must be enough for the definer function.
        assert await api.fetchval("select doclib.request_reindex($1::uuid, 't', 'v2')", file_id) is True
        job = await worker.fetchrow("select kind, content_version, status from doclib.jobs")
        assert tuple(job) == ("index", "v2", "queued")
        row = await worker.fetchrow("select content_version, status from doclib.files where file_id = $1", file_id)
        assert tuple(row) == ("v2", "pending")
        assert await worker.fetchval("select count(*) from doclib.change_events where change = 'changed'") == 1
        # Tenant isolation holds through the definer function too.
        assert await api.fetchval("select doclib.request_reindex($1::uuid, 'other', 'v3')", file_id) is False
    finally:
        await worker.close()
        await api.close()


@pytest.mark.parametrize("query", ["sslmode=prefer", "sslmode=disable", "application_name=x&sslmode=prefer"])
async def test_dsn_parameters_are_accepted(migrated, query):
    dsn = dsn_for(migrated, "doclib_worker", query=query)
    pool = await create_pool(settings_for(dsn), dsn=dsn)
    try:
        assert await pool.fetchval("select 1") == 1
    finally:
        await pool.close()


async def test_pgvector_in_its_own_schema(database):
    """Shared clusters often keep extensions out of public. Migrations and queries must still find the type."""
    await as_superuser(database, "create schema extensions")
    await as_superuser(database, "create extension vector schema extensions")
    run_dba_script(database)
    await as_superuser(database, "grant usage on schema extensions to doclib_owner, doclib_rw, doclib_ro")
    owner = dsn_for(database, "doclib_owner")
    assert await migrate(settings_for(owner), dsn=owner)
    worker = dsn_for(database, "doclib_worker")
    pool = await create_pool(settings_for(worker), dsn=worker)
    try:
        vector = "[" + ",".join(["0.5"] * 1024) + "]"
        # Twice on one pool: the setting must survive asyncpg's reset between acquires.
        for _ in range(2):
            assert await pool.fetchval("select $1::vector <=> $1::vector", vector) == 0
    finally:
        await pool.close()
