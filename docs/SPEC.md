# Document Library: Requirements and Design

This is the detailed design behind the service: what it must do, and why it's built this way. To install and use it, start with the [README](../README.md). Phase 1 in section 19 is built, along with the vision model service from section 6.3. Some items in the later phases are not built yet.

---

## 1. Context

AI agents that help operations teams need the team's know-how, and most of it lives in **shared drives**: vendor manuals, procedures (MOPs), runbooks, design documents, IP plans and port maps in spreadsheets, and contact lists.

This service, the **Document Library**, makes shared-drive documents searchable for AI agents **without copying the files out of the drive**. It indexes the content (text pieces, vectors and pointers) and fetches the original live from the drive when an agent needs to read it.

### Relationship to other components

| Component | Role | Relationship |
|---|---|---|
| **Agent memory service** | Learned experience: past incidents, causes, fixes, outcomes | **Separate service and separate schema.** Memories may *reference* library documents by stable ID (see §13). |
| **Telemetry platform** | Receives logs, metrics and traces | Optional. This service exports over OTLP when configured (see §15). |
| **API gateway** | Policy and authorisation in front of agent tools | Sits in front of this service's MCP endpoint. This service trusts the gateway's caller identity. |

---

## 2. Goals and non-goals

### Goals
1. Index documents **in place** on existing storage (SMB/CIFS file shares, SharePoint/OneDrive, Google Drive, S3-compatible storage, WebDAV/SFTP, local paths) with **no persistent copy** of source files.
2. Extract high-quality, structure-aware text from born-digital and scanned documents, including tables.
3. Provide **hybrid search** (semantic + keyword) with precise citations (file, page, section, version).
4. Let agents **open the live original** (a section or page range) on demand.
5. Keep the index **incrementally in sync** with the drive (new, changed, deleted, moved files).
6. Run **fully on-prem and air-gapped**, **CPU-only by default**, and faster with a GPU if one exists.
7. Stay **lightweight**: few moving parts, small memory footprint, one database (Postgres).

### Non-goals (v1)
- No chat UI, no agents, no LLM answer generation. This service retrieves; AI agents reason.
- No writes to source drives, ever.
- No agent memory (that is the job of a separate memory service).
- No per-user permission enforcement in v1. Permission data is captured but not enforced (see §12).
- No figure or diagram *understanding* (topology from network diagrams). Text inside figures is in scope via OCR.

---

## 3. Design principles (hard requirements)

1. **Source of truth is the drive.** The index is derived and disposable. Deleting the database and re-running must rebuild it.
2. **No file copies at rest.** Files are streamed for processing. Any temporary file lives in tmpfs or a scratch directory and is deleted immediately after processing (including on failure).
3. **Read-only access.** Use read-only credentials or service accounts. All source access paths must be read-only by configuration, not by convention.
4. **Air-gap safe.** No outbound network calls except to configured sources, configured model endpoints, the configured telemetry endpoint and the configured notification webhook. No vendor telemetry, licence keys, phone-home dependencies or runtime downloads.
5. **Commercial-licence clean.** Only permissive licences (MIT, Apache-2.0, BSD, PostgreSQL) in the shipped product. See §17.
6. **CPU-first, GPU-optional.** Every feature works without a GPU. The GPU only makes the hard-page path faster.
7. **Idempotent and restartable.** Every job is keyed by `(file_id, content_version)` and is safe to retry.
8. **Document content is untrusted data.** Indexed text may contain prompt-injection text. It must never be treated as instructions (see §16).

---

## 4. Architecture

