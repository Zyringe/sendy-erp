"""Smoke tests for models.get_ap_outstanding().

Asserts:
  - 7 invoices from the live BSN AP snapshot
  - 2 suppliers
  - grand total ฿43,640.72
  - per-supplier subtotals: เซ็นไดเทรดดิ้ง ฿38,504.76 / ศรีไทยเจริญโลหะกิจ ฿5,135.96
  - เซ็นไดเทรดดิ้ง is flagged is_intercompany=True
  - ศรีไทยเจริญโลหะกิจ is flagged is_intercompany=False
"""
import sqlite3

import pytest

import models


def test_ap_outstanding_counts(tmp_db):
    result = models.get_ap_outstanding()
    assert result['n_invoices'] == 7, (
        f"Expected 7 invoices, got {result['n_invoices']}"
    )
    assert result['n_suppliers'] == 2, (
        f"Expected 2 suppliers, got {result['n_suppliers']}"
    )


def test_ap_outstanding_grand_total(tmp_db):
    result = models.get_ap_outstanding()
    assert abs(result['grand_total'] - 43640.72) < 0.01, (
        f"Expected grand total ฿43,640.72, got {result['grand_total']}"
    )


def test_ap_outstanding_per_supplier_subtotals(tmp_db):
    result = models.get_ap_outstanding()
    by_name = {s['supplier_name']: s for s in result['suppliers']}

    assert 'เซ็นไดเทรดดิ้ง จำกัด' in by_name, (
        f"เซ็นไดเทรดดิ้ง not found; suppliers: {list(by_name)}"
    )
    assert 'ศรีไทยเจริญโลหะกิจ' in by_name, (
        f"ศรีไทยเจริญโลหะกิจ not found; suppliers: {list(by_name)}"
    )

    sendai = by_name['เซ็นไดเทรดดิ้ง จำกัด']
    assert abs(sendai['subtotal'] - 38504.76) < 0.01, (
        f"เซ็นไดเทรดดิ้ง subtotal: expected ฿38,504.76, got {sendai['subtotal']}"
    )

    srithai = by_name['ศรีไทยเจริญโลหะกิจ']
    assert abs(srithai['subtotal'] - 5135.96) < 0.01, (
        f"ศรีไทยเจริญโลหะกิจ subtotal: expected ฿5,135.96, got {srithai['subtotal']}"
    )


def test_ap_outstanding_intercompany_flag(tmp_db):
    result = models.get_ap_outstanding()
    by_name = {s['supplier_name']: s for s in result['suppliers']}

    assert by_name['เซ็นไดเทรดดิ้ง จำกัด']['is_intercompany'] is True
    assert by_name['ศรีไทยเจริญโลหะกิจ']['is_intercompany'] is False


def test_ap_outstanding_invoice_fields(tmp_db):
    """Each invoice dict has the required fields."""
    result = models.get_ap_outstanding()
    required = {'supplier_name', 'supplier_type', 'doc_no', 'supplier_invoice_no',
                'doc_date_iso', 'bill_amount', 'paid_amount', 'outstanding_amount',
                'age_days'}
    for inv in result['invoices']:
        missing = required - set(inv)
        assert not missing, f"Invoice missing fields: {missing}"


def test_ap_outstanding_accepts_open_conn(tmp_db):
    """Helper works when called with an already-open connection (caller closes)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    result = models.get_ap_outstanding(conn)
    conn.close()
    assert result['n_invoices'] == 7


def test_ap_outstanding_empty_when_no_data(empty_db):
    """Returns safe zero-state when no AP rows exist."""
    result = models.get_ap_outstanding()
    assert result['snapshot_date'] is None
    assert result['invoices'] == []
    assert result['suppliers'] == []
    assert result['grand_total'] == 0.0
    assert result['n_invoices'] == 0
    assert result['n_suppliers'] == 0
