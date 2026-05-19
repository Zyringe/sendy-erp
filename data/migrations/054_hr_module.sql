-- 054_hr_module.sql
-- Phase 1 of the Sendy modularization: the Human Resources module.
-- Creates all HR tables (employees, salary history, leave, payroll,
-- config, holidays) + seeds leave types, hr_config, and the two real
-- BSN contract employees (วุฒิพงษ์ EMP001, วิภา EMP002) with their
-- salary history.
--
-- Dates are stored ISO/Gregorian internally; พ.ศ. conversion is a
-- display-layer concern only.
--
-- Apply:    sqlite3 .../inventory.db < .../migrations/054_hr_module.sql
--           (in practice the runner applies it: database.py::run_pending_migrations)
-- Rollback: 054_hr_module.rollback.sql
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner
-- (database.py::run_pending_migrations) records every migration it
-- executes; a self-insert would duplicate-key crash on boot.

BEGIN;

-- ── employees ─────────────────────────────────────────────────────────────
CREATE TABLE employees (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_code            TEXT    UNIQUE NOT NULL,
    full_name           TEXT    NOT NULL,
    nickname            TEXT,
    national_id         TEXT,
    gender              TEXT    CHECK(gender IN ('M','F')),
    phone               TEXT,
    address             TEXT,
    position            TEXT,
    company_id          INTEGER REFERENCES companies(id),
    employment_type     TEXT    NOT NULL DEFAULT 'monthly'
                                CHECK(employment_type IN ('monthly','daily','contract')),
    start_date          TEXT,
    probation_days      INTEGER NOT NULL DEFAULT 90,
    probation_end_date  TEXT,
    end_date            TEXT,
    sso_enrolled        INTEGER NOT NULL DEFAULT 1 CHECK(sso_enrolled IN (0,1)),
    diligence_allowance REAL    NOT NULL DEFAULT 0,
    bank_name           TEXT,
    bank_branch         TEXT,
    bank_account_no     TEXT,
    bank_account_name   TEXT,
    salesperson_code    TEXT,
    user_id             INTEGER REFERENCES users(id),
    is_active           INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_employees_company  ON employees(company_id);
CREATE INDEX idx_employees_active    ON employees(is_active);
CREATE INDEX idx_employees_user      ON employees(user_id);

-- ── employee_salary_history ───────────────────────────────────────────────
-- Source of truth for the monthly rate applied in any given month.
CREATE TABLE employee_salary_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id),
    effective_date  TEXT    NOT NULL,
    monthly_salary  REAL    NOT NULL,
    reason          TEXT    NOT NULL DEFAULT 'initial'
                            CHECK(reason IN ('initial','post_probation','raise','adjust')),
    note            TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(employee_id, effective_date)
);

CREATE INDEX idx_salary_hist_emp ON employee_salary_history(employee_id);

-- ── leave_types ───────────────────────────────────────────────────────────
CREATE TABLE leave_types (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    code                     TEXT    UNIQUE NOT NULL,
    name_th                  TEXT    NOT NULL,
    default_quota_days       REAL,                       -- NULL = unlimited
    is_paid                  INTEGER NOT NULL DEFAULT 1 CHECK(is_paid IN (0,1)),
    affects_diligence        INTEGER NOT NULL DEFAULT 0 CHECK(affects_diligence IN (0,1)),
    requires_cert_after_days INTEGER,
    quota_basis              TEXT    CHECK(quota_basis IN ('after_1yr') OR quota_basis IS NULL),
    max_paid_days            REAL,
    sort_order               INTEGER NOT NULL DEFAULT 100,
    is_active                INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note                     TEXT
);

INSERT INTO leave_types
    (code, name_th, default_quota_days, is_paid, affects_diligence,
     requires_cert_after_days, quota_basis, max_paid_days, sort_order, note)
