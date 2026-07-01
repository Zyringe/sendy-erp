# ADR 0005 — Cashbook is hand-entered; retire the Excel importer + round-trip export

Status: Accepted · 2026-07-01

## Context

The cashbook (`/cashbook`, `cashbook_transactions`) was populated by one path: Put typed
rows into a Google Sheet, exported `.xlsx`, and ran `/cashbook/import`. That importer
(`import_cashbook.py`) is **full-replace per account** (`DELETE ... WHERE account_id=?`
then re-insert) and also did double duty — it **synced the HR Salary_Sheet → `employees`**
and full-replaced `salary_advances`. `/cashbook/export` produced a matching multi-sheet
workbook designed to be re-imported.

Put wants to enter cashbook data **directly in Sendy** (admin + manager + shareholder)
instead of round-tripping a spreadsheet — the sheet is friction and only he can run it.

## Decision

Make manual entry the *only* entry path and remove the import/export surface entirely.

- **Remove** `/cashbook/import` (route + template + the `import_cashbook` cashbook path)
  and `/cashbook/export` (route + builder + the two topbar buttons + the empty-state link).
- **Retire the Salary_Sheet → `employees` sync.** `/cashbook/import` was its only caller;
  the HR module (`/users`, salary history, `/hr/advances`) now owns employees, salaries and
  advances, so the sync is legacy. (Employees/salary are no longer touched from the cashbook
  at all — salary flows the *other* way now; see ADR 0006.)
- **Add a batch entry page** (`/cashbook/new`): a **shared header (date + pay/receive
  account chosen once) + a grid of per-row lines** (direction defaulting to รายจ่าย,
  category, ผู้ใช้ tag, amount, description, note). One batch = one account on one day.
  Save is **all-or-nothing** (validate every non-blank row; blank rows skipped); category
  and ผู้ใช้ tag are pick-or-type comboboxes (a new category is created on save).
- **Per-row edit/delete** on the account ledger for *manual* rows (salary rows are locked —
  ADR 0006), attributed to a new `cashbook_transactions.created_by`, mutations audit-logged.
- **Write access widens**: manager (was read-only) and shareholder (was logout-only) may now
  add/edit/delete cashbook transactions. Staff stays fully blocked from `cashbook.*`.

## Considered options

- **Keep a lightweight read-only export** for the accountant — rejected by Put; the
  dashboard/ledger + DB are the source of truth, and Excel is a rare one-off query.
- **Keep the importer as a fallback** — rejected: it is full-replace, so any imported file
  would silently wipe hand-entered rows for that account. Removing it eliminates that trap.

## Consequences

- **Full-replace-wipes-manual-rows can't happen** once import is gone — the two entry modes
  can't coexist safely, which is *why* import is removed rather than kept alongside the form.
- The ~338 existing rows (all `source_file` = one old temp import) remain as history; new
  rows have `source_file` NULL / `created_by` set.
- No way to bulk-load a spreadsheet anymore. Accepted: batch entry replaces that, and there
  is no recurring external cashbook feed.
- Adding a brand-new cashbook account has no UI (rare); handle out of band until needed.
- **Test fallout**: deleting `import_cashbook.py` + `parse_cashbook.py` + the two routes
  turns ~5 suites red (`test_cashbook_import`, `test_cashbook_parse`, `test_employee_resolver`,
  `test_hr_sync_no_clobber`, `test_bp_cashbook_routes`). They must be deleted/rewritten, and
  the plan must confirm nothing else imports `sync_salary_sheet`. The Salary_Sheet no-clobber
  machinery is retired wholesale.
