#!/usr/bin/env python3
"""
P2+P3 combined — reactivate + split the 3 REMAINING pack/loose hinge SKUs:
    #412 GP        แผง pid 81  <-> ตัว pid 111
    #2043 SS       แผง pid 90  <-> ตัว pid 1886
    ผีเสื้อ 4in      แผง pid 96  <-> ตัว pid 1888   (96 already active)

Implements the Reconciliation Procedure (RP, plan §4) from
`projects/pack-loose-sku-split/plan.md`, generalized from the proven P0
template (`p0_split_3p5in.py`) to Phase 2+3 (§6) for these 3 pairs:

  - Reactivate pid 81, 90 (`is_active=1`). 96/111/1886/1888 already active.
  - Split product_code_mapping unit-aware (mig 124 restored `bsn_unit`):
    convert/insert the unit-specific rows, delete the stale blank ('')
    catch-all so it can't shadow a third/unnormalized unit.
        (030บ3412,'แผง')->81   (030บ3412,'ตัว')->111
        (030บ5111,'แผง')->90   (030บ5111,'ตัว')->1886
        (030บ5022,'ตัว')->1886
        (030บ2044,'แผง')->90
        (030บ4000,'แผง')->96   (030บ4000,'ตัว')->1888
  - Remove the `แผง -> 3` unit_conversions merge-artifact rows on the ตัว
    SKUs (111, 1886, 1888) — no longer needed once แผง routes to its own
    แผง SKU (แผง SKU is แผง-base -> implicit 1:1, no conversion row needed).
  - Re-point sales_transactions/purchase_transactions rows for the 5 codes,
    by their ACTUAL STORED unit (sales rows are pre-normalized e.g. 'แผง'/
    'ตัว'; purchase rows store the RAW acronym e.g. 'ตว') -> normalize each
    row's stored unit via bsn_units.normalize_unit and match against the
    mapping target's (already-normalized) unit.
  - Delete the stale BSN/merge-artifact ledger on all 6 SKUs, re-sync via
    the REAL `models._sync_bsn_to_stock`, then pin final stock_levels back
    to the pre-op snapshot with one balancing ADJUST per product (Put's
    decision: final stock = current stock, preserved, not recounted).
  - Restore products.shopee_stock/lazada_stock + platform_skus.stock for
    the 6 SKUs to their pre-op snapshot too (the re-sync in the previous
    step replays historical หน้าร้าน* marketplace deductions on these
    counters; on the current prod data they're all 0 so it's a harmless
    MAX(0,...) no-op, but that's a coincidence of today's data — restoring
    them explicitly makes the whole operation stock-preserving for the
    online counters regardless of their value at apply time).
  - product 80 (Golden Lion #412 แผง, no BSN code, manual stock) is NEVER
    touched — excluded from every scope list, verified byte-identical
    before/after.
  - Runs every §4.6 invariant (+ this phase's extras) as an assertion and
    prints PASS/FAIL.

Deliberate deviation from the literal orchestrator instructions: the
mapping-table split is implemented as raw SQL against the SAME connection
used for every other step in this single transaction, NOT via
`models.upsert_mapping()`. That helper calls `get_connection()` internally
and commits+closes on its own — invoked mid-transaction on the same sqlite
file it would contend for the write lock our already-open transaction
holds (real risk: a `database is locked` timeout, or worse, a partial
commit that breaks the whole-script rollback-on-failure guarantee this
script depends on). The raw SQL here reproduces upsert_mapping's own
UPDATE-then-INSERT logic exactly, just on `conn`.

⚠ SIMULATION-FIRST TOOL. This script REFUSES to run against the live DB
(`sendy_erp/inventory_app/instance/inventory.db`) unless you pass BOTH
`--db <live path>` AND `--apply`. Default usage is always against a
`.backup`-made COPY:

    sqlite3 "<live db>" ".backup '/path/to/copy/inventory.db'"
    DATA_DIR=/path/to/copy SECRET_KEY=x ADMIN_PASSWORD=x \\
        ~/.virtualenvs/erp/bin/python p2p3_split_hinges.py --db /path/to/copy/inventory.db

The target file's basename MUST be `inventory.db` (config.py hardcodes that
name under DATA_DIR).

Re-runnable: mapping/remap/unit_conversions steps are idempotent (plain
UPDATE/DELETE/INSERT-OR-IGNORE), the ledger DELETE matches by note pattern
(nothing left to delete the 2nd time), and the re-sync only processes
synced_to_stock=0 rows. The final pin ADJUST is computed fresh each run
against whatever the ledger currently nets to, so re-running after a first
successful run should compute an ADJUST of ~0.
"""
import argparse
import os
import sys
from datetime import date

# ── Fixed scope for this phase (see plan.md §6 Phase 2 + Phase 3) ──────────
PID_EXCLUDED = 80  # Golden Lion #412 แผง — no BSN code, manual stock. NEVER touch.

HINGES = [
    {'name': '#412 GP',    'pid_panel': 81, 'pid_loose': 111},
    {'name': '#2043 SS',   'pid_panel': 90, 'pid_loose': 1886},
    {'name': 'ผีเสื้อ 4in',  'pid_panel': 96, 'pid_loose': 1888},
]
PIDS_TO_REACTIVATE = [81, 90]          # 96/111/1886/1888 already active
UC_ARTIFACT_PIDS = [111, 1886, 1888]   # remove bogus bsn_unit='แผง' ratio=3 rows here

