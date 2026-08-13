"""Non-stock billable BSN lines: revenue without a stock ledger.

Design: projects/nonstock-billable-line/design.md v4.
"""
from models.stock_filters import (
    NON_STOCK_BSN_CODES, is_non_stock_code, non_stock_clause, NonStockCodeError)


def test_constant_holds_exactly_the_two_billable_codes():
    assert NON_STOCK_BSN_CODES == frozenset({'ZZZ', '888ค8888'})
    # ค่าVAT is deliberately NOT here — VAT is tax, not revenue.
    assert '888ค8887' not in NON_STOCK_BSN_CODES


def test_is_non_stock_code():
    assert is_non_stock_code('ZZZ') is True
    assert is_non_stock_code('888ค8888') is True
    assert is_non_stock_code('888ค8887') is False
    assert is_non_stock_code('036ผ7110') is False
    assert is_non_stock_code(None) is False
    assert is_non_stock_code('') is False


def test_non_stock_clause_filters_rows(tmp_db_conn):
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions")
    for i, code in enumerate(('036ผ7110', 'ZZZ', '888ค8888', '888ค8887')):
        conn.execute(
            "INSERT INTO sales_transactions"
            " (batch_id, date_iso, doc_no, doc_base, bsn_code, qty, net)"
            " VALUES (1, '2026-06-15', ?, ?, ?, 1, 100)",
            (f'IV900{i}-1', f'IV900{i}', code))
    # Insert a NULL row (unmapped legacy case) — must be KEPT by non_stock_clause
    conn.execute(
        "INSERT INTO sales_transactions"
        " (batch_id, date_iso, doc_no, doc_base, bsn_code, qty, net)"
        " VALUES (1, '2026-06-15', ?, ?, ?, 1, 100)",
        ('IV9004-1', 'IV9004', None))
    rows = conn.execute(
        f"SELECT bsn_code FROM sales_transactions WHERE {non_stock_clause()}").fetchall()
    codes = sorted(c or '' for c in (r['bsn_code'] for r in rows))
    assert len(codes) == 3, codes          # count BEFORE the property
    assert codes == ['', '036ผ7110', '888ค8887']

    aliased = conn.execute(
        f"SELECT st.bsn_code FROM sales_transactions st WHERE {non_stock_clause('st')}"
    ).fetchall()
    assert len(aliased) == 3


def test_non_stock_code_error_is_a_value_error():
    assert issubclass(NonStockCodeError, ValueError)


import models


def _entry(code, name, doc_no, net, qty=1.0, unit='ใบ', date='2026-06-15'):
    return {
        'date_iso': date, 'doc_no': doc_no, 'product_code_raw': code,
        'product_name_raw': name, 'party': 'วรสวัสดิ์ ฮาร์ดแวร์',
        'party_code': '01อ35', 'qty': qty, 'unit': unit, 'unit_price': net / qty,
        'vat_type': 2, 'discount': '', 'total': net, 'net': net, 'line_seq': 1,
    }


def _map(conn, code, name, pid, is_ignored=0):
    # tmp_db_conn is a full clone of the LIVE dev DB (no wipe) — it already has
    # real product_code_mapping rows for these two codes (bsn_unit=''), so a
    # plain INSERT hits the (bsn_code, bsn_unit) UNIQUE constraint. Worse: even
    # after that's fixed, migration 155 (Task 8, same plan) flips their
    # is_ignored to 0 — so once that migration is applied, the clone's rows
    # would silently stop representing the pre-mig-155 state this test exists
    # to cover, while still passing green. Force the state instead of
    # inheriting it: delete every row for this bsn_code (any bsn_unit), then
    # insert exactly what the test asked for.
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (code,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id,"
        " is_ignored, bsn_unit) VALUES (?, ?, ?, ?, '')",
        (code, name, pid, is_ignored))
    conn.commit()


def test_non_stock_line_is_imported_as_revenue(tmp_db_conn):
    conn = tmp_db_conn
    # Same live-clone hazard as _map() above, one table over: the cloned DB
    # already carries 26 real historical rows for this bsn_code (two years of
    # actual ค่าขนส่ง billing), which would blow up the `len(rows) == 1` count
    # below. Force this bsn_code's row set to empty rather than inherit it.
    conn.execute("DELETE FROM sales_transactions WHERE bsn_code='888ค8888'")
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง', 'ตัว')"
    ).lastrowid
    conn.commit()
    _map(conn, '888ค8888', 'ค่าขนส่ง', pid, is_ignored=1)   # pre-mig state

    stats = models.import_weekly(
        [_entry('888ค8888', 'ค่าขนส่ง', 'IV9001-1', 30.0)], 'sales', 'test.csv')

    assert stats['non_stock'] == 1, stats
    assert stats['ignored'] == 0, stats
    rows = conn.execute(
        "SELECT net, product_id, synced_to_stock FROM sales_transactions"
        " WHERE bsn_code='888ค8888'").fetchall()
    assert len(rows) == 1, rows                 # count BEFORE the property
    assert rows[0]['net'] == 30.0
    assert rows[0]['product_id'] == pid
    # is_ignored=1 on the mapping for a non-stock code is a contradiction —
    # the constant wins, but Task 7's alert needs it recorded, not silently
    # honoured.
    contradictions = stats['ignored_contradictions']
    assert len(contradictions) == 1, contradictions     # count BEFORE the property
    assert contradictions == ['888ค8888']


