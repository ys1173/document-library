"""Worker process: runs index jobs. Every job is idempotent and safe to retry or kill."""

from __future__ import annotations

import asyncio
import os
import socket
import time

import asyncpg

from . import queue, telemetry
from .chunker import Chunk, chunk_document, doc_label
from .config import Settings
from .embeddings import EmbeddingClient, EmbeddingError, to_pgvector
from .extract.model import Document, outline
from .extract.pipeline import BackendUnavailable, ExtractionError, extract_document
from .log import bind, event, safe_error, unbind
from .queue import Job
from .rclone import Rclone, RcloneError, content_version
from .sources import load_source

TRANSIENT_DELAY_S = 60.0


class Superseded(Exception):
    """A newer version of the file exists, so this job's work must be discarded."""


async def _mark_events(conn: asyncpg.Connection, file_id: str, outcome: str, error: str | None) -> None:
    await conn.execute(
        "update doclib.change_events set state = 'ready', outcome = $2, error = $3 "
        "where file_id = $1 and state = 'pending'", file_id, outcome, error)


async def mark_failed(pool: asyncpg.Pool | asyncpg.Connection, file_id: str | None, version: str | None, error: str) -> None:
    """Final failure. Previously indexed chunks stay searchable; the failure is reported."""
    if not file_id:
        return
    async with pool.acquire() as conn, conn.transaction():
        updated = await conn.execute(
            "update doclib.files set status = 'failed', error = $3 "
            "where file_id = $1 and content_version = $2 and deleted_at is null", file_id, version, error)
        if updated.endswith(" 1"):
            await _mark_events(conn, file_id, "failed", error)


async def store(pool: asyncpg.Pool, settings: Settings, file_id: str, tenant_id: str, version: str, doc: Document,
                chunks: list[Chunk], vectors: list[list[float]]) -> None:
    """Swap to the new version atomically: searchers see the old chunks or the new ones, never a mix."""
    model = settings.embeddings.model
    dim = settings.embeddings.dimension
    rows = [(c.chunk_id, file_id, tenant_id, version, c.ordinal, c.anchor, c.heading_path, c.page_start, c.page_end,
             c.block_types, c.extraction_path, c.quality, c.text, c.token_count, to_pgvector(v), model, dim)
            for c, v in zip(chunks, vectors, strict=True)]
    async with pool.acquire() as conn, conn.transaction():
        current = await conn.fetchrow(
            "select content_version, deleted_at, path from doclib.files where file_id = $1 for update", file_id)
        if current is None or current["deleted_at"] is not None or current["content_version"] != version:
            raise Superseded()
        label = doc_label(current["path"], doc.title)
        await conn.execute("delete from doclib.chunks where file_id = $1", file_id)
        await conn.execute("delete from doclib.outlines where file_id = $1", file_id)
        await conn.executemany(
            "insert into doclib.chunks (chunk_id, file_id, tenant_id, content_version, ordinal, anchor, heading_path, "
            "page_start, page_end, block_types, extraction_path, quality, text, token_count, embedding, "
            "embedding_model, embedding_dim, doc_label) "
            "values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::vector,$16,$17,$18)",
            [(*row, label) for row in rows])
        await conn.execute(
            "insert into doclib.outlines (file_id, content_version, outline) values ($1, $2, $3)",
            file_id, version, outline(doc))
        await conn.execute(
            "update doclib.files set status = 'indexed', indexed_version = $2, indexed_at = now(), error = null, "
            "language = $3, title = $4, mime_type = coalesce($5, mime_type), extractor = $6 where file_id = $1",
            file_id, version, doc.language, doc.title, doc.mime_type, doc.extractor)
        await _mark_events(conn, file_id, "indexed", None)


async def run_index(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, embedder: EmbeddingClient, job: Job) -> None:
    row = await pool.fetchrow(
        "select file_id::text, tenant_id, source_id, path, content_version, indexed_version, status, deleted_at "
        "from doclib.files where file_id = $1", job.file_id)
    if row is None or row["deleted_at"] is not None or row["content_version"] != job.content_version:
        raise Superseded()
    if row["status"] == "indexed" and row["indexed_version"] == job.content_version:
        event("index.already_done")
        return

    source = await load_source(pool, row["source_id"])
    path = row["path"]
    before = await rclone.stat(source, path)
    if before is None:
        event("index.source_file_missing", path=path)  # The next scan records the delete.
        raise Superseded()
    live = content_version(before)
    if live != job.content_version:
        await pool.execute("select doclib.request_reindex($1, $2, $3)", job.file_id, row["tenant_id"], live)
        raise Superseded()

    timings: dict[str, float] = {}
    started = time.monotonic()
    data = await rclone.cat(source, path, source.max_file_size_mb * 1024 * 1024)
    after = await rclone.stat(source, path)
    if after is None or content_version(after) != job.content_version:
        raise RcloneError("file changed while it was being read")
    timings["read"] = time.monotonic() - started

    started = time.monotonic()
    doc = await extract_document(data, path.rsplit("/", 1)[-1], settings.extraction)
    del data
    timings["extract"] = time.monotonic() - started

    started = time.monotonic()
    chunks = chunk_document(doc, job.file_id, job.content_version, settings.chunking)
    timings["chunk"] = time.monotonic() - started

    started = time.monotonic()
    vectors = await embedder.embed_documents([c.embedding_text for c in chunks]) if chunks else []
    timings["embed"] = time.monotonic() - started

    started = time.monotonic()
    await store(pool, settings, job.file_id, row["tenant_id"], job.content_version, doc, chunks, vectors)
    timings["store"] = time.monotonic() - started
    if settings.extraction.vlm.enabled and doc.hard_pages:
        await queue.enqueue_vlm(pool, job.file_id, job.content_version, doc.hard_pages, settings.worker.vlm_priority)

    for stage, seconds in timings.items():
        telemetry.job_duration.record(seconds, {"stage": stage})
    event("index.completed", path=path, content_version=job.content_version, unit=doc.unit, pages=len(doc.pages),
          chunks=len(chunks), language=doc.language,
          ocr_pages=sum(1 for p in doc.pages if p.extraction_path == "ocr"),
          degraded_pages=sum(1 for p in doc.pages if p.quality == "degraded"),
          timings={k: round(v, 3) for k, v in timings.items()})