# (bsn_code, target_unit [already normalized], target_product_id)
MAPPING_TARGETS = [
    ('030บ3412', 'แผง', 81),
    ('030บ3412', 'ตัว', 111),
    ('030บ5111', 'แผง', 90),
    ('030บ5111', 'ตัว', 1886),
    ('030บ5022', 'ตัว', 1886),
    ('030บ2044', 'แผง', 90),
    ('030บ4000', 'แผง', 96),
    ('030บ4000', 'ตัว', 1888),
]
CODES = sorted({c for c, _, _ in MAPPING_TARGETS})

ALL_PIDS = sorted({h['pid_panel'] for h in HINGES} | {h['pid_loose'] for h in HINGES})
PID_TO_HINGE = {}
for h in HINGES:
    PID_TO_HINGE[h['pid_panel']] = h['name']
    PID_TO_HINGE[h['pid_loose']] = h['name']

PIN_NOTE = 'ตั้งต้นหลังแยก SKU 2026-07-02'
BSN_LEDGER_DELETE_SQL = (
    f"DELETE FROM transactions WHERE product_id IN ({','.join('?' * len(ALL_PIDS))}) "
    "AND (note LIKE 'BSN%' OR note LIKE 'รวม%' OR note LIKE '%reconcile ledger%')"
)
DOC_BACKED_SALES_NOTES = ('BSN ขาย', 'BSN ขาย-คืน')
DOC_BACKED_PURCHASE_NOTES = ('BSN ซื้อ', 'BSN ซื้อ-คืน')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIVE_DB_PATH = os.path.realpath(
    os.path.join(REPO_ROOT, 'sendy_erp', 'inventory_app', 'instance', 'inventory.db')
)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', required=True, help="Path to the target inventory.db (basename must be 'inventory.db')")
    p.add_argument('--apply', action='store_true',
                    help="Required in addition to --db to run against the LIVE db path. "
                         "Has no other effect (writes always happen on whatever --db points at).")
    return p.parse_args()


def guard_live_path(db_path, apply_flag):
    resolved = os.path.realpath(db_path)
    if resolved == LIVE_DB_PATH and not apply_flag:
        print(f"REFUSING: {db_path} resolves to the LIVE db ({LIVE_DB_PATH}) "
              f"and --apply was not passed. Run against a .backup COPY instead, "
              f"or pass --apply if you have explicit sign-off to touch live.",
              file=sys.stderr)
        sys.exit(1)
    if os.path.basename(db_path) != 'inventory.db':
        print(f"REFUSING: --db basename must be 'inventory.db' (config.py hardcodes this "
              f"under DATA_DIR). Got: {db_path}", file=sys.stderr)
        sys.exit(1)


def load_app_modules(db_path):
    """Point config.DATABASE_PATH at db_path (via DATA_DIR) BEFORE importing
    database/models, then return (database, models) modules."""
    data_dir = os.path.dirname(os.path.abspath(db_path))
    os.environ['DATA_DIR'] = data_dir
    os.environ.setdefault('SECRET_KEY', 'x')
    os.environ.setdefault('ADMIN_PASSWORD', 'x')
    # SENDY_APP_DIR override lets this run on prod (Railway layout /app/inventory_app,
    # no 'sendy_erp/' subdir) as well as locally.
    inventory_app_dir = os.environ.get('SENDY_APP_DIR') or os.path.join(REPO_ROOT, 'sendy_erp', 'inventory_app')
    sys.path.insert(0, inventory_app_dir)
    import database  # noqa: E402
    import models    # noqa: E402
    return database, models


