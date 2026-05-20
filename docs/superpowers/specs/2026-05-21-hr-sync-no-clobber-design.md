# HR Sync — No Silent Clobber on Existing Employees

**Date:** 2026-05-21
**Status:** Approved by Put, ready for implementation plan
**Origin:** [[project_2026_05_20_resume_here]] open item #2 — "HR nickname-override before cashbook re-imports"

## Problem

`import_cashbook.sync_salary_sheet()` (in `inventory_app/import_cashbook.py:282-316`)
currently fills three fields on an existing employee row if they are NULL/blank:
`nickname`, `bank_name`, `bank_account_no`. The rule was intended as a one-time
bootstrap convenience — populate fields the salary sheet has but the DB doesn't.

This becomes a re-clobber bug whenever Put **intentionally clears** one of those
fields via the HR UI. Concrete case: กิติยา's nickname was set to NULL on purpose;
re-importing the same NoVat workbook would refill it from the sheet.

Put's manual non-NULL edits (e.g., วิภา=หลุย, set to a different value than the
sheet) are already protected by the "fill only if NULL" guard. The bug surface
is narrow but real: **any field intentionally cleared to NULL gets repopulated
on every cashbook re-import.** With NoVat re-imports becoming routine, this
risks silently undoing manual edits.

## Goal

Make cashbook re-import incapable of modifying any field on an **existing**
employee. New-employee auto-create stays. When the sheet disagrees with the
DB, emit a diff warning into the import result so Put can act in HR UI if
desired.

## Non-Goals

- **No new schema.** No migration, no override table, no flag column.
- **No HR UI changes.** No lock indicators, no per-field lock affordance.
- **No new routes.** No `/unlock`, no admin gate work.
- **No change to new-employee creation.** First-touch creates with EMP-code
  exactly as today.
- **No change to `salary_advances`, `employee_salary_history`, or the
  nickname-map used to attribute advances.** Those flows are correct.
- **No selective-write "unlock" affordance.** If Put ever needs to copy a
  sheet value into an existing employee row, he edits the field in HR UI.
  The salary sheet is no longer authoritative for any existing employee.

## Design — Approach 1 ("existence-is-the-lock")

The simplest possible rule: if `sync_salary_sheet()` matches an existing
employee, never UPDATE — only diff and warn.

### Behavioural change

In `inventory_app/import_cashbook.py::sync_salary_sheet()`, replace the
"fill if NULL" block (lines 282-316) with a diff-only block:

```python
if emp_row is not None:
    eid      = emp_row[0]
    emp_code = emp_row[1]
    # Existing employee — never modify. Diff the 3 previously-fillable
    # fields and surface mismatches as warnings. bank_account_no is
    # masked (PII): emit field name only, never raw values.
    for idx, field, sheet_val, sensitive in (
        (2, "nickname",        nickname,  False),
        (3, "bank_name",       bank,      False),
        (4, "bank_account_no", bank_acct, True),
    ):
        db_val = emp_row[idx]
        # Treat None and "" as equivalent (both = blank).
        sv = sheet_val or None
        dv = db_val or None
        if sv != dv:
            if sensitive:
                result["warnings"].append(
                    f"DIFF {emp_code} {field}: sheet differs from DB "
                    f"(skipped — edit in HR UI)"
                )
            else:
                result["warnings"].append(
                    f"DIFF {emp_code} {field}: sheet={sheet_val!r} "
                    f"db={db_val!r} (skipped — edit in HR UI to change)"
                )
    result["skipped"].append(emp_code)
    continue
```

**PII handling note.** `bank_account_no` is the only sensitive field of the
three. Its warning carries only the field name and a "differs" marker —
never the raw account number from either source. `nickname` and `bank_name`
keep raw values in warnings because they are low-sensitivity and the raw
diff is the only thing that makes the warning actionable.

The new-employee branch (`import_cashbook.py:318` onward) is unchanged.
`_build_nickname_map()` and the `salary_advances` full-replace step
(`import_cashbook.py:525-571`) are unchanged in code, but see "Knock-on
effect on salary_advances matching" below.

### Docstring update (in-scope)

`sync_salary_sheet`'s docstring at `import_cashbook.py:236-242` claims
`updated — list of emp_codes updated (nickname / bank filled)`. After this
change, the existing-employee branch never appends to `result["updated"]`,
so the list is permanently `[]` for that branch. The key itself stays
(callers may do `.get("updated", [])`); update the docstring text to read
`updated — list of emp_codes updated (always empty for existing employees
under no-clobber rule; reserved for future use)`. No caller code changes —
the dict shape is preserved.

