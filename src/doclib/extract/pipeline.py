"""Extraction orchestration: bytes in, Document out. Independent of which text backend is configured.

Files are handled in memory, so there is nothing to clean up on disk. Config, log and source files are read
directly rather than through a backend: that keeps them verbatim (spec section 6.1), and xberg would otherwise
download tree-sitter grammars at runtime for them, which breaks air-gapped installs.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import PurePosixPath

import pypdfium2
import pypdfium2.raw as pdfium_c

from .. import telemetry
from ..config import ExtractionConfig
from ..log import event
from .backends import get_backend
from .backends.base import BackendError as ExtractionError
from .backends.base import BackendUnavailable
from .markdown import parse_markdown
from .model import Block, Document, Page
from .router import PageSignals, route, signals_for

VERBATIM_EXTENSIONS = frozenset({
    "cfg", "conf", "ini", "log", "json", "yaml", "yml", "xml", "toml", "properties", "sh", "bash", "py", "tf", "sql",
})  # fmt: skip
TEXT_EXTENSIONS = frozenset({"txt", "text"})
MARKDOWN_EXTENSIONS = frozenset({"md", "markdown"})
IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"})
DIRECT_READER = "direct"
# Keep native text unless OCR recovers at least this share of it.
MIN_OCR_TEXT_RATIO = 0.5
_pdfium_lock = threading.Lock()

__all__ = ["IMAGE_EXTENSIONS", "BackendUnavailable", "ExtractionError", "decode_text", "extract_document"]


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932", "euc_jp"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _suffix(filename: str) -> str:
    return PurePosixPath(filename).suffix.lstrip(".").lower()


def _unit_for(mime: str | None, suffix: str) -> str:
    mime = mime or ""
    if mime == "application/pdf" or suffix == "pdf":
        return "page"
    if "presentation" in mime or "powerpoint" in mime or suffix in {"pptx", "ppt", "odp"}:
        return "slide"
    if "spreadsheet" in mime or "excel" in mime or suffix in {"xlsx", "xls", "ods"}:
        return "sheet"
    return "none"


def pdf_image_ratios(data: bytes) -> dict[int, float]:
    """Share of each PDF page covered by images, by page number. Empty if the PDF can't be parsed here."""
    ratios: dict[int, float] = {}
    with _pdfium_lock:  # pdfium is not thread-safe.
        try:
            pdf = pypdfium2.PdfDocument(data)
        except pypdfium2.PdfiumError:
            return ratios
        try:
            for number in range(len(pdf)):
                page = pdf[number]
                try:
                    width, height = page.get_size()
                    total = 0.0
                    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
                        left, bottom, right, top = obj.get_bounds()
                        total += max(0.0, right - left) * max(0.0, top - bottom)
                    ratios[number + 1] = min(1.0, total / (width * height)) if width and height else 0.0
                finally:
                    page.close()
        finally:
            pdf.close()
    return ratios


def _title(blocks: list[Block], metadata_title: str | None) -> str | None:
    if metadata_title and metadata_title.strip():
        return metadata_title.strip()
    for block in blocks:
        if block.type == "heading":
            return block.text
    return None


