"""Guards for the Phase 11 `models.py` -> `models/` package split
(behavior-preserving; verbatim moves only — see models/__init__.py's
module docstring).

Two things only:
1. Facade identity: every name models/__init__.py re-imports from a
   Phase-11 submodule must be the SAME object as the one living on that
   submodule — proves __init__.py is a pure re-export, not a copy.
2. get_connection intercept: monkeypatching get_connection on the OWNING
   submodule (not on `models` itself) must actually be seen by a moved
   function — guards the Phase-12 landmine where a stale reference would
   silently bypass a test's monkeypatch.
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