### Knock-on effect on salary_advances matching

`import_cashbook.py:537-549` calls `_build_nickname_map(conn)` **after**
`sync_salary_sheet` runs, then matches each advance's `raw_name` against
that map. Today, when sync fills a NULL `employee.nickname` from the sheet,
the advance lookup that follows can match it. Under Approach 1, sync no
longer fills, so an advance whose `raw_name` would have matched a
freshly-filled nickname becomes unmatched (`employee_id=None`,
`adv_unmatched += 1`).

This affects the motivating case directly: any advance with `raw_name='กี'`
will not attach to กิติยา's row until her `employee.nickname` is populated
in HR UI. The advance row still inserts (with `employee_id=NULL` and the
raw_name preserved) — no data is lost, just unattributed.

**Operator step (one-time, post-deploy):** for each employee whose nickname
appears in the salary sheet but is NULL in DB, set the nickname via the HR
UI before the next cashbook re-import. After that one fixup, the advance
match is stable across re-imports. The DIFF warning emitted by the new
sync surfaces exactly which (employee, nickname) pairs need this treatment.

This is the same kind of one-time operator setup the rejected Approach 2
required (re-clear-then-save to bootstrap a lock). Approach 1 trades
"set up override locks" for "populate missing nicknames" — both are
one-time, ~5 employees of friction.

### Why this fixes the motivating case

- **กิติยา (DB nickname NULL, sheet has value):** before → refill on every
  import. After → DB unchanged, warning emitted (`DIFF EMP00X nickname:
  sheet='กี' db=None`). Reading the warning is opt-in.
- **วิภา=หลุย (DB nickname 'หลุย', sheet has 'วิภา'):** before → silent skip
  (non-NULL guard blocked the write). After → still no write, but a diff
  warning is now emitted (`db='หลุย' sheet='วิภา'`). This is a behavioural
  change from "silent skip" to "skip with warning." Acceptable — the warning
  is informational only, never causes a write.

### Result-summary impact

`result["warnings"]` already flows into the cashbook import response and is
displayed in the existing UI alongside other import warnings. No template
work needed. Diff warnings will appear interleaved with the existing
"New employee EMP00X 'X' created from Salary_Sheet; start_date unknown"
warnings — that's fine.