VALUES
    ('ANNUAL',    'ลาพักผ่อนประจำปี',  6,    1, 0, NULL, 'after_1yr', NULL, 10,
        'สิทธิเกิดเมื่ออายุงาน ≥ 1 ปี (สัญญาข้อ 5.3) — ก่อนครบปี entitlement = 0'),
    ('SICK',      'ลาป่วย',          30,   1, 1, 3,    NULL,        NULL, 20,
        'ต้องมีใบรับรองแพทย์เมื่อลาตั้งแต่ 3 วันขึ้นไป; กระทบเบี้ยขยัน'),
    ('PERSONAL',  'ลากิจ',            3,    1, 1, NULL, NULL,        NULL, 30,
        'ลากิจส่วนตัว; กระทบเบี้ยขยัน'),
    ('MATERNITY', 'ลาคลอด',          98,   1, 0, NULL, NULL,        45,   40,
        'ลาคลอดได้ 98 วัน นายจ้างจ่ายค่าจ้าง 45 วัน (ส่วนเกินไม่รับค่าจ้าง)'),
    ('UNPAID',    'ลาไม่รับค่าจ้าง',   NULL, 0, 1, NULL, NULL,        NULL, 50,
        'ลาโดยไม่รับค่าจ้าง; ไม่จำกัดโควตา; กระทบเบี้ยขยัน');

-- ── employee_leave_entitlements ───────────────────────────────────────────
-- Per-employee per-year override. Absent row → fall back to
-- leave_types.default_quota_days.
CREATE TABLE employee_leave_entitlements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
    year          INTEGER NOT NULL,
    quota_days    REAL,
    note          TEXT,
    UNIQUE(employee_id, leave_type_id, year)
);

CREATE INDEX idx_leave_entl_emp ON employee_leave_entitlements(employee_id);

-- ── leave_requests ────────────────────────────────────────────────────────
CREATE TABLE leave_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id      INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id    INTEGER NOT NULL REFERENCES leave_types(id),
    start_date       TEXT    NOT NULL,
    end_date         TEXT    NOT NULL,
    days             REAL    NOT NULL,           -- 0.5 allowed (half-day)
    reason           TEXT,
    has_medical_cert INTEGER NOT NULL DEFAULT 0 CHECK(has_medical_cert IN (0,1)),
    status           TEXT    NOT NULL DEFAULT 'approved'
                             CHECK(status IN ('pending','approved','rejected','cancelled')),
    created_by       TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_leave_req_emp    ON leave_requests(employee_id);
CREATE INDEX idx_leave_req_type   ON leave_requests(leave_type_id);
CREATE INDEX idx_leave_req_dates  ON leave_requests(start_date, end_date);

-- ── payroll_runs ──────────────────────────────────────────────────────────
CREATE TABLE payroll_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month   TEXT    NOT NULL,               -- 'YYYY-MM'
    company_id   INTEGER REFERENCES companies(id),
    status       TEXT    NOT NULL DEFAULT 'draft'
                         CHECK(status IN ('draft','finalized')),
    run_date     TEXT,
    finalized_at TEXT,
    created_by   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(year_month, company_id)
);

CREATE INDEX idx_payroll_runs_company ON payroll_runs(company_id);

-- ── payroll_items ─────────────────────────────────────────────────────────
CREATE TABLE payroll_items (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES payroll_runs(id),
    employee_id             INTEGER NOT NULL REFERENCES employees(id),
    salary_rate             REAL    NOT NULL DEFAULT 0,
    base_amount             REAL    NOT NULL DEFAULT 0,
    unpaid_leave_days       REAL    NOT NULL DEFAULT 0,
    unpaid_leave_deduction  REAL    NOT NULL DEFAULT 0,
    diligence_allowance     REAL    NOT NULL DEFAULT 0,
    diligence_forfeited     INTEGER NOT NULL DEFAULT 0 CHECK(diligence_forfeited IN (0,1)),
    diligence_forfeit_reason TEXT   CHECK(diligence_forfeit_reason IN ('leave','late')
                                          OR diligence_forfeit_reason IS NULL),
    bonus                   REAL    NOT NULL DEFAULT 0,
    other_additions         REAL    NOT NULL DEFAULT 0,
    other_additions_note    TEXT,
    other_deductions        REAL    NOT NULL DEFAULT 0,
    other_deductions_note   TEXT,
    sso_employee            REAL    NOT NULL DEFAULT 0,
    sso_employer            REAL    NOT NULL DEFAULT 0,
    commission_amount       REAL    NOT NULL DEFAULT 0,
    gross                   REAL    NOT NULL DEFAULT 0,
    net_pay                 REAL    NOT NULL DEFAULT 0,
    note                    TEXT,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, employee_id)
);

