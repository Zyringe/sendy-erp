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


# ─────────────────────────────────────────────────────────────────────────────
# _bsn_code_ledger_orphans — a BSN document is a MULTI-LINE bill
# ─────────────────────────────────────────────────────────────────────────────

def test_orphan_detector_ignores_sibling_lines_on_a_shared_document(empty_db_conn):
    """Two codes on ONE doc_no, each correctly posted to its own product.

    Nothing is stranded, so the count must be 0. The pre-2026-08-03 query
    required the source row to match `bsn_code` as well as (doc_no, product),
    which flagged the OTHER code's perfectly-healthy row — making
    repoint_bsn_code's documented "orphan_rows_after must be 0" invariant
    impossible to satisfy for any code that shares a bill (measured on the
    real database: 14 false positives for a single code).
    """
    import models

    conn = empty_db_conn
    A = _product(conn, 'Product A', 'ชิ้น')
    B = _product(conn, 'Product B', 'ชิ้น')
    CODE_A, CODE_B = 'ZSHARE0A', 'ZSHARE0B'
    _mapping(conn, CODE_A, A)
    _mapping(conn, CODE_B, B)
    # One bill, two lines, two products — the ordinary shape of a BSN doc.
    _purchase(conn, 'ZDOC1', A, CODE_A, qty=5, unit='ชิ้น', net=50)
    _purchase(conn, 'ZDOC1', B, CODE_B, qty=7, unit='ชิ้น', net=70)
    _sale(conn, 'ZDOC2', A, CODE_A, qty=2, unit='ชิ้น')
    _sale(conn, 'ZDOC2', B, CODE_B, qty=3, unit='ชิ้น')
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    assert models._bsn_code_ledger_orphans(conn, CODE_A) == 0
    assert models._bsn_code_ledger_orphans(conn, CODE_B) == 0


