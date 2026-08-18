-- 165_express_general_ledger.sql
-- บัญชีแยกประเภท from Express GLACC (chart) + GLJNL (vouchers) + GLJNLIT (lines),
-- carried by the daily zip since 2026-08-17.
--
-- What it is for: Sendy's /accounting computes profit from sales minus cost,
-- which is an ESTIMATE. The GL is the book the accountant actually closes, so
-- this makes an independent figure available to check it against — the same
-- reason every money change in this repo has to tie to an oracle.
--
-- ⚠ WINDOWED, and this one is a real trade-off rather than a preference.
-- The whole GL is 109,458 vouchers + 359,003 lines and measures 62MB in SQLite.
-- Prod's Railway volume is 434MB with 214MB free (measured 2026-08-18), and
-- adding the full book to BOTH the main DB and vat_book.db would take ~87MB of
-- it before the app's own gzip backups grow to match. A full volume means Sendy
-- cannot write at all, which is a far worse outcome than a shorter GL history.
--
-- So: vouchers dated in the last _GL_SINCE_YEARS calendar years (3 → from
-- 2024-01-01 today) = ~12% of the rows, ~7MB. That covers the current and prior
-- fiscal years, which is what comparing against a closed book needs.
-- UPGRADE PATH: raise _GL_SINCE_YEARS in express_dbf_source.py once the volume
-- has room, or move the GL to its own book DB like vat_book.db.
--
-- TRNTYP → entry_side is derived, and unlike CHQSTAT/DOCSTAT it is PROVEN:
-- account 41-01-00-00 รายได้จากการขาย carries 53,764 lines and every single one
-- is TRNTYP '1'. Income is credited, so 1 = credit and 0 = debit. ลูกหนี้การค้า
-- and เงินสด both agree. The raw code is stored alongside it anyway.
--
-- Apply: restart the app (database.py::init_db() auto-applies).
-- Rollback: data/migrations/165_express_general_ledger.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

CREATE TABLE IF NOT EXISTS express_gl_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity      TEXT NOT NULL,
    account_no  TEXT NOT NULL,                  -- ACCNUM, e.g. 11-01-02-01
    account_name TEXT,                          -- ACCNAM
    level       INTEGER,
    parent_no   TEXT,
    account_type TEXT,                          -- ACCTYP verbatim
    nature      TEXT,                           -- NATURE verbatim
    status      TEXT,
    UNIQUE (entity, account_no)
);

CREATE TABLE IF NOT EXISTS express_gl_vouchers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity       TEXT NOT NULL,
    voucher      TEXT NOT NULL,                 -- VOUCHER (unique across all 109,458)
    voucher_date_iso TEXT,
    journal_type TEXT,                          -- JNLTYP verbatim
    reference_no TEXT,
    description  TEXT,
    source_journal TEXT,
    status       TEXT,
    UNIQUE (entity, voucher)
);

CREATE TABLE IF NOT EXISTS express_gl_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity      TEXT NOT NULL,
    voucher     TEXT NOT NULL,
    line_seq    INTEGER,                        -- SEQIT: NOT unique per voucher (3,367 repeats)
    voucher_date_iso TEXT,
    account_no  TEXT,
    description TEXT,
    entry_side  TEXT,                           -- 'debit' / 'credit', derived (proven, see above)
    type_code   TEXT,                           -- TRNTYP verbatim
    amount      REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gl_lines_account
    ON express_gl_lines(entity, account_no, voucher_date_iso);
CREATE INDEX IF NOT EXISTS idx_gl_lines_voucher
    ON express_gl_lines(entity, voucher);
CREATE INDEX IF NOT EXISTS idx_gl_vouchers_date
    ON express_gl_vouchers(entity, voucher_date_iso);

COMMIT;
