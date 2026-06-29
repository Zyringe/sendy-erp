"""build_sku_code must emit path-safe sku_codes — no '/'.

'/' is a legitimate fraction char in size/series/model (1/2", 5/16", 3/8")
but is a path separator, so it breaks anything that uses sku_code as a folder
or URL component (e.g. the photo tool's <category>/<sku>/raw/ layout). The
builder maps '/'→'-' (Put 2026-06-29; '/'→'-' chosen over '_' because 50
sku_codes already use '_'). Fraction meaning is preserved; nothing reverse-
parses sku_code into fields, so the visual blend with the '-' delimiter is
cosmetic only.
"""
from sku_code_utils import build_sku_code


def test_slash_in_size_becomes_dash():
    sku = build_sku_code({"id": 1, "cat_short_code": "ANC",
                          "brand_short_code": "FAST", "size": "1/2"})
    assert "/" not in sku
    assert sku == "ANC-FAST-1-2"


def test_slash_in_model_becomes_dash():
    sku = build_sku_code({"id": 2, "cat_short_code": "BLT",
                          "brand_short_code": "XBR", "model": "3/8in"})
    assert "/" not in sku
    assert sku == "BLT-XBR-3-8in"


def test_slash_in_ascii_series_becomes_dash():
    sku = build_sku_code({"id": 3, "cat_short_code": "X", "series": "A/B",
                          "model": "5"})
    assert "/" not in sku
    assert sku == "X-A-B-5"


def test_compound_size_slashes_all_replaced():
    # box dimension like 10.7/8x18.1/4x10.1/4 → every '/' becomes '-'
    sku = build_sku_code({"id": 4, "cat_short_code": "BOX",
                          "size": "10.7/8x18.1/4"})
    assert "/" not in sku
    assert sku == "BOX-10.7-8x18.1-4"


def test_no_slash_unchanged():
    sku = build_sku_code({"id": 5, "cat_short_code": "ANC",
                          "brand_short_code": "SD", "model": "#6",
                          "color_code": "SKY"})
    assert sku == "ANC-SD-#6-SKY"
