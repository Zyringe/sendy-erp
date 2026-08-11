-- ============================================================================
-- 156 — move pids 1211 (ค่าขนส่ง) / 1623 (ส่วนลดพิเศษ) into the steady state
--       that migration 155 + models/stock_filters.py assume.
--
-- WHY THIS EXISTS
--   155 makes the two codes billable-but-non-stock, and _sync_bsn_to_stock
--   now refuses to post a ledger leg for them. That describes the steady
--   state correctly — but the rows ALREADY in the book are not in it: every
--   source row on these two pids is `synced_to_stock = 1` and carries a real
--   ledger leg written by the old behaviour.
--
--   The first import that puts either pid into `affected_pids` would do the
--   transition by accident: imports.py:334 deletes `note IN (bsn_notes)`
--   (the mig-080 triggers decrement stock_levels automatically), :340 resets
--   the source rows, and the re-sync then correctly declines to re-post. The
--   deliberate compensating ADJUSTs on these pids — including Put's own
--   'ล้างสต็อกค่าขนส่ง (ไม่ใช่สินค้าจริง) — Put 2026-08-05' — stay behind, so
--   stock_levels silently goes negative by exactly the legs that vanished.
--
--   This migration performs that same transition ON PURPOSE, atomically,
--   with the correction included, so the DB matches the design's assumed
--   steady state from the instant of deploy.
--
-- SELF-CORRECTING BY CONSTRUCTION — no hardcoded ±13 / ±2
--   The correcting ADJUST's quantity is SUM(quantity_change) over the rows
--   this migration actually deleted, derived in SQL. Prod and any dev
--   snapshot therefore each get the correction THEY need, whatever their
--   own ADJUST history happens to be.
--
-- ⛔ stock_levels IS NEVER UPDATED BY HAND HERE
--   The mig-080 triggers already maintain it: `after_transaction_delete`
--   subtracts each deleted quantity_change, `after_transaction_insert` adds
--   the ADJUST back. A manual UPDATE would double-count. Net effect on
--   stock_levels across this whole file is exactly ZERO — that is the
--   invariant the rehearsal asserts before AND after.
--
-- NOTE TEXT OF THE ADJUST
--   Must NOT begin with 'BSN' or 'ประวัติขาย': bsn_sync.py:529 wipes those
--   prefixes on every replay (update_unit_conversion_ratio / repoint), which
--   would delete the correction and leave the negative stock behind again.
--   reference_no stays NULL — matching the existing hand ADJUSTs on these
--   pids, and keeping the row invisible to reconcile._ledger_check, which
--   only ever loads `WHERE reference_no IN (doc_nos)`.
--
-- product_cost_ledger
--   The single PURCHASE row on pid 1211 is the derived cache of the
--   'BSN ซื้อ' leg being deleted. product_cost_ledger is rebuilt wholesale by
--   models/wacc.py::_recalculate_product_wacc (it DELETEs the product's rows
--   and replays `transactions`), so it is a cache, not a source. With
--   products.opening_cost = 0 and cost_price = 0 on both pids, a replay after
--   this migration emits ZERO entries and leaves cost_price untouched —
--   i.e. deleting the rows here IS the recalc's output, verified by running
--   the real recalculate_product_wacc on the rehearsal copy and diffing.
--   No WACC recalculation is required post-deploy.
--
-- Re-runnable: the snapshot tables use CREATE TABLE IF NOT EXISTS +
-- INSERT OR IGNORE (never drop-first — dropping a snapshot destroys the
-- rollback data), and the ADJUST insert is guarded by a NOT EXISTS on its own
-- note. A second run is a no-op in every statement. Same shape as mig 081.
-- Snapshot tables are named `migration_156_*` so dump_schema.py's
-- `tbl_name NOT LIKE 'migration\_%'` filter excludes them: no schema.sql regen.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

-- ── 1. Snapshot the ledger legs about to be deleted (for rollback) ─────────
CREATE TABLE IF NOT EXISTS migration_156_deleted_ledger (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL,
    txn_type        TEXT    NOT NULL,
    quantity_change INTEGER NOT NULL,
    unit_mode       TEXT    NOT NULL,
    reference_no    TEXT,
    note            TEXT,
    created_at      TEXT    NOT NULL,
    source_bsn_code TEXT,
    source_line_seq INTEGER
);

