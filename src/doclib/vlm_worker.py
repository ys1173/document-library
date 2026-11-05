"""Background vision worker: upgrades hard pages after a file is already searchable on OCR text.

Runs at low priority in its own process, so a slow CPU model or an offline remote GPU never blocks the fast
path. If the endpoint is down, jobs wait without using up attempts. Files stay searchable throughout.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time

import asyncpg

from . import queue, telemetry
from .chunker import chunk_document
from .config import Settings
from .embeddings import EmbeddingClient, EmbeddingError
from .extract.pipeline import IMAGE_EXTENSIONS, BackendUnavailable, extract_document
from .log import bind, event, safe_error, unbind
from .queue import Job
from .rclone import Rclone, RcloneError, content_version
from .sources import load_source
from .vlm import VlmClient, VlmUnavailable, media_type_for, render_pdf_pages
from .worker import TRANSIENT_DELAY_S, Superseded, flag_ocr_degraded, store


async def run_vlm(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, embedder: EmbeddingClient, vlm: VlmClient, job: Job) -> None:
    row = await pool.fetchrow(
        "select tenant_id, source_id, path, content_version, deleted_at from doclib.files where file_id = $1", job.file_id)
    if row is None or row["deleted_at"] is not None or row["content_version"] != job.content_version:
        raise Superseded()
    source = await load_source(pool, row["source_id"])
    path = row["path"]
    entry = await rclone.stat(source, path)
    if entry is None or content_version(entry) != job.content_version:
        raise Superseded()  # The scanner or open_doc queues the new version.
    data = await rclone.cat(source, path, source.max_file_size_mb * 1024 * 1024)
    filename = path.rsplit("/", 1)[-1]
    pages = [int(p) for p in (job.payload or {}).get("pages", [])]
    started = time.monotonic()
    if filename.rsplit(".", 1)[-1].lower() in IMAGE_EXTENSIONS:
        images, media_type = {1: data}, media_type_for(filename)
    else:
        images = await asyncio.to_thread(render_pdf_pages, data, pages, settings.extraction.vlm.render_dpi)
        media_type = "image/png"
    done, failed = await vlm.pages_to_markdown(images, media_type)
    del images
    vlm_seconds = time.monotonic() - started
    doc = await extract_document(data, filename, settings.extraction, vlm_pages=done, vlm_failed=failed)
    del data
    chunks = chunk_document(doc, job.file_id, job.content_version, settings.chunking)
    vectors = await embedder.embed_documents([c.embedding_text for c in chunks]) if chunks else []
    await store(pool, settings, job.file_id, row["tenant_id"], job.content_version, doc, chunks, vectors)
    telemetry.job_duration.record(vlm_seconds, {"stage": "vlm"})
    event("vlm.completed", path=path, pages=len(pages), upgraded=len(done), failed=len(failed),
          chunks=len(chunks), duration_s=round(vlm_seconds, 2),
          seconds_per_page=round(vlm_seconds / max(1, len(pages)), 2))


async def handle(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, embedder: EmbeddingClient, vlm: VlmClient,
                 job: Job, worker_id: str) -> None:
    token = bind(job_id=job.job_id, file_id=job.file_id, content_version=job.content_version, attempt=job.attempts)
    event("job.claimed", kind=job.kind, worker_id=worker_id)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(settings.worker.lease_s / 3)
            await queue.heartbeat(pool, job.job_id, worker_id)

    beat = asyncio.create_task(heartbeat())
    try:
        await run_vlm(pool, settings, rclone, embedder, vlm, job)
        await queue.complete(pool, job.job_id)
        event("job.done", kind=job.kind)
    except Superseded:
        await queue.complete(pool, job.job_id)
        event("job.superseded", kind=job.kind)
    except (VlmUnavailable, BackendUnavailable, EmbeddingError, RcloneError, OSError, asyncpg.PostgresConnectionError) as exc:
        await queue.retry(pool, job.job_id, safe_error(exc), TRANSIENT_DELAY_S, consume_attempt=False)
        event("vlm.waiting_for_endpoint", level=30, error=safe_error(exc), delay_s=TRANSIENT_DELAY_S)
    except Exception as exc:  # noqa: BLE001 - counts against this job's attempts
        error = safe_error(exc)
        if job.attempts >= settings.worker.max_attempts:
            await queue.bury(pool, job.job_id, error)
            await flag_ocr_degraded(pool, job.file_id, job.content_version)
            event("job.dead", level=40, kind=job.kind, error=error, exc_info=exc)
        else:
            delay = settings.worker.backoff_base_s * (2 ** (job.attempts - 1))
            await queue.retry(pool, job.job_id, error, delay)
            event("job.retry", level=30, kind=job.kind, error=error, delay_s=delay, exc_info=exc)
    finally:
        beat.cancel()
        unbind(token)


async def drain(pool: asyncpg.Pool, settings: Settings, rclone: Rclone, embedder: EmbeddingClient, vlm: VlmClient,
                worker_id: str = "vlm-drain") -> int:
    done = 0
    while job := await queue.claim(pool, worker_id, ["vlm_page"]):
        await handle(pool, settings, rclone, embedder, vlm, job, worker_id)
        done += 1
    return done


async def run_forever(pool: asyncpg.Pool, settings: Settings, rclone: Rclone) -> None:
    if not settings.extraction.vlm.enabled:
        event("vlm.disabled")
        return
    embedder = EmbeddingClient(settings.embeddings)
    vlm = VlmClient(settings.extraction.vlm)
    worker_id = f"{socket.gethostname()}-{os.getpid()}-vlm"
    event("vlm.worker_started", endpoint_host=settings.extraction.vlm.endpoint.split("/")[2],
          model=settings.extraction.vlm.model, concurrency=settings.extraction.vlm.concurrency)
    try:
        while True:
            try:
                job = await queue.claim(pool, worker_id, ["vlm_page"])
                if job is None:
                    await asyncio.sleep(settings.worker.poll_s)
                    continue
                await handle(pool, settings, rclone, embedder, vlm, job, worker_id)
            except (OSError, asyncpg.PostgresError) as exc:
                event("vlm.database_error", level=40, error=safe_error(exc))
                await asyncio.sleep(5)
    finally:
        await vlm.aclose()
        await embedder.aclose()