```
                Your storage (read-only)
  SMB · SharePoint/OneDrive · Google Drive · S3 · WebDAV/SFTP · local
                          │
                    [ rclone ]  ← one access layer for all sources
                          │
   ┌──────────────────────┴─────────────────────────┐
   │ 1. Scanner / change detector  (periodic lsjson) │
   └──────────────────────┬─────────────────────────┘
                          │ new / changed / deleted / moved
                 ┌────────▼────────┐
                 │ 2. Job queue     │  Postgres table, SKIP LOCKED
                 └────────┬────────┘
                          │
   ┌──────────────────────▼─────────────────────────┐
   │ 3. Extract worker (CPU)                         │
   │    stream file → xberg → blocks per page        │
   │    page router: easy page → keep                │
   │                 hard page → render → VLM queue  │
   └───────┬───────────────────────────┬────────────┘
           │                           │
           │              ┌────────────▼─────────────┐
           │              │ 4. VLM worker             │
           │              │  OpenAI-compatible API    │
           │              │  (vLLM / Ollama)          │
           │              │  GLM-OCR default          │
           │              └────────────┬─────────────┘
           └─────────────┬─────────────┘
                         ▼
   ┌─────────────────────────────────────────────────┐
   │ 5. Normalise → chunk → embed                     │
   │    (embeddings via OpenAI-compatible endpoint)   │
   └──────────────────────┬──────────────────────────┘
                          ▼
              ┌──────────────────────┐
              │ 6. Postgres "doclib"  │  pgvector + keyword index
              └──────────┬───────────┘
                          ▼
   ┌─────────────────────────────────────────────────┐
   │ 7. Query service: REST + MCP (read-only)         │
   │    search_docs · open_doc · get_doc_outline ·    │
   │    list_sources · index_status                   │
   └─────────────────────────────────────────────────┘
                          ▲
                  API gateway
```

### Processes / containers
| Process | Scales by | Notes |
|---|---|---|
| `doclib-scanner` | 1 per deployment | Periodic change detection; enqueues jobs. |
| `doclib-worker` | N replicas | Extract, route, normalise, chunk, embed. CPU-bound. |
| `doclib-vlm-worker` | 0..N | Upgrades hard pages through the VLM endpoint in the background. Disabled if no VLM is configured. |
| `doclib-extract` | optional | `doclib extract-server`: a text backend in its own container, used with the `http` backend. |
| `doclib-api` | N replicas, stateless | REST + MCP. Read-only DB role. |
| Postgres 16+ | external or bundled | Shared instance with an agent memory service is allowed; **separate schema `doclib`**. |
| Vision server (`deploy/vision`, or vLLM/Ollama) | optional | Local CPU or remote GPU; see §6.3. |
| Embedding server | required | Any OpenAI-compatible `/v1/embeddings` server (TEI, vLLM, Ollama). |

