from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from doclib.config import Settings, SourceConfig
from doclib.db import _init_connection, migrate
from doclib.embeddings import EmbeddingClient
from doclib.library import Library
from doclib.log import setup_logging
from doclib.rclone import Rclone
from doclib.scanner import scan_source, sync_sources
from doclib.worker import drain
from tests.fakes.fixtures import make_corpus
from tests.fakes.servers import ServerThread, create_embedding_app, create_webhook_app

DSN = os.environ.get("DOCLIB_TEST_DSN")
pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(items):
    if DSN:
        return
    skip = pytest.mark.skip(reason="DOCLIB_TEST_DSN is not set; run scripts/test-integration.sh")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def embed_state():
    return {}


@pytest.fixture(scope="session")
def embed_server(embed_state):
    with ServerThread(create_embedding_app(state=embed_state)) as server:
        yield server


@pytest.fixture(scope="session")
def webhook_state():
    return {"received": [], "control": {}}


@pytest.fixture(scope="session")
def webhook_server(webhook_state):
    with ServerThread(create_webhook_app(webhook_state["received"], webhook_state["control"])) as server:
        yield server


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    make_corpus(root)
    return root


def build_settings(embed_url: str, source: SourceConfig, **overrides) -> Settings:
    data = {"tenant_id": "acme", "postgres": {"dsn": DSN}, "sources": [source.model_dump()],
            "embeddings": {"endpoint": f"{embed_url}/v1", "model": "fake", "dimension": 1024},
            "worker": {"lease_s": 3, "backoff_base_s": 0.01}}
    data.update(overrides)
    return Settings.model_validate(data)


def default_source(share: Path, **kwargs) -> SourceConfig:
    return SourceConfig(id="ops", rclone_remote="local", root_path=str(share),
                        exclude=["**/~$*", "**/Archive/**"], **kwargs)


@pytest.fixture
def settings(share, embed_server) -> Settings:
    return build_settings(embed_server.url, default_source(share))


@pytest_asyncio.fixture
async def pool(settings, embed_state, webhook_state):
    setup_logging("doclib-test", "WARNING")
    embed_state.clear()
    webhook_state["received"].clear()
    webhook_state["control"].clear()
    admin = await asyncpg.connect(DSN)
    await admin.execute("drop schema if exists doclib cascade")
    await admin.close()
    await migrate(settings)
    pool = await asyncpg.create_pool(DSN, min_size=2, max_size=8, init=_init_connection)
    await sync_sources(pool, settings)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def embedder(settings):
    client = EmbeddingClient(settings.embeddings)
    yield client
    await client.aclose()


@pytest.fixture
def rclone() -> Rclone:
    return Rclone()


@pytest.fixture
def library(pool, settings, rclone, embedder) -> Library:
    return Library(pool, settings, rclone, embedder)


@pytest_asyncio.fixture
async def indexed(pool, settings, rclone, embedder):
    """The corpus, fully scanned and indexed."""
    await scan_source(pool, settings, rclone, settings.sources[0])
    await drain(pool, settings, rclone, embedder)
    return pool


async def resync(pool, settings, rclone, embedder):
    changes = await scan_source(pool, settings, rclone, settings.sources[0])
    await drain(pool, settings, rclone, embedder)
    return changes
