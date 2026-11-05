"""Webhook notifications and the background vision upgrade, with fake model servers."""

from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from doclib import notify, queue
from doclib.config import WebhookConfig
from doclib.scanner import scan_source
from doclib.vlm import VlmClient, vlm_reachable
from doclib.vlm_worker import drain as drain_vlm
from doclib.worker import drain
from tests.fakes.fixtures import make_scanned_pdf
from tests.fakes.servers import ServerThread, create_vision_app
from tests.integration.conftest import build_settings, default_source, resync

pytestmark = pytest.mark.integration


async def flush(pool, config, url, secret=None):
    await notify.compile_digests(pool, config)
    async with httpx.AsyncClient() as client:
        return await notify.deliver_due(pool, config, client, url, secret)


@pytest.fixture
def hook(webhook_server):
    return f"{webhook_server.url}/hook"


CONFIG = WebhookConfig(enabled=True, url="unused", digest_max_wait=300)


async def test_first_index_sends_nothing(indexed, hook, webhook_state):
    assert await flush(indexed, CONFIG, hook) == 0
    assert webhook_state["received"] == []


async def test_digest_covers_every_change_type_after_indexing(indexed, share, settings, rclone, embedder, hook, webhook_state):
    (share / "added.md").write_text("# A\n\nadded\n", encoding="utf-8")
    (share / "ja.txt").write_text("更新しました。\n", encoding="utf-8")
    (share / "router.cfg").unlink()
    (share / "notes.md").rename(share / "moved.md")
    await scan_source(indexed, settings, rclone, settings.sources[0])

    # Not indexed yet, so the digest waits: "new" must mean searchable.
    assert await flush(indexed, CONFIG, hook) == 0
    assert webhook_state["received"] == []

    await drain(indexed, settings, rclone, embedder)
    assert await flush(indexed, CONFIG, hook) == 1
    message, = webhook_state["received"]
    body = message["json"]
    assert body["summary"] == {"new": 1, "changed": 1, "deleted": 1, "moved": 1, "indexed": 2, "failed": 0, "skipped": 0}
    assert body["text"] == "ops: 1 new, 1 changed, 1 deleted, 1 moved"
    assert body["source_id"] == "ops" and body["tenant_id"] == "acme" and body["schema_version"] == "1.0"
    assert message["headers"]["x-doclib-event-id"] == body["event_id"]
    by_change = {c["change"]: c for c in body["changes"]}
    assert by_change["moved"]["previous_path"] == "notes.md" and by_change["moved"]["path"] == "moved.md"
    assert by_change["new"]["outcome"] == "indexed"
    # Paths and versions only: never document text.
    assert b"added\\n" not in message["body"] and "更新" not in message["body"].decode()

    assert await flush(indexed, CONFIG, hook) == 0  # Nothing is sent twice.


async def test_failures_are_reported(indexed, share, settings, rclone, embedder, hook, webhook_state):
    (share / "broken.docx").write_bytes(b"not a docx")
    await scan_source(indexed, settings, rclone, settings.sources[0])
    await indexed.execute("update doclib.jobs set attempts = $1 where status = 'queued'", settings.worker.max_attempts - 1)
    await drain(indexed, settings, rclone, embedder)
    await flush(indexed, CONFIG, hook)
    body = webhook_state["received"][0]["json"]
    assert body["summary"]["failed"] == 1 and body["changes"][0]["outcome"] == "failed" and body["changes"][0]["error"]


async def test_oversized_files_are_reported_as_skipped(pool, share, embed_server, rclone, hook, webhook_state):
    limited = build_settings(embed_server.url, default_source(share, max_file_size_mb=1))
    await scan_source(pool, limited, rclone, limited.sources[0])
    (share / "huge.pdf").write_bytes(b"0" * (2 * 1024 * 1024))
    await scan_source(pool, limited, rclone, limited.sources[0])
    row = await pool.fetchrow("select status, error from doclib.files where path = 'huge.pdf'")
    assert row["status"] == "skipped" and "max_file_size" in row["error"]
    await flush(pool, CONFIG, hook)
    assert webhook_state["received"][0]["json"]["summary"]["skipped"] == 1


