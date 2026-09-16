-- 184 -- GH #525: correct sales_transactions.discount/total for 176 lines the
-- CSV parser column-shifted, before the anchor fix (#525 PR1) closed the bug.
--
-- WHAT
-- inventory_app/parse_weekly.py's _TX_SALES used an unbounded, unanchored
-- character class for the ส่วนลด/ส่วนลดรวม money columns. When a line's own
-- ส่วนลด was genuinely blank and the bill carried a ส่วนลดรวม (doc-level
-- discount, reprinted on every line) printed in BAHT rather than a percent,
-- the parser grabbed รวมเงิน (the true line total) into `discount`, and the
-- small header discount into `total`. `net` was always correct -- Express's
-- ยอดขายสุทธิ never moved.
--
-- POPULATION: 176 lines / 79 doc_base. Re-derived independently in this
-- session against the DBF AND against a live prod read (2026-09-17) -- see
-- Operations/05_analysis-reports/data-quality/sales_line_reconciliation_2026-09-16.md
-- and GH #525's own comments for the full history. ⚠ An earlier draft of the
-- dispatch for this migration said "220 lines (176 with net>0 + 44 with
-- net=0)" -- that arithmetic double-counts: the 44 net=0 lines are a SUBSET
-- of the 176, not additional to it (175 mechanism-A lines = 131 net>0 + 44
-- net=0, plus 1 mechanism-B line with net>0 = 132 net>0 + 44 net=0 = 176
-- total). The 176-row VALUES block below is what was actually re-derived and
-- verified, not 220.
--
-- 175 of the 176 are "mechanism A" (the correct discount is BLANK). One line
-- -- id 36661, IV6900395-1 -- is "mechanism B": a pre-2026-05-31 comma bug,
-- imported before that fix and never reprocessed; its correct discount is
-- '1,429.00', not blank.
--
-- SOURCE OF THE CORRECT VALUES: BSN5657 Express DBF snapshot dated
-- 2026-08-25 (STCRD.DISC -> discount, STCRD.TRNVAL -> total), joined on
-- doc_base + the doc_no line-suffix == STCRD.SEQNUM, with bsn_code ==
-- STKCOD as an INDEPENDENT join control (176/176 passed, 0 UNKNOWN). This
-- migration's own re-derivation, run fresh in this session, reproduced the
-- exact same 176 ids, doc_bases, and mechanism split as the prior
-- investigation. Verified against LIVE PROD on 2026-09-17: all 176
-- (id, doc_no, bsn_code, discount, total, net) tuples below matched prod's
-- CURRENT rows byte-for-byte (0 missing, 0 mismatches) before this migration
-- was written.
--
-- SCOPE: writes ONLY `discount` and `total`. `net`, `qty`, `unit_price`,
-- `product_id` and everything touching stock/WACC are untouched -- the
-- UPDATE below does not name those columns, and the postcondition
-- re-asserts net/qty moved zero bytes.
--
-- NOT IN SCOPE: the SR credit-note lines (mechanism C, GH #556) -- a
-- different importer, a different blast radius (it corrupts `net`, this one
-- never does), already fixed separately. Do not fold #556 into this
-- migration.
--
-- WHY THIS CANNOT BE A PURE SQL PREDICATE: `correct_total` is
-- STCRD.TRNVAL, which exists only in the DBF and is not derivable from any
-- column sales_transactions already holds. The 176-row VALUES block below
-- IS this migration's data, generated from the DBF at authoring time --
-- re-deriving it later means re-running the DBF join, never hand-editing
-- these literals.
--
-- GUARD: sales_transactions_change_needs_declaration (mig 173) ABORTs any
-- UPDATE to discount/total without a fresh change_token, change_source IN
-- ('import','manual') and a non-blank change_actor -- and, since this is
-- 'manual', a change_reason >= 12 characters. audit_sales_transactions_update
-- then writes one audit_log row per changed line -- the intended paper
-- trail (confirm 176 rows land there after applying).
--
-- RE-RUNNABLE / ENVIRONMENT-SENSITIVE: the UPDATE's WHERE clause matches
-- `id` AND doc_no AND bsn_code AND the OLD (still-wrong) discount/total, so
-- it is a no-op on a DB that already holds the corrected values (a second
-- run, or a DB where the fix already landed some other way), and it is also
-- a no-op for any id that does not exist on a DB that never had this row.
-- It does not assume every environment holds all 176 rows.
--
-- PRECONDITION: ABORT if any of the 176 ids is in neither of the two
-- expected states ("still holds the old wrong value" or "already holds the
-- correct value") -- i.e. it drifted to a THIRD state between this file
-- being written and it being applied (row deleted, doc_no/bsn_code changed,
-- or discount/total hand-edited to something else). That is unexpected and
-- must stop the whole migration rather than silently overwrite or silently
-- skip a row nobody has looked at.
--
-- RE-RUNNABLE trigger/table DDL: DROP ... IF EXISTS before every CREATE.
PRAGMA busy_timeout = 10000;

BEGIN;

CREATE TABLE IF NOT EXISTS migration_184_snapshot (
    id                INTEGER PRIMARY KEY,
    doc_no            TEXT NOT NULL,
    bsn_code          TEXT NOT NULL,
    old_discount      TEXT NOT NULL,
    old_total         REAL NOT NULL,
    correct_discount  TEXT NOT NULL,
    correct_total     REAL NOT NULL,
    net_at_migration  REAL NOT NULL,  -- forensic only -- this migration never writes net
    qty_at_migration  REAL NOT NULL   -- forensic only -- this migration never writes qty
);

INSERT OR IGNORE INTO migration_184_snapshot
    (id, doc_no, bsn_code, old_discount, old_total, correct_discount, correct_total, net_at_migration, qty_at_migration)
VALUES
(28394, 'IV6800557-1', '031บ4111', '480.00', 804.0, '', 480.0, 0.0, 3.0),
(28483, 'IV6800558-1', '031บ4111', '320.00', 644.0, '', 320.0, 0.0, 2.0),
(28567, 'IV6800548-1', '001ก3310', '360.00', 4024.5, '', 360.0, 0.0, 12.0),
(28568, 'IV6800548-2', '001ก3312', '360.00', 4024.5, '', 360.0, 0.0, 12.0),
(28569, 'IV6800548-3', '001ก3320', '480.00', 4024.5, '', 480.0, 0.0, 12.0),
(28908, 'IV6800555-1', '031บ4111', '480.00', 1398.5, '', 480.0, 0.0, 3.0),
(28965, 'IV6800551-1', '001ก3310', '180.00', 2537.0, '', 180.0, 0.0, 6.0),
(28966, 'IV6800551-3', '001ก3312', '180.00', 2537.0, '', 180.0, 0.0, 6.0),
(28967, 'IV6800551-2', '001ก3320', '240.00', 2537.0, '', 240.0, 0.0, 6.0),
(29637, 'IV6800546-1', '001ก3310', '360.00', 3045.0, '', 360.0, 0.0, 12.0),
(29638, 'IV6800546-2', '001ก3312', '360.00', 3045.0, '', 360.0, 0.0, 12.0),
(29639, 'IV6800546-3', '001ก3320', '480.00', 3045.0, '', 480.0, 0.0, 12.0),
(29767, 'IV6800546-9', '031บ2550', '450.00', 3045.0, '', 450.0, 0.0, 1.0),
(30098, 'IV6800547-1', '001ก3310', '360.00', 1731.25, '', 360.0, 0.0, 12.0),
(30099, 'IV6800547-2', '001ก3312', '360.00', 1731.25, '', 360.0, 0.0, 12.0),
(30100, 'IV6800547-3', '001ก3320', '480.00', 1731.25, '', 480.0, 0.0, 12.0),
(30113, 'IV6800547-5', '031บ4111', '160.00', 1731.25, '', 160.0, 0.0, 1.0),
(30481, 'IV6701161-1', '044ล0130', '3480.00', 170.0, '', 3480.0, 3446.26, 24.0),
(30490, 'IV6701161-2', '044ล0160', '3480.00', 170.0, '', 3480.0, 3446.26, 24.0),
(30514, 'IV6701161-3', '044ล0710', '2640.00', 170.0, '', 2640.0, 2614.4, 24.0),
(30809, 'IV6800552-1', '001ก3310', '180.00', 1387.5, '', 180.0, 0.0, 6.0),
(30810, 'IV6800552-3', '001ก3312', '180.00', 1387.5, '', 180.0, 0.0, 6.0),
(30811, 'IV6800552-2', '001ก3320', '240.00', 1387.5, '', 240.0, 0.0, 6.0),
(30905, 'IV6800556-2', '031บ4111', '480.00', 1173.5, '', 480.0, 0.0, 3.0),
(30916, 'IV6800553-1', '001ก3310', '180.00', 1569.5, '', 180.0, 0.0, 6.0),
(30917, 'IV6800553-3', '001ก3312', '180.00', 1569.5, '', 180.0, 0.0, 6.0),
(30918, 'IV6800553-2', '001ก3320', '240.00', 1569.5, '', 240.0, 0.0, 6.0),
(30987, 'IV6800554-1', '031บ4111', '480.00', 1398.5, '', 480.0, 0.0, 3.0),
(31049, 'IV6800545-1', '001ก3310', '180.00', 1569.5, '', 180.0, 0.0, 6.0),
(31050, 'IV6800545-3', '001ก3312', '180.00', 1569.5, '', 180.0, 0.0, 6.0),
(31051, 'IV6800545-2', '001ก3320', '240.00', 1569.5, '', 240.0, 0.0, 6.0),
(31149, 'IV6800550-1', '001ก3310', '180.00', 2537.0, '', 180.0, 0.0, 6.0),
(31150, 'IV6800550-3', '001ก3312', '180.00', 2537.0, '', 180.0, 0.0, 6.0),
(31151, 'IV6800550-2', '001ก3320', '240.00', 2537.0, '', 240.0, 0.0, 6.0),
(31238, 'IV6702352-1', '026ต2510', '5400.00', 2856.0, '', 5400.0, 4879.96, 20.0),
(31239, 'IV6702352-2', '026ต2510', '0.00', 2856.0, '', 0.0, 0.0, 20.0),
(31248, 'IV6702352-3', '026ต2530', '10700.00', 2856.0, '', 10700.0, 9669.54, 20.0),
(31249, 'IV6702352-4', '026ต2530', '0.00', 2856.0, '', 0.0, 0.0, 20.0),
(31252, 'IV6702352-5', '026ต2540', '10700.00', 2856.0, '', 10700.0, 9669.54, 20.0),
(31253, 'IV6702352-6', '026ต2540', '0.00', 2856.0, '', 0.0, 0.0, 20.0),
(31285, 'IV6800549-1', '001ก3310', '360.00', 3513.0, '', 360.0, 0.0, 12.0),
(31286, 'IV6800549-3', '001ก3312', '360.00', 3513.0, '', 360.0, 0.0, 12.0),
(31287, 'IV6800549-2', '001ก3320', '480.00', 3513.0, '', 480.0, 0.0, 12.0),
(32448, 'IV6801816-1', '001ก1000', '149.00', 51.0, '', 149.0, 124.33, 1.0),
(32449, 'IV6802074-1', '001ก1000', '149.00', 25.0, '', 149.0, 124.0, 1.0),
(32453, 'IV6801449-3', '001ก1200', '189.00', 64.0, '', 189.0, 157.82, 1.0),
(32454, 'IV6801539-2', '001ก1200', '149.00', 51.0, '', 149.0, 124.33, 1.0),
(32467, 'IV6801816-2', '001ก1400', '159.00', 51.0, '', 159.0, 132.67, 1.0),
(32477, 'IV6801449-1', '001ก1600', '199.00', 64.0, '', 199.0, 166.18, 1.0),
(32478, 'IV6801539-1', '001ก1600', '159.00', 51.0, '', 159.0, 132.67, 1.0),
(32486, 'IV6701211-1', '001ก2280', '290.00', 55.0, '', 290.0, 243.09, 1.0),
(32508, 'IV6701211-3', '001ก5920', '50.00', 55.0, '', 50.0, 41.91, 1.0),
(32542, 'IV6700460-3', '031บ8000', '0.00', 13.0, '', 0.0, 0.0, 1.0),
(32543, 'IV6700476-3', '031บ8000', '0.00', 14.0, '', 0.0, 0.0, 1.0),
(32562, 'IV6800342-1', '035ป6106', '24.00', 14.0, '', 24.0, 19.58, 2.0),
(32584, 'IV6800342-2', '035ป6107', '52.00', 14.0, '', 52.0, 42.42, 4.0),
(32598, 'IV6801819-1', '035ป6108', '170.00', 59.0, '', 170.0, 141.34, 10.0),
(32600, 'IV6801819-2', '035ป6110', '180.00', 59.0, '', 180.0, 149.66, 10.0),
(32601, 'IV6802672-1', '036ผ4020', '19.00', 14.0, '', 19.0, 15.5, 1.0),
(32602, 'IV6802672-2', '036ผ4035', '19.00', 14.0, '', 19.0, 15.5, 1.0),
(32603, 'IV6802672-3', '036ผ4060', '19.00', 14.0, '', 19.0, 15.5, 1.0),
(32605, 'IV6802672-4', '039ผ4040', '19.00', 14.0, '', 19.0, 15.5, 1.0),
(32606, 'IV6700460-2', '040ม1010', '39.00', 13.0, '', 39.0, 32.96, 1.0),
(32607, 'IV6700476-2', '040ม1010', '39.00', 14.0, '', 39.0, 33.48, 1.0),
(32628, 'IV6801448-1', '044ล0710', '179.00', 34.0, '', 179.0, 147.63, 1.0),
(32657, 'IV6801198-1', '169ถ0035', '33.00', 14.0, '', 33.0, 27.15, 1.0),
(32660, 'IV6700460-4', '520ต2324', '45.00', 13.0, '', 45.0, 38.04, 3.0),
(32663, 'IV6700476-1', '520ต2370', '60.00', 14.0, '', 60.0, 51.52, 3.0),
(32668, 'IV6700759-1', '526บ2203', '39.00', 14.0, '', 39.0, 33.19, 1.0),
(32669, 'IV6700759-2', '526บ2204', '55.00', 14.0, '', 55.0, 46.81, 1.0),
(32674, 'IV6700441-1', '528ด8650', '35.00', 21.0, '', 35.0, 29.56, 1.0),
(32675, 'IV6700441-2', '528ด8650', '100.00', 21.0, '', 100.0, 84.44, 1.0),
(32681, 'IV6801041-1', '532ต2322', '14.00', 6.0, '', 14.0, 11.1, 1.0),
(32687, 'IV6703432-1', '532ต2324', '13.00', 5.0, '', 13.0, 10.68, 1.0),
(32688, 'IV6801002-1', '532ต2324', '15.00', 6.0, '', 15.0, 12.57, 1.0),
(32689, 'IV6801041-2', '532ต2324', '15.00', 6.0, '', 15.0, 11.9, 1.0),
(32690, 'IV6801448-2', '532ต2324', '15.00', 34.0, '', 15.0, 12.37, 1.0),
(32692, 'IV6703432-2', '532ต2327', '15.00', 5.0, '', 15.0, 12.32, 1.0),
(32697, 'IV6703455-2', '532ต2330', '17.00', 9.0, '', 17.0, 14.06, 1.0),
(32700, 'IV6801002-2', '532ต2340', '22.00', 6.0, '', 22.0, 18.43, 1.0),
(32702, 'IV6801198-2', '532ต2370', '46.00', 14.0, '', 46.0, 37.85, 1.0),
(32713, 'IV6802104-1', '561ต2060', '540.00', 95.0, '', 540.0, 445.0, 12.0),
(32716, 'IV6703455-1', '568ก1101', '35.00', 9.0, '', 35.0, 28.94, 5.0),
(32742, 'IV6800890-1', '930บ6200', '150.00', 51.0, '', 150.0, 124.5, 2.0),
(32753, 'IV6800890-2', '930บ6330', '150.00', 51.0, '', 150.0, 124.5, 2.0),
(32756, 'IV6701463-4', '999ก6000', '600.00', 322.0, '', 600.0, 501.98, 3.0),
(32770, 'IV6701211-2', '999ฝ5500', '0.00', 55.0, '', 0.0, 0.0, 1.0),
(32787, 'IV6701463-1', '999อ6110', '477.00', 322.0, '', 477.0, 399.08, 3.0),
(32788, 'IV6701463-2', '999อ6120', '447.00', 322.0, '', 447.0, 373.97, 3.0),
(32796, 'IV6701463-3', '999อ6130', '447.00', 322.0, '', 447.0, 373.97, 3.0),
(34165, 'IV6701489-1', '026ต3010', '19.00', 10.7, '', 19.0, 8.3, 1.0),
(34241, 'IV6703344-1', '026ต3020', '27.00', 5.35, '', 27.0, 24.47, 1.0),
(34957, 'IV6703344-2', '603ต2506-1', '30.00', 5.35, '', 30.0, 27.18, 1.0),
(35889, 'IV6701977-1', '001ก2280', '290.00', 51.0, '', 290.0, 243.05, 1.0),
(36017, 'IV6701034-4', '001ก3430', '50.00', 31.0, '', 50.0, 41.58, 2.0),
(36144, 'IV6702419-3', '016ด5108', '35.00', 13.0, '', 35.0, 29.52, 1.0),
(36252, 'IV6702419-2', '026ต3030', '30.00', 13.0, '', 30.0, 25.3, 1.0),
(36285, 'IV6701034-1', '026ต3040', '56.00', 31.0, '', 56.0, 46.56, 1.0),
(36354, 'IV6702419-1', '026ต4610', '18.00', 13.0, '', 18.0, 15.18, 1.0),
(36363, 'IV6701034-2', '026ต4620', '34.00', 31.0, '', 34.0, 28.27, 1.0),
(36377, 'HS6800006-1', '030บ3412', '1404.00', 4.0, '', 1404.0, 1400.0, 36.0),
(36541, 'IV6800925-1', '041ม2625', '250.00', 43.0, '', 250.0, 207.0, 10.0),
(36661, 'IV6900395-1', '045ก2330', '', 1429.0, '1,429.00', 5991.0, 5991.0, 14.0),
(37047, 'IV6701979-1', '046ส9050', '40.00', 7.0, '', 40.0, 33.0, 2.0),
(37237, 'IV6801042-1', '168ถ0050', '33.00', 5.0, '', 33.0, 28.0, 1.0),
(37260, 'IV6800766-1', '168ถ0060', '33.00', 5.0, '', 33.0, 28.0, 1.0),
(37357, 'IV6701614-2', '538ท1110', '75.00', 32.0, '', 75.0, 62.69, 3.0),
(37358, 'IV6701977-2', '538ท1112', '25.00', 51.0, '', 25.0, 20.95, 1.0),
(37407, 'IV6701978-1', '556ล1020', '59.00', 9.0, '', 59.0, 50.0, 1.0),
(37521, 'IV6701614-3', '571ข2210', '90.00', 32.0, '', 90.0, 75.23, 2.0),
(37555, 'IV6701034-3', '603ต2506-1', '44.00', 31.0, '', 44.0, 36.59, 2.0),
(37601, 'IV6701614-1', '800ป5010', '30.00', 32.0, '', 30.0, 25.08, 2.0),
(37704, 'IV6701980-1', '900ก5101', '49.00', 9.0, '', 49.0, 40.0, 1.0),
(37861, 'IV6803096-1', '900ก5101', '49.00', 68.0, '', 49.0, 39.97, 1.0),
(37934, 'IV6803096-2', '930บ6210', '320.00', 68.0, '', 320.0, 261.03, 4.0),
(38066, 'IV6701246-2', '999ฝ5500', '0.00', 10.0, '', 0.0, 0.0, 1.0),
(38123, 'IV6701977-3', '999ฝ5500', '0.00', 51.0, '', 0.0, 0.0, 1.0),
(38172, 'IV6800389-2', '999ฝ5500', '0.00', 13.0, '', 0.0, 0.0, 4.0),
(38179, 'IV6800589-2', '999ฝ5500', '0.00', 6.0, '', 0.0, 0.0, 1.0),
(38847, 'IV6900557-2', '001ก1100', '149.00', 69.0, '', 149.0, 114.5, 1.0),
(38850, 'IV6900557-1', '001ก1300', '149.00', 69.0, '', 149.0, 114.5, 1.0),
(38870, 'IV6900593-3', '031บ9510', '39.00', 50.0, '', 39.0, 30.26, 1.0),
(38887, 'IV6900520-1', '046ส9040', '300.00', 354.0, '', 300.0, 233.62, 2.0),
(38890, 'IV6900520-2', '046ส9050', '320.00', 354.0, '', 320.0, 249.2, 2.0),
(38891, 'IV6900520-3', '046ส9060', '240.00', 354.0, '', 240.0, 186.9, 2.0),
(38892, 'IV6900520-4', '046ส9070', '280.00', 354.0, '', 280.0, 218.05, 2.0),
(38893, 'IV6900520-5', '046ส9080', '220.00', 354.0, '', 220.0, 171.33, 2.0),
(38894, 'IV6900520-6', '046ส9090', '240.00', 354.0, '', 240.0, 186.9, 2.0),
(38902, 'IV6900540-1', '556ล1020', '59.00', 32.0, '', 59.0, 45.89, 1.0),
(38905, 'IV6900593-1', '556ล1020', '59.00', 50.0, '', 59.0, 45.77, 1.0),
(38926, 'IV6900535-1', '930บ6200', '300.00', 134.0, '', 300.0, 234.1, 4.0),
(38928, 'IV6900535-3', '930บ6310', '160.00', 134.0, '', 160.0, 124.85, 2.0),
(38929, 'IV6900535-2', '930บ6330', '150.00', 134.0, '', 150.0, 117.05, 2.0),
(38932, 'IV6900540-2', '999อ1500', '85.00', 32.0, '', 85.0, 66.11, 1.0),
(38934, 'IV6900593-2', '999อ1501', '125.00', 50.0, '', 125.0, 96.97, 1.0),
(39468, 'IV6900716-2', '001ก1000', '149.00', 106.0, '', 149.0, 113.67, 1.0),
(39479, 'IV6900716-1', '001ก1300', '298.00', 106.0, '', 298.0, 227.33, 2.0),
(39496, 'IV6900669-1', '035ป5507', '15.00', 14.0, '', 15.0, 11.72, 1.0),
(39527, 'IV6900627-1', '556ล1020', '59.00', 43.0, '', 59.0, 45.21, 1.0),
(39528, 'IV6900633-1', '556ล1020', '59.00', 21.0, '', 59.0, 45.82, 1.0),
(39529, 'IV6900676-1', '556ล1020', '59.00', 35.0, '', 59.0, 44.66, 1.0),
(39544, 'IV6900669-2', '900ก5101', '49.00', 14.0, '', 49.0, 38.28, 1.0),
(39552, 'IV6900627-2', '999อ1501', '125.00', 43.0, '', 125.0, 95.79, 1.0),
(39553, 'IV6900633-2', '999อ1501', '35.00', 21.0, '', 35.0, 27.18, 1.0),
(39555, 'IV6900676-2', '999อ1501', '85.00', 35.0, '', 85.0, 64.34, 1.0),
(39584, 'IV6900781-2', '001ก1000', '447.00', 179.0, '', 447.0, 339.6, 3.0),
(39585, 'IV6900749-1', '001ก1100', '149.00', 71.0, '', 149.0, 113.5, 1.0),
(39586, 'IV6900749-2', '001ก1200', '149.00', 71.0, '', 149.0, 113.5, 1.0),
(39589, 'IV6900781-1', '001ก1300', '298.00', 179.0, '', 298.0, 226.4, 2.0),
(39591, 'IV6900770-1', '041ม5880', '9.00', 41.0, '', 9.0, 7.05, 1.0),
(39599, 'IV6900779-2', '556ล1020', '59.00', 40.0, '', 59.0, 45.28, 1.0),
(39602, 'IV6900779-1', '800ป1000', '28.00', 40.0, '', 28.0, 21.49, 2.0),
(39610, 'IV6900770-2', '930บ6320', '180.00', 41.0, '', 180.0, 140.95, 2.0),
(39611, 'IV6900779-3', '999อ1501', '85.00', 40.0, '', 85.0, 65.23, 1.0),
(39723, 'IV6900824-1', '200ก0500', '23.00', 11.0, '', 23.0, 16.51, 1.0),
(39728, 'IV6900824-2', '770ก1120', '16.00', 11.0, '', 16.0, 11.49, 1.0),
(39756, 'IV6900836-1', '046ส9090', '25.00', 15.0, '', 25.0, 17.5, 1.0),
(39757, 'IV6900836-2', '046ส9120', '25.00', 15.0, '', 25.0, 17.5, 1.0),
(39761, 'IV6900841-1', '556ล1020', '59.00', 53.0, '', 59.0, 42.01, 1.0),
(39768, 'IV6900841-2', '999อ1501', '125.00', 53.0, '', 125.0, 88.99, 1.0),
(39838, 'IV6900819-4', '770ก1110', '234.00', 309.0, '', 234.0, 175.12, 18.0),
(39839, 'IV6900819-1', '770ก1120', '288.00', 309.0, '', 288.0, 215.53, 18.0),
(39840, 'IV6900819-2', '770ก1130', '400.00', 309.0, '', 400.0, 299.35, 16.0),
(39841, 'IV6900819-3', '770ก1140', '306.00', 309.0, '', 306.0, 229.0, 9.0),
(39861, 'IV6900902-1', '035ป5506', '12.00', 7.0, '', 12.0, 8.89, 1.0),
(39862, 'IV6900902-2', '035ป5507', '15.00', 7.0, '', 15.0, 11.11, 1.0),
(39879, 'IV6900835-1', '168ถ0015', '350.00', 138.0, '', 350.0, 251.43, 10.0),
(39880, 'IV6900835-2', '168ถ0020', '140.00', 138.0, '', 140.0, 100.57, 4.0),
(39891, 'IV6900915-4', '017ด5115', '36.00', 17.0, '', 36.0, 24.87, 3.0),
(39896, 'IV6900915-3', '603ต2503-21', '19.00', 17.0, '', 19.0, 13.13, 1.0),
(39921, 'IV6900936-3', '556ล1020', '59.00', 45.0, '', 59.0, 42.2, 1.0),
(39924, 'IV6900936-2', '800ป1000', '14.00', 45.0, '', 14.0, 10.01, 1.0),
(39928, 'IV6900936-5', '999อ1501', '85.00', 45.0, '', 85.0, 60.79, 1.0),
(40049, 'IV6901011-1', '168ถ0050', '40.00', 23.0, '', 40.0, 28.5, 1.0),
(40050, 'IV6901011-2', '168ถ0060', '40.00', 23.0, '', 40.0, 28.5, 1.0),
(40054, 'IV6901005-1', '900ก5101', '98.00', 37.0, '', 98.0, 70.74, 2.0);

-- -- precondition -----------------------------------------------------------
DROP TABLE IF EXISTS temp._mig184_drift;
CREATE TEMP TABLE _mig184_drift AS
SELECT s.id
  FROM migration_184_snapshot s
  LEFT JOIN sales_transactions st ON st.id = s.id
 WHERE st.id IS NULL
    OR st.doc_no   IS NOT s.doc_no
    OR st.bsn_code IS NOT s.bsn_code
    OR NOT (
          (COALESCE(st.discount, '') IS s.old_discount     AND st.total IS s.old_total)
       OR (COALESCE(st.discount, '') IS s.correct_discount AND st.total IS s.correct_total)
       );

DROP TRIGGER IF EXISTS _mig184_precondition_guard;
CREATE TEMP TRIGGER _mig184_precondition_guard
BEFORE DELETE ON _mig184_drift
BEGIN
  SELECT RAISE(ABORT, 'mig 184 precondition FAILED: a #525 row drifted to a value this migration did not expect (row missing, doc_no/bsn_code changed, or discount/total hand-edited to something other than the old-wrong or the corrected value). See migration_184_snapshot for the expected shape and re-derive from the DBF before retrying.');
END;

DELETE FROM _mig184_drift;
DROP TRIGGER _mig184_precondition_guard;
DROP TABLE _mig184_drift;

-- -- the correction -----------------------------------------------------------
UPDATE sales_transactions
   SET discount      = (SELECT s.correct_discount FROM migration_184_snapshot s WHERE s.id = sales_transactions.id),
       total         = (SELECT s.correct_total    FROM migration_184_snapshot s WHERE s.id = sales_transactions.id),
       change_token  = 'mig184-525-' || id,
       change_source = 'manual',
       change_actor  = 'ranpo-express/mig184',
       change_reason = 'GH #525: parser column-shift correction, value taken from Express BSN5657 STCRD.DISC/TRNVAL (see migration header)'
 WHERE id IN (SELECT id FROM migration_184_snapshot)
   AND COALESCE(discount, '') IS (SELECT s.old_discount FROM migration_184_snapshot s WHERE s.id = sales_transactions.id)
   AND total IS (SELECT s.old_total FROM migration_184_snapshot s WHERE s.id = sales_transactions.id);

-- -- postcondition -----------------------------------------------------------
-- Stated as the invariant itself (must hold the corrected value, net/qty
-- unmoved), not a row count, so it holds identically whether this run
-- changed 176 rows (first apply) or 0 (re-run / already-fixed DB).
DROP TABLE IF EXISTS temp._mig184_postcheck;
CREATE TEMP TABLE _mig184_postcheck AS
SELECT s.id
  FROM migration_184_snapshot s
  JOIN sales_transactions st ON st.id = s.id
 WHERE COALESCE(st.discount, '') IS NOT s.correct_discount
    OR st.total IS NOT s.correct_total
    OR st.net   IS NOT s.net_at_migration
    OR st.qty   IS NOT s.qty_at_migration;

DROP TRIGGER IF EXISTS _mig184_postcondition_guard;
CREATE TEMP TRIGGER _mig184_postcondition_guard
BEFORE DELETE ON _mig184_postcheck
BEGIN
  SELECT RAISE(ABORT, 'mig 184 postcondition FAILED: a #525 row does not hold the corrected discount/total after the UPDATE, or its net/qty moved when this migration must never touch them.');
END;

DELETE FROM _mig184_postcheck;
DROP TRIGGER _mig184_postcondition_guard;
DROP TABLE _mig184_postcheck;

COMMIT;
