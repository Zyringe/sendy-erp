-- 185 — ค่าใช้จ่ายของงวดก่อน: which period a cashbook cost belongs to.
--
-- Spec #593 user stories 8-12, ADR 0014 decision 3, CONTEXT.md "Internal
-- P&L" -> ค่าใช้จ่ายของงวดก่อน. The internal P&L is accrual: a cost belongs
-- to the period it was INCURRED. The cashbook has no field saying so today,
-- so every cost is read on the month it was PAID. That is what makes March
-- read as a disaster every year — ฿400,000 of "โบนัสปี 68" paid 2026-03-09
-- is FY2568 compensation, not March trading. Dropping it out of 2026 is not
-- available either: Sendy holds no FY2568 statement to receive it, so
-- ฿567,500 of real cost would simply vanish from every report the business
-- has. Hence its own line, and hence this column.
--
-- Column semantics: NULL = "the cost belongs to the month it was paid" —
-- every row in the table today, and the default for every row keyed after
-- this. A set value is the Gregorian period it belongs to, 'YYYY' or
-- 'YYYY-MM'. Gregorian, like every other date in this DB, even though the
-- costs themselves are named for พ.ศ. years.
--
-- The CHECK is not tidiness, it is user story 11 ("a cost reaches that line
-- only when the period it belongs to has been identified") encoded in the
-- schema instead of in prose. It refuses anything that is not a period, and
-- it refuses a period that is not STRICTLY EARLIER than the payment month —
-- comparing only as many characters of txn_date as the value is long, so
-- 'YYYY' is judged against the year and 'YYYY-MM' against the month.
-- "ไม่แน่ใจว่าเดือนไหน" is therefore un-storable, which is exactly ADR
-- 0014's "the prior-period line is not a dumping ground".
--
-- Verified empirically before writing (sqlite 3.51.0 local, 3.46.1 prod):
-- ADD COLUMN accepts this cross-column CHECK, DROP COLUMN restores the
-- table SQL byte-identically, and the CHECK accepts
-- 2025 / 2024 / 2025-12 / 2025-01 / 2026-02 against a 2026-03-09 row while
-- refusing 2026 / 2026-03 / 2026-04 / 25 / 2025-13 / 2025-00 / 2025-1 /
-- ไม่แน่ใจ / ''. The same matrix runs as a test
-- (tests/test_accounting_prior_period_expense.py).
--
-- ⚠ NO abort-precondition, on purpose (contrast 177_promo_one_per_slot):
-- the column is added ALL-NULL, so there is no existing row the CHECK could
-- grandfather in, and the stamped set is an explicitly NAMED row list which
-- cannot over-match. There is nothing for a precondition to catch.
--
-- ⚠ The audit trigger `audit_cashbook_transactions_update` deliberately does
-- NOT cover this column. It enumerates its columns, and nothing but this
-- migration writes belongs_to_period — there is no cashbook UI for it
-- (spec #593: "a YAGNI call, not an oversight"). WHOEVER BUILDS THAT UI MUST
-- EXTEND THAT TRIGGER in the same change, or the first hand-edit of a
-- period will be untraceable.
--
-- ⚠ The six ฿167,500 rows below sit on account `904`, which is
-- `is_transfer = 1` today. The statement's expense population excludes
-- transfer accounts, so those rows are STAMPED here but stay invisible on
-- the page until ADR 0017 un-flags 904 — that is why #594 is blocked by this
-- migration rather than the other way round. The population is deliberately
-- NOT widened to make them show.
--
-- Matched on (txn_date, amount, description), never on id: the ids happen to
-- agree between prod and the local dev DB today (575-580, 416, 417) but
-- nothing guarantees that. The 2026-03-09 tuple matches TWO rows on purpose
-- — the ฿400,000 is keyed as two identical ฿200,000 rows.
--
-- ⚠ NOT re-runnable on its own via a second `sqlite3 < file` apply: ALTER
-- TABLE ... ADD COLUMN has no IF NOT EXISTS form, so a second apply dies at
-- `duplicate column name: belongs_to_period` — same documented shape as
-- 159/176/178/183. The runner never repeats an applied migration (keyed by
-- filename in applied_migrations); this only matters for a hand rehearsal,
-- where 185_prior_period_expense.rollback.sql must run first.
--
-- Rehearsed forward + rollback + forward on a `sqlite3 .backup` copy of
-- ~/sendy-prod-backups/prod-2026-09-19T0820Z-post-uc4.db before commit
-- (never `cp` — sendy holds a WAL); sqlite_master diffed byte-for-byte.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE cashbook_transactions ADD COLUMN belongs_to_period TEXT
    CHECK (belongs_to_period IS NULL
           OR ((belongs_to_period GLOB '[0-9][0-9][0-9][0-9]'
                OR belongs_to_period GLOB '[0-9][0-9][0-9][0-9]-0[1-9]'
                OR belongs_to_period GLOB '[0-9][0-9][0-9][0-9]-1[0-2]')
               AND belongs_to_period < substr(txn_date, 1, length(belongs_to_period))));

-- The two costs Put confirmed as FY2568 compensation (2026-09-19).
-- พ.ศ. 2568 = Gregorian 2025. 8 rows, ฿567,500.00.
UPDATE cashbook_transactions
   SET belongs_to_period = '2025'
 WHERE direction = 'expense'
   AND category  = 'จ่ายค่าโบนัส'
   AND (txn_date, amount, description) IN (
                   SELECT '2026-01-31',  11000.0, 'โบนัสพี่ลี'
        UNION ALL  SELECT '2026-01-31',  12000.0, 'โบนัสพี่ต๋อง'
        UNION ALL  SELECT '2026-01-31',  18000.0, 'โบนัสพี่หนุ่ม'
        UNION ALL  SELECT '2026-01-31',   7500.0, 'โบนัสพี่ติม'
        UNION ALL  SELECT '2026-01-31',  19000.0, 'โบนัสพี่แต'
        UNION ALL  SELECT '2026-01-31', 100000.0, 'โบนัสพี่ต๋อ'
        UNION ALL  SELECT '2026-03-09', 200000.0, 'โบนัสปี 68'
   );

-- Self-record INSIDE this transaction — same reasoning as 183/178/176/159:
-- this file is not re-runnable, so if the runner crashed between
-- executescript() committing and its own separate applied_migrations INSERT,
-- the next boot would retry it and die on the duplicate column.
-- INSERT OR IGNORE makes this safe whether or not the runner's own
-- bookkeeping INSERT also fires.
INSERT OR IGNORE INTO applied_migrations (filename, applied_by)
VALUES ('185_prior_period_expense.sql', 'auto');

COMMIT;
