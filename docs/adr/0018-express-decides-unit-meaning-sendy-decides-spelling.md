# ADR 0018 — Express decides what a unit code means, Sendy decides the spelling, one unit map in the DB

Status: Accepted · 2026-09-19

## Context

Express writes units as short codes (`หค`, `หล`, `กร`). Sendy translates them at import through a
hand-built map, `data/reference/bsn_unit_full.json` (44 entries, reviewed by Put 2026-05-18). A copy of
it, the `bsn_unit_alias` table (migration 064), has no runtime reader. Measured 2026-09-19:

- **The map disagrees with Express's own unit list** (`ISTAB`, `TABTYP '20'`) on meaning. `กร` is
  กุรุส (factor 144 on 1,252 of 1,264 stock-card lines, Express DBF), but the map said `ตัว`. `ถง` is
  ถัง (18-50 kg grease buckets), but the map said ถุง. `บล` is บล็อก, but the map said แผง. Because the
  map rewrote `กร` as `ตัว`, a gross sold through Express reads as `ตัว` in Sendy: the trap behind #581.
- **The two Express books disagree with each other.** `หอ` is ห่อ in BSN5657 and หลอด in the VAT
  book (xp5). One map stored every VAT-book sealant and glue tube as ห่อ: 3,054 sales and 396 purchase
  lines, 36 products (local `vat_book.db`, built 2026-09-04).
- **Codes escaped translation** (prod). 3,817 purchase rows from one history batch (#37,
  2026-05-30), 1,066 `unit_conversions` rows (1,053 of them duplicating a full-word row at the same
  ratio), 6 product units. Bill RR6900079 shows `หค` and `โหลคู่` side by side for the same unit.
- **The map is a file the `/unit-conversions` page rewrites at runtime.** On prod it sits on the
  container disk, not the `/data` volume. Whether it survives a redeploy is unverified.

## Decision

1. **A code's meaning comes from the unit list of the Express book it came from** (BSN5657 or xp5).
   Sendy's map reads a code together with its book.
2. **Sendy chooses one spelling per unit** (`กิโลกรัม`, `แพ็ค`). Every variant maps to it: any Express
   code, `กก.`, `กิโล`, `1กิโล`.
3. **Sendy stores only that word, everywhere**, including the tables that copy Express lines verbatim
   (`express_sales_order_lines`, the credit-note tables). The Express code is not kept alongside it:
   the Express book keeps every line's code and can be re-read by document and line number.
4. **The map is one DB table, and only that table.** The JSON file is retired.
5. **Rows already written through the wrong map are relabelled** wherever the Express book lets us
   recover the original code. Each row's conversion moves with it, so stock, WACC and COGS do not move.

## Considered options

- **Translate on display only** and keep what Express sent. Rejected: two spellings stay in the data,
  and so do the duplicate conversion rows, which drift apart because `update_unit_conversion_ratio`
  edits one twin and not the other.
- **Keep the map as a git-tracked JSON file.** Rejected: `/unit-conversions` edits it at runtime, and a
  runtime edit has to live where both gunicorn workers read it and a deploy does not wipe it.
- **Keep Sendy's hand map as the authority for meaning.** Rejected: that is how `กร` became `ตัว`.
- **Also store the raw Express code as evidence.** Rejected: it duplicates what Express already keeps.
- **Use Express's spelling** (`กิโล`, `แพค`). Rejected: Sendy's rows already use `กิโลกรัม` (308 sales
  lines) and `แพ็ค` (425).

## Consequences

- This reverses the May 2026 review of the map on `กร` (ตัว → กุรุส). About ten products whose own
  `unit_type` `ตัว` really means a gross (the กระดาษทราย #0/#1/#2/#4 family, ขอสับ, ดินสอ Yokomo,
  ไส้ดินสอ, ตะปูควง M) will show their bills in กุรุส while the product itself still says ตัว, until each
  is rebased the way 1050/1320 were (#581). That is separate work.
- The `!` Express prints in its text reports is not part of a unit. It marks a line whose factor does
  not fit the item's base unit. The five `!` entries leave the map.
- Tier labels and the product form's unit field go through the same map. The form translates a
  variant on save, and a genuinely new word is still allowed.
- A code the map does not know still goes to `/unit-conversions` for Put to name, now recorded
  against its book.
