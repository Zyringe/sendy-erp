"""Sendy ERP — business logic + DB queries (raw SQL, no ORM).

The largest file in the app (~4,400 LOC). Holds:

- Product / brand / category readers + pricing summary helpers
  (`get_product_pricing_summary` — the customer-facing price view)
- Transaction ledger helpers (`record_*`, stock-level math)
- BSN sync + mapping (`product_code_mapping`, `unit_conversions`)
- Cost/WACC math: `get_wacc`, `_recompute_wacc_for_product`,
  `get_cost_history`
- HR queries (employees, leave entitlements, payroll runs)
- Commission helpers (shared with `commission.py` engine)
- Receivable/AR aging (used by `cashflow.py` + `/payment-status`)
- Search / filter / sort builders for the listing pages

Conventions:
  - All queries open their own connection via `database.get_connection()`.
  - Functions returning sequences yield sqlite3.Row objects (template-friendly).
  - Money math: read `sendy_erp/CLAUDE.md` for the VAT-aware `billed`
    formula — never write `SUM(net)` directly for ar/cashflow.
  - Do NOT expose `cost_price` to customer-facing routes — use
    `base_sell_price` or `get_product_pricing_summary()`.

Future split: this file is a candidate for domain-based extraction
(models_products.py / models_transactions.py / models_commission.py /
models_hr.py) when next major touch arrives — opportunistic, not big-bang.
"""
import json
import re
import sqlite3

import config
from database import get_connection
import bsn_units
import name_builder
from sku_code_utils import PACKAGING_SHORT, regenerate_for_product
from cashflow import BSN_AR_PREDICATE
from collections import defaultdict
from datetime import date

from ._shared import (
    _set_price_change_source,
    AUDIT_LOG_RETENTION_DAYS, _AUDIT_PRUNE_PREDICATE, prune_audit_log,
    _NOISE_WORDS, _QTY_PREFIX, _clean_for_match, _re_mod,
)
from .products import (
    get_products, get_product, create_product, create_structured_product,
    update_product, deactivate_product,
)
from .brands import (
    get_brands, get_brand, set_product_brand, _topup_pre_feb_for_product,
    create_brand,
)
from .stock import (
    get_stock_alerts, count_stock_alerts, get_product_locations,
    save_product_locations, count_restock_needed, count_active_products,
    count_in_stock,
)
from .transactions import (
    add_transaction, get_current_stock, get_transactions,
    get_recent_transactions, delete_transactions_by_ids,
)
from .promotions import (
    get_promotions, get_active_promotion, effective_price, create_promotion,
    deactivate_promotion, get_product_price_tiers,
)
from .customers import (
    get_customer_summary, get_regions, get_customers,
    get_all_regions_with_counts, update_region, get_active_salespersons,
    get_all_regions, get_orphan_salesperson_codes, get_customer_master,
    _BULK_MAX, update_customer_assignment, bulk_reassign_customers,
    get_customers_master, import_customers_from_bsn, get_customers_for_map,
    save_customer_geocode, get_customer_zones, get_customer_types,
    get_geocode_progress,
)
from .suppliers import get_suppliers, get_supplier_summary
from .wacc import (
    _WACC_INITIAL_DATE, recalculate_product_wacc, get_current_wacc,
    get_cost_history, recalculate_waccs_for_products,
)
from .bsn_sync import (
    to_base_units, _get_base_qty, _sync_bsn_to_stock,
    get_pending_unit_conversions, learn_acronyms_normalize,
    save_unit_conversions, dismiss_pending_unit_conversion,
    update_unit_conversion_ratio, get_all_unit_conversions,
    upsert_unit_conversion,
)
from .mapping import (
    upsert_mapping, get_pending_mappings, resolve_pending_mappings,
    _resolve_mapping, _BSN_LEDGER_NOTE_PATTERNS, _bsn_code_ledger_orphans,
    repoint_bsn_code,
)
from .imports import (
    _detect_removed_lines, preview_import, import_weekly, get_recent_imports,
    get_express_dbf_freshness,
)
from .sales import (
    get_sales, get_purchases_by_doc, get_sales_summary, get_sales_by_doc,
    get_trade_dashboard, get_product_trade_summary, get_purchases,
    get_purchases_summary, get_purchases_summary_by_vat,
)
from .commission import (
    _normalise_override_payload, _validate_override_targets,
    list_commission_overrides, get_commission_override,
    create_commission_override, update_commission_override,
    toggle_commission_override, delete_commission_override,
)
from .payments import (
    parse_payment_csv, import_payments, import_payment_records, get_payment_status,
    get_payment_summary, get_customer_debt_summary, get_ar_reconciliation,
    find_payment_candidates, get_customer_unpaid_bills,
)
from .pricing_ap import (
    get_product_pricing_summary, get_product_pricing, get_ap_outstanding,
)
from .platform_skus import (
    import_platform_skus, import_platform_products,
    _propagate_listings_to_platform_skus, get_platform_skus,
    get_platform_skus_all, get_platform_summary, update_platform_sku,
    get_platform_mapping_data, apply_platform_mapping,
    suggest_platform_mapping, get_marketplace_price_history,
)
from .conversions import (
    get_conversion_formulas, get_conversion_formula, get_buildable,
    upsert_pack_unpack_pair, delete_conversion_formula, find_pair_partner,
    derive_pair_from_formula, get_recent_conversion_runs, run_conversion,
)
from .accounting import get_accounting_summary
from .financial_health import get_break_even, get_current_month_pace
from .ecommerce import (
    import_ecommerce_listings, get_ecommerce_listing_summary,
    get_ecommerce_listings, get_listing_mapping_data, apply_listing_mapping,
    suggest_listing_mapping,
)
from .suggestions import (
    count_pending_suggestions, get_pending_suggestions,
    get_pending_suggestion, save_pending_suggestion,
    approve_pending_suggestion,
)
from .marketplace import (
    resolve_marketplace_product_id, import_marketplace_orders,
    upsert_marketplace_settlements, upsert_marketplace_fees,
    import_wallet_txns, upsert_lazada_settlements, get_payout_years,
    get_payout_summaries, LAZADA_BUCKET, GRANULAR_LABEL,
    _FEE_LABELS, _smart_label, _bucket_fee_lines,
    _fee_pct_str, get_payout_orders, get_deposit_tab_extras,
    get_payout_report, get_settlement_report, get_marketplace_order,
    get_marketplace_order_detail, resolve_line_ratio, get_order_margin,
    _RECON_CUSTOMER, get_marketplace_reconciliation, set_amount_review,
    dismiss_review_order, undismiss_review_order,
    get_marketplace_summary, get_marketplace_orders, get_marketplace_unmapped,
    _CANCEL_RETURN_STATUSES, get_marketplace_returns_cancelled,
    create_payout_batch, _BATCH_TOLERANCE, match_orders_to_amount,
    assign_orders_to_batch, assign_orders_manual, get_deposit_batch_report,
    create_baseline_batch, unassign_batch, get_iv_match_worklist,
)

