"""#602 — the product form, promotions, suggestions, the /unit-conversions
pending list and price lookup all read and write units through the unit map.

Spec #595, ADR 0018: a unit's meaning comes from the Express book it came
from, its spelling is Sendy's choice, and ONE database table (`unit_map`,
read via `inventory_app/bsn_units.py`) translates every variant into that
word. #596 built the map; #597 translated the stored rows the importer
already knew. This ticket is the remaining EDGE: everywhere a human types
or a page compares a unit.

Every test here asserts EXTERNAL behaviour — the word stored on a row, the
word a page shows — never which helper was called. `empty_db` clones the
live SCHEMA with zero rows, so `unit_map` starts EMPTY (bsn_units reads an
empty map as "every spelling unknown"); each test seeds exactly the map
rows it asserts on rather than inheriting prod's 44.

⚠ `กก.` (with the dot), `กิโล`, `1กิโล`, `คค`, `ช5` are #610's vocabulary,
not #602's — they are not in the map yet, so this file tests the mechanism
with `กก` / `หล` / `บล`, which ARE. #602's own acceptance criterion names
`กก.`; it will pass unchanged the moment #610 seeds it, and
`test_a_word_the_map_does_not_know_is_stored_as_typed` pins today's
behaviour for it (stored as typed, never dropped).
"""
import os
import sqlite3
import sys

import pytest

# `scripts/` is not on pytest.ini's pythonpath. APPEND it, never insert(0) —
# #489: a scripts/ module sharing a name with an inventory_app one must not
# shadow it. (Without this the catalog-pricing tests below pass only when
# some earlier test happens to have put scripts/ on the path first.)
_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)


# ── helpers ──────────────────────────────────────────────────────────────

def _seed_map(db_path, rows=(('BSN5657', 'หล', 'โหล'),
                             ('BSN5657', 'กก', 'กิโลกรัม'),
                             ('BSN5657', 'บล', 'แผง'))):
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT OR REPLACE INTO unit_map (book, spelling, word) VALUES (?, ?, ?)",
        list(rows))
    conn.commit()
    conn.close()


def _seed_product(db_path, name='สินค้าทดสอบหน่วย', unit_type='ตัว',
                  sku='SK-602-TEST', base=20.0, cost=10.0):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    pid = conn.execute(
        "INSERT INTO products(product_name, units_per_carton, units_per_box, "
        "  unit_type, hard_to_sell, cost_price, opening_cost, base_sell_price, "
        "  low_stock_threshold, sku_code) "
        "VALUES (?, 1, 1, ?, 0, ?, ?, ?, 10, ?)",
        (name, unit_type, cost, cost, base, sku)).lastrowid
    conn.commit()
    conn.close()
    return pid


