"""Structure-aware chunking (spec section 7). Pure, so it is easy to test."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .config import ChunkingConfig
from .extract.markdown import table_rows
from .extract.model import Block, Document, Section, sections

LARGE_SHEET_ROWS = 5000
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")
_WORD = re.compile(r"[^\s぀-ヿ㐀-䶿一-鿿가-힯＀-￯]+")
_SENTENCE = re.compile(r"(?<=[.!?。！？])\s*")


def estimate_tokens(text: str) -> int:
    """Offline estimate. CJK is about one token per character, other text about 1.3 per word."""
    return len(_CJK.findall(text)) + round(len(_WORD.findall(text)) * 1.3)


@dataclass
class Chunk:
    chunk_id: str
    ordinal: int
    anchor: str
    heading_path: str | None
    page_start: int | None
    page_end: int | None
    block_types: list[str]
    extraction_path: str
    quality: str
    text: str
    token_count: int

    @property
    def embedding_text(self) -> str:
        return f"{self.heading_path}\n\n{self.text}" if self.heading_path else self.text


def doc_label(path: str, title: str | None) -> str:
    """Words from a document's name, for keyword search: 'Vendors/Acme_SMF-v3.pdf' -> 'Vendors Acme SMF v3 pdf'."""
    return " ".join(re.sub(r"[/\\_.\-]+", " ", f"{path} {title or ''}").split())


def chunk_id(file_id: str, version: str, ordinal: int) -> str:
    return hashlib.sha256(f"{file_id}\x00{version}\x00{ordinal}".encode()).hexdigest()[:40]


@dataclass
class _Piece:
    """A unit that may not be split further, with the block it came from."""

    text: str
    block: Block
    tokens: int
    splittable_prose: bool = False
    standalone: bool = False  # Part of a split table or code block: never merged with its neighbours.


@dataclass
class _Group:
    pieces: list[_Piece] = field(default_factory=list)
    tokens: int = 0
    overlap: str = ""


def _split_prose(block: Block, config: ChunkingConfig) -> list[_Piece]:
    """Split one oversized paragraph at sentence boundaries, falling back to words or characters."""
    def atoms(text: str) -> list[str]:
        if estimate_tokens(text) <= config.max_tokens:
            return [text]
        sentences = [s for s in _SENTENCE.split(text) if s]
        if len(sentences) > 1:
            return [a for s in sentences for a in atoms(s)]
        words = text.split(" ") if " " in text else None
        size = config.target_tokens // 2 if words else config.target_tokens
        items = words if words else list(text)
        joiner = " " if words else ""
        return [joiner.join(items[i:i + size]) for i in range(0, len(items), size)]

    pieces: list[_Piece] = []
    current = ""
    for atom in atoms(block.text):
        candidate = f"{current} {atom}".strip() if current else atom
        if current and estimate_tokens(candidate) > config.target_tokens:
            pieces.append(_Piece(current, block, estimate_tokens(current), True))
            current = atom
        else:
            current = candidate
    if current:
        pieces.append(_Piece(current, block, estimate_tokens(current), True))
    return pieces


def _split_table(block: Block, config: ChunkingConfig) -> list[_Piece]:
    """Row groups with the header repeated. A row is never split, even if it exceeds the limit."""
    rows = table_rows(block.text)
    header, body = rows[:2], rows[2:]
    header_tokens = estimate_tokens("\n".join(header))
    row_cap = config.spreadsheet_row_group if len(body) > LARGE_SHEET_ROWS else None
    pieces: list[_Piece] = []
    group: list[str] = []
    tokens = header_tokens
    for row in body:
        row_tokens = estimate_tokens(row)
        if group and (tokens + row_tokens > config.max_tokens or (row_cap and len(group) >= row_cap)):
            pieces.append(_Piece("\n".join(header + group), block, tokens, standalone=True))
            group, tokens = [], header_tokens
        group.append(row)
        tokens += row_tokens
    if group or not pieces:
        pieces.append(_Piece("\n".join(header + group), block, tokens, standalone=True))
    return pieces


def _split_code(block: Block, config: ChunkingConfig) -> list[_Piece]:
    """Code is kept whole. Only blocks above max_block_tokens (long logs) split, at line boundaries."""
    tokens = estimate_tokens(block.text)
    if tokens <= config.max_block_tokens:
        return [_Piece(block.to_markdown(), block, tokens)]
    pieces: list[_Piece] = []
    current: list[str] = []
    current_tokens = 0
    for line in block.text.split("\n"):
        line_tokens = estimate_tokens(line)
        if current and current_tokens + line_tokens > config.max_block_tokens:
            part = Block("code", "\n".join(current), language=block.language)
            pieces.append(_Piece(part.to_markdown(), block, current_tokens, standalone=True))
            current, current_tokens = [], 0
        current.append(line)
        current_tokens += line_tokens
    if current:
        part = Block("code", "\n".join(current), language=block.language)
        pieces.append(_Piece(part.to_markdown(), block, current_tokens, standalone=True))
    return pieces


