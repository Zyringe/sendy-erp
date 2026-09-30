"""Resolve an OLD integer `products.sku` to a product, and say whether that
product is still a legitimate target.

`products.sku` was dropped in mig 097 (#126); `legacy_product_sku_map` is the
forensic table left behind so the migration-era CSV importers can still
translate their key column. Those importers used to take whatever product_id
the table held, with no check that the product still exists as a live record.

Measured on PROD 2026-09-30: **57 of 1,995 legacy skus point at an inactive
product.** Eleven of those carry the `[MERGED→N]` rename a merge leaves behind,
which is the only place the survivor is recorded.

This module deliberately does NOT auto-redirect a merged sku to its survivor.
The rename is a human convention in a free-text column, not a constraint, and
silently re-pointing an import at a product nobody named is how the 07-17→20
mapping audit's wrong rows got there in the first place. `usable` goes False
and `explain()` tells the operator exactly which product to use instead.
"""
import re
from typing import NamedTuple, Optional

# The rename a merge leaves on the loser, e.g. '[MERGED→1968] ดจ.โรตารี่ ...'.
# Both arrows appear in the wild; → is what the current scripts write.
_MERGED_RE = re.compile(r'^\[MERGED\s*(?:→|->)\s*(\d+)\]')


def merged_into(product_name):
    """The survivor's product_id if `product_name` carries the merge rename,
    else None. A plain deactivated product has no survivor — that is a
    different situation from a merged one and must not be conflated."""
    m = _MERGED_RE.match(product_name or '')
    return int(m.group(1)) if m else None


class LegacySku(NamedTuple):
    sku: int
    product_id: int
    product_name: str
    is_active: bool
    merged_into: Optional[int]

    @property
    def usable(self):
        """True only when the target is still a live product. A caller that
        writes a mapping (listing → product, platform sku → product) must
        refuse anything else rather than attach live data to a dead record."""
        return self.is_active

    def explain(self):
        if self.is_active:
            return f'legacy sku {self.sku} -> product {self.product_id}'
        if self.merged_into is not None:
            return (f'legacy sku {self.sku} -> product {self.product_id} '
                    f'({self.product_name!r}) was MERGED into product '
                    f'{self.merged_into}; re-key the source row to {self.merged_into} '
                    f'and re-run')
        return (f'legacy sku {self.sku} -> product {self.product_id} '
                f'({self.product_name!r}) is INACTIVE with no recorded successor; '
                f'decide the right product by hand')


def resolve_legacy_sku(conn, sku):
    """-> LegacySku, or None when the sku is not in the forensic map."""
    row = conn.execute(
        'SELECT m.sku, m.product_id, p.product_name, p.is_active '
        '  FROM legacy_product_sku_map m '
        '  JOIN products p ON p.id = m.product_id '
        ' WHERE m.sku = ?', (sku,)
    ).fetchone()
    if row is None:
        return None
    name = row['product_name']
    return LegacySku(sku=row['sku'], product_id=row['product_id'], product_name=name,
                     is_active=bool(row['is_active']), merged_into=merged_into(name))
