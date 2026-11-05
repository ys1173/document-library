"""Against a real Samba server, through rclone's SMB backend, with a share that refuses writes."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from doclib.config import SourceConfig
from doclib.embeddings import EmbeddingClient
from doclib.library import Library
from doclib.rclone import Rclone
from doclib.scanner import scan_source, sync_sources
from doclib.worker import drain
from tests.fakes.fixtures import make_corpus
from tests.integration.conftest import build_settings

HOST = os.environ.get("DOCLIB_TEST_SMB_HOST")
SHARE_DIR = os.environ.get("DOCLIB_TEST_SMB_DIR")
pytestmark = [pytest.mark.integration, pytest.mark.samba, pytest.mark.skipif(not HOST, reason="no Samba container")]


@pytest.fixture
def smb_env(monkeypatch):
    password = subprocess.run(["rclone", "obscure", "readerpass"], capture_output=True, text=True, check=True).stdout.strip()
    for key, value in {"TYPE": "smb", "HOST": HOST, "USER": "reader", "PASS": password}.items():
        monkeypatch.setenv(f"RCLONE_CONFIG_SMBOPS_{key}", value)


@pytest.fixture
def smb_share(smb_env) -> Path:
    root = Path(SHARE_DIR)
    for child in root.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    make_corpus(root)
    return root


@pytest.fixture
def smb_settings(smb_share, embed_server):
    source = SourceConfig(id="ops", rclone_remote="smbops", root_path="ops", exclude=["**/~$*", "**/Archive/**"])
    return build_settings(embed_server.url, source)


@pytest_asyncio.fixture
async def smb_pool(smb_settings, pool):
    await pool.execute("delete from doclib.sources")
    await sync_sources(pool, smb_settings)
    return pool


async def _resync(pool, settings, rclone, embedder):
    changes = await scan_source(pool, settings, rclone, settings.sources[0])
    await drain(pool, settings, rclone, embedder)
    return changes


async def test_full_cycle_over_smb(smb_pool, smb_settings, smb_share):
    rclone = Rclone()
    embedder = EmbeddingClient(smb_settings.embeddings)
    library = Library(smb_pool, smb_settings, rclone, embedder)
    try:
        await _resync(smb_pool, smb_settings, rclone, embedder)
        statuses = {r["status"] for r in await smb_pool.fetch("select status from doclib.files")}
        assert statuses == {"indexed"}
        assert await smb_pool.fetchval("select count(*) from doclib.files") == 7
        # SMB has no hashes, so versions fall back to size+modtime.
        assert await smb_pool.fetchval("select count(*) from doclib.files where content_version like 'sm:%'") == 7

        results = (await library.search_docs("acme", "restarting SMF pods"))["results"]
        hit = next(r for r in results if r["citation"]["path"] == "Vendors/Guide.pdf")
        opened = await library.open_doc("acme", uri=hit["citation"]["uri"])
        assert "Restart SMF pods" in opened["markdown"] and opened["stale_index"] is False

        # Sync: add, edit, rename, delete, move to excluded.
        (smb_share / "added.md").write_text("# A\n\nThe okapi procedure.\n", encoding="utf-8")
        await asyncio.sleep(1.1)
        (smb_share / "ja.txt").write_text("編集しました okapi2。\n", encoding="utf-8")
        (smb_share / "router.cfg").rename(smb_share / "router-renamed.cfg")
        (smb_share / "Design.pptx").unlink()
        shutil.move(smb_share / "notes.md", smb_share / "Archive" / "notes.md")
        changes = await _resync(smb_pool, smb_settings, rclone, embedder)
        assert [e.path for e in changes.new] == ["added.md"]
        assert [k.path for k, _ in changes.changed] == ["ja.txt"]
        assert [(k.path, e.path) for k, e in changes.moved] == [("router.cfg", "router-renamed.cfg")]
        assert sorted(k.path for k in changes.deleted) == ["Design.pptx", "notes.md"]
        assert {r["citation"]["path"] for r in (await library.search_docs("acme", "okapi procedure"))["results"]} >= {"added.md"}
        gone = (await library.search_docs("acme", "quokka alarms"))["results"]
        assert not [r for r in gone if "quokka" in r["text"]]

        # Stale detection over SMB.
        await asyncio.sleep(1.1)
        (smb_share / "added.md").write_text("# A\n\nEdited: the tapir procedure.\n", encoding="utf-8")
        opened = await library.open_doc("acme", source_id="ops", path="added.md")
        assert opened["stale_index"] is True and "tapir" in opened["markdown"]
    finally:
        await embedder.aclose()


async def test_credentials_cannot_write(smb_share):
    """Criterion 7: the source refuses writes, even when bypassing doclib's own allowlist."""
    def attempt(*args):
        return subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=60)

    assert attempt("lsf", "smbops:ops").returncode == 0
    assert attempt("touch", "smbops:ops/attack.txt").returncode != 0
    assert attempt("deletefile", "smbops:ops/notes.md").returncode != 0
    assert not (smb_share / "attack.txt").exists()
    assert (smb_share / "notes.md").exists()
