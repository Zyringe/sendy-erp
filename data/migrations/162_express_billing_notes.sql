-- 162_express_billing_notes.sql
-- ใบวางบิล from Express ARBIL, carried by the daily zip since 2026-08-17.
--
-- Why: /ar cannot otherwise tell an invoice nobody has billed yet from one that
-- has been formally billed and is waiting in the customer's payment run. On the
-- 2026-08-17 export, 17 of the 170 invoices it would chase (฿107,845) were
-- already on a ใบวางบิล. express_ar_outstanding.bill_no (mig 161) points here.
--
-- Not windowed, unlike the ledger: the bills open invoices point at are dated
-- 2014-02-01 .. 2026-07-25, so a 60-day window would miss 11 of the 19. The
-- whole table is ~11,925 rows.
--
-- entity mirrors express_ar_outstanding — each book keeps its own (Put,
-- 2026-08-17), so the key is (entity, bill_no) rather than bill_no alone.
--
-- Apply: restart the app (database.py::init_db() auto-applies).
-- Rollback: data/migrations/162_express_billing_notes.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

CREATE TABLE IF NOT EXISTS express_billing_notes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity            TEXT    NOT NULL,
    bill_no           TEXT    NOT NULL,
    bill_date_iso     TEXT,
    sent_date_iso     TEXT,                          -- BILOUT: went out to the customer
    approved_date_iso TEXT,                          -- APPDAT: customer acknowledged
    customer_code     TEXT,
    customer_name     TEXT,
    pay_cond          TEXT,                          -- free Thai text, e.g. 'เครดิต 30 วัน'
    net_amount        REAL    NOT NULL DEFAULT 0,
    is_cancelled      INTEGER NOT NULL DEFAULT 0,    -- DOCSTAT 'C'
    remark            TEXT,
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (entity, bill_no)
);

-- The join /ar makes: open invoice -> its billing note.
CREATE INDEX IF NOT EXISTS idx_billing_notes_customer
    ON express_billing_notes(entity, customer_code);

COMMIT;
