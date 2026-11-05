"""The real pipeline on real generated files, end to end except for OCR (see the integration tests)."""

from pathlib import Path

from tests.fakes import fixtures

from doclib.config import ExtractionConfig
from doclib.extract.model import sections
from doclib.extract.pipeline import decode_text, extract_document, pdf_image_ratios

CONFIG = ExtractionConfig()


async def test_pdf_keeps_headings_code_and_table(tmp_path: Path):
    fixtures.make_guide_pdf(tmp_path / "g.pdf")
    doc = await extract_document((tmp_path / "g.pdf").read_bytes(), "g.pdf", CONFIG)
    assert doc.unit == "page" and [p.number for p in doc.pages] == [1, 2]
    assert not doc.hard_pages
    kinds = {b.type for b in doc.blocks}
    assert {"heading", "paragraph", "code", "table"} <= kinds
    code = next(b for b in doc.blocks if b.type == "code")
    assert "    unit 0\n        family inet" in code.text
    table = next(b for b in doc.blocks if b.type == "table")
    assert "| smf-2 | 10.0.0.2 | 8805 |" in table.text
    assert "4.2" in [s.anchor.removeprefix("heading:") for s in sections(doc.blocks)]
    assert doc.title == "5G Core Ops Guide"


async def test_docx_structure(tmp_path: Path):
    fixtures.make_docx(tmp_path / "m.docx")
    doc = await extract_document((tmp_path / "m.docx").read_bytes(), "m.docx", CONFIG)
    assert any(b.type == "heading" and b.text == "Preconditions" for b in doc.blocks)
    assert any(b.type == "table" and "| Backup | NOC |" in b.text for b in doc.blocks)


async def test_spreadsheet_has_one_page_per_sheet(tmp_path: Path):
    fixtures.make_xlsx(tmp_path / "p.xlsx")
    doc = await extract_document((tmp_path / "p.xlsx").read_bytes(), "p.xlsx", CONFIG)
    assert doc.unit == "sheet"
    assert [p.label for p in doc.pages] == ["IP Plan", "Ports"]
    assert any("| TYO1 | 100 | 10.1.0.0/24 |" in b.text for b in doc.blocks)


async def test_presentation_uses_slides(tmp_path: Path):
    fixtures.make_pptx(tmp_path / "d.pptx")
    doc = await extract_document((tmp_path / "d.pptx").read_bytes(), "d.pptx", CONFIG)
    assert doc.unit == "slide" and [p.number for p in doc.pages] == [1, 2]


async def test_config_files_bypass_the_backend_and_stay_verbatim():
    raw = b"interface ge-0/0/1\n    unit 0\n\n        family inet\n"
    doc = await extract_document(raw, "router.cfg", CONFIG)
    assert doc.blocks[0].type == "code" and doc.blocks[0].text == raw.decode()
    assert doc.extractor == "direct"


async def test_japanese_shift_jis_text_decodes():
    assert decode_text("再起動手順".encode("cp932")) == "再起動手順"


async def test_scanned_pdf_page_is_routed_as_hard(tmp_path: Path):
    fixtures.make_scanned_pdf(tmp_path / "s.pdf")
    data = (tmp_path / "s.pdf").read_bytes()
    assert pdf_image_ratios(data)[1] > 0.9
    doc = await extract_document(data, "s.pdf", CONFIG)
    assert doc.hard_pages == [1] and "low_text" in doc.pages[0].route_reasons


async def test_vision_output_replaces_ocr(tmp_path: Path):
    fixtures.make_scanned_pdf(tmp_path / "s.pdf")
    doc = await extract_document((tmp_path / "s.pdf").read_bytes(), "s.pdf", CONFIG, vlm_pages={1: "# From Vision\n\nbody"})
    assert doc.pages[0].extraction_path == "vlm" and doc.blocks[0].text == "From Vision"