def _pieces(block: Block, config: ChunkingConfig) -> list[_Piece]:
    if block.type == "code":
        return _split_code(block, config)
    tokens = estimate_tokens(block.text)
    if block.type == "table":
        if tokens <= config.max_tokens and len(table_rows(block.text)) - 2 <= LARGE_SHEET_ROWS:
            return [_Piece(block.text, block, tokens)]
        return _split_table(block, config)
    if tokens > config.max_tokens:
        return _split_prose(block, config)
    return [_Piece(block.text, block, tokens, block.type in {"paragraph", "list"})]


def _tail(text: str, tokens: int) -> str:
    """The last `tokens` tokens of prose, for overlap inside a section."""
    if tokens <= 0:
        return ""
    if _CJK.search(text):
        return text[-tokens:]
    return " ".join(text.split(" ")[-max(1, round(tokens / 1.3)):])


def _group_section(pieces: list[_Piece], config: ChunkingConfig) -> list[_Group]:
    groups: list[_Group] = []
    current = _Group()
    for piece in pieces:
        full = current.tokens + piece.tokens
        over_target = current.tokens >= config.min_tokens and full > config.target_tokens
        if current.pieces and (full > config.max_tokens or over_target or piece.standalone
                               or current.pieces[-1].standalone):
            groups.append(current)
            last = current.pieces[-1]
            # Overlap only when prose continues; never repeat code or tables.
            overlap = _tail(last.text, config.overlap_tokens) if last.splittable_prose and piece.splittable_prose else ""
            current = _Group(overlap=overlap)
        current.pieces.append(piece)
        current.tokens += piece.tokens
    if current.pieces:
        groups.append(current)
    return groups


_PATH_RANK = {"fast": 0, "vlm": 1, "ocr": 2}


def page_anchor(kind: str, start: int, end: int) -> str:
    """'page:12', or 'page:12-13' for content spanning pages, so opening the citation returns all of it."""
    return f"{kind}:{start}" if start == end else f"{kind}:{start}-{end}"


def _anchor(doc: Document, first: Block, last_page: int | None, section: Section | None) -> str:
    if doc.unit == "sheet" and first.label:
        return f"sheet:{first.label}"
    if doc.unit in {"slide", "page"} and first.page is not None:
        return page_anchor(doc.unit, first.page, last_page if last_page is not None else first.page)
    if section is not None:
        return section.anchor
    return f"block:{first.index}"


def chunk_document(doc: Document, file_id: str, version: str, config: ChunkingConfig) -> list[Chunk]:
    blocks = doc.blocks
    all_sections = sections(blocks)
    by_start = {s.start: s for s in all_sections}
    chunks: list[Chunk] = []
    stack: list[Section] = []
    pending: list[Block] = []

    def flush() -> None:
        if not pending:
            return
        path = " > ".join(s.title for s in stack) or None
        pieces = [p for block in pending for p in _pieces(block, config)]
        for group in _group_section(pieces, config):
            bodies = [p.text for p in group.pieces]
            if group.overlap:
                bodies[0] = f"{group.overlap} {bodies[0]}"
            text = "\n\n".join(bodies)
            members = [p.block for p in group.pieces]
            pages = [b.page for b in members if b.page is not None]
            ordinal = len(chunks)
            chunks.append(Chunk(
                chunk_id=chunk_id(file_id, version, ordinal),
                ordinal=ordinal,
                anchor=_anchor(doc, members[0], max(pages) if pages else None, stack[-1] if stack else None),
                heading_path=path,
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                block_types=sorted({b.type for b in members}),
                extraction_path=max((b.extraction_path for b in members), key=lambda p: _PATH_RANK.get(p, 0)),
                quality="degraded" if any(b.quality == "degraded" for b in members) else "ok",
                text=text,
                token_count=estimate_tokens(text),
            ))
        pending.clear()

    for position, block in enumerate(blocks):
        if block.type == "heading":
            flush()
            section = by_start[position]
            while stack and stack[-1].level >= section.level:
                stack.pop()
            stack.append(section)
        elif block.text.strip():
            pending.append(block)
    flush()
    return chunks