async def test_retries_until_receiver_recovers(indexed, share, settings, rclone, embedder, hook, webhook_state):
    (share / "added.md").write_text("# A\n\nadded\n", encoding="utf-8")
    await resync(indexed, settings, rclone, embedder)
    webhook_state["control"]["status"] = 500
    assert await flush(indexed, CONFIG, hook) == 0
    row = await indexed.fetchrow("select status, attempts, last_error from doclib.webhook_deliveries")
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "HTTP 500")
    webhook_state["control"]["status"] = 200
    await indexed.execute("update doclib.webhook_deliveries set next_attempt_at = now()")
    assert await flush(indexed, CONFIG, hook) == 1
    ids = {m["headers"]["x-doclib-event-id"] for m in webhook_state["received"]}
    assert len(webhook_state["received"]) == 2 and len(ids) == 1  # Same event_id, so receivers can de-duplicate.


async def test_gives_up_after_max_attempts(indexed, share, settings, rclone, embedder, hook, webhook_state):
    (share / "added.md").write_text("# A\n\nadded\n", encoding="utf-8")
    await resync(indexed, settings, rclone, embedder)
    webhook_state["control"]["status"] = 500
    limited = WebhookConfig(enabled=True, url="unused", max_attempts=1)
    await flush(indexed, limited, hook)
    assert await indexed.fetchval("select status from doclib.webhook_deliveries") == "dead"


async def test_signature_verifies(indexed, share, settings, rclone, embedder, hook, webhook_state):
    (share / "added.md").write_text("# A\n\nadded\n", encoding="utf-8")
    await resync(indexed, settings, rclone, embedder)
    await flush(indexed, CONFIG, hook, secret="topsecret")
    message, = webhook_state["received"]
    timestamp = message["headers"]["x-doclib-timestamp"].encode()
    expected = "sha256=" + hmac.new(b"topsecret", timestamp + b"." + message["body"], hashlib.sha256).hexdigest()
    assert message["headers"]["x-doclib-signature"] == expected


async def test_long_scan_sends_what_is_ready_after_max_wait(indexed, share, settings, rclone, embedder, hook, webhook_state):
    (share / "one.md").write_text("# One\n\nx\n", encoding="utf-8")
    (share / "notes.md").unlink()
    await scan_source(indexed, settings, rclone, settings.sources[0])
    assert await flush(indexed, CONFIG, hook) == 0  # Deleted is ready; new is still indexing. Wait.
    assert await flush(indexed, WebhookConfig(enabled=True, url="unused", digest_max_wait=0), hook) == 1
    assert webhook_state["received"][0]["json"]["summary"]["deleted"] == 1
    await drain(indexed, settings, rclone, embedder)
    assert await flush(indexed, CONFIG, hook) == 1
    assert webhook_state["received"][1]["json"]["summary"]["new"] == 1


# Vision: the same code path for a local CPU container or a remote GPU, since only the URL differs.
@pytest.fixture
def vision_state():
    return {}


@pytest.fixture
def vision(vision_state):
    with ServerThread(create_vision_app(vision_state)) as server:
        yield server


@pytest.fixture
def vlm_settings(share, embed_server, vision):
    return build_settings(embed_server.url, default_source(share), extraction={
        "vlm": {"enabled": True, "endpoint": f"{vision.url}/v1", "model": "fake", "retries": 0}})


@pytest.fixture
async def vlm_client(vlm_settings):
    client = VlmClient(vlm_settings.extraction.vlm)
    yield client
    await client.aclose()


