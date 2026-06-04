"""
import_platform_products.py — CLI for the Full Shopee/Lazada product-info import.

Usage:
  python scripts/import_platform_products.py shopee
  python scripts/import_platform_products.py lazada
  python scripts/import_platform_products.py all

Reads the 5 product-info xlsx files from the canonical source folders:
  Shopee: E-Commerce/Shopee/sendaibyboonsawat/01_product-info/
  Lazada: E-Commerce/Lazada/01_product-info/

Runs the safe UPSERT import (no DELETE, mappings preserved). The product-grain
and variation-grain upserts each run in their own transaction (two commits, not
one); the import is idempotent and re-runnable, so a mid-run failure is fixed by
re-running and no partial state can drop a mapping. See
marketplace-product-import-spec.md §3 for the contract.

UI wiring (/ecommerce/import multi-file) is a separate future task.
"""
import os
import sys

# Add inventory_app to sys.path so models/parse_platform can be imported directly
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(_HERE, '..', 'inventory_app')
sys.path.insert(0, os.path.normpath(_APP))

os.environ.setdefault('SKIP_DB_INIT', '1')
os.environ.setdefault('SECRET_KEY', 'script-run')
os.environ.setdefault('ADMIN_PASSWORD', 'script-run')

import models
from parse_platform import parse_shopee_product_files, parse_lazada_product_files

_WORKSPACE = os.path.normpath(os.path.join(_HERE, '..', '..'))
_SHOPEE_DIR = os.path.join(_WORKSPACE, 'E-Commerce', 'Shopee', 'sendaibyboonsawat', '01_product-info')
_LAZADA_DIR = os.path.join(_WORKSPACE, 'E-Commerce', 'Lazada', '01_product-info')


def run_shopee():
    print("Parsing Shopee product-info files...")
    prod_rows, var_rows = parse_shopee_product_files(_SHOPEE_DIR)
    print(f"  Parsed {len(prod_rows)} product rows, {len(var_rows)} variation rows")

    print("Upserting platform_products (Shopee)...")
    n_prod = models.import_platform_products('shopee', prod_rows)
    print(f"  platform_products upserted: {n_prod}")

    print("Upserting platform_skus (Shopee)...")
    n_sku, n_prop = models.import_platform_skus('shopee', var_rows)
    print(f"  platform_skus upserted: {n_sku}, propagated mappings: {n_prop}")


def run_lazada():
    print("Parsing Lazada product-info files...")
    prod_rows, var_rows = parse_lazada_product_files(_LAZADA_DIR)
    print(f"  Parsed {len(prod_rows)} product rows, {len(var_rows)} variation rows")

    print("Upserting platform_products (Lazada)...")
    n_prod = models.import_platform_products('lazada', prod_rows)
    print(f"  platform_products upserted: {n_prod}")

    print("Upserting platform_skus (Lazada)...")
    n_sku, n_prop = models.import_platform_skus('lazada', var_rows)
    print(f"  platform_skus upserted: {n_sku}, propagated mappings: {n_prop}")


def main():
    platforms = sys.argv[1:] or ['all']
    target = platforms[0].lower()

    if target in ('shopee', 'all'):
        run_shopee()
    if target in ('lazada', 'all'):
        run_lazada()

    print("\nDone.")


if __name__ == '__main__':
    main()
