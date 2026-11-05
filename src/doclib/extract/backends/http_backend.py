"""Extraction over HTTP, so a backend can run in its own container on another machine.

Contract (see doclib.extract.server for a reference implementation):
  POST {endpoint}/extract?filename=<name>                 raw file as the body -> RawDocument as JSON
  POST {endpoint}/ocr?filename=<name>&pages=1,2           raw file as the body -> {"pages": {"1": "markdown"}}
  GET  {endpoint}/info                                    -> {"name": "xberg-1.3.9"}
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ...config import ExtractionConfig
from .base import BackendError, BackendUnavailable, RawDocument, RawPage


class HttpBackend:
    def __init__(self, config: ExtractionConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        text = config.text
        if not text.endpoint:
            raise ValueError("extraction.text.endpoint is required for the http backend")
        headers = {}
        if text.api_key_file:
            headers["Authorization"] = f"Bearer {Path(text.api_key_file).read_text(encoding='utf-8').strip()}"
        self._client = httpx.AsyncClient(base_url=text.endpoint.rstrip("/"), timeout=config.timeout_s, headers=headers,
                                         transport=transport)
        self._name: str | None = None

    @property
    def name(self) -> str:
        return self._name or "http"

    async def _post(self, path: str, data: bytes, params: dict[str, str]) -> dict:
        try:
            if self._name is None:
                info = await self._client.get("/info")
                info.raise_for_status()
                self._name = f"http:{info.json().get('name', 'unknown')}"
            response = await self._client.post(path, content=data, params=params)
        except httpx.TransportError as exc:
            raise BackendUnavailable(type(exc).__name__) from exc
        except httpx.HTTPStatusError as exc:
            raise BackendUnavailable(f"HTTP {exc.response.status_code}") from exc
        if response.status_code in {502, 503, 504}:
            raise BackendUnavailable(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise BackendError(f"HTTP {response.status_code}")
        return response.json()

    async def extract(self, data: bytes, filename: str) -> RawDocument:
        body = await self._post("/extract", data, {"filename": filename})
        return RawDocument(
            mime_type=body.get("mime_type"), language=body.get("language"), title=body.get("title"),
            content=body.get("content") or "",
            pages=[RawPage(p["number"], p.get("markdown") or "", p.get("label")) for p in body.get("pages") or []],
        )

    async def ocr(self, data: bytes, filename: str, pages: list[int] | None) -> dict[int, str]:
        params = {"filename": filename}
        if pages is not None:
            params["pages"] = ",".join(str(p) for p in pages)
        body = await self._post("/ocr", data, params)
        return {int(number): text for number, text in (body.get("pages") or {}).items()}