def test_vat_code_is_still_dropped_entirely(tmp_db_conn):
    """Design §7(b) #13 — BOTH halves.

    The "still alerts" half was unasserted until the final review (I4). It
    matters: the whole `import_ignored_lines` mechanism stays live only for
    888ค8887 after mig 155 unignores the other two, so if unignoring them had
    also broken the alert path, nothing would have noticed.
    `system_alerts` is wiped first — the live clone carries unrelated open
    alerts, and inheriting them would make the count assertion meaningless.
    """
    conn = tmp_db_conn
    conn.execute("DELETE FROM system_alerts")
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าVAT', 'ตัว')"
    ).lastrowid
    conn.commit()
    _map(conn, '888ค8887', 'ค่าVAT', pid, is_ignored=1)

    stats = models.import_weekly(
        [_entry('888ค8887', 'ค่าVAT', 'IV9002-1', 70.0)], 'sales', 'test.csv')

    assert stats['ignored'] == 1, stats
    assert stats['non_stock'] == 0, stats
    n = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE bsn_code='888ค8887'").fetchone()[0]
    assert n == 0

    # ── "still alerts" ──
    # Scoped to the kind under test: an import can raise several kinds now (the
    # orphan-ledger sweep is the third), and the clone carries rows others fire
    # on, so counting every open row would fail on unrelated correct behaviour.
    alerts = conn.execute(
        "SELECT kind, dedupe_key, message FROM system_alerts"
        " WHERE resolved_at IS NULL AND kind = 'import_ignored_lines'").fetchall()
    assert len(alerts) == 1, alerts             # count BEFORE the property
    assert alerts[0]['kind'] == 'import_ignored_lines', dict(alerts[0])
    assert '888ค8887' in alerts[0]['message'], alerts[0]['message']


def test_preview_agrees_with_commit_for_non_stock_codes(tmp_db_conn):
    conn = tmp_db_conn
    # Force fixture state exactly like the commit-path tests above — the
    # clone's own history for these codes must not leak into the preview
    # counts either.
    conn.execute(
        "DELETE FROM sales_transactions WHERE bsn_code IN ('888ค8888', '888ค8887')")
    pid_ship = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง', 'ตัว')"
    ).lastrowid
    pid_vat = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าVAT', 'ตัว')"
    ).lastrowid
    conn.commit()
    _map(conn, '888ค8888', 'ค่าขนส่ง', pid_ship, is_ignored=1)
    _map(conn, '888ค8887', 'ค่าVAT', pid_vat, is_ignored=1)

    ship_preview = models.preview_import(
        [_entry('888ค8888', 'ค่าขนส่ง', 'IV9003-1', 30.0)], 'sales')
    assert ship_preview['non_stock'] == 1, ship_preview
    assert ship_preview['ignored'] == 0, ship_preview

    vat_preview = models.preview_import(
        [_entry('888ค8887', 'ค่าVAT', 'IV9004-1', 70.0)], 'sales')
    assert vat_preview['ignored'] == 1, vat_preview
    assert vat_preview['non_stock'] == 0, vat_preview


def test_non_stock_line_creates_no_ledger_row(tmp_db_conn):
    conn = tmp_db_conn
    # tmp_db_conn clones the LIVE dev DB: 26 real historical 888ค8888 rows are
    # already there. Force the state (same rule as _map) or the assertions
    # below read someone else's data. Per-test, matching the pattern already
    # established in this file.
    conn.execute("DELETE FROM sales_transactions WHERE bsn_code='888ค8888'")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง', 'ตัว')"
    ).lastrowid
    # A conversion EXISTS on purpose: the guard must not depend on a missing
    # ratio. This is the ค่าขนส่ง phantom-stock trap.
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
                 " VALUES (?, 'ใบ', 1.0)", (pid,))
    conn.commit()
    _map(conn, '888ค8888', 'ค่าขนส่ง', pid)

    before = conn.execute(
        "SELECT COALESCE(quantity, 0) FROM stock_levels WHERE product_id=?",
        (pid,)).fetchone()
    before = before[0] if before else 0

    models.import_weekly(
        [_entry('888ค8888', 'ค่าขนส่ง', 'IV9003-1', 30.0)], 'sales', 'test.csv')

    ledger = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
    assert ledger == 0

    after = conn.execute(
        "SELECT COALESCE(quantity, 0) FROM stock_levels WHERE product_id=?",
        (pid,)).fetchone()
    after = after[0] if after else 0
    assert after == before

    flag = conn.execute(
        "SELECT synced_to_stock FROM sales_transactions WHERE bsn_code='888ค8888'"
    ).fetchone()[0]
    assert flag == 0, "must stay 0 — reconcile.py requires unsynced rows to be ledger-free"


