from doclib.chunker import chunk_document, estimate_tokens
from doclib.config import ChunkingConfig
from doclib.extract.markdown import parse_markdown, table_is_ragged
from doclib.extract.model import Block, Document, Page, outline, sections

CONFIG = ChunkingConfig()


def doc_from(markdown: str, unit: str = "none", page: int | None = None) -> Document:
    return Document(unit=unit, pages=[Page(page, parse_markdown(markdown))])


def chunks_of(doc: Document, config: ChunkingConfig = CONFIG):
    return chunk_document(doc, "file", "v1", config)


def test_heading_breadcrumb_is_prepended_to_embedded_text():
    chunk, = chunks_of(doc_from("# Guide\n\n## 4 SMF\n\n### 4.2 Restart\n\nBody text."))
    assert chunk.heading_path == "Guide > 4 SMF > 4.2 Restart"
    assert chunk.embedding_text.startswith("Guide > 4 SMF > 4.2 Restart\n\n")
    assert chunk.text == "Body text."


def test_heading_stack_pops_at_same_level():
    chunks = chunks_of(doc_from("# A\n\n## B\n\nb\n\n## C\n\nc\n\n# D\n\nd"))
    assert [c.heading_path for c in chunks] == ["A > B", "A > C", "D"]


def test_code_block_is_never_split_and_keeps_whitespace():
    code = "\n".join(f"    line  {i}   value" for i in range(300))
    chunk_list = chunks_of(doc_from(f"# H\n\n```\n{code}\n```"))
    assert len(chunk_list) == 1
    assert code in chunk_list[0].text
    assert chunk_list[0].block_types == ["code"]


def test_oversized_code_splits_only_at_line_boundaries():
    code = "\n".join(f"log line number {i}" for i in range(2000))
    chunk_list = chunks_of(doc_from(f"```\n{code}\n```"))
    assert len(chunk_list) > 1
    rebuilt = "\n".join(c.text.removeprefix("```\n").removesuffix("\n```") for c in chunk_list)
    assert rebuilt == code


def test_long_table_repeats_header_and_never_splits_rows():
    rows = "\n".join(f"| node-{i} | 10.0.{i % 255}.1 | description for node {i} |" for i in range(600))
    table = f"| Name | IP | Notes |\n| --- | --- | --- |\n{rows}"
    chunk_list = chunks_of(doc_from(f"# Plan\n\n{table}"))
    assert len(chunk_list) > 1
    seen = []
    for chunk in chunk_list:
        lines = chunk.text.split("\n")
        assert lines[:2] == ["| Name | IP | Notes |", "| --- | --- | --- |"]
        assert not table_is_ragged(chunk.text)
        seen += lines[2:]
    assert seen == rows.split("\n")


def test_very_large_sheet_uses_row_groups():
    rows = "\n".join(f"| {i} | x |" for i in range(5100))
    config = ChunkingConfig(spreadsheet_row_group=200, max_tokens=100000, target_tokens=100000)
    chunk_list = chunks_of(doc_from(f"| a | b |\n| --- | --- |\n{rows}"), config)
    assert max(len(c.text.split("\n")) - 2 for c in chunk_list) == 200


def test_long_section_splits_with_overlap_only_inside_section():
    paragraphs = "\n\n".join(" ".join(f"word{p}x{i}" for i in range(200)) for p in range(12))
    chunk_list = chunks_of(doc_from(f"# A\n\n{paragraphs}\n\n# B\n\nFresh section."))
    in_a = [c for c in chunk_list if c.heading_path == "A"]
    assert len(in_a) > 1
    tail = in_a[0].text.split(" ")[-5:]
    assert " ".join(tail) in in_a[1].text
    assert chunk_list[-1].text == "Fresh section."  # No overlap across a heading.


def test_chunk_size_stays_near_target():
    paragraphs = "\n\n".join(" ".join(f"w{i}" for i in range(100)) for _ in range(40))
    for chunk in chunks_of(doc_from(paragraphs)):
        assert chunk.token_count <= CONFIG.max_tokens + CONFIG.overlap_tokens


def test_japanese_paragraph_without_spaces_splits():
    text = "再起動手順" * 800
    chunk_list = chunks_of(doc_from(text))
    assert len(chunk_list) > 1
    assert estimate_tokens(chunk_list[0].text) <= CONFIG.max_tokens + CONFIG.overlap_tokens
    assert estimate_tokens("再起動") == 3


def test_chunk_ids_are_stable_and_version_specific():
    doc = doc_from("# A\n\nBody")
    first = chunk_document(doc, "f", "v1", CONFIG)[0].chunk_id
    assert first == chunk_document(doc, "f", "v1", CONFIG)[0].chunk_id
    assert first != chunk_document(doc, "f", "v2", CONFIG)[0].chunk_id


def test_page_anchor_and_range():
    doc = Document(unit="page", pages=[Page(3, [Block("paragraph", "one")]), Page(4, [Block("paragraph", "two")])])
    chunk, = chunks_of(doc)
    assert (chunk.anchor, chunk.page_start, chunk.page_end) == ("page:3-4", 3, 4)


def test_sheet_and_slide_anchors():
    sheet = Document(unit="sheet", pages=[Page(1, [Block("paragraph", "x")], label="IP Plan")])
    assert chunks_of(sheet)[0].anchor == "sheet:IP Plan"
    slide = Document(unit="slide", pages=[Page(2, [Block("paragraph", "x")])])
    assert chunks_of(slide)[0].anchor == "slide:2"


def test_heading_anchor_uses_section_number_and_deduplicates():
    found = sections(parse_markdown("# 4.2 Restart\n\nx\n\n# Overview\n\ny\n\n# Overview\n\nz"))
    assert [s.anchor for s in found] == ["heading:4.2", "heading:overview", "heading:overview~2"]


def test_outline_tree():
    tree = outline(doc_from("# A\n\n## B\n\nb\n\n## C\n\n# D"))
    assert [n["title"] for n in tree] == ["A", "D"]
    assert [n["title"] for n in tree[0]["children"]] == ["B", "C"]


def test_worst_quality_and_path_win():
    doc = Document(unit="page", pages=[Page(1, [Block("paragraph", "a")]),
                                       Page(2, [Block("paragraph", "b")], extraction_path="ocr", quality="degraded")])
    chunk, = chunks_of(doc)
    assert (chunk.extraction_path, chunk.quality) == ("ocr", "degraded")


def test_anchor_covers_every_page_a_chunk_spans():
    doc = Document(unit="slide", pages=[Page(2, [Block("paragraph", "a")]), Page(3, [Block("paragraph", "b")])])
    assert chunks_of(doc)[0].anchor == "slide:2-3"


def test_doc_label_splits_names_into_searchable_words():
    from doclib.chunker import doc_label
    assert doc_label("Vendors/Acme_SMF-Upgrade_v3.pdf", "Core Guide") == "Vendors Acme SMF Upgrade v3 pdf Core Guide"
    assert doc_label("a.md", None) == "a md"
