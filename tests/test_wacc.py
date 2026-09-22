"""
Smoke tests for WACC (Weighted Average Cost) recalculation.

See models.recalculate_product_wacc (models.py:2391+).
WACC events fire only on/after _WACC_INITIAL_DATE ('2026-03-03').

Regressions covered:
- b6f67ee: reaching zero stock keeps last WACC (does not reset to next purchase price)
- 5ce0b79: same-day stock import is included in INITIAL ledger entry's display_stock
"""
import sqlite3


def _mk_product(conn, sku, name, cost_price=10.0, unit_type='ตัว'):
    # opening_cost mirrors cost_price, the production invariant since mig 111: the
    # ledger seeds from opening_cost, and create_product / the mig backfill always set
    # it alongside cost_price. Tests needing a costless seed override opening_cost=0.
    cur = conn.execute("INSERT INTO products (product_name, unit_type, cost_price, opening_cost) VALUES (?, ?, ?, ?)", (name, unit_type, cost_price, cost_price))
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    return pid


def _add_purchase_txn(conn, product_id, doc_no, qty, net, date_iso='2026-03-10'):
    """Add a purchase_transactions row + paired BSN-style transactions IN.
    purchase_transactions has no doc_base column (only sales_transactions does)."""
    conn.execute("""
        INSERT INTO purchase_transactions
            (date_iso, doc_no, product_id, bsn_code, product_name_raw,
             supplier, supplier_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES (?, ?, ?, 'X', 'x', 's', 'sc', ?, 'ตัว', ?, 0, '', ?, ?, 1)
    """, (date_iso, doc_no, product_id, qty, net / qty, net, net))
    conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, 'IN', ?, 'unit', ?, 'BSN ซื้อ', ?)
    """, (product_id, int(qty), doc_no, date_iso + ' 00:00:00'))


def _add_sale_txn(conn, product_id, doc_no, qty, date_iso='2026-03-15'):
    conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, 'OUT', ?, 'unit', ?, 'BSN ขาย', ?)
    """, (product_id, -int(qty), doc_no, date_iso + ' 00:00:00'))


def _add_conversion_txn(conn, product_id, ref, qty, unit_cost, date_iso='2026-03-14'):
    """Add a conversion_cost_log row + its paired CONVERSION_IN transactions row,
    same shape models/conversions.py writes (output_product_id/reference_no/
    event_date/output_qty/total_input_cost/unit_cost + an IN txn whose note
    starts 'แปลง:', which is what wacc.py's walk keys off of)."""
    conn.execute("""
        INSERT INTO conversion_cost_log
            (output_product_id, reference_no, event_date, output_qty,
             total_input_cost, unit_cost)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (product_id, ref, date_iso, qty, qty * unit_cost, unit_cost))
    conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, 'IN', ?, 'unit', ?, 'แปลง: test formula', ?)
    """, (product_id, int(qty), ref, date_iso + ' 00:00:00'))


def _open_alerts(conn):
    return conn.execute(
        "SELECT id, kind, severity, message, context_json FROM system_alerts"
        " WHERE resolved_at IS NULL ORDER BY id").fetchall()


# ── Basic purchase IN drives WACC ────────────────────────────────────────────

def test_purchase_in_sets_wacc(empty_db_conn):
    """First purchase on/after INITIAL_DATE establishes WACC = unit_cost."""
    import models

    pid = _mk_product(empty_db_conn, 91001, "WACC A", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW001", qty=10, net=200.0,
                      date_iso='2026-03-10')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    # 200 / 10 = 20.0
    assert wacc == 20.0

    # Ledger has a PURCHASE event with the right unit_cost
    row = empty_db_conn.execute(
        "SELECT unit_cost, wacc_after FROM product_cost_ledger"
        " WHERE product_id=? AND event_type='PURCHASE'", (pid,)
    ).fetchone()
    assert row['unit_cost']  == 20.0
    assert row['wacc_after'] == 20.0


# ── #546 (2026-09-16): zero-stock purchase takes the bill ───────────────────
# Flipped from the old b6f67ee regression test. #546 REVERTS b6f67ee: at walk
# stock 0 the incoming batch IS the whole stock, so its price is the weighted
# average — there is nothing else to blend it against. b6f67ee's freeze was
# protecting a DISPLAY-only figure (WACC did not write products.cost_price
# until #165/244d434, 2026-06-17); no issue/PR/ADR ever named a failing case
# it prevented, and two later features (Card B #427, suggestion approve) had
# to add their own defences because the rule let a wrong seed (the SONAX
# 20.8x case) outlive every purchase that should have corrected it. Put's
# 2026-09-16 triage picked option B: see
# Operations/05_analysis-reports/finance/wacc_zero_stock_purchase_2026-09-16.md