If diff volume becomes noisy later (it shouldn't with 5 employees), a
follow-up could group them under a separate result key. Not in scope here.

## Testing

New file `tests/test_hr_sync_no_clobber.py`. Seven cases — one
new-employee regression guard, four behaviour assertions, one PII
assertion, one salary-advance knock-on regression guard:

1. **`test_existing_employee_with_non_null_nickname_not_modified`** — Seed
   employee with `nickname='หลุย'`. Run `sync_salary_sheet` with a row whose
   nickname is `'วิภา'`. Assert: DB nickname unchanged; warning contains
   `DIFF`, `nickname`, `หลุย`, `วิภา`; `emp_code` in `result["skipped"]`.
2. **`test_existing_employee_with_null_nickname_not_refilled`** — กิติยา
   case. Seed employee with `nickname=NULL`, sheet row has `nickname='กี'`.
   Assert: DB nickname still NULL; diff warning emitted; `skipped` not
   `updated`.
3. **`test_existing_employee_matching_sheet_silent_skip`** — Seed employee
   with all 3 fields matching the sheet exactly. Assert: no diff warning;
   `result["skipped"]` contains emp_code.
4. **`test_new_employee_still_auto_created`** — Regression guard. Sheet has
   a name no DB employee matches. Assert: new row inserted with auto-
   generated EMP-code; `result["created"]` includes it; salary_history seed
   row exists.
5. **`test_re_import_idempotent_after_manual_clear`** — End-to-end. Run
   import once (creates EMP). Direct `UPDATE employees SET nickname=NULL
   WHERE emp_code=?` to simulate Put clearing via HR UI. Re-run import on
   same workbook. Assert: DB nickname still NULL after second run; diff
   warning emitted on second run.
6. **`test_bank_account_no_diff_does_not_leak_raw_values`** — PII
   regression. Seed employee with `bank_account_no='1234567890'`, AND
   seed `nickname` + `bank_name` to **match the sheet exactly** so they
   produce no warning. Sheet row carries
   `bank_account_no='9999999999'` (the only divergent field). Run
   `sync_salary_sheet`. Assert: exactly one warning emitted; that warning
   contains `bank_account_no`, `DIFF`, `emp_code`; that warning does NOT
   contain `'1234567890'` or `'9999999999'`; and (defensive) no warning
   string in the full result list contains either digit substring. This
   locks masking behaviour so a future refactor cannot regress to dumping
   raw account numbers into `result["warnings"]`. (Pre-matching nickname
   and bank_name ensures the test passes for the right reason — bank_acct
   diff isolated as the only candidate for leak.)
7. **`test_advance_unmatched_when_existing_nickname_null`** — Knock-on
   regression guard. Seed existing employee with `nickname=NULL`. Sheet
   has `nickname='X'` plus a salary advance with `raw_name='X'`. Run full
   `import_cashbook`. Assert: employee row unchanged (DB nickname still
   NULL); advance row inserted with `employee_id=NULL`; DIFF warning for
   the nickname mismatch is emitted. This documents the trade — the
   advance is preserved, just unattributed until Put fills the nickname
   in HR UI.

Tests must use an isolated SQLite fixture (existing `conftest.py` pattern in
`tests/`). No tests against `instance/inventory.db`.

## Rollout

1. Branch `fix/hr-sync-no-clobber` off `main`.
2. Single PR containing the code change + 7 tests + docstring fix on
   `sync_salary_sheet` (`import_cashbook.py:236-242`).
3. Pre-merge gates: full `pytest` green, `codex:rescue` adversarial pass,
   `/scrutinize` review.
4. Squash-merge to `main`; Railway auto-deploys via existing flow.
5. Post-deploy verification (production):
   - Open `/cashbook/import`, re-upload the last NoVat workbook
     (`Document/Boonsawat/...NoVat.xlsx`).
   - Confirm the import result shows DIFF warnings for any mismatches
     (expected: at least the กิติยา nickname row).
   - Confirm no employee row's `updated_at` advances as a result of the
     import. (`SELECT emp_code, updated_at FROM employees` before/after.)

## Risk + rollback

- **Risk:** none material. The change strictly removes UPDATE statements
  from the import path; cannot cause data loss. Worst case: warnings are
  noisy on first re-import. Rollback = revert the PR; behaviour returns to
  the original "fill if NULL."
- **Migration risk:** none — no schema change.
- **Operational risk:** Put must read the diff warnings in the import
  result if he wants to know about sheet-vs-DB mismatches. The flow is
  unchanged otherwise.
- **Pre-existing matching edge case (not introduced here, but worth
  flagging):** `sync_salary_sheet` matches existing employees by
  `full_name`, falling back to `WHERE nickname=?` against the sheet's
  nickname. If `full_name` doesn't match (typo, slight rename) AND the DB
  employee's `nickname` is NULL, both lookups fail and the row falls
  through to the new-employee INSERT path — creating a duplicate
  employee. Approach 1 doesn't fix this and doesn't make it worse. Worth
  knowing about when watching the post-deploy verification step.
- **Salary-advance side effect (see "Knock-on effect" above):** the same
  one-time operator step (populate missing nicknames in HR UI) needed to
  silence DIFF warnings is also what keeps advances attributed. Documented
  in the design above; test #7 locks the behaviour.

## Rejected alternatives (recorded for the next reviewer)

- **Approach 2 — per-field override table + UI auto-lock + bootstrap mig.**
  Originally chosen, then rejected during `/scrutinize`. Verdict: schema +
  UI work disproportionate to the problem surface (1 row, 1 field, 5-person
  fleet); the "selective unlock then re-import" workflow has no concrete
  use case in Put's workflow; and the design as drafted failed to lock the
  motivating กิติยา case (the bootstrap mig couldn't lock a NULL field, and
  the auto-lock-on-edit rule required a diff that "edit-and-save-empty"
  doesn't produce). If granular unlock becomes a real need later, the
  override table is purely additive — can be added then.
- **Approach 3 — diff-only audit table.** Strictly worse than Approach 1:
  the audit row is stored instead of warned, requiring a reader. The 3rd-
  party-audit utility is unmotivated for the 5-person fleet.

## References

- Implementation site: `inventory_app/import_cashbook.py:282-316`
- Cashbook module memory: [[project_2026_05_18_cashbook_module]]
- HR module memory: [[project_2026_05_18_hr_module]]
- Resume point: [[project_2026_05_20_resume_here]] item #2
