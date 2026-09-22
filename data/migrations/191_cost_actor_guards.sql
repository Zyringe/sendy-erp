-- 191 — every cost write carries who made it (#590, PR 2 of 2).
--
-- Design: docs/specs/2026-09-19-590-cost-audit-actor-design.md §A3, §A4, §A5
-- (branch feat/590-cost-audit-actor, revision 2). PR 1 (#621) registered the
-- SQL function sendy_actor(field) on every connection the app opens; this file
-- is the first thing that CALLS it, so it must never run on code older than PR 1.
--
-- WHAT IS GUARDED — exactly this, nothing more:
--   products              UPDATE OF cost_price, opening_cost: refused when a value
--                         changes and nobody is declared; the change is audited
--                         with user / change_source / change_reason. opening_cost
--                         (the WACC basis) is audited for the first time.
--   product_cost_ledger   INSERT, UPDATE, DELETE: refused when nobody is declared.
--                         No actor column: every recalculation deletes and
--                         re-inserts a product's rows, so a column would only
--                         name the last recalculator. The engine writes one
--                         recalc-event audit row instead (models/wacc.py).
--   conversion_cost_log   INSERT, UPDATE, DELETE: refused when nobody is declared.
--                         written_by is stamped BY THE DATABASE as JSON
--                         {who, source, reason}; a caller can neither supply
--                         nor rewrite it. The 40 existing rows become
--                         'legacy:pre-590'.
--
-- CEILING (design §5): products INSERT, INSERT OR REPLACE / REPLACE INTO
-- products (never fires an UPDATE trigger) and products DELETE are recorded by
-- the existing audit triggers WITHOUT an actor and are not refused. A function
-- inside a products INSERT or DELETE trigger would break every raw INSERT —
-- SQLite resolves it when it compiles the statement (tests/test_actor_sqlite_contract.py).
--
-- ⚠ A connection that has not registered sendy_actor() — the sqlite3 CLI, a raw
-- sqlite3.connect() — can no longer change cost at all ("no such function:
-- sendy_actor"). That is the guard working, not a regression. Scripts use
-- database.script_connection(__file__, operator=..., reason=...).
--
-- ⚠ ROLLBACK ORDER: run 191_cost_actor_guards.rollback.sql BEFORE reverting any
-- code, and never revert PR 1 (#621) while these triggers exist — every cost
-- write would then fail compilation. The rollback restores audit_products_update
-- byte-identically (mig 159's text) and drops written_by.
--
-- Re-runnability: every trigger is DROP ... IF EXISTS first. ALTER TABLE ADD
-- COLUMN is not idempotent; a hand re-run on a DB that already has written_by
-- should start from the first DROP TRIGGER.

BEGIN;

ALTER TABLE conversion_cost_log ADD COLUMN written_by TEXT;
UPDATE conversion_cost_log SET written_by = 'legacy:pre-590' WHERE written_by IS NULL;

-- products: cost leaves the general audit trigger (which has no actor) and gets
-- its own column-list pair. A column list means only a statement that SETs a
-- cost column compiles these, so raw non-cost UPDATEs of products keep working.
DROP TRIGGER IF EXISTS audit_products_update;
CREATE TRIGGER audit_products_update
AFTER UPDATE ON products
WHEN (
       OLD.product_name        IS NOT NEW.product_name
    OR OLD.unit_type           IS NOT NEW.unit_type
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.units_per_carton    IS NOT NEW.units_per_carton
    OR OLD.units_per_box       IS NOT NEW.units_per_box
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    OR OLD.hard_to_sell        IS NOT NEW.hard_to_sell
    OR OLD.is_active           IS NOT NEW.is_active
    OR OLD.weight_kg           IS NOT NEW.weight_kg
    OR OLD.weight_source       IS NOT NEW.weight_source
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'products', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'product_name'        AS field, OLD.product_name        AS old_v, NEW.product_name        AS new_v WHERE OLD.product_name        IS NOT NEW.product_name
        UNION ALL SELECT 'unit_type',           OLD.unit_type,           NEW.unit_type           WHERE OLD.unit_type           IS NOT NEW.unit_type
        UNION ALL SELECT 'base_sell_price',     OLD.base_sell_price,     NEW.base_sell_price     WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'units_per_carton',    OLD.units_per_carton,    NEW.units_per_carton    WHERE OLD.units_per_carton    IS NOT NEW.units_per_carton
        UNION ALL SELECT 'units_per_box',       OLD.units_per_box,       NEW.units_per_box       WHERE OLD.units_per_box       IS NOT NEW.units_per_box
        UNION ALL SELECT 'low_stock_threshold', OLD.low_stock_threshold, NEW.low_stock_threshold WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
        UNION ALL SELECT 'hard_to_sell',        OLD.hard_to_sell,        NEW.hard_to_sell        WHERE OLD.hard_to_sell        IS NOT NEW.hard_to_sell
        UNION ALL SELECT 'is_active',           OLD.is_active,           NEW.is_active           WHERE OLD.is_active           IS NOT NEW.is_active
        UNION ALL SELECT 'weight_kg',           OLD.weight_kg,           NEW.weight_kg           WHERE OLD.weight_kg           IS NOT NEW.weight_kg
        UNION ALL SELECT 'weight_source',       OLD.weight_source,       NEW.weight_source       WHERE OLD.weight_source       IS NOT NEW.weight_source
    );
END;

DROP TRIGGER IF EXISTS products_cost_needs_actor;
CREATE TRIGGER products_cost_needs_actor
BEFORE UPDATE OF cost_price, opening_cost ON products
WHEN (OLD.cost_price IS NOT NEW.cost_price OR OLD.opening_cost IS NOT NEW.opening_cost)
 AND sendy_actor('who') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'ต้องระบุตัวผู้แก้ต้นทุน (#590): เขียนผ่านหน้าเว็บ หรือเปิด connection ด้วย database.script_connection(__file__, operator=..., reason=...)');
END;

DROP TRIGGER IF EXISTS audit_products_cost_update;
CREATE TRIGGER audit_products_cost_update
AFTER UPDATE OF cost_price, opening_cost ON products
WHEN (OLD.cost_price IS NOT NEW.cost_price OR OLD.opening_cost IS NOT NEW.opening_cost)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields,
                           user, change_source, change_reason)
    SELECT 'products', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v)),
           sendy_actor('who'), sendy_actor('source'), sendy_actor('reason')
    FROM (
                  SELECT 'cost_price'   AS field, OLD.cost_price   AS old_v, NEW.cost_price   AS new_v WHERE OLD.cost_price   IS NOT NEW.cost_price
        UNION ALL SELECT 'opening_cost',          OLD.opening_cost,          NEW.opening_cost          WHERE OLD.opening_cost IS NOT NEW.opening_cost
    );
END;

-- product_cost_ledger: every verb.
DROP TRIGGER IF EXISTS product_cost_ledger_insert_needs_actor;
CREATE TRIGGER product_cost_ledger_insert_needs_actor
BEFORE INSERT ON product_cost_ledger
WHEN sendy_actor('who') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'ต้องระบุตัวผู้แก้ต้นทุน (#590): เขียนผ่านหน้าเว็บ หรือเปิด connection ด้วย database.script_connection(__file__, operator=..., reason=...)');
END;

