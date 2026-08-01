-- ============================================================================
-- Migration 148 — record WHICH purchase line each BSN stock-IN came from.
--
-- Why
--   recalculate_product_wacc pairs the Nth 'BSN ซื้อ' IN of a document with the
--   Nth purchase_transactions row of that document, BY POSITION (wacc.py
--   pt_by_docno + pt_cursor). `unit_cost = net / qty` takes `net` from the
--   purchase row at the cursor and `qty` from the TRANSACTION. When several
--   INs of one document share a created_at their order is decided only by
--   transactions.id — a surrogate key that re-imports and ledger rebuilds
--   reissue. Reorder them and a quantity is multiplied against another line's
--   net (pid 988 / RR6700096: ฿55.57 -> ฿1,278.22 per piece under reversal).
--
--   The fix is to stop guessing: purchase_transactions already has a stable
--   per-line identity, (doc_no, bsn_code, line_seq), formalised by mig 091.
--   This migration stores it on the ledger row so WACC can look the line up
--   directly instead of counting positions.
--
-- What it does
--   1. adds transactions.source_bsn_code / source_line_seq (nullable)
--   2. backfills them for every existing 'BSN ซื้อ' IN by REPLAYING today's
--      positional pairing, so the stored link is exactly the pair WACC
--      resolves right now -> recomputing after this migration is a no-op BY
--      CONSTRUCTION, not by luck
--   3. enforces the source identity with a partial UNIQUE index
--
-- Safety
--   Every guard uses INSERT OR ROLLBACK. A plain CHECK violation uses SQLite's
--   default ABORT, which unwinds the STATEMENT but not the enclosing
--   transaction, and database.py's runner does not call conn.rollback() — it
--   only prints and re-raises, with a comment ASSUMING SQLite already rolled
--   back. OR ROLLBACK makes the unwind explicit so a failed guard can never
--   leave a partial link set behind.
--
--   ⚠ Apply through the migration runner (database.py -> conn.executescript),
--   NOT the `sqlite3` CLI. Verified 2026-08-02: on a forced guard failure
--   executescript raises at the first error and leaves NOTHING behind (no
--   columns, no index, connection not in a transaction). The CLI instead keeps
--   executing after the error, so the trailing CREATE UNIQUE INDEX runs
--   outside the rolled-back transaction and IS left behind — a partial state
--   the runner never produces.
--
--   No table rebuild: two plain ADD COLUMNs, so all six triggers on
--   `transactions` survive untouched. The backfill updates only the two new
--   columns, and both UPDATE triggers' WHEN clauses list only the original
--   columns — so it fires NEITHER audit_transactions_update (no 4,005-row
--   audit spam) NOR after_transaction_update (stock_levels is not touched).
--   Do NOT add the new columns to those WHEN clauses.
--
-- Measured on the 2026-08-02 prod snapshot before writing this:
--   4,005 'BSN ซื้อ' INs, all qty > 0, all with a reference_no
--   4,005 / 4,005 pair cleanly (converted qty agrees within 1e-4)
--   0 duplicate (doc_no, bsn_code, line_seq); 0 purchase rows with NULL bsn_code
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE transactions ADD COLUMN source_bsn_code TEXT;
ALTER TABLE transactions ADD COLUMN source_line_seq INTEGER;

