# ADR 0017 — `is_transfer` carries one meaning only; account 904 is un-flagged

Status: Accepted · 2026-09-19 · builds on ADR 0014

## Context

`cashbook_accounts.is_transfer` is documented as marking a **conduit**: capital and inter-account
movements that are real cash but not operating activity, so they are excluded from the headline P&L,
the category summary and the monthly chart.

Account **904** carries the flag, and its own note says what it actually is: *"ประวัติก่อนเริ่มใช้สมุด
(ธ.ค. 68 ถึง มี.ค.)"* — the pre-cashbook history bucket. It is `is_active = 0`, and its rows run
2025-12-12 → 2026-03-09. So the flag was doing **two** jobs on that account: keeping genuine transfers
out, and keeping a historical era out.

The second job had a cost. Measured on prod (2026-09-18, snapshot max sale date 2026-09-17), 904 holds
**฿205,278.91 of expense in non-transfer categories** for 2025-12 → 2026-03, against ฿3,142,342.77 of
genuine `เงินทุน/เงินโอน` movements. None of that ฿205k reached the P&L.

**It is additional cost, not a duplicate of the live accounts.** In several categories 904 is the only
account carrying anything at all that month:

| month | category | 904 | live accounts |
|---|---|---:|---:|
| 2026-02 | ภาษี/ค่าปรับ | 12,000.00 | 0.00 |
| 2026-02 | ค่าทำบัญชี | 7,588.00 | 0.00 |
| 2026-03 | ค่าธรรมเนียม/ราชการ | 8,000.00 | 0.00 |
| 2026-01 | จ่ายค่าโบนัส | 167,500.00 | 0.00 |

⚠ A same-date/same-amount collision test was also run to look for duplicates and came back 0 — but its
**control also came back 0** (no cross-account amount pairs exist in Jan–Mar at all), so that test had
no power and is not evidence. The category table above is the evidence.

The decisive check was on the other side: **the category filter alone already excludes transfers
everywhere.** All five cashbook read paths filter the transfer *category* in the same WHERE clause as
the account flag (`blueprints/cashbook.py` `_get_monthly_summary`, `_get_category_summary`, the per-tag
summary, `_expense_by_category_range`, and the drill-down), as do `models/accounting.py` and
`models/financial_health.py`. So the flag's transfer-blocking job on 904 is redundant with the category,
and removing the flag cannot leak ฿3.14m of transfers into anything.

## This extends the 2026-09-15 ruling; it does not reverse it

Put had already decided this account once, four days earlier (`decisions/log.md` `[2026-09-15]`):

> **บัญชี 904 = ประวัติก่อนเริ่มใช้สมุด (ธ.ค. 68 ถึง มี.ค. 69) ตั้งใจให้อยู่นอกรายรับ-รายจ่าย → ปิดใช้งาน**
> … REASONING: *904: ย้อนแก้ ม.ค.-มี.ค. ใช้แรงเยอะได้น้อย (P&L จริงอยู่ที่สำนักงานบัญชี) และการปิดใช้งาน
> กันไม่ให้ใครคีย์ค่าใช้จ่ายลงบัญชีโอนแล้วหายจาก P&L*

That ruling was reached in a question-by-question grilling, so its attribution is strong. It is worth
reading closely, because it contains **both** halves of this ADR:

- **His protective mechanism was `is_active = 0`, and this ADR does not touch it.** 904 stays
  deactivated, so nobody can key a new expense into it. The guard he chose still works.
- **The harm he named is precisely what had already happened.** "กันไม่ให้ใครคีย์ค่าใช้จ่ายลงบัญชีโอน
  แล้วหายจาก P&L" describes ฿205,278.91 that had already vanished. He was protecting the future; the
  past was still missing.
- **The one part that has genuinely changed is a premise, not a preference.** He judged going back over
  Jan–Mar as low value *because* "P&L จริงอยู่ที่สำนักงานบัญชี" — the real P&L lives at the accounting
  firm. ADR 0014 ended that: `/accounting` now has a declared audience of its own (Put's monthly
  decisions) and the accounting firm is explicitly out of scope. The effort/value calculation was
  correct on its premise and the premise no longer holds.

⚠ **Process note, recorded deliberately.** When Q10 was put to Put on 2026-09-18 the 09-15 ruling was
not shown to him, because the main thread had not read `nami-cfo`'s finance memory or searched the
decision log for `904` first. He answered on incomplete information; the miss was surfaced on 09-19 with
the verbatim entry, and he then left the call open ("ปิด หรือ เปิดก็ได้"). **Search the decision log and
the owning agent's memory for a subject before putting a question about it to Put** — the log is
append-only precisely so an earlier ruling can be found.

## Decision

1. **`is_transfer` means exactly one thing: this account is a conduit, and its movements are not
   operating activity.** It is not a device for hiding an era, a closed account, or an inconvenient
   period.
2. **904 is un-flagged** (`is_transfer = 0`, staying `is_active = 0`). Its genuine transfers continue to
   be excluded by the `เงินทุน/เงินโอน` category, exactly as every other account's are.
3. A period that should not be read is handled by the **incomplete-month** mechanism (ADR 0014's
   companion reading), never by flagging an account.

Put chose this on 2026-09-18 over leaving Jan–Mar structurally understated, and on 2026-09-19 — after
being shown the 09-15 ruling he had not been given the first time — left the call open either way
("Q10 ปิด หรือ เปิดก็ได้"). It was taken as stated here, on the reconciliation above: the deactivation
he asked for stays, and only the redundant half of the flag comes off.

## Consequences

- **Eight surfaces move, and they were enumerated before the change, not after**: `/accounting` opex ·
  `financial_health` overhead · the cashbook monthly chart, category summary, per-tag summary,
  expense-by-category range and drill-down · and 904 relocates from the transfer-account list to the
  operating-account list on the accounts page.
- **No new payment path opens.** The guards that stop a transfer account being chosen as a pay-from
  account (`commission_bp.py`, `commission.py`, `hr.py`, `hr_queries.py`) also require `is_active = 1`,
  and 904 stays inactive — so it remains unusable as a payment target.
- **Net effect on the statement, after ADR 0014's prior-period line**: 2026-02 +฿29,678.91 and 2026-03
  +฿8,100 of operating expense. **2026-01 does not change** — its ฿167,500 is FY2568 staff bonuses
  (Put, 2026-09-19) and lands on the prior-period line, not in January's operating expense.
- ⚠ **904 also has income**, which nothing had considered: ฿8,102.05 รายได้อื่นๆ, ฿2,030.69
  ดอกเบี้ยเงินฝาก, ฿600 ยอดขายของ (plus ฿3,338,388.94 of transfer income the category still excludes).
  This does **not** touch `/accounting` revenue, which reads `sales_transactions`, but it does enter the
  `/cashbook` income figures. **The ฿600 "ยอดขายของ" must be checked against `sales_transactions`
  before anyone treats cashbook income as a revenue source** — if that sale is also invoiced, it is
  counted twice in the cashbook's own view.
- The flag's remaining legitimate user is the transfer-account concept itself. If a second account ever
  needs to be excluded for a reason that is *not* "it is a conduit", that is a signal a new concept is
  missing — not a reason to reach for this flag.
