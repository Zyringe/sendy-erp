# Production salary-advance and April-payroll reconciliation (2026-08-05)

**Immutable evidence, not tooling.** These are the exact artifacts from a production
data-ops session, preserved for audit and review. Every file carries a `.py.txt`
extension so it is not presented as ordinary runnable tooling and is less likely to be
executed accidentally. Python can still execute the text if passed explicitly
(`python foo.py.txt`) — do not rename or run these files.

Nothing here is a migration. Nothing here should ever run again.

## Layout

```
applied/              — the three scripts that actually mutated production
break-tests-unsafe/   — deliberately-broken variants used to prove the checks bite
```

### `applied/`

| File | What it did on prod |
|---|---|
| `fix_luy_advances.py.txt` | หลุย: relabelled cashbook row 638 to `เงินเดือน (เบิกล่วงหน้า)` + linked it to `salary_advances` 27; created the Jul/Aug ฿3,100 ค่าอาหาร advance pairs (adv 35/ct 732, adv 36/ct 733) |
| `fix_ball_1000.py.txt` | บอล: created `salary_advances` 37 and linked it to the **existing** cashbook row 634 — no new cash row, which would have double-counted June expense |
| `fix_april_ball_option_c.py.txt` | บอล: patched `payroll_items` id 9 (run 4) to the 13 days actually paid — base/gross 6,066.67 → 5,633.33, net 2,166.67 → 1,733.33 |

### `break-tests-unsafe/`

⚠️ **These are destructive by design.** They were run only against throwaway snapshots
on the prod container's `/tmp`, to prove the verification checks could go red.

| File | What it deliberately breaks |
|---|---|
| `fix_ball_1000_tampered.py.txt` | also inserts a duplicate ฿1,000 cash row → the two double-count guards must FAIL |
| `fix_april_ball_option_c_tampered.py.txt` | also changes `employees.start_date` → the employees/leave guards must FAIL |

## Known limitations of these artifacts

Recorded honestly because they shaped how safe the session actually was. Fix these
patterns in any **future** one-off; they cannot be retrofitted here.

1. **`rehearse` was a label, not a mode.** All three applied scripts take
   `<rehearse|live>`, but the argument only affects printed output — both paths run the
   same `BEGIN IMMEDIATE`, mutations and `commit()`. Safety came entirely from passing a
   snapshot path, never from the flag. A future one-off should require an explicit
   snapshot path and refuse known production paths without a separate confirmation.
2. **Invariants ran after `commit()`.** Each script commits, reopens a fresh connection,
   then verifies. A failed check exits non-zero but leaves the mutation in place — the
   scripts could report the damage, not prevent it. What actually protected production
   was rehearsing on a snapshot of the same state first. A future one-off should assert
   inside the transaction and roll back on failure, then re-read after commit as
   independent confirmation.
3. Their only real protection against a re-run is the pre-state guard at the top of each
   file, which aborts once the mutation has landed. That is incidental, not a design.

## Audit-trail coverage (checked against the LIVE trigger, 2026-08-05)

`audit_payroll_items_update` on prod tracks 13 fields — including `note`,
`diligence_forfeit_reason`, `other_additions_note`, `other_deductions_note` — so the
April change was recorded with its `gross`, `net_pay` **and** the explanatory `note`
(`audit_log` id 527368).

⚠️ **Read `sqlite_master`, not the migration file.** `071_audit_hr_payroll_triggers.sql`
lists only 9 fields and is stale — `073_audit_hr_trigger_gaps.sql` rebuilt this trigger.
A review that quoted 071 concluded the note was unaudited; it is not.

### Known audit gap: `base_amount`

`base_amount` is genuinely **not** covered by the trigger, and this archive does not fix it.

Reachability, verified: **no *application* path performs an in-place `UPDATE` of
`base_amount`** — `generate_run()` replaces rows via DELETE+INSERT (whose triggers carry
the full payload, migration 074), and `update_payroll_item()` writes bonus / additions /
deductions / diligence plus the derived gross and net. That leaves **direct SQL**, which
is a real administrative path and is exactly the path used here — so this is a live gap,
not a theoretical one. What makes it tolerable is that this particular change is recorded
in three other places (the audit row's gross/net/note, root commit `4f19fe3`, and the
analysis report), not that the gap is unreachable.

**Revisit when** either another in-place `base_amount` correction becomes necessary, or
Sendy gains a base-adjustment UI. Closing it then means a migration + trigger rebuild
(drop-first, both directions rehearsed on a `.backup`) + `schema.sql` regen.

## Supporting evidence

- Decision record: root-repository commit `4f19fe3` (`decisions/log.md`, entry `[2026-08-05]`)
- Full analysis: `Operations/05_analysis-reports/finance/advance-category-mismatch-review_2026-08-05.md`
  (gitignored business workspace)
- The superseded option-A scripts and the simulation that rejected the app-native
  reopen→regenerate path are deliberately excluded from this applied-path archive.

## Reviewing safely

Read them. Do not execute them, and do not rename them back to `.py`.

```bash
# they are text files — read, diff, grep
git diff -- scripts/oneoff/2026-08-05_salary-advance-payroll-reconciliation/
```

Any future data repair needs a fresh backup, a rehearsal on a snapshot, explicit
approval, and independent verification.
