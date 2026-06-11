"""Deletion detection in import_weekly / preview_import.

The gap (2026-06-11): when the team DELETES a line from an already-imported
invoice in Express and re-exports, the importer only ever inserts/updates/skips
— it never removed the now-orphaned stored line, so its stock movement persisted
forever (e.g. กันชน went to -2; IV6900437-2; RR6900057). These tests pin the fix.

Critical safety constraint: detection is scoped to docs (doc_base) PRESENT in the
file. A partial slice that doesn't mention a doc must NEVER reverse that doc.
"""
import sqlite3


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _seed(path, sku, code, unit_type='ตัว'):
    c = _conn(path)
    pid = c.execute("INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, 0)",
                    (f"P{sku}", unit_type)).lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) VALUES (?, ?, ?)",
              (code, f"n{sku}", pid))
    c.commit(); c.close()
    return pid


def _entry(doc_no, code, qty, *, unit='ตัว', price=10.0, net=None, line_seq=1):
    return {
        'date_iso': '2026-04-24', 'doc_no': doc_no, 'line_seq': line_seq,
        'qty': qty, 'unit': unit, 'unit_price': price,
        'vat_type': 0, 'discount': '', 'total': net if net is not None else qty * price,
        'net': net if net is not None else qty * price,
        'product_name_raw': 'n', 'product_code_raw': code,
        'party': 'ร้านทดสอบ', 'party_code': 'pc',
    }


def _stock(path, pid):
    c = _conn(path)
    row = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    c.close()
    return row[0] if row else None


def _rows(path, table, doc_base):
    c = _conn(path)
    n = c.execute(f"SELECT COUNT(*) FROM {table} WHERE doc_base=?", (doc_base,)).fetchone()[0]
    c.close()
    return n


# ── SALES: a line deleted from an invoice present in the file is reversed ──
def test_sales_deleted_line_is_reversed(empty_db):
    import models
    a = _seed(empty_db, 50001, 'A1')
    b = _seed(empty_db, 50002, 'A2')
    # invoice IV900 with two lines
    models.import_weekly([_entry('IV900-1', 'A1', 10), _entry('IV900-2', 'A2', 5)], 'sales', 'f1')
    assert _stock(empty_db, a) == -10 and _stock(empty_db, b) == -5

    # re-export the SAME invoice with line -1 (A1) deleted
    s = models.import_weekly([_entry('IV900-2', 'A2', 5)], 'sales', 'f2')
    assert s['removed'] == 1, s
    assert _rows(empty_db, 'sales_transactions', 'IV900') == 1, "orphan line must be gone"
    assert _stock(empty_db, a) == 0, "deleted line's stock must be restored"
    assert _stock(empty_db, b) == -5, "kept line unchanged"


# ── SAFETY: a partial file must NOT reverse docs it does not mention ──
def test_partial_file_does_not_reverse_absent_docs(empty_db):
    import models
    a = _seed(empty_db, 50101, 'B1')
    b = _seed(empty_db, 50102, 'B2')
    models.import_weekly([_entry('IV910-1', 'B1', 7)], 'sales', 'f1')
    models.import_weekly([_entry('IV911-1', 'B2', 3)], 'sales', 'f2')
    assert _stock(empty_db, a) == -7 and _stock(empty_db, b) == -3

    # a slice that only contains IV911 must leave IV910 completely alone
    s = models.import_weekly([_entry('IV911-1', 'B2', 3)], 'sales', 'f3')
    assert s['removed'] == 0, s
    assert _stock(empty_db, a) == -7, "doc absent from the slice must NOT be reversed"
    assert _rows(empty_db, 'sales_transactions', 'IV910') == 1


# ── PURCHASE: multi-line doc, one line_seq deleted, reversed via line_seq key ──
def test_purchase_deleted_line_seq_is_reversed(empty_db):
    import models
    d = _seed(empty_db, 50201, 'D1')
    models.import_weekly([
        _entry('HP301', 'D1', 10, price=5.0, line_seq=1),
        _entry('HP301', 'D1', 5, price=5.0, line_seq=2),
    ], 'purchase', 'f1')
    assert _stock(empty_db, d) == 15

    s = models.import_weekly([_entry('HP301', 'D1', 10, price=5.0, line_seq=1)], 'purchase', 'f2')
    assert s['removed'] == 1, s
    assert _rows(empty_db, 'purchase_transactions', 'HP301') == 1
    assert _stock(empty_db, d) == 10, "deleted second line must reverse its stock"


# ── identical re-import never reports a phantom removal ──
def test_identical_reimport_no_phantom_removal(empty_db):
    import models
    _seed(empty_db, 50301, 'E1')
    models.import_weekly([_entry('IV920-1', 'E1', 4)], 'sales', 'f1')
    s = models.import_weekly([_entry('IV920-1', 'E1', 4)], 'sales', 'f2')
    assert s['removed'] == 0 and s['unchanged'] == 1, s


# ── apply_removals=False (filtered-export guard): detect but DON'T reverse ──
def test_removals_opt_out_keeps_orphan(empty_db):
    import models
    a = _seed(empty_db, 50501, 'G1')
    b = _seed(empty_db, 50502, 'G2')
    models.import_weekly([_entry('IV940-1', 'G1', 6), _entry('IV940-2', 'G2', 2)], 'sales', 'f1')
    assert _stock(empty_db, a) == -6

    # re-import with line -1 gone BUT opt-out → orphan kept, stock untouched,
    # reported as skipped (this is the guard for product/salesperson-filtered files)
    s = models.import_weekly([_entry('IV940-2', 'G2', 2)], 'sales', 'f2', apply_removals=False)
    assert s['removed'] == 0 and s['removed_skipped'] == 1, s
    assert _stock(empty_db, a) == -6, "opt-out must NOT reverse the missing line"
    assert _rows(empty_db, 'sales_transactions', 'IV940') == 2, "orphan row must remain"


# ── preview surfaces removals, read-only, and reconciles with apply ──
def test_preview_lists_removals_readonly(empty_db):
    import models
    a = _seed(empty_db, 50401, 'F1')
    b = _seed(empty_db, 50402, 'F2')
    models.import_weekly([_entry('IV930-1', 'F1', 9), _entry('IV930-2', 'F2', 1)], 'sales', 'f1')

    reduced = [_entry('IV930-2', 'F2', 1)]
    prev = models.preview_import(reduced, 'sales')
    assert prev['removed'] == 1, prev
    assert any(r['bsn_code'] == 'F1' for r in prev['removed_rows'])
    # read-only
    assert _stock(empty_db, a) == -9 and _rows(empty_db, 'sales_transactions', 'IV930') == 2

    st = models.import_weekly(reduced, 'sales', 'f2')
    assert st['removed'] == prev['removed']
    assert _stock(empty_db, a) == 0
