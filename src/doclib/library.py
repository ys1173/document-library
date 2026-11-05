"""The read API: search_docs, open_doc, get_doc_outline, list_sources, index_status (spec section 10)."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from . import telemetry
from .changes import extension
from .chunker import page_anchor
from .config import Settings
from .embeddings import EmbeddingClient, EmbeddingError, EmbeddingRejected, to_pgvector
from .extract.model import Block, Document, sections
from .extract.pipeline import extract_document
from .log import event, safe_error
from .rclone import Rclone, content_version
from .sources import load_source
from .vlm import vlm_reachable

MAX_TOP_K = 50
_URI = re.compile(r"^doclib://(?P<source>[^/]+)/(?P<file>[0-9a-fA-F-]{36})(?:@(?P<version>[^#]+))?(?:#(?P<anchor>.+))?$")
_PAGE_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


@dataclass
class Filters:
    source_ids: list[str] | None = None
    path_prefix: str | None = None
    tags: list[str] | None = None
    file_types: list[str] | None = None
    modified_after: datetime | None = None
    language: str | None = None
    block_types: list[str] | None = None


def _where(tenant_id: str, filters: Filters, start: int) -> tuple[str, list[Any]]:
    """Shared WHERE clause. The tenant filter and live-version check are always present."""
    clauses = [f"c.tenant_id = ${start}", "f.tenant_id = c.tenant_id", "f.deleted_at is null",
               "c.content_version = f.indexed_version"]
    params: list[Any] = [tenant_id]

    def add(sql: str, value: Any) -> None:
        params.append(value)
        clauses.append(sql.format(n=start + len(params) - 1))

    if filters.source_ids:
        add("f.source_id = any(${n}::text[])", filters.source_ids)
    if filters.path_prefix:
        escaped = filters.path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        add("f.path like ${n}", escaped + "%")
    if filters.tags:
        add("f.tags && ${n}::text[]", filters.tags)
    if filters.file_types:
        add("lower(substring(f.path from '\\.([^./]+)$')) = any(${n}::text[])",
            [t.lower().lstrip(".") for t in filters.file_types])
    if filters.modified_after:
        add("f.modtime >= ${n}", filters.modified_after)
    if filters.language:
        add("f.language = ${n}", filters.language)
    if filters.block_types:
        add("c.block_types && ${n}::text[]", filters.block_types)
    return " and ".join(clauses), params


def rrf_merge(rankings: list[list[str]], k: int) -> dict[str, float]:
    """Reciprocal Rank Fusion, normalised so a top hit in every ranking scores 1.0."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    best = len(rankings) / (k + 1)
    return {key: score / best for key, score in scores.items()}


def _merged_anchor(anchor: str, starts: list[int], ends: list[int]) -> str:
    """A merged result's citation must cover all its chunks, not only the first."""
    kind = anchor.partition(":")[0]
    if kind in {"page", "slide"} and starts and ends:
        return page_anchor(kind, min(starts), max(ends))
    return anchor


