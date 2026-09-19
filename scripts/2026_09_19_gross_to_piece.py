"""2026-09-19 — base unit กุรุส -> piece (pids 1050 แผ่น, 1320 แท่ง).

WHY. Issue #581. `unit_conversions` says `แผ่น = 1.0` and `แท่ง = 1.0` on
products whose base unit is really a กุรุส (gross = 12 dozen = 144), so a bill
written in pieces posts 144x its true quantity to the stock ledger and a
back-solved opening ADJUST hides the difference exactly.

Put ruled 2026-09-18/19:
  * 1 กุรุส = 144 — confirmed on prod from BOTH axes independently. pid 1320's
    IV6801282-4 sells 72 แท่ง @ ฿8.68 while its own purchase leg RR6800218 buys
    0.5 กร: 72/144 = 0.5 (quantity) and 8.68*144 = ฿1,249.92 ~ the ฿1,250 กุรุส
    tier (price). Neither axis needs the other.
  * Option ข — rebase the base unit rather than store a fractional 1/144 ratio.
    Same policy as the 2026-08-17 โหล->ตัว batch: base unit = the SMALLEST unit,
    the catalogue keeps quoting the pack through `product_price_tiers`.
  * CONTRACT: preserve the physical quantity, change only what the number means.
    Stock is not re-verified and not moved; 0 กุรุส becomes 0 แผ่น.

⚠ NOT a blanket `quantity_change * 144`. pid 1320's sales arrive in TWO units
for the same physical amount — `ตัว` (actually a gross) at qty 0.5/1.0 and
`แท่ง` (the piece) at qty 72. Multiplying the ledger would scale the already-
correct `แท่ง` rows too. Each leg is re-derived from its OWN bill's unit through
the app's replay, after which both spellings converge on -72. That convergence
is the fix.

⚠ NOT `scripts/apply_unit_type_change.py` (DEPRECATED): it hand-writes
`stock_levels`, which double-counts since mig 080 added
`after_transaction_update` (measured 288 vs 24), and it converts the ledger but
not the prices.

The replay engine is the app's own (`models.bsn_sync._sync_bsn_to_stock`, the
same Reconciliation Procedure `update_unit_conversion_ratio` runs). This script
only orchestrates: it cannot replay one unit at a time because unit_type, three
ratios and two prices must move together or the ledger is momentarily incoherent.

FREE EVIDENCE. The opening ADJUST is recomputed after the replay rather than
rescaled. If it lands near zero, the old plug was pure ledger error and there was
no real opening stock — the independent check that the plug itself could never
provide, because `opening = oracle - net` absorbs any error by construction.

    python3 scripts/2026_09_19_gross_to_piece.py --db PATH --threshold keep
    python3 scripts/2026_09_19_gross_to_piece.py --db PATH --threshold keep --apply
"""
import argparse
import os
import sqlite3
import sys

SOURCE = 'script:2026_09_19_gross_to_piece'
RATIO = 144
OPENING_NOTE = 'ยอดยกมา (back-solved)'
RECONCILE_NOTE = 'ปรับยอดคงเหลือ (แปลงหน่วย 2026-09-19)'
OPENING_STAMP = '2024-01-03 00:00:00'

# pid: (label, new base unit, new per-piece base_sell_price — /144 rounded UP,
#       Put's batch-1 convention: cost divides exactly, sell price rounds up)
PLAN = {
    1050: ('กระดาษทรายขัดไม้ จระเข้ #3', 'แผ่น', 6.46),
    1320: ('ดินสอช่างไม้พระจันทร์แท้', 'แท่ง', 8.69),
}


class RebaseRefused(Exception):
    """A precondition failed. Nothing has been written."""


def _stock(conn, pid):
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return row[0] if row else 0.0


def _ledger_sum(conn, pid):
    return conn.execute(
        "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id=?",
        (pid,)).fetchone()[0]


