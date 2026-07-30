-- ============================================================================
-- Migration 143 — commission_customer_reassign
--
-- Apply:    restart the server (database.py::init_db() auto-applies on boot)
-- Rollback: 143_commission_customer_reassign.rollback.sql
--
-- Why
--   Commission is attributed from `received_payments.salesperson` — the rep
--   code stamped on the RE document by Express. When a rep stops servicing a
--   customer, Express keeps stamping the old code, so the departed rep keeps
--   earning on that customer forever.
--
--   Sendy had no way to say otherwise. `customers.salesperson` looks like the
--   lever but is NOT read by the commission engine at all: three of the four
--   customers below were already set to '00' on 2026-04-24 and still fed rep
--   31's commission base in June 2026 (฿13,381.50 of it). Editing
--   `received_payments.salesperson` directly is not an option either — the
--   importer UPSERTs that column, so any hand-edit is silently overwritten on
--   the next import (models/payments.py).
--
--   This table is therefore the durable, import-proof place to record the
--   decision.
--
-- Semantics — deliberately keyed on the INVOICE date, not the receipt date
--   A rule applies to a sales document when
--       sales_transactions.customer_code = customer_code
--   AND sales_transactions.date_iso     >= effective_from
--
--   "He sold it, he earns it": an order written BEFORE the cut keeps paying
--   the original rep whenever it is eventually collected, so no already-paid
--   commission cycle is ever restated. Keying on the receipt date instead
--   would be a hard cutover that retroactively strips a rep of sales he
--   genuinely made. (Put's call, 2026-07-30.)
--
--   Multiple rows per customer are allowed. The rule that applies to a
--   document is the one with the LATEST effective_from <= that document's
--   date, so a customer can move 31 -> 00 now and 00 -> someone else later
--   with the history staying correct.
--
-- Scope
--   Attribution only. It does not change rates (that is
--   `commission_overrides`), does not touch `commission_payouts` (payout rows
--   keep whatever salesperson_code they were recorded under), and does not
--   alter `received_payments` or any imported data.
--
--   ⚠ Because payouts are NOT rewritten, creating a rule whose effective_from
--   reaches into an ALREADY-PAID cycle will leave that cycle showing an
--   overpayment. That is a real outcome, not a bug — but pick dates
--   deliberately. The four seeded rules below are all chosen to fall after
--   every cycle already paid to rep 31.
--
-- Seed — the four customers Put moved to the company (2026-07-30).
--   Each date is one day before the first order that should belong to the
--   company. Verified against every 2026 order for these customers: no
--   already-paid cycle is disturbed, and no uncollected pre-cut order exists,
--   so nothing keeps paying rep 31 after the switch.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

CREATE TABLE commission_customer_reassign (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code  TEXT    NOT NULL REFERENCES customers(code),
    to_salesperson TEXT    NOT NULL REFERENCES salespersons(code),
    effective_from TEXT    NOT NULL,                    -- YYYY-MM-DD, vs INVOICE date
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note           TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(customer_code, effective_from)
);

-- The engine resolves a rule per sales line: filter by customer_code, then
-- take the latest effective_from at or before the line's date.
CREATE INDEX idx_ccr_lookup
    ON commission_customer_reassign(customer_code, effective_from DESC);

CREATE TRIGGER audit_commission_customer_reassign_insert
AFTER INSERT ON commission_customer_reassign
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_customer_reassign', NEW.id, 'INSERT',
        json_object(
            'customer_code',  NEW.customer_code,
            'to_salesperson', NEW.to_salesperson,
            'effective_from', NEW.effective_from,
            'is_active',      NEW.is_active,
            'note',           NEW.note
        ));
END;

CREATE TRIGGER audit_commission_customer_reassign_update
AFTER UPDATE ON commission_customer_reassign
WHEN (
       OLD.customer_code  IS NOT NEW.customer_code
    OR OLD.to_salesperson IS NOT NEW.to_salesperson
    OR OLD.effective_from IS NOT NEW.effective_from
    OR OLD.is_active      IS NOT NEW.is_active
    OR OLD.note           IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'commission_customer_reassign', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'customer_code'  AS field, OLD.customer_code  AS old_v, NEW.customer_code  AS new_v WHERE OLD.customer_code  IS NOT NEW.customer_code
        UNION ALL SELECT 'to_salesperson',          OLD.to_salesperson,          NEW.to_salesperson          WHERE OLD.to_salesperson IS NOT NEW.to_salesperson
        UNION ALL SELECT 'effective_from',          OLD.effective_from,          NEW.effective_from          WHERE OLD.effective_from IS NOT NEW.effective_from
        UNION ALL SELECT 'is_active',               OLD.is_active,               NEW.is_active               WHERE OLD.is_active      IS NOT NEW.is_active
        UNION ALL SELECT 'note',                    OLD.note,                    NEW.note                    WHERE OLD.note           IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_commission_customer_reassign_delete
BEFORE DELETE ON commission_customer_reassign
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_customer_reassign', OLD.id, 'DELETE',
        json_object('customer_code',  OLD.customer_code,
                    'to_salesperson', OLD.to_salesperson,
                    'effective_from', OLD.effective_from));
END;

-- Seeded only where the customer and the target rep both exist, so a fresh
-- DB built from schema.sql (no customer master yet) applies cleanly.
INSERT INTO commission_customer_reassign
       (customer_code, to_salesperson, effective_from, note)
SELECT v.cc, '00', v.eff, v.note
  FROM (SELECT '58บ001' AS cc, '2026-04-07' AS eff,
               'บัญชารถเกี่ยว → บริษัท. หนุ่ม /31 ออก; ออร์เดอร์ตั้งแต่ IV6900514 (ซื้อ 2026-04-08) เป็นของบริษัท. บิล ม.ค. ที่จ่ายคอมไปแล้วรอบ ก.พ. ไม่กระทบ. (Put 2026-07-30)' AS note
        UNION ALL SELECT '62ค003', '2026-05-06',
               'ร้าน คูณมีวัสดุ → บริษัท. ออร์เดอร์ตั้งแต่ IV6900652 (ซื้อ 2026-05-07) เป็นของบริษัท. (Put 2026-07-30)'
        UNION ALL SELECT '47ท002', '2026-05-20',
               'หจก. ไทยทวีกิจ → บริษัท. ออร์เดอร์ตั้งแต่ IV6900737 (ซื้อ 2026-05-21) เป็นของบริษัท. บิล ม.ค. 5 ใบ ที่จ่ายคอมไปแล้วรอบ ก.พ. ไม่กระทบ. (Put 2026-07-30)'
        UNION ALL SELECT '62ห007', '2026-04-26',
               'หนองแสงอลูมินั่ม → บริษัท. ออร์เดอร์ตั้งแต่ IV6900581 (ซื้อ 2026-04-27) เป็นของบริษัท. บิล ม.ค. ที่จ่ายคอมไปแล้วรอบ เม.ย. ไม่กระทบ. (Put 2026-07-30)') v
 WHERE EXISTS (SELECT 1 FROM customers    c WHERE c.code = v.cc)
   AND EXISTS (SELECT 1 FROM salespersons s WHERE s.code = '00');

-- Keep the customer master consistent with the commission decision. The other
-- three were already set to '00' on 2026-04-24; ร้าน คูณมีวัสดุ was missed.
UPDATE customers SET salesperson = '00'
 WHERE code = '62ค003' AND salesperson = '31';

COMMIT;