INSERT OR IGNORE INTO migration_156_deleted_ledger
    (id, product_id, txn_type, quantity_change, unit_mode, reference_no,
     note, created_at, source_bsn_code, source_line_seq)
SELECT id, product_id, txn_type, quantity_change, unit_mode, reference_no,
       note, created_at, source_bsn_code, source_line_seq
  FROM transactions
 WHERE product_id IN (1211, 1623)
   AND note IN ('BSN ขาย', 'BSN ขาย-คืน', 'BSN ซื้อ', 'BSN ซื้อ-คืน');

-- ── 2. Snapshot which source rows were synced (for rollback) ───────────────
CREATE TABLE IF NOT EXISTS migration_156_synced_sources (
    src_table TEXT    NOT NULL,
    row_id    INTEGER NOT NULL,
    PRIMARY KEY (src_table, row_id)
);

INSERT OR IGNORE INTO migration_156_synced_sources (src_table, row_id)
SELECT 'sales_transactions', id FROM sales_transactions
 WHERE product_id IN (1211, 1623) AND synced_to_stock = 1
UNION ALL
SELECT 'purchase_transactions', id FROM purchase_transactions
 WHERE product_id IN (1211, 1623) AND synced_to_stock = 1;

-- ── 3. Snapshot the derived cost-ledger rows (for rollback) ────────────────
CREATE TABLE IF NOT EXISTS migration_156_deleted_cost_ledger (
    id           INTEGER PRIMARY KEY,
    product_id   INTEGER NOT NULL,
    event_type   TEXT    NOT NULL,
    event_date   TEXT    NOT NULL,
    qty_change   REAL    NOT NULL,
    unit_cost    REAL    NOT NULL,
    stock_after  REAL    NOT NULL,
    wacc_after   REAL    NOT NULL,
    reference_no TEXT,
    note         TEXT,
    created_at   TEXT    NOT NULL
);

INSERT OR IGNORE INTO migration_156_deleted_cost_ledger
    (id, product_id, event_type, event_date, qty_change, unit_cost,
     stock_after, wacc_after, reference_no, note, created_at)
SELECT id, product_id, event_type, event_date, qty_change, unit_cost,
       stock_after, wacc_after, reference_no, note, created_at
  FROM product_cost_ledger
 WHERE product_id IN (1211, 1623);

-- ── 4. Delete the ledger legs (triggers decrement stock_levels) ────────────
DELETE FROM transactions
 WHERE id IN (SELECT id FROM migration_156_deleted_ledger);

-- ── 5. Put back EXACTLY what step 4 removed, as one named ADJUST per pid ───
-- quantity_change is derived, never hardcoded. HAVING <> 0 keeps a pid whose
-- legs happen to net to zero from getting a pointless 0-qty row. The
-- NOT EXISTS is what makes a second run a no-op instead of a double ADJUST.
INSERT INTO transactions
    (product_id, txn_type, quantity_change, unit_mode, reference_no, note)
SELECT s.product_id, 'ADJUST', SUM(s.quantity_change), 'unit', NULL,
       'คืนยอดสต็อกหลังถอน ledger ของบรรทัดไม่นับสต็อก (migration 156) — ค่าขนส่ง/ส่วนลดพิเศษ ไม่ใช่สินค้าจริง'
  FROM migration_156_deleted_ledger s
 WHERE NOT EXISTS (
           SELECT 1 FROM transactions t
            WHERE t.product_id = s.product_id
              AND t.note = 'คืนยอดสต็อกหลังถอน ledger ของบรรทัดไม่นับสต็อก (migration 156) — ค่าขนส่ง/ส่วนลดพิเศษ ไม่ใช่สินค้าจริง')
 GROUP BY s.product_id
HAVING SUM(s.quantity_change) <> 0;

-- ── 6. Reset the source rows to the steady state the design assumes ────────
UPDATE sales_transactions    SET synced_to_stock = 0 WHERE product_id IN (1211, 1623);
UPDATE purchase_transactions SET synced_to_stock = 0 WHERE product_id IN (1211, 1623);

-- ── 7. Drop the derived cost-ledger rows for these pids ────────────────────
-- Equals what _recalculate_product_wacc would write after step 4 (proved by
-- rehearsal). Leaving them would keep a cache describing a purchase the
-- ledger no longer contains.
DELETE FROM product_cost_ledger WHERE product_id IN (1211, 1623);

COMMIT;