-- ── Materialise the pairing ONCE ────────────────────────────────────────────
-- Validating and writing from two independently-built CTEs risks drift, so the
-- numbered mapping is frozen here and every later statement reads this table.
--
-- Ledger side mirrors wacc.py's walk order exactly:
--   ORDER BY created_at, IN-first, id   (per product+document, across the
--   WHOLE product history — NOT partitioned by created_at, because the cursor
--   is per-document for the entire walk).
-- Only rows that actually advance WACC's cursor are numbered: the purchase
-- branch requires txn_type='IN' AND note='BSN ซื้อ' AND qty > 0.
--
-- base_qty mirrors bsn_sync._get_base_qty:
--   same unit (trimmed compare)      -> qty UNROUNDED
--   else ratio from unit_conversions -> ROUND(qty * ratio, 4)
--   else                             -> NULL  (must fail the guard, never link)
-- The unit_conversions join uses the RAW unit, matching the helper's
-- `WHERE product_id = ? AND bsn_unit = ?`, and is cardinality-safe because the
-- schema enforces UNIQUE(product_id, bsn_unit).
CREATE TEMP TABLE _bf148 AS
WITH led AS (
    SELECT t.id              AS txn_id,
           t.product_id      AS product_id,
           t.reference_no    AS doc_no,
           t.quantity_change AS txn_qty,
           ROW_NUMBER() OVER (
               PARTITION BY t.product_id, t.reference_no
               ORDER BY t.created_at,
                        CASE WHEN t.txn_type = 'IN' THEN 0 ELSE 1 END,
                        t.id
           ) AS rn
      FROM transactions t
     WHERE t.txn_type = 'IN'
       AND t.note = 'BSN ซื้อ'
       AND t.reference_no IS NOT NULL
       AND t.quantity_change > 0
),
src AS (
    SELECT pt.id         AS pt_id,
           pt.product_id AS product_id,
           pt.doc_no     AS doc_no,
           pt.bsn_code   AS bsn_code,
           pt.line_seq   AS line_seq,
           pt.qty        AS qty,
           pt.unit       AS unit,
           ROW_NUMBER() OVER (
               PARTITION BY pt.product_id, pt.doc_no
               ORDER BY pt.id
           ) AS rn
      FROM purchase_transactions pt
)
SELECT led.txn_id,
       led.product_id,
       led.doc_no,
       led.txn_qty,
       src.pt_id,
       src.bsn_code,
       src.line_seq,
       CASE
           WHEN src.unit IS NOT NULL
                AND TRIM(src.unit) = TRIM(COALESCE(p.unit_type, ''))
               THEN COALESCE(src.qty, 0)
           WHEN uc.ratio IS NOT NULL
               THEN ROUND(COALESCE(src.qty, 0) * uc.ratio, 4)
           ELSE NULL
       END AS base_qty
  FROM led
  LEFT JOIN src ON src.product_id = led.product_id
               AND src.doc_no     = led.doc_no
               AND src.rn         = led.rn
  LEFT JOIN products p ON p.id = led.product_id
  LEFT JOIN unit_conversions uc ON uc.product_id = src.product_id
                               AND uc.bsn_unit   = src.unit;

-- ── Guard 1: every IN must pair with a source line whose qty agrees ─────────
-- NULL-safe on purpose: `ABS(a - b) > 0.0001` alone yields UNKNOWN when either
-- side is NULL, which would silently DROP the row from the mismatch count and
-- link it unguarded. Tolerance matches backfill_coverage.py: <= 0.0001 passes.
CREATE TEMP TABLE _guard148_pair (ok INTEGER NOT NULL CHECK (ok = 1));
INSERT OR ROLLBACK INTO _guard148_pair(ok)
SELECT CASE WHEN (
           SELECT COUNT(*) FROM _bf148
            WHERE pt_id    IS NULL
               OR base_qty IS NULL
               OR txn_qty  IS NULL
               OR ABS(base_qty - txn_qty) > 0.0001
       ) = 0 THEN 1 ELSE 0 END;

-- ── Guard 2: the business key must already be unique before we index it ────
CREATE TEMP TABLE _guard148_dup (ok INTEGER NOT NULL CHECK (ok = 1));
INSERT OR ROLLBACK INTO _guard148_dup(ok)
SELECT CASE WHEN (
           SELECT COUNT(*) FROM (
               SELECT 1 FROM purchase_transactions
                WHERE bsn_code IS NOT NULL
                GROUP BY doc_no, bsn_code, line_seq
               HAVING COUNT(*) > 1
           )
       ) = 0 THEN 1 ELSE 0 END;

-- ── Write the links ────────────────────────────────────────────────────────
UPDATE transactions
   SET source_bsn_code = _bf148.bsn_code,
       source_line_seq = _bf148.line_seq
  FROM _bf148
 WHERE _bf148.txn_id = transactions.id;

-- ── Guard 3: no unlinked purchase IN, and no partial provenance anywhere ───
-- Partial provenance (exactly one column set) is an invalid state the read
-- path must never silently treat as "legacy".
CREATE TEMP TABLE _guard148_post (ok INTEGER NOT NULL CHECK (ok = 1));
INSERT OR ROLLBACK INTO _guard148_post(ok)
SELECT CASE WHEN (
           (SELECT COUNT(*) FROM transactions
             WHERE txn_type = 'IN' AND note = 'BSN ซื้อ'
               AND reference_no IS NOT NULL AND quantity_change > 0
               AND (source_bsn_code IS NULL OR source_line_seq IS NULL)) = 0
           AND
           (SELECT COUNT(*) FROM transactions
             WHERE (source_bsn_code IS NULL) <> (source_line_seq IS NULL)) = 0
       ) THEN 1 ELSE 0 END;

-- ── Enforce the source identity from here on ───────────────────────────────
-- Partial (WHERE bsn_code IS NOT NULL) so a historical NULL-coded row could
-- never retro-fail; today there are none.
CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_txn_doc_code_line
    ON purchase_transactions(doc_no, bsn_code, line_seq)
 WHERE bsn_code IS NOT NULL;

DROP TABLE _bf148;
DROP TABLE _guard148_pair;
DROP TABLE _guard148_dup;
DROP TABLE _guard148_post;

COMMIT;
