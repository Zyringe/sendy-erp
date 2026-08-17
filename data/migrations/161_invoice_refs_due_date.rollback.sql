-- Rollback for 161_invoice_refs_due_date.sql
--
-- SQLite here cannot DROP COLUMN, so the table is rebuilt. Two things this
-- MUST NOT do, both learned the hard way while writing it:
--
--  1. NEVER `CREATE TABLE ... AS SELECT`. It copies rows and DISCARDS every
--     constraint: PRIMARY KEY AUTOINCREMENT, NOT NULL, DEFAULT, and the two
--     foreign keys all vanish, leaving `id INT` with nothing enforcing it. The
--     first draft of this file did exactly that, was run against the dev DB,
--     and the damage only surfaced because `scripts/dump_schema.py` regenerated
--     schema.sql from the degraded table — one commit away from shipping a
--     constraint-free table to every fresh build. Spell the DDL out.
--
--  2. NEVER forget the indexes. DROP TABLE takes them with it, and they are
--     not part of the CREATE statement, so a rebuild that only restores the
--     table looks complete and is not. All four are recreated below; three
--     predate this migration (idx_express_ar_customer / _doc / _snapshot /
--     _entity_snapshot) and one is 161's own, which is the one being removed.
--
-- Rows are copied from the CURRENT table, not a snapshot, so anything written
-- after the forward migration survives the rollback — see
-- .claude/rules/erp-engineering-discipline.md.

BEGIN;

DROP INDEX IF EXISTS idx_ar_outstanding_due;

CREATE TABLE express_ar_outstanding__rb (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER NOT NULL REFERENCES express_import_log(id) ON DELETE CASCADE,
    snapshot_date_iso   TEXT    NOT NULL,              -- 2026-04-30
    customer_code       TEXT    NOT NULL,
    customer_name       TEXT,
    customer_id         TEXT REFERENCES customers(code),
    customer_type       TEXT,                          -- ลูกค้าประจำ / ตัวแทนจำหน่าย / ฯลฯ
    doc_date_iso        TEXT,
    doc_no              TEXT    NOT NULL,
    is_anomalous        INTEGER NOT NULL DEFAULT 0,    -- ! prefix
    salesperson_code    TEXT,
    bill_amount         REAL    NOT NULL DEFAULT 0,
    paid_amount         REAL    NOT NULL DEFAULT 0,
    outstanding_amount  REAL    NOT NULL DEFAULT 0,
    has_warning         INTEGER NOT NULL DEFAULT 0,    -- *** marker
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    entity              TEXT    NOT NULL DEFAULT 'SD'
);

INSERT INTO express_ar_outstanding__rb
    (id, batch_id, snapshot_date_iso, customer_code, customer_name, customer_id,
     customer_type, doc_date_iso, doc_no, is_anomalous, salesperson_code,
     bill_amount, paid_amount, outstanding_amount, has_warning, created_at, entity)
    SELECT id, batch_id, snapshot_date_iso, customer_code, customer_name, customer_id,
           customer_type, doc_date_iso, doc_no, is_anomalous, salesperson_code,
           bill_amount, paid_amount, outstanding_amount, has_warning, created_at, entity
      FROM express_ar_outstanding;

DROP TABLE express_ar_outstanding;
ALTER TABLE express_ar_outstanding__rb RENAME TO express_ar_outstanding;

CREATE INDEX IF NOT EXISTS idx_express_ar_customer
    ON express_ar_outstanding(customer_id);
CREATE INDEX IF NOT EXISTS idx_express_ar_doc
    ON express_ar_outstanding(doc_no);
CREATE INDEX IF NOT EXISTS idx_express_ar_snapshot
    ON express_ar_outstanding(snapshot_date_iso, customer_code);
CREATE INDEX IF NOT EXISTS idx_express_ar_entity_snapshot
    ON express_ar_outstanding(entity, snapshot_date_iso);

COMMIT;
