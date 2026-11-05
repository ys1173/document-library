"""The source registry. Postgres is the single place every process reads source definitions from, so a
connector created through the admin API reaches the scanner, workers and API without a restart."""

from __future__ import annotations

import asyncpg

from .config import Settings, SourceConfig
from .log import event
from .rclone import remote_name


class UnknownSource(KeyError):
    pass


def _parse(row: asyncpg.Record) -> SourceConfig:
    return SourceConfig.model_validate(row["config"])


async def load_source(pool: asyncpg.Pool, source_id: str) -> SourceConfig:
    row = await pool.fetchrow("select config from doclib.sources where source_id = $1", source_id)
    if row is None:
        raise UnknownSource(source_id)
    return _parse(row)


async def load_enabled_sources(pool: asyncpg.Pool) -> list[SourceConfig]:
    rows = await pool.fetch("select config from doclib.sources where enabled order by source_id")
    return [_parse(r) for r in rows]


async def sync_file_sources(pool: asyncpg.Pool, settings: Settings) -> None:
    """Mirror sources declared in the config file. Connectors created through the API are left alone."""
    declared = []
    for source in settings.sources:
        tenant = source.tenant_id or settings.tenant_id
        config = source.model_copy(update={"tenant_id": tenant}).model_dump(mode="json")
        declared.append(source.id)
        result = await pool.execute(
            "insert into doclib.sources (source_id, tenant_id, rclone_remote, root_path, config, origin) "
            "values ($1, $2, $3, $4, $5, 'file') on conflict (source_id) do update set tenant_id = excluded.tenant_id, "
            "rclone_remote = excluded.rclone_remote, root_path = excluded.root_path, config = excluded.config, "
            "enabled = true, updated_at = now() where doclib.sources.origin = 'file'",
            source.id, tenant, remote_name(source), source.root_path, config)
        if result.endswith(" 0"):
            event("sources.file_source_conflict", level=30, source_id=source.id,
                  error="an API connector already uses this id, so the config file entry is ignored")
    # A source removed from the file stops being scanned; its index stays until an admin deletes it.
    await pool.execute("update doclib.sources set enabled = false where origin = 'file' and not source_id = any($1::text[])",
                       declared)