def test_zero_stock_keeps_last_wacc(empty_db_conn):
    """
    Sequence:
      buy 10 @ 20  → WACC = 20, stock = 10
      sell 10      → stock = 0
      buy 5 @ 50   → stock == 0: TAKE THE BILL, WACC = 50 (not 20 — this is
                     the behaviour #546 changes)
      buy 5 @ 30   → stock == 10 > 0 now: CONTROL proving the change is
                     scoped to the exactly-0 branch only — normal weighting
                     still applies: (5*50 + 5*30) / 10 = 40
    """
    import models

    pid = _mk_product(empty_db_conn, 91002, "WACC B", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW100", qty=10, net=200.0,
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid, "IVW100-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid, "HPW101", qty=5, net=250.0,  # 50/unit
                      date_iso='2026-03-14')
    _add_purchase_txn(empty_db_conn, pid, "HPW102", qty=5, net=150.0,  # 30/unit
                      date_iso='2026-03-16')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    assert wacc == 40.0, (
        f"Expected 40.0 (the zero-stock bill's 50 blended normally with the "
        f"next bill's 30), got {wacc}."
    )

    rows = empty_db_conn.execute(
        "SELECT event_type, wacc_after FROM product_cost_ledger"
        " WHERE product_id=? AND event_type='PURCHASE' ORDER BY id",
        (pid,)).fetchall()
    assert [r['wacc_after'] for r in rows] == [20.0, 50.0, 40.0], rows


def test_negative_stock_freeze_still_freezes(empty_db_conn):
    """#546 touches ONLY the exactly-0 branch. A sale keyed before its
    matching purchase (walk stock goes negative) must still freeze WACC,
    unchanged from before #546.

    Sequence:
      buy 10 @ 20  → WACC = 20, stock = 10
      sell 15      → stock = -5 (negative — a sale keyed ahead of its bill)
      buy 5 @ 100  → current_stock < 0: FREEZE, WACC stays 20 (neither 100
                     nor the zero-stock branch's "take the bill")
    """
    import models

    pid = _mk_product(empty_db_conn, 91003, "WACC NEG", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW200", qty=10, net=200.0,
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid, "IVW200-1", qty=15, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid, "HPW201", qty=5, net=500.0,  # 100/unit
                      date_iso='2026-03-14')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    assert wacc == 20.0, f"Negative-stock freeze must still hold; got {wacc}"


def test_conversion_in_at_zero_stock_takes_the_bill(empty_db_conn):
    """#546's second branch (CONVERSION_IN, wacc.py ~L351): a conversion
    landing at walk stock 0 takes the conversion's own unit cost, same
    reasoning as the purchase branch.

    Sequence:
      buy 10 @ 20        → WACC = 20, stock = 10
      sell 10            → stock = 0
      convert in 5 @ 45  → stock == 0: TAKE THE COST, WACC = 45 (not 20)
    """
    import models

    pid = _mk_product(empty_db_conn, 91004, "WACC CONV", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW300", qty=10, net=200.0,
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid, "IVW300-1", qty=10, date_iso='2026-03-12')
    _add_conversion_txn(empty_db_conn, pid, "CONV300", qty=5, unit_cost=45.0,
                        date_iso='2026-03-14')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    assert wacc == 45.0, f"Expected the CONVERSION_IN's own cost 45.0, got {wacc}"


def test_zero_stock_outlier_flags_without_changing_the_written_cost(empty_db_conn):
    """The outlier flag is ADVISORY ONLY (#546 AC: "must never block or alter
    the write"). A bill outside 1/3x..3x of the carried cost at zero stock
    still sets WACC to the bill's own price, but opens a system_alerts row.
    Ratio here is 10x (20 → 200), well outside the 1/3x..3x band.
    """
    import models

    pid = _mk_product(empty_db_conn, 91005, "WACC OUTLIER", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW400", qty=10, net=200.0,   # 20/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid, "IVW400-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid, "HPW401", qty=5, net=1000.0,  # 200/unit → 10x
                      date_iso='2026-03-14')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    # The write is untouched by the flag: still takes the bill in full.
    assert wacc == 200.0

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1, rows
    assert rows[0]['kind'] == models.KIND_WACC_COST_OUTLIER
    assert rows[0]['severity'] == 'warning'
    assert 'HPW401' in rows[0]['message']
    assert str(pid) in rows[0]['message']


