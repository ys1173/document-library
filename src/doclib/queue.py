"""Postgres job queue using SKIP LOCKED. Jobs are keyed by (file_id, content_version) and safe to retry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg


@dataclass(frozen=True)
class Job:
    job_id: int
    kind: str
    file_id: str | None
    content_version: str | None
    payload: dict[str, Any] | None
    attempts: int


CLAIM_SQL = """
update doclib.jobs set status = 'running', locked_by = $1, locked_at = now(), attempts = attempts + 1
where job_id = (
  select job_id from doclib.jobs
   where status = 'queued' and run_after <= now() and kind = any($2::text[])
   order by priority, run_after, job_id
   for update skip locked limit 1
)
returning job_id, kind, file_id::text, content_version, payload, attempts
"""


async def enqueue_index(conn: asyncpg.Connection, file_id: str, version: str, priority: int = 100) -> None:
    await conn.execute(
        "insert into doclib.jobs (kind, file_id, content_version, priority) "
        "values ('index', $1, $2, $3) on conflict do nothing", file_id, version, priority)


async def enqueue_vlm(conn: asyncpg.Connection | asyncpg.Pool, file_id: str, version: str, pages: list[int], priority: int) -> None:
    await conn.execute(
        "insert into doclib.jobs (kind, file_id, content_version, payload, priority) "
        "values ('vlm_page', $1, $2, $3, $4) on conflict do nothing", file_id, version, {"pages": pages}, priority)


async def claim(pool: asyncpg.Pool, worker_id: str, kinds: list[str]) -> Job | None:
    row = await pool.fetchrow(CLAIM_SQL, worker_id, kinds)
    return Job(**dict(row)) if row else None


async def heartbeat(pool: asyncpg.Pool, job_id: int, worker_id: str) -> None:
    await pool.execute(
        "update doclib.jobs set locked_at = now() where job_id = $1 and locked_by = $2 and status = 'running'",
        job_id, worker_id)


async def complete(conn: asyncpg.Connection | asyncpg.Pool, job_id: int) -> None:
    await conn.execute(
        "update doclib.jobs set status = 'done', finished_at = now(), locked_by = null, locked_at = null, "
        "last_error = null where job_id = $1", job_id)


async def retry(pool: asyncpg.Pool, job_id: int, error: str, delay_s: float, *, consume_attempt: bool = True) -> None:
    await pool.execute(
        "update doclib.jobs set status = 'queued', locked_by = null, locked_at = null, last_error = $2, "
        "run_after = now() + make_interval(secs => $3), attempts = attempts - $4 where job_id = $1",
        job_id, error, float(delay_s), 0 if consume_attempt else 1)


async def bury(conn: asyncpg.Connection | asyncpg.Pool, job_id: int, error: str) -> None:
    await conn.execute(
        "update doclib.jobs set status = 'dead', finished_at = now(), locked_by = null, locked_at = null, "
        "last_error = $2 where job_id = $1", job_id, error)


async def reap_expired(pool: asyncpg.Pool, lease_s: float, max_attempts: int) -> list[dict[str, Any]]:
    """Recover jobs from killed workers. Returns the jobs that ran out of attempts, already buried."""
    await pool.execute(
        "update doclib.jobs set status = 'queued', locked_by = null, locked_at = null, "
        "last_error = 'worker lease expired' "
        "where status = 'running' and locked_at < now() - make_interval(secs => $1) and attempts < $2",
        float(lease_s), max_attempts)
    rows = await pool.fetch(
        "update doclib.jobs set status = 'dead', finished_at = now(), locked_by = null, locked_at = null, "
        "last_error = 'worker lease expired' "
        "where status = 'running' and locked_at < now() - make_interval(secs => $1) and attempts >= $2 "
        "returning job_id, kind, file_id::text, content_version", float(lease_s), max_attempts)
    return [dict(r) for r in rows]
