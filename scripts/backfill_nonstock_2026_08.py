"""One-off: backfill the 3 888ค8888 lines dropped while the code was is_ignored.

NOT an importer mode — run once, verify, then archive as .py.txt.

Assertions run INSIDE the transaction and roll back on failure. A check placed
after commit narrates damage instead of preventing it.

Scope: everything inside Sendy's data era only (Put, 2026-08-06). Pre-2024
(702 lines, ฿73,948.93) is excluded from this backfill.

SR renumber reconciliation (handled separately, by hand, NOT by this script):
Sendy holds SR6700078-1 (net 5.35, product_id 1211, synced_to_stock=1,
2024-10-28) and SR6700163-15 (net -320.00, product_id 1211,
synced_to_stock=1, 2025-09-17) where the DBF now carries the same two
economic lines as -2 and -16. Verified live in sales_transactions
2026-08-10 (ids 38533 / 38655) — both rows already exist, both amounts match
the DBF-stated economics, and both were already synced to stock under the
old (pre-fix) code path, so a renumber changes only a label, not any ledger
or stock state.

Decision: LEAVE the stored doc_no as-is on both rows. Do not rename, do not
insert a second copy under the DBF's current suffix. Rationale: renaming a
settled, already-synced financial row's identifier is an irreversible label
change with no correctness upside for THIS backfill (which only inserts the
3 888ค8888 rows above) — and the risk it exists to pre-empt (a future
full-history reimport re-inserting these under -2/-16 because doc_no no
longer matches) is a general importer-dedup concern, not something this
one-off script can safely paper over. Flagged to team-lead/Put as a
residual open item rather than silently mutated here.
"""
import sqlite3
import sys

PRODUCT_ID = 1211            # ค่าขนส่ง pseudo-product
EXPECTED_ROWS = 3
EXPECTED_NET = -70.00

# (date_iso, doc_no, doc_base, product_name_raw, customer, customer_code,
#  qty, unit, unit_price, vat_type, total, net)
ROWS = [
    ('2024-11-12', 'IV6703039-2', 'IV6703039', 'น้ำหนักเกิน',
     'หน้าร้านL', 'Lหน้าร้าน', 1.0, 'ใบ', -10.0, 1, -10.0, -10.0),
    ('2025-02-27', 'IV6800633-2', 'IV6800633', 'น้ำหนักเกิน',
     'หน้าร้านL', 'Lหน้าร้าน', 1.0, 'ใบ', -30.0, 1, -30.0, -30.0),
    ('2025-09-15', 'IV6802322-2', 'IV6802322', 'น้ำหนักเกิน',
     'หน้าร้านL', 'Lหน้าร้าน', 1.0, 'ใบ', -30.0, 1, -30.0, -30.0),
]


def main(db_path):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")

    stock_before = conn.execute(
        "SELECT COALESCE(quantity, 0) FROM stock_levels WHERE product_id=?",
        (PRODUCT_ID,)).fetchone()
    stock_before = stock_before[0] if stock_before else 0
    ledger_before = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (PRODUCT_ID,)).fetchone()[0]

    try:
        conn.execute("BEGIN IMMEDIATE")

        # Idempotence: refuse rather than double-insert. These are money rows.
        existing = conn.execute(
            "SELECT COUNT(*) FROM sales_transactions WHERE doc_no IN (?,?,?)",
            tuple(r[1] for r in ROWS)).fetchone()[0]
        if existing:
            raise AssertionError(
                "refusing: %d of these doc_no already present — already run?" % existing)

        cur = conn.execute(
            "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes)"
            " VALUES ('backfill_nonstock_2026_08', ?, 0, 'one-off 888ค8888 backfill')",
            (len(ROWS),))
        batch_id = cur.lastrowid

        for (date_iso, doc_no, doc_base, name, cust, cust_code,
             qty, unit, unit_price, vat_type, total, net) in ROWS:
            conn.execute(
                "INSERT INTO sales_transactions"
                " (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code,"
                "  product_name_raw, customer, customer_code, qty, unit,"
                "  unit_price, vat_type, discount, total, net, synced_to_stock)"
                " VALUES (?,?,?,?,?,'888ค8888',?,?,?,?,?,?,?,'',?,?,0)",
                (batch_id, date_iso, doc_no, doc_base, PRODUCT_ID, name, cust,
                 cust_code, qty, unit, unit_price, vat_type, total, net))

        # ── assert INSIDE the transaction; any failure rolls the whole thing back
        rows = conn.execute(
            "SELECT net FROM sales_transactions WHERE batch_id=?", (batch_id,)).fetchall()
        assert len(rows) == EXPECTED_ROWS, "inserted %d, expected %d" % (
            len(rows), EXPECTED_ROWS)
        total_net = round(sum(r['net'] for r in rows), 2)
        assert total_net == EXPECTED_NET, "Σnet %s, expected %s" % (total_net, EXPECTED_NET)

        ledger_after = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE product_id=?", (PRODUCT_ID,)).fetchone()[0]
        assert ledger_after == ledger_before, "ledger rows changed: %d -> %d" % (
            ledger_before, ledger_after)

        stock_after = conn.execute(
            "SELECT COALESCE(quantity, 0) FROM stock_levels WHERE product_id=?",
            (PRODUCT_ID,)).fetchone()
        stock_after = stock_after[0] if stock_after else 0
        assert stock_after == stock_before, "stock moved: %s -> %s" % (
            stock_before, stock_after)

        conn.commit()
    except BaseException as exc:
        conn.rollback()
        conn.close()
        print("ROLLED BACK — nothing written: %s" % exc, file=sys.stderr)
        raise SystemExit(1)
    conn.close()

    # ── independent signal: a FRESH connection, after commit
    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    got = check.execute(
        "SELECT COUNT(*) n, ROUND(SUM(net),2) s FROM sales_transactions"
        " WHERE doc_no IN (?,?,?)", tuple(r[1] for r in ROWS)).fetchone()
    led = check.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (PRODUCT_ID,)).fetchone()[0]
    check.close()
    print("VERIFY rows=%s net=%s ledger=%s (expected 3 / -70.0 / %s)"
          % (got['n'], got['s'], led, ledger_before))
    if got['n'] != EXPECTED_ROWS or got['s'] != EXPECTED_NET or led != ledger_before:
        raise SystemExit("POST-COMMIT VERIFY FAILED")


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit("usage: backfill_nonstock_2026_08.py <db_path>")
    main(sys.argv[1])
