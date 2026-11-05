"""Normalised document model shared by all extraction paths (spec section 6.4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

UNIT_ANCHOR = {"page": "page", "sheet": "sheet", "slide": "slide"}


@dataclass
class Block:
    type: str  # heading | paragraph | list | table | code | figure | caption
    text: str
    level: int | None = None
    language: str | None = None
    # Filled in when the block is placed in a document.
    page: int | None = None
    label: str | None = None  # sheet name
    extraction_path: str = "fast"  # fast | vlm | ocr
    quality: str = "ok"  # ok | degraded
    index: int = -1

    def to_markdown(self) -> str:
        if self.type == "heading":
            return f"{'#' * (self.level or 1)} {self.text}"
        if self.type == "code":
            longest = max((len(m) for m in re.findall(r"`{3,}", self.text)), default=2)
            fence = "`" * (longest + 1)
            return f"{fence}{self.language or ''}\n{self.text}\n{fence}"
        return self.text


@dataclass
class Page:
    number: int | None
    blocks: list[Block]
    label: str | None = None
    extraction_path: str = "fast"
    quality: str = "ok"
    route_reasons: list[str] = field(default_factory=list)


@dataclass
class Document:
    unit: str  # page | sheet | slide | none
    pages: list[Page]
    mime_type: str | None = None
    language: str | None = None
    title: str | None = None
    hard_pages: list[int] = field(default_factory=list)  # pages the vision model should upgrade
    extractor: str | None = None  # the backend that produced this, for example 'xberg-1.3.9'

    def __post_init__(self) -> None:
        index = 0
        for page in self.pages:
            for block in page.blocks:
                block.page = page.number
                block.label = page.label
                block.extraction_path = page.extraction_path
                block.quality = page.quality
                block.index = index
                index += 1

    @property
    def blocks(self) -> list[Block]:
        return [b for page in self.pages for b in page.blocks]


@dataclass
class Section:
    anchor: str
    title: str
    level: int
    start: int  # index of the heading block
    end: int  # exclusive index of the block that ends this section
    page_start: int | None
    page_end: int | None


def slug(text: str) -> str:
    return re.sub(r"[^\w.]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")[:80] or "section"


def heading_key(title: str) -> str:
    """'4.2 Restarting SMF pods' -> '4.2'. Other headings use a slug of their text."""
    match = re.match(r"^(?:section\s+|chapter\s+)?(\d+(?:\.\d+)*)\.?(?:\s|$)", title.strip(), re.IGNORECASE)
    return match.group(1) if match else slug(title)


def sections(blocks: list[Block]) -> list[Section]:
    """Every heading with its extent. Anchors are deterministic for a given content version."""
    headings = [b for b in blocks if b.type == "heading"]
    used: dict[str, int] = {}
    out: list[Section] = []
    position = {b.index: i for i, b in enumerate(blocks)}
    for i, heading in enumerate(headings):
        key = heading_key(heading.text)
        used[key] = used.get(key, 0) + 1
        anchor = f"heading:{key}" if used[key] == 1 else f"heading:{key}~{used[key]}"
        level = heading.level or 1
        end = len(blocks)
        for later in headings[i + 1:]:
            if (later.level or 1) <= level:
                end = position[later.index]
                break
        pages = [b.page for b in blocks[position[heading.index]:end] if b.page is not None]
        out.append(Section(anchor, heading.text, level, position[heading.index], end,
                           min(pages) if pages else None, max(pages) if pages else None))
    return out


def outline(doc: Document) -> list[dict]:
    """Heading tree with anchors and page ranges (spec sections 6.4 and 10.3)."""
    roots: list[dict] = []
    stack: list[tuple[int, dict]] = []
    for section in sections(doc.blocks):
        node = {"title": section.title, "level": section.level, "anchor": section.anchor,
                "page_start": section.page_start, "page_end": section.page_end, "children": []}
        while stack and stack[-1][0] >= section.level:
            stack.pop()
        (stack[-1][1]["children"] if stack else roots).append(node)
        stack.append((section.level, node))
    return roots
