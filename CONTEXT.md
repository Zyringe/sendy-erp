# Sendy — Domain Glossary

> Ubiquitous language for the Sendy ERP. Glossary only — no implementation details.
> Add a term the moment a conversation reveals it was ambiguous.

## Conversions (the `/conversions` feature — "แปลงสินค้า")

- **สูตรแปลงสินค้า (conversion formula)** — the umbrella concept: a rule that consumes
  one or more input products from stock and produces an output product into stock.
  Stored in `conversion_formulas` (+ `conversion_formula_inputs`). Running one is a
  stock movement, not a sale. The `/conversions` page lists these formulas.

- **แปลง (run a conversion)** — *executing* an existing formula once (× a multiplier):
  decrement the inputs, increment the output. Distinct from *creating* a formula.

- **สร้างสูตรแปลง แพ็ค-ตัวหลวม (pack↔loose pairing)** — the **only** way to create or
  edit a conversion formula. Picks one pack product (e.g. unit แผง) + one loose product
  (e.g. unit ตัว) + a ratio, and creates a **reciprocal pair** of formulas in one step
  (แกะ: แผง→ตัว, and แพ็ค: ตัว→แผง). Backed by `upsert_pack_unpack_pair`; idempotent, so
  re-saving the same pack+loose with a new ratio **edits** the existing pair (no
  duplicate). Was previously labelled "จับคู่แพ็ค-ตัวหลวม". Editing a formula reopens
  this same screen, prefilled.

- **สูตรขั้นสูง (advanced formula) — REMOVED.** Formerly a general N-inputs → 1-output
  builder for manufacturing/re-packing. Removed because it overlapped with the pairing
  tool and was never used (0 of the formulas in use). See `docs/adr/0001`.

- **ทิศเดียว (one-way / partnerless)** — a pack/unpack formula whose reciprocal half is
  missing. Flagged for review because a pair is normally created together.

- **pack vs loose** — "pack" = a product sold as a multi-unit bundle (units like
  แผง/แพ็ค/ชุด/กล่อง/โหล); "loose" = the single-piece form (ตัว/อัน/ชิ้น/ใบ/ดอก).
  The boundary is **not** derivable from `unit_type` alone — the unit vocabulary is
  inconsistent, so the two are related explicitly via a pack↔loose pairing, never
  inferred by filtering on unit.

## Accounts & people (the `/users` page + HR)

- **User account (บัญชีผู้ใช้ / login)** — a credential to sign in to Sendy: `username`,
  password, `role`, `display_name`, `is_active`. Lives in `users`. NOT a person — it is
  a way to log in. May exist with no person attached (e.g. test/system accounts).

- **Employee (พนักงาน)** — an HR record of a real person (name, salary, bank, position…).
  Lives in `employees`. A person on the team. May exist with **no** login (e.g. บอล, ริน
  — staff who never use the system).

- **The link (employee ↔ account)** — `employees.user_id → users.id`, **1:1 and optional
  on both sides**: an employee has at most one login; a login belongs to at most one
  employee; either can exist without the other. The account is normally created first and
  the employee linked **later** (the person may not have an HR record yet). Edited in
  **exactly one place: `/users`** (the HR employee page shows it read-only). The picker
  offers only employees/users not already linked elsewhere (+ the current one when editing).

- **role (บทบาท) vs position (ตำแหน่ง)** — two different things, do not conflate.
  `role` = the Sendy *permission level* (one of the five below). `position` (on the
  employee record, e.g. กรรมการผู้จัดการ, เสมียน) = the person's *HR job title*. A
  shareholder-role login and a "ผู้ถือหุ้น" position are unrelated by mechanism.

