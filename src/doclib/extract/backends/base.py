"""The contract every text extraction backend implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class BackendError(Exception):
    """The file could not be extracted. Counts against the job's attempts."""


class BackendUnavailable(Exception):
    """A remote backend can't be reached. Not the file's fault, so the job waits without using attempts."""


@dataclass
class RawPage:
    number: int
    markdown: str
    label: str | None = None  # sheet name, for spreadsheets


@dataclass
class RawDocument:
    """Backend output. `pages` is empty for formats with no real pages; `content` then holds everything."""

    mime_type: str | None = None
    language: str | None = None
    title: str | None = None
    content: str = ""
    pages: list[RawPage] = field(default_factory=list)


@runtime_checkable
class TextBackend(Protocol):
    @property
    def name(self) -> str:
        """Name and version, recorded on each file (for example 'xberg-1.3.9') to show what needs re-indexing."""
        ...

    async def extract(self, data: bytes, filename: str) -> RawDocument:
        """Extract text as Markdown, without OCR. Must not write document content to disk or call the internet."""
        ...

    async def ocr(self, data: bytes, filename: str, pages: list[int] | None) -> dict[int, str]:
        """OCR the 1-based pages (or the whole image file if None). Returns Markdown by page number."""
        ...