DROP TRIGGER IF EXISTS product_cost_ledger_update_needs_actor;
CREATE TRIGGER product_cost_ledger_update_needs_actor
BEFORE UPDATE ON product_cost_ledger
WHEN sendy_actor('who') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'ต้องระบุตัวผู้แก้ต้นทุน (#590): เขียนผ่านหน้าเว็บ หรือเปิด connection ด้วย database.script_connection(__file__, operator=..., reason=...)');
END;

DROP TRIGGER IF EXISTS product_cost_ledger_delete_needs_actor;
CREATE TRIGGER product_cost_ledger_delete_needs_actor
BEFORE DELETE ON product_cost_ledger
WHEN sendy_actor('who') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'ต้องระบุตัวผู้แก้ต้นทุน (#590): เขียนผ่านหน้าเว็บ หรือเปิด connection ด้วย database.script_connection(__file__, operator=..., reason=...)');
END;

-- conversion_cost_log: every verb, plus a stamp the database owns.
DROP TRIGGER IF EXISTS conversion_cost_log_insert_needs_actor;
CREATE TRIGGER conversion_cost_log_insert_needs_actor
BEFORE INSERT ON conversion_cost_log
WHEN sendy_actor('who') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'ต้องระบุตัวผู้แก้ต้นทุน (#590): เขียนผ่านหน้าเว็บ หรือเปิด connection ด้วย database.script_connection(__file__, operator=..., reason=...)');
END;

DROP TRIGGER IF EXISTS conversion_cost_log_written_by_is_stamped;
CREATE TRIGGER conversion_cost_log_written_by_is_stamped
BEFORE INSERT ON conversion_cost_log
WHEN NEW.written_by IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'written_by is stamped by the database; do not supply it (#590)');
END;

DROP TRIGGER IF EXISTS conversion_cost_log_stamp;
CREATE TRIGGER conversion_cost_log_stamp
AFTER INSERT ON conversion_cost_log
BEGIN
    UPDATE conversion_cost_log
       SET written_by = json_object('who', sendy_actor('who'),
                                    'source', sendy_actor('source'),
                                    'reason', sendy_actor('reason'))
     WHERE id = NEW.id;
END;

DROP TRIGGER IF EXISTS conversion_cost_log_update_needs_actor;
CREATE TRIGGER conversion_cost_log_update_needs_actor
BEFORE UPDATE ON conversion_cost_log
WHEN sendy_actor('who') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'ต้องระบุตัวผู้แก้ต้นทุน (#590): เขียนผ่านหน้าเว็บ หรือเปิด connection ด้วย database.script_connection(__file__, operator=..., reason=...)');
END;

DROP TRIGGER IF EXISTS conversion_cost_log_written_by_is_final;
CREATE TRIGGER conversion_cost_log_written_by_is_final
BEFORE UPDATE OF written_by ON conversion_cost_log
WHEN OLD.written_by IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'written_by is stamped once and never rewritten (#590)');
END;

DROP TRIGGER IF EXISTS conversion_cost_log_delete_needs_actor;
CREATE TRIGGER conversion_cost_log_delete_needs_actor
BEFORE DELETE ON conversion_cost_log
WHEN sendy_actor('who') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'ต้องระบุตัวผู้แก้ต้นทุน (#590): เขียนผ่านหน้าเว็บ หรือเปิด connection ด้วย database.script_connection(__file__, operator=..., reason=...)');
END;

COMMIT;
