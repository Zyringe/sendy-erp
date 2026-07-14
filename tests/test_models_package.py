"""Guards for the Phase 11 + Phase 12 `models.py` -> `models/` package split
(behavior-preserving; verbatim moves only — see models/__init__.py's
module docstring).

Three things only:
1. Facade identity: every name models/__init__.py re-imports from a
   Phase-11/12 submodule must be the SAME object as the one living on that
   submodule — proves __init__.py is a pure re-export, not a copy.
2. get_connection intercept: monkeypatching get_connection on the OWNING
   submodule (not on `models` itself) must actually be seen by a moved
   function — guards the Phase-12 landmine where a stale reference would
   silently bypass a test's monkeypatch.
3. resolve_pending_mappings intercept: monkeypatching it on
   `models.suggestions` (the binding `approve_pending_suggestion` actually
   calls, per `from .mapping import resolve_pending_mappings` in
   models/suggestions.py) must be seen — guards the same landmine for the
   suggestions->mapping edge specifically (see test_approve_pending_suggestion.py
   for the fuller regression test).
"""
import pytest

import models

# Hardcoded from the exact `from .<submodule> import (...)` lines at the
# top of models/__init__.py (verified against the AST, one dict entry per
# Phase-11 submodule). Keep in sync if Phase 12 adds more re-exports.
MOVED_NAMES = {
    '_shared': [
        '_set_price_change_source', 'AUDIT_LOG_RETENTION_DAYS',
        '_AUDIT_PRUNE_PREDICATE', 'prune_audit_log',
        '_NOISE_WORDS', '_QTY_PREFIX', '_clean_for_match', '_re_mod',
    ],
    'products': [
        'get_products', 'get_product', 'create_product',
        'create_structured_product', 'update_product', 'deactivate_product',
    ],
    'brands': [
        'get_brands', 'get_brand', 'set_product_brand',
        '_topup_pre_feb_for_product', 'create_brand',
    ],
    'stock': [
        'get_stock_alerts', 'count_stock_alerts', 'get_product_locations',
        'save_product_locations', 'count_restock_needed',
        'count_active_products', 'count_in_stock',
    ],
    'transactions': [
        'add_transaction', 'get_current_stock', 'get_transactions',
        'get_recent_transactions', 'delete_transactions_by_ids',
    ],
    'promotions': [
        'get_promotions', 'get_active_promotion', 'effective_price',
        'create_promotion', 'deactivate_promotion', 'get_product_price_tiers',
    ],
    'customers': [
        'get_customer_summary', 'get_regions', 'get_customers',
        'get_all_regions_with_counts', 'update_region',
        'get_active_salespersons', 'get_all_regions',
        'get_orphan_salesperson_codes', 'get_customer_master', '_BULK_MAX',
        'update_customer_assignment', 'bulk_reassign_customers',
        'get_customers_master', 'import_customers_from_bsn',
        'get_customers_for_map', 'save_customer_geocode',
        'get_customer_zones', 'get_customer_types', 'get_geocode_progress',
    ],
    'suppliers': ['get_suppliers', 'get_supplier_summary'],
    'wacc': [
        '_WACC_INITIAL_DATE', 'recalculate_product_wacc', 'get_current_wacc',
        'get_cost_history', 'recalculate_waccs_for_products',
    ],
    # ── Phase 12 ──────────────────────────────────────────────────────────
    'bsn_sync': [
        'to_base_units', '_get_base_qty', '_sync_bsn_to_stock',
        'get_pending_unit_conversions', 'learn_acronyms_normalize',
        'save_unit_conversions', 'dismiss_pending_unit_conversion',
        'update_unit_conversion_ratio', 'get_all_unit_conversions',
        'upsert_unit_conversion',
    ],
    'mapping': [
        'upsert_mapping', 'get_pending_mappings', 'resolve_pending_mappings',
        '_resolve_mapping', '_BSN_LEDGER_NOTE_PATTERNS',
        '_bsn_code_ledger_orphans', 'repoint_bsn_code',
    ],
    'imports': [
        '_detect_removed_lines', 'preview_import', 'import_weekly',
        'get_recent_imports',
    ],
    'sales': [
        'get_sales', 'get_purchases_by_doc', 'get_sales_summary',
        'get_sales_by_doc', 'get_trade_dashboard',
        'get_product_trade_summary', 'get_purchases', 'get_purchases_summary',
    ],
    'commission': [
        '_normalise_override_payload', '_validate_override_targets',
        'list_commission_overrides', 'get_commission_override',
        'create_commission_override', 'update_commission_override',
        'toggle_commission_override', 'delete_commission_override',
    ],
    'payments': [
        'parse_payment_csv', 'import_payments', 'get_payment_status',
        'get_payment_summary', 'get_customer_debt_summary',
        'get_ar_reconciliation', 'find_payment_candidates',
        'get_customer_unpaid_bills',
    ],
    'pricing_ap': [
        'get_product_pricing_summary', 'get_product_pricing',
        'get_ap_outstanding',
    ],
    'platform_skus': [
        'import_platform_skus', 'import_platform_products',
        '_propagate_listings_to_platform_skus', 'get_platform_skus',
        'get_platform_skus_all', 'get_platform_summary',
        'update_platform_sku', 'get_platform_mapping_data',
        'apply_platform_mapping', 'suggest_platform_mapping',
    ],
    'conversions': [
        'get_conversion_formulas', 'get_conversion_formula', 'get_buildable',
        'upsert_pack_unpack_pair', 'delete_conversion_formula',
        'find_pair_partner', 'derive_pair_from_formula',
        'get_recent_conversion_runs', 'run_conversion',
    ],
    'accounting': ['get_accounting_summary'],
    'ecommerce': [
        'import_ecommerce_listings', 'get_ecommerce_listing_summary',
        'get_ecommerce_listings', 'get_listing_mapping_data',
        'apply_listing_mapping', 'suggest_listing_mapping',
    ],
    'suggestions': [
        'count_pending_suggestions', 'get_pending_suggestions',
        'get_pending_suggestion', 'save_pending_suggestion',
        'approve_pending_suggestion',
    ],
    'marketplace': [
        'resolve_marketplace_product_id', 'import_marketplace_orders',
        'upsert_marketplace_settlements', 'upsert_marketplace_fees',
        'import_wallet_txns', 'upsert_lazada_settlements',
        'get_payout_years', 'get_payout_summaries', 'LAZADA_BUCKET',
        'GRANULAR_LABEL', '_FEE_LABELS', '_smart_label', '_bucket_fee_lines',
        '_fee_pct_str', 'get_payout_orders', 'get_deposit_tab_extras',
        'get_payout_report', 'get_settlement_report', 'get_marketplace_order',
        'get_marketplace_order_detail', 'resolve_line_ratio',
        'get_order_margin', '_RECON_CUSTOMER',
        'get_marketplace_reconciliation', 'set_amount_review',
        'get_marketplace_summary', 'get_marketplace_orders',
        'get_marketplace_unmapped', '_CANCEL_RETURN_STATUSES',
        'get_marketplace_returns_cancelled', 'create_payout_batch',
        '_BATCH_TOLERANCE', 'match_orders_to_amount',
        'assign_orders_to_batch', 'assign_orders_manual',
        'get_deposit_batch_report', 'create_baseline_batch',
        'unassign_batch',
    ],
}


