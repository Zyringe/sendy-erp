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

- **Advance (cashbook-sourced) (เงินเดือน (เบิกล่วงหน้า))** — a salary advance is entered in the
  **cashbook** (`/cashbook/new`, category `เงินเดือน (เบิกล่วงหน้า)`), NOT in HR: the ผู้ใช้ cell
  becomes a required active-employee picker, and saving writes BOTH a `salary_advances` row and a
  linked `เงินเดือน (เบิกล่วงหน้า)` **expense** row in one commit (FK
  `cashbook_transactions.salary_advance_id`, UNIQUE — a "linked & locked row", same idea as the
  salary `payroll_item_id`). `/hr/advances` is a **read-only mirror**. An advance row is **not
  editable in the ledger** — correct it by delete + re-add; **delete cascades** to
  `salary_advances`, but only while **un-deducted** (`deducted_in_run_id IS NULL`), after which
  both rows are locked. Excluded from overspend flags (advances are lumpy). "ดูประวัติ" shows that
  employee's month advances + total outstanding + net salary (an over-advance guard). See ADR 0008
  + mig 128.

- **Pay-from account** — the cashbook account a given salary transfer is paid *out of*.
  Chosen per employee at mark-paid time, defaulting to that employee's **default pay-from
  account** (`employees.default_cashbook_account_id`, e.g. Put's staff → `392`, his mother's
  → `ชฎามาศ`). Transfer accounts (`is_transfer=1`) are not eligible (they're excluded from
  the P&L).

- **Data-entry default account** — the cashbook account `/cashbook/new` **pre-selects** for a **login
  user** (`users.default_cashbook_account_id`): Put→`392`, ชฎามาศ→`ชฎามาศ`, กิติยา (login `Siang`)→
  `กิติยา`. A *different sense* from **Pay-from account** (which is per-*employee*, for where a salary is
  paid out of): กิติยา's salary is paid *from* 392, but her data-entry default is *กิติยา*. Editable on
  `/users`; still changeable per row on the form. _Avoid_: conflating with pay-from account.

- **Single vs Bulk entry** — the two modes of `/cashbook/new`. **Single** (default) = one row + one date.
  **Bulk** (tickbox) = many rows sharing **one account**, each row carrying **its own date** (pre-filled
  from a top "default date", overridable). Un-ticking is blocked once 2+ rows exist.

  > The **salary-family "source of truth"** model (advances sourced in the cashbook with HR write-back;
  > salary & in-engine commission sourced from their home pages and auto-posting locked cashbook rows;
  > off-system commission staying manual) is **proposed but not yet built** — see ADR 0008 +
  > `projects/cashbook-entry-reconcile/plan.md` (Phases 2–3). Not added to this glossary as current truth
  > until the behavior ships.

- **All-time mode vs Month-scoped mode** — the two states of the `/cashbook` dashboard, chosen by
  the month picker. All-time (`?month=ทั้งหมด`) = every txn (the original behavior). Month-scoped
  (`?month=YYYY-MM`) filters the cards, per-account table, category/tag summaries and the doughnut
  to one calendar month. Default on load = the most recent month **with data** (not the current
  month, which entry-lag often leaves empty). The trend chart + สรุปรายเดือน table never scope
  (they are the all-months story + the month navigator). See ADR 0007.

- **สุทธิเดือนนี้ (net this month)** — operating `income − expense` for the selected month; the 3rd
  headline card in month mode (the same slot shows **คงเหลือ**, true cash, in all-time mode — the
  label swaps with the mode). It is a monthly P&L net, **NOT** a cash balance, and is computed as
  `income − expense`, never `sum(account.balance)` (which folds in `เงินทุน/เงินโอน` transfers).

- **Overspend flag** — a per-category expense alert on the month-scoped dashboard: this month's
  category total ≥ 20% **and** ≥ ฿1,000 above the previous month's (both). Categories absent last
  month are labelled "ใหม่", not flagged. Roll-up "▲N หมวดบวม" on the รายจ่ายรวม card. Baseline is
  the previous month (thresholds are tunable constants). See ADR 0007.

- **MTD (month-to-date) comparison** — for the current, incomplete month the overspend/delta compare
  uses the previous month's **same day-range** (day 1..today, clamped to the prev month's length) so
  a partial month isn't judged against a full one; tagged "เดือนยังไม่จบ". Past months compare
  full-vs-full.

## Product creation & naming

- **Structured product** — a product whose spec columns (`brand_id`, `category_id`,
  `sub_category`, `series`, `model`, `size`, `color_code`, `packaging_th`, `condition`,
  `pack_variant`) are filled, so its `product_name` and `sku_code` are **derived** from
  those columns by the naming engine — not typed free-hand. The target shape for every
  product in the catalog.

- **Bare product** — a product carrying only a free-text `product_name` (+ unit / pack
  counts) with no spec columns, hence no derived `sku_code`. Produced by the old hand
  form, the CSV master importer, and the legacy quick-create. Being phased out: creation
  now goes through the structured path. _Avoid_: "quick product", "stub product".

