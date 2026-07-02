# ADR 0006 — Salary posts to the cashbook as a pay-event (per employee), not on finalize

Status: Accepted · 2026-07-01

## Context

HR *computes* payroll (`payroll_runs` / `payroll_items`, draft → finalized → reopen) but
nothing posted it to the cashbook. Salary appeared there only because Put hand-typed monthly
`เงินเดือน` expenses into a Google Sheet (41 rows, ~฿666k), unlinked to HR — double entry.

Put wants salary to flow from HR into the cashbook, entered once, and needs each employee's
**bank name + account number + name** on one screen to make the transfers.

An earlier draft of this ADR posted on **finalize** with **one pay-from account per run**.
Scrutiny killed both halves:
- Finalize is a *numbers-locked* event, **not a cash event** — the transfer happens later.
  Posting on finalize records cash that hasn't moved and forces a withdraw-on-reopen dance;
  `finalize_run` (`hr.py:650`) is also a **no-op when already finalized**, so "re-finalize
  re-posts idempotently" was never true.
- History pays a single month's salary from **two accounts** — `392` (staff, ~฿169k) and
  `ชฎามาศ` (~฿496k) — because **Put pays some employees and his mother (a shareholder) pays
  others**. One account per run would mis-state balances by ~half the payroll.

## Decision

Model salary in the cashbook as a **pay-event**: the cashbook records the *actual transfer*,
not the calculation. Posting is decoupled from finalize and tracked **per employee**.

- **Finalize** is unchanged (locks numbers, stamps consumed advances). It does **not** touch
  the cashbook.
- **The payroll detail page is the transfer checklist.** Each employee row shows name +
  `bank_name` + `bank_account_no` + `net_pay`, a paid/unpaid state, and its **default
  pay-from account** (new `employees.default_cashbook_account_id`, seeded from history:
  Put's people → 392, mother's → ชฎามาศ; overridable per row).
- **Mark paid (per employee, "จ่ายแล้ว")** posts one cashbook `เงินเดือน` expense:
  `amount = net_pay`, `account_id` = that row's pay-from account, `txn_date` = the pay date
  entered (default today), `user_category` = nickname, `created_by` = the payer, stamped with
  `payroll_run_id` + `payroll_item_id`. **Skip when `net_pay <= 0`** (nothing transferred).
- **Un-post (per employee, "ยกเลิกการจ่าย")** deletes that item's cashbook row → unpaid.
- **Paid-state is derived** from the existence of the linked cashbook row (single source of
  truth — no denormalized `paid` flag to drift). Which account / date / payer all read off
  that row.
- **Reopen a run is blocked while any item is paid** — un-post first, then reopen → fix →
  re-finalize → re-pay. You never edit a salary figure while its cash is posted.
- **Paid rows are read-only in the cashbook** (they carry `payroll_item_id`): the only way
  to remove one is un-post on the payroll page.
- **Who**: admin, manager and shareholder (anyone with cashbook write) may mark-paid /
  un-post; **any** of them may mark **any** employee (no per-payer row lock — the default
  account routes each row correctly; trust-based, family shop). This is *why* shareholder
  gains cashbook write (ADR 0005) — the mother records the salaries she pays.
- **Historical dup guard**: marking paid warns if manual (unlinked) `เงินเดือน` rows already
  exist for that month, so the 41 hand-typed rows aren't silently duplicated.

## Considered options

- **Post on finalize, one account per run** (earlier draft) — rejected: mis-models a cash
  ledger, contradicts the 392/ชฎามาศ split, and needs the reopen-withdraw workaround.
- **Post on finalize, per-employee account** — fixes the split but still records cash before
  it moves and keeps the reopen-withdraw fragility.
- **Whole-run single "mark paid" click** — rejected: two people pay independent subsets, so a
  single click can't attribute who paid what, when.
- **Backfill Apr/May/June + delete the 41 manual rows** — rejected: those runs record no
  pay-from account, so backfilled rows would be mis-attributed. Only new pay-events post.

## Consequences

- **Cash-basis correctness**: the cashbook shows salary when it's actually transferred, by
  the real account and date, attributed to the real payer.
- **Advances still aren't posted to the cashbook** but reduce `net_pay`, so posting only net
  leaves the earlier advance cash-out unrecorded (account balance overstated by outstanding
  advances). Deliberately deferred as a separate follow-up.
- **SSO remittance / employer SSO not posted** — this records take-home actually transferred,
  not full employer cost (a cash book, not a P&L).
- Employees with no bank details (EMP001, EMP008) show a "ยังไม่มีเลขบัญชี" placeholder on the
  checklist — a nudge to fill them in HR.
- **Pay-from dropdown must exclude `is_transfer` accounts** (e.g. 904): a transfer account is
  excluded from the cashbook P&L, so salary posted there would silently vanish from รายจ่าย.
- Schema: `cashbook_transactions` += `created_by`, `payroll_run_id`, `payroll_item_id`;
  `employees` += `default_cashbook_account_id`. No new `payroll_items` columns (paid-state is
  derived).
