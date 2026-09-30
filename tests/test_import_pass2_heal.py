"""Card E PR-2: pass 2 of import_weekly = replay (the affected products) + a
table-wide heal pass, and the heal is COUNTED as stats['first_synced_pending'].

No stock behaviour change (Put, 2026-09-30): the heal pass still first-syncs a
pending mapped row of a product this file never touched. What is new is that
somebody can now see it happen. The differential proof is
projects/sendy-card-e-import-plan/tools/dbf_replay.py; these are the unit cases.

import_weekly opens its own connection, so this uses the empty_db PATH fixture.
"""
import os
import sqlite3

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_124 = os.path.join(_REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")
_MIG_185 = os.path.join(_REPO, "data", "migrations", "185_unit_map_table.sql")


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture(autouse=True)
def _prep(empty_db):
    c = _conn(empty_db)
    with open(_MIG_185, encoding="utf-8") as f:
        c.executescript(f.read())
    cols = {r[1] for r in c.execute("PRAGMA table_info(product_code_mapping)")}
    if "bsn_unit" not in cols:
        with open(_MIG_124, encoding="utf-8") as f:
            c.executescript(f.read())
    c.commit()
    c.close()


def _seed(path, sku, code, unit_type='ตัว'):
    c = _conn(path)
    pid = c.execute("INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, 0)",
                    (f"P{sku}", unit_type)).lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 100)", (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) VALUES (?, ?, ?)",
              (code, f"n{sku}", pid))
    c.commit()
    c.close()
    return pid


def _pending(path, doc, code, pid, *, qty=5, unit='ตัว', fk_off=False):
    """A mapped sales row nobody has synced, shaped like the importer's INSERT."""
    c = _conn(path)
    if fk_off:
        c.execute("PRAGMA foreign_keys = OFF")
    batch = c.execute("INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)"
                      " VALUES ('seed', 0, 0, 'seed')").lastrowid
    c.execute(
        "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,"
        " product_name_raw, customer, customer_code, qty, unit, unit_price, vat_type, discount, total, net,"
        " synced_to_stock, change_source, change_actor, change_token)"
        " VALUES (?, '2026-04-24', ?, ?, ?, ?, 'n', 'c', 'CE', ?, ?, 10, 1, '', 50, 50, 0,"
        " 'import', 'seed', ?)", (batch, f'{doc}-1', doc, pid, code, qty, unit, f'seed-{doc}'))
    c.commit()
    c.close()


def _entry(doc_no, code, qty):
    return {
        'date_iso': '2026-04-24', 'doc_no': doc_no, 'line_seq': 1,
        'qty': qty, 'unit': 'ตัว', 'unit_price': 10.0, 'vat_type': 0, 'discount': '',
        'total': qty * 10.0, 'net': qty * 10.0, 'product_name_raw': 'n',
        'product_code_raw': code, 'party': 'ร้านทดสอบ', 'party_code': 'pc',
    }


def _synced(path, doc):
    c = _conn(path)
    r = c.execute("SELECT synced_to_stock FROM sales_transactions WHERE doc_base=?", (doc,)).fetchone()
    c.close()
    return r[0]


def _ledger(path, pid):
    c = _conn(path)
    r = c.execute("SELECT COALESCE(SUM(quantity_change),0) FROM transactions"
                  " WHERE product_id=? AND note LIKE 'BSN%'", (pid,)).fetchone()
    c.close()
    return r[0]


# (1) the return value of _sync_bsn_to_stock ---------------------------------

def test_sync_returns_count_of_posted_rows_only(empty_db):
    from models import bsn_sync
    ok = _seed(empty_db, 1, 'OK1')
    ratio = _seed(empty_db, 2, 'RT1')
    _pending(empty_db, 'D1', 'OK1', ok)
    _pending(empty_db, 'D2', 'OK1', ok)
    _pending(empty_db, 'D3', 'RT1', ratio, unit='โหล')        # no conversion: skipped
    _pending(empty_db, 'D4', '888ค8888', ok)                   # non-stock: skipped
    _pending(empty_db, 'D5', 'OK1', 987654, fk_off=True)          # product row gone

    c = _conn(empty_db)
    n = bsn_sync._sync_bsn_to_stock(c, 'sales_transactions', 'sales')
    c.commit(); c.close()

    assert n == 2, "only D1 and D2 post a ledger row"
    assert _ledger(empty_db, ok) == -10
    assert _synced(empty_db, 'D3') == 0 and _synced(empty_db, 'D4') == 0
    assert _synced(empty_db, 'D5') == 1, "existing behaviour: a missing product is marked, not posted"