def test_orphan_detector_still_catches_a_stranded_ledger_row(empty_db_conn):
    """The check must still FAIL on the corruption it exists for: source row
    moved to another product, ledger row left behind on the old one."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old product', 'ชิ้น')
    NEW = _product(conn, 'New product', 'ชิ้น')
    CODE = 'ZSTRAND1'
    _mapping(conn, CODE, OLD)
    _purchase(conn, 'ZDOC3', OLD, CODE, qty=5, unit='ชิ้น', net=50)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    assert models._bsn_code_ledger_orphans(conn, CODE) == 0

    # Reproduce the old buggy script: move ONLY the source row.
    conn.execute("UPDATE purchase_transactions SET product_id=? WHERE bsn_code=?",
                 (NEW, CODE))
    conn.commit()

    assert models._bsn_code_ledger_orphans(conn, CODE) == 1


# ─────────────────────────────────────────────────────────────────────────────
# preserve_stock — opening balances were seeded while the code was mis-attributed
# ─────────────────────────────────────────────────────────────────────────────

def _opening(conn, pid, qty):
    conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change, "
        "unit_mode, note) VALUES (?, 'ADJUST', ?, 'unit', 'ยอดยกมา')",
        (pid, qty),
    )


def _preserve_stock_fixture(conn):
    """OLD carries an opening balance seeded while CODE_MOVE's goods were still
    mis-attributed to it, plus a second code that legitimately stays. The net
    of the moving code is NEGATIVE (bought less than sold on paper) — the real
    shape measured on 532ข6740 and 999ล2030, and the one that drives the
    DESTINATION product negative."""
    OLD = _product(conn, 'Old product', 'ชิ้น')
    NEW = _product(conn, 'New product', 'ชิ้น')
    MOVE, STAY = 'ZMOVE001', 'ZSTAY001'
    _mapping(conn, MOVE, OLD)
    _mapping(conn, STAY, OLD)
    _opening(conn, OLD, 30)
    _purchase(conn, 'ZPM1', OLD, MOVE, qty=5, unit='ชิ้น', net=50)
    _sale(conn, 'ZSM1', OLD, MOVE, qty=8, unit='ชิ้น')
    _purchase(conn, 'ZPS1', OLD, STAY, qty=2, unit='ชิ้น', net=20)
    _sale(conn, 'ZSS1', OLD, STAY, qty=1, unit='ชิ้น')
    return OLD, NEW, MOVE


def test_repoint_preserve_stock_holds_every_level(empty_db_conn):
    import models

    conn = empty_db_conn
    OLD, NEW, MOVE = _preserve_stock_fixture(conn)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    before = (_stock(conn, OLD), _stock(conn, NEW))
    assert before == (28, 0)          # 30 - 3 (moving code) + 1 (staying code)

    report = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()

    assert (_stock(conn, OLD), _stock(conn, NEW)) == before
    assert report['stock_after'] == report['stock_before']
    # The compensation only RE-ALLOCATES: it must not create or destroy stock.
    assert sum(report['stock_adjustments'].values()) == 0
    assert report['stock_adjustments'] == {OLD: -3, NEW: 3}
    # And the history really did move.
    assert conn.execute(
        "SELECT product_id FROM purchase_transactions WHERE bsn_code=?", (MOVE,)
    ).fetchone()['product_id'] == NEW


def test_repoint_without_preserve_stock_drives_destination_negative(empty_db_conn):
    """Pins the DEFAULT (unchanged) behaviour, and documents exactly why
    preserve_stock exists: moving the history alone puts NEW under water."""
    import models

    conn = empty_db_conn
    OLD, NEW, MOVE = _preserve_stock_fixture(conn)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    report = models.repoint_bsn_code(conn, MOVE, NEW)
    conn.commit()

    assert (_stock(conn, OLD), _stock(conn, NEW)) == (31, -3)
    assert report['stock_adjustments'] == {}


def test_preserve_stock_adjust_is_backdated_to_the_head_of_the_ledger(empty_db_conn):
    """The compensation is an OPENING-balance re-allocation, so it must sort
    before every historical movement — not carry today's date.

    Stamping it "now" leaves the destination short for the whole span of
    history, which sends models/wacc.py down its `current_stock < 0` branch and
    FREEZES the weighted average. Measured on the real database before this was
    fixed: pid 1795 ended at a frozen 18.2604 with stock_after -12 in its cost
    ledger; backdated, the same replay walks 16.03 -> 17.15 -> 16.59 -> 17.70.
    """
    import models

    conn = empty_db_conn
    OLD, NEW, MOVE = _preserve_stock_fixture(conn)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()

    for pid in (OLD, NEW):
        rows = conn.execute(
            "SELECT created_at, note FROM transactions WHERE product_id=?"
            " ORDER BY created_at, id", (pid,)
        ).fetchall()
        comp = [r for r in rows if r['note'].startswith('ปรับยอดยกมา:')]
        assert len(comp) == 1, f"pid {pid}: expected exactly one compensation row"
        assert comp[0]['created_at'] == rows[0]['created_at'], (
            f"pid {pid}: compensation must sit at the head of the ledger, "
            f"got {comp[0]['created_at']} vs head {rows[0]['created_at']}"
        )
        # and specifically NOT stamped with today's clock
        assert not comp[0]['created_at'].startswith(
            conn.execute("SELECT date('now','localtime') d").fetchone()['d']
        ), f"pid {pid}: compensation was stamped today instead of backdated"


def test_repoint_refuses_a_destination_that_cannot_convert_the_unit(empty_db_conn):
    """A destination with no ratio for an incoming unit must be refused BEFORE
    anything is written — _sync_bsn_to_stock skips such rows silently, so the
    alternative is a half-applied move whose missing side looks like real
    stock loss (measured: pid 791 went 24 -> -558 exactly this way)."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old product', 'โหล')
    NEW = _product(conn, 'New product', 'ชิ้น')   # deliberately NO unit_conversions
    CODE = 'ZNOCONV1'
    _mapping(conn, CODE, OLD)
    _purchase(conn, 'ZNC1', OLD, CODE, qty=5, unit='โหล', net=500)
    _sale(conn, 'ZNC2', OLD, CODE, qty=3, unit='โหล')
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    before = _stock(conn, OLD)

    with pytest.raises(ValueError, match='unit_conversions'):
        models.repoint_bsn_code(conn, CODE, NEW, preserve_stock=True)
    conn.rollback()

    # Refused before any mutation: mapping and stock are untouched.
    assert conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=?", (CODE,)
    ).fetchone()['product_id'] == OLD
    assert _stock(conn, OLD) == before
    assert _stock(conn, NEW) == 0

    # With the ratio defined, the same call goes through.
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
                 " VALUES (?, 'โหล', 12)", (NEW,))
    conn.commit()
    models.repoint_bsn_code(conn, CODE, NEW, preserve_stock=True)
    conn.commit()
    assert _stock(conn, OLD) == before


