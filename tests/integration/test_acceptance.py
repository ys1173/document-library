"""Phase 1 acceptance criteria (spec section 20) against real Postgres and rclone."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import pytest
import yaml
from fastapi.testclient import TestClient

from doclib.api import create_app
from doclib.db import _init_connection
from doclib.library import BadRequest, Filters, Library, NotFound, _where
from doclib.scanner import MassDeleteBlocked, scan_source
from doclib.worker import drain, reap
from tests.fakes.fixtures import make_guide_pdf
from tests.integration.conftest import DSN, build_settings, default_source, resync

pytestmark = pytest.mark.integration
TENANT = "acme"


async def search(library, query, **kwargs):
    response = await library.search_docs(TENANT, query, **kwargs)
    assert response["degraded"] == [], "a search path failed; results would hide it"
    return response["results"]


async def hits_containing(library, word):
    return [r for r in await search(library, word) if word in r["text"]]


async def paths_for(library, query):
    return {r["citation"]["path"] for r in await search(library, query)}


def tree_digest(root: Path) -> dict[str, tuple[str, int]]:
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


# Criterion 1: no copies, and a report of the index size.
def test_no_file_copies_on_disk_after_full_index(share, settings, tmp_path, pool):
    home, scratch, work = tmp_path / "home", tmp_path / "scratch", tmp_path / "cwd"
    for folder in (home, scratch, work):
        folder.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(json.loads(settings.model_dump_json())), encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "TMPDIR": str(scratch), "XDG_CACHE_HOME": str(home / ".cache")}
    for command in ("scan", "drain"):
        done = subprocess.run([sys.executable, "-m", "doclib.cli", "--config", str(config), command], env=env, cwd=work,
                              capture_output=True, text=True, timeout=240)
        assert done.returncode == 0, done.stderr[-2000:]
    leftovers = [str(p) for folder in (home, scratch, work) for p in folder.rglob("*") if p.is_file()]
    assert leftovers == []


async def test_index_contents(indexed, library):
    statuses = {r["path"]: r["status"] for r in await indexed.fetch("select path, status from doclib.files")}
    assert set(statuses) == {"Vendors/Guide.pdf", "MOPs/Upgrade.docx", "IP Plan.xlsx", "Design.pptx", "router.cfg",
                             "notes.md", "ja.txt"}  # Lock file and Archive/ are excluded.
    assert set(statuses.values()) == {"indexed"}
    assert await indexed.fetchval("select count(*) from doclib.chunks where embedding is null") == 0
    assert await indexed.fetchval("select count(distinct extractor) from doclib.files") == 2  # xberg and direct


# Criterion 2: sync. Each change type is reflected in search.
async def test_new_file_appears(indexed, library, share, settings, rclone, embedder):
    assert not await hits_containing(library, "xylophone")
    (share / "new.md").write_text("# New\n\nThe xylophone runbook.\n", encoding="utf-8")
    changes = await resync(indexed, settings, rclone, embedder)
    assert [e.path for e in changes.new] == ["new.md"]
    assert "new.md" in await paths_for(library, "xylophone")


async def test_edit_replaces_content(indexed, library, share, settings, rclone, embedder):
    (share / "notes.md").write_text("# Notes\n\nNow about zebrafish pagers.\n", encoding="utf-8")
    await resync(indexed, settings, rclone, embedder)
    assert "notes.md" in await paths_for(library, "zebrafish")
    assert not await hits_containing(library, "quokka")
    assert await indexed.fetchval(
        "select count(distinct c.content_version) from doclib.chunks c join doclib.files f using (file_id) "
        "where f.path = 'notes.md'") == 1


async def test_delete_removes_content_and_keeps_tombstone(indexed, library, share, settings, rclone, embedder):
    (share / "notes.md").unlink()
    await resync(indexed, settings, rclone, embedder)
    assert not [r for r in await search(library, "quokka") if "quokka" in r["text"]]
    row = await indexed.fetchrow("select status, deleted_at from doclib.files where path = 'notes.md'")
    assert row["status"] == "deleted" and row["deleted_at"] is not None
    assert await indexed.fetchval(
        "select count(*) from doclib.chunks c join doclib.files f using (file_id) where f.path = 'notes.md'") == 0


async def test_rename_keeps_file_id_without_reextracting(indexed, library, share, settings, rclone, embedder):
    before = await indexed.fetchrow("select file_id, indexed_at from doclib.files where path = 'notes.md'")
    (share / "notes.md").rename(share / "renamed.md")
    changes = await scan_source(indexed, settings, rclone, settings.sources[0])
    assert len(changes.moved) == 1 and not changes.new and not changes.deleted
    assert await indexed.fetchval("select count(*) from doclib.jobs where status = 'queued'") == 0
    after = await indexed.fetchrow("select file_id, indexed_at, path from doclib.files where file_id = $1", before["file_id"])
    assert after["path"] == "renamed.md" and after["indexed_at"] == before["indexed_at"]
    assert "renamed.md" in await paths_for(library, "quokka")


async def test_moving_to_excluded_folder_removes_from_search(indexed, library, share, settings, rclone, embedder):
    shutil.move(share / "notes.md", share / "Archive" / "notes.md")
    await resync(indexed, settings, rclone, embedder)
    assert not [r for r in await search(library, "quokka") if "quokka" in r["text"]]


async def test_recreating_a_deleted_path_works(indexed, library, share, settings, rclone, embedder):
    (share / "notes.md").unlink()
    await resync(indexed, settings, rclone, embedder)
    (share / "notes.md").write_text("# Back\n\nThe narwhal returns.\n", encoding="utf-8")
    await resync(indexed, settings, rclone, embedder)
    assert "notes.md" in await paths_for(library, "narwhal")
    assert await indexed.fetchval("select count(*) from doclib.files where path = 'notes.md'") == 1


async def test_failed_listing_deletes_nothing(indexed, share, settings, rclone):
    before = await indexed.fetchval("select count(*) from doclib.files where deleted_at is null")
    hidden = share.parent / "hidden"
    share.rename(hidden)
    with pytest.raises(Exception):  # noqa: B017 - any listing failure counts
        await scan_source(indexed, settings, rclone, settings.sources[0])
    hidden.rename(share)
    assert await indexed.fetchval("select count(*) from doclib.files where deleted_at is null") == before
    row = await indexed.fetchrow("select last_scan_ok, last_scan_error from doclib.sources")
    assert row["last_scan_ok"] is False and row["last_scan_error"]


async def test_mass_delete_guard(pool, share, settings, rclone, embedder):
    for index in range(12):
        (share / f"bulk{index}.md").write_text(f"# B{index}\n\nx\n", encoding="utf-8")
    await resync(pool, settings, rclone, embedder)
    for path in share.glob("bulk*.md"):
        path.unlink()
    (share / "notes.md").unlink()
    (share / "ja.txt").unlink()
    (share / "router.cfg").unlink()
    (share / "Design.pptx").unlink()
    with pytest.raises(MassDeleteBlocked):
        await scan_source(pool, settings, rclone, settings.sources[0])
    assert await pool.fetchval("select count(*) from doclib.files where deleted_at is not null") == 0
    await scan_source(pool, settings, rclone, settings.sources[0], allow_mass_delete=True)
    assert await pool.fetchval("select count(*) from doclib.files where deleted_at is not null") == 16


# Criterion 3: idempotency.
async def test_duplicate_scans_and_jobs_do_not_duplicate_chunks(indexed, settings, rclone, embedder):
    count = await indexed.fetchval("select count(*) from doclib.chunks")
    changes = await resync(indexed, settings, rclone, embedder)
    assert changes.total == 0
    await indexed.execute(
        "insert into doclib.jobs (kind, file_id, content_version) select 'index', file_id, content_version from doclib.files")
    await drain(indexed, settings, rclone, embedder)
    assert await indexed.fetchval("select count(*) from doclib.chunks") == count


async def test_killed_worker_loses_nothing(pool, share, settings, rclone, embed_state, tmp_path, embedder):
    """SIGKILL a real worker process mid-job, then recover through the normal lease path."""
    await scan_source(pool, settings, rclone, settings.sources[0])
    total = await pool.fetchval("select count(*) from doclib.jobs")
    embed_state["delay_s"] = 30  # Holds the worker inside a job so it can be killed there.
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(json.loads(settings.model_dump_json())), encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "doclib.cli", "--config", str(config), "worker"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if await pool.fetchval("select count(*) from doclib.jobs where status = 'running'"):
                break
            await asyncio.sleep(0.2)
        assert await pool.fetchval("select count(*) from doclib.jobs where status = 'running'") >= 1
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
    embed_state["delay_s"] = 0
    await asyncio.sleep(settings.worker.lease_s + 0.5)
    await reap(pool, settings)
    await drain(pool, settings, rclone, embedder)
    assert await pool.fetchval("select count(*) from doclib.files where status = 'indexed'") == total
    duplicates = await pool.fetchval(
        "select count(*) from (select file_id, ordinal from doclib.chunks group by 1, 2 having count(*) > 1) d")
    assert duplicates == 0
    assert await pool.fetchval("select count(*) from doclib.jobs where status in ('queued','running','dead')") == 0


async def test_infrastructure_outage_does_not_burn_attempts(pool, settings, rclone, embedder, embed_state):
    await scan_source(pool, settings, rclone, settings.sources[0])
    embed_state["down"] = True
    embedder.config.batch_size = 64
    from doclib import queue
    from doclib.worker import handle
    job = await queue.claim(pool, "t", ["index"])
    while job and job.file_id:
        row = await pool.fetchrow("select path from doclib.files where file_id = $1", job.file_id)
        if row["path"].endswith(".md"):
            break
        await queue.complete(pool, job.job_id)
        job = await queue.claim(pool, "t", ["index"])
    await handle(pool, settings, rclone, embedder, job, "t")
    row = await pool.fetchrow("select status, attempts, last_error from doclib.jobs where job_id = $1", job.job_id)
    assert row["status"] == "queued" and row["attempts"] == 0 and "Embedding" in row["last_error"]


async def test_corrupt_file_fails_and_keeps_others(pool, share, settings, rclone, embedder):
    (share / "broken.docx").write_bytes(b"this is not a docx")
    await scan_source(pool, settings, rclone, settings.sources[0])
    await pool.execute("update doclib.jobs set attempts = $1 where status = 'queued'", settings.worker.max_attempts - 1)
    await drain(pool, settings, rclone, embedder)
    row = await pool.fetchrow("select status, error from doclib.files where path = 'broken.docx'")
    assert row["status"] == "failed" and row["error"]
    assert await pool.fetchval("select count(*) from doclib.files where status = 'indexed'") == 7


# Criterion 4: citations open to the cited part of the live file.
@pytest.mark.parametrize("query,path,marker,anchor_kind", [
    ("restarting SMF pods readiness", "Vendors/Guide.pdf", "Restart SMF pods", "page"),
    ("UPF restart drains user plane", "Vendors/Guide.pdf", "drains user plane", "page"),
    ("configuration backup maintenance window", "MOPs/Upgrade.docx", "backup", "heading"),
    ("TYO1 subnet VLAN", "IP Plan.xlsx", "TYO1", "sheet"),
    ("standby UPF failover", "Design.pptx", "standby", "slide"),
    ("quokka alarms", "notes.md", "quokka", "heading"),
])
async def test_citation_opens_to_cited_part(indexed, library, query, path, marker, anchor_kind):
    results = [r for r in await search(library, query) if r["citation"]["path"] == path]
    assert results, f"{path} not found for {query!r}"
    result = results[0]
    citation = result["citation"]
    assert marker in result["text"]
    assert citation["anchor"].startswith(f"{anchor_kind}:")
    opened = await library.open_doc(TENANT, uri=citation["uri"])
    assert marker in opened["markdown"]
    assert opened["locator_found"] is True and opened["stale_index"] is False and opened["version_changed"] is False
    assert opened["content_is_untrusted"] is True


async def test_page_anchor_only_returns_that_page(indexed, library):
    file_id = await indexed.fetchval("select file_id::text from doclib.files where path = 'Vendors/Guide.pdf'")
    first = await library.open_doc(TENANT, file_id=file_id, anchor="page:1")
    second = await library.open_doc(TENANT, file_id=file_id, page_range="2")
    assert "SMF" in first["markdown"] and "UPF restart" not in first["markdown"]
    assert "UPF restart" in second["markdown"] and (second["page_start"], second["page_end"]) == (2, 2)


async def test_chunks_preserve_table_and_code_verbatim(indexed, library):
    hit, = [r for r in await search(library, "smf-2 8805") if "smf-2" in r["text"]][:1]
    assert "| smf-2 | 10.0.0.2 | 8805 |" in hit["text"]
    assert "    unit 0\n        family inet address" in hit["text"]
    assert hit["citation"]["heading_path"].endswith("4.2 Restarting SMF pods")


async def test_outline(indexed, library):
    file_id = await indexed.fetchval("select file_id::text from doclib.files where path = 'Vendors/Guide.pdf'")
    outline = await library.get_doc_outline(TENANT, file_id=file_id)
    flat = json.dumps(outline["outline"])
    assert "4.2 Restarting SMF pods" in flat and "heading:4.2" in flat
    opened = await library.open_doc(TENANT, file_id=file_id, heading="heading:4.2")
    assert "Restart SMF pods" in opened["markdown"] and "UPF restart" not in opened["markdown"]


async def test_truncation_gives_a_working_continuation(pool, share, settings, rclone, embedder, library):
    body = "\n\n".join(f"## Section {i}\n\nParagraph {i} " + "text " * 100 for i in range(10))
    (share / "long.md").write_text(f"# Long\n\n{body}", encoding="utf-8")
    await resync(pool, settings, rclone, embedder)
    opened = await library.open_doc(TENANT, source_id="ops", path="long.md", max_chars=1000)
    assert opened["truncated"] is True
    collected = opened["markdown"]
    while opened["continuation_anchor"]:
        opened = await library.open_doc(TENANT, source_id="ops", path="long.md", anchor=opened["continuation_anchor"],
                                        max_chars=1000)
        collected += opened["markdown"]
    for index in range(10):
        assert f"Paragraph {index} " in collected


# Criterion 5: stale detection, using the read-only role as in production.
async def test_stale_index_is_detected_and_reindexed(indexed, share, settings, rclone, embedder, library):
    (share / "notes.md").write_text("# Notes\n\nEdited after indexing: wombat.\n", encoding="utf-8")
    opened = await library.open_doc(TENANT, source_id="ops", path="notes.md")
    assert opened["stale_index"] is True and "wombat" in opened["markdown"]
    assert await indexed.fetchval(
        "select count(*) from doclib.jobs where status = 'queued' and kind = 'index'") == 1
    await drain(indexed, settings, rclone, embedder)
    again = await library.open_doc(TENANT, source_id="ops", path="notes.md")
    assert again["stale_index"] is False
    # The scan afterwards sees nothing new, so it doesn't index twice.
    changes = await scan_source(indexed, settings, rclone, settings.sources[0])
    assert changes.total == 0


async def test_saved_reference_with_old_version_flags_change(indexed, share, library):
    row = await indexed.fetchrow("select file_id::text, content_version from doclib.files where path = 'notes.md'")
    uri = f"doclib://ops/{row['file_id']}@{row['content_version']}#heading:on-call"
    (share / "notes.md").write_text("# Notes\n\nCompletely replaced.\n", encoding="utf-8")
    opened = await library.open_doc(TENANT, uri=uri)
    assert opened["version_changed"] is True and opened["locator_found"] is False
    assert "Completely replaced" in opened["markdown"]


async def test_file_id_survives_full_rebuild(indexed, pool, settings, rclone, embedder):
    before = await pool.fetchval("select file_id::text from doclib.files where path = 'notes.md'")
    await pool.execute("drop schema doclib cascade")
    from doclib.db import migrate
    from doclib.scanner import sync_sources
    await migrate(settings)
    await sync_sources(pool, settings)
    await resync(pool, settings, rclone, embedder)
    assert await pool.fetchval("select file_id::text from doclib.files where path = 'notes.md'") == before


# Criterion 7: read-only.
async def test_source_is_untouched_by_indexing_and_reading(pool, share, settings, rclone, embedder, library):
    before = tree_digest(share)
    await resync(pool, settings, rclone, embedder)
    await library.open_doc(TENANT, source_id="ops", path="notes.md")
    assert tree_digest(share) == before


async def test_api_role_can_only_read_and_request_reindex(indexed, settings, share, rclone, embedder):
    admin = await asyncpg.connect(DSN)
    await admin.execute("""do $$ begin
        if not exists (select from pg_roles where rolname = 'doclib_api_test') then
          create role doclib_api_test login password 'x' in role doclib_ro; end if; end $$""")
    await admin.close()
    api_dsn = DSN.replace("postgres:test@", "doclib_api_test:x@")
    readonly = await asyncpg.create_pool(api_dsn, min_size=1, max_size=4, init=_init_connection)
    try:
        library = Library(readonly, settings, rclone, embedder)
        assert await library.search_docs(TENANT, "quokka")
        for statement in ("delete from doclib.chunks", "update doclib.files set status = 'failed'",
                          "insert into doclib.jobs (kind) values ('index')", "truncate doclib.files",
                          "drop table doclib.chunks"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await readonly.execute(statement)
        (share / "notes.md").write_text("# Notes\n\nEdited: capybara.\n", encoding="utf-8")
        opened = await library.open_doc(TENANT, source_id="ops", path="notes.md")
        assert opened["stale_index"] is True
        assert await indexed.fetchval("select count(*) from doclib.jobs where status = 'queued'") == 1
    finally:
        await readonly.close()


# Search behaviour and tenancy.
async def test_filters(indexed, library):
    assert {r["citation"]["path"] for r in await search(library, "restart", filters=Filters(path_prefix="Vendors/"))} == {"Vendors/Guide.pdf"}
    assert {r["citation"]["path"] for r in await search(library, "plan site", filters=Filters(file_types=["xlsx"]))} == {"IP Plan.xlsx"}
    tables = await search(library, "subnet", filters=Filters(block_types=["table"]))
    assert tables and all("|" in r["text"] for r in tables)
    assert not await search(library, "restart", filters=Filters(source_ids=["other"]))
    assert not await search(library, "restart", filters=Filters(tags=["vendor:none"]))


async def test_keyword_search_finds_exact_identifiers_that_vectors_miss(indexed, library):
    """A direct check on the keyword path, which matters for CLI tokens and IPs."""
    where, params = _where(TENANT, Filters(), start=2)
    ranked = await library._keyword_search("10.1.0.0/24 TYO1", where, params, 10)
    assert ranked
    text = await indexed.fetchval("select text from doclib.chunks where chunk_id = $1", ranked[0])
    assert "TYO1" in text
    assert await library._keyword_search("nosuchtermanywhere", where, params, 10) == []
    assert await library._keyword_search("!!!", where, params, 10) == []  # Punctuation only: no error.


async def test_percent_in_path_prefix_is_literal(indexed, library):
    assert not await search(library, "restart", filters=Filters(path_prefix="%"))


async def test_tenant_isolation(indexed, library):
    assert await search(library, "quokka")
    assert not (await library.search_docs("other-tenant", "quokka"))["results"]
    file_id = await indexed.fetchval("select file_id::text from doclib.files where path = 'notes.md'")
    with pytest.raises(NotFound):
        await library.open_doc("other-tenant", file_id=file_id)
    with pytest.raises(NotFound):
        await library.get_doc_outline("other-tenant", file_id=file_id)
    assert (await library.list_sources("other-tenant"))["sources"] == []
    assert (await library.index_status("other-tenant"))["failed_files"] == 0


async def test_search_survives_embedding_outage(indexed, library, embed_state):
    embed_state["down"] = True
    response = await library.search_docs(TENANT, "quokka")
    assert response["degraded"] == ["vector"] and response["results"]


async def test_bad_requests(indexed, library):
    with pytest.raises(BadRequest):
        await library.search_docs(TENANT, "   ")
    with pytest.raises(BadRequest):
        await library.open_doc(TENANT)
    with pytest.raises(BadRequest):
        await library.open_doc(TENANT, uri="https://evil.example/x")


async def test_japanese_document_is_indexed(indexed, library):
    row = await indexed.fetchrow("select status from doclib.files where path = 'ja.txt'")
    assert row["status"] == "indexed"
    assert "ja.txt" in {r["citation"]["path"] for r in await search(library, "SMFポッドの再起動手順について説明します")}


# Real tesseract in the image: the scanned PDF is searchable through OCR.
async def test_scanned_pdf_becomes_searchable_through_ocr(pool, share, settings, rclone, embedder, library):
    from tests.fakes.fixtures import make_scanned_pdf
    make_scanned_pdf(share / "scan.pdf")
    await resync(pool, settings, rclone, embedder)
    row = await pool.fetchrow(
        "select c.extraction_path, c.text from doclib.chunks c join doclib.files f using (file_id) where f.path = 'scan.pdf'")
    assert row["extraction_path"] == "ocr" and "AMF" in row["text"]
    assert "scan.pdf" in await paths_for(library, "AMF pods maintenance window")


# REST and MCP over the real ASGI app.
@pytest.fixture
def api_client(settings, indexed):
    with TestClient(create_app(settings)) as client:
        yield client


def test_rest_endpoints(api_client):
    found = api_client.post("/v1/search_docs", json={"query": "quokka alarms"}).json()
    assert found["content_is_untrusted"] is True and found["results"][0]["citation"]["path"] == "notes.md"
    citation = found["results"][0]["citation"]
    opened = api_client.post("/v1/open_doc", json={"uri": citation["uri"]}).json()
    assert "quokka" in opened["markdown"]
    outline = api_client.get("/v1/get_doc_outline", params={"file_id": citation["file_id"]}).json()
    assert outline["outline"][0]["title"] == "Notes"
    sources = api_client.get("/v1/list_sources").json()["sources"]
    assert sources[0]["files_by_status"]["indexed"] == 7 and sources[0]["last_scan_ok"] is True
    status = api_client.get("/v1/index_status").json()
    assert status["failed_files"] == 0 and status["vlm"]["enabled"] is False
    assert api_client.get("/healthz").json() == {"status": "ok"}
    assert api_client.post("/v1/open_doc", json={"file_id": "00000000-0000-0000-0000-000000000000"}).status_code == 404
    assert api_client.post("/v1/search_docs", json={"query": ""}).status_code == 400


def test_no_admin_or_write_routes_exposed(api_client):
    methods = {(m, r.path) for r in api_client.app.routes for m in getattr(r, "methods", None) or []}
    writes = {(m, p) for m, p in methods if m in {"PUT", "PATCH", "DELETE"} or (m == "POST" and p.startswith("/v1/")
              and p not in {"/v1/search_docs", "/v1/open_doc"})}
    assert writes == set()


def test_mcp_tools_over_streamable_http(api_client):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def call(method, params=None, id_=1):
        return api_client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})

    init = call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "1"}})
    assert init.status_code == 200, init.text
    listed = call("tools/list").json()["result"]["tools"]
    names = {t["name"] for t in listed}
    assert names == {"search_docs", "open_doc", "get_doc_outline", "list_sources", "index_status"}
    assert not any("quokka" in json.dumps(t) for t in listed)  # Document text never reaches tool descriptions.
    result = call("tools/call", {"name": "search_docs", "arguments": {"query": "quokka alarms"}}).json()["result"]
    payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert payload["content_is_untrusted"] is True and "quokka" in payload["results"][0]["text"]
    missing = call("tools/call", {"name": "open_doc", "arguments": {"source_id": "ops", "path": "nope.md"}}).json()["result"]
    body = missing.get("structuredContent") or json.loads(missing["content"][0]["text"])
    assert body["error"]["code"] == "not_found"


def test_bearer_auth_applies_to_rest_and_mcp(share, embed_server, indexed, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("s" * 40)
    secured = build_settings(embed_server.url, default_source(share),
                             auth={"mode": "bearer", "token_secret_file": str(secret)})
    with TestClient(create_app(secured)) as client:
        assert client.post("/v1/search_docs", json={"query": "x"}).status_code == 401
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/healthz").status_code == 200
        import jwt
        token = jwt.encode({"tenant_id": "other", "exp": time.time() + 60}, "s" * 40, algorithm="HS256")
        reply = client.post("/v1/search_docs", json={"query": "quokka"}, headers={"Authorization": f"Bearer {token}"})
        assert reply.status_code == 200 and reply.json()["results"] == []


# Guide edits change the PDF; proves binary formats go through the same sync path.
async def test_pdf_edit_is_reindexed(indexed, library, share, settings, rclone, embedder):
    make_guide_pdf(share / "Vendors" / "Guide.pdf", extra="Check the pangolin counter first.")
    await resync(indexed, settings, rclone, embedder)
    assert "Vendors/Guide.pdf" in await paths_for(library, "pangolin counter")


# Pointing at a different embedding service.
async def test_changing_model_never_compares_vectors_across_models(indexed, library, settings, rclone, embedder):
    assert await search(library, "quokka alarms")
    settings.embeddings.model = "another-model"
    # Old chunks stop matching by vector, but keyword search still finds them.
    where, params = _where(TENANT, Filters(), start=2)
    assert await library._vector_search("quokka alarms", where, params, 10) == []
    found = await library.search_docs(TENANT, "quokka alarms")
    assert found["degraded"] == [] and found["results"][0]["citation"]["path"] == "notes.md"
    status = await library.index_status(TENANT)
    assert set(status["embeddings"]["chunks_by_model"]) == {"fake"} and status["embeddings"]["model"] == "another-model"


async def test_reindex_after_model_change_keeps_files_searchable(indexed, library, settings, rclone, embedder, tmp_path):
    """The swap procedure: old chunks stay available until each file's new chunks replace them."""
    settings.embeddings.model = "another-model"
    await indexed.execute("update doclib.files set status = 'pending'")
    await indexed.execute("insert into doclib.jobs (kind, file_id, content_version) "
                          "select 'index', file_id, content_version from doclib.files")
    assert (await library.search_docs(TENANT, "quokka alarms"))["results"]  # Mid-swap, still found.
    await drain(indexed, settings, rclone, embedder)
    where, params = _where(TENANT, Filters(), start=2)
    assert await library._vector_search("quokka alarms", where, params, 10)
    assert set((await library.index_status(TENANT))["embeddings"]["chunks_by_model"]) == {"another-model"}


