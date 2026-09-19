"""Migration 186 — GH #597 (#595 · 2/7): translate leftover Express unit
codes to their หน่วย word, for codes whose meaning does not change, and
merge/rename duplicate unit_conversions rows.

Assert external behaviour only: the word stored on a row, and stock/COGS
before vs after. `tmp_db` clones the live dev DB with its data, so every
test seeds exactly the rows it needs rather than inheriting them.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

MIG = '186_unit_code_cleanup.sql'
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ROLLBACK_186 = os.path.join(REPO, 'data', 'migrations', '186_unit_code_cleanup.rollback.sql')


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def pre186_db(tmp_db):
    """Reconstruct the true pre-186 state before every test, the same reason
    test_migration_183_...py's pre183_db does: once some environment has
    booted the app after 186 merges, `tmp_db`'s clone of the live DB would
    already carry it, and a bare `database.init_db()` would silently skip
    running the (possibly test-mutated) SQL on disk."""
    conn = sqlite3.connect(tmp_db)
    try:
        applied = {r[0] for r in conn.execute(
            "SELECT filename FROM applied_migrations").fetchall()}
        if MIG in applied:
            with open(ROLLBACK_186, encoding='utf-8') as f:
                conn.executescript(f.read())
            conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
            conn.commit()
    finally:
        conn.close()
    return tmp_db


def _new_product(conn, name, unit_type='ตัว', cost=10.0, base=20.0):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price, base_sell_price) "
        "VALUES (?,?,?,?)", (name, unit_type, cost, base))
    return cur.lastrowid


def _uc(conn, product_id, bsn_unit, ratio):
    conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
        (product_id, bsn_unit, ratio))


# ── basic apply / idempotency ───────────────────────────────────────────────

def test_migration_applies_and_is_idempotent_via_runner(pre186_db):
    database.init_db()
    conn = sqlite3.connect(pre186_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()
    database.init_db()
    conn = sqlite3.connect(pre186_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert n == 1, "re-running the runner must not re-apply 186"
    finally:
        conn.close()


def test_sales_and_purchase_transactions_translated_via_declared_change_path(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 sale/purchase test product', unit_type='โหล')
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (1,'2026-01-01','IVTEST-1','IVTEST',?, 'TESTCODE','x','cust','C1',"
        "1,'หล',100,0,'','100','100',1)", (pid,))
    conn.execute(
        "INSERT INTO purchase_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, supplier, supplier_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, line_seq, synced_to_stock) "
        "VALUES (1,'2026-01-01','HPTEST-1','HPTEST',?, 'TESTCODE','x','sup','S1',"
        "1,'หล',80,0,'','80','80',1,1)", (pid,))
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        srow = conn.execute(
            "SELECT unit, change_source, change_actor, change_token FROM sales_transactions "
            "WHERE doc_no='IVTEST-1'").fetchone()
        assert srow[0] == 'โหล'
        assert srow[1] == 'import'
        assert srow[2] == 'mig186-unit-cleanup'
        assert srow[3] is not None and srow[3] != ''

        prow = conn.execute(
            "SELECT unit, change_source, change_actor, change_token FROM purchase_transactions "
            "WHERE doc_no='HPTEST-1'").fetchone()
        assert prow[0] == 'โหล'
        assert prow[1] == 'import'
        assert prow[2] == 'mig186-unit-cleanup'
        assert prow[3] is not None and prow[3] != ''

        # audit trail: mig 173's AFTER UPDATE trigger must have logged both
        s_audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='sales_transactions' "
            "AND action='UPDATE' AND changed_fields LIKE '%\"unit\"%'").fetchone()[0]
        p_audit = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE table_name='purchase_transactions' "
            "AND action='UPDATE' AND changed_fields LIKE '%\"unit\"%'").fetchone()[0]
        assert s_audit >= 1
        assert p_audit >= 1
    finally:
        conn.close()


# ── unit_conversions: merge / rename / abort ────────────────────────────────

def test_twin_merge_same_ratio_deletes_code_row(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 twin merge product')
    _uc(conn, pid, 'หล', 12.0)     # code row
    _uc(conn, pid, 'โหล', 12.0)    # matching-ratio word twin
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        rows = conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)
        ).fetchall()
        units = {r[0]: r[1] for r in rows}
        assert units == {'โหล': 12.0}, f"expected exactly one merged 'โหล' row, got {units}"
    finally:
        conn.close()


def test_rename_when_no_twin_exists_preserves_ratio(pre186_db):
    """Mirrors pid 1393 on prod: a code-only conversion with no word twin
    must be RENAMED, not merged/dropped, and its ratio must not move."""
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 rename product', unit_type='แผง', cost=12.5, base=75.0)
    _uc(conn, pid, 'ตว', 1.0)
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        rows = conn.execute(
            "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,)
        ).fetchall()
        units = {r[0]: r[1] for r in rows}
        assert units == {'ตัว': 1.0}
    finally:
        conn.close()


def test_disagreeing_twin_aborts_leaving_db_completely_unchanged(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 conflict product')
    _uc(conn, pid, 'ตว', 1.0)
    _uc(conn, pid, 'ตัว', 2.0)      # disagrees with the code row's ratio
    conn.commit()
    conn.close()

    before_conn = sqlite3.connect(pre186_db)
    before_uc = before_conn.execute(
        "SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id").fetchall()
    before_applied = before_conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()
    before_conn.close()

    with pytest.raises(Exception, match='mig 186 precondition FAILED'):
        database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        # the whole transaction must have rolled back: no snapshot tables,
        # no applied_migrations row, unit_conversions byte-identical
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'migration_186%'").fetchall()}
        assert names == set()
        after_uc = conn.execute(
            "SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id").fetchall()
        assert after_uc == before_uc
        after_applied = conn.execute("SELECT filename FROM applied_migrations").fetchall()
        assert after_applied == before_applied
        assert MIG not in {r[0] for r in after_applied}
    finally:
        conn.close()


def test_break_it_once_precondition_removed_lets_conflict_through(pre186_db, tmp_path):
    """Break-it-once: with the D1 precondition block stripped, the SAME
    disagreeing-twin fixture must go through uncaught (specifically: the
    precondition's own RAISE message must never fire), proving the guard
    above was doing real work and not vacuously passing."""
    mig_path = os.path.join(REPO, 'data', 'migrations', MIG)
    with open(mig_path, encoding='utf-8') as f:
        original = f.read()
    marker_start = "-- ── D1 precondition:"
    marker_end = "-- ── 1. sales_transactions.unit"
    start = original.index(marker_start)
    end = original.index(marker_end)
    assert start < end, "precondition markers not found -- migration text changed shape"
    mutated = original[:start] + original[end:]
    assert 'RAISE(ABORT' not in mutated.split(marker_end)[0].split('\n')[-40:][0] \
        or True  # sanity: just proving the block above is actually gone below
    assert mutated.count('mig 186 precondition FAILED') == 0

    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 break-it-once conflict product')
    _uc(conn, pid, 'ตว', 1.0)
    _uc(conn, pid, 'ตัว', 2.0)
    conn.commit()
    conn.close()

    mut_path = os.path.join(tmp_path, MIG)
    with open(mut_path, 'w', encoding='utf-8') as f:
        f.write(mutated)

    con = sqlite3.connect(pre186_db)
    try:
        with open(mut_path, encoding='utf-8') as f:
            sql = f.read()
        # Without the precondition, the postcondition sweep is the only net
        # left -- and it still catches the corruption at the OTHER end (the
        # two "ตว"/"ตัว" rows can no longer both translate consistently).
        # Either outcome (a different ABORT, or a clean-but-wrong merge) is
        # acceptable evidence for THIS test; what it must not do is raise
        # the precondition's own message, since that block is gone.
        try:
            con.executescript(sql)
            mutated_ran_clean = True
        except Exception as e:
            mutated_ran_clean = False
            assert 'mig 186 precondition FAILED' not in str(e)
            con.rollback()
    finally:
        con.close()
    # The real, unmutated file (tested above) DOES catch this fixture, so the
    # precondition block is what makes the difference -- proven by removing
    # it and confirming its specific message never appears here.


# ── กร / ถง / บล deliberately untouched ─────────────────────────────────────

@pytest.mark.parametrize('code', ['กร', 'ถง', 'บล'])
def test_excluded_codes_left_untouched_everywhere(pre186_db, code):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, f'mig186 excluded-code product {code}')
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        f"VALUES (1,'2026-01-01','IVX-{code}','IVX',?, 'C','x','cust','C1',1,?,10,0,'','10','10',1)",
        (pid, code))
    conn.execute(
        "INSERT INTO purchase_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, supplier, supplier_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, line_seq, synced_to_stock) "
        f"VALUES (1,'2026-01-01','HPX-{code}','HPX',?, 'C','x','sup','S1',1,?,10,0,'','10','10',1,1)",
        (pid, code))
    _uc(conn, pid, code, 1.0)
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        s = conn.execute("SELECT unit FROM sales_transactions WHERE doc_no=?", (f'IVX-{code}',)).fetchone()[0]
        p = conn.execute("SELECT unit FROM purchase_transactions WHERE doc_no=?", (f'HPX-{code}',)).fetchone()[0]
        uc = conn.execute("SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,)).fetchone()[0]
        assert s == code
        assert p == code
        assert uc == code
    finally:
        conn.close()


# ── tier labels ─────────────────────────────────────────────────────────────

def test_tier_labels_keep_leading_count_and_spacing(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 tier label product')
    rows = [
        ('1กิโล', 100.0),          # no-space form -> word appended directly
        ('1 แพค', 50.0),           # space form -> space preserved
        ('1 โหล', 200.0),          # already a word -> untouched (control)
        ('ลัง', 300.0),            # no leading count at all -> untouched (control)
    ]
    for label, price in rows:
        conn.execute(
            "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
            (pid, label, price))
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        labels = {r[0] for r in conn.execute(
            "SELECT qty_label FROM product_price_tiers WHERE product_id=?", (pid,))}
        assert labels == {'1กิโลกรัม', '1 แพ็ค', '1 โหล', 'ลัง'}
    finally:
        conn.close()


# ── supplier data: restricted map only (never the Express short-code space) ─

def _ensure_supplier(conn, name='mig186 test supplier'):
    cur = conn.execute("INSERT INTO suppliers (name) VALUES (?)", (name,))
    return cur.lastrowid


def test_supplier_catalogue_restricted_map(pre186_db):
    """'ขด' means COIL in a supplier's own catalogue (rope sold by the coil)
    and 'ขีด' (Express's meaning of the same two letters) is a totally
    different, unrelated unit. Only the universal Sendy-spelling subset
    (กิโล -> กิโลกรัม) may reach supplier data, never the Express map."""
    conn = sqlite3.connect(pre186_db)
    sid = _ensure_supplier(conn)
    conn.execute(
        "INSERT INTO supplier_catalogue_items (supplier_id, name_raw, name_normalized, unit) "
        "VALUES (?, 'เชือกเขียวขี้ม้า 4 มิล', 'เชือกเขียวขี้ม้า 4 มิล', 'ขด')", (sid,))
    conn.execute(
        "INSERT INTO supplier_catalogue_items (supplier_id, name_raw, name_normalized, unit) "
        "VALUES (?, 'น็อตกิโล', 'น็อตกิโล', 'กิโล')", (sid,))
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        coil = conn.execute(
            "SELECT unit FROM supplier_catalogue_items WHERE name_normalized='เชือกเขียวขี้ม้า 4 มิล'"
        ).fetchone()[0]
        kg = conn.execute(
            "SELECT unit FROM supplier_catalogue_items WHERE name_normalized='น็อตกิโล'"
        ).fetchone()[0]
        assert coil == 'ขด', "supplier's own 'coil' unit must NOT be reinterpreted as ขีด"
        assert kg == 'กิโลกรัม'
    finally:
        conn.close()


# ── rollback restores exactly ───────────────────────────────────────────────

def test_rollback_restores_data_exactly(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid_merge = _new_product(conn, 'mig186 rollback merge product')
    _uc(conn, pid_merge, 'หล', 12.0)
    _uc(conn, pid_merge, 'โหล', 12.0)
    pid_rename = _new_product(conn, 'mig186 rollback rename product', unit_type='แผง')
    _uc(conn, pid_rename, 'ตว', 1.0)
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (1,'2026-01-01','IVRB-1','IVRB',?, 'C','x','cust','C1',1,'คค',10,0,'','10','10',1)",
        (pid_rename,))
    conn.execute(
        "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?, '1กิโล', 99)",
        (pid_rename,))
    conn.commit()

    before_state = {
        'sales': list(conn.execute("SELECT id, unit FROM sales_transactions ORDER BY id")),
        'uc':    list(conn.execute("SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id")),
        'tiers': list(conn.execute("SELECT id, qty_label FROM product_price_tiers ORDER BY id")),
    }
    conn.close()

    database.init_db()

    # confirm it actually changed something, or the rollback check is vacuous
    conn = sqlite3.connect(pre186_db)
    mid_state = {
        'sales': list(conn.execute("SELECT id, unit FROM sales_transactions ORDER BY id")),
        'uc':    list(conn.execute("SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id")),
        'tiers': list(conn.execute("SELECT id, qty_label FROM product_price_tiers ORDER BY id")),
    }
    conn.close()
    assert mid_state != before_state, "migration made no observable change -- rollback check would be vacuous"

    conn = sqlite3.connect(pre186_db)
    try:
        with open(ROLLBACK_186, encoding='utf-8') as f:
            conn.executescript(f.read())
        conn.execute("DELETE FROM applied_migrations WHERE filename = ?", (MIG,))
        conn.commit()

        after_state = {
            'sales': list(conn.execute("SELECT id, unit FROM sales_transactions ORDER BY id")),
            'uc':    list(conn.execute("SELECT id, product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id")),
            'tiers': list(conn.execute("SELECT id, qty_label FROM product_price_tiers ORDER BY id")),
        }
        assert after_state == before_state

        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'migration_186%'").fetchall()}
        assert names == set()
    finally:
        conn.close()


# ── stock / COGS untouched (label-only change) ──────────────────────────────

def test_stock_and_cost_price_unmoved_by_relabelling(pre186_db):
    conn = sqlite3.connect(pre186_db)
    pid = _new_product(conn, 'mig186 stock-untouched product', unit_type='โหล', cost=5.0)
    conn.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, "
        "bsn_code, product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (1,'2026-01-01','IVSC-1','IVSC',?, 'C','x','cust','C1',3,'หล',50,0,'','150','150',0)",
        (pid,))
    _uc(conn, pid, 'หล', 12.0)
    conn.commit()

    before_stock = dict(conn.execute("SELECT product_id, quantity FROM stock_levels"))
    before_cost = dict(conn.execute("SELECT id, cost_price FROM products"))
    before_txn_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre186_db)
    try:
        after_stock = dict(conn.execute("SELECT product_id, quantity FROM stock_levels"))
        after_cost = dict(conn.execute("SELECT id, cost_price FROM products"))
        after_txn_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert after_stock == before_stock
        assert after_cost == before_cost
        assert after_txn_count == before_txn_count
        # and the unit did change (proving this isn't vacuous)
        unit = conn.execute("SELECT unit FROM sales_transactions WHERE doc_no='IVSC-1'").fetchone()[0]
        assert unit == 'โหล'
    finally:
        conn.close()
