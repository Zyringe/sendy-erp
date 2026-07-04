# ADR 0008 — One source of truth per salary-family money type (asymmetric)

Status: Accepted · 2026-07-04 · builds on ADR 0006

## Context

Salary, salary **advances**, and **commission** were each recorded in **two** places — their
computing system (payroll / commission engine / HR `salary_advances`) **and** hand-typed cashbook
rows — causing recurring double-entry and double-booking. ADR 0006 fixed salary (payroll pay-event
auto-posts a locked cashbook row) but explicitly **deferred advances**. Commission still double-enters.

Put wants each type entered **exactly once**, with `/cashbook/new` guiding entry to the right home.

## Decision

Each salary-family type has **one source of truth**, but the direction is **asymmetric** because the
natural point of entry differs:

- **Advance (`เงินเดือน (เบิกล่วงหน้า)`) — sourced in the cashbook.** An advance is a *cash event first*
  (money handed over mid-month), so it is entered on `/cashbook/new` and the save **writes back** to HR
  `salary_advances` (one commit). `/hr/advances` becomes a **read-only mirror** (add form removed). The
  cashbook row and its `salary_advances` record are linked 1:1; **edit/delete cascades to HR and is
  allowed until a payroll run deducts the advance** (`deducted_in_run_id` set), after which the row is
  **locked** (changing a deducted advance would corrupt a computed `net_pay`).
- **Salary (`เงินเดือน`) — sourced in HR payroll** (unchanged, ADR 0006). Manual `เงินเดือน` entry in the
  cashbook is **hard-blocked**; the category shows a redirect to the HR page.
- **Commission (`จ่ายค่าคอมมิชชั่น`) — sourced on `/commission`** for reps **in the commission engine**:
  recording a payout **auto-posts a locked cashbook row** (mirrors salary; deleting the payout removes
  it). Its pay-from account defaults to the **logged-in user's** `users.default_cashbook_account_id`.
  For **off-system** reps (not in `salespersons`) the cashbook stays the **manual** home (**hybrid**),
  because forcing ad-hoc reps into the engine pollutes it with "owed 0 / paid X" phantoms.

Implement as **one** "linked & locked cashbook row" concept (`payroll_item_id` / new
`commission_payout_id` / new `salary_advance_id`), generalizing `_reject_if_salary_row` — not three
parallel systems.

## Considered options

- **Passive info-modal only** (show HR/commission data, still hand-type) — rejected: informs but does
  not end double-entry.
- **Hard-block all three uniformly + register every rep** — rejected: strands off-system commission reps
  and pollutes the engine with phantom over-payments (real names `เจียรนัย/อัคเรศ/ทวีเกียรติ` are **not**
  in `salespersons`; zero overlap with coded reps).
- **Symmetric "always auto-post from the home system"** — rejected for advances: an advance is a cash
  event that originates at the cashbook, so cashbook-as-source is the honest model.

## Consequences

- **Kills the double-book**: salary/in-engine-commission can no longer be hand-entered in the cashbook,
  and advances stop being maintained in two places.
- **Bulk paste can no longer include salary or in-engine-commission lines** — those rows are **skipped
  with a summary**, not errored (the batch still saves the rest).
- **Commission identity is load-bearing**: the hybrid only works if Put recognizes in-engine reps in the
  picker. The `salespersons` labels (`ต๋อ /06`) must carry **real-name aliases** or the hybrid
  re-creates the double-book. Resolved as a hard gate before the commission phase.
- **Worker race**: advance write-back is one commit; edit/delete re-checks `deducted_in_run_id` at write
  time and aborts on 0 rows (Railway `gunicorn -w 2`; never reproduces on the single-process dev server).
- **Reporting**: two salary-family expense categories now hit the P&L; `เงินเดือน (เบิกล่วงหน้า)` is
  excluded from the overspend flag (advances are lumpy). `advance + net_pay = gross` (net_pay is already
  reduced by the advance) → no double-count.
- Schema: `cashbook_transactions` += `salary_advance_id`, `commission_payout_id`;
  `users` += `default_cashbook_account_id` (data-entry default; distinct from
  `employees.default_cashbook_account_id` pay-from). New category `เงินเดือน (เบิกล่วงหน้า)`.