def test_zero_stock_in_range_bill_does_not_flag(empty_db_conn):
    """Control for the outlier test: a bill inside 1/3x..3x of the carried
    cost at zero stock takes the bill normally and opens NO alert."""
    import models

    pid = _mk_product(empty_db_conn, 91006, "WACC IN RANGE", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW500", qty=10, net=200.0,  # 20/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid, "IVW500-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid, "HPW501", qty=5, net=125.0,  # 25/unit → 1.25x
                      date_iso='2026-03-14')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    assert wacc == 25.0
    assert _open_alerts(empty_db_conn) == []


def test_conversion_in_zero_stock_outlier_also_flags(empty_db_conn):
    """Same alert wiring on the CONVERSION_IN branch, not just PURCHASE — both
    call sites share the same helper; this proves the conversion one is
    actually wired rather than silently skipped by a copy-paste that
    diverged. Ratio 0.1x (20 → 2), outside the band on the low side."""
    import models

    pid = _mk_product(empty_db_conn, 91007, "WACC CONV OUTLIER", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid, "HPW600", qty=10, net=200.0,  # 20/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid, "IVW600-1", qty=10, date_iso='2026-03-12')
    _add_conversion_txn(empty_db_conn, pid, "CONV600", qty=5, unit_cost=2.0,
                        date_iso='2026-03-14')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    assert wacc == 2.0  # still takes the conversion's own cost, unaltered
    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1, rows
    assert rows[0]['kind'] == models.KIND_WACC_COST_OUTLIER
    assert 'CONV600' in rows[0]['message']


# ── PR #551 review, SHOULD-FIX 1 ─────────────────────────────────────────────
# The PURCHASE branch is protected upstream by `if net > 0:` (wacc.py ~L336),
# so an uncosted purchase line never enters the costing block. The
# CONVERSION_IN branch had no equivalent guard: it took
# conversion_cost_log.unit_cost unconditionally, so a 0-cost conversion
# landing at walk stock 0 drove wacc_after to 0.0 while the final
# `current_wacc > 0` write guard left products.cost_price at its old value —
# the ledger and cost_price silently disagreed until the next costed bill.

def test_conversion_in_zero_cost_at_zero_stock_does_not_zero_the_wacc(empty_db_conn):
    """Product A (the bug): buy 10 @ 20 -> sell 10 (stock 0) -> convert in
    4 @ 0.0 -> WACC must stay 20 (not fall to 0), and the conversion must not
    even write a ledger row — the same shape a zero-net PURCHASE takes
    (falls through to the plain `current_stock += qty`, uncosted).
    Product B (control, same test): identical sequence but the conversion's
    unit_cost is 45.0 (positive) -> still takes the bill, WACC becomes 45.0.
    Proves the fix removed exactly the zero-cost case, not the whole branch.
    """
    import models

    pid_a = _mk_product(empty_db_conn, 91008, "WACC CONV ZERO COST", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid_a, "HPW700", qty=10, net=200.0,  # 20/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid_a, "IVW700-1", qty=10, date_iso='2026-03-12')
    _add_conversion_txn(empty_db_conn, pid_a, "CONV700", qty=4, unit_cost=0.0,
                        date_iso='2026-03-14')

    pid_b = _mk_product(empty_db_conn, 91009, "WACC CONV POS COST CTRL", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid_b, "HPW701", qty=10, net=200.0,  # 20/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid_b, "IVW701-1", qty=10, date_iso='2026-03-12')
    _add_conversion_txn(empty_db_conn, pid_b, "CONV701", qty=4, unit_cost=45.0,
                        date_iso='2026-03-14')
    empty_db_conn.commit()

    wacc_a = models.recalculate_product_wacc(pid_a, empty_db_conn)
    wacc_b = models.recalculate_product_wacc(pid_b, empty_db_conn)
    empty_db_conn.commit()

    assert wacc_a == 20.0, f"A zero-cost conversion must not zero the WACC; got {wacc_a}"
    assert _open_alerts(empty_db_conn) == []
    conv_rows_a = empty_db_conn.execute(
        "SELECT * FROM product_cost_ledger WHERE product_id=? AND event_type='CONVERSION_IN'",
        (pid_a,)).fetchall()
    assert conv_rows_a == [], conv_rows_a

    assert wacc_b == 45.0, (
        f"Control: a positive-cost conversion at zero stock must still take "
        f"the bill in full; got {wacc_b}"
    )


# ── PR #551 review, SHOULD-FIX 2 ─────────────────────────────────────────────
# The AC-specified 1/3x..3x outlier band was pinned by NO test: mutation m5a
# (narrowing the band to 1/2x..2x) left the whole file green because the two
# existing fixtures (10x, 1.25x) sit outside any band between ~1.25x and
# ~10x. Boundary fixtures on both sides of the real 1/3x..3x edges, using a
# prior cost of 31.0 chosen so 31/3.1 == 10.0 exactly (no float-boundary
# flakiness on the low side's flagging case).

def test_outlier_band_boundary_high_side(empty_db_conn):
    """2.9x (89.9) sits inside the band -> no alert. 3.1x (96.1) sits
    outside -> alert. Pins the HIGH edge at 3.0x, not some wider value."""
    import models

    pid_in = _mk_product(empty_db_conn, 91010, "WACC BAND HIGH IN", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid_in, "HPW800", qty=10, net=310.0,  # 31/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid_in, "IVW800-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid_in, "HPW801", qty=5, net=449.5,  # 89.9/unit = 2.9x
                      date_iso='2026-03-14')

    pid_out = _mk_product(empty_db_conn, 91011, "WACC BAND HIGH OUT", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid_out, "HPW802", qty=10, net=310.0,  # 31/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid_out, "IVW802-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid_out, "HPW803", qty=5, net=480.5,  # 96.1/unit = 3.1x
                      date_iso='2026-03-14')
    empty_db_conn.commit()

    models.recalculate_product_wacc(pid_in, empty_db_conn)
    models.recalculate_product_wacc(pid_out, empty_db_conn)
    empty_db_conn.commit()

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1, rows
    assert 'HPW803' in rows[0]['message']
    assert not any('HPW801' in r['message'] for r in rows)


def test_outlier_band_boundary_low_side(empty_db_conn):
    """1/2.9x (~10.6897) sits inside the band -> no alert. 1/3.1x (10.0)
    sits outside -> alert. Pins the LOW edge at 1/3x, not some wider value."""
    import models

    pid_in = _mk_product(empty_db_conn, 91012, "WACC BAND LOW IN", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid_in, "HPW810", qty=10, net=310.0,  # 31/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid_in, "IVW810-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid_in, "HPW811", qty=5, net=53.448275862068964,  # 31/2.9/unit
                      date_iso='2026-03-14')

    pid_out = _mk_product(empty_db_conn, 91013, "WACC BAND LOW OUT", cost_price=0.0)
    _add_purchase_txn(empty_db_conn, pid_out, "HPW812", qty=10, net=310.0,  # 31/unit
                      date_iso='2026-03-10')
    _add_sale_txn(empty_db_conn, pid_out, "IVW812-1", qty=10, date_iso='2026-03-12')
    _add_purchase_txn(empty_db_conn, pid_out, "HPW813", qty=5, net=50.0,  # 31/3.1 = 10.0/unit
                      date_iso='2026-03-14')
    empty_db_conn.commit()

    models.recalculate_product_wacc(pid_in, empty_db_conn)
    models.recalculate_product_wacc(pid_out, empty_db_conn)
    empty_db_conn.commit()

    rows = _open_alerts(empty_db_conn)
    assert len(rows) == 1, rows
    assert 'HPW813' in rows[0]['message']
    assert not any('HPW811' in r['message'] for r in rows)


# ── Regression for commit 5ce0b79 ────────────────────────────────────────────

def test_initial_ledger_includes_same_day_stock_import(empty_db_conn):
    """
    Regression for 5ce0b79: stock-import IN dated exactly on _WACC_INITIAL_DATE
    must be reflected in the INITIAL ledger entry's display_stock (not 0).
    """
    import models

    pid = _mk_product(empty_db_conn, 91003, "WACC C", cost_price=15.0)

    # Stock-import IN: a non-BSN, non-conversion IN with a non-matching note
    # on exactly the INITIAL_DATE — this is the case that was wrong before 5ce0b79.
    empty_db_conn.execute("""
        INSERT INTO transactions
            (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
        VALUES (?, 'IN', 25, 'unit', NULL, 'นำเข้าสต็อค', '2026-03-03 00:00:00')
    """, (pid,))
    empty_db_conn.commit()

    models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    row = empty_db_conn.execute(
        "SELECT stock_after, qty_change FROM product_cost_ledger"
        " WHERE product_id=? AND event_type='INITIAL'", (pid,)
    ).fetchone()
    assert row is not None, "INITIAL ledger entry missing"
    # The fix: display_stock = current_stock + initial_date_stock_imports
    # i.e. the same-day import (25) is visible, not 0.
    assert row['stock_after'] == 25, (
        f"Expected INITIAL stock_after=25 (same-day import), got {row['stock_after']}. "
        "See models.py:2446-2455 + 2477-2485."
    )


# ── Live DB sanity (does not modify data — uses tmp_db copy) ─────────────────

def test_wacc_against_live_db_smoke(tmp_db):
    """
    Pick an active product from the temp copy of the live DB and verify
    recalculate_product_wacc runs without exceptions and returns a number.
    """
    import actor, models, sqlite3

    conn = actor.install(sqlite3.connect(tmp_db))   # #590: a rebuild needs an actor
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM products WHERE is_active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        conn.close()
        return  # no products — nothing to assert

    wacc = models.recalculate_product_wacc(row['id'], conn)
    conn.commit()
    conn.close()
    assert isinstance(wacc, (int, float))
    assert wacc >= 0


# ── Option D: cost_price is the live WACC output, opening_cost is the seed ────

def test_recompute_writes_cost_price_from_opening_cost_idempotently(empty_db_conn):
    """recalculate_product_wacc must (1) seed the INITIAL entry from opening_cost,
    (2) write the resulting live WACC back to products.cost_price (what margin/COGS/
    quote readers consume), and (3) be IDEMPOTENT — re-running it must NOT compound.

    Compounding was the latent bug from the 2026-06-17 bulk sync: when the seed and
    the output are the same column, each recompute re-blends past purchases into the
    seed and drifts the cost upward. opening_cost (immutable) fixes that.

    Scenario: opening 10 units @ 10 (opening_cost), then buy 10 @ 12.
              true WACC = (10*10 + 10*12) / 20 = 11.0
    """
    import models
    c = empty_db_conn

    pid = _mk_product(c, 95001, "D-core", cost_price=10.0)
    c.execute("UPDATE products SET opening_cost=10.0 WHERE id=?", (pid,))
    # opening stock: 10 units on INITIAL_DATE (non-purchase IN → seeds INITIAL stock)
    c.execute("""INSERT INTO transactions
                   (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
                 VALUES (?, 'IN', 10, 'unit', NULL, 'ยอดยกมา', '2026-03-03 00:00:00')""", (pid,))
    _add_purchase_txn(c, pid, "HPD001", qty=10, net=120.0, date_iso='2026-03-10')  # 12/unit
    c.commit()

    w1 = models.recalculate_product_wacc(pid, c)
    c.commit()
    assert round(w1, 6) == 11.0
    cp1 = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(cp1, 6) == 11.0, "recompute must write the live WACC to cost_price"

    # Idempotency: opening_cost is the seed (still 10), so a re-run stays at 11.0,
    # NOT 11.5 (which is what seeding from the just-written cost_price would give).
    w2 = models.recalculate_product_wacc(pid, c)
    c.commit()
    assert round(w2, 6) == 11.0, "recompute must be idempotent (no compounding)"
    cp2 = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(cp2, 6) == 11.0
    opening = c.execute("SELECT opening_cost FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(opening, 6) == 10.0, "opening_cost must remain the immutable seed"


def test_recompute_does_not_zero_a_costless_products_cost_price(empty_db_conn):
    """A product with no derivable WACC (opening_cost 0, no purchases) must keep its
    existing cost_price — recompute writes only when it has a real (>0) WACC, so a
    manually-set cost on a not-yet-purchased product is never wiped to 0."""
    import models
    c = empty_db_conn
    pid = _mk_product(c, 95002, "D-costless", cost_price=7.5)
    c.execute("UPDATE products SET opening_cost=0.0 WHERE id=?", (pid,))
    c.commit()

    models.recalculate_product_wacc(pid, c)
    c.commit()
    cp = c.execute("SELECT cost_price FROM products WHERE id=?", (pid,)).fetchone()[0]
    assert round(cp, 6) == 7.5, "cost_price must be preserved when WACC is 0"