def test_non_stock_row_survives_pass_2_rebuild(tmp_db_conn):
    """imports.py adds every inserted mapped pid to affected_pids; pass 2 then
    DELETEs that product's BSN ledger, resets its source rows to
    synced_to_stock=0 and re-syncs. Design v1 died here."""
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions WHERE bsn_code='888ค8888'")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง', 'ตัว')"
    ).lastrowid
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
                 " VALUES (?, 'ใบ', 1.0)", (pid,))
    conn.commit()
    _map(conn, '888ค8888', 'ค่าขนส่ง', pid)

    models.import_weekly(
        [_entry('888ค8888', 'ค่าขนส่ง', 'IV9004-1', 30.0)], 'sales', 'test.csv')
    # Second import on the SAME pid with a CHANGED net forces affected_pids
    # to include pid, which triggers the pass-2 delete/reset/re-sync.
    models.import_weekly(
        [_entry('888ค8888', 'ค่าขนส่ง', 'IV9004-1', 45.0)], 'sales', 'test.csv')

    rows = conn.execute(
        "SELECT net, synced_to_stock FROM sales_transactions WHERE bsn_code='888ค8888'"
    ).fetchall()
    assert len(rows) == 1, rows
    assert rows[0]['net'] == 45.0
    assert rows[0]['synced_to_stock'] == 0
    ledger = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
    assert ledger == 0


def test_reimport_identical_non_stock_file_is_a_true_no_op(tmp_db_conn):
    """Design §7(b) #15 — implemented at final review (I4).

    Idempotency of the weekly import is the most-exercised property in this
    app (test_import_weekly_idempotent.py covers ordinary codes), and the
    non-stock branch now sits directly above the `same` comparison at
    imports.py:262. Re-uploading the identical file must be a genuine no-op
    for these codes too: `unchanged`, no duplicate row, no ledger row.

    The row's **id** is asserted stable, not just the row COUNT — a
    DELETE + re-INSERT would keep the count at 1 while churning the ledger
    through pass 2, which is exactly the failure this pins. `overwritten == 0`
    is the second, independent witness of the same thing.

    Note `non_stock` is NOT terminal: it overlaps `unchanged` rather than
    partitioning against it (final review §2), so the second import reports
    both. Asserted here so a future refactor that "tidies" that has to make
    the change deliberately.
    """
    conn = tmp_db_conn
    conn.execute("DELETE FROM sales_transactions WHERE bsn_code='888ค8888'")
    conn.commit()
    pid = conn.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('ค่าขนส่ง', 'ตัว')"
    ).lastrowid
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
                 " VALUES (?, 'ใบ', 1.0)", (pid,))
    conn.commit()
    _map(conn, '888ค8888', 'ค่าขนส่ง', pid)

    entry = _entry('888ค8888', 'ค่าขนส่ง', 'IV9005-1', 30.0)

    first = models.import_weekly([dict(entry)], 'sales', 'test.csv')
    assert first['non_stock'] == 1, first
    assert first['imported'] == 1, first
    rows1 = conn.execute(
        "SELECT id, net FROM sales_transactions WHERE bsn_code='888ค8888'"
    ).fetchall()
    assert len(rows1) == 1, rows1               # count BEFORE the property
    assert rows1[0]['net'] == 30.0
    ledger1 = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
    assert ledger1 == 0, 'first import already posted a ledger row'
    stock1 = conn.execute(
        "SELECT COALESCE(quantity, 0) FROM stock_levels WHERE product_id=?",
        (pid,)).fetchone()
    stock1 = stock1[0] if stock1 else 0

    second = models.import_weekly([dict(entry)], 'sales', 'test.csv')

    assert second['unchanged'] == 1, second
    assert second['imported'] == 0, second
    assert second['overwritten'] == 0, second   # no DELETE + re-INSERT
    assert second['non_stock'] == 1, second     # overlaps `unchanged`, by design

    rows2 = conn.execute(
        "SELECT id, net FROM sales_transactions WHERE bsn_code='888ค8888'"
    ).fetchall()
    assert len(rows2) == 1, rows2               # no duplicate
    assert rows2[0]['id'] == rows1[0]['id'], 'row was replaced, not left alone'
    assert rows2[0]['net'] == 30.0

    ledger2 = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
    assert ledger2 == 0, 'the re-import posted a ledger row'
    stock2 = conn.execute(
        "SELECT COALESCE(quantity, 0) FROM stock_levels WHERE product_id=?",
        (pid,)).fetchone()
    stock2 = stock2[0] if stock2 else 0
    assert stock2 == stock1
