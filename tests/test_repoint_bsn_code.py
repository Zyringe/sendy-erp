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

def _opening(conn, pid, qty, created_at='2024-01-03 00:00:00'):
    """An opening balance, dated BEFORE the movements — the shape real data has
    (every ยอดยกมา row on prod is stamped 2024-01-03). Letting it default to
    `now` would put the opening AFTER the history and make the whole fixture
    behave like a product that starts at zero."""
    conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change, "
        "unit_mode, note, created_at) VALUES (?, 'ADJUST', ?, 'unit', 'ยอดยกมา', ?)",
        (pid, qty, created_at),
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


def test_compensation_precedes_an_IN_that_shares_the_head_timestamp(empty_db_conn):
    """Backdating must clear the head STRICTLY, not tie with it.

    models/wacc.py orders `created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1
    END, id` — so an IN stamped with the same timestamp as the compensation
    sorts FIRST and is costed against the deficit the compensation exists to
    remove. 21 products in the live catalogue have an IN at their earliest
    timestamp, so the tie is reachable, and asserting only `created_at ==
    head` would pass while the replay is still wrong.
    """
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old product', 'ชิ้น')
    NEW = _product(conn, 'New product', 'ชิ้น')
    CODE = 'ZHEADIN1'
    _mapping(conn, CODE, OLD)
    _opening(conn, OLD, 30)
    # The destination's very first transaction is a PURCHASE, at the same
    # timestamp the head of the affected ledger will resolve to.
    _purchase(conn, 'ZHP0', NEW, 'ZOTHER01', qty=4, unit='ชิ้น', net=40,
              date_iso='2024-01-03')
    _mapping(conn, 'ZOTHER01', NEW)
    _purchase(conn, 'ZHP1', OLD, CODE, qty=5, unit='ชิ้น', net=50)
    _sale(conn, 'ZHS1', OLD, CODE, qty=8, unit='ชิ้น')
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()

    models.repoint_bsn_code(conn, CODE, NEW, preserve_stock=True)
    conn.commit()

    comp = conn.execute(
        "SELECT created_at FROM transactions WHERE product_id=? AND note LIKE 'ปรับยอดยกมา:%'",
        (NEW,)).fetchone()['created_at']
    others = [r['created_at'] for r in conn.execute(
        "SELECT created_at FROM transactions WHERE product_id=? AND note NOT LIKE 'ปรับยอดยกมา:%'",
        (NEW,))]
    assert others, 'fixture must give NEW some history to precede'
    assert all(comp < o for o in others), (
        f"compensation {comp} must be STRICTLY before every other row {sorted(set(others))}"
    )
    # And the replay never sees a phantom deficit on the destination.
    assert not conn.execute(
        "SELECT 1 FROM product_cost_ledger WHERE product_id=? AND stock_after < 0",
        (NEW,)).fetchone()