async def test_file_is_searchable_on_ocr_then_upgraded_by_vision(pool, share, vlm_settings, rclone, embedder, library, vlm_client, vision_state):
    make_scanned_pdf(share / "scan.pdf")
    await scan_source(pool, vlm_settings, rclone, vlm_settings.sources[0])
    await drain(pool, vlm_settings, rclone, embedder)

    # Fast path done, vision not yet: already searchable, and nothing waited on the vision model.
    assert vision_state.get("calls", 0) == 0
    assert await pool.fetchval(
        "select c.extraction_path from doclib.chunks c join doclib.files f using (file_id) where f.path = 'scan.pdf'") == "ocr"
    job = await pool.fetchrow("select kind, payload, priority from doclib.jobs where status = 'queued'")
    assert job["kind"] == "vlm_page" and job["payload"] == {"pages": [1]} and job["priority"] == 500

    assert await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client) == 1
    assert vision_state["calls"] == 1
    row = await pool.fetchrow(
        "select c.extraction_path, c.quality, c.text, c.heading_path, f.status, f.title from doclib.chunks c "
        "join doclib.files f using (file_id) where f.path = 'scan.pdf'")
    assert row["extraction_path"] == "vlm" and row["quality"] == "ok" and "visionmarker" in row["text"]
    assert row["heading_path"] == "Vision Title" and row["status"] == "indexed"
    results = (await library.search_docs("acme", "visionmarker"))["results"]
    assert results[0]["extraction_path"] == "vlm"


async def test_born_digital_files_never_call_the_vision_model(indexed, vlm_settings, rclone, embedder, vision_state, vlm_client):
    assert await drain_vlm(indexed, vlm_settings, rclone, embedder, vlm_client) == 0
    assert vision_state.get("calls", 0) == 0


async def test_gpu_offline_jobs_wait_and_search_keeps_working(pool, share, vlm_settings, rclone, embedder, library, vlm_client, vision_state):
    make_scanned_pdf(share / "scan.pdf")
    await scan_source(pool, vlm_settings, rclone, vlm_settings.sources[0])
    await drain(pool, vlm_settings, rclone, embedder)
    vision_state["down"] = True
    await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client)
    job = await pool.fetchrow("select status, attempts from doclib.jobs where kind = 'vlm_page'")
    assert job["status"] == "queued" and job["attempts"] == 0  # Waits as long as needed; no attempts used.
    assert (await library.search_docs("acme", "AMF pods"))["results"]

    vision_state["down"] = False
    await pool.execute("update doclib.jobs set run_after = now() where kind = 'vlm_page'")
    await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client)
    assert await pool.fetchval("select extraction_path from doclib.chunks c join doclib.files f using (file_id) "
                               "where f.path = 'scan.pdf'") == "vlm"


async def test_unreachable_endpoint_counts_as_waiting(pool, share, embed_server, rclone, embedder):
    settings = build_settings(embed_server.url, default_source(share), extraction={
        "vlm": {"enabled": True, "endpoint": "http://127.0.0.1:9/v1", "model": "fake", "retries": 0}})
    make_scanned_pdf(share / "scan.pdf")
    await scan_source(pool, settings, rclone, settings.sources[0])
    await drain(pool, settings, rclone, embedder)
    client = VlmClient(settings.extraction.vlm)
    await drain_vlm(pool, settings, rclone, embedder, client)
    await client.aclose()
    assert await pool.fetchval("select attempts from doclib.jobs where kind = 'vlm_page'") == 0
    assert await vlm_reachable(settings.extraction.vlm) is False


async def test_vision_job_for_old_version_is_discarded(pool, share, vlm_settings, rclone, embedder, vlm_client, vision_state):
    make_scanned_pdf(share / "scan.pdf")
    await scan_source(pool, vlm_settings, rclone, vlm_settings.sources[0])
    await drain(pool, vlm_settings, rclone, embedder)
    make_scanned_pdf(share / "scan.pdf", ("A different scanned page entirely",))
    await scan_source(pool, vlm_settings, rclone, vlm_settings.sources[0])
    job = await queue.claim(pool, "t", ["vlm_page"])
    assert job is not None
    await pool.execute("update doclib.jobs set status = 'queued', locked_by = null where job_id = $1", job.job_id)
    await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client)
    assert vision_state.get("calls", 0) == 0


async def test_page_level_failure_keeps_ocr_and_flags_degraded(pool, share, vlm_settings, rclone, embedder, vlm_client, vision_state):
    make_scanned_pdf(share / "scan.pdf")
    await scan_source(pool, vlm_settings, rclone, vlm_settings.sources[0])
    await drain(pool, vlm_settings, rclone, embedder)
    vision_state["reply"] = ""  # The endpoint answers but returns nothing usable.
    await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client)
    row = await pool.fetchrow(
        "select c.extraction_path, c.quality from doclib.chunks c join doclib.files f using (file_id) where f.path = 'scan.pdf'")
    assert (row["extraction_path"], row["quality"]) == ("ocr", "degraded")


