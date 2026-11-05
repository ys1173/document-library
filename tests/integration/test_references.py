"""Do citations point at the right document and page? Checked against files with known answers."""

from __future__ import annotations

import json

import pytest

from doclib.library import Filters
from doclib.scanner import scan_source
from tests.fakes.fixtures import REFERENCE_MARKERS, make_reference_pdf, make_spanning_pdf
from tests.integration.conftest import resync

pytestmark = pytest.mark.integration
TENANT = "acme"


def only(path):
    return Filters(path_prefix=path)


@pytest.fixture
async def manual(indexed, share, settings, rclone, embedder):
    make_reference_pdf(share / "Manual.pdf")
    await resync(indexed, settings, rclone, embedder)
    return indexed


@pytest.mark.parametrize("page,word", sorted(REFERENCE_MARKERS.items()))
async def test_citation_points_to_the_right_page(manual, library, page, word):
    results = (await library.search_docs(TENANT, f"{word} equipment procedure", filters=only("Manual.pdf")))["results"]
    hit = next(r for r in results if word in r["text"])
    citation = hit["citation"]
    assert citation["path"] == "Manual.pdf" and citation["title"] == "Reference Manual"
    assert citation["page_start"] <= page <= citation["page_end"]
    # Agents can act on the reference three ways, and all must land on that page.
    assert word in (await library.open_doc(TENANT, uri=citation["uri"]))["markdown"]
    assert word in (await library.open_doc(TENANT, file_id=citation["file_id"], anchor=citation["anchor"]))["markdown"]
    assert word in (await library.open_doc(TENANT, source_id="ops", path="Manual.pdf", page_range=str(page)))["markdown"]


async def test_opening_a_page_returns_only_that_page(manual, library):
    opened = await library.open_doc(TENANT, source_id="ops", path="Manual.pdf", page_range="3")
    assert (opened["page_start"], opened["page_end"]) == (3, 3)
    assert "chinchilla" in opened["markdown"]
    assert not [w for p, w in REFERENCE_MARKERS.items() if p != 3 and w in opened["markdown"]]


async def test_page_range_returns_exactly_those_pages(manual, library):
    opened = await library.open_doc(TENANT, source_id="ops", path="Manual.pdf", page_range="2-4")
    found = {p for p, w in REFERENCE_MARKERS.items() if w in opened["markdown"]}
    assert found == {2, 3, 4} and (opened["page_start"], opened["page_end"]) == (2, 4)


async def test_a_chunk_spanning_pages_opens_all_of_its_pages(indexed, share, settings, rclone, embedder, library):
    make_spanning_pdf(share / "Span.pdf")
    await resync(indexed, settings, rclone, embedder)
    hit = (await library.search_docs(TENANT, "spanstartmarker", filters=only("Span.pdf")))["results"][0]
    citation = hit["citation"]
    assert (citation["page_start"], citation["page_end"]) == (1, 2)
    assert citation["anchor"] == "page:1-2"
    opened = await library.open_doc(TENANT, uri=citation["uri"])
    assert "spanstartmarker" in opened["markdown"] and "spanendmarker" in opened["markdown"]


async def test_adjacent_chunks_merged_into_one_result_open_in_full(indexed, share, settings, rclone, embedder, library):
    """Search merges neighbouring hits, so the merged citation has to cover all of them."""
    body = "\n\n".join(f"Paragraph {i} on the wombat procedure. " + "filler words here " * 80 for i in range(12))
    (share / "long.md").write_text(f"# Long\n\n{body}", encoding="utf-8")
    await resync(indexed, settings, rclone, embedder)
    hit = (await library.search_docs(TENANT, "wombat procedure", filters=only("long.md")))["results"][0]
    assert len(hit["chunk_ids"]) > 1
    opened = await library.open_doc(TENANT, uri=hit["citation"]["uri"], max_chars=500000)
    assert "Paragraph 0 " in opened["markdown"] and "Paragraph 11 " in opened["markdown"]


async def test_format_specific_references(indexed, library):
    async def citation(query, path):
        results = (await library.search_docs(TENANT, query, filters=only(path)))["results"]
        return results[0]["citation"]

    slide = await citation("standby UPF failover", "Design.pptx")
    assert slide["anchor"] == "slide:2" and slide["page_start"] == 2
    sheet = await citation("sw1 port device", "IP Plan.xlsx")
    assert sheet["anchor"].startswith("sheet:")
    # Word files have no fixed pages, so the reference is the section, and page numbers stay empty rather than guessed.
    word = await citation("configuration backup", "MOPs/Upgrade.docx")
    assert word["anchor"].startswith("heading:") and word["page_start"] is None
    assert "Preconditions" in word["heading_path"]


