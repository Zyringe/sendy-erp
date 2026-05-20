# Extend HR Sync DIFF Warnings — `is_active` + `sso_enrolled`

**Date:** 2026-05-21
**Status:** Approved (issue #43 body serves as ratified scope; deferred from PR #42)
**Origin:** [GitHub issue #43](https://github.com/Zyringe/sendy-erp/issues/43) — surfaces the Codex finding from PR #42 that the post-no-clobber diff loop only covered 3 fields while the parser actually consumes 5 (the 3 + `sso_deduction` + `is_active`).

## Problem

PR #42 made `import_cashbook.sync_salary_sheet()` no-clobber for existing employees: it emits DIFF warnings for sheet/DB mismatches on `nickname`, `bank_name`, `bank_account_no`. The parser ALSO consumes `sso_deduction` and `is_active` from the salary sheet (`import_cashbook.py:277-278`), but the diff loop doesn't compare them. Result: a salary-sheet edit that flips someone's `is_active` to `0` or changes their SSO deduction leaves the HR row untouched AND emits no warning. Silent payroll-state drift.

## Goal

Extend the DIFF-warning loop in `sync_salary_sheet` to also compare derived `sso_enrolled` (from sheet's `sso_deduction > 0`) and `is_active` (sheet boolean → DB 0/1). Surface as DIFF warnings; never UPDATE. Same shape as the existing 3-field warnings.

## Non-Goals

- **No auto-update of `is_active` / `sso_enrolled`.** No-clobber rule still applies. The operator decides via HR UI.
- **No `salary` diff against `employee_salary_history`.** Separate concern; `employee_salary_history` is append-only with its own state machine. Out of scope for this PR.
- **No new fields beyond the 5.** Other employee columns (`diligence_allowance`, `probation_days`, `start_date`, `company_id`) are not consumed from the salary sheet at all.
- **No schema change.** No migration, no new tables.
- **No HR UI changes.** Operator workflow unchanged.

## Design

### Code change

In `inventory_app/import_cashbook.py::sync_salary_sheet()` (lines ~297-324, the existing-employee branch), append a second small diff loop AFTER the 3-field text loop:

```python
            # ── Int-valued field diffs (derived, no None-normalization) ─────
            # sso_enrolled is derived from sheet's sso_deduction (>0 → 1).
            # is_active is derived from sheet's is_active (truthy → 1).
            sheet_sso_enrolled = 1 if sso_ded > 0 else 0
            sheet_is_active    = 1 if is_active else 0
            for idx, field, sheet_val in (
                (6, "sso_enrolled", sheet_sso_enrolled),
                (7, "is_active",    sheet_is_active),
            ):
                db_val = emp_row[idx]
                if sheet_val != db_val:
                    result["warnings"].append(
                        f"DIFF {emp_code} {field}: sheet={sheet_val} "
                        f"db={db_val} (skipped — edit in HR UI to change)"
                    )
```

Indices 6 and 7 are already in the SELECT (`SELECT id, emp_code, nickname, bank_name, bank_account_no, diligence_allowance, sso_enrolled, is_active FROM employees`). No SELECT change needed.

### Docstring updates

Three sites to refresh — they currently claim the diff covers "3 previously-fillable fields":

1. `import_cashbook.py:34-40` (module-level "HR sync rules" comment) — bump "3 previously-fillable fields" to "3 previously-fillable fields + 2 derived state fields (sso_enrolled, is_active)".
2. `import_cashbook.py::sync_salary_sheet` docstring Returns block — same bump.
3. `inventory_app/import_cashbook.py:299` (inline comment on the diff loop) — "Diff the 3 previously-fillable fields…" becomes "Diff 5 fields (3 previously-fillable + 2 derived state)".

### Spec link-back

After this PR ships, update the older spec `docs/superpowers/specs/2026-05-21-hr-sync-no-clobber-design.md` to point to this design as the follow-up that closed the deferred drift surfacing. Add a one-line "Update 2026-05-21" footer.

## Testing

Three new tests appended to `tests/test_hr_sync_no_clobber.py`:

1. **`test_sheet_sso_deduction_diff_emits_warning`** — Seed employee with `sso_enrolled=1`, sheet row with `sso_deduction=0.0`. Run sync. Assert: DB `sso_enrolled` still 1; DIFF warning emitted containing `EMP00X`, `sso_enrolled`, `sheet=0`, `db=1`; `EMP00X` in `result["skipped"]`.

2. **`test_sheet_is_active_diff_emits_warning`** — Seed employee with `is_active=1`, sheet row with `is_active=False`. Run sync. Assert: DB `is_active` still 1; DIFF warning emitted with `is_active`, `sheet=0`, `db=1`; `skipped`.

3. **`test_sso_and_active_match_no_warning`** — Seed all 5 fields matching the sheet exactly (nickname, bank, bank_acct, sso_enrolled, is_active). Run sync. Assert: zero DIFF warnings; `EMP00X` in `skipped`.

Tests use the existing `_seed_employee` helper but with extra kwargs for `sso_enrolled` and `is_active`. Helper to be extended (one-line addition each).

## Rollout

1. Branch `fix/hr-sync-drift-surfacing` off `origin/main` (already done; PR #42 merged at `6b024e6`).
2. Single PR: code change + docstring updates + 3 new tests.
3. Pre-merge gates: full `pytest -x -ra` green, Codex adversarial pass.
4. Squash-merge → Railway auto-deploys.
5. Post-deploy validation: re-import last NoVat workbook; expect new DIFF warnings for any employees whose sheet `is_active` / SSO deduction diverges from DB.

## Risk + rollback

- **Risk:** none material. Adds new informational warnings; never writes. Worst case: too many warnings on first re-import surfaces real drift Put didn't know about (which is the point). Rollback = revert the PR.
- **Migration risk:** none — no schema change.
- **Operator risk:** Put may see a new wave of DIFF warnings for `sso_enrolled` and `is_active` mismatches that have been silently piling up. Investigate each. Use HR UI to fix as needed.

## Acceptance criteria

- [ ] `sync_salary_sheet` emits DIFF warnings for `sso_enrolled` and `is_active` when sheet/DB disagree.
- [ ] `result["skipped"]` still contains the emp_code (no UPDATE happens).
- [ ] 3 new tests pass; full suite still green.
- [ ] Codex adversarial review pass: no findings re "missing field coverage".
- [ ] Closes issue #43.
