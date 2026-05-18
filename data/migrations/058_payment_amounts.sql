-- 058_payment_amounts.sql
-- Adds money columns to the BSN payment-import tables so partial-payment /
-- outstanding-per-invoice can be computed.
--
-- Rationale: the BSN payment CSV carries a paid amount per invoice line and
-- (when present) an RE-header receipt total, but the importer currently
-- discards both — paid_invoices / received_payments only store the binary
-- "this RE paid this IV" link. Without the amount we cannot tell a full
-- payment from a partial one, nor compute outstanding per invoice.
--
--   * paid_invoices.amount   = baht applied to that invoice by that receipt
--   * received_payments.total = RE header total when the source provides it
--
-- Both columns are NULLABLE on purpose. Rows imported before this migration
-- have no amount on record; NULL means "amount unknown / legacy binary link"
-- and is deliberately DISTINCT from 0 (a real zero-baht line). Do NOT add
-- NOT NULL or DEFAULT — that would erase the legacy/known distinction.
--
-- Apply:    via database.py::run_pending_migrations (automatic on boot)
-- Rollback: 058_payment_amounts.rollback.sql
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner records
-- every migration it executes; a self-insert would duplicate-key crash on boot.

BEGIN;

ALTER TABLE paid_invoices
    ADD COLUMN amount REAL;

ALTER TABLE received_payments
    ADD COLUMN total REAL;

COMMIT;