def _col(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── 1. product create / edit ─────────────────────────────────────────────

def test_create_product_stores_the_word(empty_db):
    """AC: saving a product with a variant stores the หน่วย word."""
    import models
    _seed_map(empty_db)

    pid = models.create_structured_product({'product_name': 'ลวดทดสอบ',
                                            'unit_type': 'กก'}, 'test')

    assert _col(empty_db, "SELECT unit_type FROM products WHERE id=?",
                (pid,))[0]['unit_type'] == 'กิโลกรัม'


def test_a_word_the_map_does_not_know_is_stored_as_typed(empty_db):
    """AC: a genuinely new unit word is still allowed. CONTROL for the test
    above — without it, a bug that stored a constant would look like a pass.
    `กก.` is here deliberately: it is #610's vocabulary, so today it is an
    unknown spelling and must survive rather than be dropped or defaulted."""
    import models
    _seed_map(empty_db)

    known = models.create_structured_product(
        {'product_name': 'ลวดทดสอบ 2', 'unit_type': 'กก'}, 'test')
    unknown = models.create_structured_product(
        {'product_name': 'ห่วงทดสอบ', 'unit_type': 'ห่วงพิเศษ'}, 'test')
    dotted = models.create_structured_product(
        {'product_name': 'ลวดทดสอบ 3', 'unit_type': 'กก.'}, 'test')

    got = {r['id']: r['unit_type'] for r in _col(
        empty_db, "SELECT id, unit_type FROM products WHERE id IN (?,?,?)",
        (known, unknown, dotted))}
    assert got[known] == 'กิโลกรัม'      # control: the map DID fire
    assert got[unknown] == 'ห่วงพิเศษ'
    assert got[dotted] == 'กก.'          # #610 seeds this one


def test_create_product_still_defaults_a_blank_unit_to_tua(empty_db):
    import models
    _seed_map(empty_db)
    pid = models.create_structured_product({'product_name': 'ไม่ระบุหน่วย',
                                            'unit_type': '   '}, 'test')
    assert _col(empty_db, "SELECT unit_type FROM products WHERE id=?",
                (pid,))[0]['unit_type'] == 'ตัว'


def test_create_product_translates_a_padded_variant(empty_db):
    """' กก ' must still translate — the strip has to happen BEFORE the map
    lookup, not after (the map is keyed on the exact spelling)."""
    import models
    _seed_map(empty_db)
    pid = models.create_structured_product({'product_name': 'ลวดเว้นวรรค',
                                            'unit_type': '  กก  '}, 'test')
    assert _col(empty_db, "SELECT unit_type FROM products WHERE id=?",
                (pid,))[0]['unit_type'] == 'กิโลกรัม'


def test_legacy_create_product_also_stores_the_word(empty_db):
    """`models.create_product` has no live caller, but it is exported and the
    census declares it — a revived caller must not reintroduce variants."""
    import models
    _seed_map(empty_db)
    pid = models.create_product({
        'product_name': 'ลวดเก่า', 'units_per_carton': 1, 'units_per_box': 1,
        'unit_type': 'กก', 'hard_to_sell': 0, 'cost_price': 1.0,
        'base_sell_price': 2.0, 'low_stock_threshold': 5})
    assert _col(empty_db, "SELECT unit_type FROM products WHERE id=?",
                (pid,))[0]['unit_type'] == 'กิโลกรัม'


def test_update_product_stores_the_word(empty_db):
    """AC: EDITING a product translates too. `update_product` is the live
    /products/<id>/edit write path."""
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db)

    models.update_product(pid, {'unit_type': 'กก'})

    assert _col(empty_db, "SELECT unit_type FROM products WHERE id=?",
                (pid,))[0]['unit_type'] == 'กิโลกรัม'


def test_update_product_leaves_unit_type_alone_when_absent(empty_db):
    """CONTROL: the whitelist behaviour update_product exists for must not
    change — a save that does not carry unit_type keeps the stored one."""
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, unit_type='แกลลอน')

    models.update_product(pid, {'base_sell_price': 99.0})

    assert _col(empty_db, "SELECT unit_type FROM products WHERE id=?",
                (pid,))[0]['unit_type'] == 'แกลลอน'


def test_product_edit_route_stores_the_word(empty_db):
    """The LIVE path, driven through the real route (a 302 alone proves
    nothing — assert the stored value)."""
    import app as app_module
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-ROUTE')

    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s['role'] = 'admin'
        s['username'] = 'tester'
        s['user_id'] = 1
    resp = client.post(f'/products/{pid}/edit', data={
        'product_name': 'สินค้าทดสอบหน่วย',
        'unit_type': 'กก',
        'cost_price': '10',
        'base_sell_price': '20',
        'low_stock_threshold': '10',
        'units_per_carton': '1',
        'units_per_box': '1',
    })
    assert resp.status_code in (200, 302)
    assert _col(empty_db, "SELECT unit_type FROM products WHERE id=?",
                (pid,))[0]['unit_type'] == 'กิโลกรัม'


# ── 2. the unit suggestion list offers words only ────────────────────────

