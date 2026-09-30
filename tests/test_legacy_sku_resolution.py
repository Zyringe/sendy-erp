"""A legacy integer sku must never silently resolve to a product that is gone.

`products.sku` was dropped in mig 097 and `legacy_product_sku_map` is the
forensic translation table left behind. Three migration-era scripts still read
it, and all three took whatever product_id the table held — including products
that have since been deactivated or merged away.

Measured on PROD 2026-09-30: **57 of 1,995 legacy skus point at an inactive
product**, 11 of which carry the `[MERGED→N]` rename that says where the goods
actually went. `import_listing_mapping_csv.py` even asked for the active one
first and then deliberately fell back to the unrestricted lookup, so a merged
product won on the second try and a listing got attached to a dead record —
exactly the mis-mapping class the 07-17→20 mapping audit had to clean up.
"""
import os
import sys

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import legacy_sku  # noqa: E402


def _seed(conn, rows):
    """rows: (sku, pid, name, is_active)"""
    for sku, pid, name, active in rows:
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, cost_price, "
            "base_sell_price, is_active) VALUES (?,?,'ตัว',0,0,?)", (pid, name, active))
        conn.execute("INSERT INTO legacy_product_sku_map (product_id, sku) VALUES (?,?)",
                     (pid, sku))
    conn.commit()


def test_active_target_resolves(empty_db_conn):
    _seed(empty_db_conn, [(995, 958, 'ดจ.โรตารี่ Sendai 16x210', 1)])
    hit = legacy_sku.resolve_legacy_sku(empty_db_conn, 995)
    assert hit.product_id == 958
    assert hit.is_active is True
    assert hit.merged_into is None
    assert hit.usable is True


def test_merged_target_is_not_usable_and_names_the_survivor(empty_db_conn):
    _seed(empty_db_conn, [(995, 958, '[MERGED→1968] ดจ.โรตารี่ Sendai 16x210', 0)])
    hit = legacy_sku.resolve_legacy_sku(empty_db_conn, 995)
    assert hit.product_id == 958
    assert hit.is_active is False
    assert hit.merged_into == 1968, 'the survivor is recorded in the product name'
    assert hit.usable is False, 'a merged-away product must never win a mapping'


def test_plain_inactive_target_is_not_usable_and_has_no_survivor(empty_db_conn):
    _seed(empty_db_conn, [(303, 294, 'ดจ.โรตารี่ Sendai 19inx210mm', 0)])
    hit = legacy_sku.resolve_legacy_sku(empty_db_conn, 303)
    assert hit.is_active is False
    assert hit.merged_into is None, 'deactivated is not the same as merged'
    assert hit.usable is False


def test_unknown_sku_returns_none(empty_db_conn):
    assert legacy_sku.resolve_legacy_sku(empty_db_conn, 999999) is None


def test_explain_is_operator_readable(empty_db_conn):
    """The scripts print this line instead of silently skipping a row."""
    _seed(empty_db_conn, [(995, 958, '[MERGED→1968] ดจ.โรตารี่ Sendai 16x210', 0)])
    msg = legacy_sku.resolve_legacy_sku(empty_db_conn, 995).explain()
    assert '995' in msg and '958' in msg and '1968' in msg


@pytest.mark.parametrize('name, want', [
    ('[MERGED→1968] ดจ.โรตารี่ Sendai 16x210', 1968),
    ('[MERGED→474] โครงเลื่อยคันธนู+ใบ #GL4010-30in', 474),
    ('ดจ.โรตารี่ Sendai 19inx210mm', None),
    ('MERGED 1968 no brackets', None),
    ('[MERGED→] empty', None),
    ('[MERGED->1968] ascii arrow', 1968),
])
def test_merged_into_parsing(name, want):
    assert legacy_sku.merged_into(name) == want
