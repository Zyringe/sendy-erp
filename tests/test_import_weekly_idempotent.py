"""Idempotent diff-based import_weekly (PR2).

Put's requirement: re-uploading the FULL or a PARTIAL overlapping range of the
same ขาย/ซื้อ data must converge to the same correct stock — re-importing
unchanged data is a no-op, overlapping ranges merge, multi-line docs don't
collapse, and a corrected price overwrites instead of double-counting.

import_weekly opens its own connection via get_connection(), so these tests use
the empty_db PATH fixture (which patches config/database.DATABASE_PATH) and open
fresh connections for seeding + assertions.
"""
import os
import sqlite3

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_124 = os.path.join(_REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _ensure_bsn_unit(c):
    """empty_db clones the live schema, which doesn't have mig 124 (bsn_unit
    restore) applied on this machine yet — apply it once so import_weekly's
    _resolve_mapping call doesn't hit 'no such column: bsn_unit'. Guarded by
    a column check since _seed() (which calls this) runs more than once per
    test on the same DB file."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(product_code_mapping)")}
    if "bsn_unit" not in cols:
        with open(_MIG_124, encoding="utf-8") as f:
            c.executescript(f.read())


def _seed(path, sku, code, unit_type='ตัว'):
    c = _conn(path)
    _ensure_bsn_unit(c)
    cur = c.execute("INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, 0)", (f"P{sku}", unit_type))
    pid = cur.lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES (?, ?, ?)", (code, f"n{sku}", pid))
    c.commit()
    c.close()
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


def test_reimport_identical_is_noop(empty_db):
    """Re-uploading the exact same file → 0 changes, stock unchanged."""
    import models
    pid = _seed(empty_db, 90101, 'B101')

    s1 = models.import_weekly([_entry('HP101', 'B101', 100)], 'purchase', 'f1')
    assert s1['imported'] == 1
    assert _stock(empty_db, pid) == 100

    s2 = models.import_weekly([_entry('HP101', 'B101', 100)], 'purchase', 'f2')
    assert s2['unchanged'] == 1, s2
    assert s2['imported'] == 0
    assert s2['affected_products'] == 0
    assert _stock(empty_db, pid) == 100, "identical re-import must not change stock"


def test_overlapping_ranges_converge(empty_db):
    """Import {1,2,3} then {3,4,5} (overlap doc 3) → same as importing {1..5}."""
    import models
    pids = {code: _seed(empty_db, 90200 + i, code)
            for i, code in enumerate(['C1', 'C2', 'C3', 'C4', 'C5'])}

    models.import_weekly([_entry('HP1', 'C1', 10), _entry('HP2', 'C2', 10),
                          _entry('HP3', 'C3', 10)], 'purchase', 'a')
    models.import_weekly([_entry('HP3', 'C3', 10), _entry('HP4', 'C4', 10),
                          _entry('HP5', 'C5', 10)], 'purchase', 'b')

    for code in ['C1', 'C2', 'C4', 'C5']:
        assert _stock(empty_db, pids[code]) == 10
    # The overlapping doc must NOT double-count.
    assert _stock(empty_db, pids['C3']) == 10, "overlap doc double-counted"


def test_purchase_multiline_not_collapsed(empty_db):
    """One purchase doc, two lines of the SAME product (line_seq 1 & 2) → both
    post; the old (doc_base,bsn_code,unit_price) key would have collapsed them."""
    import models
    pid = _seed(empty_db, 90301, 'D1')

    s = models.import_weekly([
        _entry('HP301', 'D1', 10, price=5.0, line_seq=1),
        _entry('HP301', 'D1', 5, price=5.0, line_seq=2),  # same doc+code+price
    ], 'purchase', 'f1')
    assert s['imported'] == 2, s
    assert _stock(empty_db, pid) == 15, "two same-price lines must not collapse"

    # Re-import identical → no-op (both lines matched by line_seq).
    s2 = models.import_weekly([
        _entry('HP301', 'D1', 10, price=5.0, line_seq=1),
        _entry('HP301', 'D1', 5, price=5.0, line_seq=2),
    ], 'purchase', 'f2')
    assert s2['unchanged'] == 2 and s2['imported'] == 0
    assert _stock(empty_db, pid) == 15


def test_reimport_raw_stored_unit_is_noop(empty_db):
    """A row stored with a RAW acronym unit (loader/rebuild origin) is a no-op
    on re-import through the normalising path — the diff compares
    normalize(old.unit) == new.unit (BLOCKER-1 regression). Without this, ~95%
    of purchase rows churned cosmetically.
    """
    import models, bsn_units
    raw = 'ลง'
    norm = bsn_units.normalize_unit(raw)
    assert norm != raw, "fixture needs a unit whose normalize() differs from raw"

    c = _conn(empty_db)
    _ensure_bsn_unit(c)
    cur = c.execute("INSERT INTO products (product_name, unit_type, cost_price) VALUES ('Praw', ?, 0)", (norm,))   # unit_type=norm → 1:1
    pid = cur.lastrowid
    # stock_levels starts at 0; the seeded transaction below drives it to 5 via
    # the mig-080 after_transaction_insert trigger (do NOT also seed 5, or the
    # trigger double-counts it to 10).
    c.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES ('F1', 'n', ?)", (pid,))
    bid = c.execute("INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
                    "VALUES ('seed', 0, 0, 'purchase')").lastrowid
    # stored synced purchase row carrying the RAW unit (as the loader wrote it)
    c.execute(
        "INSERT INTO purchase_transactions (batch_id, date_iso, doc_no, doc_base, "
        "product_id, bsn_code, product_name_raw, supplier, supplier_code, qty, unit, "
        "unit_price, vat_type, discount, total, net, synced_to_stock, line_seq) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)",
        (bid, '2026-04-24', 'HP501', 'HP501', pid, 'F1', 'n', 's', 'sc',
         5, raw, 10.0, 0, '', 50.0, 50.0))
    c.execute("INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, "
              "reference_no, note, created_at) VALUES (?, 'IN', 5, 'unit', 'HP501', "
              "'BSN ซื้อ', '2026-04-24 00:00:00')", (pid,))
    c.commit()
    c.close()

    s = models.import_weekly(
        [_entry('HP501', 'F1', 5, unit=raw, price=10.0, net=50.0, line_seq=1)],
        'purchase', 'reimport')
    assert s['unchanged'] == 1, s
    assert s['overwritten'] == 0
    assert _stock(empty_db, pid) == 5, "raw-unit re-import must not churn stock"


def test_unmapped_code_preserves_existing_product_link(empty_db):
    """A bsn_code dropped from product_code_mapping but whose stored row is still
    linked to a product must KEEP that product_id on re-import — nulling it would
    orphan the row's stock movement and float stock UP, contradicting the
    preview's "won't affect stock" for a truly-unmapped code (scrutiny major)."""
    import models
    c = _conn(empty_db)
    _ensure_bsn_unit(c)
    cur = c.execute("INSERT INTO products (product_name, unit_type, cost_price) VALUES ('Linked', 'ตัว', 0)")
    pid = cur.lastrowid
    c.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    bid = c.execute("INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
                    "VALUES ('seed', 0, 0, 'purchase')").lastrowid
    # stored purchase row LINKED to pid, but there is NO mapping row for 'NOMAP'
    c.execute(
        "INSERT INTO purchase_transactions (batch_id, date_iso, doc_no, doc_base, "
        "product_id, bsn_code, product_name_raw, supplier, supplier_code, qty, unit, "
        "unit_price, vat_type, discount, total, net, synced_to_stock, line_seq) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)",
        (bid, '2026-04-24', 'HP701', 'HP701', pid, 'NOMAP', 'n', 's', 'sc',
         10, 'ตัว', 5.0, 0, '', 50.0, 50.0))
    c.execute("INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, "
              "reference_no, note, created_at) VALUES (?, 'IN', 10, 'unit', 'HP701', "
              "'BSN ซื้อ', '2026-04-24 00:00:00')", (pid,))
    c.commit()
    c.close()

    entry = _entry('HP701', 'NOMAP', 10, price=5.0, net=50.0, line_seq=1)
    prev = models.preview_import([dict(entry)], 'purchase')
    assert prev['unmapped'] == 0, "a linked row must not be shown as harmless 'unmapped'"

    models.import_weekly([dict(entry)], 'purchase', 'reimport')
    c = _conn(empty_db)
    row = c.execute("SELECT product_id FROM purchase_transactions WHERE doc_no='HP701'").fetchone()
    c.close()
    assert row['product_id'] == pid, "product link must be preserved, not nulled"
    assert _stock(empty_db, pid) == 10, "stock must not float up when a link is preserved"


def test_preview_is_readonly_and_reconciles_with_apply(empty_db):
    """preview_import writes nothing and its counts match what import_weekly does."""
    import models
    pids = {c: _seed(empty_db, 90600 + i, c) for i, c in enumerate(['G1', 'G2'])}
    models.import_weekly([_entry('HP1', 'G1', 10), _entry('HP2', 'G2', 10)], 'purchase', 'a')

    entries = [
        _entry('HP1', 'G1', 10),   # unchanged
        _entry('HP2', 'G2', 20),   # changed qty 10→20
        _entry('HP9', 'G9', 5),    # unmapped (no mapping for G9)
    ]
    before = {pid: _stock(empty_db, pid) for pid in pids.values()}
    prev = models.preview_import(entries, 'purchase')

    # read-only
    for pid in pids.values():
        assert _stock(empty_db, pid) == before[pid], "preview must not change stock"
    assert prev['unchanged'] == 1
    assert prev['changed'] == 1
    assert prev['unmapped'] == 1
    assert len(prev['changes']) == 1 and prev['changes'][0]['bsn_code'] == 'G2'

    # apply → stats reconcile with the preview
    st = models.import_weekly(entries, 'purchase', 'b')
    assert st['unchanged'] == prev['unchanged']
    assert st['overwritten'] == prev['changed']
    assert st['new_unmapped'] == len(prev['new_codes'])
    assert st['imported'] == prev['new'] + prev['changed'] + prev['unmapped']
    assert _stock(empty_db, pids['G2']) == 20   # the confirmed change applied


def test_corrected_price_overwrites_not_doubles(empty_db):
    """Re-uploading a line with a corrected price overwrites (the old unit_price
    key would have inserted a 2nd row, double-counting stock)."""
    import models
    pid = _seed(empty_db, 90401, 'E1')

    models.import_weekly([_entry('HP401', 'E1', 10, price=5.0, net=50.0)], 'purchase', 'f1')
    assert _stock(empty_db, pid) == 10

    s2 = models.import_weekly([_entry('HP401', 'E1', 10, price=7.0, net=70.0)], 'purchase', 'f2')
    assert s2['overwritten'] == 1, s2
    assert _stock(empty_db, pid) == 10, "corrected price must overwrite, not double-count"

    c = _conn(empty_db)
    rows = c.execute("SELECT unit_price, net FROM purchase_transactions WHERE doc_no='HP401'").fetchall()
    c.close()
    assert len(rows) == 1, "must be a single row after the price correction"
    assert rows[0]['net'] == 70.0