def test_preserve_stock_is_idempotent(empty_db_conn):
    """Re-running the same preserve_stock repoint must not stack a second
    compensation. The ADJUST is not tagged 'BSN%', so step 4's ledger wipe
    leaves it in place — the second run has to see stock already correct and
    post nothing."""
    import models

    conn = empty_db_conn
    OLD, NEW, MOVE = _preserve_stock_fixture(conn)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    before = (_stock(conn, OLD), _stock(conn, NEW))

    r1 = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()
    r2 = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()

    assert r1['stock_adjustments'] == {OLD: -3, NEW: 3}
    assert r2['stock_adjustments'] == {}, "second run must not compensate again"
    assert (_stock(conn, OLD), _stock(conn, NEW)) == before
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE note LIKE 'ปรับยอดยกมา:%'"
    ).fetchone()['c'] == 2      # one per product, from the FIRST run only


def test_preserve_stock_with_unit_scoped_slice(empty_db_conn):
    """preserve_stock composed with --bsn-unit: only the named slice moves, and
    the sibling slice's product is left completely alone (not compensated, not
    touched)."""
    import models

    conn = empty_db_conn
    A = _product(conn, 'Pack product', 'แผง')
    B = _product(conn, 'Loose product', 'ตัว')
    C = _product(conn, 'Destination', 'แผง')
    CODE = 'ZSPLIT01'
    _mapping(conn, CODE, A, bsn_unit='แผง')
    _mapping(conn, CODE, B, bsn_unit='ตัว')
    _opening(conn, A, 20)
    _opening(conn, B, 50)
    _purchase(conn, 'ZSP1', A, CODE, qty=10, unit='แผง', net=100)
    _sale(conn, 'ZSS1', A, CODE, qty=14, unit='แผง')
    _purchase(conn, 'ZSP2', B, CODE, qty=30, unit='ตัว', net=60)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    before = {p: _stock(conn, p) for p in (A, B, C)}
    assert before == {A: 16, B: 80, C: 0}       # 20+10-14, 50+30, nothing

    report = models.repoint_bsn_code(conn, CODE, C, bsn_unit='แผง',
                                     preserve_stock=True)
    conn.commit()

    assert {p: _stock(conn, p) for p in (A, B, C)} == before
    assert B not in report['affected_pids'], "the ตัว slice must not be touched"
    assert B not in report['stock_adjustments']
    assert sum(report['stock_adjustments'].values()) == 0
    # the ตัว mapping row still points at B
    assert conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=? AND bsn_unit='ตัว'",
        (CODE,)).fetchone()['product_id'] == B


def test_missing_unit_ratios_rejects_an_unknown_product(empty_db_conn):
    """The CLI dry-run calls this directly, without repoint_bsn_code's own
    product-exists check in front of it — a bad --to must produce a readable
    error, not a bare TypeError on a None row."""
    import models

    with pytest.raises(ValueError, match='not found'):
        models.missing_unit_ratios(empty_db_conn, 999999, [])