async def handle(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, embedder: EmbeddingClient, job: Job,
                 worker_id: str) -> None:
    token = bind(job_id=job.job_id, file_id=job.file_id, content_version=job.content_version, attempt=job.attempts)
    event("job.claimed", kind=job.kind, worker_id=worker_id)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(settings.worker.lease_s / 3)
            await queue.heartbeat(pool, job.job_id, worker_id)

    beat = asyncio.create_task(heartbeat())
    try:
        with telemetry.tracer.start_as_current_span("doclib.job", attributes={"job.kind": job.kind}):
            if job.kind != "index":
                raise ValueError(f"unknown job kind: {job.kind}")
            await run_index(pool, settings, rclone, embedder, job)
        await queue.complete(pool, job.job_id)
        event("job.done", kind=job.kind)
    except Superseded:
        await queue.complete(pool, job.job_id)
        event("job.superseded", kind=job.kind)
    except (EmbeddingError, BackendUnavailable, RcloneError, OSError, asyncpg.PostgresConnectionError) as exc:
        # The infrastructure is at fault, not the file: keep waiting without using up attempts.
        await queue.retry(pool, job.job_id, safe_error(exc), TRANSIENT_DELAY_S, consume_attempt=False)
        event("job.retry_transient", level=30, error=safe_error(exc), delay_s=TRANSIENT_DELAY_S)
    except Exception as exc:  # noqa: BLE001 - any file-specific failure counts against its attempts
        error = safe_error(exc)
        known_cause = isinstance(exc, ExtractionError | asyncio.TimeoutError)
        if job.attempts >= settings.worker.max_attempts:
            await queue.bury(pool, job.job_id, error)
            await mark_failed(pool, job.file_id, job.content_version, error)
            event("job.dead", level=40, error=error, exc_info=None if known_cause else exc)
        else:
            delay = settings.worker.backoff_base_s * (2 ** (job.attempts - 1))
            await queue.retry(pool, job.job_id, error, delay)
            event("job.retry", level=30, error=error, delay_s=delay, exc_info=None if known_cause else exc)
    finally:
        beat.cancel()
        unbind(token)


async def flag_ocr_degraded(pool: asyncpg.Pool, file_id: str | None, version: str | None) -> None:
    """The vision upgrade finally failed. The file stays searchable on OCR text, flagged degraded."""
    await pool.execute(
        "update doclib.chunks set quality = 'degraded' where file_id = $1 and content_version = $2 "
        "and extraction_path = 'ocr'", file_id, version)


async def reap(pool: asyncpg.Pool, settings: Settings) -> None:
    dead = await queue.reap_expired(pool, settings.worker.lease_s, settings.worker.max_attempts)
    for job in dead:
        if job["kind"] == "index":
            await mark_failed(pool, job["file_id"], job["content_version"], "worker lease expired")
        else:
            await flag_ocr_degraded(pool, job["file_id"], job["content_version"])
        event("job.dead", level=40, job_id=job["job_id"], file_id=job["file_id"], error="worker lease expired")


async def drain(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, embedder: EmbeddingClient, worker_id: str = "drain") -> int:
    """Run jobs until none are due. For the CLI and tests."""
    done = 0
    while job := await queue.claim(pool, worker_id, ["index"]):
        await handle(pool, settings, rclone, embedder, job, worker_id)
        done += 1
    return done


async def run_forever(pool: asyncpg.Pool, settings: Settings, rclone: Rclone) -> None:
    embedder = EmbeddingClient(settings.embeddings)
    base_id = f"{socket.gethostname()}-{os.getpid()}"

    async def loop(index: int) -> None:
        worker_id = f"{base_id}-{index}"
        while True:
            try:
                job = await queue.claim(pool, worker_id, ["index"])
                if job is None:
                    await asyncio.sleep(settings.worker.poll_s)
                    continue
                await handle(pool, settings, rclone, embedder, job, worker_id)
            except (OSError, asyncpg.PostgresError) as exc:
                event("worker.database_error", level=40, error=safe_error(exc))
                await asyncio.sleep(5)

    async def reaper() -> None:
        while True:
            try:
                await reap(pool, settings)
            except (OSError, asyncpg.PostgresError) as exc:
                event("worker.reap_failed", level=30, error=safe_error(exc))
            await asyncio.sleep(max(5.0, settings.worker.lease_s / 4))

    event("worker.started", concurrency=settings.worker.concurrency)
    try:
        async with asyncio.TaskGroup() as group:
            for index in range(settings.worker.concurrency):
                group.create_task(loop(index))
            group.create_task(reaper())
    finally:
        await embedder.aclose()
