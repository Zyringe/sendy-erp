-- 198 · sales_line_unit_corrections — แก้หน่วยบรรทัด (#692, ADR 0021)
--
-- One row per correction of ONE sales line's หน่วย. Express is not edited, so
-- the row keeps the Express signature the correction was made against
-- (unit, qty, unit_price, net, product): the daily zip reads the line as
-- unchanged only while Express still says exactly that.
--
-- Keyed on the line key (doc_no, bsn_code), not sales_transactions.id: the
-- importer replaces a changed line with DELETE + INSERT, which issues a new id.
--
-- States: active -> cancelled (admin) | retired (importer). The CHECKs hold
-- each state's shape; the partial unique index allows one ACTIVE correction
-- per line and any number of ended ones, so a new correction is a new row.
--
-- offset_txn_id has no REFERENCES on purpose: an ended correction outlives the
-- offset row that cancel/retire deletes.
--
-- Pure DDL, no data. Re-runnable: the index is dropped before its CREATE and
-- the table is IF NOT EXISTS, so a second apply never drops a correction.

DROP INDEX IF EXISTS idx_sales_line_unit_corrections_active;

CREATE TABLE IF NOT EXISTS sales_line_unit_corrections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no            TEXT    NOT NULL,
    bsn_code          TEXT    NOT NULL,
    doc_base          TEXT    NOT NULL,
    product_id        INTEGER NOT NULL,
    express_unit_raw  TEXT    NOT NULL,   -- sales_transactions.unit verbatim; cancel restores this
    express_unit      TEXT    NOT NULL,   -- the same, normalised
    qty               REAL    NOT NULL,
    unit_price        REAL,
    net               REAL,
    corrected_unit    TEXT    NOT NULL,
    stock_mode        TEXT    NOT NULL CHECK (stock_mode IN ('hold', 'move')),
    offset_txn_id     INTEGER,            -- transactions.id of the offset ADJUST; NULL in 'move'
    reason            TEXT    NOT NULL CHECK (length(trim(reason)) >= 12),
    created_by        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    status            TEXT    NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'cancelled', 'retired')),
    ended_at          TEXT,
    ended_by          TEXT,
    end_cause         TEXT,
    end_reason        TEXT,               -- the admin's reason on cancel
    CHECK ((status = 'active') = (ended_at IS NULL)),
    CHECK ((status = 'active') = (end_cause IS NULL)),
    CHECK ((status = 'cancelled') = (end_cause IS 'cancelled')),
    CHECK (status <> 'retired'
           OR end_cause IN ('express_changed', 'express_removed', 'express_agrees'))
);

CREATE UNIQUE INDEX idx_sales_line_unit_corrections_active
    ON sales_line_unit_corrections (doc_no, bsn_code) WHERE status = 'active';