CREATE INDEX idx_payroll_items_run ON payroll_items(run_id);
CREATE INDEX idx_payroll_items_emp ON payroll_items(employee_id);

-- ── hr_config ─────────────────────────────────────────────────────────────
CREATE TABLE hr_config (
    key   TEXT PRIMARY KEY,
    value TEXT,
    note  TEXT
);

INSERT INTO hr_config (key, value, note) VALUES
    ('sso_rate',     '0.05',  'อัตราเงินสมทบประกันสังคม (ลูกจ้าง/นายจ้างฝั่งละ 5%)'),
    ('sso_min_base', '1650',  'ฐานค่าจ้างขั้นต่ำสำหรับคำนวณประกันสังคม (บาท/เดือน)'),
    ('sso_max_base', '15000', 'ฐานค่าจ้างสูงสุดสำหรับคำนวณประกันสังคม (เพดานหัก 750/เดือน)'),
    ('day_divisor',  '30',    'ตัวหารวันต่อเดือนสำหรับ prorate/หักลา (ค่าจ้าง ÷ 30 × วัน)');

-- ── company_holidays ──────────────────────────────────────────────────────
-- Seeded empty in v1. The contract references ≥13 วันหยุดประเพณี/ปี but
-- holidays are not yet wired into payroll deductions.
CREATE TABLE company_holidays (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER REFERENCES companies(id),
    holiday_date TEXT    NOT NULL,
    name_th      TEXT,
    year         INTEGER
);

CREATE INDEX idx_company_holidays_company ON company_holidays(company_id);

-- ── seed: the two real BSN contract employees ─────────────────────────────
INSERT INTO employees
    (emp_code, full_name, gender, national_id, phone, address, position,
     company_id, employment_type, start_date, probation_days,
     probation_end_date, sso_enrolled, diligence_allowance,
     bank_name, bank_branch, bank_account_no, bank_account_name, is_active)
VALUES
    ('EMP001', 'วุฒิพงษ์ แปงนุจา', 'M', '1529900888689', '0936960709',
     '37 หมู่ที่ 2 ตำบลนาแก้ว อำเภอเกาะคา จังหวัดลำปาง',
     'พนักงานยกของและขับรถส่งของ',
     1, 'monthly', '2026-05-02', 90, '2026-07-30', 1, 500,
     'กสิกรไทย', 'เซ็นทรัล ลำปาง', '187-2-91746-9', 'นาย วุฒิพงษ์ แปงนุจา', 1),
    ('EMP002', 'วิภา ขมสันเทียะ', 'F', '1300800128279', '0615569387',
     '169 หมู่ที่ 10 ตำบลโนนเมืองพัฒนา อำเภอด่านขุนทด จังหวัดนครราชสีมา',
     'เสมียน',
     1, 'monthly', '2026-04-01', 90, '2026-06-29', 1, 500,
     'กรุงไทย', 'บิ๊กซี การเคหะ พระราม 2', '173-0-43577-7', 'น.ส. วิภา ขมสันเทียะ', 1);

-- ── seed: salary history ──────────────────────────────────────────────────
-- Subselects keep this correct regardless of autoincrement ids.
INSERT INTO employee_salary_history
    (employee_id, effective_date, monthly_salary, reason, note)
VALUES
    ((SELECT id FROM employees WHERE emp_code='EMP001'),
     '2026-05-02', 13000, 'initial', 'ค่าจ้างเริ่มต้นตามสัญญา (คงที่)'),
    ((SELECT id FROM employees WHERE emp_code='EMP002'),
     '2026-04-01', 13000, 'initial', 'ค่าจ้างช่วงทดลองงาน'),
    ((SELECT id FROM employees WHERE emp_code='EMP002'),
     '2026-07-01', 15000, 'post_probation',
     'ทดลองงาน 90 วันสิ้นสุด 2026-06-29 (วันที่ 91 = 2026-06-30); ปรับขึ้นมีผลเดือนเต็มถัดไป = 2026-07-01');

COMMIT;
