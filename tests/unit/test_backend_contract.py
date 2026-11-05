"""The contract every text backend must meet. To check a new backend, add it to BACKENDS."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.fakes.fixtures import make_guide_pdf, make_xlsx

from doclib.config import ExtractionConfig, TextBackendConfig
from doclib.extract import backends
from doclib.extract.backends.base import BackendUnavailable, RawDocument, RawPage, TextBackend
from doclib.extract.backends.http_backend import HttpBackend
from doclib.extract.backends.xberg_backend import XbergBackend
from doclib.extract.pipeline import extract_document
from doclib.extract.server import create_extract_app


def _http_backend() -> HttpBackend:
    """The http backend talking to the reference server, which wraps xberg."""
    client = TestClient(create_extract_app(ExtractionConfig()))

    def handler(request: httpx.Request) -> httpx.Response:
        response = client.request(request.method, str(request.url), content=request.read())
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    config = ExtractionConfig(text=TextBackendConfig(backend="http", endpoint="http://testserver/v1"))
    return HttpBackend(config, transport=httpx.MockTransport(handler))


BACKENDS = {"xberg": lambda: XbergBackend(ExtractionConfig()), "http": _http_backend}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request) -> TextBackend:
    return BACKENDS[request.param]()


async def test_implements_protocol(backend):
    assert isinstance(backend, TextBackend)


async def test_pdf_returns_markdown_per_page(backend, tmp_path: Path):
    make_guide_pdf(tmp_path / "g.pdf")
    raw = await backend.extract((tmp_path / "g.pdf").read_bytes(), "g.pdf")
    assert raw.mime_type == "application/pdf"
    assert [p.number for p in raw.pages] == [1, 2]
    assert "4.2 Restarting SMF pods" in raw.pages[0].markdown
    assert "UPF" in raw.pages[1].markdown


async def test_spreadsheet_pages_are_labelled_with_sheet_names(backend, tmp_path: Path):
    make_xlsx(tmp_path / "p.xlsx")
    raw = await backend.extract((tmp_path / "p.xlsx").read_bytes(), "p.xlsx")
    assert [p.label for p in raw.pages] == ["IP Plan", "Ports"]


async def test_name_reports_backend_and_version(backend, tmp_path: Path):
    make_guide_pdf(tmp_path / "g.pdf")
    await backend.extract((tmp_path / "g.pdf").read_bytes(), "g.pdf")
    assert "xberg-" in backend.name


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown text backend"):
        backends.create_backend(ExtractionConfig(text=TextBackendConfig(backend="nope")))


class StubBackend:
    """A stand-in for a future extraction model. Selected by config, with no pipeline changes."""

    name = "stub-1"

    def __init__(self, config: ExtractionConfig) -> None:
        self.config = config

    async def extract(self, data: bytes, filename: str) -> RawDocument:
        pages = [RawPage(1, "# Stub Heading\n\n" + "stub backend body text that is long enough to stay on the fast path")]
        return RawDocument(mime_type="application/pdf", language="eng", pages=pages)

    async def ocr(self, data: bytes, filename: str, pages: list[int] | None) -> dict[int, str]:
        return {}


async def test_swapping_backend_is_config_only():
    config = ExtractionConfig(text=TextBackendConfig(backend=f"{__name__}:StubBackend"))
    doc = await extract_document(b"not parsed by the stub", "x.pdf", config)
    assert doc.extractor == "stub-1"
    assert doc.blocks[0].text == "Stub Heading"
    assert doc.unit == "page"


async def test_unreachable_remote_backend_is_transient():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    config = ExtractionConfig(text=TextBackendConfig(backend="http", endpoint="http://gone/v1"))
    backend = HttpBackend(config, transport=httpx.MockTransport(refuse))
    with pytest.raises(BackendUnavailable):
        await backend.extract(b"x", "x.pdf")


def test_reference_server_requires_key_when_configured(tmp_path: Path):
    key = tmp_path / "key"
    key.write_text("s3cret")
    client = TestClient(create_extract_app(ExtractionConfig(), api_key_file=str(key)))
    assert client.get("/v1/info").status_code == 401
    assert client.get("/v1/info", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert json.loads(client.get("/healthz").content)["status"] == "ok"
