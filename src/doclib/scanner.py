"""Scanner process: detects drive changes, queues indexing, and runs the notification loops."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import UTC, datetime

import asyncpg

from . import notify, queue, telemetry
from .changes import ChangeSet, KnownFile, detect, extension, in_scope
from .chunker import doc_label
from .config import Settings, SourceConfig
from .log import bind, event, safe_error, unbind
from .rclone import Entry, Rclone, RcloneError, acl_from_metadata, best_hash, content_version
from .sources import load_enabled_sources, sync_file_sources

BATCH = 500
RECONCILE_S = 5.0
FILE_NAMESPACE = uuid.UUID("6e0f3a52-6d0b-4f6f-9d55-7c0a3b1e9a10")


class MassDeleteBlocked(Exception):
    pass


async def sync_sources(pool: asyncpg.Pool, settings: Settings) -> None:
    await sync_file_sources(pool, settings)


def _tenant(source: SourceConfig, settings: Settings) -> str:
    return source.tenant_id or settings.tenant_id


def _tags(source: SourceConfig, path: str) -> list[str]:
    tags = list(source.tags)
    for prefix, extra in source.path_tags.items():
        if path.startswith(prefix):
            tags.extend(extra)
    return sorted(set(tags))


def _content_hash(entry: Entry) -> str | None:
    picked = best_hash(entry.hashes)
    return f"{picked[0]}:{picked[1]}" if picked else None


def _status_for(entry: Entry, source: SourceConfig) -> tuple[str, str | None]:
    if entry.size > source.max_file_size_mb * 1024 * 1024:
        return "skipped", f"larger than max_file_size_mb ({source.max_file_size_mb})"
    return "pending", None


async def _emit(conn: asyncpg.Connection, *, enabled: bool, settings: Settings, source: SourceConfig, scan_id: uuid.UUID,
                file_id: str, change: str, path: str, version: str, previous_path: str | None = None,
                outcome: str | None = None, error: str | None = None) -> None:
    if not enabled:
        return
    await conn.execute(
        "insert into doclib.change_events (tenant_id, source_id, scan_id, file_id, change, path, previous_path, "
        "content_version, state, outcome, error) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) on conflict do nothing",
        _tenant(source, settings), source.id, scan_id, file_id, change, path, previous_path, version,
        "ready" if outcome else "pending", outcome, error)


async def _insert_new(conn: asyncpg.Connection, settings: Settings, source: SourceConfig, entry: Entry,
                      version: str, status: str, error: str | None) -> str:
    # A path-derived id keeps saved references from other services valid across a full index rebuild.
    tenant = _tenant(source, settings)
    file_id = str(uuid.uuid5(FILE_NAMESPACE, f"{tenant}/{source.id}/{entry.path}"))
    params = (tenant, source.id, entry.path, entry.size, entry.modtime, _content_hash(entry), version,
              entry.mime_type, _tags(source, entry.path), acl_from_metadata(entry.metadata), status, error)
    sql = """
      insert into doclib.files (file_id, tenant_id, source_id, path, size_bytes, modtime, content_hash,
        content_version, mime_type, tags, acl, status, error)
      values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
      on conflict (file_id) do update set path = excluded.path, size_bytes = excluded.size_bytes,
        modtime = excluded.modtime, content_hash = excluded.content_hash, content_version = excluded.content_version,
        indexed_version = null, mime_type = excluded.mime_type, tags = excluded.tags, acl = excluded.acl,
        status = excluded.status, error = excluded.error, deleted_at = null, indexed_at = null, changed_at = now()
      where doclib.files.deleted_at is not null
      returning file_id::text
    """
    row = await conn.fetchrow(sql, file_id, *params)
    if row is None:  # That id now belongs to a file that moved away from this path.
        file_id = str(uuid.uuid4())
        row = await conn.fetchrow(sql, file_id, *params)
    return row["file_id"]


async def apply_changes(pool: asyncpg.Pool, settings: Settings, source: SourceConfig, changes: ChangeSet,
                        scan_id: uuid.UUID, notify_changes: bool) -> None:
    emit = {"enabled": notify_changes, "settings": settings, "source": source, "scan_id": scan_id}

    # Applied in small batches: a crash mid-scan is fine because the next scan recomputes the difference.
    async def run(items, apply_one) -> None:
        for start in range(0, len(items), BATCH):
            async with pool.acquire() as conn, conn.transaction():
                for item in items[start:start + BATCH]:
                    await apply_one(conn, item)

    async def apply_delete(conn: asyncpg.Connection, known: KnownFile) -> None:
        await conn.execute(
            "update doclib.files set status = 'deleted', deleted_at = now(), changed_at = now() where file_id = $1",
            known.file_id)
        await conn.execute("delete from doclib.chunks where file_id = $1", known.file_id)
        await conn.execute("delete from doclib.outlines where file_id = $1", known.file_id)
        await conn.execute("delete from doclib.jobs where file_id = $1 and status = 'queued'", known.file_id)
        await conn.execute("delete from doclib.change_events where file_id = $1 and state = 'pending'", known.file_id)
        await _emit(conn, **emit, file_id=known.file_id, change="deleted", path=known.path,
                    version=known.content_version, outcome="deleted")
        event("scan.file_deleted", file_id=known.file_id, path=known.path)

    async def apply_move(conn: asyncpg.Connection, item: tuple[KnownFile, Entry]) -> None:
        known, entry = item
        await conn.execute(
            "update doclib.files set path = $2, modtime = $3, tags = $4, changed_at = now() where file_id = $1",
            known.file_id, entry.path, entry.modtime, _tags(source, entry.path))
        title = await conn.fetchval("select title from doclib.files where file_id = $1", known.file_id)
        await conn.execute("update doclib.chunks set doc_label = $2 where file_id = $1",
                           known.file_id, doc_label(entry.path, title))
        await _emit(conn, **emit, file_id=known.file_id, change="moved", path=entry.path, previous_path=known.path,
                    version=known.content_version, outcome="moved")
        event("scan.file_moved", file_id=known.file_id, path=entry.path, previous_path=known.path)

    async def apply_changed(conn: asyncpg.Connection, item: tuple[KnownFile, Entry]) -> None:
        known, entry = item
        version = content_version(entry)
        status, error = _status_for(entry, source)
        await conn.execute(
            "update doclib.files set size_bytes = $2, modtime = $3, content_hash = $4, content_version = $5, "
            "mime_type = $6, acl = $7, status = $8, error = $9, changed_at = now() where file_id = $1",
            known.file_id, entry.size, entry.modtime, _content_hash(entry), version, entry.mime_type,
            acl_from_metadata(entry.metadata), status, error)
        if status == "skipped":
            await conn.execute("delete from doclib.chunks where file_id = $1", known.file_id)
            await conn.execute("update doclib.files set indexed_version = null where file_id = $1", known.file_id)
            await _emit(conn, **emit, file_id=known.file_id, change="changed", path=entry.path, version=version,
                        outcome="skipped", error=error)
        else:
            await queue.enqueue_index(conn, known.file_id, version)
            await _emit(conn, **emit, file_id=known.file_id, change="changed", path=entry.path, version=version)
        event("scan.file_changed", file_id=known.file_id, path=entry.path, content_version=version, status=status)

    async def apply_new(conn: asyncpg.Connection, entry: Entry) -> None:
        version = content_version(entry)
        status, error = _status_for(entry, source)
        file_id = await _insert_new(conn, settings, source, entry, version, status, error)
        if status == "skipped":
            await _emit(conn, **emit, file_id=file_id, change="new", path=entry.path, version=version,
                        outcome="skipped", error=error)
        else:
            await queue.enqueue_index(conn, file_id, version)
            await _emit(conn, **emit, file_id=file_id, change="new", path=entry.path, version=version)
        event("scan.file_new", file_id=file_id, path=entry.path, content_version=version, status=status,
              file_type=extension(entry.path))

    # Deletes and moves first, so a path freed in this scan can be reused by a new file.
    await run(changes.deleted, apply_delete)
    await run(changes.moved, apply_move)
    await run(changes.changed, apply_changed)
    await run(changes.new, apply_new)


async def scan_source(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, source: SourceConfig,
                      *, allow_mass_delete: bool = False) -> ChangeSet:
    started = time.monotonic()
    snapshot = datetime.now(UTC)
    scan_id = uuid.uuid4()
    token = bind(source_id=source.id, scan_id=str(scan_id))
    event("scan.started")
    await pool.execute("update doclib.sources set scan_requested_at = null where source_id = $1", source.id)
    try:
        lock_key = int.from_bytes(hashlib.sha256(source.id.encode()).digest()[:4], "big")
        async with pool.acquire() as lock_conn:
            if not await lock_conn.fetchval("select pg_try_advisory_lock($1)", lock_key):
                event("scan.skipped_already_running")
                return ChangeSet()
            try:
                rows = await lock_conn.fetch(
                    "select file_id::text, path, content_version, size_bytes, status from doclib.files "
                    "where source_id = $1 and deleted_at is null", source.id)
                known = [KnownFile(**dict(r)) for r in rows]
                listing = [e async for e in rclone.list_files(source) if in_scope(e.path, source)]
                changes = detect(listing, known)
                if (len(known) >= 10 and len(changes.deleted) > source.max_delete_ratio * len(known)
                        and not allow_mass_delete):
                    raise MassDeleteBlocked(
                        f"scan would delete {len(changes.deleted)} of {len(known)} files; "
                        "aborted. Check the source, or run: doclib scan --allow-mass-delete")
                baseline_at = await lock_conn.fetchval(
                    "select baseline_at from doclib.sources where source_id = $1", source.id)
                await apply_changes(pool, settings, source, changes, scan_id, notify_changes=baseline_at is not None)
                await lock_conn.execute(
                    "update doclib.sources set last_scan_at = $2, last_scan_ok = true, last_scan_error = null, "
                    "last_success_at = $2, baseline_at = coalesce(baseline_at, $2) where source_id = $1",
                    source.id, snapshot)
            finally:
                await lock_conn.execute("select pg_advisory_unlock($1)", lock_key)
        duration = time.monotonic() - started
        telemetry.scan_duration.record(duration, {"source_id": source.id})
        event("scan.completed", duration_s=round(duration, 3), files=len(listing), new=len(changes.new),
              changed=len(changes.changed), deleted=len(changes.deleted), moved=len(changes.moved),
              unchanged=changes.unchanged, baseline=baseline_at is None)
        return changes
    except (RcloneError, MassDeleteBlocked, OSError) as exc:
        await pool.execute(
            "update doclib.sources set last_scan_at = $2, last_scan_ok = false, last_scan_error = $3 "
            "where source_id = $1", source.id, snapshot, safe_error(exc, 500))
        event("scan.failed", level=40, error=safe_error(exc, 500))
        raise
    finally:
        unbind(token)


async def maintenance(pool: asyncpg.Pool, settings: Settings) -> None:
    """Purge expired tombstones and old bookkeeping, and publish gauges."""
    purged = await pool.execute(
        "delete from doclib.files where deleted_at < now() - make_interval(days => $1)", settings.tombstone_days)
    await pool.execute("delete from doclib.jobs where status = 'done' and finished_at < now() - interval '7 days'")
    await pool.execute(
        "delete from doclib.change_events where state = 'ready' and created_at < now() - make_interval(days => $1)",
        settings.notifications.webhook.event_retention_days)
    await pool.execute(
        "delete from doclib.webhook_deliveries where status <> 'pending' and created_at < now() - make_interval(days => $1) "
        "and not exists (select 1 from doclib.change_events e where e.delivery_id = doclib.webhook_deliveries.delivery_id)",
        settings.notifications.webhook.event_retention_days)
    for row in await pool.fetch("select status, count(*) n from doclib.files group by status"):
        telemetry.files_by_status.set(row["n"], {"status": row["status"]})
    for row in await pool.fetch("select kind, count(*) n from doclib.jobs where status = 'queued' group by kind"):
        telemetry.queue_depth.set(row["n"], {"kind": row["kind"]})
    for row in await pool.fetch(
            "select source_id, extract(epoch from now() - last_success_at)::float8 age from doclib.sources "
            "where last_success_at is not null"):
        telemetry.scan_age.set(row["age"], {"source_id": row["source_id"]})
    event("maintenance.completed", tombstones_purged=purged)


async def _wait_until_due(pool: asyncpg.Pool, source: SourceConfig, started: float) -> None:
    """Sleep until the next scan is due, waking early if someone asks for a scan now."""
    deadline = started + source.scan_interval
    while (remaining := deadline - time.monotonic()) > 0:
        await asyncio.sleep(min(RECONCILE_S, remaining))
        if await pool.fetchval("select scan_requested_at from doclib.sources where source_id = $1", source.id):
            return


async def _source_loop(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, source: SourceConfig) -> None:
    while True:
        started = time.monotonic()
        try:
            await scan_source(pool, settings, rclone, source)
        except (RcloneError, MassDeleteBlocked, OSError):
            pass  # Recorded on the source and logged; retried at the next interval.
        except Exception as exc:  # noqa: BLE001 - one bad scan must not stop the others
            event("scan.crashed", level=40, source_id=source.id, error=safe_error(exc, 500), exc_info=exc)
        await _wait_until_due(pool, source, started)


async def reconcile(pool: asyncpg.Pool, settings: Settings, rclone: Rclone,
                    running: dict[str, tuple[str, asyncio.Task]]) -> None:
    """Match running scan loops to the registry, so connector changes apply without a restart."""
    wanted = {s.id: s for s in await load_enabled_sources(pool)}
    for source_id, (fingerprint, task) in list(running.items()):
        if source_id not in wanted or wanted[source_id].model_dump_json() != fingerprint:
            task.cancel()
            del running[source_id]
            event("scan.loop_stopped", source_id=source_id)
    for source_id, source in wanted.items():
        if source_id not in running:
            task = asyncio.create_task(_source_loop(pool, settings, rclone, source))
            running[source_id] = (source.model_dump_json(), task)
            event("scan.loop_started", source_id=source_id)


async def run_forever(pool: asyncpg.Pool, settings: Settings, rclone: Rclone) -> None:
    await sync_sources(pool, settings)
    running: dict[str, tuple[str, asyncio.Task]] = {}

    async def reconcile_loop() -> None:
        while True:
            try:
                await reconcile(pool, settings, rclone, running)
            except (OSError, asyncpg.PostgresError) as exc:
                event("scan.reconcile_failed", level=40, error=safe_error(exc))
            await asyncio.sleep(RECONCILE_S)

    async def maintenance_loop() -> None:
        while True:
            try:
                await maintenance(pool, settings)
            except Exception as exc:  # noqa: BLE001
                event("maintenance.failed", level=40, error=safe_error(exc), exc_info=exc)
            await asyncio.sleep(300)

    async with asyncio.TaskGroup() as group:
        group.create_task(reconcile_loop())
        group.create_task(maintenance_loop())
        group.create_task(notify.run_forever(pool, settings))