def test_sync_with_empty_product_ids_returns_zero(empty_db):
    from models import bsn_sync
    c = _conn(empty_db)
    assert bsn_sync._sync_bsn_to_stock(c, 'sales_transactions', 'sales', product_ids=[]) == 0
    c.close()


# (2)-(5) through import_weekly ------------------------------------------------

def test_heal_of_untouched_pending_row_is_counted(empty_db):
    import models
    a = _seed(empty_db, 10, 'A10')
    b = _seed(empty_db, 11, 'B11')
    _pending(empty_db, 'HEAL1', 'B11', b)

    stats = models.import_weekly([_entry('IVA1', 'A10', 3)], 'sales', 'f', apply_removals=True)

    assert stats['imported'] == 1
    assert _ledger(empty_db, a) == -3                      # control: A's own row posted
    assert _synced(empty_db, 'HEAL1') == 1 and _ledger(empty_db, b) == -5
    assert stats['first_synced_pending'] == 1


def test_nonstock_pending_row_is_neither_synced_nor_counted(empty_db):
    import models
    _seed(empty_db, 20, 'A20')
    ns = _seed(empty_db, 21, '888ค8888')
    _pending(empty_db, 'NS1', '888ค8888', ns)

    stats = models.import_weekly([_entry('IVA2', 'A20', 3)], 'sales', 'f', apply_removals=True)

    assert stats['imported'] == 1
    assert _synced(empty_db, 'NS1') == 0 and _ledger(empty_db, ns) == 0
    assert stats['first_synced_pending'] == 0


def test_own_rows_are_not_counted_as_heal(empty_db):
    import models
    a = _seed(empty_db, 30, 'A30')
    stats = models.import_weekly([_entry('IVA3', 'A30', 3)], 'sales', 'f', apply_removals=True)
    assert _ledger(empty_db, a) == -3, 'control: the row did post'
    assert stats['first_synced_pending'] == 0
    # a re-post of an already-synced product (a correction) is a replay, not a heal
    stats = models.import_weekly([_entry('IVA3', 'A30', 4)], 'sales', 'f', apply_removals=True)
    assert stats['overwritten'] == 1 and _ledger(empty_db, a) == -4
    assert stats['first_synced_pending'] == 0


def test_removed_rows_product_still_joins_the_replay(empty_db):
    """C's only line vanishes from the doc. C is affected (replayed), so a
    pending row on C is synced by the REPLAY and is not counted as a heal."""
    import models
    a = _seed(empty_db, 40, 'A40')
    c = _seed(empty_db, 41, 'C41')
    models.import_weekly([_entry('IVR1-1', 'A40', 3), _entry('IVR1-2', 'C41', 2)],
                         'sales', 'f', apply_removals=True)
    assert _ledger(empty_db, c) == -2
    _pending(empty_db, 'PENDC', 'C41', c)

    stats = models.import_weekly([_entry('IVR1-1', 'A40', 3)], 'sales', 'f2', apply_removals=True)

    assert stats['removed'] == 1, stats
    assert _ledger(empty_db, c) == -5, 'removed line left the ledger; the pending row was replayed'
    assert _synced(empty_db, 'PENDC') == 1
    assert stats['first_synced_pending'] == 0


def test_noop_reimport_has_no_heal_pass(empty_db):
    """Nothing affected: pass 2 does not run at all (today's behaviour), so a
    pending row is NOT healed and the key is present and 0."""
    import models
    _seed(empty_db, 50, 'A50')
    b = _seed(empty_db, 51, 'B51')
    models.import_weekly([_entry('IVN1', 'A50', 3)], 'sales', 'f', apply_removals=True)
    _pending(empty_db, 'NOOP1', 'B51', b)
    stats = models.import_weekly([_entry('IVN1', 'A50', 3)], 'sales', 'f2', apply_removals=True)
    assert stats['unchanged'] == 1 and stats['affected_products'] == 0
    assert _synced(empty_db, 'NOOP1') == 0
    assert stats['first_synced_pending'] == 0