async def test_input_the_service_refuses_fails_the_file_instead_of_waiting_forever(pool, share, settings, rclone, embedder, monkeypatch):
    from doclib import queue
    from doclib.embeddings import EmbeddingRejected
    from doclib.worker import handle

    await scan_source(pool, settings, rclone, settings.sources[0])

    async def refuse(texts):
        raise EmbeddingRejected("HTTP 413: the service refused this input")

    monkeypatch.setattr(embedder, "embed_documents", refuse)
    await pool.execute("update doclib.jobs set attempts = $1", settings.worker.max_attempts - 1)
    job = await queue.claim(pool, "t", ["index"])
    while job:
        path = await pool.fetchval("select path from doclib.files where file_id = $1", job.file_id)
        if path == "notes.md":
            break
        await queue.complete(pool, job.job_id)
        job = await queue.claim(pool, "t", ["index"])
    await handle(pool, settings, rclone, embedder, job, "t")
    row = await pool.fetchrow("select status, error from doclib.files where path = 'notes.md'")
    assert row["status"] == "failed" and "refused" in row["error"]


def test_check_command_reports_problems_clearly(settings, pool, tmp_path, embed_server):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(json.loads(settings.model_dump_json())), encoding="utf-8")
    run = lambda path: subprocess.run([sys.executable, "-m", "doclib.cli", "--config", str(path), "check"],
                                      capture_output=True, text=True, timeout=60)
    good = run(config)
    assert good.returncode == 0, good.stdout + good.stderr
    report = json.loads(good.stdout[good.stdout.index('{\n'):])
    assert report["embeddings"]["dimension"] == 1024 and report["postgres"]["embedding_column_dimension"] == 1024

    wrong = json.loads(settings.model_dump_json())
    wrong["embeddings"]["dimension"] = 768
    (tmp_path / "wrong.yaml").write_text(yaml.safe_dump(wrong), encoding="utf-8")
    bad = run(tmp_path / "wrong.yaml")
    assert bad.returncode == 1
    assert "1024" in bad.stdout and "768" in bad.stdout

    down = json.loads(settings.model_dump_json())
    down["embeddings"]["endpoint"] = "http://127.0.0.1:9/v1"
    (tmp_path / "down.yaml").write_text(yaml.safe_dump(down), encoding="utf-8")
    assert run(tmp_path / "down.yaml").returncode == 1
