"""Cleanup the 5 split-mapping stub products into their canonical siblings.

Each stub is a placeholder product (no brand, 0 stock, unit_type='ตัว') that
exists ONLY to serve as a FK target for BSN mappings where bsn_unit='ตัว'.
The canonical sibling is unit_type='แผง' with real stock + brand. Pre-mig-063
this setup "worked" only because the commission engine joined by bsn_code
alone — the stub's mapping silently routed ตัว sales into a 0-stock product,
corrupting stock and (via the duplication bug) inflating commission.

After this cleanup:
  - 1 product per "thing" (the แผง sibling is canonical)
  - 1 unit_conversions row per stub→sibling pair: bsn_unit='ตัว', ratio=0.5
    (1 BSN ตัว = 0.5 product แผง — sibling stocks the แผง pack)
  - Stub transactions are converted (× 0.5) before being reassigned, so
    stock_levels stay consistent after merge.
  - Stub product is_active=0 (kept for audit / FK safety).

Why a fresh script (not merge_product.py): merge_product.py does a plain
product_id reassign across all tables. For same-unit dupes that's correct.
For our case the stub and sibling have DIFFERENT unit_type ('ตัว' vs 'แผง')
so transactions.quantity_change must be RATIO-CONVERTED first, otherwise
sibling's stock balloons (or shrinks) by the unit mismatch.

Pairs (verified by audit 2026-05-20):
  pid 1995 (ตัว) → pid  17 (แผง)  code: 001ก2600   กลอนพฤกษา #260
  pid 1996 (ตัว) → pid  87 (แผง)  code: 030บ4033   บานพับ #480
  pid 1997 (ตัว) → pid  93 (แผง)  code: 030บ3043   บานพับ #3043
  pid 1999 (ตัว) → pid 149 (แผง)  code: 041ม2850   มือจับ #777
  pid 2000 (ตัว) → pid 162 (แผง)  code: 041ม3315 + 041ม3350  มือจับ #1000

Usage:
  Dry-run:  python scripts/cleanup_split_mapping_stubs.py
  Apply:    python scripts/cleanup_split_mapping_stubs.py --apply

A unique DB backup is taken automatically before --apply.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
BACKUPS = ROOT / "data" / "backups"

# (stub_pid, sibling_pid, ratio=ตัว→แผง)
# ratio convention: 1 BSN unit = ratio × product unit_type
# 1 ตัว = 0.5 แผง  →  ratio = 0.5
# Each ratio physically verified by the operator (Put) at the shop on
# 2026-05-20: every แผง of these SKUs holds exactly 2 ตัว.
PAIRS = [
    (1995, 17,  0.5),   # กลอนพฤกษา #260   — verified 1 แผง = 2 ตัว
    (1996, 87,  0.5),   # บานพับ #480      — verified 1 แผง = 2 ตัว
    (1997, 93,  0.5),   # บานพับ #3043     — verified 1 แผง = 2 ตัว
    (1999, 149, 0.5),   # มือจับ #777      — verified + price ratio = 0.49
    (2000, 162, 0.5),   # มือจับโค้ง #1000 — verified 1 แผง = 2 ตัว
]
STUB_BSN_UNIT = "ตัว"

# Sibling pids whose existing unit_conversions row for bsn_unit='ตัว' has a
# WRONG ratio (1.0 instead of 0.5) — overwriting it is intentional, not a
# Codex-flagged silent overwrite. Each entry must be backed by physical
# verification documented above. Empty by default; populating this set is
# the explicit acknowledgement that _check_existing_ratio requires.
RATIO_OVERRIDE_CONFIRMED = {
    93,    # บานพับ #3043 — DB had ratio=1.0; Put physically verified 1 แผง = 2 ตัว on 2026-05-20
    162,   # มือจับโค้ง #1000 — DB had ratio=1.0; Put physically verified 1 แผง = 2 ตัว on 2026-05-20
}

# Tables whose product_id can be reassigned without any column-level unit
# conversion. Each carries the BSN unit string in its OWN column (or is
# unit-neutral), so pointing the FK at the sibling preserves semantics.
# Verified for stubs 1995/1996/1997/1999/2000 via dry-run on 2026-05-20.
SAFE_REASSIGN_TABLES = {
    "product_code_mapping",       # mapping carries bsn_code + bsn_unit
    "sales_transactions",         # qty stays in BSN unit (own column)
    "purchase_transactions",      # qty stays in BSN unit (own column)
    "express_sales",              # qty stays in BSN unit (own column)
    "express_credit_note_lines",  # qty stays in BSN unit
    "product_locations",          # location, unit-neutral
    "product_barcodes",           # barcode metadata, unit-neutral
    "product_attributes",         # taxonomy
    "product_brand_map",          # brand mapping
    "supplier_product_mapping",   # supplier-code mapping
    "pending_product_suggestions",
    "ecommerce_listings",         # listing rows
    "platform_skus",              # marketplace SKU map
    "listing_bundles",
    "audit_log",                  # historical record
    "import_log",                 # historical record
}

# Tables specially handled by cleanup_pair (transactions converted by ratio,
# stock_levels recalculated, unit_conversions deduplicated).
SPECIAL_HANDLED = {"transactions", "stock_levels", "unit_conversions"}

# Tables that store unit-sensitive numbers (cost per unit, price per unit).
# If a stub has rows here we MUST NOT blindly reassign — bail out.
UNIT_SENSITIVE_TABLES = {
    "product_cost_ledger",
    "product_price_history",
    "product_price_tiers",
    "commission_overrides",   # fixed-per-unit / price-gate rules
    "conversion_formulas",    # manufacturing recipes
    "conversion_formula_inputs",
    "conversion_cost_log",
}


def _tables_with_product_id(conn):
    out = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        if "product_id" in cols:
            out.append(name)
    return out


def _ledger_stock(conn, pid):
    r = conn.execute(
        "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id = ?",
        (pid,),
    ).fetchone()
    return r[0] if r else 0


def _verify_stub_invariant(conn, stub, sibling):
    """Assert the stub matches what we audited: brand NULL, 0 stock,
    unit_type='ตัว', sibling unit_type='แผง'. Refuse to merge otherwise."""
    s = conn.execute(
        "SELECT brand_id, unit_type FROM products WHERE id=?",
        (stub,),
    ).fetchone()
    sl = conn.execute(
        "SELECT COALESCE(quantity, 0) FROM stock_levels WHERE product_id=?",
        (stub,),
    ).fetchone()
    stub_stock = sl[0] if sl else 0
    sib = conn.execute(
        "SELECT brand_id, unit_type FROM products WHERE id=?",
        (sibling,),
    ).fetchone()
    if s is None or sib is None:
        raise RuntimeError(f"pid {stub} or {sibling} not found")
    if s[0] is not None:
        raise RuntimeError(
            f"stub {stub} has brand_id={s[0]} — refusing (not a stub anymore)"
        )
    if stub_stock != 0:
        raise RuntimeError(
            f"stub {stub} has stock={stub_stock} — refusing "
            f"(real stock, can't blindly merge)"
        )
    if s[1] != "ตัว":
        raise RuntimeError(
            f"stub {stub} unit_type={s[1]!r}, expected 'ตัว' — refusing"
        )
    if sib[1] != "แผง":
        raise RuntimeError(
            f"sibling {sibling} unit_type={sib[1]!r}, expected 'แผง' — refusing"
        )


def _check_no_unit_sensitive_rows(conn, stub):
    """Refuse if the stub has rows in any unit-sensitive table (cost ledger,
    price history, etc.) — blanket FK reassign would corrupt them. None of
    the 5 stubs we know about have such rows; fail fast if a future caller
    introduces one."""
    bad = {}
    for t in UNIT_SENSITIVE_TABLES:
        try:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE product_id = ?", (stub,)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue  # table may not exist in this DB version
        if n:
            bad[t] = n
    if bad:
        raise RuntimeError(
            f"stub {stub} has rows in unit-sensitive tables {bad} — "
            f"blanket reassign would corrupt unit-keyed numbers. "
            f"Add table-specific conversion before re-running."
        )


def _check_no_unexpected_tables(conn, stub, touched):
    """Refuse if any table OUTSIDE the safe allowlist (or special-handled
    set) has rows for the stub. Forces us to think about new tables added
    later instead of silently inheriting blanket reassign."""
    known = SAFE_REASSIGN_TABLES | SPECIAL_HANDLED
    unknown = {t: n for t, n in touched.items() if t not in known}
    if unknown:
        raise RuntimeError(
            f"stub {stub} has rows in tables not in the safe allowlist: "
            f"{unknown}. Decide table-specific handling and update "
            f"SAFE_REASSIGN_TABLES or UNIT_SENSITIVE_TABLES before re-running."
        )


def _check_mapping_conflicts(conn, stub, sibling):
    """Refuse if moving stub's product_code_mapping rows to sibling would
    create rows sharing the same (bsn_code, bsn_unit). The table enforces
    UNIQUE(bsn_code, bsn_unit) so a collision would raise IntegrityError
    mid-transaction; check up-front and produce a clear message instead."""
    conflicts = conn.execute(
        """
        SELECT m1.bsn_code, m1.bsn_unit
          FROM product_code_mapping m1
          JOIN product_code_mapping m2
            ON m1.bsn_code = m2.bsn_code
           AND m1.bsn_unit = m2.bsn_unit
           AND m1.id != m2.id
         WHERE m1.product_id = ? AND m2.product_id = ?
        """,
        (stub, sibling),
    ).fetchall()
    if conflicts:
        raise RuntimeError(
            f"moving stub {stub} → sibling {sibling} would create duplicate "
            f"(bsn_code, bsn_unit) mapping rows: {conflicts}. Resolve "
            f"(consolidate or delete one side) in /mapping before re-running."
        )


def _check_existing_ratio(conn, sibling, ratio):
    """If sibling already has a unit_conversions row for STUB_BSN_UNIT with
    a DIFFERENT ratio, refuse — UNLESS the sibling is explicitly listed in
    RATIO_OVERRIDE_CONFIRMED (= operator has physically verified the new
    ratio against shop inventory)."""
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit=?",
        (sibling, STUB_BSN_UNIT),
    ).fetchone()
    if row is None or abs(row[0] - ratio) <= 1e-6:
        return
    if sibling in RATIO_OVERRIDE_CONFIRMED:
        print(
            f"  [override] sibling {sibling}: replacing ratio "
            f"{row[0]} → {ratio} (explicit RATIO_OVERRIDE_CONFIRMED)"
        )
        return
    raise RuntimeError(
        f"sibling {sibling} already has unit_conversions for "
        f"bsn_unit={STUB_BSN_UNIT!r} with ratio={row[0]}, script wants "
        f"ratio={ratio}. Add the sibling pid to RATIO_OVERRIDE_CONFIRMED "
        f"with a verification comment before re-running."
    )


def cleanup_pair(conn, stub, sibling, ratio, *, apply):
    """Merge one stub into its sibling with unit conversion. Returns a dict
    of before/after numbers for verification.

    Called inside an open transaction. Caller commits / rollbacks.
    """
    _verify_stub_invariant(conn, stub, sibling)
    _check_no_unit_sensitive_rows(conn, stub)
    _check_mapping_conflicts(conn, stub, sibling)
    _check_existing_ratio(conn, sibling, ratio)

    # Capture before state for verification
    stub_txn_qty_before = _ledger_stock(conn, stub)  # in stub's unit (ตัว)
    sib_stock_before = _ledger_stock(conn, sibling)  # in sibling's unit (แผง)
    expected_after = round(sib_stock_before + stub_txn_qty_before * ratio, 4)

    # Count touched rows per table
    tabs = _tables_with_product_id(conn)
    touched = {}
    for t in tabs:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE product_id = ?", (stub,)
        ).fetchone()[0]
        if n:
            touched[t] = n

    _check_no_unexpected_tables(conn, stub, touched)

    if not apply:
        return {
            "stub": stub,
            "sibling": sibling,
            "ratio": ratio,
            "stub_ledger_before_in_stub_unit": stub_txn_qty_before,
            "sibling_ledger_before": sib_stock_before,
            "expected_sibling_ledger_after": expected_after,
            "touched_tables": touched,
        }

    # ── APPLY ────────────────────────────────────────────────────────────

    # 1. Add (or update) unit_conversions for sibling: bsn_unit='ตัว', ratio.
    #    _check_existing_ratio already refused if a conflicting ratio exists,
    #    so ON CONFLICT here only fires when the existing ratio equals ours.
    conn.execute(
        """
        INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
        VALUES (?, ?, ?)
        ON CONFLICT(product_id, bsn_unit) DO UPDATE SET ratio = excluded.ratio
        """,
        (sibling, STUB_BSN_UNIT, ratio),
    )

    # 2. Convert stub's transactions to sibling's unit AND reassign FK.
    #    Both must happen together — pre-fix had only the qty conversion,
    #    leaving product_id=stub. The DB-wide leftover scan caught it
    #    (the transaction rolled back).
    conn.execute(
        "UPDATE transactions SET "
        "  quantity_change = quantity_change * ?, "
        "  product_id = ? "
        "WHERE product_id = ?",
        (ratio, sibling, stub),
    )

    # 3. unit_conversions: move stub's rows to sibling, dedup on bsn_unit.
    sib_units = {
        r[0]
        for r in conn.execute(
            "SELECT bsn_unit FROM unit_conversions WHERE product_id=?",
            (sibling,),
        )
    }
    for row in conn.execute(
        "SELECT id, bsn_unit FROM unit_conversions WHERE product_id=?",
        (stub,),
    ).fetchall():
        if row[1] in sib_units:
            conn.execute("DELETE FROM unit_conversions WHERE id=?", (row[0],))
        else:
            conn.execute(
                "UPDATE unit_conversions SET product_id=? WHERE id=?",
                (sibling, row[0]),
            )

    # 4. Reassign rows in tables on the safe allowlist (only those touched).
    for t, _n in touched.items():
        if t in SPECIAL_HANDLED:
            continue
        # SAFE_REASSIGN_TABLES — verified by _check_no_unexpected_tables.
        conn.execute(
            f"UPDATE {t} SET product_id = ? WHERE product_id = ?",
            (sibling, stub),
        )

    # 5. Recalc sibling's stock_levels from the now-merged ledger
    conn.execute("DELETE FROM stock_levels WHERE product_id = ?", (sibling,))
    conn.execute(
        "INSERT INTO stock_levels (product_id, quantity) "
        "SELECT ?, COALESCE(SUM(quantity_change), 0) FROM transactions "
        "WHERE product_id = ?",
        (sibling, sibling),
    )

    # 6. Verify BEFORE deactivation (Codex pass 3): full DB-wide scan for
    # any leftover stub references. Skip stock_levels (handled in step 7)
    # and products (the stub itself stays for audit/FK safety).
    sib_stock_after = _ledger_stock(conn, sibling)
    if abs(sib_stock_after - expected_after) > 1e-6:
        raise RuntimeError(
            f"stock mismatch for sibling {sibling}: "
            f"after={sib_stock_after} expected={expected_after}"
        )
    leftover_per_table = {}
    for t in _tables_with_product_id(conn):
        if t in ("stock_levels", "products"):
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE product_id = ?", (stub,)
        ).fetchone()[0]
        if n:
            leftover_per_table[t] = n
    if leftover_per_table:
        raise RuntimeError(
            f"stub {stub} still has product_id refs in {leftover_per_table} "
            f"after reassign — refusing to deactivate"
        )

    # 7. Now-safe to drop stub stock_levels + deactivate.
    conn.execute("DELETE FROM stock_levels WHERE product_id = ?", (stub,))
    conn.execute("UPDATE products SET is_active = 0 WHERE id = ?", (stub,))

    return {
        "stub": stub,
        "sibling": sibling,
        "ratio": ratio,
        "stub_ledger_before_in_stub_unit": stub_txn_qty_before,
        "sibling_ledger_before": sib_stock_before,
        "sibling_ledger_after": sib_stock_after,
        "expected_sibling_ledger_after": expected_after,
        "touched_tables": touched,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    if a.apply:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = BACKUPS / f"inventory.before-stub-cleanup-{ts}.db"
        shutil.copy2(a.db, backup)
        print(f"Backup → {backup}")

    conn = sqlite3.connect(str(a.db))
    conn.execute("PRAGMA foreign_keys = OFF")  # match app behaviour

    if a.apply:
        conn.execute("BEGIN")
    try:
        results = []
        for stub, sibling, ratio in PAIRS:
            print(f"\n── pair {stub} → {sibling} (ratio={ratio}) ──")
            res = cleanup_pair(conn, stub, sibling, ratio, apply=a.apply)
            results.append(res)
            print(f"  stub ledger (in ตัว):     {res['stub_ledger_before_in_stub_unit']}")
            print(f"  sibling ledger before:    {res['sibling_ledger_before']}")
            print(f"  expected sibling after:   {res['expected_sibling_ledger_after']}")
            if a.apply:
                print(f"  sibling ledger after:     {res['sibling_ledger_after']}  OK")
            print(f"  tables touched: {res['touched_tables']}")
    except Exception as e:
        if a.apply:
            conn.execute("ROLLBACK")
        print(f"\nERROR — {'rolled back' if a.apply else 'aborted'}: {e}", file=sys.stderr)
        conn.close()
        return 1

    if a.apply:
        conn.execute("COMMIT")
        print(f"\nAPPLIED. {len(results)} stubs cleaned up.")
        sibs = [str(sib) for _, sib, _ in PAIRS]
        print("\n⚠ WACC stale until you run the follow-up recalc:")
        print(f"  cd {ROOT} && {sys.executable} -c \"")
        print(f"import sys, sqlite3; sys.path.insert(0, 'inventory_app');")
        print(f"import models; c = sqlite3.connect('{a.db}');")
        print(f"c.row_factory = sqlite3.Row; c.execute('PRAGMA foreign_keys = OFF')")
        print(f"for sib in [{', '.join(sibs)}]: models.recalculate_product_wacc(sib, c)")
        print(f"c.commit(); c.close()\"")
        print("(kept outside the merge transaction so a WACC failure cannot leave the merge half-applied)")
    else:
        print(f"\nDRY-RUN — {len(results)} pairs would be merged.")
        print("Re-run with --apply to commit (auto DB backup taken).")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
