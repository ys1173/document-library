"""Page router: decide whether a page needs the hard path (spec section 6.2). Pure, so it is easy to test."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import RouterConfig
from .markdown import table_is_ragged
from .model import Block


@dataclass(frozen=True)
class PageSignals:
    text: str
    tables: tuple[str, ...] = ()
    image_area_ratio: float | None = None
    is_image_file: bool = False


def garbled_fraction(text: str) -> float:
    """Share of characters that are replacement or non-printable. Scored on non-space characters."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for c in chars if c == "�" or (not c.isprintable()))
    return bad / len(chars)


def route(signals: PageSignals, config: RouterConfig) -> list[str]:
    """Reasons this page needs the hard path. Empty means keep the fast-path result."""
    reasons: list[str] = []
    if signals.is_image_file:
        reasons.append("image_file")
    if len(signals.text.strip()) < config.min_chars_per_page:
        reasons.append("low_text")
    if signals.image_area_ratio is not None and signals.image_area_ratio > config.image_area_ratio:
        reasons.append("image_heavy")
    if any(table_is_ragged(t) for t in signals.tables):
        reasons.append("ragged_table")
    if garbled_fraction(signals.text) > config.garbled_ratio:
        reasons.append("garbled")
    return reasons


def signals_for(blocks: list[Block], image_area_ratio: float | None = None) -> PageSignals:
    text = "\n".join(b.text for b in blocks)
    tables = tuple(b.text for b in blocks if b.type == "table")
    return PageSignals(text=text, tables=tables, image_area_ratio=image_area_ratio)