def test_unit_suggestions_offer_words_not_codes(empty_db):
    import sqlite3 as _s
    import form_options
    _seed_map(empty_db)
    _seed_product(empty_db, name='ก', unit_type='กก', sku='S1')
    _seed_product(empty_db, name='ข', unit_type='กิโลกรัม', sku='S2')
    _seed_product(empty_db, name='ค', unit_type='ตัว', sku='S3')

    conn = _s.connect(empty_db)
    try:
        opts = form_options.units(conn)
    finally:
        conn.close()

    assert 'กก' not in opts, opts            # the code is gone
    assert 'กิโลกรัม' in opts, opts          # CONTROL: its word is offered
    assert 'ตัว' in opts, opts               # CONTROL: untouched words survive
    assert len(opts) == len(set(opts)), opts  # one entry per word


def test_mapping_page_unit_options_contain_no_codes(empty_db):
    """Render test on the ELEMENT itself: mapping.html builds the combo's
    option list as `units: [...]` inside its own <script>. Asserted on that
    array, not on the whole page, with a control."""
    import json
    import re
    import app as app_module
    _seed_map(empty_db)
    _seed_product(empty_db, name='ก', unit_type='กก', sku='S1')
    _seed_product(empty_db, name='ข', unit_type='ตัว', sku='S2')

    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s['role'] = 'admin'
        s['username'] = 'tester'
        s['user_id'] = 1
    html = client.get('/mapping').get_data(as_text=True)

    m = re.search(r'\n\s*units:\s*\[(.*?)\],\n', html, re.S)
    assert m, 'the units option array did not render at all'
    arr = m.group(1)
    # `|tojson` \u-escapes Thai, so compare the way the BROWSER reads it.
    def js(x):
        return json.dumps(x)
    assert js('ตัว') in arr, arr             # CONTROL: the array has content
    assert js('กก') not in arr, arr
    assert js('กิโลกรัม') in arr, arr


# ── 3. promotion bundle unit ─────────────────────────────────────────────

def test_replace_promotion_stores_the_bundle_unit_word(empty_db):
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-PROMO')

    models.replace_promotion(pid, {
        'promo_name': 'โปรทดสอบ', 'promo_type': 'bundle',
        'bundle_buy': 10, 'bundle_free': 1, 'bundle_unit': 'หล',
        'date_start': '2026-01-01', 'date_end': '2026-12-31',
    }, '2026-01-01')

    rows = _col(empty_db, "SELECT bundle_unit FROM promotions WHERE product_id=?", (pid,))
    assert [r['bundle_unit'] for r in rows] == ['โหล']


def test_create_promotion_stores_the_bundle_unit_word(empty_db):
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-PROMO2')

    models.create_promotion({
        'product_id': pid,
        'promo_name': 'โปรทดสอบ 2', 'promo_type': 'bundle',
        'bundle_buy': 10, 'bundle_free': 1, 'bundle_unit': 'บล',
        'date_start': '2026-01-01', 'date_end': '2026-12-31',
    })

    rows = _col(empty_db, "SELECT bundle_unit FROM promotions WHERE product_id=?", (pid,))
    assert [r['bundle_unit'] for r in rows] == ['แผง']


def test_promotion_bundle_unit_unknown_word_survives(empty_db):
    """CONTROL for the two above."""
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-PROMO3')
    models.replace_promotion(pid, {
        'promo_name': 'โปรทดสอบ 3', 'promo_type': 'bundle',
        'bundle_buy': 10, 'bundle_free': 1, 'bundle_unit': 'ห่วงพิเศษ',
        'date_start': '2026-01-01', 'date_end': '2026-12-31',
    }, '2026-01-01')
    rows = _col(empty_db, "SELECT bundle_unit FROM promotions WHERE product_id=?", (pid,))
    assert [r['bundle_unit'] for r in rows] == ['ห่วงพิเศษ']


# ── 4. unit_conversions writers ──────────────────────────────────────────