def confirm_on_copy(conn, expected_path, apply_flag=False):
    """Independent check that this connection really points at our target
    file, not the live DB — never trust env-var wiring blindly."""
    row = conn.execute("PRAGMA database_list").fetchone()
    actual = os.path.realpath(row['file'])
    expected = os.path.realpath(expected_path)
    print(f"  connection file : {actual}")
    print(f"  expected file   : {expected}")
    assert actual == expected, "Connection is NOT pointing at the target --db path!"
    if not apply_flag:
        assert actual != LIVE_DB_PATH, "Connection is pointing at the LIVE db — aborting (pass --apply for an explicit, signed-off live apply)."
        print("  CONFIRMED: connected to the target copy, not live.\n")
    else:
        print(f"  APPLYING (explicit --apply) to: {actual}\n")


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def qone(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def snapshot_stock(conn, pids):
    rows = q(conn, f"SELECT product_id, quantity FROM stock_levels WHERE product_id IN ({','.join('?'*len(pids))})", pids)
    snap = {pid: 0 for pid in pids}
    for r in rows:
        snap[r['product_id']] = r['quantity']
    return snap


def main():
    args = parse_args()
    guard_live_path(args.db, args.apply)

    if not os.path.exists(args.db):
        print(f"REFUSING: --db path does not exist: {args.db}", file=sys.stderr)
        sys.exit(1)

    database, models = load_app_modules(args.db)
    conn = database.get_connection()

    print("=== 0. Confirm connection targets the intended DB ===")
    confirm_on_copy(conn, args.db, args.apply)

    try:
        run(conn, models)
        conn.commit()
        print("\nCOMMITTED to the target DB (this should be the COPY — re-check the path above).")
    except Exception:
        conn.rollback()
        print("\nROLLED BACK due to an error/assertion failure. No changes persisted.", file=sys.stderr)
        raise
    finally:
        conn.close()


def upsert_mapping_row(conn, bsn_code, bsn_unit, product_id, bsn_name, is_ignored, ignore_reason):
    """Reproduces models.upsert_mapping()'s UPDATE-then-INSERT logic, on the
    SAME conn as every other step (see module docstring for why we don't
    call the real helper mid-transaction)."""
    updated = conn.execute("""
        UPDATE product_code_mapping SET
            bsn_name      = ?,
            product_id    = ?,
            is_ignored    = ?,
            ignore_reason = ?
        WHERE bsn_code = ? AND bsn_unit = ?
    """, (bsn_name, product_id, is_ignored, ignore_reason, bsn_code, bsn_unit)).rowcount
    if not updated:
        conn.execute("""
            INSERT OR IGNORE INTO product_code_mapping
                (bsn_code, bsn_name, product_id, is_ignored, ignore_reason, bsn_unit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (bsn_code, bsn_name, product_id, is_ignored, ignore_reason, bsn_unit))


def run(conn, models):
    results = []

    def check(name, cond, detail):
        status = "PASS" if cond else "FAIL"
        results.append((name, status, detail))
        print(f"  [{status}] {name} — {detail}")

    # ── STEP 1: snapshot pid 80 baseline (must stay byte-identical) ────────
    print("=== 1. SNAPSHOT pid 80 (Golden Lion #412 panel, EXCLUDED) baseline ===")
    pid80_before = qone(conn, "SELECT is_active FROM products WHERE id=?", (PID_EXCLUDED,))
    pid80_stock_before = snapshot_stock(conn, [PID_EXCLUDED])[PID_EXCLUDED]
    pid80_ledger_before = q(conn, "SELECT note, COUNT(*) c, SUM(quantity_change) s FROM transactions "
                                    "WHERE product_id=? GROUP BY note ORDER BY note", (PID_EXCLUDED,))
    print(f"  pid 80 is_active={pid80_before['is_active']}, stock={pid80_stock_before}")
    for r in pid80_ledger_before:
        print(f"    note={r['note']!r} rows={r['c']} sum={r['s']}")

    # ── STEP 2: snapshot target stocks (RP §4.1) — pin targets ──────────────
    print("\n=== 2. SNAPSHOT stock_levels for the 6 in-scope SKUs (target final stock) ===")
    snapshot = snapshot_stock(conn, ALL_PIDS)
    for pid in ALL_PIDS:
        print(f"  pid {pid} ({PID_TO_HINGE[pid]}): snapshot = {snapshot[pid]}")

    # ── STEP 3: capture full before-state for the report ────────────────────
    print("\n=== 3. Capture before-state (mapping / unit_conversions / is_active / row counts) ===")
    before_mapping = {code: q(conn, "SELECT bsn_code, bsn_unit, bsn_name, product_id, is_ignored, ignore_reason "
                                      "FROM product_code_mapping WHERE bsn_code=? ORDER BY bsn_unit", (code,))
                       for code in CODES}
    for code, rows in before_mapping.items():
        for r in rows:
            print(f"  mapping {code!r} unit={r['bsn_unit']!r} -> pid {r['product_id']} (name={r['bsn_name']!r})")

    before_active = {pid: qone(conn, "SELECT is_active FROM products WHERE id=?", (pid,))['is_active']
                      for pid in (81, 90, 96, 111, 1886, 1888)}
    print(f"  is_active before: {before_active}")

    before_uc_artifact = {pid: qone(conn, "SELECT ratio FROM unit_conversions WHERE product_id=? AND bsn_unit='แผง'", (pid,))
                           for pid in UC_ARTIFACT_PIDS}
    for pid, r in before_uc_artifact.items():
        print(f"  pid {pid} bogus แผง unit_conversion ratio (before) = {r['ratio'] if r else None}")

    before_bsn_count = {pid: qone(conn, "SELECT COUNT(*) c FROM transactions WHERE product_id=? AND "
                                          "(note LIKE 'BSN%' OR note LIKE 'รวม%' OR note LIKE '%reconcile ledger%')", (pid,))['c']
                         for pid in ALL_PIDS}
    print(f"  BSN/merge-note transactions count before (per pid): {before_bsn_count}")

    # Pre-move row-count totals per code (must be conserved — no rows silently dropped).
    pre_sales_count = {code: qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE bsn_code=?", (code,))['c']
                        for code in CODES}
    pre_purch_count = {code: qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE bsn_code=?", (code,))['c']
                        for code in CODES}
    print(f"  pre-move sales_transactions row counts by code: {pre_sales_count}")
    print(f"  pre-move purchase_transactions row counts by code: {pre_purch_count}")

    # Platform-stock baseline (see risk note in report — hinge #412 code
    # 030บ3412 has real หน้าร้าน B/L/S marketplace lines; _sync_bsn_to_stock
    # deducts shopee/lazada_stock + platform_skus.stock for OUT rows with
    # those customers on every sync, and we're forcing a full re-sync).
    before_platform = {pid: dict(qone(conn, "SELECT shopee_stock, lazada_stock FROM products WHERE id=?", (pid,)))
                        for pid in ALL_PIDS}
    before_platform_skus = q(conn, f"SELECT id, platform, internal_product_id, stock FROM platform_skus "
                                     f"WHERE internal_product_id IN ({','.join('?'*len(ALL_PIDS))})", ALL_PIDS)
    print(f"  products.shopee_stock/lazada_stock before: {before_platform}")
    print(f"  platform_skus.stock before: {[dict(r) for r in before_platform_skus]}")

    # ── STEP 4: ORPHAN-SAFETY PRE-CHECK (RP §4.6, before ANY mutation) ──────
    # Classify every transactions row about to be deleted (BSN%/รวม%/reconcile
    # ledger% on the 6 pids) as either:
    #   (a) doc-backed (BSN ขาย/ขาย-คืน/ซื้อ/ซื้อ-คืน) — resolve via
    #       (doc_no=reference_no AND product_id=t.product_id) against
    #       sales_transactions/purchase_transactions (product_id disambiguates
    #       multi-line purchase docs that share one doc_no — a plain doc_no
    #       join fans out across sibling lines). "rebuilt" if the resolved
    #       row's bsn_code is one of our 5 known codes (will be re-pointed +
    #       re-synced fresh); else needs an independent duplicate-elsewhere
    #       check (P0-style) before we can call it safe.
    #   (b) non-doc-backed (รวม%/reconcile-ledger%, reference_no IS NULL) —
    #       pure ADJUST merge-artifact rows with no source document. These
    #       are discarded BY DESIGN (that is the point of this RP); their
    #       net effect is absorbed into the final pinning ADJUST (step 9).
    print("\n=== 4. ORPHAN-SAFETY PRE-CHECK (before any mutation) ===")
    to_delete_preview = q(conn,
        f"SELECT id, product_id, note, reference_no, quantity_change FROM transactions "
        f"WHERE product_id IN ({','.join('?'*len(ALL_PIDS))}) "
        f"AND (note LIKE 'BSN%' OR note LIKE 'รวม%' OR note LIKE '%reconcile ledger%')", ALL_PIDS)
    print(f"  total rows scheduled for deletion: {len(to_delete_preview)}")

    orphans = []
    rebuilt_count = 0
    artifact_count = 0
    for row in to_delete_preview:
        note = row['note']
        if note in DOC_BACKED_SALES_NOTES:
            # sales_transactions.doc_no already carries the printed "-N" line
            # suffix, so it's (almost) unique per line item — do NOT also
            # require product_id=t.product_id here: these are exactly the
            # STRANDED historical rows whose ledger product_id (e.g. pid 81,
            # from before the 2026-06-19 merge) no longer matches the doc's
            # CURRENT sales_transactions.product_id (e.g. 111, post-merge).
            # That mismatch is the whole reason this row is being rebuilt.
            resolved_rows = q(conn, "SELECT bsn_code, product_id FROM sales_transactions WHERE doc_no=?",
                               (row['reference_no'],))
            if resolved_rows and any(r['bsn_code'] in CODES for r in resolved_rows):
                rebuilt_count += 1
            else:
                orphans.append((row['id'], row['product_id'], note, row['reference_no'],
                                 [dict(r) for r in resolved_rows]))
        elif note in DOC_BACKED_PURCHASE_NOTES:
            resolved = qone(conn, "SELECT bsn_code, product_id FROM purchase_transactions "
                                    "WHERE doc_no=? AND product_id=?", (row['reference_no'], row['product_id']))
            if resolved and resolved['bsn_code'] in CODES:
                rebuilt_count += 1
            else:
                orphans.append((row['id'], row['product_id'], note, row['reference_no'],
                                 dict(resolved) if resolved else None))
        else:
            # รวม% / reconcile-ledger% artifact — must have NO reference_no
            # (pure ADJUST, not doc-backed). If it unexpectedly HAS one,
            # treat it as needing manual review rather than assume it's junk.
            if row['reference_no'] is None:
                artifact_count += 1
            else:
                orphans.append((row['id'], row['product_id'], note, row['reference_no'], 'UNEXPECTED: artifact-pattern row has a reference_no'))

    print(f"  doc-backed rows resolved in-scope (will be rebuilt fresh by resync): {rebuilt_count}")
    print(f"  non-doc-backed merge-artifact rows (discarded by design, absorbed by pin): {artifact_count}")
    print(f"  UNRESOLVED orphans (need independent duplicate-elsewhere check): {len(orphans)}")
    for o in orphans:
        print(f"    id={o[0]} pid={o[1]} note={o[2]!r} reference_no={o[3]!r} resolved={o[4]}")

    if orphans:
        # Independent check (P0-style): is each orphan row a verified
        # duplicate that ALSO exists unchanged on some other product_id?
        # We do NOT auto-clear these — surface them and stop.
        raise AssertionError(
            f"{len(orphans)} unresolved orphan row(s) found in the pre-delete scan — "
            f"see printed detail above. STOPPING per honesty rule (ambiguous cause, "
            f"do not guess). Re-run after manual duplicate-elsewhere verification."
        )
    check("orphan-safety pre-check: 0 unresolved orphans",
          len(orphans) == 0,
          f"rebuilt={rebuilt_count}, artifact={artifact_count}, orphans={len(orphans)}, "
          f"total_scheduled={len(to_delete_preview)} (rebuilt+artifact should == total_scheduled: "
          f"{rebuilt_count + artifact_count} == {len(to_delete_preview)})")

    # ── STEP 5: reactivate 81, 90 ────────────────────────────────────────────
    print("\n=== 5. Reactivate pid 81, 90 (is_active=1) ===")
    for pid in PIDS_TO_REACTIVATE:
        conn.execute("UPDATE products SET is_active=1 WHERE id=?", (pid,))
        print(f"  SQL: UPDATE products SET is_active=1 WHERE id={pid}")

    # ── STEP 6: split product_code_mapping (unit-aware) ─────────────────────
    print("\n=== 6. Split product_code_mapping: upsert unit-specific rows, delete blank catch-all ===")
    for code in CODES:
        blank = qone(conn, "SELECT bsn_name, is_ignored, ignore_reason FROM product_code_mapping "
                             "WHERE bsn_code=? AND bsn_unit=''", (code,))
        if blank:
            bsn_name, is_ignored, ignore_reason = blank['bsn_name'], blank['is_ignored'], blank['ignore_reason']
        else:
            # Re-run case: a prior run already converted/deleted the blank
            # catch-all row. Recover bsn_name from an existing split row
            # instead of failing, so this step stays idempotent.
            existing = qone(conn, "SELECT bsn_name, is_ignored, ignore_reason FROM product_code_mapping "
                                    "WHERE bsn_code=? LIMIT 1", (code,))
            if not existing:
                raise AssertionError(f"No mapping row at all (blank or split) for {code!r} — "
                                      f"reality contradicts the plan; stopping.")
            bsn_name, is_ignored, ignore_reason = existing['bsn_name'], existing['is_ignored'], existing['ignore_reason']
        targets = [(u, pid) for c, u, pid in MAPPING_TARGETS if c == code]
        for unit, pid in targets:
            upsert_mapping_row(conn, code, unit, pid, bsn_name, is_ignored, ignore_reason)
            print(f"  upsert ({code!r}, unit={unit!r}) -> pid {pid} (name={bsn_name!r})")
        deleted = conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=? AND bsn_unit=''", (code,)).rowcount
        print(f"  DELETE blank catch-all for {code!r}: {deleted} row(s) removed")

    after_mapping = {code: q(conn, "SELECT bsn_unit, product_id FROM product_code_mapping WHERE bsn_code=? ORDER BY bsn_unit", (code,))
                      for code in CODES}
    for code, rows in after_mapping.items():
        print(f"  {code!r} mapping rows after split: {[(r['bsn_unit'], r['product_id']) for r in rows]}")

    # ── STEP 7: remove the แผง->3 unit_conversions merge artifact ───────────
    print("\n=== 7. Remove bogus แผง->3 unit_conversions artifact on 111/1886/1888 ===")
    for pid in UC_ARTIFACT_PIDS:
        cur = conn.execute("DELETE FROM unit_conversions WHERE product_id=? AND bsn_unit='แผง'", (pid,))
        print(f"  pid {pid}: DELETE FROM unit_conversions WHERE bsn_unit='แผง' -> {cur.rowcount} row(s) deleted")
        # 1 on a first run (removing the merge artifact); 0 on a re-run
        # (already removed) — both are fine, idempotent. Anything else is not.
        assert cur.rowcount in (0, 1), f"Expected 0 or 1 artifact row on pid {pid}, got {cur.rowcount}"

    remaining_uc = {pid: q(conn, "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=? ORDER BY bsn_unit", (pid,))
                     for pid in ALL_PIDS}
    for pid, rows in remaining_uc.items():
        print(f"  pid {pid} unit_conversions remaining: {[(r['bsn_unit'], r['ratio']) for r in rows]}")

    # ── STEP 8: re-point sales_transactions / purchase_transactions ────────
    print("\n=== 8. Re-point sales_transactions / purchase_transactions by (code, normalized stored unit) ===")
    matched_sales = {code: 0 for code in CODES}
    matched_purch = {code: 0 for code in CODES}
    distinct_units_seen = {}  # code -> {table: set(raw units seen)}

    for code in CODES:
        targets_for_code = {u: pid for c, u, pid in MAPPING_TARGETS if c == code}
        distinct_units_seen[code] = {'sales_transactions': set(), 'purchase_transactions': set()}

        for table in ('sales_transactions', 'purchase_transactions'):
            rows = q(conn, f"SELECT id, unit, product_id FROM {table} WHERE bsn_code=?", (code,))
            for row in rows:
                raw_unit = row['unit']
                distinct_units_seen[code][table].add(raw_unit)
                norm_unit = models.bsn_units.normalize_unit(raw_unit) or ''
                target_pid = targets_for_code.get(norm_unit)
                if target_pid is None:
                    raise AssertionError(
                        f"UNMATCHED unit: {table} id={row['id']} code={code!r} raw_unit={raw_unit!r} "
                        f"normalized={norm_unit!r} has no mapping target — reality contradicts the "
                        f"plan's known-units table. Stopping (do not guess a route)."
                    )
                conn.execute(f"UPDATE {table} SET product_id=?, synced_to_stock=0 WHERE id=?",
                             (target_pid, row['id']))
                if table == 'sales_transactions':
                    matched_sales[code] += 1
                else:
                    matched_purch[code] += 1

    for code in CODES:
        print(f"  {code!r}: sales matched/repointed = {matched_sales[code]} (of {pre_sales_count[code]}), "
              f"purchase matched/repointed = {matched_purch[code]} (of {pre_purch_count[code]}); "
              f"stored units seen: sales={sorted(distinct_units_seen[code]['sales_transactions'])}, "
              f"purchase={sorted(distinct_units_seen[code]['purchase_transactions'])}")
        check(f"{code!r}: all sales rows matched a target unit",
              matched_sales[code] == pre_sales_count[code],
              f"{matched_sales[code]} == {pre_sales_count[code]}")
        check(f"{code!r}: all purchase rows matched a target unit",
              matched_purch[code] == pre_purch_count[code],
              f"{matched_purch[code]} == {pre_purch_count[code]}")

    # ── STEP 9: delete stale ledger for all 6 SKUs ──────────────────────────
    print("\n=== 9. DELETE old ledger for the 6 SKUs (BSN%/รวม%/reconcile ledger) ===")
    preview = q(conn,
        f"SELECT product_id, note, COUNT(*) c, SUM(quantity_change) s FROM transactions "
        f"WHERE product_id IN ({','.join('?'*len(ALL_PIDS))}) "
        f"AND (note LIKE 'BSN%' OR note LIKE 'รวม%' OR note LIKE '%reconcile ledger%') "
        f"GROUP BY product_id, note ORDER BY product_id", ALL_PIDS)
    print("  Preview of rows to be deleted:")
    for r in preview:
        print(f"    pid {r['product_id']:>5} | {r['note']:<32} | rows={r['c']:>4} | sum={r['s']}")
    total_to_delete = sum(r['c'] for r in preview)
    cur = conn.execute(BSN_LEDGER_DELETE_SQL, ALL_PIDS)
    print(f"  -> {cur.rowcount} row(s) deleted (preview said {total_to_delete})")
    assert cur.rowcount == total_to_delete, "Delete count didn't match preview count!"
    assert cur.rowcount == len(to_delete_preview), (
        f"Delete count ({cur.rowcount}) doesn't match the STEP 4 orphan-safety preview count "
        f"({len(to_delete_preview)}) — the scope drifted between the pre-check and the real delete."
    )

    kept = q(conn, f"SELECT product_id, note, quantity_change FROM transactions "
                    f"WHERE product_id IN ({','.join('?'*len(ALL_PIDS))}) ORDER BY product_id, note", ALL_PIDS)
    print("  Rows KEPT after delete (should be only opening/manual/count/pack-unpack rows):")
    for r in kept:
        print(f"    pid {r['product_id']:>5} | {r['note']:<60} | qty_change={r['quantity_change']}")

    # ── STEP 10: re-sync via the REAL app function ──────────────────────────
    print("\n=== 10. Re-sync via models._sync_bsn_to_stock (sales, then purchase) ===")
    unsynced_sales_before = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    unsynced_purch_before = qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    print(f"  (whole-table unsynced rows before: sales={unsynced_sales_before}, purchase={unsynced_purch_before} "
          f"— re-sync processes ALL pending rows, not just our 5 codes)")

    models._sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    models._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')

    unsynced_sales_after = qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    unsynced_purch_after = qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE product_id IS NOT NULL AND synced_to_stock=0")['c']
    print(f"  whole-table unsynced rows after: sales={unsynced_sales_after}, purchase={unsynced_purch_after}")

    rebuilt = q(conn, f"SELECT product_id, COUNT(*) c, SUM(quantity_change) s FROM transactions "
                        f"WHERE product_id IN ({','.join('?'*len(ALL_PIDS))}) AND note LIKE 'BSN%' "
                        f"GROUP BY product_id", ALL_PIDS)
    print("  Rebuilt BSN rows per SKU:")
    for r in rebuilt:
        print(f"    pid {r['product_id']:>5} | rows={r['c']:>4} | sum={r['s']}")

    # ── STEP 11: pin stock to snapshot with a balancing ADJUST ──────────────
    print("\n=== 11. PIN stock_levels back to snapshot (balancing ADJUST per SKU) ===")
    current = snapshot_stock(conn, ALL_PIDS)
    for pid in ALL_PIDS:
        target = snapshot[pid]
        cur_qty = current[pid]
        delta = target - cur_qty
        print(f"  pid {pid} ({PID_TO_HINGE[pid]}): target={target}, current_after_resync={cur_qty}, delta(ADJUST)={delta}")
        if abs(delta) > 1e-9:
            conn.execute("""
                INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
                VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
            """, (pid, delta, PIN_NOTE, date.today().isoformat() + ' 00:00:00'))
        else:
            print(f"    -> delta ~0, no ADJUST needed")

    final = snapshot_stock(conn, ALL_PIDS)
    print(f"  FINAL stock_levels: {final}")

    # ── STEP 12: restore platform/online-stock counters to snapshot ────────
    # Step 10's re-sync replays _sync_bsn_to_stock's shopee/lazada_stock +
    # platform_skus.stock deduction for every historical หน้าร้าน* OUT row it
    # reprocesses (see step-3 risk note — code 030บ3412 alone carries ~117
    # such rows). On THIS snapshot those counters are all 0, so the
    # MAX(0,...) floor happens to make it a no-op today — but that's a
    # coincidence of the current data, not a guarantee: these online
    # counters self-heal to non-zero on the next Shopee/Lazada listing
    # upload, and a re-sync against a non-zero baseline would spuriously
    # deduct already-historical units. Restore them to the step-1/3
    # snapshot explicitly (mirrors the main stock_levels pin) so this
    # operation is stock-preserving for the ONLINE counters too, regardless
    # of their value at apply time. Scoped to exactly the rows read in step
    # 3 (our 6 SKUs' products row + their platform_skus rows) — does not
    # touch or interfere with any other product's platform stock.
    print("\n=== 12. RESTORE platform/online-stock counters to snapshot (mirrors the main stock pin) ===")
    for pid in ALL_PIDS:
        snap = before_platform[pid]
        conn.execute("UPDATE products SET shopee_stock=?, lazada_stock=? WHERE id=?",
                     (snap['shopee_stock'], snap['lazada_stock'], pid))
        print(f"  pid {pid}: restore shopee_stock={snap['shopee_stock']}, lazada_stock={snap['lazada_stock']}")
    for row in before_platform_skus:
        conn.execute("UPDATE platform_skus SET stock=? WHERE id=?", (row['stock'], row['id']))
        print(f"  platform_skus id={row['id']} ({row['platform']}, pid={row['internal_product_id']}): restore stock={row['stock']}")

    # ── VERIFY (§4.6 invariants + phase-specific extras) ────────────────────
    print("\n=== VERIFY ===")

    check("pid 81 is_active=1", qone(conn, "SELECT is_active FROM products WHERE id=81")['is_active'] == 1,
          f"is_active = {qone(conn, 'SELECT is_active FROM products WHERE id=81')['is_active']}")
    check("pid 90 is_active=1", qone(conn, "SELECT is_active FROM products WHERE id=90")['is_active'] == 1,
          f"is_active = {qone(conn, 'SELECT is_active FROM products WHERE id=90')['is_active']}")
    for pid in (96, 111, 1886, 1888):
        v = qone(conn, "SELECT is_active FROM products WHERE id=?", (pid,))['is_active']
        check(f"pid {pid} is_active=1 (was already active)", v == 1, f"is_active = {v}")

    pid80_after = qone(conn, "SELECT is_active FROM products WHERE id=?", (PID_EXCLUDED,))
    pid80_stock_after = snapshot_stock(conn, [PID_EXCLUDED])[PID_EXCLUDED]
    pid80_ledger_after = q(conn, "SELECT note, COUNT(*) c, SUM(quantity_change) s FROM transactions "
                                   "WHERE product_id=? GROUP BY note ORDER BY note", (PID_EXCLUDED,))
    check("pid 80 (EXCLUDED) is_active unchanged",
          pid80_after['is_active'] == pid80_before['is_active'],
          f"before={pid80_before['is_active']}, after={pid80_after['is_active']}")
    check("pid 80 (EXCLUDED) stock unchanged",
          abs(pid80_stock_after - pid80_stock_before) < 1e-9,
          f"before={pid80_stock_before}, after={pid80_stock_after}")
    check("pid 80 (EXCLUDED) ledger byte-identical",
          [dict(r) for r in pid80_ledger_before] == [dict(r) for r in pid80_ledger_after],
          f"before={[dict(r) for r in pid80_ledger_before]}, after={[dict(r) for r in pid80_ledger_after]}")
    check("pid 80 (EXCLUDED) no mapping row created",
          qone(conn, "SELECT COUNT(*) c FROM product_code_mapping WHERE product_id=?", (PID_EXCLUDED,))['c'] == 0,
          "count == 0")

    for pid in ALL_PIDS:
        check(f"stock_levels pid {pid} == snapshot", abs(final[pid] - snapshot[pid]) < 1e-9,
              f"final={final[pid]}, snapshot={snapshot[pid]}")

    dupes = q(conn, f"SELECT reference_no, product_id, COUNT(*) c FROM transactions "
                      f"WHERE product_id IN ({','.join('?'*len(ALL_PIDS))}) AND note LIKE 'BSN%' AND reference_no IS NOT NULL "
                      f"GROUP BY reference_no, product_id HAVING c > 1", ALL_PIDS)
    check("no duplicate (reference_no, product_id) among BSN rows on the 6 SKUs", len(dupes) == 0,
          f"dupes = {[dict(d) for d in dupes]}")

    # Ratio check for SALES-sourced rows: each rebuilt BSN ขาย row's
    # |quantity_change| must equal the sale's own qty (ratio 1 both on the
    # แผง-base panel SKU and on the ตัว-base loose SKU post-cleanup). Purchase
    # rows are checked separately below (raw 'ตว' unit vs base_qty via
    # unit_conversions, which is still ratio 1 for our 3 loose SKUs).
    sales_ratio_mismatches = q(conn, f"""
        SELECT t.reference_no, t.product_id, t.quantity_change, s.qty, s.unit
        FROM transactions t
        JOIN sales_transactions s ON s.doc_no = t.reference_no AND s.product_id = t.product_id
        WHERE t.product_id IN ({','.join('?'*len(ALL_PIDS))}) AND t.note IN ('BSN ขาย', 'BSN ขาย-คืน')
          AND ABS(ABS(t.quantity_change) - s.qty) > 1e-6
    """, ALL_PIDS)
    check("sales-sourced BSN rows: |quantity_change| == qty (ratio 1, no stray ×3)",
          len(sales_ratio_mismatches) == 0, f"mismatches = {[dict(m) for m in sales_ratio_mismatches]}")

    purch_ratio_mismatches = q(conn, f"""
        SELECT t.reference_no, t.product_id, t.quantity_change, p.qty, p.unit
        FROM transactions t
        JOIN purchase_transactions p ON p.doc_no = t.reference_no AND p.product_id = t.product_id
        WHERE t.product_id IN ({','.join('?'*len(ALL_PIDS))}) AND t.note IN ('BSN ซื้อ', 'BSN ซื้อ-คืน')
          AND ABS(ABS(t.quantity_change) - p.qty) > 1e-6
    """, ALL_PIDS)
    check("purchase-sourced BSN rows: |quantity_change| == qty (ratio 1 via unit_conversions ตว/ตัว==1)",
          len(purch_ratio_mismatches) == 0, f"mismatches = {[dict(m) for m in purch_ratio_mismatches]}")

    # แผง SKUs' rebuilt rows must be in แผง; ตัว SKUs' in ตัว.
    for h in HINGES:
        panel_wrong_unit = q(conn, """
            SELECT t.reference_no, s.unit FROM transactions t
            JOIN sales_transactions s ON s.doc_no = t.reference_no AND s.product_id = t.product_id
            WHERE t.product_id = ? AND t.note LIKE 'BSN%' AND s.unit != 'แผง'
        """, (h['pid_panel'],))
        check(f"{h['name']}: panel pid {h['pid_panel']} BSN sales rows are all unit=แผง",
              len(panel_wrong_unit) == 0, f"offenders = {[dict(r) for r in panel_wrong_unit]}")

    # Pre-move sales/purchase row COUNTS must be conserved (no rows dropped
    # or duplicated by the re-point).
    post_sales_count = {code: qone(conn, "SELECT COUNT(*) c FROM sales_transactions WHERE bsn_code=?", (code,))['c']
                         for code in CODES}
    post_purch_count = {code: qone(conn, "SELECT COUNT(*) c FROM purchase_transactions WHERE bsn_code=?", (code,))['c']
                         for code in CODES}
    check("re-pointed sales row counts match pre-move totals (per code)",
          post_sales_count == pre_sales_count, f"before={pre_sales_count} after={post_sales_count}")
    check("re-pointed purchase row counts match pre-move totals (per code)",
          post_purch_count == pre_purch_count, f"before={pre_purch_count} after={post_purch_count}")

    # Each split code now routes ONLY to its expected product_id set.
    for code in CODES:
        expected_pids = {pid for c, u, pid in MAPPING_TARGETS if c == code}
        actual_sales_pids = {r['product_id'] for r in q(conn, "SELECT DISTINCT product_id FROM sales_transactions WHERE bsn_code=?", (code,))}
        actual_purch_pids = {r['product_id'] for r in q(conn, "SELECT DISTINCT product_id FROM purchase_transactions WHERE bsn_code=?", (code,))}
        actual_pids = actual_sales_pids | actual_purch_pids
        check(f"{code!r} routes only to its expected product_id(s)",
              actual_pids <= expected_pids,
              f"expected subset of {expected_pids}, sales_pids={actual_sales_pids}, purch_pids={actual_purch_pids}")

    # Dry-run resolve each (code, unit) via the REAL resolver.
    print("\n  Dry-run models._resolve_mapping(conn, code, unit) checks:")
    resolve_checks = [
        ('030บ3412', 'แผง', 81), ('030บ3412', 'ตว', 111),   # ตว normalizes -> ตัว -> 111
        ('030บ5111', 'แผง', 90), ('030บ5111', 'ตว', 1886),
        ('030บ5022', 'ตว', 1886),
        ('030บ2044', 'แผง', 90),
        ('030บ4000', 'แผง', 96), ('030บ4000', 'ตว', 1888),
    ]
    for code, unit, expected_pid in resolve_checks:
        pid_resolved, is_ignored, mapped = models._resolve_mapping(conn, code, unit)
        check(f"_resolve_mapping({code!r}, {unit!r}) -> pid {expected_pid}",
              mapped and pid_resolved == expected_pid,
              f"mapped={mapped}, resolved_pid={pid_resolved}, is_ignored={is_ignored}")

    # get_buildable still returns for each pair.
    buildable = models.get_buildable(ALL_PIDS, conn=conn)
    for h in HINGES:
        loose_has_sources = h['pid_loose'] in buildable and len(buildable[h['pid_loose']]['sources']) > 0
        check(f"{h['name']}: get_buildable still returns pack->unpack source for loose pid {h['pid_loose']}",
              loose_has_sources,
              f"buildable[{h['pid_loose']}] = {buildable.get(h['pid_loose'])}")

    # Golden Lion #412's formula 90 (80 -> 3x111) must still exist/valid, untouched.
    formula90 = qone(conn, "SELECT is_active, output_product_id, output_qty FROM conversion_formulas WHERE id=90")
    check("formula 90 (Golden Lion 80 unpack -> 111) still active & valid",
          formula90 is not None and formula90['is_active'] == 1 and formula90['output_product_id'] == 111,
          f"formula90 = {dict(formula90) if formula90 else None}")

    # Negative stock — flag, don't fail (expected & OK per plan §4.6 note:
    # แผง SKUs have no purchase source, only ขึ้นแผง pack events).
    negatives = [(pid, final[pid]) for pid in ALL_PIDS if final[pid] < 0]
    if negatives:
        print(f"  [FLAG] negative stock after pin: {negatives} — expected/OK per plan for แผง SKUs "
              f"with no purchase source, Put accepts, recount later.")
    else:
        print(f"  [OK] no negative stock among {ALL_PIDS} after pin.")

    # Platform-stock pin check (step 12): the online counters must be
    # RESTORED to the step-1/3 snapshot exactly, the same invariant as the
    # main stock_levels pin — this holds regardless of whether the snapshot
    # baseline was 0 or non-zero (unlike a plain "unchanged" check, which
    # would only ever have been true by coincidence on a zero baseline).
    after_platform = {pid: dict(qone(conn, "SELECT shopee_stock, lazada_stock FROM products WHERE id=?", (pid,)))
                       for pid in ALL_PIDS}
    after_platform_skus = q(conn, f"SELECT id, platform, internal_product_id, stock FROM platform_skus "
                                    f"WHERE internal_product_id IN ({','.join('?'*len(ALL_PIDS))})", ALL_PIDS)
    platform_ok = all(v['shopee_stock'] >= 0 and v['lazada_stock'] >= 0 for v in after_platform.values())
    platform_pinned = after_platform == before_platform
    check("products.shopee_stock/lazada_stock non-negative after restore", platform_ok,
          f"after = {after_platform}")
    check("products.shopee_stock/lazada_stock RESTORED to step-1/3 snapshot exactly",
          platform_pinned, f"snapshot={before_platform}, after={after_platform}")
    platform_skus_pinned = [dict(r) for r in before_platform_skus] == [dict(r) for r in after_platform_skus]
    check("platform_skus.stock RESTORED to step-1/3 snapshot exactly",
          platform_skus_pinned,
          f"snapshot={[dict(r) for r in before_platform_skus]}, after={[dict(r) for r in after_platform_skus]}")

    failed = [r for r in results if r[1] == 'FAIL']
    print(f"\n=== SUMMARY: {len(results)-len(failed)}/{len(results)} checks PASSED ===")
    if failed:
        for name, status, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        raise AssertionError(f"{len(failed)} verification check(s) failed — see above. Not committing further.")


if __name__ == '__main__':
    main()