def test_orphan_detector_uses_purchase_line_provenance(empty_db_conn):
    """With mig-148 provenance, a stranded purchase row is caught even when the
    product it stranded on still carries ANOTHER line of the same document —
    the blind spot the doc+product fallback has."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old product', 'ชิ้น')
    NEW = _product(conn, 'New product', 'ชิ้น')
    CODE, OTHER = 'ZPROV001', 'ZPROV002'
    _mapping(conn, CODE, OLD)
    _mapping(conn, OTHER, OLD)
    # Both lines of doc ZPD1 post onto OLD.
    _purchase(conn, 'ZPD1', OLD, CODE, qty=5, unit='ชิ้น', net=50)
    _purchase(conn, 'ZPD1', OLD, OTHER, qty=3, unit='ชิ้น', net=30)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    assert models._bsn_code_ledger_orphans(conn, CODE) == 0

    # Old-buggy-script shape: move only CODE's source row. OLD still holds
    # OTHER's line on the same doc, so the doc+product fallback alone would
    # report a clean 0 — provenance must still see the stranding.
    conn.execute("UPDATE purchase_transactions SET product_id=? WHERE bsn_code=?",
                 (NEW, CODE))
    conn.commit()

    assert models._bsn_code_ledger_orphans(conn, CODE) == 1


def test_repoint_owns_its_transaction_when_conn_is_none(empty_db, monkeypatch):
    """The web route (blueprints/bsn.py) calls this with conn=None, so the
    function opens, locks, commits and closes its own connection. That path is
    where BEGIN IMMEDIATE has to live: stock_before is read at the top and the
    compensating ADJUST is written near the bottom, and Railway runs
    gunicorn -w 2, so a second worker moving stock in between would otherwise
    be silently reverted by the compensation."""
    import sqlite3

    import database
    import models

    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))

    setup = sqlite3.connect(str(empty_db))
    setup.row_factory = sqlite3.Row
    OLD, NEW, MOVE = _preserve_stock_fixture(setup)
    setup.commit()
    models._sync_bsn_to_stock(setup, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(setup, 'purchase_transactions', 'purchase')
    setup.commit()
    before = (_stock(setup, OLD), _stock(setup, NEW))
    setup.close()

    report = models.repoint_bsn_code(None, MOVE, NEW, preserve_stock=True)

    assert report['stock_adjustments'] == {OLD: -3, NEW: 3}
    # Committed and closed by the function itself — re-open to confirm it stuck.
    check = sqlite3.connect(str(empty_db))
    check.row_factory = sqlite3.Row
    try:
        assert (_stock(check, OLD), _stock(check, NEW)) == before
        assert check.execute(
            "SELECT product_id FROM product_code_mapping WHERE bsn_code=?", (MOVE,)
        ).fetchone()['product_id'] == NEW
    finally:
        check.close()


def test_repoint_takes_the_write_lock_before_it_reads_stock(empty_db, monkeypatch):
    """BEGIN IMMEDIATE must be held from BEFORE the first read that feeds the
    decision — not merely by the time the first write happens.

    stock_before is read at the top and the compensating ADJUST that restores
    it is written near the bottom. Under gunicorn -w 2 a second worker can move
    stock in that window, and with preserve_stock the compensation would then
    "restore" a level a legitimate sale had already changed — silently
    reverting it. The seam here is deliberately EARLY (before any write): a
    deferred transaction holds no lock yet at that point, so this test can tell
    BEGIN IMMEDIATE apart from "a write happened to escalate the lock later".
    """
    import sqlite3

    import database
    import models
    from models import mapping as mapping_mod

    monkeypatch.setattr(database, 'DATABASE_PATH', str(empty_db))

    setup = sqlite3.connect(str(empty_db))
    setup.row_factory = sqlite3.Row
    OLD, NEW, MOVE = _preserve_stock_fixture(setup)
    setup.commit()
    models._sync_bsn_to_stock(setup, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(setup, 'purchase_transactions', 'purchase')
    setup.commit()
    before = (_stock(setup, OLD), _stock(setup, NEW))
    setup.close()

    seen = {}
    real = mapping_mod.missing_unit_ratios

    def spy(conn, new_pid, rows):
        # A short timeout makes "locked out" a fast, certain answer.
        other = sqlite3.connect(str(empty_db), timeout=0.1)
        try:
            other.execute(
                "INSERT INTO transactions (product_id, txn_type, quantity_change,"
                " unit_mode, note) VALUES (?, 'OUT', -1, 'unit', 'interloper')",
                (OLD,))
            other.commit()
            seen['excluded'] = False
        except sqlite3.OperationalError as e:
            seen['excluded'] = 'locked' in str(e).lower()
        finally:
            other.close()
        return real(conn, new_pid, rows)

    monkeypatch.setattr(mapping_mod, 'missing_unit_ratios', spy)
    models.repoint_bsn_code(None, MOVE, NEW, preserve_stock=True)

    assert seen.get('excluded') is True, (
        "a concurrent writer got in before repoint_bsn_code had read stock — "
        "the write lock is not spanning the check-then-write"
    )
    check = sqlite3.connect(str(empty_db))
    check.row_factory = sqlite3.Row
    try:
        assert (_stock(check, OLD), _stock(check, NEW)) == before
    finally:
        check.close()


def _new_destination_fixture(conn):
    """The pid-2054 shape: a BRAND-NEW destination whose incoming history nets
    POSITIVE while its real stock is 0, so the compensation is negative and
    larger than anything the destination can absorb at the head of its ledger."""
    OLD = _product(conn, 'Old blended product', 'ชิ้น')
    NEW = _product(conn, 'Brand new product', 'ชิ้น')   # no ledger of its own
    MOVE, STAY = 'ZNEWD001', 'ZNEWD002'
    _mapping(conn, MOVE, OLD)
    _mapping(conn, STAY, OLD)
    _opening(conn, OLD, 40)
    # MOVE's own history nets +30 (bought 40, sold 10) but NEW really holds 0.
    _purchase(conn, 'ZND1', OLD, MOVE, qty=40, unit='ชิ้น', net=400)   # 10.00 each
    _sale(conn, 'ZND2', OLD, MOVE, qty=10, unit='ชิ้น')
    _purchase(conn, 'ZND3', OLD, STAY, qty=5, unit='ชิ้น', net=100)
    return OLD, NEW, MOVE


def test_compensation_does_not_freeze_a_new_destination_wacc(empty_db_conn):
    """A negative compensation must not be backdated into a destination that
    cannot absorb it — doing so starts the replay under water, every purchase
    hits models/wacc.py's `current_stock < 0` freeze, and cost_price is left at
    0.00 despite real priced purchases.

    Measured on prod 2026-08-03: pid 2054 (แปรงทาสีขนดำ KP 1/2in) came out of
    exactly this shape with cost 0.00 and two negative cost-ledger events, from
    three purchases totalling ฿453. Repaired by hand to ฿5.8611; this pins the
    code so the next re-point cannot repeat it.
    """
    import models

    conn = empty_db_conn
    OLD, NEW, MOVE = _new_destination_fixture(conn)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    before = (_stock(conn, OLD), _stock(conn, NEW))

    report = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()

    assert (_stock(conn, OLD), _stock(conn, NEW)) == before, 'stock must not move'
    assert sum(report['stock_adjustments'].values()) == 0

    assert NEW in report['compensations_moved_late'], (
        'the relocation must be reported, not silent — remap_bsn_code.py prints it'
    )
    cost = conn.execute("SELECT cost_price FROM products WHERE id=?", (NEW,)).fetchone()['cost_price']
    assert cost > 0, (
        f"destination cost_price froze at {cost} — the compensation was placed "
        f"where it drove the WACC replay below zero"
    )
    assert cost == pytest.approx(10.0), 'the only priced lot was 40 @ 10.00'
    assert not conn.execute(
        "SELECT 1 FROM product_cost_ledger WHERE product_id=? AND stock_after < 0",
        (NEW,)).fetchone(), 'no purchase may be costed while stock is negative'


def test_a_positive_compensation_is_never_relocated(empty_db_conn):
    """The late-placement fallback must consider NEGATIVE compensations only.

    A positive compensation at the head can only raise the running balance, so
    it cannot be what put a purchase under water — but a destination whose own
    sales predate its own purchases already has such a purchase. Letting the
    fallback fire on that evidence strips the help the backdating provides and
    re-creates the original deficit: measured while building this, it moved pid
    1795's cost_price from 17.5898 to 16.0311.
    """
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old product', 'ชิ้น')
    NEW = _product(conn, 'Destination', 'ชิ้น')
    MOVE, OWN = 'ZPOSC001', 'ZPOSC002'
    _mapping(conn, MOVE, OLD)
    _mapping(conn, OWN, NEW)
    _opening(conn, OLD, 60)
    # NEW's OWN history sells before it buys, so its ledger carries a purchase
    # costed at negative stock no matter where the compensation goes.
    _sale(conn, 'ZPC1', NEW, OWN, qty=9, unit='ชิ้น', date_iso='2024-02-01')
    _purchase(conn, 'ZPC2', NEW, OWN, qty=6, unit='ชิ้น', net=60, date_iso='2024-03-01')
    _purchase(conn, 'ZPC3', OLD, MOVE, qty=10, unit='ชิ้น', net=200, date_iso='2024-04-01')
    _sale(conn, 'ZPC4', OLD, MOVE, qty=25, unit='ชิ้น', date_iso='2024-05-01')
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    before = (_stock(conn, OLD), _stock(conn, NEW))

    report = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()

    assert report['stock_adjustments'][NEW] > 0, 'fixture must give NEW a positive compensation'
    assert NEW not in report['compensations_moved_late'], (
        'a positive compensation must stay at the head even when the product '
        'already has a purchase costed at negative stock'
    )
    row = conn.execute(
        "SELECT created_at FROM transactions WHERE product_id=? AND note LIKE 'ปรับยอดยกมา:%'",
        (NEW,)).fetchone()
    today = conn.execute("SELECT date('now','localtime') d").fetchone()['d']
    assert not row['created_at'].startswith(today), 'positive compensation was relocated to today'
    assert (_stock(conn, OLD), _stock(conn, NEW)) == before


def test_a_negative_compensation_is_not_relocated_on_someone_elses_freeze(empty_db_conn):
    """The relocation must be attributable, not merely correlated.

    A product can already carry a purchase costed at negative stock for reasons
    that predate the re-point. Relocating on that evidence is NOT harmless:
    lifting the ADJUST off the ledger head raises current_stock for the whole
    replay, so every later purchase blends at a different weight and cost_price
    moves. The measure has to be "does moving THIS row fix one", not "does a
    frozen purchase exist".
    """
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old product', 'ชิ้น')
    NEW = _product(conn, 'Destination', 'ชิ้น')
    MOVE, OWN = 'ZATTR001', 'ZATTR002'
    _mapping(conn, MOVE, OLD)
    _mapping(conn, OWN, NEW)
    _opening(conn, OLD, 80)
    _opening(conn, NEW, 200)
    # NEW's OWN history sells before it buys — a frozen purchase the re-point
    # has nothing to do with, and one a small negative compensation cannot fix.
    _sale(conn, 'ZAT1', NEW, OWN, qty=260, unit='ชิ้น', date_iso='2024-02-01')
    _purchase(conn, 'ZAT2', NEW, OWN, qty=20, unit='ชิ้น', net=200, date_iso='2024-03-01')
    _purchase(conn, 'ZAT3', NEW, OWN, qty=40, unit='ชิ้น', net=800, date_iso='2024-06-01')
    # The moving code nets POSITIVE into NEW, so NEW's compensation is negative.
    _purchase(conn, 'ZAT4', OLD, MOVE, qty=30, unit='ชิ้น', net=300, date_iso='2024-04-01')
    _sale(conn, 'ZAT5', OLD, MOVE, qty=10, unit='ชิ้น', date_iso='2024-05-01')
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    before = (_stock(conn, OLD), _stock(conn, NEW))

    report = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()

    assert report['stock_adjustments'][NEW] < 0, 'fixture must give NEW a negative compensation'
    assert conn.execute(
        "SELECT COUNT(*) c FROM product_cost_ledger WHERE product_id=?"
        " AND event_type='PURCHASE' AND stock_after - qty_change < 0", (NEW,)
    ).fetchone()['c'] > 0, 'fixture must leave a frozen purchase the move cannot fix'
    assert NEW not in report['compensations_moved_late'], (
        'relocated on a freeze it did not cause — that silently re-weights every '
        'later purchase in the WACC replay'
    )
    row = conn.execute(
        "SELECT created_at FROM transactions WHERE product_id=? AND note LIKE 'ปรับยอดยกมา:%'",
        (NEW,)).fetchone()
    today = conn.execute("SELECT date('now','localtime') d").fetchone()['d']
    assert not row['created_at'].startswith(today), 'compensation must still sit at the head'
    assert (_stock(conn, OLD), _stock(conn, NEW)) == before


def test_relocated_compensation_survives_a_re_run(empty_db_conn):
    """Re-running a repoint whose compensation was relocated must leave it
    where it was — not re-post it at the head and freeze the WACC again.

    Codex flagged this as untested on #357. It holds by construction (the
    second run finds stock already correct, so `delta` is 0 and the 6b loop
    never runs) but "by construction" is the claim, not the evidence.
    """
    import models

    conn = empty_db_conn
    OLD, NEW, MOVE = _new_destination_fixture(conn)
    conn.commit()
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    before = (_stock(conn, OLD), _stock(conn, NEW))

    r1 = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()
    assert NEW in r1['compensations_moved_late'], 'fixture must trigger a relocation'
    cost1 = conn.execute("SELECT cost_price FROM products WHERE id=?", (NEW,)).fetchone()['cost_price']
    stamp1 = conn.execute(
        "SELECT created_at FROM transactions WHERE product_id=? AND note LIKE 'ปรับยอดยกมา:%'",
        (NEW,)).fetchone()['created_at']

    r2 = models.repoint_bsn_code(conn, MOVE, NEW, preserve_stock=True)
    conn.commit()

    assert r2['stock_adjustments'] == {}, 'second run must not compensate again'
    assert r2['compensations_moved_late'] == []
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE product_id=? AND note LIKE 'ปรับยอดยกมา:%'",
        (NEW,)).fetchone()['c'] == 1, 'exactly one compensation row, not stacked'
    assert conn.execute(
        "SELECT created_at FROM transactions WHERE product_id=? AND note LIKE 'ปรับยอดยกมา:%'",
        (NEW,)).fetchone()['created_at'] == stamp1, 'the relocated row must not move back'
    assert conn.execute(
        "SELECT cost_price FROM products WHERE id=?", (NEW,)).fetchone()['cost_price'] == cost1
    assert (_stock(conn, OLD), _stock(conn, NEW)) == before


def test_repoint_survives_a_non_stock_row_sharing_the_source_product(empty_db_conn):
    """Task 9 (coverage sweep) gap: repoint_bsn_code resets synced_to_stock=0
    for EVERY source row on an affected product — not just the bsn_code being
    moved — then deletes and re-syncs that product's whole 'BSN%' ledger
    (docstring: "not just this bsn_code's"). A non-stock billable line
    (888ค8888/ZZZ) sharing OLD's product_id under a DIFFERENT bsn_code sits
    right in that blast radius. It must come out the other side with its
    revenue row intact, unsynced, and ledger-free — the ONLY thing protecting
    it is _sync_bsn_to_stock's own is_non_stock_code guard firing again on
    the replay, since repoint_bsn_code itself has no non-stock-aware code.
    """
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old (mixed)', 'ตัว')
    NEW = _product(conn, 'New', 'ตัว')
    CODE = 'ZNONSTOCK01'
    _mapping(conn, CODE, OLD)
    _sale(conn, 'ZN-S1', OLD, CODE, qty=4, unit='ตัว')

    # The non-stock line: a DIFFERENT bsn_code, same OLD product, real revenue.
    conn.execute(
        "INSERT INTO sales_transactions"
        " (date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,"
        "  customer, customer_code, qty, unit, unit_price, vat_type, discount,"
        "  total, net, synced_to_stock)"
        " VALUES ('2026-06-01','ZN-SHIP1','ZN-SHIP1',?,'888ค8888','ค่าขนส่ง',"
        # unit == OLD's own unit_type ('ตัว') on purpose: _get_base_qty short-
        # circuits on that equality with no unit_conversions lookup at all, so
        # the ONLY thing that can skip this row is the is_non_stock_code guard
        # — a mismatched unit would ALSO skip it via the separate "ratio not
        # defined yet" branch, which would make this test pass for the wrong
        # reason (caught by the break-it-once proof — see task-9-report.md).
        "  'ลูกค้าทดสอบ','C1',1,'ตัว',30.0,0,'',30.0,30.0,0)", (OLD,))
    conn.commit()

    # Pre-existing CORRECT state, same setup every sibling test in this file
    # uses: sync once so OLD's ordinary line has a real ledger row, and
    # confirm the non-stock line is already left unsynced with no ledger
    # (exactly the state _sync_bsn_to_stock's guard leaves it in on import).
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    conn.commit()
    assert conn.execute(
        "SELECT synced_to_stock FROM sales_transactions WHERE doc_no='ZN-SHIP1'"
    ).fetchone()['synced_to_stock'] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE reference_no='ZN-SHIP1'"
    ).fetchone()[0] == 0

    report = models.repoint_bsn_code(conn, CODE, NEW)
    conn.commit()

    assert 'error' not in report, report      # replay must not report a shortfall
    assert report['orphan_rows_after'] == 0

    # The ordinary line actually moved (sanity control — the repoint did work).
    assert conn.execute(
        "SELECT product_id FROM sales_transactions WHERE doc_no='ZN-S1'"
    ).fetchone()['product_id'] == NEW

    # The non-stock row survives untouched on OLD: count first, then value.
    ship_rows = conn.execute(
        "SELECT product_id, net, synced_to_stock FROM sales_transactions"
        " WHERE doc_no='ZN-SHIP1'").fetchall()
    assert len(ship_rows) == 1, ship_rows
    assert ship_rows[0]['product_id'] == OLD, "non-stock row must not move with the code"
    assert ship_rows[0]['net'] == 30.0, "revenue must survive the replay"
    assert ship_rows[0]['synced_to_stock'] == 0, "must stay unsynced"
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE reference_no='ZN-SHIP1'"
    ).fetchone()[0] == 0, "must never gain a ledger row"


def test_repoint_of_a_protected_code_skips_the_unit_ratio_preflight(empty_db_conn):
    """Codex I1: missing_unit_ratios() must not block repointing a non-stock
    billable code (888ค8888/ZZZ) onto a destination with no unit_conversions
    row for its unit. _sync_bsn_to_stock's is_non_stock_code guard means
    these rows never consume a ratio — refusing to repoint them over a
    missing one is refusing a real ค่าขนส่ง/ZZZ line for a conversion
    migration 155 deliberately deleted as unsafe (stock_filters.py).

    Deliberately mismatched unit ('ใบ' vs NEW's unit_type 'ตัว') and NO
    unit_conversions row for NEW — before the I1 fix this raised
    'has no unit_conversions ratio ... Define the ratio first' (see the
    break-it-once note in the fix report)."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old (protected)', 'ตัว')
    NEW = _product(conn, 'New', 'ตัว')     # deliberately NO unit_conversions row
    CODE = '888ค8888'
    _mapping(conn, CODE, OLD)
    conn.execute(
        "INSERT INTO sales_transactions"
        " (date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,"
        "  customer, customer_code, qty, unit, unit_price, vat_type, discount,"
        "  total, net, synced_to_stock)"
        " VALUES ('2026-06-01','ZP-SHIP1','ZP-SHIP1',?,'888ค8888','น้ำหนักเกิน',"
        "  'ลูกค้าทดสอบ','C1',1,'ใบ',30.0,0,'',30.0,30.0,0)", (OLD,))
    conn.commit()

    # Pre-existing CORRECT state: unsynced, no ledger — exactly what import
    # leaves a protected line in.
    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    conn.commit()
    assert conn.execute(
        "SELECT synced_to_stock FROM sales_transactions WHERE doc_no='ZP-SHIP1'"
    ).fetchone()['synced_to_stock'] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE reference_no='ZP-SHIP1'"
    ).fetchone()[0] == 0

    report = models.repoint_bsn_code(conn, CODE, NEW)
    conn.commit()

    assert 'error' not in report, report

    row = conn.execute(
        "SELECT product_id, net, synced_to_stock FROM sales_transactions"
        " WHERE doc_no='ZP-SHIP1'").fetchone()
    assert row['product_id'] == NEW, "the protected row must move with its code"
    assert row['net'] == 30.0, "revenue must survive the repoint"
    assert row['synced_to_stock'] == 0, "must stay unsynced — no ratio was ever needed"

    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE reference_no='ZP-SHIP1'"
    ).fetchone()[0] == 0, "must never gain a ledger row on either product"
    assert conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=?", (CODE,)
    ).fetchone()['product_id'] == NEW