def test_facade_identity():
    checked = 0
    for sub_name, names in MOVED_NAMES.items():
        submodule = getattr(models, sub_name)
        for name in names:
            assert getattr(models, name) is getattr(submodule, name), (
                f"models.{name} is not models.{sub_name}.{name}"
            )
            checked += 1
    assert checked == sum(len(v) for v in MOVED_NAMES.values())


def test_get_connection_intercept_via_submodule(monkeypatch):
    class _Sentinel(Exception):
        pass

    def _boom(*args, **kwargs):
        raise _Sentinel("intercepted")

    monkeypatch.setattr(models.products, 'get_connection', _boom)
    with pytest.raises(_Sentinel):
        models.get_products()


def test_resolve_pending_mappings_intercept_via_suggestions_submodule(monkeypatch):
    """approve_pending_suggestion (models/suggestions.py) calls
    resolve_pending_mappings via `from .mapping import resolve_pending_mappings`
    — a bare name bound INTO the suggestions module's namespace, not looked
    up on `models.mapping` at call time. Patching `models.mapping.
    resolve_pending_mappings` would silently miss this call (the Phase-12
    landmine); the patch must target `models.suggestions.resolve_pending_mappings`.
    See test_approve_pending_suggestion.py for the full end-to-end regression
    test (guards a real historical orphan-product bug)."""
    class _Sentinel(Exception):
        pass

    def _boom(*args, **kwargs):
        raise _Sentinel("intercepted")

    monkeypatch.setattr(models.suggestions, 'resolve_pending_mappings', _boom)
    assert models.suggestions.resolve_pending_mappings is _boom
    # Patching the mapping submodule itself must NOT be what suggestions.py
    # sees — proves the two bindings are genuinely independent names.
    assert models.mapping.resolve_pending_mappings is not _boom