def test_save_unit_conversions_stores_the_word(empty_db):
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-UC')

    res = models.save_unit_conversions([{'product_id': pid,
                                         'bsn_unit': 'หล', 'ratio': 12.0}])

    assert res['saved'] == 1, res
    rows = _col(empty_db, "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=?", (pid,))
    assert [(r['bsn_unit'], r['ratio']) for r in rows] == [('โหล', 12.0)]


def test_save_unit_conversions_keeps_the_spelling_the_ledger_still_uses(empty_db):
    """The guard: `_get_base_qty` matches `unit_conversions.bsn_unit` against
    the ledger row's own `unit` EXACTLY. Storing the word for a row that
    still says `หล` would leave it permanently unsynced (silently — it just
    never posts). While a raw spelling is still in the ledger, its
    conversion follows it. #600/#610 relabel those rows; then this branch
    stops firing."""
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-UC2')
    conn = sqlite3.connect(empty_db)
    conn.execute(
        "INSERT INTO purchase_transactions (date_iso, doc_no, bsn_code, unit, qty, "
        "  net, product_id, synced_to_stock) "
        "VALUES ('2026-01-01','RR1','X1','หล',1,10,?,0)", (pid,))
    conn.commit()
    conn.close()

    models.save_unit_conversions([{'product_id': pid, 'bsn_unit': 'หล', 'ratio': 12.0}])

    rows = _col(empty_db, "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,))
    assert [r['bsn_unit'] for r in rows] == ['หล']
    # and the row it exists for actually synced
    got = _col(empty_db, "SELECT synced_to_stock FROM purchase_transactions "
                         "WHERE product_id=?", (pid,))
    assert got[0]['synced_to_stock'] == 1


def test_upsert_unit_conversion_stores_the_word(empty_db):
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-UC3')

    assert models.upsert_unit_conversion(pid, 'หล', 12.0) is True

    rows = _col(empty_db, "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,))
    assert [r['bsn_unit'] for r in rows] == ['โหล']


def test_upsert_unit_conversion_unknown_code_survives(empty_db):
    """CONTROL: a code the map does not know is still stored (it goes to
    /unit-conversions for Put to name), not dropped."""
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-UC4')
    assert models.upsert_unit_conversion(pid, 'ซซ', 3.0) is True
    rows = _col(empty_db, "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,))
    assert [r['bsn_unit'] for r in rows] == ['ซซ']


# ── 5. the /unit-conversions pending list groups by word ─────────────────

def test_pending_unit_conversions_group_by_word(empty_db):
    """AC: one group per หน่วย. Two spellings of the same unit on one
    product are ONE pending row, and the row names both spellings so the
    ratio it collects reaches every ledger row behind it."""
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-PEND')
    conn = sqlite3.connect(empty_db)
    conn.executemany(
        "INSERT INTO purchase_transactions (date_iso, doc_no, bsn_code, unit, qty, "
        "  net, product_id, synced_to_stock) VALUES (?,?,?,?,?,?,?,0)",
        [('2026-01-01', 'RR1', 'X1', 'หล', 1, 10, pid),
         ('2026-01-02', 'RR2', 'X1', 'โหล', 1, 10, pid)])
    conn.commit()
    conn.close()

    pending = [p for p in models.get_pending_unit_conversions()
               if p['product_id'] == pid]

    assert len(pending) == 1, pending
    assert pending[0]['bsn_unit'] == 'โหล'
    assert sorted(pending[0]['spellings']) == ['หล', 'โหล']
    assert pending[0]['row_count'] == 2


def test_pending_unit_conversions_keep_distinct_units_apart(empty_db):
    """CONTROL for the grouping: two DIFFERENT units stay two rows."""
    import models
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-PEND2')
    conn = sqlite3.connect(empty_db)
    conn.executemany(
        "INSERT INTO purchase_transactions (date_iso, doc_no, bsn_code, unit, qty, "
        "  net, product_id, synced_to_stock) VALUES (?,?,?,?,?,?,?,0)",
        [('2026-01-01', 'RR1', 'X1', 'หล', 1, 10, pid),
         ('2026-01-02', 'RR2', 'X1', 'บล', 1, 10, pid)])
    conn.commit()
    conn.close()

    pending = sorted((p['bsn_unit'] for p in models.get_pending_unit_conversions()
                      if p['product_id'] == pid))
    assert pending == ['แผง', 'โหล']


def test_unit_conversions_save_route_reaches_every_spelling_in_a_group(empty_db):
    """The grouped row posts ONE ratio under the word; the route has to
    apply it to every raw spelling behind that group, or the rows spelled
    the other way stay unsynced forever."""
    import app as app_module
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-PEND3')
    conn = sqlite3.connect(empty_db)
    conn.executemany(
        "INSERT INTO purchase_transactions (date_iso, doc_no, bsn_code, unit, qty, "
        "  net, product_id, synced_to_stock) VALUES (?,?,?,?,?,?,?,0)",
        [('2026-01-01', 'RR1', 'X1', 'หล', 1, 10, pid),
         ('2026-01-02', 'RR2', 'X1', 'โหล', 1, 10, pid)])
    conn.commit()
    conn.close()

    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s['role'] = 'admin'
        s['username'] = 'tester'
        s['user_id'] = 1
    client.post('/unit-conversions/save', data={f'ratio_{pid}_โหล': '12'})

    stored = sorted(r['bsn_unit'] for r in _col(
        empty_db, "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,)))
    assert stored == ['หล', 'โหล'], stored
    synced = [r['synced_to_stock'] for r in _col(
        empty_db, "SELECT synced_to_stock FROM purchase_transactions WHERE product_id=?",
        (pid,))]
    assert synced == [1, 1], synced


# ── 6. suggestions ───────────────────────────────────────────────────────

def _seed_user(db_path, uid=1):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, role) "
                 "VALUES (?, 'tester', 'x', 'admin')", (uid,))
    conn.commit()
    conn.close()
    return uid


def _suggestion(bsn_code, **over):
    d = {
        'bsn_code': bsn_code, 'bsn_name': 'ของทดสอบ',
        'suggested_name': 'ของทดสอบ ' + bsn_code, 'category': None,
        'series': None, 'brand_id': None, 'model': None, 'size': None,
        'color_th': None, 'color_code': None, 'packaging': None,
        'condition': None, 'pack_variant': None, 'suggested_cost': 0.0,
        'suggested_unit_type': 'ตัว', 'units_per_carton': 1, 'units_per_box': 1,
        'bsn_unit': None, 'unit_conversion_ratio': None,
    }
    d.update(over)
    return d



def test_save_pending_suggestion_stores_words(empty_db):
    import models
    _seed_map(empty_db)

    _seed_user(empty_db)
    sid = models.save_pending_suggestion(_suggestion(
        'ZZ999', suggested_unit_type='กก', bsn_unit='หล',
        unit_conversion_ratio=12.0), user_id=1)

    row = _col(empty_db, "SELECT bsn_unit, suggested_unit_type "
                         "FROM pending_product_suggestions WHERE id=?", (sid,))[0]
    assert row['suggested_unit_type'] == 'กิโลกรัม'
    assert row['bsn_unit'] == 'โหล'


def test_approve_pending_suggestion_stores_the_conversion_word(empty_db):
    import models
    _seed_map(empty_db)
    _seed_user(empty_db)
    sid = models.save_pending_suggestion(_suggestion(
        'ZZ998', suggested_unit_type='ตัว', bsn_unit='หล',
        unit_conversion_ratio=12.0), user_id=1)

    new_pid = models.approve_pending_suggestion(sid, {}, reviewer_id=1)

    rows = _col(empty_db, "SELECT bsn_unit FROM unit_conversions WHERE product_id=?",
                (new_pid,))
    assert [r['bsn_unit'] for r in rows] == ['โหล']


def test_approve_translates_a_suggestion_staged_before_the_map_knew_it(empty_db):
    """The approve-side guard on its own. The test above cannot see it: it
    stages through `save_pending_suggestion`, which already translated, so
    approve's own call is a no-op there (caught by break-it-once — deleting
    it left that test green). A row staged BEFORE the map learnt the
    spelling is the real case, and it is inserted here directly."""
    import models
    _seed_map(empty_db)
    _seed_user(empty_db)
    conn = sqlite3.connect(empty_db)
    sid = conn.execute(
        "INSERT INTO pending_product_suggestions "
        "  (bsn_code, bsn_name, suggested_name, suggested_unit_type, "
        "   bsn_unit, unit_conversion_ratio, suggested_by_user_id, status) "
        "VALUES ('ZZ997', 'ของเก่า', 'ของเก่า', 'ตัว', 'หล', 12.0, 1, 'pending')"
    ).lastrowid
    conn.commit()
    conn.close()

    new_pid = models.approve_pending_suggestion(sid, {}, reviewer_id=1)

    rows = _col(empty_db, "SELECT bsn_unit FROM unit_conversions WHERE product_id=?",
                (new_pid,))
    assert [r['bsn_unit'] for r in rows] == ['โหล']


def test_bsn_suggest_counts_a_unit_once_per_word(empty_db):
    """AC: a product billed in `หล` and `โหล` is ONE unit, not two — the
    length of this list is what flags a split-unit code."""
    import sqlite3 as _s
    import bsn_suggest
    _seed_map(empty_db)
    conn = _s.connect(empty_db)
    conn.row_factory = _s.Row
    conn.executemany(
        "INSERT INTO purchase_transactions (date_iso, doc_no, bsn_code, unit, qty, net) "
        "VALUES (?,?,?,?,?,?)",
        [('2026-01-01', 'RR1', 'ZZ100', 'หล', 1, 10),
         ('2026-03-01', 'RR2', 'ZZ100', 'โหล', 1, 10),
         ('2026-02-01', 'RR3', 'ZZ100', 'บล', 1, 10)])
    conn.commit()
    try:
        seen = bsn_suggest._all_units_seen(conn, 'ZZ100')
    finally:
        conn.close()

    units = [s['unit'] for s in seen]
    assert units == ['โหล', 'แผง'], seen        # merged, still latest-first
    assert seen[0]['last_date'] == '2026-03-01'  # the group keeps its latest


# ── 7. price lookup resolves any spelling ────────────────────────────────

def _resolve(db_path, **kw):
    """`resolve_price` on its own connection over the temp DB."""
    import price_lookup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return price_lookup.resolve_price(conn, **kw)
    finally:
        conn.close()



def _seed_priced_product(db_path):
    pid = _seed_product(db_path, name='สินค้าราคา', sku='SK-602-PRICE',
                        base=10.0, cost=5.0)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
                 "VALUES (?, '1 โหล', 100.0)", (pid,))
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                 "VALUES (?, 'โหล', 12.0)", (pid,))
    conn.commit()
    conn.close()
    return pid


@pytest.mark.parametrize('asked', ['โหล', '1 โหล', 'หล'])
def test_price_lookup_finds_the_tier_in_any_spelling(empty_db, asked):
    _seed_map(empty_db)
    pid = _seed_priced_product(empty_db)

    out = _resolve(empty_db, product_id=pid, unit=asked)

    assert out['answer']['unit'] == 'โหล', out['answer']
    assert out['answer']['price_per_unit'] == 100.0, out['answer']


def test_price_lookup_finds_a_word_tier_when_asked_by_code(empty_db):
    """AC: asking in a variant finds a tier stored under the word. `กิโล`
    is #610's spelling; `กก` is the one the map holds today and proves the
    same mechanism."""
    _seed_map(empty_db)
    pid = _seed_product(empty_db, name='ลวดราคา', unit_type='ตัว',
                        sku='SK-602-KG', base=10.0, cost=5.0)
    conn = sqlite3.connect(empty_db)
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
                 "VALUES (?, '1 กิโลกรัม', 250.0)", (pid,))
    conn.commit()
    conn.close()

    out = _resolve(empty_db, product_id=pid, unit='กก')

    assert out['answer']['unit'] == 'กิโลกรัม'
    assert out['answer']['price_per_unit'] == 250.0


