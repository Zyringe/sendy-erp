"""Non-stock billable BSN lines: revenue without a stock ledger.

Design: projects/nonstock-billable-line/design.md v4.
"""
from models.stock_filters import (
    NON_STOCK_BSN_CODES, is_non_stock_code, non_stock_clause, NonStockCodeError)


def test_constant_holds_exactly_the_two_billable_codes():
    assert NON_STOCK_BSN_CODES == frozenset({'ZZZ', '888ค8888'})
    # ค่าVAT is deliberately NOT here — VAT is tax, not revenue.
    assert '888ค8887' not in NON_STOCK_BSN_CODES


def test_is_non_stock_code():
    assert is_non_stock_code('ZZZ') is True
    assert is_non_stock_code('888ค8888') is True
    assert is_non_stock_code('888ค8887') is False
    assert is_non_stock_code('036ผ7110') is False
    assert is_non_stock_code(None) is False
    assert is_non_stock_code('') is False


def test_non_stock_clause_filters_rows(tmp_db_conn):
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    for i, code in enumerate(('036ผ7110', 'ZZZ', '888ค8888', '888ค8887')):
        conn.execute(
            "INSERT INTO sales_transactions"
            " (batch_id, date_iso, doc_no, doc_base, bsn_code, qty, net)"
            " VALUES (1, '2026-06-15', ?, ?, ?, 1, 100)",
            (f'IV900{i}-1', f'IV900{i}', code))
    rows = conn.execute(
        f"SELECT bsn_code FROM sales_transactions WHERE {non_stock_clause()}").fetchall()
    codes = sorted(r['bsn_code'] for r in rows)
    assert len(codes) == 2, codes          # count BEFORE the property
    assert codes == ['036ผ7110', '888ค8887']

    aliased = conn.execute(
        f"SELECT st.bsn_code FROM sales_transactions st WHERE {non_stock_clause('st')}"
    ).fetchall()
    assert len(aliased) == 2


def test_non_stock_code_error_is_a_value_error():
    assert issubclass(NonStockCodeError, ValueError)
