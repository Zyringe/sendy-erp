import os
import sqlite3
from html.parser import HTMLParser

import pytest


os.environ.setdefault('SKIP_DB_INIT', '1')


PRODUCT_ID = 9586004
UNIT_A = 'กล่อง'
UNIT_B = 'โหล'


class _NamedInputParser(HTMLParser):
    def __init__(self, target_name):
        super().__init__()
        self.target_name = target_name
        self.matches = []

    def handle_starttag(self, tag, attrs):
        parsed = dict(attrs)
        if tag == 'input' and parsed.get('name') == self.target_name:
            self.matches.append(parsed)


@pytest.fixture
def admin_client(tmp_db):
    from app import app

    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    client = app.test_client()
    with client.session_transaction() as session:
        session['role'] = 'admin'
        session['username'] = 'x'
        session['user_id'] = 99
    return client


def _seed_pending_rows(tmp_db, units):
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute('PRAGMA foreign_keys = OFF')
        for table in (
            'sales_transactions',
            'purchase_transactions',
            'unit_conversions',
            'transactions',
            'stock_levels',
        ):
            conn.execute(f'DELETE FROM {table} WHERE product_id = ?', (PRODUCT_ID,))
        conn.execute('DELETE FROM products WHERE id = ?', (PRODUCT_ID,))
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, sku_code, is_active) "
            "VALUES (?, 'TEST UNIT CONVERSION BLANK', 'ชิ้น', 'TEST-586-BLANK', 1)",
            (PRODUCT_ID,),
        )
        for index, unit in enumerate(units, start=1):
            doc_no = f'TEST-586-{index}'
            conn.execute(
                "INSERT INTO sales_transactions "
                "(date_iso, doc_no, doc_base, product_id, bsn_code, "
                " product_name_raw, qty, unit, synced_to_stock) "
                "VALUES ('2026-09-19', ?, ?, ?, ?, 'TEST RAW', 1, ?, 0)",
                (doc_no, doc_no, PRODUCT_ID, f'TEST586-{index}', unit),
            )
        conn.commit()
    finally:
        conn.close()


def test_general_ratio_input_does_not_prefill_or_require_value(admin_client, tmp_db):
    _seed_pending_rows(tmp_db, [UNIT_A])

    response = admin_client.get('/unit-conversions')
    parser = _NamedInputParser(f'ratio_{PRODUCT_ID}_{UNIT_A}')
    parser.feed(response.get_data(as_text=True))

    assert len(parser.matches) == 1
    attrs = parser.matches[0]
    assert 'value' not in attrs
    assert 'required' not in attrs
    assert attrs.get('placeholder') == 'เช่น 12'


def test_save_skips_blank_ratio_and_syncs_filled_ratio(admin_client, tmp_db):
    _seed_pending_rows(tmp_db, [UNIT_A, UNIT_A, UNIT_B])

    admin_client.post('/unit-conversions/save', data={
        f'ratio_{PRODUCT_ID}_{UNIT_A}': '',
        f'ratio_{PRODUCT_ID}_{UNIT_B}': '12',
    })

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        saved = conn.execute(
            "SELECT ratio FROM unit_conversions "
            "WHERE product_id = ? AND bsn_unit = ?",
            (PRODUCT_ID, UNIT_B),
        ).fetchone()
        assert saved is not None
        assert saved['ratio'] == pytest.approx(12.0)

        blank = conn.execute(
            "SELECT ratio FROM unit_conversions "
            "WHERE product_id = ? AND bsn_unit = ?",
            (PRODUCT_ID, UNIT_A),
        ).fetchone()
        assert blank is None

        source_state = conn.execute(
            "SELECT COUNT(*) AS row_count, SUM(synced_to_stock) AS synced_count "
            "FROM sales_transactions WHERE product_id = ? AND unit = ?",
            (PRODUCT_ID, UNIT_A),
        ).fetchone()
        assert source_state['row_count'] == 2
        assert source_state['synced_count'] == 0
    finally:
        conn.close()
