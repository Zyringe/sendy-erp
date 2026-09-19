-- Rollback 186 — restore every cell 186 changed from migration_186_snapshot,
-- and re-insert every unit_conversions row 186 deleted from
-- migration_186_uc_deleted, then drop both so sqlite_master returns to its
-- pre-186 shape.
--
-- A cell/row is restored ONLY while it still holds exactly what 186 wrote —
-- same stance as mig 184's rollback: a row someone (or a later migration)
-- changed again after 186 is newer than the snapshot and is left alone,
-- rather than silently clobbering a real edit with 186's stale new_value.
--
-- sales_transactions / purchase_transactions restores go through the
-- mig-173 declared-change path with their own fresh change_token (a plain
-- UPDATE would ABORT under sales_transactions_change_needs_declaration /
-- purchase_transactions_change_needs_declaration).
--
-- Re-runnable: IF NOT EXISTS / IF EXISTS keep a second run a no-op rather
-- than an error on the missing snapshot tables.

PRAGMA busy_timeout = 10000;

BEGIN;

-- ── sales_transactions / purchase_transactions (declared-change path) ────
UPDATE sales_transactions
   SET unit          = (SELECT s.old_value FROM migration_186_snapshot s
                          WHERE s.table_name = 'sales_transactions' AND s.column_name = 'unit'
                            AND s.row_id = sales_transactions.id),
       change_source = 'manual',
       change_actor  = 'mig186-rollback',
       change_token  = 'mig186-rollback-unit-' || id,
       change_reason = 'Rollback of mig 186 (GH #597 unit code cleanup) -- restoring pre-migration unit from migration_186_snapshot'
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'sales_transactions' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'sales_transactions' AND s.column_name = 'unit'
                   AND s.row_id = sales_transactions.id);

UPDATE purchase_transactions
   SET unit          = (SELECT s.old_value FROM migration_186_snapshot s
                          WHERE s.table_name = 'purchase_transactions' AND s.column_name = 'unit'
                            AND s.row_id = purchase_transactions.id),
       change_source = 'manual',
       change_actor  = 'mig186-rollback',
       change_token  = 'mig186-rollback-unit-' || id,
       change_reason = 'Rollback of mig 186 (GH #597 unit code cleanup) -- restoring pre-migration unit from migration_186_snapshot'
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'purchase_transactions' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'purchase_transactions' AND s.column_name = 'unit'
                   AND s.row_id = purchase_transactions.id);

-- ── plain-UPDATE tables: one UPDATE per (table_name, column_name) pair ────
UPDATE products
   SET unit_type = (SELECT s.old_value FROM migration_186_snapshot s
                      WHERE s.table_name = 'products' AND s.column_name = 'unit_type'
                        AND s.row_id = products.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'products' AND column_name = 'unit_type')
   AND unit_type IS (SELECT s.new_value FROM migration_186_snapshot s
                       WHERE s.table_name = 'products' AND s.column_name = 'unit_type'
                         AND s.row_id = products.id);

UPDATE promotions
   SET bundle_unit = (SELECT s.old_value FROM migration_186_snapshot s
                        WHERE s.table_name = 'promotions' AND s.column_name = 'bundle_unit'
                          AND s.row_id = promotions.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'promotions' AND column_name = 'bundle_unit')
   AND bundle_unit IS (SELECT s.new_value FROM migration_186_snapshot s
                         WHERE s.table_name = 'promotions' AND s.column_name = 'bundle_unit'
                           AND s.row_id = promotions.id);

UPDATE product_code_mapping
   SET bsn_unit = (SELECT s.old_value FROM migration_186_snapshot s
                     WHERE s.table_name = 'product_code_mapping' AND s.column_name = 'bsn_unit'
                       AND s.row_id = product_code_mapping.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'product_code_mapping' AND column_name = 'bsn_unit')
   AND bsn_unit IS (SELECT s.new_value FROM migration_186_snapshot s
                      WHERE s.table_name = 'product_code_mapping' AND s.column_name = 'bsn_unit'
                        AND s.row_id = product_code_mapping.id);

UPDATE pending_product_suggestions
   SET bsn_unit = (SELECT s.old_value FROM migration_186_snapshot s
                     WHERE s.table_name = 'pending_product_suggestions' AND s.column_name = 'bsn_unit'
                       AND s.row_id = pending_product_suggestions.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'pending_product_suggestions' AND column_name = 'bsn_unit')
   AND bsn_unit IS (SELECT s.new_value FROM migration_186_snapshot s
                      WHERE s.table_name = 'pending_product_suggestions' AND s.column_name = 'bsn_unit'
                        AND s.row_id = pending_product_suggestions.id);

UPDATE pending_product_suggestions
   SET suggested_unit_type = (SELECT s.old_value FROM migration_186_snapshot s
                                WHERE s.table_name = 'pending_product_suggestions' AND s.column_name = 'suggested_unit_type'
                                  AND s.row_id = pending_product_suggestions.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'pending_product_suggestions' AND column_name = 'suggested_unit_type')
   AND suggested_unit_type IS (SELECT s.new_value FROM migration_186_snapshot s
                                 WHERE s.table_name = 'pending_product_suggestions' AND s.column_name = 'suggested_unit_type'
                                   AND s.row_id = pending_product_suggestions.id);

