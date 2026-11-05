from datetime import UTC, datetime

from doclib.changes import KnownFile, detect, in_scope, matches_any
from doclib.config import SourceConfig
from doclib.rclone import Entry, content_version

T = datetime(2026, 1, 1, tzinfo=UTC)


def entry(path, h="a", size=1, modtime=T):
    return Entry(path, size, modtime, {"md5": h} if h else {})


def known(path, h="a", file_id=None):
    return KnownFile(file_id or path, path, content_version(entry(path, h)))


def test_new_changed_deleted_unchanged():
    result = detect([entry("new.pdf", "n"), entry("same.pdf"), entry("edit.pdf", "two")],
                    [known("same.pdf"), known("edit.pdf", "one"), known("gone.pdf", "g")])
    assert [e.path for e in result.new] == ["new.pdf"]
    assert [k.path for k, _ in result.changed] == ["edit.pdf"]
    assert [k.path for k in result.deleted] == ["gone.pdf"]
    assert result.unchanged == 1 and not result.moved


def test_rename_keeps_file_identity():
    result = detect([entry("b/new.pdf", "h")], [known("a/old.pdf", "h", file_id="id-1")])
    assert not result.new and not result.deleted
    (old, new), = result.moved
    assert old.file_id == "id-1" and new.path == "b/new.pdf"


def test_copy_is_not_a_move():
    result = detect([entry("a.pdf", "h"), entry("copy.pdf", "h")], [known("a.pdf", "h")])
    assert [e.path for e in result.new] == ["copy.pdf"] and not result.moved


def test_identical_files_moved_together_pair_up():
    result = detect([entry("n/1.pdf", "h"), entry("n/2.pdf", "h")], [known("o/1.pdf", "h"), known("o/2.pdf", "h")])
    assert len(result.moved) == 2 and not result.new and not result.deleted


def test_size_modtime_move_requires_one_to_one_match():
    # No hash available (SMB). One candidate each side: a move.
    old = KnownFile("id", "old.pdf", content_version(entry("old.pdf", None)))
    assert len(detect([entry("new.pdf", None)], [old]).moved) == 1
    # Two candidates could be different files with coincidental size+modtime: delete and add instead.
    olds = [KnownFile("1", "a.pdf", old.content_version), KnownFile("2", "b.pdf", old.content_version)]
    ambiguous = detect([entry("c.pdf", None), entry("d.pdf", None)], olds)
    assert not ambiguous.moved and len(ambiguous.new) == 2 and len(ambiguous.deleted) == 2


def test_version_uses_hash_else_size_and_modtime():
    assert content_version(entry("x", "abc")) == "md5:abc"
    assert content_version(entry("x", None, size=5)).startswith("sm:5-")
    assert content_version(Entry("x", 1, T, {"md5": "m", "sha256": "s"})) == "sha256:s"


def test_globs():
    assert matches_any("~$x.docx", ["**/~$*"]) and matches_any("a/b/~$x.docx", ["**/~$*"])
    assert matches_any("a/Archive/x.pdf", ["**/Archive/**"]) and matches_any("Archive/x.pdf", ["**/Archive/**"])
    assert not matches_any("Archived/x.pdf", ["**/Archive/**"])
    assert matches_any("Operations/x.pdf", ["Operations/**"]) and not matches_any("Other/x.pdf", ["Operations/**"])


def test_scope_filters_globs_and_extensions():
    source = SourceConfig(id="s", rclone_remote="local", root_path="/r", exclude=["**/Archive/**"])
    assert in_scope("a/guide.PDF", source)
    assert not in_scope("Archive/guide.pdf", source)
    assert not in_scope("a/program.exe", source)