**Language:** Python 3.12 is recommended for workers and API (xberg's Rust core does the heavy lifting; the VLM and embeddings run in their own servers). A Go or Rust implementation is acceptable if it uses the same libraries and contracts.

---

## 5. Source access (rclone)

- Use **rclone** as the single access layer. Sources are configured as rclone remotes plus a root path.
- v1 must support: **SMB/CIFS**, **SharePoint/OneDrive**, **local filesystem**. Should support: **Google Drive**, **S3/MinIO**, **WebDAV**, **SFTP**.
- Access only through `rclone lsjson` (listing), `rclone cat` / ranged reads (content) and `rclone backend` metadata calls. **No `sync`, `copy` or `mount` with write.** Enforce read-only at credential level where the source supports it.
- Each source has:
  - `include` / `exclude` glob lists (for example, include `Operations/**`, exclude `**/~$*`, `**/.DS_Store`)
  - `max_file_size_mb` (default 200)
  - `allowed_extensions` (default: see §6.1)
  - `scan_interval` (default 15 min)
  - `tags` (free-form labels, for example `vendor:acme`, `type:runbook`), applied to all files from that source or path prefix
- Rate limits and concurrency per source must be configurable (SharePoint and Google Drive throttle).

### 5.1 Change detection
1. Run `rclone lsjson --recursive --hash --metadata` (use `--fast-list` where supported) on each source.
2. Compare each entry `(path, size, modtime, hash or etag)` with `doclib.files`:
   - **New:** enqueue `index`.
   - **Changed** (hash or etag differs; fall back to size+modtime if no hash): enqueue `index` with the new version.
   - **Missing:** mark the file deleted and remove its chunks (tombstone kept 30 days for audit).
   - **Moved/renamed** (same hash, new path, old path missing): update the path without re-extracting.
3. Store the full listing snapshot time per source, and expose it via `index_status`.
4. **Later optimisation (not v1):** native change feeds (Microsoft Graph delta, Google Drive changes API) behind the same interface.
5. **Mass-delete guard:** if a listing would delete more than `max_delete_ratio` of a source's files (default 0.5), the scan aborts and logs an error, so an unmounted share cannot empty the index. An admin can override this through the CLI.

### 5.3 Connectors (declarative sources)
Other platform services add sources through a **separate admin API** (`doclib admin-api`). It has its own port and auth and no MCP, so agents can't add sources (§16).

- **Declarative:** a connector is `type` (any allowed rclone backend), `root_path`, non-secret `options`, and `credentials`. Types and options are validated against rclone's own catalogue (`rclone config providers`), so a backend rclone supports works with no code change. `PUT` is idempotent, and errors share one shape.
- **Credentials:** `credentials` maps an rclone option to an **environment variable name**. Values are never stored or returned, and reach rclone through its environment, not its command line. Names must carry the `DOCLIB_CRED_` prefix, so a caller can't request this service's own secrets. Secrets in `options` are refused.
- **Allowed types** are configuration. `local` and wrapper backends (`alias`, `crypt`, `union` and so on) are excluded because they could read this service's own files.
- **No restart:** Postgres is the registry that every process reads, and the scanner reconciles its scan loops against it. A new environment variable does need a restart, because environments are fixed at process start.

The admin API uses the read-write database role. It must not be exposed to agents.

### 5.2 Change notifications (webhook)
Every detected change is indexed automatically and reported through **one standard outbound webhook**. The service only knows the webhook; the receiver decides which channels (chat, email, ticketing) to use.

- **When:** after indexing finishes, so "new" or "changed" means searchable now. Failures are reported too.
- **Grouping:** one digest per source per scan. A long-running scan sends what is ready after `digest_max_wait` (default 5 min). Large digests are split into parts of `max_changes_per_message` (default 500).
- **First index:** the first successful scan of a source is the baseline and sends no notifications.
- **Delivery:** at-least-once, via a Postgres outbox with retries and backoff. Receivers de-duplicate on `event_id`.
- **Signing:** optional HMAC-SHA256 of `{timestamp}.{body}` in `X-Doclib-Signature: sha256=<hex>`, with `X-Doclib-Timestamp` and `X-Doclib-Event-Id`. The secret comes from a file.
- **Content:** paths, versions and change types only; never document text.

```json
{
  "schema_version": "1.0",
  "event_id": "uuid",
  "event_type": "doclib.source.changes",
  "occurred_at": "2026-11-05T03:22:00Z",
  "tenant_id": "acme",
  "source_id": "ops-share",
  "scan_id": "uuid",
  "part": 1, "parts": 1,
  "text": "ops-share: 1 new, 1 changed, 1 failed",
  "summary": {"new": 1, "changed": 1, "deleted": 0, "moved": 0, "indexed": 1, "failed": 1, "skipped": 0},
  "changes": [
    {"change": "new", "outcome": "indexed", "file_id": "uuid", "path": "Vendors/Guide.pdf",
     "previous_path": null, "content_version": "…", "error": null}
  ]
}
```

---

## 6. Extraction

### 6.1 Fast path (default, CPU): xberg
- Text extraction is a **pluggable backend** selected by `extraction.text.backend`: `xberg` (in-process, default), `http` (a remote container with a three-route contract, so it can live on other hardware or in another language), or `module:Class`. A backend returns Markdown per page; the router, chunker and block model never depend on which one is used. Each file records its `extractor`, so after a swap (`doclib reindex --all`) it's clear what is left. Page anchors survive a swap; heading anchors may not, so `open_doc` reports `locator_found: false` when one no longer resolves.
- Library: **xberg** (MIT, Rust core). Always run with `use_cache=False`. Config, log and source text files are read directly and kept verbatim, because xberg downloads tree-sitter grammars at runtime for source-code types.
- Formats, by priority:
  - **P1:** PDF, DOCX, DOC, XLSX, XLS, CSV, PPTX, PPT, TXT, MD, HTML
  - **P2:** EML, MSG, RTF, ODT/ODS/ODP, JSON/YAML/XML, log and config text files
  - **Images** (PNG/JPG/TIFF): always go to the hard path, or to OCR fallback if no VLM
- Output per document: an ordered list of **blocks** with `type` (heading / paragraph / list / table / code / figure / caption), heading level, page (or slide or sheet) number, and text or table structure.
- Spreadsheets: one logical "page" per sheet. Tables keep headers. Very large sheets (> 5,000 rows) are indexed as row groups of N rows, with the header repeated in each group.
- Code, CLI and config blocks (monospace runs, `.cfg`/`.conf` files) must be preserved verbatim, keeping whitespace.

### 6.2 Page router
For each page from the fast path, send it to the **hard path** if **any** of these is true (all thresholds configurable):
- No text layer, or fewer than `min_chars_per_page` characters (default 50).
- Images cover more than `image_area_ratio` of the page (default 0.6).
- A table was detected but its structure is low confidence (structure output missing or ragged).
- The text looks garbled: replacement/non-printable character ratio above `garbled_ratio` (default 0.05), or broken encoding.
- The input is an image file.

If no VLM is configured, hard pages fall back to **xberg's built-in OCR** (CPU; backend is configuration). If OCR returns far less text than the native layer, keep the native text and flag the page `quality=degraded`. Record which path produced each page.

### 6.3 Hard path (VLM)
The vision model runs as a **separate service behind an OpenAI-compatible chat completions endpoint**. Where it runs is purely a deployment choice, and doclib only needs its URL:

| Scenario | Runs as | `extraction.vlm.endpoint` | Suggested `concurrency` |
|---|---|---|---|
| Small site, no GPU | `doclib-vision:cpu` container beside doclib | `http://vision:8000/v1` | 1 |
| GPU on another machine | `doclib-vision:gpu` container on that machine | `https://gpu-host/v1` (TLS or VPN, plus API key) | 8 |
| Existing model serving | vLLM, Ollama or llama.cpp | that server's URL | to suit |

- **Order of work:** text extraction always runs first, per page. Pages it handled well never reach the vision model. Vision output replaces a page's text only if it keeps at least half of what the native text layer had.
- Files are never held up. A hard page gets OCR text straight away, so the file is searchable and notified as soon as it is indexed. A low-priority `vlm_page` job in its own `doclib-vlm-worker` then upgrades those pages and swaps the chunks atomically. It re-reads the file from the source, so nothing is stored while waiting.
- If the endpoint is unreachable, jobs wait without using up attempts, and `index_status` shows `vlm.reachable: false`.
- Page images are rendered in memory (PDF via pdfium; default 170 DPI) and sent in the request body. The server keeps nothing on disk and never logs content. Across machines, page images leave the host, so TLS or a private network and an API key are required.
- Default model: **GLM-OCR** (MIT), because a single whole-page prompt returns Markdown. **PaddleOCR-VL-1.5** (Apache-2.0) is the alternative, but its documented page-level parsing needs Paddle's separate pipeline and its plain model route handles single page elements, so it would need its own server to meet this contract. Model name and prompt are configuration. The bake-off confirms the choice.
- Expected output: Markdown (headings, tables as Markdown or HTML tables, lists), parsed back into the same block model as the fast path.
- Concurrency limit, per-request timeout and retries. Pages that still fail keep their OCR text and are flagged `quality=degraded`.
- **CPU mode:** measured at about 30 to 170 seconds per page on an Apple M4 Max in Docker. It needs about 6 GB of memory for the model alone, on top of §18's minimum, so allow at least 12 GB in total. The queue is low priority and never blocks the fast path.
- `open_doc` uses the fast path and OCR for live reads, because agents can't wait for a CPU vision model.

### 6.4 Normalised document model
Each processed file version produces:
- `document.md`: Markdown rendering. Not stored in full; generated on demand by `open_doc`. Only chunks are stored.
- An outline: the heading tree, with page ranges per heading.
- Blocks → chunks (§7).

Each document records the language it was detected in. Mixed English/Japanese is expected.

---

## 7. Chunking

- **Structure-aware**: chunk by heading sections first, then split long sections by paragraph boundaries.
- Target chunk size: 400–800 tokens (configurable), with a small overlap (default 50 tokens) **only** when splitting inside a section.
- **Never split** a code/CLI block or a table row. The one exception is a code block above `max_block_tokens` (default 2,000; for example a long log file), which is split at line boundaries because embedding servers reject oversized input. Tables longer than the target are split by row groups, with the header row repeated in each.
- Each chunk carries:
  - `heading_path` breadcrumb, for example `"5G Core Ops Guide > 4 SMF > 4.2 Restarting SMF pods"`, which is also prepended to the embedded text
  - `page_start`, `page_end` (or sheet name or slide number)
  - `block_types` contained (for example `["table"]`)
  - `extraction_path` (`fast` / `vlm` / `ocr`) and `quality`
  - a stable `chunk_id` = hash of `(file_id, content_version, ordinal)`
  - an `anchor`: a stable, human-readable locator that is used for citations and for saved references from other services: `page:12`, or `page:12-13` when content spans pages (`slide:` for presentations), `sheet:IP Plan` for spreadsheets, and `heading:4.2` for formats with no fixed pages, such as Word and Markdown
  - a `doc_label`: words from the file name and title, in the keyword index only, so a document can be found by what it's called. Renames update it without re-embedding

Page numbers are positions in the file, so PDF page 130 may be printed as "page 112". Word and Markdown files have no fixed pages, so their references are sections and their page fields stay empty rather than guessed.

---

## 8. Embeddings

- Call any **OpenAI-compatible `/v1/embeddings`** endpoint. Model name and dimension are configuration.
- The model must be **multilingual** (English + Japanese at minimum) and permissively licensed. Default suggestion: **BGE-M3**. Final choice comes from the bake-off (§19).
- Store `embedding_model` and `embedding_dim` per chunk. Changing the model triggers a background **re-embed** job (no re-extraction needed).
- Batch requests (default batch 64) and limit concurrency.
- The embedding model is a **separate service reached by URL**, so a deployment can use one that already exists in its cluster. `deploy/compose.embed.yml` is an optional bundled server for testing and small sites. `doclib check` confirms the service's dimension matches the config and database.
- Vector search only compares chunks from the configured model, so a model change can't return meaningless matches. Until `doclib reindex --all` finishes, older chunks are found by keyword.

---

## 9. Storage (Postgres, schema `doclib`)

Postgres 16+ with **pgvector**. Draft DDL (the implementer may refine it, but must keep the semantics):

```sql
create schema if not exists doclib;

create table doclib.sources (
  source_id      text primary key,          -- e.g. 'ops-share'
  tenant_id      text not null default 'default',
  rclone_remote  text not null,
  root_path      text not null,
  config         jsonb not null,            -- include/exclude, tags, limits
  last_scan_at   timestamptz,
  last_scan_ok   boolean,
  baseline_at    timestamptz                -- first successful scan; no notifications before this
);

create table doclib.files (
  file_id        uuid primary key,
  tenant_id      text not null,
  source_id      text not null references doclib.sources,
  path           text not null,
  size_bytes     bigint,
  modtime        timestamptz,
  content_hash   text,                       -- hash or etag
  content_version text not null,             -- latest seen: hash, else size+modtime
  indexed_version text,                      -- version the live chunks belong to
  mime_type      text,
  language       text,
  title          text,
  tags           text[] default '{}',
  acl            jsonb,                      -- captured principals, not enforced in v1
  status         text not null,              -- pending|indexed|failed|deleted|skipped
  error          text,
  indexed_at     timestamptz,
  deleted_at     timestamptz
);
-- Partial, so a path can be reused while an old tombstone exists.
create unique index on doclib.files (source_id, path) where deleted_at is null;

create table doclib.outlines (
  file_id        uuid references doclib.files on delete cascade,
  content_version text,
  outline        jsonb not null,             -- heading tree with page ranges
  primary key (file_id, content_version)
);

create table doclib.chunks (
  chunk_id       text primary key,
  file_id        uuid not null references doclib.files on delete cascade,
  tenant_id      text not null,
  content_version text not null,
  ordinal        int not null,
  anchor         text not null,
  heading_path   text,
  page_start     int, page_end int,
  block_types    text[],
  extraction_path text,                      -- fast|vlm|ocr
  quality        text,                       -- ok|degraded
  text           text not null,
  token_count    int,
  embedding      vector(1024),               -- dimension fixed at install (HNSW requires one)
  embedding_model text,
  embedding_dim  int,
  tsv            tsvector                    -- keyword index (see note)
);
create index on doclib.chunks using hnsw (embedding vector_cosine_ops);
create index on doclib.chunks using gin (tsv);
create index on doclib.chunks (file_id);

create table doclib.jobs (
  job_id         bigserial primary key,
  kind           text not null,              -- index|delete|move|reembed|vlm_page
  file_id        uuid,
  payload        jsonb,
  priority       int default 100,
  attempts       int default 0,
  run_after      timestamptz default now(),
  locked_by      text, locked_at timestamptz,
  last_error     text,
  status         text default 'queued'       -- queued|running|done|dead
);
create index on doclib.jobs (status, priority, run_after);
```

Changing to a model with a different dimension means adding a new column and index, then re-embedding. Tables for §5.2 (`change_events`, `webhook_deliveries`) are in `src/doclib/sql/`.

**Keyword search and Japanese:** Postgres's built-in full-text search does not segment Japanese. Keyword search **must** work for Japanese text. Choose and document one of:
- **PGroonga** (preferred if it can be packaged for air-gapped installs)
- **pg_bigm**

Keep `tsv` (the `simple` config) for English, and add the Japanese-capable index on `text`.

**Database roles:** the workers use a read-write role on `doclib`. `doclib-api` uses a **read-only** role. Its only write path is `doclib.request_reindex()`, a narrow `SECURITY DEFINER` function used by `open_doc` when it detects a stale index.

---

## 10. Search and read API

All endpoints are read-only. Expose them as **REST** (JSON) and as an **MCP server** (streamable HTTP), with identical semantics.

### 10.1 `search_docs`
**Input:** `query` (string, required), `top_k` (default 8, max 50), and optional filters:
- `source_ids[]`
- `path_prefix`
- `tags[]`
- `file_types[]`
- `modified_after`
- `language`
- `block_types[]` (for example only tables)

**Retrieval:**
1. Run vector search (top 50) and keyword search (top 50) in parallel.
2. Merge them with **Reciprocal Rank Fusion**.
3. Optionally rerank with a cross-encoder, through a configurable OpenAI-compatible or TEI rerank endpoint (default off in v1).
4. Group adjacent chunks from the same section into one result where they rank together.

**Output per result:**
```json
{
  "chunk_id": "…",
  "text": "…",
  "score": 0.83,
  "citation": {
    "source_id": "ops-share",
    "path": "Vendors/Acme/5G Core Ops Guide v3.pdf",
    "title": "5G Core Ops Guide",
    "anchor": "page:112",
    "heading_path": "4 SMF > 4.2 Restarting SMF pods",
    "page_start": 112, "page_end": 113,
    "content_version": "…",
    "modtime": "2026-08-14T03:22:00Z"
  },
  "extraction_path": "fast",
  "quality": "ok"
}
```

### 10.2 `open_doc`
- **Input:** `file_id` or (`source_id`, `path`), plus one of: `anchor`, `page_range`, or `heading`.
- Fetches the **live original from the source** via rclone, extracts the requested part and returns Markdown.
- If the live `content_version` differs from the indexed one, return the live content anyway, set `stale_index: true` and enqueue a re-index.
- Limit output size (default 20k characters). Return `truncated: true` and a continuation anchor when needed.

### 10.3 `get_doc_outline`
Returns the heading tree with anchors and page ranges for a file, so an agent can navigate a long manual.

### 10.4 `list_sources`
Returns sources, file counts by status, and last scan time.

### 10.5 `index_status`
Returns queue depth by job kind, failed and dead jobs, VLM availability, and freshness (oldest unprocessed change).

### 10.6 Auth and tenancy
- The service sits behind the API gateway. Accept a caller identity and `tenant_id` from the gateway (mTLS or signed bearer token; configurable).
- **Every query is filtered by `tenant_id`.** The default deployment is single-tenant on-prem, but the schema and queries must be tenant-aware for hosted, multi-tenant use.

---

## 11. Freshness and performance targets

All targets are measured on the bake-off corpus and reference hardware. If a target can't be met, report the measured numbers; don't drop features.

| Target | Value |
|---|---|
| Change → searchable (fast-path files) | ≤ scan interval + 5 min (default ≤ 20 min) |
| Fast-path throughput, born-digital PDF | ≥ 10 pages/s per CPU core (to validate) |
| Worker memory | ≤ 2 GB RSS per worker, excluding model servers |
| `search_docs` latency, 1M chunks, no reranker | p95 ≤ 300 ms |
| `open_doc` latency (SMB, 50-page PDF section) | p95 ≤ 3 s |
| Scale design point | 1M files / 20M chunks per tenant |
| Index size | Report the ratio of index size to source size (expected to be a small fraction) |

---

## 12. Permissions

- **v1:** index only the folders the deployment designates, using a read-only service account. Every agent on that deployment can search everything indexed.
- **Capture ACLs anyway** where the source exposes them (SharePoint/OneDrive and Google Drive permissions via metadata; SMB ACLs where obtainable). Store them in `files.acl` as a list of `{principal, type, permission}`.
- **v2 (design for, don't build):** filter `search_docs` by the caller's principals and groups passed from the gateway. Do not require re-indexing to enable this.

---

## 13. Integration with an agent memory service

- An agent memory service runs as a **separate service**. The Library does not write to the memory service, and the memory service does not write to the Library.
- When an incident memory refers to a document, the memory service stores a **reference**, not the content:
  `doclib://{source_id}/{file_id}@{content_version}#{anchor}`
- The Library must **resolve these URIs** (`open_doc` accepts them). It must keep the stability guarantees: `file_id` is stable across renames and moves, and `anchor` values are stable for a given `content_version`.
- If the referenced version no longer exists, `open_doc` returns the current version and flags `version_changed: true`.

---

## 14. Configuration

- One YAML file, with environment-variable overrides. Secrets come from files or a mounted vault path, never inline.
- Sketch:

```yaml
tenant_id: acme
postgres:
  dsn_file: /run/secrets/doclib_dsn
sources:
  - id: ops-share
    rclone_remote: smb-ops
    root_path: /Operations
    include: ["**"]
    exclude: ["**/~$*", "**/.DS_Store", "**/Archive/**"]
    tags: ["type:ops"]
    scan_interval: 15m
    max_file_size_mb: 200
  - id: sharepoint-noc
    rclone_remote: sp-noc
    root_path: /Shared Documents/NOC
    scan_interval: 15m
extraction:
  text:
    backend: xberg                    # or http, with endpoint: http://extract:8090/v1
  router:
    min_chars_per_page: 50
    image_area_ratio: 0.6
    garbled_ratio: 0.05
  vlm:
    enabled: true
    endpoint: http://vision:8000/v1   # or https://gpu-host/v1
    api_key_file: /run/secrets/vision_api_key
    model: zai-org/GLM-OCR
    prompt: "Text Recognition:"
    concurrency: 1                    # 8 or so on a GPU
    timeout_s: 300
    render_dpi: 170
  ocr_fallback: tesseract
chunking:
  target_tokens: 600
  overlap_tokens: 50
embeddings:
  endpoint: http://embed:8080/v1
  model: BAAI/bge-m3
  batch_size: 64
search:
  rerank:
    enabled: false
    endpoint: http://rerank:8080
notifications:
  webhook:
    enabled: true
    url_file: /run/secrets/doclib_webhook_url
    secret_file: /run/secrets/doclib_webhook_secret   # optional HMAC signing
    digest_max_wait: 5m
    max_changes_per_message: 500
logging:
  level: INFO
  file: null          # optional rotating file; JSON goes to stdout regardless
telemetry:
  otlp_endpoint: null # set to http://otel-collector:4318 to export telemetry
```

---

## 15. Observability

- When `telemetry.otlp_endpoint` is set, send **OpenTelemetry** logs, metrics and traces over OTLP.
- Required metrics:
  - files by status
  - queue depth by job kind
  - job duration (p50 and p95) by stage
  - pages by extraction path (fast / vlm / ocr)
  - VLM error and timeout rate
  - embedding throughput
  - search latency
  - stale-index hits from `open_doc`
  - scan duration and age of the last successful scan per source
- Structured logs with `file_id`, `source_id`, `job_id` and `trace_id`. **Never log document content** beyond 200-character snippets at debug level.

### 15.1 Internal logging
- Every process writes **structured JSON logs to stdout**, with an optional rotating file. This works with no external system.
- Log everything the system does: scans and detected changes, job claims, retries and outcomes, extraction routing, embedding calls, webhook deliveries, API requests and admin actions.
- The rule above still applies: no document content, and text snippets of at most 200 characters at debug level only. Search queries are logged by length and hash, not text.

---

## 16. Security

- Read-only source credentials, the least privilege needed, stored as secrets.
- Temporary files go in tmpfs or scratch, are deleted after each job and are cleaned on startup.
- **Prompt-injection hygiene:** document text is untrusted. MCP responses must return content in clearly delimited data fields (never in tool descriptions or system-style text), and include `"content_is_untrusted": true`. No component of this service may follow instructions found in documents.
- Optional (default off): redact secrets such as passwords, keys and tokens in chunk text at index time, using a pattern list. Many runbooks contain credentials, so this should be easy to switch on.
- The agent-facing API is read-only, uses a read-only DB role, and has no administrative endpoints. Admin actions go through the CLI or, for connectors, the separate service-to-service admin API (§5.3).

---

## 17. Licensing rules

The shipped product must use only **MIT, Apache-2.0, BSD, ISC, PostgreSQL or MPL-2.0** components (MPL only unmodified).

| Allowed (planned) | Not allowed without written approval |
|---|---|
| rclone (MIT), xberg (MIT), asyncpg (Apache-2.0), PaddleOCR-VL-1.5 (Apache-2.0), GLM-OCR (Apache-2.0 / MIT), Docling (MIT, fallback), pgvector (PostgreSQL), PGroonga (PostgreSQL), pg_bigm (PostgreSQL), BGE-M3 (MIT), vLLM (Apache-2.0), pdfium (BSD/Apache) | Marker and Chandra weights (OpenRAIL with revenue limit and non-compete), PyMuPDF / PyMuPDF4LLM (AGPL), MinerU (requires public attribution for online services), Pathway (BSL, licence key and telemetry), psycopg (LGPL), anything AGPL/GPL/LGPL in the shipped path |

Produce a dependency licence report (SBOM) as part of the build.

---

## 18. Deployment

- Container images for `scanner`, `worker`, `vlm-worker` and `api`. Provide a **Helm chart** (k3s/Kubernetes) and a **docker-compose** file for small sites.
- Air-gapped install: all images and model weights ship as an offline bundle. There are no runtime downloads (set the Hugging Face offline flags, and pre-fetch rclone and ONNX models).
- Minimum CPU-only site: 4 vCPU and 8 GB RAM for the Library (excluding Postgres and model servers).
- With GPU: a single 16–24 GB GPU is sufficient for the 0.9B VLM. It may be shared with other your model serving.

---

## 19. Delivery phases

**Phase 0: Bake-off harness (first)**
- Corpus: 50–100 real documents from a real deployment: vendor manuals, MOPs, IP-plan spreadsheets, PowerPoint designs, scans, and some Japanese documents.
- Compare:
  - fast path: **xberg vs Docling**
  - hard path: **GLM-OCR vs PaddleOCR-VL-1.5**
  - embeddings: **BGE-M3 vs one alternative**
- Metrics:
  - table fidelity (manual scoring on 30 tables)
  - heading-structure accuracy
  - pages per second (CPU and GPU)
  - **retrieval recall@5** on 50 real operator questions with known answer locations
- Output: a short results report, then lock the defaults.

**Phase 1: MVP**
- Sources: SMB, local, SharePoint/OneDrive.
- Vision model service with background upgrade (moved from Phase 2).
- Fast path, OCR fallback, chunking, embeddings, pgvector + English keyword search.
- `search_docs`, `open_doc`, `get_doc_outline`, `list_sources`, `index_status` over REST + MCP.
- Incremental sync (new, changed, deleted, moved) with webhook notifications, internal logging, optional OTLP telemetry, Helm + compose.

**Phase 2**
- Japanese keyword search (PGroonga or pg_bigm), optional reranker.
- Google Drive and S3 sources, secret redaction option, re-embed job.

**Phase 3**
- ACL enforcement, native change feeds (Graph delta, Drive changes).
- Visio (`.vsdx`) text and connection extractor (zipped XML: shape text and connectors).
- Figure captioning with a general VLM (optional).

---

## 20. Acceptance criteria (Phase 1)

1. **No copies:** after a full index of a 10 GB test share, no source file content exists on disk outside the database (verify scratch dirs are empty), and the index-to-source size ratio is reported.
2. **Sync:** for each change type (adding, editing, deleting, renaming, and moving a file to an excluded folder), the change is reflected in search within the freshness target. Deleted content no longer appears in results.
3. **Idempotency:** killing workers mid-job and restarting produces no duplicate chunks and no lost files.
4. **Citations:** every search result opens via `open_doc` to the cited page or section of the live file.
5. **Stale detection:** editing a file after indexing returns `stale_index: true` from `open_doc` and triggers a re-index.
6. **Air-gap:** with network egress blocked except to sources, Postgres and model endpoints, a full index and search succeed.
7. **Read-only:** source credentials cannot write. The automated tests confirm no write calls are issued.
8. **Retrieval quality:** recall@5 on the Phase 0 question set is at or above the agreed threshold (set after Phase 0).
9. **Licence report:** the SBOM shows no disallowed licences.
10. **Tests:** unit tests for the router, chunker and change detector, and integration tests against a Samba container and a local fixture corpus.

---

## 21. Open questions

1. PGroonga vs pg_bigm for Japanese keyword search: which packages more cleanly for air-gapped Postgres?
2. Final embedding model after the bake-off (BGE-M3 is the default candidate).
3. Do any early deployments keep key documents in Confluence? If so, add a Confluence source (API-based, not rclone) in Phase 2.
4. Should spreadsheets with IP plans or port maps *also* be exported as structured rows to the entity registry? That would be a separate integration; flag it if the data looks clean.
5. Secret-redaction default for production: on or off?
