"""filters.html_text — render marketplace HTML (Lazada descriptions) as
readable plain text for the /ecommerce/product/<id> desc boxes."""
from filters import html_text


def test_block_close_tags_become_newlines_and_entities_unescape():
    assert html_text('<p style="x">ก&amp;ข</p><p>ค</p>') == 'ก&ข\nค'


def test_br_becomes_newline_inline_tags_drop():
    assert html_text('หนึ่ง<br/>สอง <span>สาม</span>') == 'หนึ่ง\nสอง สาม'


def test_plain_text_passthrough_preserves_paragraph_gap():
    # Shopee descriptions are already plain text — blank lines must survive.
    assert html_text('บรรทัด1\n\nบรรทัด2') == 'บรรทัด1\n\nบรรทัด2'


def test_none_and_empty_return_empty_string():
    assert html_text(None) == ''
    assert html_text('') == ''
