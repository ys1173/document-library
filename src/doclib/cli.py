"""Admin CLI and process entry points. Admin actions live here, not on the agent-facing API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import asyncpg

from . import scanner, worker
from .config import Settings, load_settings
from .db import MigrationError, create_pool, migrate
from .embeddings import EmbeddingClient, EmbeddingError, EmbeddingRejected
from .log import event, safe_error, setup_logging
from .rclone import Rclone
from .sources import load_enabled_sources
from .telemetry import setup_telemetry
from .vlm import vlm_reachable


def _setup(settings: Settings, service: str) -> None:
    log = settings.logging
    setup_logging(service, log.level, log.file, log.file_max_mb, log.file_backups)
    setup_telemetry(service, settings.telemetry.otlp_endpoint)


def _rclone(settings: Settings) -> Rclone:
    return Rclone(settings.rclone_binary, settings.rclone_config_file)


async def _with_pool(settings: Settings, func) -> None:
    pool = await create_pool(settings)
    try:
        await func(pool)
    finally:
        await pool.close()


async def _scan(settings: Settings, args: argparse.Namespace) -> None:
    async def run(pool) -> None:
        await scanner.sync_sources(pool, settings)
        for source in await load_enabled_sources(pool):
            if not args.source or source.id == args.source:
                changes = await scanner.scan_source(pool, settings, _rclone(settings), source,
                                                    allow_mass_delete=args.allow_mass_delete)
                print(json.dumps({"source": source.id, "new": len(changes.new), "changed": len(changes.changed),
                                  "deleted": len(changes.deleted), "moved": len(changes.moved),
                                  "unchanged": changes.unchanged}))
    await _with_pool(settings, run)


async def _reindex(settings: Settings, args: argparse.Namespace) -> None:
    async def run(pool) -> None:
        where = "deleted_at is null and status <> 'skipped'"
        params: list[str] = []
        if args.file_id:
            where += " and file_id = $1::uuid"
            params = [args.file_id]
        elif args.source:
            where += " and source_id = $1"
            params = [args.source]
        elif not args.all:
            raise SystemExit("choose --file-id, --source or --all")
        rows = await pool.fetch(f"select file_id::text, content_version from doclib.files where {where}", *params)
        async with pool.acquire() as conn, conn.transaction():
            for row in rows:
                await conn.execute(
                    # indexed_version stays, so the file remains searchable on its old chunks until the new ones swap in.
                    "update doclib.files set status = 'pending', error = null where file_id = $1",
                    row["file_id"])
                await conn.execute(
                    "insert into doclib.jobs (kind, file_id, content_version) values ('index', $1, $2) on conflict do nothing",
                    row["file_id"], row["content_version"])
        event("admin.reindex_requested", files=len(rows))
        print(json.dumps({"queued": len(rows)}))
    await _with_pool(settings, run)


async def _check(settings: Settings) -> int:
    """Run after changing endpoints. Reports every problem at once, rather than leaving jobs waiting."""
    report: dict[str, object] = {}
    problems = 0
    conn = None
    try:
        conn = await asyncpg.connect(settings.postgres.resolve_dsn())
        dimension = await conn.fetchval(
            "select atttypmod from pg_attribute where attrelid = to_regclass('doclib.chunks') and attname = 'embedding'")
        report["postgres"] = {"ok": True, "schema_migrated": dimension is not None, "embedding_column_dimension": dimension}
    except (OSError, ValueError, asyncpg.PostgresError) as exc:
        report["postgres"] = {"ok": False, "error": safe_error(exc)}
        dimension = None
        problems += 1
    finally:
        if conn:
            await conn.close()
    embedder = EmbeddingClient(settings.embeddings)
    try:
        report["embeddings"] = await embedder.check()
    except (EmbeddingError, EmbeddingRejected) as exc:
        report["embeddings"] = {"ok": False, "error": str(exc), "configured_dimension": settings.embeddings.dimension}
        problems += 1
    finally:
        await embedder.aclose()
    if dimension is not None and dimension != settings.embeddings.dimension:
        report["embeddings"] = {**report["embeddings"], "ok": False, "error": (
            f"the database column holds {dimension} dimensions, but the config says {settings.embeddings.dimension}. "
            "A new dimension needs a new column and index, then a full re-index")}
        problems += 1
    vlm = settings.extraction.vlm
    if vlm.enabled:
        reachable = await vlm_reachable(vlm)
        report["vision"] = {"ok": reachable, "model": vlm.model}
        problems += not reachable
    else:
        report["vision"] = {"ok": True, "enabled": False}
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="doclib", description="Document Library")
    parser.add_argument("--config", help="Path to the YAML config (default: $DOCLIB_CONFIG or config.yaml)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Create or upgrade the database schema")
    commands.add_parser("scanner", help="Run the scanner: change detection and notifications")
    commands.add_parser("worker", help="Run an index worker")
    commands.add_parser("vlm-worker", help="Run a background vision model worker")
    commands.add_parser("api", help="Run the REST and MCP API")
    commands.add_parser("admin-api", help="Run the service-to-service connector API")
    commands.add_parser("extract-server", help="Serve the configured text backend over HTTP, for remote deployment")
    scan = commands.add_parser("scan", help="Scan once and exit")
    scan.add_argument("--source")
    scan.add_argument("--allow-mass-delete", action="store_true")
    commands.add_parser("check", help="Check Postgres and the model services match this config. Exits 1 on any problem")
    commands.add_parser("drain", help="Process queued index jobs, then exit")
    reindex = commands.add_parser("reindex", help="Queue a re-index")
    reindex.add_argument("--file-id")
    reindex.add_argument("--source")
    reindex.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    _setup(settings, f"doclib-{args.command}")
    match args.command:
        case "migrate":
            try:
                print(json.dumps({"applied": asyncio.run(migrate(settings))}))
            except MigrationError as exc:
                raise SystemExit(str(exc)) from exc
        case "scanner":
            asyncio.run(_with_pool(settings, lambda pool: scanner.run_forever(pool, settings, _rclone(settings))))
        case "worker":
            asyncio.run(_with_pool(settings, lambda pool: worker.run_forever(pool, settings, _rclone(settings))))
        case "vlm-worker":
            from . import vlm_worker
            asyncio.run(_with_pool(settings, lambda pool: vlm_worker.run_forever(pool, settings, _rclone(settings))))
        case "api":
            import uvicorn

            from .api import create_app
            uvicorn.run(create_app(settings), host=settings.api.host, port=settings.api.port, log_config=None, access_log=False)
        case "admin-api":
            import uvicorn

            from .admin_api import create_admin_app
            uvicorn.run(create_admin_app(settings), host=settings.admin.host, port=settings.admin.port,
                        log_config=None, access_log=False)
        case "extract-server":
            import os

            import uvicorn

            from .extract.server import create_extract_app
            uvicorn.run(create_extract_app(settings.extraction, api_key_file=os.environ.get("DOCLIB_EXTRACT_API_KEY_FILE")),
                        host=settings.api.host, port=int(os.environ.get("DOCLIB_EXTRACT_PORT", "8090")),
                        log_config=None, access_log=False)
        case "scan":
            asyncio.run(_scan(settings, args))
        case "check":
            sys.exit(asyncio.run(_check(settings)))
        case "drain":
            async def run_drain(pool) -> None:
                embedder = EmbeddingClient(settings.embeddings)
                try:
                    print(json.dumps({"jobs": await worker.drain(pool, settings, _rclone(settings), embedder)}))
                finally:
                    await embedder.aclose()
            asyncio.run(_with_pool(settings, run_drain))
        case "reindex":
            asyncio.run(_reindex(settings, args))


if __name__ == "__main__":
    sys.exit(main())
