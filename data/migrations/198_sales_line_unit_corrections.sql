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
-- Pure DDL, no data. Re-runnable: the index and the triggers are dropped
-- before their CREATE and the table is IF NOT EXISTS, so a second apply never
-- drops a correction.

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

-- audit_log, the same three-trigger shape as the other audited tables. `user`
-- is the person the row itself names: who made it, then who ended it.
DROP TRIGGER IF EXISTS audit_sales_line_unit_corrections_insert;
DROP TRIGGER IF EXISTS audit_sales_line_unit_corrections_update;
DROP TRIGGER IF EXISTS audit_sales_line_unit_corrections_delete;

CREATE TRIGGER audit_sales_line_unit_corrections_insert
AFTER INSERT ON sales_line_unit_corrections
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields, user)
    VALUES (
        'sales_line_unit_corrections', NEW.id, NEW.doc_no || '|' || NEW.bsn_code, 'INSERT',
        json_object(
            'doc_no',           NEW.doc_no,
            'bsn_code',         NEW.bsn_code,
            'doc_base',         NEW.doc_base,
            'product_id',       NEW.product_id,
            'express_unit_raw', NEW.express_unit_raw,
            'express_unit',     NEW.express_unit,
            'qty',              NEW.qty,
            'unit_price',       NEW.unit_price,
            'net',              NEW.net,
            'corrected_unit',   NEW.corrected_unit,
            'stock_mode',       NEW.stock_mode,
            'offset_txn_id',    NEW.offset_txn_id,
            'reason',           NEW.reason,
            'created_by',       NEW.created_by,
            'status',           NEW.status,
            'ended_at',         NEW.ended_at,
            'ended_by',         NEW.ended_by,
            'end_cause',        NEW.end_cause,
            'end_reason',       NEW.end_reason
        ),
        NEW.created_by
    );
END;

CREATE TRIGGER audit_sales_line_unit_corrections_update
AFTER UPDATE ON sales_line_unit_corrections
WHEN (
       OLD.doc_no           IS NOT NEW.doc_no
    OR OLD.bsn_code         IS NOT NEW.bsn_code
    OR OLD.doc_base         IS NOT NEW.doc_base
    OR OLD.product_id       IS NOT NEW.product_id
    OR OLD.express_unit_raw IS NOT NEW.express_unit_raw
    OR OLD.express_unit     IS NOT NEW.express_unit
    OR OLD.qty              IS NOT NEW.qty
    OR OLD.unit_price       IS NOT NEW.unit_price
    OR OLD.net              IS NOT NEW.net
    OR OLD.corrected_unit   IS NOT NEW.corrected_unit
    OR OLD.stock_mode       IS NOT NEW.stock_mode
    OR OLD.offset_txn_id    IS NOT NEW.offset_txn_id
    OR OLD.reason           IS NOT NEW.reason
    OR OLD.created_by       IS NOT NEW.created_by
    OR OLD.status           IS NOT NEW.status
    OR OLD.ended_at         IS NOT NEW.ended_at
    OR OLD.ended_by         IS NOT NEW.ended_by
    OR OLD.end_cause        IS NOT NEW.end_cause
    OR OLD.end_reason       IS NOT NEW.end_reason
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields, user)
    SELECT 'sales_line_unit_corrections', NEW.id, NEW.doc_no || '|' || NEW.bsn_code, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v)),
           COALESCE(NEW.ended_by, NEW.created_by)
    FROM (
        SELECT 'doc_no' AS field, OLD.doc_no AS old_v, NEW.doc_no AS new_v WHERE OLD.doc_no IS NOT NEW.doc_no
        UNION ALL SELECT 'bsn_code', OLD.bsn_code, NEW.bsn_code WHERE OLD.bsn_code IS NOT NEW.bsn_code
        UNION ALL SELECT 'doc_base', OLD.doc_base, NEW.doc_base WHERE OLD.doc_base IS NOT NEW.doc_base
        UNION ALL SELECT 'product_id', OLD.product_id, NEW.product_id WHERE OLD.product_id IS NOT NEW.product_id
        UNION ALL SELECT 'express_unit_raw', OLD.express_unit_raw, NEW.express_unit_raw WHERE OLD.express_unit_raw IS NOT NEW.express_unit_raw
        UNION ALL SELECT 'express_unit', OLD.express_unit, NEW.express_unit WHERE OLD.express_unit IS NOT NEW.express_unit
        UNION ALL SELECT 'qty', OLD.qty, NEW.qty WHERE OLD.qty IS NOT NEW.qty
        UNION ALL SELECT 'unit_price', OLD.unit_price, NEW.unit_price WHERE OLD.unit_price IS NOT NEW.unit_price
        UNION ALL SELECT 'net', OLD.net, NEW.net WHERE OLD.net IS NOT NEW.net
        UNION ALL SELECT 'corrected_unit', OLD.corrected_unit, NEW.corrected_unit WHERE OLD.corrected_unit IS NOT NEW.corrected_unit
        UNION ALL SELECT 'stock_mode', OLD.stock_mode, NEW.stock_mode WHERE OLD.stock_mode IS NOT NEW.stock_mode
        UNION ALL SELECT 'offset_txn_id', OLD.offset_txn_id, NEW.offset_txn_id WHERE OLD.offset_txn_id IS NOT NEW.offset_txn_id
        UNION ALL SELECT 'reason', OLD.reason, NEW.reason WHERE OLD.reason IS NOT NEW.reason
        UNION ALL SELECT 'created_by', OLD.created_by, NEW.created_by WHERE OLD.created_by IS NOT NEW.created_by
        UNION ALL SELECT 'status', OLD.status, NEW.status WHERE OLD.status IS NOT NEW.status
        UNION ALL SELECT 'ended_at', OLD.ended_at, NEW.ended_at WHERE OLD.ended_at IS NOT NEW.ended_at
        UNION ALL SELECT 'ended_by', OLD.ended_by, NEW.ended_by WHERE OLD.ended_by IS NOT NEW.ended_by
        UNION ALL SELECT 'end_cause', OLD.end_cause, NEW.end_cause WHERE OLD.end_cause IS NOT NEW.end_cause
        UNION ALL SELECT 'end_reason', OLD.end_reason, NEW.end_reason WHERE OLD.end_reason IS NOT NEW.end_reason
    );
END;

CREATE TRIGGER audit_sales_line_unit_corrections_delete
BEFORE DELETE ON sales_line_unit_corrections
BEGIN
    INSERT INTO audit_log (table_name, row_id, row_key, action, changed_fields, user)
    VALUES (
        'sales_line_unit_corrections', OLD.id, OLD.doc_no || '|' || OLD.bsn_code, 'DELETE',
        json_object(
            'doc_no',           OLD.doc_no,
            'bsn_code',         OLD.bsn_code,
            'doc_base',         OLD.doc_base,
            'product_id',       OLD.product_id,
            'express_unit_raw', OLD.express_unit_raw,
            'express_unit',     OLD.express_unit,
            'qty',              OLD.qty,
            'unit_price',       OLD.unit_price,
            'net',              OLD.net,
            'corrected_unit',   OLD.corrected_unit,
            'stock_mode',       OLD.stock_mode,
            'offset_txn_id',    OLD.offset_txn_id,
            'reason',           OLD.reason,
            'created_by',       OLD.created_by,
            'status',           OLD.status,
            'ended_at',         OLD.ended_at,
            'ended_by',         OLD.ended_by,
            'end_cause',        OLD.end_cause,
            'end_reason',       OLD.end_reason
        ),
        COALESCE(OLD.ended_by, OLD.created_by)
    );
END;