def group_adjacent(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join hits that are neighbours in one section into a single result, keeping the best score's place."""
    used: set[str] = set()
    by_position = {(r["file_id"], r["ordinal"]): r for r in rows}
    results: list[dict[str, Any]] = []
    for row in rows:
        if row["chunk_id"] in used:
            continue
        run = [row]
        for step in (-1, 1):
            ordinal = row["ordinal"] + step
            while (other := by_position.get((row["file_id"], ordinal))) and other["chunk_id"] not in used \
                    and other["heading_path"] == row["heading_path"]:
                run.append(other)
                ordinal += step
        run.sort(key=lambda r: r["ordinal"])
        used.update(r["chunk_id"] for r in run)
        first = run[0]
        pages_start = [r["page_start"] for r in run if r["page_start"] is not None]
        pages_end = [r["page_end"] for r in run if r["page_end"] is not None]
        results.append({
            **first,
            "text": "\n\n".join(r["text"] for r in run),
            "chunk_ids": [r["chunk_id"] for r in run],
            "score": row["score"],
            "page_start": min(pages_start) if pages_start else None,
            "page_end": max(pages_end) if pages_end else None,
            "quality": "degraded" if any(r["quality"] == "degraded" for r in run) else first["quality"],
            "anchor": _merged_anchor(first["anchor"], pages_start, pages_end),
        })
    return results


def render(blocks: list[Block], limit: int, end: int) -> tuple[str, bool, str | None]:
    """Markdown for blocks, cut at a block boundary. Returns text, truncated, continuation anchor."""
    parts: list[str] = []
    used = 0
    for position, block in enumerate(blocks):
        text = block.to_markdown()
        if parts and used + len(text) > limit:
            return "\n\n".join(parts), True, f"block:{block.index}-{end}"
        if not parts and len(text) > limit:  # One huge block: must still make progress.
            next_anchor = f"block:{blocks[position + 1].index}-{end}" if position + 1 < len(blocks) else None
            return text[:limit], True, next_anchor
        parts.append(text)
        used += len(text) + 2
    return "\n\n".join(parts), False, None


def select_blocks(doc: Document, *, anchor: str | None, page_range: str | None, heading: str | None) -> tuple[list[Block], bool]:
    """The requested part of a document. Second value is False if the locator wasn't found."""
    blocks = doc.blocks
    if page_range:
        match = _PAGE_RANGE.match(page_range)
        if not match:
            raise BadRequest("page_range must look like '12' or '12-15'")
        low = int(match.group(1))
        high = int(match.group(2) or low)
        chosen = [b for b in blocks if b.page is not None and low <= b.page <= high]
        return (chosen, True) if chosen else (blocks, False)
    if heading:
        wanted = heading.removeprefix("heading:").strip().lower()
        for section in sections(blocks):
            if section.anchor.removeprefix("heading:").lower() == wanted or section.title.strip().lower() == wanted:
                return blocks[section.start:section.end], True
        return blocks, False
    if not anchor:
        return blocks, True
    kind, _, value = anchor.partition(":")
    if kind in {"page", "slide"} and (span := _PAGE_RANGE.match(value)):
        low = int(span.group(1))
        high = int(span.group(2) or low)
        chosen = [b for b in blocks if b.page is not None and low <= b.page <= high]
        return (chosen, True) if chosen else (blocks, False)
    if kind == "sheet":
        chosen = [b for b in blocks if b.label == value]
        return (chosen, True) if chosen else (blocks, False)
    if kind == "heading":
        return select_blocks(doc, anchor=None, page_range=None, heading=value)
    if kind == "block":
        start, _, stop = value.partition("-")
        if not start.isdigit() or (stop and not stop.isdigit()):
            raise BadRequest("invalid block anchor")
        high = int(stop) if stop else len(blocks)
        chosen = [b for b in blocks if int(start) <= b.index < high]
        return (chosen, True) if chosen else (blocks, False)
    raise BadRequest(f"unknown anchor: {anchor}")


class Library:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, rclone: Rclone, embedder: EmbeddingClient) -> None:
        self.pool = pool
        self.settings = settings
        self.rclone = rclone
        self.embedder = embedder
        self._iterative_scan: bool | None = None

    async def _has_iterative_scan(self, conn: asyncpg.Connection) -> bool:
        """pgvector 0.8+ keeps filtered searches from coming back short. An existing cluster may be older."""
        if self._iterative_scan is None:
            version = await conn.fetchval("select extversion from pg_extension where extname = 'vector'") or "0"
            self._iterative_scan = tuple(int(p) for p in version.split(".")[:2]) >= (0, 8)
        return self._iterative_scan

    async def _vector_search(self, query: str, where: str, params: list[Any], limit: int) -> list[str]:
        vector = await self.embedder.embed_query(query)
        # Vectors from different models aren't comparable. After a model change, old chunks are found by keyword
        # until they are re-indexed, rather than returning meaningless matches.
        model_param = len(params) + 2
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('hnsw.ef_search', $1, true)", str(self.settings.search.ef_search))
            if await self._has_iterative_scan(conn):
                await conn.execute("select set_config('hnsw.iterative_scan', 'relaxed_order', true)")
            rows = await conn.fetch(
                f"select c.chunk_id from doclib.chunks c join doclib.files f using (file_id) where {where} "
                f"and c.embedding_model = ${model_param} and c.embedding is not null "
                f"order by c.embedding <=> $1::vector limit {int(limit)}",
                to_pgvector(vector), *params, self.settings.embeddings.model)
        return [r["chunk_id"] for r in rows]

    async def _keyword_search(self, query: str, where: str, params: list[Any], limit: int) -> list[str]:
        # Any term may match, so long questions still return results; more matching terms rank higher.
        query_sql = "nullif(replace(plainto_tsquery('simple', $1)::text, ' & ', ' | '), '')::tsquery"
        rows = await self.pool.fetch(
            f"select c.chunk_id from doclib.chunks c join doclib.files f using (file_id) "
            f"cross join lateral (select {query_sql} as q) k "
            f"where {where} and c.tsv @@ k.q order by ts_rank_cd(c.tsv, k.q) desc limit {int(limit)}",
            query, *params)
        return [r["chunk_id"] for r in rows]

    async def search_docs(self, tenant_id: str, query: str, top_k: int = 8, filters: Filters | None = None) -> dict[str, Any]:
        if not query or not query.strip():
            raise BadRequest("query is required")
        top_k = max(1, min(int(top_k), MAX_TOP_K))
        started = time.monotonic()
        where, params = _where(tenant_id, filters or Filters(), start=2)
        candidates = max(self.settings.search.candidates, top_k)
        vector_task = self._vector_search(query, where, params, candidates)
        keyword_task = self._keyword_search(query, where, params, candidates)
        vector, keyword = await asyncio.gather(vector_task, keyword_task, return_exceptions=True)
        degraded: list[str] = []
        rankings: list[list[str]] = []
        for name, outcome in (("vector", vector), ("keyword", keyword)):
            if isinstance(outcome, EmbeddingError | EmbeddingRejected | asyncpg.PostgresError | OSError):
                degraded.append(name)
                event("search.path_failed", level=30, path=name, error=safe_error(outcome))
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                rankings.append(outcome)
        if not rankings:
            raise RuntimeError("both search paths failed")
        scores = rrf_merge(rankings, self.settings.search.rrf_k)
        # Fetch extra neighbours so adjacent hits can merge, then cut to top_k after grouping.
        ranked = sorted(scores, key=lambda key: scores[key], reverse=True)[:top_k * 3]
        rows = await self.pool.fetch(
            "select c.chunk_id, c.file_id::text, c.ordinal, c.anchor, c.heading_path, c.page_start, c.page_end, c.text, "
            "c.extraction_path, c.quality, c.content_version, f.source_id, f.path, f.title, f.modtime "
            "from doclib.chunks c join doclib.files f using (file_id) "
            "where c.chunk_id = any($1::text[]) and c.tenant_id = $2", ranked, tenant_id)
        lookup = {r["chunk_id"]: {**dict(r), "score": scores[r["chunk_id"]]} for r in rows}
        ordered = [lookup[key] for key in ranked if key in lookup]
        results = []
        for item in group_adjacent(ordered)[:top_k]:
            uri = f"doclib://{item['source_id']}/{item['file_id']}@{item['content_version']}#{item['anchor']}"
            results.append({
                "chunk_id": item["chunk_id"],
                "chunk_ids": item["chunk_ids"],
                "text": item["text"],
                "score": round(item["score"], 4),
                "citation": {
                    "source_id": item["source_id"],
                    "file_id": item["file_id"],
                    "path": item["path"],
                    "title": item["title"],
                    "anchor": item["anchor"],
                    "heading_path": item["heading_path"],
                    "page_start": item["page_start"],
                    "page_end": item["page_end"],
                    "content_version": item["content_version"],
                    "modtime": item["modtime"].isoformat().replace("+00:00", "Z") if item["modtime"] else None,
                    "uri": uri,
                },
                "extraction_path": item["extraction_path"],
                "quality": item["quality"],
            })
        duration = time.monotonic() - started
        telemetry.search_latency.record(duration)
        # The query itself may be sensitive, so log its length and hash only.
        event("search.completed", tenant_id=tenant_id, query_len=len(query),
              query_sha=hashlib.sha256(query.encode()).hexdigest()[:12], results=len(results),
              duration_ms=round(duration * 1000, 1), degraded=degraded)
        return {"results": results, "degraded": degraded, "content_is_untrusted": True}

    async def _find_file(self, tenant_id: str, file_id: str | None, source_id: str | None, path: str | None) -> asyncpg.Record:
        columns = "file_id::text, tenant_id, source_id, path, content_version, indexed_version, title, status"
        if file_id:
            row = await self.pool.fetchrow(
                f"select {columns} from doclib.files where file_id = $1::uuid and tenant_id = $2 and deleted_at is null",
                file_id, tenant_id)
        elif source_id and path:
            row = await self.pool.fetchrow(
                f"select {columns} from doclib.files where source_id = $1 and path = $2 and tenant_id = $3 "
                "and deleted_at is null", source_id, path, tenant_id)
        else:
            raise BadRequest("provide uri, file_id, or source_id and path")
        if row is None:
            raise NotFound("file not found in the index")
        return row

    async def open_doc(self, tenant_id: str, *, uri: str | None = None, file_id: str | None = None, source_id: str | None = None,
                       path: str | None = None, anchor: str | None = None, page_range: str | None = None,
                       heading: str | None = None, max_chars: int | None = None) -> dict[str, Any]:
        wanted_version: str | None = None
        if uri:
            match = _URI.match(uri)
            if not match:
                raise BadRequest("invalid doclib:// URI")
            file_id, wanted_version = match.group("file"), match.group("version")
            anchor = anchor or match.group("anchor")
        if sum(1 for v in (anchor, page_range, heading) if v) > 1:
            raise BadRequest("provide only one of anchor, page_range or heading")
        row = await self._find_file(tenant_id, file_id, source_id, path)
        source = await load_source(self.pool, row["source_id"])
        entry = await self.rclone.stat(source, row["path"])
        if entry is None:
            raise NotFound("file no longer exists at the source")
        live = content_version(entry)
        indexed = row["indexed_version"] or row["content_version"]
        stale = live != indexed
        reindex_queued = False
        if stale:
            reindex_queued = await self.pool.fetchval(
                "select doclib.request_reindex($1::uuid, $2, $3)", row["file_id"], tenant_id, live)
            telemetry.stale_hits.add(1)
            event("open_doc.stale", file_id=row["file_id"], source_id=row["source_id"], live_version=live,
                  indexed_version=indexed, reindex_queued=reindex_queued)
        data = await self.rclone.cat(source, row["path"], source.max_file_size_mb * 1024 * 1024)
        filename = row["path"].rsplit("/", 1)[-1]
        doc = await extract_document(data, filename, self.settings.extraction)
        del data
        chosen, found = select_blocks(doc, anchor=anchor, page_range=page_range, heading=heading)
        limit = max_chars or self.settings.search.open_doc_max_chars
        end = (chosen[-1].index + 1) if chosen else 0
        markdown, truncated, continuation = render(chosen, limit, end)
        pages = [b.page for b in chosen if b.page is not None]
        event("open_doc.completed", file_id=row["file_id"], source_id=row["source_id"], stale_index=stale,
              chars=len(markdown), truncated=truncated, located=found)
        return {
            "file_id": row["file_id"],
            "source_id": row["source_id"],
            "path": row["path"],
            "title": row["title"] or doc.title,
            "content_version": live,
            "indexed_version": row["indexed_version"],
            "stale_index": stale,
            "version_changed": bool(wanted_version and wanted_version != live),
            "locator_found": found,
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
            "markdown": markdown,
            "truncated": truncated,
            "continuation_anchor": continuation,
            "content_is_untrusted": True,
        }

    async def get_doc_outline(self, tenant_id: str, *, file_id: str | None = None, source_id: str | None = None,
                              path: str | None = None) -> dict[str, Any]:
        row = await self._find_file(tenant_id, file_id, source_id, path)
        outline = await self.pool.fetchval(
            "select outline from doclib.outlines where file_id = $1::uuid and content_version = $2",
            row["file_id"], row["indexed_version"])
        return {"file_id": row["file_id"], "source_id": row["source_id"], "path": row["path"], "title": row["title"],
                "content_version": row["indexed_version"], "indexed": outline is not None, "outline": outline or [],
                "content_is_untrusted": True}

    async def list_sources(self, tenant_id: str) -> dict[str, Any]:
        rows = await self.pool.fetch(
            """
            select s.source_id, s.rclone_remote, s.root_path, s.last_scan_at, s.last_scan_ok, s.last_scan_error,
                   s.last_success_at, coalesce(jsonb_object_agg(c.status, c.n) filter (where c.status is not null), '{}') counts
              from doclib.sources s
              left join (select source_id, status, count(*) n from doclib.files group by 1, 2) c using (source_id)
             where s.tenant_id = $1 group by s.source_id order by s.source_id
            """, tenant_id)
        return {"sources": [{
            "source_id": r["source_id"], "root_path": r["root_path"], "files_by_status": r["counts"],
            "last_scan_at": r["last_scan_at"], "last_scan_ok": r["last_scan_ok"], "last_scan_error": r["last_scan_error"],
            "last_success_at": r["last_success_at"]} for r in rows]}

    async def index_status(self, tenant_id: str) -> dict[str, Any]:
        tenant_jobs = "from doclib.jobs j join doclib.files f using (file_id) where f.tenant_id = $1"
        queue_rows = await self.pool.fetch(
            f"select j.kind, j.status, count(*) n {tenant_jobs} and j.status in ('queued','running','dead') group by 1, 2",
            tenant_id)
        depth: dict[str, dict[str, int]] = {}
        for row in queue_rows:
            depth.setdefault(row["kind"], {})[row["status"]] = row["n"]
        dead = await self.pool.fetch(
            f"select j.job_id, j.kind, j.file_id::text, f.path, j.last_error {tenant_jobs} and j.status = 'dead' "
            "order by j.finished_at desc limit 20", tenant_id)
        failed = await self.pool.fetchval(
            "select count(*) from doclib.files where tenant_id = $1 and status = 'failed'", tenant_id)
        oldest = await self.pool.fetchval(
            "select min(changed_at) from doclib.files where tenant_id = $1 and status = 'pending'", tenant_id)
        webhook = await self.pool.fetch(
            "select status, count(*) n from doclib.webhook_deliveries where tenant_id = $1 group by status", tenant_id)
        vlm = self.settings.extraction.vlm
        return {
            "queue": depth,
            "failed_files": failed,
            "dead_jobs": [dict(r) for r in dead],
            "oldest_unprocessed_change": oldest,
            "vlm": {"enabled": vlm.enabled, "model": vlm.model if vlm.enabled else None,
                    "reachable": await vlm_reachable(vlm) if vlm.enabled else None},
            "webhook": {r["status"]: r["n"] for r in webhook},
            "embeddings": {
                "model": self.settings.embeddings.model,
                # Chunks from any other model still need `doclib reindex --all` to be found by vector search.
                "chunks_by_model": {r["embedding_model"]: r["n"] for r in await self.pool.fetch(
                    "select embedding_model, count(*) n from doclib.chunks where tenant_id = $1 group by 1", tenant_id)},
            },
        }


__all__ = ["BadRequest", "Filters", "Library", "NotFound", "extension"]
