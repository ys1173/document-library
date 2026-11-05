"""Vision model client (spec section 6.3). Calls any OpenAI-compatible chat completions endpoint.

Where the model runs (local CPU container, remote GPU, vLLM) is only a matter of the configured URL. Page
images are built in memory and sent in the request body; nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import base64
import re
import struct
import threading
import zlib
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pypdfium2

from .config import VlmConfig
from .log import event

_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)
_HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "tif": "image/tiff", "tiff": "image/tiff",
                "webp": "image/webp", "bmp": "image/bmp"}
_render_lock = threading.Lock()


class VlmUnavailable(Exception):
    """The endpoint can't be reached (for example, the remote GPU is off). Wait; this isn't the page's fault."""


class VlmConnectionLost(Exception):
    """The connection dropped mid-request, so the server may have crashed on this page. This counts against the
    job's attempts: a page that always kills the server must eventually give up rather than retry forever."""


class VlmPageError(Exception):
    """This page failed even though the endpoint answered."""


def encode_png(width: int, height: int, rgb: bytes, stride: int) -> bytes:
    """Minimal RGB PNG encoder, so the shipped path needs no imaging library."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))
    raw = b"".join(b"\x00" + rgb[y * stride:y * stride + width * 3] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def render_pdf_pages(data: bytes, pages: list[int], dpi: int) -> dict[int, bytes]:
    """Render 1-based page numbers to PNG bytes in memory."""
    images: dict[int, bytes] = {}
    with _render_lock:  # pdfium is not thread-safe.
        pdf = pypdfium2.PdfDocument(data)
        try:
            for number in pages:
                if not 1 <= number <= len(pdf):
                    continue
                page = pdf[number - 1]
                try:
                    bitmap = page.render(scale=dpi / 72, rev_byteorder=True)
                    if bitmap.n_channels != 3:  # Pages with transparency render as RGBA or similar.
                        pixels = bytes(bitmap.buffer)
                        step = bitmap.n_channels
                        rgb = bytearray()
                        for y in range(bitmap.height):
                            row = pixels[y * bitmap.stride:y * bitmap.stride + bitmap.width * step]
                            if step == 1:
                                rgb += bytes(v for g in row for v in (g, g, g))
                            else:
                                for x in range(0, len(row), step):
                                    rgb += row[x:x + 3]
                        images[number] = encode_png(bitmap.width, bitmap.height, bytes(rgb), bitmap.width * 3)
                    else:
                        images[number] = encode_png(bitmap.width, bitmap.height, bytes(bitmap.buffer), bitmap.stride)
                finally:
                    page.close()
        finally:
            pdf.close()
    return images


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in {"td", "th"}:
            self._cell = []
            value = dict(attrs).get("colspan") or "1"
            self._span = int(value) if value.isdigit() else 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            if not self.rows:
                self.rows.append([])
            text = " ".join("".join(self._cell).split()).replace("|", "\\|")
            self.rows[-1].extend([text] + [""] * (self._span - 1))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_markdown(html: str) -> str:
    parser = _TableParser()
    parser.feed(html)
    rows = [r for r in parser.rows if r]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def parse_response(text: str) -> str:
    """Normalise model output to Markdown: drop a wrapping code fence, and convert HTML tables."""
    match = _FENCE.match(text)
    body = match.group(1) if match else text
    return _HTML_TABLE.sub(lambda m: f"\n\n{html_table_to_markdown(m.group(0))}\n\n", body).strip()


def media_type_for(filename: str) -> str:
    return _MEDIA_TYPES.get(filename.rsplit(".", 1)[-1].lower(), "image/png")


def _headers(config: VlmConfig) -> dict[str, str]:
    if config.api_key_file:
        return {"Authorization": f"Bearer {Path(config.api_key_file).read_text(encoding='utf-8').strip()}"}
    return {}


async def vlm_reachable(config: VlmConfig) -> bool:
    if not config.endpoint:
        return False
    try:
        async with httpx.AsyncClient(timeout=3, headers=_headers(config)) as client:
            return (await client.get(config.endpoint.rstrip("/") + "/models")).status_code == 200
    except httpx.HTTPError:
        return False


class VlmClient:
    def __init__(self, config: VlmConfig) -> None:
        if not config.endpoint:
            raise ValueError("extraction.vlm.endpoint is required when the vision model is enabled")
        self.config = config
        self._client = httpx.AsyncClient(base_url=config.endpoint.rstrip("/"), timeout=config.timeout_s, headers=_headers(config))
        self._limit = asyncio.Semaphore(max(1, config.concurrency))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def page_to_markdown(self, image: bytes, media_type: str = "image/png") -> str:
        url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
        body = {"model": self.config.model, "max_tokens": self.config.max_tokens, "temperature": 0, "messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}},
                                         {"type": "text", "text": self.config.prompt}]}]}
        last: str = "no attempt"
        for attempt in range(self.config.retries + 1):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            try:
                async with self._limit:
                    response = await self._client.post("/chat/completions", json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise VlmUnavailable(type(exc).__name__) from exc
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as exc:
                raise VlmConnectionLost(type(exc).__name__) from exc
            except httpx.HTTPError as exc:
                last = type(exc).__name__  # For example a read timeout: this page took too long.
                continue
            if response.status_code in {502, 503, 504}:
                raise VlmUnavailable(f"HTTP {response.status_code}")
            if response.status_code != 200:
                last = f"HTTP {response.status_code}"
                continue
            try:
                return parse_response(response.json()["choices"][0]["message"]["content"] or "")
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last = f"bad response: {type(exc).__name__}"
        raise VlmPageError(last)

    async def pages_to_markdown(self, images: dict[int, bytes], media_type: str = "image/png") -> tuple[dict[int, str], set[int]]:
        """Returns Markdown by page and the pages that failed. Raises VlmUnavailable if the endpoint is down."""
        async def one(number: int, image: bytes) -> tuple[int, str | None]:
            try:
                text = await self.page_to_markdown(image, media_type)
                return number, text if text.strip() else None
            except VlmPageError as exc:
                event("vlm.page_failed", level=30, page=number, error=str(exc))
                return number, None

        results = await asyncio.gather(*(one(n, img) for n, img in sorted(images.items())))
        done = {n: text for n, text in results if text is not None}
        return done, {n for n, text in results if text is None}