UPDATE credit_note_imports
   SET unit = (SELECT s.old_value FROM migration_186_snapshot s
                WHERE s.table_name = 'credit_note_imports' AND s.column_name = 'unit'
                  AND s.row_id = credit_note_imports.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'credit_note_imports' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'credit_note_imports' AND s.column_name = 'unit'
                   AND s.row_id = credit_note_imports.id);

UPDATE express_credit_note_lines
   SET unit = (SELECT s.old_value FROM migration_186_snapshot s
                WHERE s.table_name = 'express_credit_note_lines' AND s.column_name = 'unit'
                  AND s.row_id = express_credit_note_lines.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'express_credit_note_lines' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'express_credit_note_lines' AND s.column_name = 'unit'
                   AND s.row_id = express_credit_note_lines.id);

UPDATE express_sales
   SET unit = (SELECT s.old_value FROM migration_186_snapshot s
                WHERE s.table_name = 'express_sales' AND s.column_name = 'unit'
                  AND s.row_id = express_sales.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'express_sales' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'express_sales' AND s.column_name = 'unit'
                   AND s.row_id = express_sales.id);

UPDATE express_sales_order_lines
   SET unit = (SELECT s.old_value FROM migration_186_snapshot s
                WHERE s.table_name = 'express_sales_order_lines' AND s.column_name = 'unit'
                  AND s.row_id = express_sales_order_lines.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'express_sales_order_lines' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'express_sales_order_lines' AND s.column_name = 'unit'
                   AND s.row_id = express_sales_order_lines.id);

UPDATE supplier_catalogue_items
   SET unit = (SELECT s.old_value FROM migration_186_snapshot s
                WHERE s.table_name = 'supplier_catalogue_items' AND s.column_name = 'unit'
                  AND s.row_id = supplier_catalogue_items.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'supplier_catalogue_items' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'supplier_catalogue_items' AND s.column_name = 'unit'
                   AND s.row_id = supplier_catalogue_items.id);

UPDATE supplier_catalogue_price_history
   SET unit = (SELECT s.old_value FROM migration_186_snapshot s
                WHERE s.table_name = 'supplier_catalogue_price_history' AND s.column_name = 'unit'
                  AND s.row_id = supplier_catalogue_price_history.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'supplier_catalogue_price_history' AND column_name = 'unit')
   AND unit IS (SELECT s.new_value FROM migration_186_snapshot s
                 WHERE s.table_name = 'supplier_catalogue_price_history' AND s.column_name = 'unit'
                   AND s.row_id = supplier_catalogue_price_history.id);

UPDATE supplier_product_mapping
   SET erp_unit = (SELECT s.old_value FROM migration_186_snapshot s
                     WHERE s.table_name = 'supplier_product_mapping' AND s.column_name = 'erp_unit'
                       AND s.row_id = supplier_product_mapping.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'supplier_product_mapping' AND column_name = 'erp_unit')
   AND erp_unit IS (SELECT s.new_value FROM migration_186_snapshot s
                      WHERE s.table_name = 'supplier_product_mapping' AND s.column_name = 'erp_unit'
                        AND s.row_id = supplier_product_mapping.id);

UPDATE supplier_product_mapping
   SET supplier_unit = (SELECT s.old_value FROM migration_186_snapshot s
                          WHERE s.table_name = 'supplier_product_mapping' AND s.column_name = 'supplier_unit'
                            AND s.row_id = supplier_product_mapping.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'supplier_product_mapping' AND column_name = 'supplier_unit')
   AND supplier_unit IS (SELECT s.new_value FROM migration_186_snapshot s
                           WHERE s.table_name = 'supplier_product_mapping' AND s.column_name = 'supplier_unit'
                             AND s.row_id = supplier_product_mapping.id);

UPDATE product_price_tiers
   SET qty_label = (SELECT s.old_value FROM migration_186_snapshot s
                      WHERE s.table_name = 'product_price_tiers' AND s.column_name = 'qty_label'
                        AND s.row_id = product_price_tiers.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'product_price_tiers' AND column_name = 'qty_label')
   AND qty_label IS (SELECT s.new_value FROM migration_186_snapshot s
                       WHERE s.table_name = 'product_price_tiers' AND s.column_name = 'qty_label'
                         AND s.row_id = product_price_tiers.id);

-- ── unit_conversions: undo renames, then re-insert deleted (merged) rows ──
UPDATE unit_conversions
   SET bsn_unit = (SELECT s.old_value FROM migration_186_snapshot s
                     WHERE s.table_name = 'unit_conversions' AND s.column_name = 'bsn_unit'
                       AND s.row_id = unit_conversions.id)
 WHERE id IN (SELECT row_id FROM migration_186_snapshot WHERE table_name = 'unit_conversions' AND column_name = 'bsn_unit')
   AND bsn_unit IS (SELECT s.new_value FROM migration_186_snapshot s
                      WHERE s.table_name = 'unit_conversions' AND s.column_name = 'bsn_unit'
                        AND s.row_id = unit_conversions.id);

INSERT INTO unit_conversions (id, product_id, bsn_unit, ratio, created_at)
SELECT id, product_id, bsn_unit, ratio, created_at
  FROM migration_186_uc_deleted d
 WHERE NOT EXISTS (SELECT 1 FROM unit_conversions u WHERE u.id = d.id);

DROP TABLE migration_186_snapshot;
DROP TABLE migration_186_uc_deleted;

COMMIT;
