"""Pure change detection: compare a source listing with what the index knows (spec section 5.1)."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache

from .config import SourceConfig
from .rclone import Entry, content_version


@dataclass(frozen=True)
class KnownFile:
    file_id: str
    path: str
    content_version: str
    size_bytes: int | None = None
    status: str = "indexed"


@dataclass
class ChangeSet:
    new: list[Entry] = field(default_factory=list)
    changed: list[tuple[KnownFile, Entry]] = field(default_factory=list)
    deleted: list[KnownFile] = field(default_factory=list)
    moved: list[tuple[KnownFile, Entry]] = field(default_factory=list)
    unchanged: int = 0

    @property
    def total(self) -> int:
        return len(self.new) + len(self.changed) + len(self.deleted) + len(self.moved)


@lru_cache(maxsize=512)
def _compile_glob(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += r"(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(_compile_glob(p).match(path) for p in patterns)


def extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def in_scope(path: str, source: SourceConfig) -> bool:
    """A file moved out of scope (excluded folder, disallowed type) then counts as deleted."""
    if matches_any(path, source.exclude) or not matches_any(path, source.include):
        return False
    allowed = {e.lower().lstrip(".") for e in source.allowed_extensions}
    return extension(path) in allowed


def detect(listing: Iterable[Entry], known: Iterable[KnownFile]) -> ChangeSet:
    known_by_path = {k.path: k for k in known}
    result = ChangeSet()
    seen: set[str] = set()
    unmatched_new: list[Entry] = []
    for entry in listing:
        seen.add(entry.path)
        existing = known_by_path.get(entry.path)
        if existing is None:
            unmatched_new.append(entry)
        elif existing.content_version == content_version(entry):
            result.unchanged += 1
        else:
            result.changed.append((existing, entry))
    missing = [k for path, k in known_by_path.items() if path not in seen]

    # Moves: same version, old path gone, new path appeared. With a real hash the content is
    # known to be identical. With size+modtime, only accept an unambiguous one-to-one match.
    new_by_version: dict[str, list[Entry]] = defaultdict(list)
    for entry in unmatched_new:
        new_by_version[content_version(entry)].append(entry)
    moved_paths: set[str] = set()
    missing_by_version: dict[str, list[KnownFile]] = defaultdict(list)
    for item in missing:
        missing_by_version[item.content_version].append(item)
    for version, olds in missing_by_version.items():
        news = new_by_version.get(version, [])
        if not news or (version.startswith("sm:") and (len(olds) != 1 or len(news) != 1)):
            continue
        for old, new in zip(sorted(olds, key=lambda k: k.path), sorted(news, key=lambda e: e.path), strict=False):
            result.moved.append((old, new))
            moved_paths.update((old.path, new.path))
    result.new = [e for e in unmatched_new if e.path not in moved_paths]
    result.deleted = [k for k in missing if k.path not in moved_paths]
    return result