async def test_page_that_keeps_crashing_the_server_eventually_gives_up(pool, share, vlm_settings, rclone, embedder, vlm_client, monkeypatch):
    """A lost connection uses up attempts, so one bad page can't block the queue forever."""
    from doclib.vlm import VlmConnectionLost

    make_scanned_pdf(share / "scan.pdf")
    await scan_source(pool, vlm_settings, rclone, vlm_settings.sources[0])
    await drain(pool, vlm_settings, rclone, embedder)

    async def crash(*args, **kwargs):
        raise VlmConnectionLost("RemoteProtocolError")

    monkeypatch.setattr(vlm_client, "page_to_markdown", crash)
    await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client)
    job = await pool.fetchrow("select status, attempts from doclib.jobs where kind = 'vlm_page'")
    assert job["status"] == "queued" and job["attempts"] == 1  # It counted, unlike an offline endpoint.

    await pool.execute("update doclib.jobs set run_after = now(), attempts = $1 where kind = 'vlm_page'",
                       vlm_settings.worker.max_attempts - 1)
    await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client)
    assert await pool.fetchval("select status from doclib.jobs where kind = 'vlm_page'") == "dead"
    row = await pool.fetchrow(
        "select c.extraction_path, c.quality, f.status from doclib.chunks c join doclib.files f using (file_id) "
        "where f.path = 'scan.pdf'")
    assert (row["extraction_path"], row["quality"], row["status"]) == ("ocr", "degraded", "indexed")  # Still searchable.


async def test_vision_is_a_per_page_fallback_after_text_extraction(pool, share, vlm_settings, rclone, embedder, vlm_client, vision_state):
    """Only the page text extraction couldn't handle goes to the vision model."""
    from tests.fakes.fixtures import make_mixed_pdf

    make_mixed_pdf(share / "mixed.pdf")
    await scan_source(pool, vlm_settings, rclone, vlm_settings.sources[0])
    await drain(pool, vlm_settings, rclone, embedder)
    job = await pool.fetchrow("select payload from doclib.jobs where kind = 'vlm_page' and status = 'queued'")
    assert job["payload"] == {"pages": [2]}  # Page 1 had a good text layer. Other corpus files queued nothing.
    assert await pool.fetchval("select count(*) from doclib.jobs where kind = 'vlm_page'") == 1

    assert await drain_vlm(pool, vlm_settings, rclone, embedder, vlm_client) == 1
    assert vision_state["calls"] == 1  # One page, not the whole document.
    rows = await pool.fetch(
        "select c.extraction_path, c.page_start, c.page_end, c.text from doclib.chunks c join doclib.files f using (file_id) "
        "where f.path = 'mixed.pdf' order by c.ordinal")
    native = "\n".join(r["text"] for r in rows)
    assert "Native text layer line 0" in native  # The native page survives untouched.
    assert "visionmarker" in native


async def test_vision_cannot_overwrite_good_text_with_less(tmp_path):
    """A page routed for its large image, but with a full text layer, keeps the longer native text."""
    from doclib.config import ExtractionConfig, RouterConfig
    from doclib.extract.pipeline import extract_document
    from tests.fakes.fixtures import make_guide_pdf

    make_guide_pdf(tmp_path / "g.pdf")
    data = (tmp_path / "g.pdf").read_bytes()
    forced = ExtractionConfig(router=RouterConfig(min_chars_per_page=10000))  # Treats every page as hard.
    kept = await extract_document(data, "g.pdf", forced, vlm_pages={1: "tiny", 2: "tiny"})
    assert kept.pages[0].extraction_path != "vlm" and "Restart SMF pods" in "\n".join(b.text for b in kept.blocks)
    long_text = "Vision text. " * 200
    used = await extract_document(data, "g.pdf", forced, vlm_pages={1: long_text, 2: long_text})
    assert used.pages[0].extraction_path == "vlm"
