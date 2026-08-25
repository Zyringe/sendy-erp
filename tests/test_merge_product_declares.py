"""Merging a duplicate product must declare itself, and must still merge.

WHY THIS EXISTS — mig 173 broke this tool and no sweep could see it.
`scripts/merge_product.py` re-points every table that has a `product_id`
column, and it gets that list from `sqlite_master` at RUNTIME. So the table
names `sales_transactions` / `purchase_transactions` never appear as literals
in the file, and `test_source_doc_writer_coverage.py` — which matches SQL
text — is structurally blind to it. Its own docstring says so ("SQL assembled
at runtime from pieces that are never adjacent literals"); this is that case,
found by the guard aborting a real merge, not by the sweep.

The regression it protects against is not subtle: after mig 173 shipped,
`merge_product.py --apply` rolled back on every product that had ever been
sold or purchased, which is nearly all of them.
"""
import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import merge_product as mp  # noqa: E402

SRC, DST = 903301, 903302


def _seed(c):
    for pid in (SRC, DST):
        c.execute("INSERT INTO products (id, product_name, unit_type, sku_code,"
                  " is_active) VALUES (?, ?, 'ตัว', ?, 1)", (pid, f'P{pid}', f'S{pid}'))
    c.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base,"
              " customer_code, bsn_code, product_name_raw, qty, unit, unit_price,"
              " vat_type, discount, total, net, product_id)"
              " VALUES ('2026-01-05','IVM01-1','IVM01','C1','M001','ของ',1,'ตัว',"
              "10,1,'',10,10,?)", (SRC,))
    c.execute("INSERT INTO purchase_transactions (date_iso, doc_no, doc_base,"
              " supplier_code, bsn_code, product_name_raw, qty, unit, unit_price,"
              " vat_type, discount, total, net, line_seq, product_id)"
              " VALUES ('2026-01-06','RRM01','RRM01','S1','M001','ของ',1,'ตัว',"
              "10,1,'',10,10,1,?)", (SRC,))
    c.commit()


def _run(db_path):
    argv = sys.argv
    sys.argv = ['merge_product.py', '--from', str(SRC), '--to', str(DST),
                '--apply', '--db', db_path]
    try:
        return mp.main()
    finally:
        sys.argv = argv


def test_merge_repoints_source_documents_and_says_who(empty_db, empty_db_conn):
    _seed(empty_db_conn)
    # CONTROL: the rows really are on SRC before the merge, so the assertions
    # below cannot pass on an empty table.
    assert empty_db_conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE product_id=?",
        (SRC,)).fetchone()[0] == 1

    _run(str(empty_db))

    c = sqlite3.connect(str(empty_db))
    c.row_factory = sqlite3.Row
    for table in ('sales_transactions', 'purchase_transactions'):
        rows = c.execute(f"SELECT * FROM {table}").fetchall()
        assert len(rows) == 1, f'{table}: {len(rows)} rows'
        r = rows[0]
        assert r['product_id'] == DST, f'{table} was not re-pointed'
        assert r['change_source'] == 'manual'
        assert r['change_actor'] == 'merge-product'
        assert str(SRC) in (r['change_reason'] or '') and str(DST) in (r['change_reason'] or ''), \
            f'the reason must name both products, got {r["change_reason"]!r}'
        assert r['change_token'], 'no token — the guard would accept a silent re-edit'

    # …and the audit trail carries the same declaration, one row per line.
    # ⚠ Filtered to UPDATE on purpose: seeding the two rows already wrote an
    # INSERT audit row each, so an unfiltered count of 2 would be wrong and an
    # unfiltered count of 4 would pass whether or not the merge was recorded.
    audit = c.execute(
        "SELECT table_name, action, user, change_source, change_reason FROM audit_log"
        " WHERE table_name IN ('sales_transactions','purchase_transactions')"
        "   AND action = 'UPDATE'").fetchall()
    assert len(audit) == 2, f'{len(audit)} UPDATE audit rows, expected 2'
    for a in audit:
        assert a['user'] == 'merge-product' and a['change_source'] == 'manual'
        assert (a['change_reason'] or '').strip()
    # CONTROL: the INSERT rows are there too, so the filter above is selecting
    # from a populated table rather than reading an empty one.
    assert c.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='INSERT' AND table_name IN"
        " ('sales_transactions','purchase_transactions')").fetchone()[0] == 2
    c.close()