def test_repoint_of_an_ordinary_code_still_refuses_missing_ratio_with_a_protected_sibling(empty_db_conn):
    """I1 fix scope check: the preflight skip is keyed on the bsn_code THIS
    call is repointing, not on whether a protected code happens to exist
    anywhere in the DB. repoint_bsn_code's sales_rows/purchase_rows are
    always scoped to one bsn_code via `WHERE bsn_code=?`
    (_unit_scoped_source_rows in models/mapping.py), so a single call can
    never see a mix of protected + ordinary rows — proven here by adding an
    unrelated protected line on the SAME source product and confirming the
    ordinary code's missing-ratio refusal still fires exactly as before."""
    import models

    conn = empty_db_conn
    OLD = _product(conn, 'Old (mixed)', 'โหล')
    NEW = _product(conn, 'New', 'ชิ้น')     # deliberately NO unit_conversions
    CODE = 'ZORD01'
    _mapping(conn, CODE, OLD)
    _sale(conn, 'ZO-S1', OLD, CODE, qty=3, unit='โหล')

    # Unrelated protected line, same OLD product, different bsn_code — must
    # not leak the I1 skip onto the ordinary repoint below.
    conn.execute(
        "INSERT INTO sales_transactions"
        " (date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,"
        "  customer, customer_code, qty, unit, unit_price, vat_type, discount,"
        "  total, net, synced_to_stock)"
        " VALUES ('2026-06-01','ZO-SHIP1','ZO-SHIP1',?,'888ค8888','น้ำหนักเกิน',"
        "  'ลูกค้าทดสอบ','C1',1,'ตัว',30.0,0,'',30.0,30.0,0)", (OLD,))
    conn.commit()

    with pytest.raises(ValueError, match='unit_conversions'):
        models.repoint_bsn_code(conn, CODE, NEW)
    conn.rollback()

    assert conn.execute(
        "SELECT product_id FROM product_code_mapping WHERE bsn_code=?", (CODE,)
    ).fetchone()['product_id'] == OLD, "refused before any mutation, as before"
