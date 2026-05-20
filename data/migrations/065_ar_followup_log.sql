-- 065_ar_followup_log.sql
-- AR follow-up workspace (Phase 4 — accounting workflow add-on).
-- Records outreach attempts for unpaid invoices: who was contacted, when,
-- via what channel, with what result, and what they promised. Drives the
-- /accounting/ar-followup page.
--
-- customer is a free-text key matching sales_transactions.customer (no FK —
-- BSN sync may add new customer strings before any /customers row exists).
-- customer_code captures the BSN code at log time for resilience to renames.
--
-- Apply:    sqlite3 .../inventory.db < .../migrations/065_ar_followup_log.sql
-- Rollback: 065_ar_followup_log.rollback.sql

BEGIN;

CREATE TABLE ar_followup_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer          TEXT    NOT NULL,
    customer_code     TEXT,
    log_date          TEXT    NOT NULL,
    channel           TEXT    NOT NULL
                              CHECK(channel IN ('phone','line','sms','email','visit','other')),
    contact_person    TEXT,
    result            TEXT    NOT NULL
                              CHECK(result IN (
                                  'promised','partial_paid','paid_full',
                                  'denied','no_answer','wrong_number',
                                  'closed','snooze','other'
                              )),
    promised_amount   REAL,
    promised_date     TEXT,
    next_action_date  TEXT,
    notes             TEXT,
    created_by        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT
);

CREATE INDEX idx_ar_followup_customer       ON ar_followup_log(customer);
CREATE INDEX idx_ar_followup_next_action    ON ar_followup_log(next_action_date)
    WHERE next_action_date IS NOT NULL;
CREATE INDEX idx_ar_followup_log_date       ON ar_followup_log(log_date DESC);

COMMIT;
