# ADR 0019 — Unit conversion: one home for every reader, one named rule per reader family

Status: Accepted · 2026-09-29

## Context

`unit_conversions` answers "how many base units is one of this unit" (1 โหล = 12 ตัว). Before card A,
about ten places read it with their own SQL: the stock writer, the price resolver, COGS, the marketplace
sold count, the call card, the quote CLI, the VAT-sub unit selector, review rules and the remap preflight.
The first plan was one function with an `on_miss` flag. An `/interrogate` round (Fable, Codex Sol and
Opus, 2026-09-29) showed that this cannot preserve behaviour. The readers differ on five axes, not one:

- **How they compare the bill unit with the base unit.** By the unit's word (the price resolver), by raw
  spelling after `strip()` (the stock writer, the remap preflight), or by raw spelling as stored (COGS,
  the call card, the CLI, review rules).
- **Which spellings they try.** The exact spelling only, or the spelling and then its word through the
  unit map (ADR 0018).
- **Whether the base unit or a stored row wins.** Most readers short-circuit the base unit first.
  `_resolve_unit` reads the row first, and its `ratio_source` is part of the resolver's output.
- **What a blank unit means.** 1.0 for the resolver and COGS. A held line for the stock writer.
- **What a miss means.** See Decision 3.

PROD, 2026-09-29: the families agree on today's data. The exact family holds 0 lines that the word family
would convert. There are 733 rows keyed on a product's own base unit and none of them has a ratio other
than 1. No variant spelling is in use anywhere. The gap is real in the code and latent in the data. It
reopens as soon as a non-canonical spelling comes back.

## Decision

1. **`inventory_app/unit_conversion.py` is the one home for reading ratios.** A reader census
   (`tests/test_unit_reader_census.py`) makes every other direct `SELECT`/`JOIN` on the table a writer
   or an exemption with a reason.
2. **Each reader family keeps its own rule under its own name.** No flag chooses between them.
   `word_ratio` (the price resolver's bill lookup, and since #668 the call card's, on the stripped unit),
   `conversion_ratio` (the row lookup that `_resolve_unit` and `_bundle_buy_ratio` build on),
   `exact_ratio` / `exact_ratios` (exact spelling, for the quote CLI and the VAT-sub selector), and the
   COGS SQL trio (`base_qty_sql`,
   `unit_conversion_join`, `unratioed_line_sql`). The stock side reads through
   `exact_ratio(..., strip=True)`: the stock writer (`bsn_sync._get_base_qty`, kept as a thin wrapper
   that owns the 4-dp round) and the remap preflight (`mapping.missing_unit_ratios`). `strip` applies to
   the base-unit compare only; the lookup is the raw spelling. Review rules (`_get_ratio`) predict the
   stock writer but compare raw, as they always have. Their exact, raw-spelling lookup is kept (Q11).
3. **A miss is the caller's policy, named here and not in a parameter:**

   | Reader | A unit with no ratio |
   |---|---|
   | stock writer | the line is held: stock is not moved until a ratio exists |
   | remap preflight (`missing_unit_ratios`) | the unit is listed: repoint refuses, the dry-run warns |
   | review rules (R4) | a high flag: หน่วย "…" ไม่มีอัตราแปลง — สต๊อกจะไม่ตัด |
   | price evidence (`word_ratio`) | the bill is skipped (the lowest-price scan also counts it in `n_unratioed`) |
   | customer-page cost block (`word_ratio`) | the figure shows —, never a ratio of 1 |
   | COGS (`base_qty_sql`) | costed at 1, counted, and disclosed on `/accounting` |
   | marketplace sold count | counted at 1 and disclosed on the product page (Q14) |
   | call card (`word_ratio`, #668) | no price: ราคาตั้ง and the promo modal say ไม่มีอัตราแปลง and link to `/unit-conversions`; a ratio of 0 is a miss |
   | promo bundle gating (`_bundle_buy_ratio`) | 1.0, silently |
   | quote CLI | the bill is skipped |

4. **One silent-1.0 site is kept on purpose (Q16).** `_bundle_buy_ratio` feeds an internal promo
   gating check, not a number anyone reads; it is named here so it does not read as an oversight.
   The call card's unconverted price was the second one until #668 (Put, 2026-10-02): it now finds the
   ratio by word and shows no price on a miss.
5. **The marketplace sold count moves onto the COGS rule (Q14).** A line in the product's own unit is
   ratio 1, whatever a row keyed on that unit says. Lines with no ratio are counted and disclosed. On
   PROD, no number moved (the 0 base-keyed rows above).

## Considered options

- **One `ratio(..., on_miss=)` for everyone.** Rejected. Two of its three values returned the same
  thing (holding and skipping are what the caller does next). The third made "1.0 because missing"
  impossible to tell apart from a real 1.0. And it said nothing about the four axes that actually differ.
- **Unify on the word family.** Rejected for now. It would change COGS (a `กุรุส` line on a product
  whose row is stored as `กร` reads 1.0 in SQL today and 144 through the word) and the stock ledger.
  A money change needs its own measurement and Put's call, not a refactor.
- **A Python-vs-SQL twin test.** Rejected. By design the SQL is the exact family, so the test would
  either fail on day one or be written to expect the difference. The difference is pinned as expected
  rows in `tests/test_unit_conversion_families.py` instead.

## Consequences

- A new reader has to pick a named family, or explain itself in the census.
- The known disagreements are test rows (`tests/test_unit_conversion_families.py`). If one is ever
  closed, the change is visible there and not a side effect.
- `sales_filters` still re-exports the SQL trio, for the dated rebase scripts that import it from there.
- The stock writer's 4-dp round stays with the stock writer. Nothing in the module rounds.

## Follow-up (Q11), not done here

PROD, 2026-09-29 (M1): the exact and word families agree on every mapped ledger row. No line the stock
writer holds would be converted by the word family, and no synced line would change its base quantity
(0 variant spellings, 0 blank units; the 35 held rows are on pids 300, 302, 1211 and 1623 and have no
ratio under either rule). So a canonical-word compare on the stock side is allowed as a later PR. It
must move four things in lockstep, or held lines, the pending list and the review flags disagree: the stock family's
`exact_ratio(strip=True)`, `bsn_sync.conversion_unit_key` (the writer's key, which follows the ledger's
spelling today), the NOT EXISTS predicate in `get_pending_unit_conversions`, and `review_rules._get_ratio` (R4 predicts
the stock writer, and already differs from it on whitespace, pinned in the families test). Its gate is the same
as this card's: re-measure M1 on PROD first, then a per-line differential and a stock replay.
