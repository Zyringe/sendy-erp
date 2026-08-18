-- 163_express_bank_cheques.sql
-- ทะเบียนเช็ค from Express BKTRN, carried by the daily zip since 2026-08-17.
--
-- Why: a customer who paid by post-dated cheque still reads as "owing" in Sendy
-- until the cheque clears, so /ar would have the team chasing someone who has
-- already paid. On the 2026-08-17 export there were 9 such cheques worth ฿69,814.
-- It is also the only forward-looking cash figure in the book: money whose
-- arrival date is already known.
--
-- Replace-per-entity, NOT an upsert, because BKTRN has no unique natural key:
-- CHQNUM repeats on 38 rows and (BKTRNTYP, CHQNUM) repeats on the same 38. The
-- table is a mirror of Express's register at export time — the same shape as the
-- outstanding snapshots — and at 12,805 rows replacing it costs nothing.
--
-- ⚠ Column meanings deliberately NOT interpreted. CHQSTAT's six values
-- (10/05/01/00/20/02) cannot be decoded from the data: status 10 holds both
-- long-cleared cheques and the 9 still in the future, so it does not mean
-- "cleared". The four dates are stored under their DBF names for the same
-- reason — TRNDAT and CHQDAT differ on 5,451 rows and the ordering between
-- TRNDAT/CHQDAT/GETDAT/PAYINDAT is not consistent. One confirmed data point:
-- the GL voucher clearing ทองประสิทธิ์'s cheque is dated 2026-12-18, matching
-- that row's TRNDAT. Label these once someone who knows the book says what they
-- mean; storing them raw loses nothing in the meantime.
--
-- Nine BKTRN columns are empty on every one of the 12,805 rows and are not
-- carried: POSTGL VATDAT VATPRD VATLATE VATTYP AUTHID APPROVE TAXID ORGNUM.
--
-- Apply: restart the app (database.py::init_db() auto-applies).
-- Rollback: data/migrations/163_express_bank_cheques.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

CREATE TABLE IF NOT EXISTS express_bank_cheques (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity           TEXT    NOT NULL,
    kind             TEXT    NOT NULL,               -- 'received' (QR) / 'paid' (QP)
    type_code        TEXT,                           -- BKTRNTYP verbatim
    cheque_no        TEXT,                           -- CHQNUM (NOT unique: 38 repeats)
    trn_date_iso     TEXT,                           -- TRNDAT
    cheque_date_iso  TEXT,                           -- CHQDAT
    received_date_iso TEXT,                          -- GETDAT
    paid_in_date_iso TEXT,                           -- PAYINDAT
    bank_code        TEXT,
    branch           TEXT,
    bank_account     TEXT,
    party_code       TEXT,                           -- CUSCOD
    party_name       TEXT,                           -- NAME
    amount           REAL    NOT NULL DEFAULT 0,
    charge           REAL    NOT NULL DEFAULT 0,
    vat_amount       REAL    NOT NULL DEFAULT 0,
    net_amount       REAL    NOT NULL DEFAULT 0,
    remaining_amount REAL    NOT NULL DEFAULT 0,     -- REMAMT
    status_code      TEXT,                           -- CHQSTAT verbatim, uninterpreted
    remark           TEXT,
    ref_doc          TEXT,
    ref_no           TEXT,
    voucher          TEXT,
    imported_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- "which cheques have not fallen due yet", the question this exists to answer.
CREATE INDEX IF NOT EXISTS idx_bank_cheques_due
    ON express_bank_cheques(entity, kind, trn_date_iso);
CREATE INDEX IF NOT EXISTS idx_bank_cheques_party
    ON express_bank_cheques(entity, party_code);

COMMIT;
