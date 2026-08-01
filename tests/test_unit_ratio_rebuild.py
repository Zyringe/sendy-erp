"""Pin update_unit_conversion_ratio's ledger rebuild + fractional stock counts.

Three defects this file exists to keep dead (review, 2026-08-01):

1. The rebuild DELETEd ledger rows by (product_id, doc_no) but reset
   synced_to_stock only for the edited `bsn_unit`. A doc carrying the SAME
   product in two units therefore lost the other unit's movement for good,
   and the stock_levels re-derivation that ran next HID it by recomputing
   stock from the already-truncated ledger — no drift check could ever see
   it. Three such docs were live (products 591 / 720 / 785, all purchases,
   so WACC's doc_no→purchase-row cursor desynced too).

2. That same re-derivation ran `SELECT product_id, SUM(...) WHERE
   product_id=?` with no GROUP BY, which yields one (NULL, 0) row when the
   product has no ledger rows. NULL on stock_levels' INTEGER PRIMARY KEY (a
   rowid alias) is auto-assigned an unrelated rowid and then dies on the FK
   to products → 500. Six live unit_conversions rows sat on empty ledgers.

3. The manual stock-count route parsed with int(), so a legitimately
   fractional balance (pid 663 held 11.4 กล่อง from real 0.1/0.4-กล่อง BSN
   sales) could not be counted or corrected by hand.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import models

PID = 960001    # unit_type อัน, two BSN units on one doc
EMPTY = 960002  # unit_type อัน, has a conversion but an empty ledger
OTHER = 960003  # unit_type อัน, an UNRELATED product with a pending row


def _seed(conn):
    # batch_id is left NULL (it FKs import_log, which these rows never
    # populate) so the seed needs no FK games — models.* opens its own
    # connections with foreign_keys=ON and must stay happy.
    for pid, name in ((PID, 'TWOUNIT'), (EMPTY, 'NOLEDGER'), (OTHER, 'UNRELATED')):
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, sku_code, is_active)"
            " VALUES (?, ?, 'อัน', ?, 1)", (pid, name, f'SKU-{pid}'))
    conn.commit()


def _purchase(conn, doc, pid, qty, unit):
    conn.execute(
        "INSERT INTO purchase_transactions"
        " (batch_id,date_iso,doc_no,doc_base,product_id,bsn_code,product_name_raw,"
        "  supplier,supplier_code,qty,unit,unit_price,vat_type,discount,total,net,"
        "  synced_to_stock)"
        " VALUES (NULL,'2025-01-01',?,?,?,'C1','raw','S','S1',?,?,10,0,0,0,0,0)",
        (doc, doc, pid, qty, unit))


def _mkt_sale(conn, doc, pid, qty, unit, customer='หน้าร้านS'):
    """A marketplace sale — the customer name is what makes _sync_bsn_to_stock
    deduct platform_skus.stock (PLATFORM_STOCK_DEDUCT_CUSTOMERS)."""
    conn.execute(
        "INSERT INTO sales_transactions"
        " (batch_id,date_iso,doc_no,doc_base,product_id,bsn_code,product_name_raw,"
        "  customer,customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
        "  synced_to_stock)"
        " VALUES (NULL,'2025-01-01',?,?,?,'C1','raw',?,'S1',?,?,10,0,0,0,0,0)",
        (doc, doc, pid, customer, qty, unit))


def _platform_stock(conn, pid=PID):  # noqa: D401
    return conn.execute(
        "SELECT stock FROM platform_skus WHERE internal_product_id=?", (pid,)
    ).fetchone()['stock']


def _stock(conn, pid=PID):
    """Read stock_levels directly — an independent signal, not the app's own
    accessor that the code under test also uses."""
    row = conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return None if row is None else row['quantity']


def _bsn_ledger(conn, pid=PID):
    return conn.execute(
        "SELECT reference_no, quantity_change FROM transactions"
        " WHERE product_id=? AND note LIKE 'BSN%' ORDER BY id", (pid,)).fetchall()


@pytest.fixture
def admin_client(empty_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 't'
        sess['role'] = 'admin'
    return c


def test_ratio_edit_keeps_the_other_unit_on_the_same_doc(empty_db_conn):
    """Defect 1: one purchase doc, same product, two units. Editing one
    unit's ratio must not delete the other unit's movement."""
    _seed(empty_db_conn)
    _purchase(empty_db_conn, 'RR001', PID, 2, 'โหล')    # 2 × 12 = 24 อัน
    _purchase(empty_db_conn, 'RR001', PID, 120, 'อัน')  # unit == unit_type → 120 อัน
    empty_db_conn.commit()

    models.save_unit_conversions([{'product_id': PID, 'bsn_unit': 'โหล', 'ratio': 12}])
    assert _stock(empty_db_conn) == 144
    assert len(_bsn_ledger(empty_db_conn)) == 2

    assert models.update_unit_conversion_ratio(PID, 'โหล', 6) == {'ok': True}

    # 2 โหล now converts to 12 อัน; the 120 อัน row is untouched.
    ledger = _bsn_ledger(empty_db_conn)
    assert sorted(r['quantity_change'] for r in ledger) == [12, 120]
    assert _stock(empty_db_conn) == 132
    # And every source row that was synced is still synced — none stranded.
    assert empty_db_conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions"
        " WHERE product_id=? AND synced_to_stock=0", (PID,)).fetchone()[0] == 0


def test_ratio_edit_on_a_product_with_an_empty_ledger(empty_db_conn):
    """Defect 2: no transactions at all must not 500 or mint a phantom row."""
    _seed(empty_db_conn)
    models.save_unit_conversions([{'product_id': EMPTY, 'bsn_unit': 'โหล', 'ratio': 12}])

    assert models.update_unit_conversion_ratio(EMPTY, 'โหล', 6) == {'ok': True}

    assert empty_db_conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit='โหล'",
        (EMPTY,)).fetchone()['ratio'] == 6
    orphans = empty_db_conn.execute(
        "SELECT COUNT(*) FROM stock_levels"
        " WHERE product_id NOT IN (SELECT id FROM products)").fetchone()[0]
    assert orphans == 0


def test_replay_does_not_re_deduct_platform_stock(empty_db_conn):
    """Defect 4: deleting a ledger row does NOT put back the platform_skus.stock
    that posting it took off, so a rebuild must not run that deduction again.
    The deduction belongs to a row FIRST becoming synced (an import), not to a
    replay. 327 products / 5,220 synced marketplace sales rows were exposed."""
    _seed(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO platform_skus (platform, product_name, variation_id,"
        " internal_product_id, qty_per_sale, stock)"
        " VALUES ('shopee','listing','V1',?,1,100)", (PID,))
    _mkt_sale(empty_db_conn, 'IV001', PID, 1, 'โหล')   # 1 โหล = 12 อัน
    empty_db_conn.commit()

    models.save_unit_conversions([{'product_id': PID, 'bsn_unit': 'โหล', 'ratio': 12}])
    # The import-time deduction is correct and must still happen.
    assert _platform_stock(empty_db_conn) == 88

    assert models.update_unit_conversion_ratio(PID, 'โหล', 12) == {'ok': True}

    assert _platform_stock(empty_db_conn) == 88, 'replay re-deducted platform stock'
    assert _stock(empty_db_conn) == -12


def test_a_newly_synced_row_cannot_compensate_for_a_lost_one(empty_db_conn):
    """Defect 5 (Codex, 2026-08-01): the rebuild guard compared synced-row
    COUNTS, so a row that first-syncs during the replay masks one that can no
    longer replay. Setup: a โหล purchase already on the ledger whose conversion
    was later deleted by an ad-hoc script, plus a still-pending กล่อง purchase.
    Editing the กล่อง ratio takes the โหล movement out and puts a กล่อง one in;
    before=1, after=1, so a count-based guard commits the loss."""
    _seed(empty_db_conn)
    _purchase(empty_db_conn, 'RR001', PID, 1, 'โหล')
    empty_db_conn.commit()
    models.save_unit_conversions([{'product_id': PID, 'bsn_unit': 'โหล', 'ratio': 12}])
    assert _stock(empty_db_conn) == 12

    # The โหล ratio is deleted (scripts/ do this), stranding its synced row.
    empty_db_conn.execute(
        "DELETE FROM unit_conversions WHERE product_id=? AND bsn_unit='โหล'", (PID,))
    # A กล่อง purchase arrives and its conversion exists, but nothing has
    # synced it yet — the row is still pending.
    _purchase(empty_db_conn, 'RR002', PID, 1, 'กล่อง')
    empty_db_conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,'กล่อง',10)",
        (PID,))
    empty_db_conn.commit()

    result = models.update_unit_conversion_ratio(PID, 'กล่อง', 10)

    assert 'error' in result, f'guard let the โหล movement vanish: {result}'
    assert _stock(empty_db_conn) == 12, 'stock changed despite the bail'
    assert empty_db_conn.execute(
        "SELECT synced_to_stock FROM purchase_transactions WHERE doc_no='RR001'"
    ).fetchone()['synced_to_stock'] == 1


def test_replay_does_not_sweep_in_another_products_pending_row(empty_db_conn):
    """Defect 6 (Codex, 2026-08-01): _sync_bsn_to_stock scans EVERY unsynced
    mapped row, so a replay (deduct_platform=False) would first-sync an
    unrelated product's pending marketplace sale and skip its platform-stock
    deduction — the deduction that row is owed and will never get again."""
    _seed(empty_db_conn)
    _purchase(empty_db_conn, 'RR001', PID, 1, 'โหล')
    empty_db_conn.commit()
    models.save_unit_conversions([{'product_id': PID, 'bsn_unit': 'โหล', 'ratio': 12}])

    # OTHER's marketplace sale lands AFTER that (unit == unit_type, so it needs
    # no conversion and will sync the moment anything scans for pending rows).
    # It is still owed its platform deduction when the replay below runs.
    empty_db_conn.execute(
        "INSERT INTO platform_skus (platform, product_name, variation_id,"
        " internal_product_id, qty_per_sale, stock)"
        " VALUES ('shopee','other','V9',?,1,100)", (OTHER,))
    _mkt_sale(empty_db_conn, 'IV900', OTHER, 5, 'อัน')
    empty_db_conn.commit()

    assert models.update_unit_conversion_ratio(PID, 'โหล', 6) == {'ok': True}

    row = empty_db_conn.execute(
        "SELECT synced_to_stock FROM sales_transactions WHERE doc_no='IV900'").fetchone()
    assert row['synced_to_stock'] == 0, "replay swept in another product's row"
    assert _platform_stock(empty_db_conn, OTHER) == 100


def test_ratio_routes_reject_a_non_finite_ratio(admin_client, empty_db_conn):
    """Defect 7 (Codex, 2026-08-01): `ratio > 0` is True for inf, so both the
    save and the edit path would store it and multiply it into the ledger."""
    _seed(empty_db_conn)
    _purchase(empty_db_conn, 'RR001', PID, 1, 'โหล')
    empty_db_conn.commit()

    admin_client.post('/unit-conversions/save',
                      data={f'ratio_{PID}_โหล': 'inf'})
    assert empty_db_conn.execute(
        "SELECT COUNT(*) FROM unit_conversions WHERE product_id=?", (PID,)
    ).fetchone()[0] == 0

    models.save_unit_conversions([{'product_id': PID, 'bsn_unit': 'โหล', 'ratio': 12}])
    admin_client.post('/unit-conversions/edit',
                      data={'product_id': PID, 'bsn_unit': 'โหล', 'ratio': 'inf'})

    assert empty_db_conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=?", (PID,)
    ).fetchone()['ratio'] == 12
    assert _stock(empty_db_conn) == 12


def test_stock_adjust_accepts_a_fractional_count(admin_client, empty_db_conn):
    """Defect 3: a fractional balance must be countable by hand."""
    _seed(empty_db_conn)
    models.add_transaction(PID, 'IN', 12, 'unit', note='seed')

    resp = admin_client.post(f'/products/{PID}/adjust',
                             data={'new_quantity': '11.4', 'reason': 'count'})

    assert resp.status_code == 302
    assert _stock(empty_db_conn) == pytest.approx(11.4)
    adj = empty_db_conn.execute(
        "SELECT quantity_change FROM transactions"
        " WHERE product_id=? AND txn_type='ADJUST'", (PID,)).fetchone()
    # -0.6 exactly, not the -0.5999999999999996 a raw float subtraction gives.
    assert adj['quantity_change'] == -0.6
