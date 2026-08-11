"""Classification must survive a FRESH build and the VAT-book ordering.

This is the test that would have caught the design defect that killed the
column approach: vat_book_builder runs init_db() FIRST, then inserts its
mappings from STMAS, then imports with since_days=None. A migration that
UPDATEs product_code_mapping rows therefore updates ZERO rows on a fresh
build, and the codes classify as ordinary stock over the whole history.
A constant cannot be missed that way — these tests pin that.
"""
import models


def _seed_mapping_the_way_vat_book_builder_does(conn, code, name, pid):
    """Mirrors vat_book_builder.seed_products_from_stmas (~:83): a plain
    INSERT with NO is_ignored and NO classification, executed AFTER
    init_db() has already run every migration."""
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit)"
        " VALUES (?, ?, ?, '')", (code, name, pid))
    conn.commit()


def _entry(code, name, doc_no, net):
    return {'date_iso': '2026-06-15', 'doc_no': doc_no, 'product_code_raw': code,
            'product_name_raw': name, 'party': 'วรสวัสดิ์ ฮาร์ดแวร์',
            'party_code': '01อ35', 'qty': 1.0, 'unit': 'ใบ', 'unit_price': net,
            'vat_type': 2, 'discount': '', 'total': net, 'net': net, 'line_seq': 1}


def test_fresh_build_classifies_without_any_migration_touching_the_row(empty_db_conn):
    conn = empty_db_conn
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง','ตัว')"
    ).lastrowid
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
                 " VALUES (?, 'ใบ', 1.0)", (pid,))
    conn.commit()
    _seed_mapping_the_way_vat_book_builder_does(conn, '888ค8888', 'ค่าขนส่ง', pid)

    # Sanity: this row has NEVER been touched by a migration — is_ignored is the
    # column default. A column-based design would classify it as ordinary stock.
    row = conn.execute(
        "SELECT is_ignored FROM product_code_mapping WHERE bsn_code='888ค8888'"
    ).fetchone()
    assert row is not None
    assert row['is_ignored'] == 0

    stats = models.import_weekly(
        [_entry('888ค8888', 'ค่าขนส่ง', 'IV9500-1', 30.0)], 'sales', 'fresh.csv')

    assert stats['non_stock'] == 1, stats
    rev = conn.execute(
        "SELECT net FROM sales_transactions WHERE bsn_code='888ค8888'").fetchall()
    assert len(rev) == 1, rev
    assert rev[0]['net'] == 30.0
    ledger = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
    assert ledger == 0


def test_ordinary_code_on_a_fresh_build_still_moves_stock(empty_db_conn):
    """CONTROL. Without this, the test above could pass because the fresh DB
    cannot post ledger rows at all, rather than because the guard fired."""
    conn = empty_db_conn
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('แผ่นตัด 14 นิ้ว','ตัว')"
    ).lastrowid
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
                 " VALUES (?, 'ใบ', 1.0)", (pid,))
    conn.commit()
    _seed_mapping_the_way_vat_book_builder_does(conn, '036ผ7110', 'แผ่นตัด 14 นิ้ว', pid)

    models.import_weekly(
        [_entry('036ผ7110', 'แผ่นตัด 14 นิ้ว', 'IV9501-1', 80.0)], 'sales', 'fresh.csv')

    ledger = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
    assert ledger == 1, "the control must post a ledger row, or test 1 proves nothing"