def test_price_lookup_finds_a_variant_tier_when_asked_by_the_word(empty_db):
    """The TIER side of the same coin, and the half the ask-normalisation
    alone does NOT cover: the tier is still labelled with a variant and the
    ask is the หน่วย word. This is the state #600/#610 clean up — a tier
    label written before the map knew the spelling — and it is the only
    thing `_find_matching_tier`'s map fallback exists for. (Caught by
    break-it-once: deleting that fallback left every other price test
    green, because those all normalise the ASK onto a tier already stored
    as the word.)"""
    _seed_map(empty_db)
    pid = _seed_product(empty_db, name='สินค้าเทียร์โค้ด', unit_type='ตัว',
                        sku='SK-602-TIERCODE', base=10.0, cost=5.0)
    conn = sqlite3.connect(empty_db)
    conn.execute("INSERT INTO product_price_tiers (product_id, qty_label, price) "
                 "VALUES (?, '1 หล', 100.0)", (pid,))
    conn.commit()
    conn.close()

    out = _resolve(empty_db, product_id=pid, unit='โหล')

    assert out['answer']['price_per_unit'] == 100.0, out['answer']
    assert out['list']['list_source'] == 'tier', out['list']


def test_price_lookup_still_refuses_a_unit_nothing_can_answer(empty_db):
    """CONTROL for the two above: the map must not turn the C1 'no ratio
    known' refusal into a silent 1.0."""
    _seed_map(empty_db)
    pid = _seed_priced_product(empty_db)
    with pytest.raises(ValueError):
        _resolve(empty_db, product_id=pid, unit='ลัง')