- **Smart Suggest (staged suggestion)** — the `/mapping` flow that parses a BSN code's
  raw name into spec fields, stages them as a `pending_product_suggestion`, and a
  manager **approves** to create a *structured product* mapped to that code. The trusted
  template for how products should be created. _Avoid_: "auto-map", "quick create".

- **Canonical product creation (single source of truth)** — the one path every new
  structured product goes through: type a raw name → the parser fills spec fields →
  review/edit → engine builds the derived name + `sku_code` → insert. Both the hand form
  (`/products/new`) and Smart Suggest approval funnel through it. There is deliberately
  **no** "create a bare product" path. See `docs/adr/0004`.

- **created_via (product origin / provenance)** — a label recording **how** a product
  entered the catalog: `manual` (hand form), `smart_mapping` (Smart Suggest approval), or
  `legacy` (existed before origin tracking, incl. bulk-imported rows). A provenance tag
  shown on the product page. It is **not** a permission, a status, or a lifecycle state.
  _Avoid_: "source", "type", "created_by" (that would be a person, not a method).

## Product labels (ป้ายสินค้า — the `/labels` print feature)

- **ป้ายสินค้า (product label / hang-tag)** — an **own-brand compliance sticker** printed and
  stuck on the product: name, brand, วิธีใช้ (usage), ข้อแนะนำ (warning), บรรจุ (packaging), size,
  plus a fixed **company block** and a fixed price line. Printed in one of two **label modes**
  (บาร์โค้ด / สคบ — see below) to a **GoDEX thermal printer**. This is the concept behind Put's 2017
  Excel barcode master. _Avoid_: "price tag", "sticker" (ambiguous — see below).

- **label mode (บาร์โค้ด / สคบ)** — a ป้ายสินค้า prints in one of two modes, chosen **per print run**,
  NOT stored on the product: **บาร์โค้ด** (the EAN-13 barcode tag) or **สคบ** (the no-barcode
  compliance tag). สคบ works for every product; บาร์โค้ด only for rows that carry a barcode (a
  barcodeless product is flagged and excluded from a barcode run). Each mode × size has ONE fixed
  layout applied to all products. _Avoid_: "label type" (mode is a print-time choice, not a product
  attribute).

- **สคบ label (ป้ายคำแนะนำ สคบ)** — the **no-barcode** compliance/instruction ป้ายสินค้า mandated by
  the **สคบ** (สำนักงานคณะกรรมการคุ้มครองผู้บริโภค — Office of the Consumer Protection Board):
  product name + เครื่องหมายการค้า, วิธีใช้, ข้อแนะนำ, ผู้จัดจำหน่าย, ราคา, บรรจุ. Same media/sizes
  as the barcode tag. An optional field a product has no data for **collapses** (its line drops);
  name, ราคา and ผู้จัดจำหน่าย always print. _Avoid_: "instruction label", "info sticker".

- **card registration (per-card offset)** — where each label sits across a **multi-up** roll,
  positioned by a per-card horizontal offset (the small roll is 3-up). A property of the **physical
  roll** — it drifts per roll — NOT of the layout, and it lives server-side so Mac (tuning) and
  Windows (printing) agree. _Avoid_: "margin" (ambiguous with a label's inner padding), "gap".

- **price tag / shelf label** — the *other*, simpler label the existing `/labels` page renders
  today: product name + a live **numeric price** + barcode, for shelf/หน้าร้าน display. A **different
  artefact** from a ป้ายสินค้า, which never shows a number. Keep the two distinct.

- **ตรวจสอบ ณ จุดขาย (price line)** — a ป้ายสินค้า **never prints a numeric price**; the price
  field is always the literal text "ราคา : ตรวจสอบ ณ จุดขาย". Own-brand B2B prices vary per
  customer, so no price is committed to the physical sticker.

- **company block (constant boilerplate)** — the ~70% of a ป้ายสินค้า that is **identical on every
  label**: ผู้จัดจำหน่าย (distributor = บุญสวัสดิ์นำชัย), นำเข้าโดย (importer = เซ็นไดเทรดดิ้ง),
  addresses, ประเทศที่ผลิต, quality line. Stored **once**, not per row. Only ~6 fields vary per
  product (name, brand, barcode, usage, warning, packaging, size).

- **barcode (EAN-13)** — a **registered** GS1 barcode (885… = Thailand). These live in the label
  master; Sendy's own `product_barcodes` table is empty. On a ป้ายสินค้า the barcode is printed
  identity, **not** currently scanned into any system.

- **label master (standalone dataset)** — the ป้ายสินค้า data is its **own table**, imported from
  the Excel and edited in Sendy, **not tied to `products`**. It is a superset of the live own-brand
  catalogue and has no reliable key back to a product. A future `product_id` link will be matched by
  **barcode**. See `docs/adr/0009`. _Avoid_: "product field", "product column".
