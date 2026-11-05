"""xberg, in-process (default). The only module that imports xberg.

use_cache=False matters: xberg's cache would otherwise write extracted content to disk (spec section 3.2).
"""

from __future__ import annotations

import asyncio
import importlib.metadata

import xberg

from ...config import ExtractionConfig
from .base import BackendError, RawDocument, RawPage


class XbergBackend:
    def __init__(self, config: ExtractionConfig) -> None:
        self.config = config
        self._name = f"xberg-{importlib.metadata.version('xberg')}"

    @property
    def name(self) -> str:
        return self._name

    @staticmethod
    def _options(**overrides) -> xberg.ExtractionConfig:
        return xberg.ExtractionConfig(
            use_cache=False,
            output_format="markdown",
            pages=xberg.PageConfig(extract_pages=True),
            language_detection=xberg.LanguageDetectionConfig(enabled=True),
            **overrides,
        )

    async def _run(self, data: bytes, filename: str, options: xberg.ExtractionConfig):
        source = xberg.ExtractInput(kind="bytes", bytes=data, filename=filename)
        result = await asyncio.wait_for(xberg.extract(source, options), self.config.timeout_s)
        if result.errors:
            raise BackendError(f"extraction reported {len(result.errors)} error(s)")
        if not result.results:
            raise BackendError("extraction returned no document")
        return result.results[0]

    async def extract(self, data: bytes, filename: str) -> RawDocument:
        doc = await self._run(data, filename, self._options(disable_ocr=True))
        languages = doc.detected_languages or []
        return RawDocument(
            mime_type=doc.mime_type,
            language=languages[0] if languages else None,
            title=getattr(doc.metadata, "title", None),
            content=doc.content or "",
            pages=[RawPage(p.page_number, p.content or "", p.sheet_name) for p in (doc.pages or [])],
        )

    async def ocr(self, data: bytes, filename: str, pages: list[int] | None) -> dict[int, str]:
        overrides = {"ocr": xberg.OcrConfig(backend=self.config.ocr_fallback, language=self.config.ocr_languages)}
        if pages is not None:
            overrides["force_ocr_pages"] = pages
        doc = await self._run(data, filename, self._options(**overrides))
        recovered = {p.page_number: p.content or "" for p in (doc.pages or [])}
        if not recovered and doc.content:
            recovered = {1: doc.content}
        return recovered