def test_price_lookup_resolves_a_conversion_stored_under_a_code(empty_db):
    """The other direction: the ASK is the word and the STORED conversion is
    still a code (the state #600/#610 clear). Both sides go through the map,
    so the ratio is found either way."""
    _seed_map(empty_db)
    pid = _seed_product(empty_db, name='สินค้าโค้ด', sku='SK-602-CODE',
                        base=10.0, cost=5.0)
    conn = sqlite3.connect(empty_db)
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                 "VALUES (?, 'หล', 12.0)", (pid,))
    conn.commit()
    conn.close()

    out = _resolve(empty_db, product_id=pid, unit='โหล')

    assert out['unit']['ratio'] == 12.0
    assert out['unit']['ratio_source'] == 'unit_conversions'


def test_price_lookup_answers_twin_conversions_the_same_either_way(empty_db):
    """Twin rows (`หล` 12 and `โหล` 6) exist until #600 merges them. Asking
    in EITHER spelling must give the SAME answer — one หน่วย, one price —
    and it must be the WORD row's ratio, deterministically, never whichever
    row the table scan hands back first. (`หล` already resolved to the
    `โหล` row before this change, through the hand-coded alias list; this
    pins that it still does.)"""
    _seed_map(empty_db)
    pid = _seed_product(empty_db, name='สินค้าคู่แฝด', sku='SK-602-TWIN',
                        base=10.0, cost=5.0)
    conn = sqlite3.connect(empty_db)
    conn.executemany("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                     "VALUES (?, ?, ?)", [(pid, 'หล', 12.0), (pid, 'โหล', 6.0)])
    conn.commit()
    conn.close()

    assert _resolve(empty_db, product_id=pid, unit='โหล')['unit']['ratio'] == 6.0
    assert _resolve(empty_db, product_id=pid, unit='หล')['unit']['ratio'] == 6.0


def test_price_lookup_last_paid_matches_a_bill_spelled_differently(empty_db):
    """AC: 'a price asked in any spelling finds ... the customer's last
    price'. The bill says `หล`, the ask says `โหล`."""
    _seed_map(empty_db)
    pid = _seed_priced_product(empty_db)
    conn = sqlite3.connect(empty_db)
    conn.execute("INSERT INTO customers (code, name) VALUES ('C602', 'ลูกค้าทดสอบ')")
    conn.execute(
        # `customer` (the NAME) is load-bearing: price_evidence_filter's
        # marketplace exclusion is `customer NOT LIKE 'หน้าร้าน%'`, and NULL
        # NOT LIKE ... is NULL, so a row without it is silently dropped.
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, customer, "
        "  customer_code, bsn_code, unit, qty, net, vat_type, product_id) "
        "VALUES (date('now','-30 days'), 'IV602-1', 'IV602', 'ลูกค้าทดสอบ', "
        "        'C602', 'X1', 'หล', 2, 180.0, 0, ?)",
        (pid,))
    conn.commit()
    conn.close()

    out = _resolve(empty_db, product_id=pid, unit='โหล', customer_code='C602')

    assert out['answer']['basis'] == 'last_paid', out['answer']
    assert out['answer']['price_per_unit'] == 90.0, out['answer']


# ── 8. the catalog-pricing importer ──────────────────────────────────────

def test_catalog_pricing_plans_tier_and_bundle_units_as_words(empty_db):
    """`_build_ops` is the choke point: every unit the CSV carries is
    translated BEFORE the plan is reconciled against the DB, so the tier
    the importer compares, writes and re-reads is the same string."""
    import sqlite3 as _s
    import import_catalog_pricing as icp
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-CSV')

    conn = _s.connect(empty_db)
    conn.row_factory = _s.Row
    try:
        ops, _meta = icp._build_ops(conn, [{
            'product_id': str(pid), 'base_sell_price': '',
            'tier1_qty_label': '1 หล', 'tier1_price': '100', 'tier1_note': '',
            'promo_type': 'bundle', 'promo_value': '',
            'bundle_buy': '10', 'bundle_free': '1', 'bundle_unit': 'หล',
        }], '2026-09-21', None)
    finally:
        conn.close()

    assert [t[3] for t in ops['tiers']] == ['1 โหล'], ops['tiers']
    assert [p[1]['bundle_unit'] for p in ops['promo_insert']] == ['โหล']


def test_catalog_pricing_keeps_an_unknown_tier_label_as_written(empty_db):
    """CONTROL for the test above."""
    import sqlite3 as _s
    import import_catalog_pricing as icp
    _seed_map(empty_db)
    pid = _seed_product(empty_db, sku='SK-602-CSV2')

    conn = _s.connect(empty_db)
    conn.row_factory = _s.Row
    try:
        ops, _meta = icp._build_ops(conn, [{
            'product_id': str(pid), 'base_sell_price': '',
            'tier1_qty_label': '1 ห่วงพิเศษ', 'tier1_price': '100', 'tier1_note': '',
        }], '2026-09-21', None)
    finally:
        conn.close()

    assert [t[3] for t in ops['tiers']] == ['1 ห่วงพิเศษ'], ops['tiers']


# ── 9. the tier-label splitter the importer and price lookup share ───────

@pytest.mark.parametrize('label,expected', [
    ('1 หล', '1 โหล'),
    ('1หล', '1โหล'),
    ('200 หล', '200 โหล'),
    ('โหล', 'โหล'),
    ('1 โหลคู่', '1 โหลคู่'),      # a DIFFERENT unit — never collapsed
    ('1 โหล (special)', '1 โหล (special)'),
    ('', ''),
])
def test_normalize_tier_label_keeps_the_count_and_translates_the_unit(empty_db, label, expected):
    import bsn_units
    _seed_map(empty_db)
    conn = sqlite3.connect(empty_db)
    try:
        assert bsn_units.normalize_tier_label(label, conn=conn) == expected
    finally:
        conn.close()
