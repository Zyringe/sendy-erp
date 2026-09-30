# ADR 0021 — A sales line's หน่วย can be corrected in Sendy, and Express wins when it moves

Status: Accepted · 2026-09-30 (Put, issue #692 and its amendments R1–R3)

## Context

Express sometimes holds the wrong หน่วย on a sales line: a marketplace order for 2 หลอด keyed as 2 โหล.
Express is not edited (Put), so the line stays wrong at source. In Sendy it over-deducts stock (−24
instead of −2) and over-costs the line, because COGS multiplies the line's qty by the ratio of its
stored unit at read time.

A hand edit of the stored row does not survive. The daily zip (ADR 0020) diffs every in-window line on
`qty, unit, unit_price, net, product_id` and replaces a changed one with DELETE + INSERT, under a new row
id. The standing technique "make the hand fix equal what the importer writes" cannot apply here: the
importer writes Express's unit, which is the thing being corrected.

Measured on PROD 2026-09-30 (read-only), product 727, base unit หลอด, โหล = 12: `IV6901446-2`,
`IV6900538-1` and `IV6701891-1` each match a marketplace order that sold หลอด, each line key is unique
and each posted one ordinary `BSN ขาย` ledger row. Only the first is inside the zip's 60-day window.

## Decision

1. **Only the หน่วย is correctable.** qty, unit_price, net and product stay Express's. The new unit must
   be the product's base unit or a stored ratio spelling, and a fixed point of the unit map, because the
   stock writer and COGS look the stored spelling up exactly (`line_unit_correction.allowed_units`).
2. **One table, one row per correction**: `sales_line_unit_corrections` (mig 198), keyed on the line key
   `(doc_no, bsn_code)` and never on the row id. The row carries the Express signature it was made
   against (unit raw and normalised, qty, unit_price, net, product). States are
   `active → cancelled` (admin) and `active → retired` (importer); the table CHECKs hold each state's
   shape and a partial unique index allows one active correction per line. A new correction is a new row.
3. **One module owns every rule**: `inventory_app/line_unit_correction.py`. The importer, the drift scan
   and the guarded writers call it. `decide()` is pure and returns `keep`, `express_agrees` or
   `express_changed`; the importer branches on that alone.
4. **apply is one `BEGIN IMMEDIATE`.** The sales row's unit changes through `declared_update`
   (`source='manual'`, the admin's reason). The ONE verified ledger row is updated in place, so it keeps
   its id and date and no other row of the product is re-posted. The review scan and the WACC
   recalculation run on the same connection before the commit; a cost failure rolls the whole correction
   back.
5. **Stock: the admin picks `hold` or `move`** (R1). "Was there a stock count after the sale" cannot be
   read off the ledger: a count equal to book stock writes no row, and other adjusts are the same
   absolute write under another note. The preview proposes `hold` when any ADJUST sits after the sale,
   shows that row, and leaves the choice to the admin.
   - `hold`: stock must not move. The offset ADJUST is the MEASURED difference
     (`stock_before − stock_after`), so its sign is right for a sale and for an SR return alike, and it
     is dated at the line's own timestamp. The WACC walk orders `created_at, IN first, id`. For a
     **sale** (an OUT) the running stock at every purchase is then what it was before the correction and
     cost history does not move. For an **SR return** that is not guaranteed: the return is an IN and its
     offset an ADJUST, so a costed lot of the same timestamp with a higher transaction id is walked
     between them and re-weighted. No placement of the offset avoids that (an earlier timestamp changes
     what the same-day lots with a lower id see instead), so `preview` reports the number of such lots
     (`Effect.hold_reweights_purchases`, 0 for a sale) and the admin decides.
   - `move`: no offset, stock moves by the difference, and purchases after the sale are re-weighted (R3,
     accepted). The preview measures the lowest running stock and the purchases costed at or below zero
     stock, before and after.
6. **cancel is the inverse, and "pre-correction state" is semantic**: the sales row equal except its four
   `change_*` columns; ledger rows equal as the multiset
   `(product_id, txn_type, quantity_change, reference_no, note, created_at)`; `stock_levels.quantity`
   equal at 4 dp; `product_cost_ledger` rows equal except ids; `audit_log` gains rows. cancel refuses
   when the line no longer holds the corrected unit, moved product, or when Express's unit has lost its
   ratio (the restored row could never post again).
7. **The daily zip, sales of the main book only.** The active corrections are loaded after the importer's
   first write, inside its write transaction. Mapping is resolved on Express's unit as before, so a
   correction never changes the product.
   - Express still says the signature → the line is `unchanged`. Nothing is written.
   - Express now says the corrected unit → retired `express_agrees`, the offset is KEPT (nothing physical
     changed) and the line reads unchanged.
   - Anything else, or the line is removed from its document → retired `express_changed` /
     `express_removed`, the offset is deleted and the ordinary replace or removal runs. Express wins.
   `retire` only deletes the offset, stamps the row and raises the `unit_correction_retired` alert on the
   importer's connection. Pass 2, the post-commit WACC and the route's review scan then run once, on the
   final state.
8. **Outside the 60-day window nothing retires a correction** (R2): the importer never sees the document.
   The drift scan is the signal and an admin cancels by hand.
9. **Drift scan: the override is on the Sendy side.** A stored line that still holds its corrected unit is
   compared as Express's unit, so a corrected document equals Express until Express changes. The Express
   side is untouched, so a baselined or acknowledged document keeps its fingerprint. A finding on a
   document with an active correction carries the correction ids into its alert.
10. **Writers that would strand a correction refuse while one is active**, through one function,
    `blocking()`: `repoint_bsn_code` (by code and every affected product),
    `update_unit_conversion_ratio`, `dismiss_pending_unit_conversion`, `apply_reconcile_flag` (by
    document) and `scripts/merge_product.py` (both products). The message is
    `ยกเลิกการแก้หน่วยบรรทัดก่อน (<doc_no>)`.
11. **The VAT book keeps copying Express.** The importer gate is explicit (`book == DEFAULT_BOOK`), not
    a side effect of the build DB's table being empty: one document number can exist in both books.

## Considered

- **Override the incoming unit before the mapping lookup**: mapping is unit-aware, so a split code would
  resolve another product and the line would read `changed` on every zip.
- **Delete the ledger row and re-sync the product**: `_sync_bsn_to_stock` posts every unsynced row of the
  product, and two lines can share one literal `doc_no`. The in-place UPDATE of one verified row touches
  nothing else.
- **Compute the offset from the two base quantities**: the written formula had the wrong sign for an
  ordinary sale. Measuring has no sign to get wrong.
- **Date the offset at the count, or today**: both re-weight purchases between the sale and that date.
  The sale's own timestamp re-weights none.
- **Let `retire` share cancel's re-sync, scan and WACC**: inside the importer that would run in pass 1 on
  a ledger pass 2 is about to rebuild, and a cost failure there would abort the whole import.
- **Migrate the offset through every mutator** (remap, ratio edit, rebase, merge): far more surface than
  refusing. Corrections are rare.
- **Apply the correction on the Express side of the drift scan**: it would move the fingerprint of every
  baselined or acknowledged document the day a line in it is corrected.

## Consequences

- Express stays wrong for a corrected line, and so does the VAT book. Accepted.
- A document older than 60 days whose Express line later changes raises one ordinary drift alert naming
  the correction. If Express is re-keyed to exactly the corrected unit the alert still fires once (Sendy's
  side reads the old Express unit), which is the prompt to cancel.
- A whole document deleted in Express never reaches the importer. It becomes a reconcile flag, and
  applying that flag is refused until the correction is cancelled.
- `preview_import`, the dormant text door of ADR 0020, knows nothing about corrections. Re-enabling that
  door means teaching it the same `decide()`.
- The table CHECKs fix each state's shape but no trigger forbids moving a row back to `active`. Only
  this module writes the table.
- Not guarded: the ratio upserts on `/unit-conversions` (`save_unit_conversions`,
  `upsert_unit_conversion`) and the dated rebase scripts. A future rebase or merge script must call
  `line_unit_correction.blocking` before it writes.
- `hold` on an SR return re-weights any purchase or conversion lot dated the same day and posted after
  it (decision 5). Pinned by `test_hold_on_a_return_reweights_a_same_day_purchase_posted_after_it`.
- No page yet. The routes, the confirm panel and the invoice badge are PR-2; `badges_for_doc` and
  `preview` are the engine halves they will call.
