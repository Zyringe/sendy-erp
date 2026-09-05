import sqlite3

import pytest

import express_registers


BANK_CHEQUE = {
    'kind': 'received',
    'type_code': 'QR',
    'cheque_no': 'CHQ-1',
    'trn_date_iso': '2026-09-05',
    'cheque_date_iso': '2026-09-05',
    'received_date_iso': '2026-09-05',
    'paid_in_date_iso': None,
    'bank_code': 'KBANK',
    'branch': '001',
    'bank_account': '123-4',
    'party_code': 'C001',
    'party_name': 'ลูกค้า 1',
    'amount': 100.0,
    'charge': 0.0,
    'vat_amount': 0.0,
    'net_amount': 100.0,
    'remaining_amount': 100.0,
    'status_code': 'N',
    'remark': '',
    'ref_doc': 'IV001',
    'ref_no': 'RE001',
    'voucher': 'RV001',
}

ORDER = {
    'so_no': 'SO-OLD',
    'so_date_iso': '2026-09-05',
    'customer_code': 'C001',
    'customer_name': 'ลูกค้า 1',
    'salesperson_code': '00',
    'your_ref': '',
    'pay_terms': 0,
    'delivery_date_iso': None,
    'completed_date_iso': None,
    'total': 100.0,
    'discount_amount': 0.0,
    'vat_amount': 7.0,
    'net_amount': 107.0,
    'status_code': 'N',
}

ORDER_LINE = {
    'so_no': 'SO-OLD',
    'line_seq': 1,
    'product_code': 'P001',
    'product_name': 'สินค้า 1',
    'ordered_qty': 1.0,
    'cancelled_qty': 0.0,
    'remaining_qty': 1.0,
    'unit': 'ตัว',
    'unit_price': 100.0,
    'line_total': 100.0,
}


def _count(db_path, table, entity='BSN'):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            'SELECT COUNT(*) FROM {} WHERE entity = ?'.format(table),
            (entity,),
        ).fetchone()[0]
    finally:
        conn.close()


def _order_numbers(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute(
            'SELECT so_no FROM express_sales_orders ORDER BY so_no')]
    finally:
        conn.close()


def _bank_cheques(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            'SELECT cheque_no, amount, remaining_amount '
            'FROM express_bank_cheques ORDER BY cheque_no').fetchall()
    finally:
        conn.close()


def test_replace_bank_cheques_replaces_and_returns_count(empty_db):
    counts = express_registers.replace(
        'bank_cheques', ([BANK_CHEQUE],), 'BSN', empty_db)
    replacement = dict(
        BANK_CHEQUE,
        cheque_no='CHQ-2',
        amount=250.0,
        remaining_amount=25.0,
    )
    replacement_counts = express_registers.replace(
        'bank_cheques', ([replacement],), 'BSN', empty_db)

    assert counts == (1,)
    assert replacement_counts == (1,)
    assert _count(empty_db, 'express_bank_cheques') == 1
    assert _bank_cheques(empty_db) == [('CHQ-2', 250.0, 25.0)]


def test_empty_replacement_refuses_to_erase_stored_rows(empty_db):
    express_registers.replace(
        'bank_cheques', ([BANK_CHEQUE],), 'BSN', empty_db)

    with pytest.raises(ValueError, match='refusing to erase'):
        express_registers.replace('bank_cheques', ([],), 'BSN', empty_db)

    assert _count(empty_db, 'express_bank_cheques') == 1


def test_multitable_replacement_rolls_back_every_delete_on_bad_record(empty_db):
    express_registers.replace(
        'sales_orders', ([ORDER], [ORDER_LINE]), 'BSN', empty_db)
    new_order = dict(ORDER, so_no='SO-NEW')

    with pytest.raises(KeyError):
        express_registers.replace(
            'sales_orders', ([new_order], [{'so_no': 'SO-BROKEN'}]),
            'BSN', empty_db)

    assert _order_numbers(empty_db) == ['SO-OLD']
    assert _count(empty_db, 'express_sales_order_lines') == 1
