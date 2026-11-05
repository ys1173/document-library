from doclib.config import RouterConfig
from doclib.extract.router import PageSignals, garbled_fraction, route

CONFIG = RouterConfig()
GOOD_TEXT = "Restart the SMF pods one at a time and wait for readiness between each of them."
GOOD_TABLE = "| a | b |\n| --- | --- |\n| 1 | 2 |"


def test_good_page_stays_on_fast_path():
    assert route(PageSignals(GOOD_TEXT, (GOOD_TABLE,), 0.1), CONFIG) == []


def test_low_text():
    assert "low_text" in route(PageSignals("x" * 49), CONFIG)
    assert "low_text" not in route(PageSignals("x" * 50), CONFIG)


def test_image_heavy():
    assert "image_heavy" in route(PageSignals(GOOD_TEXT, image_area_ratio=0.61), CONFIG)
    assert route(PageSignals(GOOD_TEXT, image_area_ratio=0.6), CONFIG) == []


def test_ragged_table():
    assert "ragged_table" in route(PageSignals(GOOD_TEXT, ("| a | b |\n| --- | --- |\n| 1 |",)), CONFIG)
    assert "ragged_table" in route(PageSignals(GOOD_TEXT, ("| a | b |\n| --- | --- |",)), CONFIG)  # No data rows.


def test_garbled():
    text = GOOD_TEXT + "�" * 10
    assert garbled_fraction(text) > 0.05
    assert "garbled" in route(PageSignals(text), CONFIG)


def test_japanese_text_is_not_garbled():
    assert garbled_fraction("SMFポッドの再起動手順について説明します。") == 0.0


def test_image_file_always_routes():
    assert "image_file" in route(PageSignals("", is_image_file=True), CONFIG)


def test_thresholds_are_configurable():
    assert route(PageSignals("short"), RouterConfig(min_chars_per_page=2)) == []