async def extract_document(data: bytes, filename: str, config: ExtractionConfig,
                           vlm_pages: dict[int, str] | None = None, vlm_failed: set[int] | None = None) -> Document:
    """`vlm_pages` holds vision model Markdown by page, used instead of OCR for those pages.

    `vlm_failed` pages fall back to OCR and are flagged degraded (spec section 6.3).
    """
    vlm_pages = vlm_pages or {}
    vlm_failed = vlm_failed or set()
    suffix = _suffix(filename)

    if suffix in VERBATIM_EXTENSIONS:
        return Document(unit="none", pages=[Page(None, [Block("code", decode_text(data))])], mime_type="text/plain",
                        extractor=DIRECT_READER)
    if suffix in TEXT_EXTENSIONS:
        paragraphs = [p.strip("\n") for p in decode_text(data).replace("\r\n", "\n").split("\n\n")]
        blocks = [Block("paragraph", p) for p in paragraphs if p.strip()]
        return Document(unit="none", pages=[Page(None, blocks)], mime_type="text/plain", extractor=DIRECT_READER)
    if suffix in MARKDOWN_EXTENSIONS:
        blocks = parse_markdown(decode_text(data))
        return Document(unit="none", pages=[Page(None, blocks)], mime_type="text/markdown",
                        title=_title(blocks, None), extractor=DIRECT_READER)

    backend = get_backend(config)
    if suffix in IMAGE_EXTENSIONS:
        reasons = route(PageSignals(text="", is_image_file=True), config.router)
        if 1 in vlm_pages:
            page = Page(1, parse_markdown(vlm_pages[1]), extraction_path="vlm", route_reasons=reasons)
        else:
            recovered = await backend.ocr(data, filename, None)
            page = Page(1, parse_markdown(recovered.get(1, "")), extraction_path="ocr", route_reasons=reasons)
            if not page.blocks or 1 in vlm_failed:
                page.quality = "degraded"
        telemetry.pages_by_path.add(1, {"path": page.extraction_path})
        return Document(unit="page", pages=[page], mime_type=f"image/{suffix}", hard_pages=[1], extractor=backend.name)

    raw = await backend.extract(data, filename)
    unit = _unit_for(raw.mime_type, suffix)
    if unit == "none" or not raw.pages:
        blocks = parse_markdown(raw.content)
        telemetry.pages_by_path.add(1, {"path": "fast"})
        return Document(unit="none", pages=[Page(None, blocks)], mime_type=raw.mime_type, language=raw.language,
                        title=_title(blocks, raw.title), extractor=backend.name)

    pages = [Page(p.number, parse_markdown(p.markdown), label=p.label if unit == "sheet" else None) for p in raw.pages]
    if unit == "page":
        await _route_pdf_pages(backend, data, filename, pages, config, vlm_pages, vlm_failed)
    counts: dict[str, int] = {}
    for page in pages:
        counts[page.extraction_path] = counts.get(page.extraction_path, 0) + 1
    for path, count in counts.items():
        telemetry.pages_by_path.add(count, {"path": path})
    return Document(unit=unit, pages=pages, mime_type=raw.mime_type, language=raw.language,
                    title=_title([b for p in pages for b in p.blocks], raw.title),
                    hard_pages=[p.number for p in pages if p.route_reasons], extractor=backend.name)


async def _route_pdf_pages(backend, data: bytes, filename: str, pages: list[Page], config: ExtractionConfig,
                           vlm_pages: dict[int, str], vlm_failed: set[int]) -> None:
    ratios = await asyncio.to_thread(pdf_image_ratios, data)
    hard: dict[int, Page] = {}
    for page in pages:
        page.route_reasons = route(signals_for(page.blocks, ratios.get(page.number)), config.router)
        if page.number in vlm_pages:
            # Vision is a fallback: it must not overwrite good native text with something shorter, such as on a
            # page that was routed for its large image but already has a full text layer.
            vision_blocks = parse_markdown(vlm_pages[page.number])
            native_len = sum(len(b.text) for b in page.blocks)
            if sum(len(b.text) for b in vision_blocks) >= MIN_OCR_TEXT_RATIO * native_len:
                page.blocks = vision_blocks
                page.extraction_path = "vlm"
            else:
                event("extract.vision_discarded", page=page.number, native_chars=native_len)
        elif page.route_reasons:
            hard[page.number] = page
    if not hard:
        return
    event("extract.pages_routed", hard_pages=len(hard), total_pages=len(pages),
          reasons=sorted({r for p in hard.values() for r in p.route_reasons}))
    # OCR gives searchable text straight away. The vision model upgrades these pages in the background.
    try:
        recovered = await backend.ocr(data, filename, sorted(hard))
    except BackendUnavailable:
        raise  # A remote backend is down: retry the job rather than store degraded text.
    except Exception as exc:  # noqa: BLE001 - keep the native text and flag it rather than lose the file
        event("extract.ocr_failed", level=30, error=type(exc).__name__, pages=sorted(hard))
        for page in hard.values():
            page.quality = "degraded"
        return
    for number, page in hard.items():
        native_len = sum(len(b.text) for b in page.blocks)
        text = recovered.get(number, "")
        if len(text.strip()) >= max(1, MIN_OCR_TEXT_RATIO * native_len):
            page.blocks = parse_markdown(text)
            page.extraction_path = "ocr"
        else:
            page.quality = "degraded"
        if number in vlm_failed:
            page.quality = "degraded"
