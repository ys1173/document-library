"""Parse Markdown into blocks. Used for xberg's per-page output, .md files and, in Phase 2, VLM output."""

from __future__ import annotations

import re

from .model import Block

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _is_table_start(lines: list[str], i: int) -> bool:
    return (i + 1 < len(lines) and "|" in lines[i] and "|" in lines[i + 1]
            and bool(_TABLE_SEPARATOR.match(lines[i + 1])))


def parse_markdown(text: str) -> list[Block]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if fence := _FENCE.match(line):
            marker = fence.group(2)
            body: list[str] = []
            i += 1
            while i < len(lines) and not (lines[i].strip().startswith(marker[0] * len(marker))
                                          and not lines[i].strip().strip(marker[0])):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence
            blocks.append(Block("code", "\n".join(body), language=fence.group(3) or None))
            continue
        if heading := _HEADING.match(line):
            blocks.append(Block("heading", heading.group(2), level=len(heading.group(1))))
            i += 1
            continue
        if _is_table_start(lines, i):
            start = i
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                i += 1
            blocks.append(Block("table", "\n".join(lines[start:i])))
            continue
        if _LIST.match(line):
            start = i
            while i < len(lines) and (_LIST.match(lines[i]) or (lines[i].startswith((" ", "\t")) and lines[i].strip())):
                i += 1
            blocks.append(Block("list", "\n".join(lines[start:i])))
            continue
        start = i
        while (i < len(lines) and lines[i].strip() and not _HEADING.match(lines[i]) and not _FENCE.match(lines[i])
               and not _is_table_start(lines, i) and not _LIST.match(lines[i])):
            i += 1
        if i == start:  # Defensive: always make progress.
            i += 1
        blocks.append(Block("paragraph", "\n".join(lines[start:i])))
    return blocks


def table_rows(table_text: str) -> list[str]:
    return [line for line in table_text.split("\n") if line.strip()]


def split_cells(row: str) -> list[str]:
    body = row.strip()
    body = body.removeprefix("|").removesuffix("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def table_is_ragged(table_text: str) -> bool:
    """Rows with different column counts, or a header with no data rows, mean low-confidence structure."""
    rows = table_rows(table_text)
    if len(rows) < 3:
        return True
    widths = {len(split_cells(r)) for r in rows}
    return len(widths) > 1