async def test_page_number_is_kept_after_the_vision_upgrade(pool, share, embed_server, rclone, embedder):
    from doclib.vlm import VlmClient
    from doclib.vlm_worker import drain as drain_vlm
    from doclib.worker import drain
    from tests.fakes.fixtures import make_mixed_pdf
    from tests.fakes.servers import ServerThread, create_vision_app
    from tests.integration.conftest import build_settings, default_source

    with ServerThread(create_vision_app({})) as vision:
        settings = build_settings(embed_server.url, default_source(share), extraction={
            "vlm": {"enabled": True, "endpoint": f"{vision.url}/v1", "model": "fake", "retries": 0}})
        make_mixed_pdf(share / "mixed.pdf")
        await scan_source(pool, settings, rclone, settings.sources[0])
        await drain(pool, settings, rclone, embedder)
        client = VlmClient(settings.extraction.vlm)
        await drain_vlm(pool, settings, rclone, embedder, client)
        await client.aclose()
    rows = await pool.fetch(
        "select c.page_start, c.page_end, c.extraction_path, c.text from doclib.chunks c join doclib.files f using (file_id) "
        "where f.path = 'mixed.pdf' order by c.ordinal")
    by_text = {("native" if "Native text" in r["text"] else "vision"): r for r in rows}
    assert by_text["native"]["page_start"] == 1
    assert by_text["vision"]["page_start"] == 2 or by_text["native"]["page_end"] == 2


async def test_file_id_and_references_follow_a_rename(manual, library, share, settings, rclone, embedder):
    before = (await library.search_docs(TENANT, "dugong equipment", filters=only("Manual.pdf")))["results"][0]["citation"]
    (share / "Manual.pdf").rename(share / "Renamed Manual.pdf")
    await resync(manual, settings, rclone, embedder)
    after = (await library.search_docs(TENANT, "dugong equipment", filters=only("Renamed")))["results"][0]["citation"]
    assert after["file_id"] == before["file_id"] and after["path"] == "Renamed Manual.pdf"
    assert after["page_start"] == before["page_start"]
    # A reference saved before the rename still opens.
    assert "dugong" in (await library.open_doc(TENANT, uri=before["uri"]))["markdown"]


# Finding a document by what it's called, not just what it says.
async def test_document_can_be_found_by_file_name_and_folder(indexed, share, settings, rclone, embedder, library):
    (share / "Vendors" / "Acme_SMF-Upgrade_v3.md").write_text("# Procedure\n\nStep one is to check the pods.\n", encoding="utf-8")
    await resync(indexed, settings, rclone, embedder)
    where_paths = {r["citation"]["path"] for r in (await library.search_docs(TENANT, "Acme"))["results"]}
    assert "Vendors/Acme_SMF-Upgrade_v3.md" in where_paths
    # Names compete with content, so another document that discusses upgrades may rank above this one.
    results = (await library.search_docs(TENANT, "Acme upgrade v3", top_k=3))["results"]
    named = next(r for r in results if r["citation"]["path"] == "Vendors/Acme_SMF-Upgrade_v3.md")
    # And the name isn't added to the text agents read.
    assert "Acme" not in named["text"]
    # Renaming updates it without re-indexing.
    (share / "Vendors" / "Acme_SMF-Upgrade_v3.md").rename(share / "Vendors" / "Globex_Runbook.md")
    await scan_source(indexed, settings, rclone, settings.sources[0])
    assert "Vendors/Globex_Runbook.md" in {r["citation"]["path"] for r in (await library.search_docs(TENANT, "Globex"))["results"]}


async def test_document_can_be_found_by_title(manual, library):
    results = (await library.search_docs(TENANT, "Reference Manual"))["results"]
    assert results[0]["citation"]["path"] == "Manual.pdf"


async def test_print_a_real_citation_for_the_docs(manual, library, capsys):
    result = (await library.search_docs(TENANT, "chinchilla equipment", top_k=1, filters=only("Manual.pdf")))["results"][0]
    result["text"] = result["text"][:70] + "..."
    with capsys.disabled():
        print("\nSAMPLE:" + json.dumps(result, ensure_ascii=False))


async def test_page_anchor_ranges_and_out_of_range_pages(manual, library):
    opened = await library.open_doc(TENANT, source_id="ops", path="Manual.pdf", anchor="page:2-3")
    assert (opened["page_start"], opened["page_end"]) == (2, 3) and opened["locator_found"] is True
    missing = await library.open_doc(TENANT, source_id="ops", path="Manual.pdf", anchor="page:99")
    assert missing["locator_found"] is False  # Says so, rather than quietly returning something else as if it matched.
