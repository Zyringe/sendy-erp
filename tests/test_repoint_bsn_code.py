"""Direct unit tests for models.repoint_bsn_code — the root-cause fix for
scripts/remap_bsn_code.py's old bug (that script moved
sales_transactions/purchase_transactions.product_id but never the
`transactions` ledger rows tagged 'BSN%', stranding orphans on the OLD
product; see models.repoint_bsn_code's docstring + decisions/log.md
2026-07-02/07-03).

scripts/test_remap_bsn_code.py covers the CLI wrapper end-to-end; this file
exercises models.repoint_bsn_code directly against a real (schema-accurate,
empty) SQLite DB — no mocks, per project convention.
"""
import pytest


def _product(conn, name, unit_type):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES (?, ?)",
        (name, unit_type),
    )
    return cur.lastrowid


def _mapping(conn, code, pid, bsn_unit=''):
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, is_ignored, bsn_unit) "
        "VALUES (?, ?, ?, 0, ?)",
        (code, code, pid, bsn_unit),
    )


def _sale(conn, doc_no, pid, code, qty, unit, customer='ลูกค้าทดสอบ', date_iso='2026-06-01'):
    conn.execute(
        """
        INSERT INTO sales_transactions
            (date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
             customer, customer_code, qty, unit, unit_price, vat_type, discount,
             total, net, synced_to_stock)
        VALUES (?, ?, ?, ?, ?, 'test', ?, 'C1', ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (date_iso, doc_no, doc_no, pid, code, customer, qty, unit, qty, qty),
    )


def _purchase(conn, doc_no, pid, code, qty, unit, net, date_iso='2026-06-01'):
    conn.execute(
        """
        INSERT INTO purchase_transactions
            (date_iso, doc_no, product_id, bsn_code, product_name_raw,
             supplier, supplier_code, qty, unit, unit_price, vat_type, discount,
             total, net, synced_to_stock)
        VALUES (?, ?, ?, ?, 'test', 'sup', 'sup1', ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (date_iso, doc_no, pid, code, qty, unit, net, net),
    )


def _stock(conn, pid):
    row = conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()
    return row['quantity'] if row else 0


def _bsn_ledger(conn, pid):
    """(note, quantity_change) rows for pid tagged 'BSN%', ordered — used to
    detect duplicates/orphans by direct inspection."""
    return [
        (r['note'], r['quantity_change'])
        for r in conn.execute(
            "SELECT note, quantity_change FROM transactions "
            "WHERE product_id=? AND note LIKE 'BSN%' ORDER BY id",
            (pid,),
        ).fetchall()
    ]


def test_repoint_bsn_code_basic_moves_ledger_and_stock(empty_db_conn):
    """Whole-code repoint: mapping + source rows + ledger all move to the new
    product, unit_conversions ratio is honored (not the raw qty), OLD's
    ledger is fully cleared (no orphan left behind), and WACC is recomputed
    on the new product from the converted (base) qty."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old product', 'โหล')     # bsn_unit=='โหล' matches OLD's own unit_type: ratio 1
    NEW = _product(conn, 'New product', 'ชิ้น')    # needs a unit_conversions row for 'โหล'
    CODE = 'ZBASIC01'
    _mapping(conn, CODE, OLD)
    conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'โหล', 12)",
        (NEW,),
    )
    _sale(conn, 'ZS1', OLD, CODE, qty=3, unit='โหล')
    _purchase(conn, 'ZP1', OLD, CODE, qty=5, unit='โหล', net=500)
    conn.commit()

    # Simulate the pre-existing CORRECT state: these rows are already synced
    # onto OLD (real ledger there) before the repoint.
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    assert _stock(conn, OLD) == 2          # 5 - 3, ratio 1
    assert len(_bsn_ledger(conn, OLD)) == 2

    report = models.repoint_bsn_code(conn, CODE, NEW)
    conn.commit()

    assert conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=?", (CODE,)
    ).fetchone()['product_id'] == NEW
    assert conn.execute(
        "SELECT product_id FROM sales_transactions WHERE doc_no='ZS1'"
    ).fetchone()['product_id'] == NEW
    assert conn.execute(
        "SELECT product_id FROM purchase_transactions WHERE doc_no='ZP1'"
    ).fetchone()['product_id'] == NEW

    # OLD's ledger fully cleared — no orphan left stranded behind.
    assert _bsn_ledger(conn, OLD) == []
    assert _stock(conn, OLD) == 0

    # NEW's ledger rebuilt with the unit_conversions ratio applied (x12), not
    # the raw BSN qty.
    new_ledger = dict(_bsn_ledger(conn, NEW))
    assert new_ledger['BSN ขาย'] == -36     # 3 * 12
    assert new_ledger['BSN ซื้อ'] == 60     # 5 * 12
    assert _stock(conn, NEW) == 24          # 60 - 36

    # WACC recomputed from the converted (base) qty: net / base_qty.
    cost_price = conn.execute(
        "SELECT cost_price FROM products WHERE id=?", (NEW,)
    ).fetchone()['cost_price']
    assert cost_price == pytest.approx(500 / 60)

    assert report['affected_pids'] == sorted([OLD, NEW])
    assert report['stock_before'] == {OLD: 2, NEW: 0}
    assert report['stock_after'] == {OLD: 0, NEW: 24}
    assert report['orphan_rows_after'] == 0


def test_repoint_bsn_code_no_orphans_independent_audit(empty_db_conn):
    """Regression against the real bug — an audit query written FRESH here
    (independent of repoint_bsn_code's own `orphan_rows_after`, per
    verification-discipline) must find 0 stranded ledger rows after a
    repoint, and must be able to actually CATCH the bug pattern when it is
    deliberately reproduced (proves the check has teeth, isn't vacuous)."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old', 'ตัว')
    NEW = _product(conn, 'New', 'ตัว')
    CODE = 'ZORPHAN01'
    _mapping(conn, CODE, OLD)
    _sale(conn, 'ZO-S1', OLD, CODE, qty=7, unit='ตัว')
    _sale(conn, 'ZO-S2', OLD, CODE, qty=2, unit='ตัว')
    _purchase(conn, 'ZO-P1', OLD, CODE, qty=9, unit='ตัว', net=90)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    models.repoint_bsn_code(conn, CODE, NEW)
    conn.commit()

    def _sales_orphans(doc_nos):
        if not doc_nos:
            return 0
        ph = ','.join('?' * len(doc_nos))
        return conn.execute(
            f"""
            SELECT COUNT(*) c FROM transactions t
            WHERE t.note LIKE 'BSN%'
              AND t.reference_no IN ({ph})
              AND NOT EXISTS (
                  SELECT 1 FROM sales_transactions st
                  WHERE st.doc_no = t.reference_no AND st.product_id = t.product_id
              )
            """,
            doc_nos,
        ).fetchone()['c']

    def _purchase_orphans(doc_nos):
        if not doc_nos:
            return 0
        ph = ','.join('?' * len(doc_nos))
        return conn.execute(
            f"""
            SELECT COUNT(*) c FROM transactions t
            WHERE t.note LIKE 'BSN%'
              AND t.reference_no IN ({ph})
              AND NOT EXISTS (
                  SELECT 1 FROM purchase_transactions pt
                  WHERE pt.doc_no = t.reference_no AND pt.product_id = t.product_id
              )
            """,
            doc_nos,
        ).fetchone()['c']

    sales_docs = [r['doc_no'] for r in conn.execute(
        "SELECT doc_no FROM sales_transactions WHERE bsn_code=?", (CODE,))]
    purchase_docs = [r['doc_no'] for r in conn.execute(
        "SELECT doc_no FROM purchase_transactions WHERE bsn_code=?", (CODE,))]

    assert _sales_orphans(sales_docs) == 0
    assert _purchase_orphans(purchase_docs) == 0

    # Prove the check has teeth: deliberately reproduce the OLD bug shape
    # (source row moved back, ledger left on the other product) and confirm
    # the SAME query flags exactly 1 orphan.
    conn.execute("UPDATE sales_transactions SET product_id=? WHERE doc_no='ZO-S1'", (OLD,))
    conn.commit()
    assert _sales_orphans(['ZO-S1']) == 1


def test_repoint_bsn_code_idempotent(empty_db_conn):
    """Re-running with identical args is a no-op: same stock, no duplicate
    ledger rows, still 0 orphans."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old', 'ตัว')
    NEW = _product(conn, 'New', 'ตัว')
    CODE = 'ZIDEMP01'
    _mapping(conn, CODE, OLD)
    _sale(conn, 'ZI-S1', OLD, CODE, qty=4, unit='ตัว')
    _purchase(conn, 'ZI-P1', OLD, CODE, qty=10, unit='ตัว', net=100)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    r1 = models.repoint_bsn_code(conn, CODE, NEW)
    conn.commit()
    stock_1 = {NEW: _stock(conn, NEW), OLD: _stock(conn, OLD)}
    ledger_1 = _bsn_ledger(conn, NEW)

    r2 = models.repoint_bsn_code(conn, CODE, NEW)
    conn.commit()
    stock_2 = {NEW: _stock(conn, NEW), OLD: _stock(conn, OLD)}
    ledger_2 = _bsn_ledger(conn, NEW)

    assert stock_1 == stock_2
    assert ledger_1 == ledger_2
    assert len(ledger_2) == 2   # 1 sale + 1 purchase — no duplicates
    assert r1['orphan_rows_after'] == 0
    assert r2['orphan_rows_after'] == 0


def test_repoint_bsn_code_unit_scoped_split_leaves_sibling_untouched(empty_db_conn):
    """Split code (แผง→A, ตัว→B via two mapping rows, mig 124): repointing
    only the แผง slice to C must leave the ตัว→B mapping row, B's source row,
    B's ledger, and B's stock completely untouched."""
    import models

    conn = empty_db_conn
    A = _product(conn, 'Panel A', 'แผง')
    B = _product(conn, 'Loose B', 'ตัว')
    C = _product(conn, 'Panel C', 'แผง')
    CODE = 'ZSPLIT01'
    _mapping(conn, CODE, A, bsn_unit='แผง')
    _mapping(conn, CODE, B, bsn_unit='ตัว')
    _sale(conn, 'ZSP-S1', A, CODE, qty=2, unit='แผง')
    _sale(conn, 'ZSP-S2', B, CODE, qty=6, unit='ตัว')
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    conn.commit()

    b_stock_before = _stock(conn, B)
    b_ledger_before = _bsn_ledger(conn, B)
    b_row_before = dict(conn.execute(
        "SELECT product_id, synced_to_stock FROM sales_transactions WHERE doc_no='ZSP-S2'"
    ).fetchone())

    report = models.repoint_bsn_code(conn, CODE, C, bsn_unit='แผง')
    conn.commit()

    # แผง slice moved: mapping + source + ledger.
    assert conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=? AND bsn_unit='แผง'", (CODE,)
    ).fetchone()['product_id'] == C
    assert conn.execute(
        "SELECT product_id FROM sales_transactions WHERE doc_no='ZSP-S1'"
    ).fetchone()['product_id'] == C
    assert _stock(conn, A) == 0
    assert _bsn_ledger(conn, A) == []
    assert _stock(conn, C) == -2   # OUT of 2, ratio 1 (unit matches C's unit_type)

    # ตัว slice (B) untouched: mapping row, source row, ledger, stock all
    # byte-identical to before.
    assert conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=? AND bsn_unit='ตัว'", (CODE,)
    ).fetchone()['product_id'] == B
    b_row_after = dict(conn.execute(
        "SELECT product_id, synced_to_stock FROM sales_transactions WHERE doc_no='ZSP-S2'"
    ).fetchone())
    assert b_row_after == b_row_before
    assert _stock(conn, B) == b_stock_before
    assert _bsn_ledger(conn, B) == b_ledger_before

    assert report['affected_pids'] == sorted([A, C])   # B never in scope
    assert report['orphan_rows_after'] == 0

    # resolver also agrees post-repoint.
    assert models._resolve_mapping(conn, CODE, 'แผง') == (C, 0, True)
    assert models._resolve_mapping(conn, CODE, 'ตัว') == (B, 0, True)


def test_repoint_bsn_code_history_import_pairing_not_duplicated(empty_db_conn):
    """Edge case found while designing the fix: a history_import-tagged sale
    creates a paired 'ประวัติขาย (ไม่นับสต็อค)' IN row alongside its 'BSN ขาย'
    OUT (net 0, doesn't touch real stock — see models._sync_bsn_to_stock). If
    the ledger DELETE only matched 'BSN%' and left that pairing row behind, a
    resync would create a SECOND pairing row on the new product: a duplicate.
    """
    import models

    conn = empty_db_conn
    # batch_id='history_import' is a legacy string sentinel (see
    # scripts/reimport_2026_04_28/run.py + models._sync_bsn_to_stock) written
    # by a one-off script that bypassed FK enforcement — the column is
    # declared `INTEGER REFERENCES import_log(id)` but the sentinel is text,
    # so it can only be inserted with foreign_keys OFF (verified: SQLite
    # raises FOREIGN KEY constraint failed otherwise). PRAGMA foreign_keys is
    # a no-op inside a pending transaction, so toggle it OFF here as the very
    # first statement on this connection, before any INSERT opens one.
    conn.execute("PRAGMA foreign_keys = OFF")

    OLD = _product(conn, 'Old', 'ตัว')
    NEW = _product(conn, 'New', 'ตัว')
    CODE = 'ZHIST01'
    _mapping(conn, CODE, OLD)
    conn.execute(
        """
        INSERT INTO sales_transactions
            (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,
             product_name_raw, customer, customer_code, qty, unit, unit_price,
             vat_type, discount, total, net, synced_to_stock)
        VALUES ('history_import', '2026-06-01', 'ZH-S1', 'ZH-S1', ?, ?,
                'test', 'ลูกค้าทดสอบ', 'C1', 4, 'ตัว', 1.0, 0, '', 4, 4, 0)
        """,
        (OLD, CODE),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    conn.commit()

    def _note_counts(pid):
        rows = conn.execute(
            "SELECT note, COUNT(*) c FROM transactions WHERE product_id=? GROUP BY note",
            (pid,),
        ).fetchall()
        return {r['note']: r['c'] for r in rows}

    before = _note_counts(OLD)
    assert before.get('BSN ขาย') == 1
    assert before.get('ประวัติขาย (ไม่นับสต็อค): test') == 1

    models.repoint_bsn_code(conn, CODE, NEW)
    conn.commit()

    assert _note_counts(OLD) == {}                              # nothing stranded on OLD
    after_new = _note_counts(NEW)
    assert after_new.get('BSN ขาย') == 1                        # rebuilt exactly once
    assert after_new.get('ประวัติขาย (ไม่นับสต็อค): test') == 1   # NOT duplicated
    assert _stock(conn, NEW) == 0                                # net 0 (pairing cancels out)


def test_repoint_bsn_code_unknown_new_pid_raises(empty_db_conn):
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old', 'ตัว')
    _mapping(conn, 'ZBADPID', OLD)
    conn.commit()
    with pytest.raises(ValueError):
        models.repoint_bsn_code(conn, 'ZBADPID', 999999)