- **The five roles** — the `role` enum, in descending privilege. Source of truth = the
  POST whitelists + GET gating + `_MODULE_DEFS` roles in `app.py`; the `/users`
  role-permission summary is the human-readable mirror and must be kept in sync.
  - **ผู้ดูแลระบบ (admin)** — full access: manages users, edits products/master data,
    sees cost/GP, every module. Only role that reaches the "ระบบ" admin module (`/users`).
  - **ผู้จัดการ (manager)** — sees cost/GP + payment status, approves leave/advances,
    edits product names/packaging, enters HR + Cashbook. Cannot manage users.
  - **พนักงานออฟฟิศ (staff)** — desktop back-office: imports every file type + stock/sales
    views, stock-adjust, mapping. Does **not** see cost/GP; blocked from HR + Cashbook.
  - **ผู้ถือหุ้น (shareholder)** — reads *everything* (incl. cost/GP, HR, Cashbook) and may
    **add/edit/delete Cashbook transactions** + **mark payroll salaries paid** (which posts
    those cashbook rows — how Put's mother records the salaries she pays); otherwise the only
    POST it makes is logout. It still cannot change payroll *numbers* (generate/edit/finalize)
    or edit anything else.
  - **พนักงานทั่วไป (general)** — mobile PWA kiosk only: ค้นหาสต็อก + own leave + own
    payslip. Desktop sidebar is empty; every other endpoint redirects to stock search.

- **Impersonate a user (จำลอง / "ดูในมุมมองของ…")** — an admin temporarily *becomes a
  specific other user*: the session's `user_id` + `role` + `display_name` are swapped to
  the target so identity-keyed pages (`/me/*`) show **that person's** data, and the admin
  can act **as** them (writes are attributed to the target). The real identity is stashed
  in `_real_*` and restored on exit; "exit impersonation" is always reachable regardless
  of the impersonated role. This is **impersonating a user**, NOT the older "simulate a
  role" (which swapped only the permission level and kept the admin's own `user_id`, so
  `/me/*` still showed the admin's data). Only a (real) admin can start it; entering is
  audit-logged under the real admin.

- **identity-keyed vs role-gated pages** — `/me/leave` + `/me/payslip` resolve their data
  from `session['user_id']` (via `_my_employee()`), so *who* you are changes what they
  show. Every other page is role-gated only: same global data regardless of user. This is
  why impersonation must swap `user_id`, not just `role`.

## Cashbook (the `/cashbook` feature — บัญชีรับ-จ่าย)

- **Cashbook (บัญชีรับ-จ่าย)** — the multi-account operating cash ledger: money in/out of
  the family's bank accounts + wallets. **Separate from the BSN VAT books**
  (`sales_transactions` / `purchase_transactions`) — a different set of money. Lives in
  `cashbook_transactions` over `cashbook_accounts`.

- **Cashbook account (บัญชี)** — one bank/wallet the cash flows through (`392`, `LEX`,
  `SPX`, `ชฎามาศ`, `กิติยา`, `904`). `cashbook_accounts`. NOT a User account (login) and
  NOT an Employee — a third, unrelated "account" sense. Accounts are entered/edited out of
  band (no in-app add-account screen yet).

- **Cashbook transaction (รายการรับ-จ่าย)** — one **income (รายรับ)** or **expense
  (รายจ่าย)** line against a cashbook account: date, category, ผู้ใช้ tag, amount,
  description, note. Entered **by hand** — the Excel importer + round-trip export are
  retired (ADR 0005).

- **Category (หมวดหมู่)** — the accounting bucket of a transaction (`เงินเดือน`, `ค่าไฟ`,
  `ซื้อสินค้า`, …). Lives in `cashbook_categories`, scoped by direction. A new one can be
  typed on the entry form (created on save).
  _Not to be confused with_: ผู้ใช้ tag.

- **ผู้ใช้ tag (`user_category`)** — a free-text "**who / where** this money was for" label
  (e.g. `บ่าว`, `โกดัง Lion`, `ออฟฟิสสุนทร`). A *different axis* from category, and NOT the
  person who keyed the row (that is `created_by`). For salary rows it holds the employee's
  nickname.
  _Avoid_: calling this a "user" — it has nothing to do with a login.

- **Transfer account / transfer category** — capital / inter-account movements
  (`cashbook_accounts.is_transfer=1`, e.g. `904`, and the `เงินทุน/เงินโอน` category). Real
  cash (so they count toward an account **balance**) but excluded from the headline **P&L**
  (รายรับ/รายจ่าย), the category summary and the monthly chart.

- **Salary posting (pay-event)** — salary reaches the cashbook when a transfer is actually
  **marked paid**, per employee, on the payroll detail page — NOT when the run is finalized
  (finalize only locks the numbers). "จ่ายแล้ว" posts one `เงินเดือน` **expense**
  (amount = `net_pay`, skipped if `net_pay <= 0`) into that row's pay-from account, dated the
  real pay date, stamped `payroll_run_id` + `payroll_item_id`. "ยกเลิกการจ่าย" deletes it.
  **Paid-state is derived from the linked cashbook row** (no separate flag). Paid rows are
  **read-only in the cashbook**; a run can't be reopened while any item is paid. Two people
  pay independent subsets (Put + his mother), so it is per-employee, not one click. See
  ADR 0006.

- **Pay-from account** — the cashbook account a given salary transfer is paid *out of*.
  Chosen per employee at mark-paid time, defaulting to that employee's **default pay-from
  account** (`employees.default_cashbook_account_id`, e.g. Put's staff → `392`, his mother's
  → `ชฎามาศ`). Transfer accounts (`is_transfer=1`) are not eligible (they're excluded from
  the P&L).
