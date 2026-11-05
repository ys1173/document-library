"""Postgres access: connection pool and migrations.

The database may be a shared cluster where doclib has no superuser rights, so migrate() checks what it needs up
front and says exactly what a DBA must run. See deploy/postgres/existing-cluster.sql.
"""

from __future__ import annotations

import json
from importlib import resources

import asyncpg

from .config import Settings
from .log import event

SCHEMA = "doclib"
REQUIRED_ROLES = ("doclib_rw", "doclib_ro")
MIN_PGVECTOR = (0, 5)  # HNSW arrived in 0.5.0.
_DBA_HINT = "A DBA can prepare the database by running deploy/postgres/existing-cluster.sql."


class MigrationError(Exception):
    """The database isn't ready for doclib. The message says what to do, so show it without a traceback."""


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def pgvector_version(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("select extversion from pg_extension where extname = 'vector'")


async def _vector_search_path(conn: asyncpg.Connection) -> str | None:
    """A shared cluster may keep extensions in their own schema. The vector type must still resolve."""
    schema = await conn.fetchval(
        "select n.nspname from pg_extension e join pg_namespace n on n.oid = e.extnamespace where e.extname = 'vector'")
    if schema is None or schema == "public":
        return None
    quoted = '"' + schema.replace('"', '""') + '"'
    return f'"$user", public, {quoted}'


async def create_pool(settings: Settings, *, dsn: str | None = None) -> asyncpg.Pool:
    dsn = dsn or settings.postgres.resolve_dsn()
    server_settings = {"application_name": "doclib"}
    probe = await asyncpg.connect(dsn)
    try:
        search_path = await _vector_search_path(probe)
    finally:
        await probe.close()
    if search_path:
        server_settings["search_path"] = search_path
    return await asyncpg.create_pool(
        dsn,
        min_size=settings.postgres.pool_min,
        max_size=settings.postgres.pool_max,
        init=_init_connection,
        server_settings=server_settings,
    )


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


async def _ensure_pgvector(conn: asyncpg.Connection) -> None:
    version = await pgvector_version(conn)
    if version is None:
        try:
            await conn.execute("create extension vector")
        except asyncpg.PostgresError as exc:
            raise MigrationError(
                "pgvector is not installed in this database and this role can't install it "
                f"({exc}). A DBA must run: CREATE EXTENSION vector; {_DBA_HINT}") from exc
        version = await pgvector_version(conn)
    if _version_tuple(version) < MIN_PGVECTOR:
        required = ".".join(str(part) for part in MIN_PGVECTOR)
        raise MigrationError(
            f"pgvector {version} is too old: doclib needs {required} or newer for HNSW indexes. "
            "A DBA must run: ALTER EXTENSION vector UPDATE;")


async def _ensure_roles(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch("select rolname from pg_roles where rolname = any($1::text[])", list(REQUIRED_ROLES))
    missing = sorted(set(REQUIRED_ROLES) - {r["rolname"] for r in rows})
    if not missing:
        return
    can_create = await conn.fetchval(
        "select rolsuper or rolcreaterole from pg_roles where rolname = current_user")
    if can_create:
        for role in missing:
            await conn.execute(f'create role "{role}" nologin')
        return
    statements = " ".join(f"CREATE ROLE {role} NOLOGIN;" for role in missing)
    raise MigrationError(f"Missing database roles and this role can't create them. A DBA must run: {statements} {_DBA_HINT}")


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    # Checked first because CREATE SCHEMA IF NOT EXISTS needs database-level CREATE even when the schema exists.
    if await conn.fetchval("select to_regnamespace($1) is not null", SCHEMA):
        if not await conn.fetchval("select has_schema_privilege(current_user, $1, 'CREATE')", SCHEMA):
            raise MigrationError(
                f'Schema {SCHEMA} exists but this role can\'t create objects in it. '
                f'A DBA must run: ALTER SCHEMA {SCHEMA} OWNER TO "<this role>"; {_DBA_HINT}')
        return
    try:
        await conn.execute(f"create schema {SCHEMA}")
    except asyncpg.InsufficientPrivilegeError as exc:
        raise MigrationError(
            f'Schema {SCHEMA} doesn\'t exist and this role can\'t create it. '
            f'A DBA must run: CREATE SCHEMA {SCHEMA} AUTHORIZATION "<this role>"; {_DBA_HINT}') from exc


async def migrate(settings: Settings, *, dsn: str | None = None) -> list[str]:
    """Apply pending SQL migrations in order. Safe to run repeatedly. Raises MigrationError if the database isn't ready."""
    conn = await asyncpg.connect(dsn or settings.postgres.resolve_dsn())
    applied: list[str] = []
    try:
        await conn.execute("select pg_advisory_lock(hashtext('doclib.migrate'))")
        await _ensure_pgvector(conn)
        await _ensure_roles(conn)
        await _ensure_schema(conn)
        search_path = await _vector_search_path(conn)
        if search_path:
            await conn.execute("select set_config('search_path', $1, false)", search_path)
        await conn.execute(
            f"create table if not exists {SCHEMA}.schema_migrations "
            "(name text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["name"] for r in await conn.fetch(f"select name from {SCHEMA}.schema_migrations")}
        folder = resources.files("doclib") / "sql"
        for entry in sorted(folder.iterdir(), key=lambda e: e.name):
            if not entry.name.endswith(".sql") or entry.name in done:
                continue
            sql = entry.read_text(encoding="utf-8").replace(
                "{{EMBEDDING_DIM}}", str(int(settings.embeddings.dimension)))
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(f"insert into {SCHEMA}.schema_migrations (name) values ($1)", entry.name)
            applied.append(entry.name)
            event("db.migration_applied", migration=entry.name)
        # Schema-wide grants in migrations also cover this table. Only the owner should touch it.
        await conn.execute(f"revoke all on {SCHEMA}.schema_migrations from {', '.join(REQUIRED_ROLES)}")
    finally:
        await conn.close()  # Closing the session also releases the advisory lock.
    return applied
