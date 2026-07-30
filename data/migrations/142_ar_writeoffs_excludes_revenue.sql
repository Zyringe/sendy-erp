-- ============================================================================
-- Migration 142 — ar_writeoffs.excludes_revenue
--
-- Apply:    restart the server (database.py::init_db() auto-applies on boot)
-- Rollback: 142_ar_writeoffs_excludes_revenue.rollback.sql
--
-- Why
--   `ar_writeoffs` already answers "stop chasing this document". It does NOT
--   answer "this document was never a sale" — and the two are different.
--
--     bad debt   — the sale really happened, the customer just never paid.
--                  Revenue STAYS; the loss shows up as a bad-debt expense.
--     booked in error — no sale ever occurred. Revenue must be reversed.
--
--   Sendy had no way to say the second thing, so three วรสวัสดิ์ giveaway
--   invoices (IV6900401/402/403, 2026-03-11, goods given away free but
--   invoiced by mistake — Put wrote them off 2026-06-24) still counted as
--   ฿154,122.80 of March-2026 revenue: 25% of that month.
--
--   Put's accountant ruled 2026-07-30 that NO credit note will be issued in
--   Express. Express will therefore never self-correct, so Sendy has to carry
--   the correction itself — which is why this lives in the schema rather than
--   waiting for an upstream import to fix it.
--
-- Scope of the flag — deliberately narrow
--   It suppresses REVENUE only. The goods physically left the warehouse, so
--   their cost is a genuine expense and stays in COGS; gross profit for the
--   month correctly drops by the full invoiced amount. Stock ledger, AR
--   exclusion, and document visibility are all untouched.
--
-- Only these three rows are flagged. The other 46 expense write-offs are
-- ordinary bad debt (real sales, unpaid) and keep their revenue — see
-- decisions/log.md 2026-07-30, option A, Put's call.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE ar_writeoffs
    ADD COLUMN excludes_revenue INTEGER NOT NULL DEFAULT 0
        CHECK (excludes_revenue IN (0, 1));

UPDATE ar_writeoffs
   SET excludes_revenue = 1
 WHERE doc_no IN ('IV6900401', 'IV6900402', 'IV6900403');

COMMIT;
