import struct
import zlib

from doclib.extract.markdown import parse_markdown
from doclib.extract.model import Block
from doclib.vlm import encode_png, html_table_to_markdown, parse_response


def types(markdown):
    return [b.type for b in parse_markdown(markdown)]


def test_parses_all_block_types():
    text = "# T\n\nPara\nline two\n\n- a\n- b\n\n```ini\n  x = 1\n```\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    assert types(text) == ["heading", "paragraph", "list", "code", "table"]


def test_code_keeps_whitespace_and_language():
    code, = parse_markdown("```ini\n  x  =  1\n\n    y\n```")
    assert code.text == "  x  =  1\n\n    y" and code.language == "ini"


def test_unclosed_fence_does_not_hang_or_lose_text():
    assert parse_markdown("```\nabc")[0].text == "abc"


def test_code_round_trip_with_embedded_fence():
    block = Block("code", "before\n```\nafter")
    assert parse_markdown(block.to_markdown())[0].text == "before\n```\nafter"


def test_paragraph_followed_directly_by_table():
    assert types("intro\n| a |\n| --- |\n| 1 |") == ["paragraph", "table"]


def test_pipe_in_a_paragraph_is_not_a_table():
    assert types("run a | b to pipe") == ["paragraph"]


def test_html_table_to_markdown():
    html = "<table><tr><th>Node</th><th>IP</th></tr><tr><td>smf-1</td><td>10.0.0.1</td></tr></table>"
    assert html_table_to_markdown(html) == "| Node | IP |\n| --- | --- |\n| smf-1 | 10.0.0.1 |"


def test_html_table_colspan_and_short_rows_stay_rectangular():
    html = "<table><tr><td colspan=2>Head</td></tr><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"
    lines = html_table_to_markdown(html).split("\n")
    assert len({line.count("|") for line in lines}) == 1


def test_parse_response_unwraps_fence_and_converts_tables():
    out = parse_response("```markdown\n# T\n\n<table><tr><td>a</td></tr><tr><td>1</td></tr></table>\n```")
    assert out.startswith("# T") and "| a |" in out and "<table" not in out
    assert [b.type for b in parse_markdown(out)] == ["heading", "table"]


def test_png_encoder_produces_valid_png():
    png = encode_png(2, 1, bytes([255, 0, 0, 0, 255, 0]), 6)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (2, 1)
    length = struct.unpack(">I", png[33:37])[0]
    assert zlib.decompress(png[41:41 + length]) == b"\x00\xff\x00\x00\x00\xff\x00"