def rebase(conn, pid, new_unit, ratio, new_base_sell, preserve_stock, source=SOURCE):
    """Rebase one product onto `new_unit`, preserving its physical quantity.

    Returns the recomputed opening quantity — the free-evidence number.
    Raises RebaseRefused before writing anything if the product is not in the
    expected pre-state.

    `source` names the script in each cost-ledger note. It defaults to this
    file, so the run already applied to prod (1050/1320) is unchanged; a later
    script reusing this engine passes its own name (#586, 689/767).
    """
    from models import bsn_sync

    row = conn.execute(
        "SELECT unit_type, cost_price, base_sell_price, opening_cost FROM products WHERE id=?",
        (pid,)).fetchone()
    if row is None:
        raise RebaseRefused("pid %s does not exist — wrong DB?" % pid)
    unit_type, cost, base, opening_cost = row
    if unit_type == new_unit:
        raise RebaseRefused("pid %s is already %r — refusing to convert twice" % (pid, new_unit))
    if not cost or cost <= 0:
        raise RebaseRefused("pid %s: cost_price %r — a zero breaks the value check" % (pid, cost))

    # 1. product: cost divides EXACTLY (rounding loses stock value), sell price
    #    is the caller's rounded-up figure, threshold is left alone (Put 08-17:
    #    it is base-unit-expressed, so converting changes its physical meaning).
    conn.execute(
        "UPDATE products SET unit_type=?, cost_price=?, opening_cost=?, base_sell_price=? "
        "WHERE id=?",
        (new_unit, cost / ratio, (opening_cost or cost) / ratio, new_base_sell, pid))

    # 2. ratios. Every existing unit was expressed in the OLD base (a gross), so
    #    it scales by `ratio`; the new base unit is 1.0 by definition. A bill
    #    written at the old `unit_type` spelling means a GROSS, not a piece —
    #    that row is why a blanket ledger multiply would be wrong.
    for (bsn_unit,) in conn.execute(
            "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,)).fetchall():
        conn.execute(
            "UPDATE unit_conversions SET ratio=? WHERE product_id=? AND bsn_unit=?",
            (1.0 if bsn_unit == new_unit else ratio, pid, bsn_unit))
    conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,1.0) "
        "ON CONFLICT(product_id, bsn_unit) DO UPDATE SET ratio=1.0", (pid, new_unit))

    # 3. replay through the app's own engine, exactly as
    #    bsn_sync.update_unit_conversion_ratio does: reset synced -> delete every
    #    ledger row a BSN sync wrote -> re-sync -> verify identity-by-identity.
    synced_before = bsn_sync._synced_source_ids(conn, pid)
    for table in ('sales_transactions', 'purchase_transactions'):
        conn.execute("UPDATE %s SET synced_to_stock=0 WHERE product_id=?" % table, (pid,))
    conn.execute(
        "DELETE FROM transactions WHERE product_id=? AND ({})".format(
            " OR ".join("note LIKE ?" for _ in bsn_sync._BSN_LEDGER_NOTE_PATTERNS)),
        (pid, *bsn_sync._BSN_LEDGER_NOTE_PATTERNS))
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
    synced_after = bsn_sync._synced_source_ids(conn, pid)
    if synced_before - synced_after:
        raise RebaseRefused(
            "pid %s: replay lost %d movement(s) — %r"
            % (pid, len(synced_before - synced_after), sorted(synced_before - synced_after)[:5]))

    # 4. opening. Recomputed, never rescaled: the old plug was denominated in the
    #    old base AND contained the bug it was hiding, so neither its number nor
    #    its unit survives. Stamped strictly before the head because WACC sorts
    #    IN first on a created_at tie, so a row sharing the head's timestamp
    #    would be costed AFTER the first purchase.
    conn.execute("DELETE FROM transactions WHERE product_id=? AND note IN (?,?)",
                 (pid, OPENING_NOTE, RECONCILE_NOTE))
    needed = preserve_stock - _ledger_sum(conn, pid)
    if abs(needed) > 1e-9:
        head = conn.execute(
            "SELECT MIN(created_at) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
        stamp = (conn.execute("SELECT datetime(?, '-1 second')", (head,)).fetchone()[0]
                 if head else OPENING_STAMP)
        if needed > 0:
            conn.execute(
                "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, "
                "note, created_at) VALUES (?, 'ADJUST', ?, 'unit', ?, ?)",
                (pid, needed, OPENING_NOTE, stamp))
        else:
            # A NEGATIVE opening cannot sit at the head: the running balance would
            # start below zero and recalculate_product_wacc takes its
            # "negative stock — freeze WACC" branch, so WACC stops being a
            # weighted average for the whole history. phase_c_replay_apply hit
            # this and split it the same way — zero at the head, the real
            # reconcile at the TAIL, after every movement it has to survive.
            tail = conn.execute(
                "SELECT MAX(created_at) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
            conn.execute(
                "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, "
                "note, created_at) VALUES (?, 'ADJUST', ?, 'unit', ?, ?)",
                (pid, needed, RECONCILE_NOTE, tail or OPENING_STAMP))

    # 5. cost ledger: same physical history, re-denominated. The note is REBUILT
    #    from structured values and APPENDED — never text-substituted. Batch 1's
    #    v1 ran replace() on the unit WORDS and left the NUMBERS, producing false
    #    audit evidence from the step meant to keep the record honest.
    for lid, qty, unit_cost, note in conn.execute(
            "SELECT id, qty_change, unit_cost, note FROM product_cost_ledger "
            "WHERE product_id=? ORDER BY id", (pid,)).fetchall():
        conn.execute(
            "UPDATE product_cost_ledger SET qty_change=qty_change*?, stock_after=stock_after*?, "
            "unit_cost=unit_cost/?, wacc_after=wacc_after/?, note=? WHERE id=?",
            (ratio, ratio, ratio, ratio,
             "{} | แปลงหน่วย 2026-09-19: {:g} {} @ ฿{:.4f} → {:g} {} @ ฿{:.6f} ({})".format(
                 note or '(ไม่มีหมายเหตุเดิม)', qty, unit_type, unit_cost,
                 qty * ratio, new_unit, unit_cost / ratio, source),
             lid))
    return needed


# ── CLI ─────────────────────────────────────────────────────────────────────
# Shape borrowed from scripts/2026_08_17_bolt_dozen_to_piece.py: refuse on any
# drift BEFORE the first write, fingerprint every other product, assert
# invariants inside the transaction, and roll back unless --apply.

# Tables that would each need their own conversion story. Unlike batch 1 these
# products DO have sales, purchases, unit_conversions and a tier — that is the
# whole reason batch 1's script refuses them — so those four are handled, and
# only the ones with no story are required to be empty.
MUST_BE_EMPTY = (
    ('promotions', 'product_id'),
    ('platform_skus', 'internal_product_id'),
    ('ecommerce_listings', 'product_id'),
    ('conversion_formula_inputs', 'product_id'),
)


def preconditions(conn):
    bad = []
    for pid, (label, new_unit, _) in PLAN.items():
        row = conn.execute("SELECT unit_type, cost_price, base_sell_price FROM products "
                           "WHERE id=?", (pid,)).fetchone()
        if row is None:
            bad.append("%s (pid %s) does not exist — wrong DB?" % (label, pid))
            continue
        unit_type, cost, base = row
        if unit_type == new_unit:
            bad.append("%s: already %s — refusing to convert twice" % (label, new_unit))
            continue
        if unit_type != 'ตัว':
            bad.append("%s: unit_type is %r, expected 'ตัว'" % (label, unit_type))
        if not cost or cost <= 0 or not base or base <= 0:
            bad.append("%s: cost %r / base %r — a zero breaks the value check" % (label, cost, base))

        for table, col in MUST_BE_EMPTY:
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table, col), (pid,)).fetchone()[0]
            if n:
                bad.append("%s: %d row(s) in %s — needs its own conversion story" % (label, n, table))

        # An unsynced source row means the ledger does not currently reflect the
        # bills, so the replay would start from a base we never measured.
        for table in ('sales_transactions', 'purchase_transactions'):
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE product_id=? AND "
                             "COALESCE(synced_to_stock,0)=0" % table, (pid,)).fetchone()[0]
            if n:
                bad.append("%s: %d unsynced row(s) in %s — sync before converting" % (label, n, table))

        units = {u for (u,) in conn.execute(
            "SELECT DISTINCT unit FROM sales_transactions WHERE product_id=? "
            "UNION SELECT DISTINCT unit FROM purchase_transactions WHERE product_id=?",
            (pid, pid))}
        unknown = units - {u for (u,) in conn.execute(
            "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,))} - {new_unit}
        if unknown:
            bad.append("%s: bills use %s with no unit_conversions row — the replay would "
                       "silently skip them" % (label, sorted(unknown)))
    return bad


def fingerprint_others(conn):
    """Every non-target product's stock and money columns, as one comparable set."""
    pids = ','.join(str(p) for p in PLAN)
    rows = conn.execute(
        "SELECT p.id, p.unit_type, p.cost_price, p.base_sell_price, COALESCE(s.quantity, 0) "
        "  FROM products p LEFT JOIN stock_levels s ON s.product_id = p.id "
        " WHERE p.id NOT IN (%s) ORDER BY p.id" % pids).fetchall()
    return [tuple(r) for r in rows]


def snapshot(conn):
    out = {}
    for pid in PLAN:
        p = conn.execute("SELECT unit_type, cost_price, base_sell_price, low_stock_threshold "
                         "FROM products WHERE id=?", (pid,)).fetchone()
        out[pid] = {
            'unit_type': p[0], 'cost': p[1], 'base': p[2], 'threshold': p[3],
            'stock': _stock(conn, pid),
            'opening': conn.execute(
                "SELECT COALESCE(SUM(quantity_change),0) FROM transactions "
                "WHERE product_id=? AND note=?", (pid, OPENING_NOTE)).fetchone()[0],
            'tiers': [tuple(r) for r in conn.execute(
                "SELECT qty_label, price FROM product_price_tiers "
                "WHERE product_id=? ORDER BY qty_label", (pid,))],
            'ledger_value': conn.execute(
                "SELECT COALESCE(SUM(qty_change*unit_cost),0) FROM product_cost_ledger "
                "WHERE product_id=?", (pid,)).fetchone()[0],
        }
    return out


def assert_invariants(conn, before, others_before, openings):
    bad = []
    for pid, (label, new_unit, new_base) in PLAN.items():
        b = before[pid]
        row = conn.execute("SELECT unit_type, cost_price, base_sell_price, low_stock_threshold "
                           "FROM products WHERE id=?", (pid,)).fetchone()
        stock = _stock(conn, pid)

        if row[0] != new_unit:
            bad.append("%s: unit_type %r != %r" % (label, row[0], new_unit))
        # THE CONTRACT: the physical quantity does not move.
        if abs(stock - b['stock']) > 1e-9:
            bad.append("%s: stock %s != preserved %s" % (label, stock, b['stock']))
        led = _ledger_sum(conn, pid)
        if abs(led - stock) > 1e-9:
            bad.append("%s: stock %s != SUM(ledger) %s" % (label, stock, led))
        if abs(row[1] - b['cost'] / RATIO) > 1e-12:
            bad.append("%s: cost %r != %r (must divide EXACTLY)" % (label, row[1], b['cost'] / RATIO))
        if abs(row[2] - new_base) > 1e-9:
            bad.append("%s: base_sell %r != planned %r" % (label, row[2], new_base))
        if row[3] != b['threshold']:
            bad.append("%s: low_stock_threshold moved %r -> %r (Put: keep)" % (label, b['threshold'], row[3]))
        if [tuple(r) for r in conn.execute(
                "SELECT qty_label, price FROM product_price_tiers WHERE product_id=? "
                "ORDER BY qty_label", (pid,))] != b['tiers']:
            bad.append("%s: a tier price moved — a tier is the PACK total" % label)

        ratios = dict(conn.execute("SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?",
                                   (pid,)))
        if abs(ratios.get(new_unit, 0) - 1.0) > 1e-9:
            bad.append("%s: new base unit %s has ratio %r, expected 1.0" % (label, new_unit, ratios.get(new_unit)))
        for u, r in ratios.items():
            if u != new_unit and abs(r - RATIO) > 1e-9:
                bad.append("%s: %s ratio %r, expected %s" % (label, u, r, RATIO))

        # THE CORE CLAIM: a gross-unit bill and a piece-unit bill of the same
        # physical amount must now post the same number. Only assert it where
        # the product actually has both spellings.
        legs = conn.execute(
            "SELECT s.unit, s.qty, t.quantity_change FROM transactions t "
            "  JOIN sales_transactions s ON s.doc_no = t.reference_no AND s.product_id = t.product_id "
            " WHERE t.product_id=? AND t.txn_type='OUT'", (pid,)).fetchall()
        for unit, qty, change in legs:
            expect = -qty * (1.0 if unit == new_unit else RATIO)
            if abs(change - expect) > 1e-6:
                bad.append("%s: %g %s posted %g, expected %g" % (label, qty, unit, change, expect))

        val_before, val_after = b['stock'] * b['cost'], stock * row[1]
        if abs(val_before - val_after) > 0.005:
            bad.append("%s: stock value %.4f != %.4f" % (label, val_after, val_before))
        lv = conn.execute("SELECT COALESCE(SUM(qty_change*unit_cost),0) FROM product_cost_ledger "
                          "WHERE product_id=?", (pid,)).fetchone()[0]
        if abs(lv - b['ledger_value']) > 0.005:
            bad.append("%s: cost-ledger value %.4f != %.4f" % (label, lv, b['ledger_value']))
        n_noted = conn.execute("SELECT COUNT(*) FROM product_cost_ledger WHERE product_id=? "
                               "AND note LIKE '%แปลงหน่วย%'", (pid,)).fetchone()[0]
        n_rows = conn.execute("SELECT COUNT(*) FROM product_cost_ledger WHERE product_id=?",
                              (pid,)).fetchone()[0]
        if n_rows and n_noted != n_rows:
            bad.append("%s: %d/%d cost-ledger rows carry no conversion record" % (label, n_rows - n_noted, n_rows))

    if fingerprint_others(conn) != others_before:
        bad.append("a product OUTSIDE the plan changed")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', required=True)
    ap.add_argument('--threshold', required=True, choices=['keep', 'scale'],
                    help="low_stock_threshold is base-unit-expressed, so converting changes its "
                         "physical meaning. Put ruled 'keep' on 2026-08-17. No default: his call.")
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    if a.threshold != 'keep':
        print("REFUSED — only --threshold keep is implemented (Put's 2026-08-17 ruling).")
        sys.exit(2)

    # The app package must be importable. Deriving it from __file__ breaks the
    # moment this script is copied elsewhere (it was, to /tmp on the prod
    # container, and `models` then resolved against `/`), so try the real
    # import first and only fall back to a path guess.
    for cand in (os.environ.get('SENDY_APP_DIR'),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'inventory_app'),
                 '/app/inventory_app'):
        if cand and os.path.isdir(os.path.join(cand, 'models')):
            sys.path.insert(0, cand)
            break
    else:
        print("REFUSED — cannot locate inventory_app; set SENDY_APP_DIR")
        sys.exit(2)
    conn = sqlite3.connect(a.db, timeout=15)
    # set up exactly like models.database.get_connection: _sync_bsn_to_stock
    # indexes rows by NAME (row['bsn_code']), so a default tuple factory raises
    # "tuple indices must be integers".
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        problems = preconditions(conn)
        if problems:
            conn.rollback()
            print("REFUSED — preconditions not met:")
            for p in problems:
                print("  ✗", p)
            sys.exit(2)

        before = snapshot(conn)
        others_before = fingerprint_others(conn)
        print("BEFORE")
        for pid, b in before.items():
            print("  %-32s %-5s stock %8g  cost %10.4f  base %8.2f  opening-plug %g"
                  % (PLAN[pid][0], b['unit_type'], b['stock'], b['cost'], b['base'], b['opening']))

        conn.execute("INSERT INTO price_change_source (id, source) VALUES (1, ?) "
                     "ON CONFLICT(id) DO UPDATE SET source = excluded.source", (SOURCE,))
        openings = {}
        for pid, (label, new_unit, new_base) in PLAN.items():
            openings[pid] = rebase(conn, pid, new_unit, RATIO, new_base, before[pid]['stock'])
        conn.execute("INSERT INTO price_change_source (id, source) VALUES (1, NULL) "
                     "ON CONFLICT(id) DO UPDATE SET source = NULL")

        bad = assert_invariants(conn, before, others_before, openings)
    except RebaseRefused as exc:
        conn.rollback()
        print("REFUSED —", exc)
        sys.exit(2)
    except Exception:
        conn.rollback()
        raise

    if bad:
        conn.rollback()
        print("\nROLLED BACK — invariants failed:")
        for p in bad:
            print("  ✗", p)
        sys.exit(1)

    print("\nAFTER (in-transaction)")
    for pid, (label, new_unit, _) in PLAN.items():
        r = conn.execute("SELECT unit_type, cost_price, base_sell_price FROM products WHERE id=?",
                         (pid,)).fetchone()
        print("  %-32s %-5s stock %8g  cost %10.6f  base %8.2f"
              % (label, r[0], _stock(conn, pid), r[1], r[2]))

    print("\nRECOMPUTED OPENING — the free evidence")
    print("  Near zero means the old back-solved plug was pure ledger error, i.e. there was no")
    print("  real opening stock. The plug could never show this itself: opening = oracle - net")
    print("  absorbs any error by construction.")
    for pid, (label, _, _) in PLAN.items():
        print("    %-32s old plug %10g  ->  recomputed %10g" % (label, before[pid]['opening'], openings[pid]))

    if not a.apply:
        conn.rollback()
        print("\nREHEARSAL — rolled back, nothing written.")
        return

    conn.commit()
    conn.close()

    chk = sqlite3.connect(a.db)
    print("\nCOMMITTED — re-read on a new connection:")
    bad_n = 0
    for pid, (label, new_unit, new_base) in PLAN.items():
        r = chk.execute("SELECT p.unit_type, p.cost_price, p.base_sell_price, s.quantity "
                        "FROM products p JOIN stock_levels s ON s.product_id=p.id WHERE p.id=?",
                        (pid,)).fetchone()
        ok = (r[0] == new_unit and abs(r[2] - new_base) < 1e-9)
        bad_n += 0 if ok else 1
        print("  %s %-32s %-5s stock %g cost %.6f base %.2f"
              % ('OK ' if ok else 'BAD', label, r[0], r[3], r[1], r[2]))
    sys.exit(1 if bad_n else 0)


if __name__ == '__main__':
    main()
