"""document_kind owns "is this a return document?" (SR sales / GR purchase).

The predicate is checked two ways: as a string, and by RUNNING it in sqlite
against the Python twin, so the two can never drift (SQLite LIKE is
case-insensitive for ASCII; the twin must agree).
"""
import sqlite3

import pytest

import document_kind as dk

SALES = [('SR6901001', True), ('SR6901001-3', True), ('IV6901001', False),
         ('IV6901001-1', False), ('HS6901001', False), ('GR6901001', False),
         ('sr6901001', True), ('XSR1', False), ('', False), (None, None)]
PURCHASE = [('GR6901001', True), ('GR6901001-2', True), ('HP6901001', False),
            ('RR6901001', False), ('SR6901001', False), ('gr1', True),
            ('', False), (None, None)]


def _run(sql_frag, value, col='doc_base'):
    c = sqlite3.connect(':memory:')
    c.execute(f'CREATE TABLE t ({col} TEXT)')
    c.execute(f'INSERT INTO t VALUES (?)', (value,))
    return c.execute(f'SELECT {sql_frag} FROM t').fetchone()[0]


@pytest.mark.parametrize('doc,expected', SALES)
def test_sales_sql_and_python_agree(doc, expected):
    got = _run(dk.is_return_sql('', 'sales'), doc)
    assert got == (None if expected is None else int(expected))
    assert dk.is_return(doc, 'sales') is bool(expected)


@pytest.mark.parametrize('doc,expected', PURCHASE)
def test_purchase_sql_and_python_agree(doc, expected):
    got = _run(dk.is_return_sql('', 'purchase'), doc)
    assert got == (None if expected is None else int(expected))
    assert dk.is_return(doc, 'purchase') is bool(expected)


def test_alias_and_column():
    assert dk.is_return_sql('st', 'sales') == "st.doc_base LIKE 'SR%'"
    assert dk.is_return_sql('st', 'sales', col='doc_no') == "st.doc_no LIKE 'SR%'"
    assert dk.is_return_sql('', 'purchase') == "doc_base LIKE 'GR%'"


def test_not_return_is_the_sql_negation_and_keeps_null_semantics():
    assert dk.not_return_sql('s', 'sales') == "s.doc_base NOT LIKE 'SR%'"
    # NULL stays NULL (row falls out of a WHERE) unless null_safe is asked for.
    assert _run(dk.not_return_sql('', 'sales'), None) is None
    assert _run(dk.not_return_sql('', 'purchase', null_safe=True), None) == 1
    assert _run(dk.not_return_sql('', 'purchase', null_safe=True), 'GR1') == 0
    assert dk.not_return_sql('x', 'purchase', null_safe=True) == \
        "COALESCE(x.doc_base, '') NOT LIKE 'GR%'"


def test_hs_is_a_sale_on_both_readings():
    assert dk.is_return('HS6901001', 'sales') is False
    assert _run(dk.is_return_sql('', 'sales'), 'HS6901001') == 0


def test_parse_weekly_return_line_regex_is_unchanged():
    # W3: _SR_DOC_LINE is built from return_prefix(); it must equal the literal it replaced.
    import parse_weekly
    assert parse_weekly._SR_DOC_LINE.pattern == r'\d{2}/\d{2}/\d{2}\s+SR\d'
    assert parse_weekly._SR_DOC_LINE.flags == __import__('re').compile('x').flags
    assert dk.return_prefix('purchase') == 'GR'


def test_side_and_alias_are_required():
    with pytest.raises(TypeError):
        dk.is_return_sql()
    with pytest.raises(TypeError):
        dk.not_return_sql('s')
    with pytest.raises(TypeError):
        dk.is_return('SR1')


def test_python_twin_is_ascii_only_like_sqlite_like():
    # str.upper() would fold U+017F (long s) to 'S'; SQLite LIKE does not.
    assert dk.is_return('\u017fR1', 'sales') is False
    assert _run(dk.is_return_sql('', 'sales'), '\u017fR1') == 0


def test_bad_side_fails_loud():
    with pytest.raises(ValueError):
        dk.is_return_sql('', 'sale')
    with pytest.raises(ValueError):
        dk.is_return('SR1', 'nope')
