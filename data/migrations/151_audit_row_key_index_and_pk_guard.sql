-- ============================================================================
-- 151 — index `audit_log(table_name, row_key)`, and pin the four TEXT
-- business keys that `row_key` depends on as immutable.
--
-- Follows migration 150 (audit_log.row_key TEXT, backfilled + written by 12
-- triggers on customers/salespersons/commission_assignments/customer_crm).
-- Two gaps PR #351's review flagged before the history card can trust it
-- (see projects/customer-edit-card/plan.md, "P4a MERGED"):
--
-- 1. row_key is stable against VACUUM (that's the whole point of 150) but
--    NOT against a rename of the business key itself. Repro on a copy:
--    INSERT customers code='RK-OLD' -> audit row row_key='RK-OLD'; UPDATE
--    customers SET code='RK-NEW' succeeds but audit_customers_update never
--    fires, because `code` is not in its WHEN clause — history queried by
--    RK-NEW then can't see anything from before the rename.
--
--    Verified 2026-08-02: no application code path renames any of these
--    four keys today (grepped every UPDATE against these tables + every
--    `SET code=` / `SET customer_code=` / `SET salesperson_code=` in
--    *.py — the only hits are commission_customer_reassign.customer_code,
--    an FK column on a DIFFERENT table, and cashbook_accounts.code, an
--    unrelated table). So this migration makes that already-true property
--    an enforced invariant rather than an unwritten assumption: `code` is
--    Express's customer import match key and is referenced by
--    commission_customer_reassign, product_code_mapping and others, so a
--    silent rename would already have broken those, not just history.
--
-- 2. The history card queries `audit_log` by `row_key`; the only existing
--    indexes are `idx_audit_table_row` (table_name, row_id) and
--    `idx_audit_log_table_time` (table_name, created_at) — neither covers
--    row_key, so every render would scan.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

CREATE INDEX idx_audit_log_table_row_key ON audit_log(table_name, row_key);

-- ── customers.code is immutable ─────────────────────────────────────────
CREATE TRIGGER customers_code_immutable
BEFORE UPDATE ON customers
WHEN NEW.code IS NOT OLD.code
BEGIN
    SELECT RAISE(ABORT,
        'ห้ามเปลี่ยนรหัสลูกค้า (customers.code) เพราะเป็น key ที่ผูกกับ commission_customer_reassign, product_code_mapping, การนำเข้า Express และประวัติ audit_log — ถ้าต้องการเปลี่ยนรหัส ให้สร้างลูกค้าใหม่ด้วยรหัสที่ต้องการแล้วย้ายข้อมูล/ประวัติไปแทน');
END;

-- ── salespersons.code is immutable ──────────────────────────────────────
CREATE TRIGGER salespersons_code_immutable
BEFORE UPDATE ON salespersons
WHEN NEW.code IS NOT OLD.code
BEGIN
    SELECT RAISE(ABORT,
        'ห้ามเปลี่ยนรหัสพนักงานขาย (salespersons.code) เพราะเป็น key ที่ผูกกับ commission_assignments, customers.salesperson และประวัติคอมมิชชั่น — ถ้าต้องการเปลี่ยนรหัส ให้สร้างพนักงานขายใหม่ด้วยรหัสที่ต้องการแล้วย้ายลูกค้า/กฎคอมมิชชั่นไปแทน');
END;

-- ── commission_assignments.salesperson_code is immutable ────────────────
CREATE TRIGGER commission_assignments_salesperson_code_immutable
BEFORE UPDATE ON commission_assignments
WHEN NEW.salesperson_code IS NOT OLD.salesperson_code
BEGIN
    SELECT RAISE(ABORT,
        'ห้ามเปลี่ยน salesperson_code ของกฎคอมมิชชั่นนี้ (เป็น primary key) — ให้ลบกฎเดิมแล้วสร้างกฎใหม่ให้พนักงานขายที่ถูกต้องแทน');
END;

-- ── customer_crm.customer_code is immutable ─────────────────────────────
CREATE TRIGGER customer_crm_customer_code_immutable
BEFORE UPDATE ON customer_crm
WHEN NEW.customer_code IS NOT OLD.customer_code
BEGIN
    SELECT RAISE(ABORT,
        'ห้ามเปลี่ยน customer_code ของ CRM row นี้ (เป็น primary key) — ให้ลบ row เดิมแล้วสร้างใหม่ให้ลูกค้าที่ถูกต้องแทน');
END;

COMMIT;
