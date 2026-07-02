# ADR 0007 — Cashbook dashboard is a month-scoped view; the 3rd card changes meaning by mode

Status: Accepted · 2026-07-02

## Context

`/cashbook` showed only **all-time cumulative** totals (every txn since the ledger began). The
headline cards (รายรับรวม / รายจ่ายรวม / คงเหลือ) summed the whole history, so the page read as a
lifetime summary, not a monthly tracker. The only monthly lenses were the "แนวโน้มรายเดือน" chart
and "สรุปรายเดือน" table at the bottom (click-to-drill), plus a month filter on the per-account
ledger. Put wanted monthly **expense awareness** as the primary lens — "เดือนนี้บวมกว่าเดือนก่อน
ผิดปกติไหม, จ่ายเกินรับหรือเปล่า" — not budget-vs-actual, not an accountant close.

## Decision

A single **month picker scopes the whole page**. Selecting a month filters the headline cards, the
per-account table, the category/tag summaries and the expense doughnut to that calendar month; an
**"ทั้งหมด"** option restores today's all-time behavior. The default on load is the **most recent
month that has data** (not `strftime('now')` — manual entry lags, so a strict current-month default
would render a misleading ฿0 page).

- **The 3rd headline card changes meaning by mode, and its label changes with it.** All-time mode:
  **คงเหลือ** = true cash on hand (income + transfers − expense, unchanged). Month mode:
  **สุทธิเดือนนี้** = operating `income − expense` for the month (a P&L net, NOT a cash balance).
  It is computed as `total_income − total_expense`, never `sum(account.balance)` — the latter folds
  in `เงินทุน/เงินโอน` transfers this figure must exclude.
- **Overspend is flagged per-category vs the previous month.** A category is flagged when this
  month ≥ prev × 1.20 **AND** (this − prev) ≥ ฿1,000 (both — the ฿1,000 floor kills noise on small
  lumpy categories). A category absent last month is labelled "ใหม่", not flagged. A "▲N หมวดบวม"
  roll-up sits on the รายจ่ายรวม card.
- **The current, incomplete month compares month-to-date vs the previous month's same day-range**
  (day 1..D, D = today's day-of-month, D clamped to the prev month's length), tagged "เดือนยังไม่จบ".
  A fully-past month compares full-vs-full.
- **The trend chart never scopes.** "แนวโน้มรายเดือน" stays all-months (it *is* the all-months
  story) and doubles as the month navigator: clicking a bar sets the page's month; the selected
  month's bars are highlighted. The "สรุปรายเดือน" table likewise cannot scope — its month rows
  navigate `?month=` (consistent with the chart) rather than opening the old drill modal.

## Alternatives rejected

- **A compact "this-month-vs-last" strip on top of the unchanged all-time dashboard** — simpler and
  lower-risk, but Put wanted the full scope (see the whole page as one month).
- **Card 3 = running end-of-month cash balance** — heavier query and answers a different question
  than "did I bleed cash this month".
- **3-month-average overspend baseline** — more robust for lumpy hardware purchases, but Put chose
  the simpler previous-month baseline. Mitigated by the ฿1,000 floor + MTD; thresholds are named
  constants (`_OVERSPEND_PCT_THRESHOLD`, `_OVERSPEND_DIFF_FLOOR`) to tune after living with it.

## Consequences

- A future reader will wonder why one card silently changes meaning and why the trend chart alone
  ignores the month filter — this ADR is that answer. The label swap (คงเหลือ ↔ สุทธิเดือนนี้) is
  the guard against misreading it.
- The prev-month baseline can produce noisy flags in months following a big stock-purchase month;
  the ฿1,000 floor + MTD reduce but don't eliminate this. Revisit the thresholds after a month.
- The per-account table's "คงเหลือ" column becomes net-change-in-month in month mode (not a running
  balance) — consistent with card 3.
- Implementation trap (see `blueprints/cashbook.py`): `_get_accounts_with_totals` is a LEFT JOIN, so
  the month filter lives **inside each `CASE WHEN`** (and `COUNT(CASE WHEN …)`), never the `WHERE` —
  a `WHERE` filter would drop accounts idle that month. The two inner-join helpers use a plain
  `WHERE`. Read-only change; no migration.
