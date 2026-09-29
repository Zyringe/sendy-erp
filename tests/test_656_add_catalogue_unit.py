"""#656 — add a unit word that exists only in the printed catalogue.

`/unit-conversions` only ever offered units that arrived on a bill
(`get_pending_unit_conversions` reads unsynced ledger rows), and the edit
button changes a ratio that already exists. A word that is printed in the
catalogue but has never been billed (pid 791 'ชุด', 1061 'แผ่น', 1107 'กล.',
973 'กล่องเล็ก' on prod) had no way in: the price resolver raised
`ValueError` for it until someone wrote a script.

`models.add_catalogue_unit_conversion` + `POST /unit-conversions/add` are
that way in. The invariants pinned here:

* the row is stored under the หน่วย WORD (`กล.` -> `กล่อง`), because the
  resolver matches stored rows by spelling first;
* an existing ratio is NEVER overwritten from here. Changing a ratio must go
  through `update_unit_conversion_ratio`, which re-syncs the ledger;
* units still waiting on the pending table (unsynced bills) are refused,
  because that table's save is the one that syncs them;
* the pack/loose hazard blocks exactly as it does for every other writer;
* nothing touches stock or the ledger.
"""
import math
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import models
from models import bsn_sync, system_alerts


P = 965601           # unit_type แกลลอน, never billed in กล่อง
BOXED = 965602       # unit_type กล่อง
LONEPACK = 965603    # unit_type แผง, no pack/unpack pair
PACK, LOOSE = 965604, 965605   # an active [แพ็ค]/[แกะ] pair
OFF = 965606         # inactive product


def _seed_map(conn):
    conn.executemany(
        "INSERT INTO unit_map (book, spelling, word) VALUES (?, ?, ?)",
        [('*', 'กล.', 'กล่อง'), ('BSN5657', 'กล', 'กล่อง')])
    conn.commit()


def _seed_products(conn):
    for pid, name, unit, active in (
        (P, 'สีทดสอบ 1 แกลลอน', 'แกลลอน', 1),
        (BOXED, 'ตะปูทดสอบ', 'กล่อง', 1),
        (LONEPACK, 'กลอนแผงทดสอบ', 'แผง', 1),
        (PACK, 'มือจับแผงทดสอบ', 'แผง', 1),
        (LOOSE, 'มือจับตัวทดสอบ', 'ตัว', 1),
        (OFF, 'สินค้าเลิกขายทดสอบ', 'ตัว', 0),
    ):
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, base_sell_price, "
            "  sku_code, is_active) VALUES (?, ?, ?, 100.0, ?, ?)",
            (pid, name, unit, f'SKU-{pid}', active))
    # real stock, so "stock unchanged" can fail
    conn.execute(
        "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note) "
        "VALUES (?, 'IN', 7, 'unit', 'seed')", (P,))
    conn.commit()


@pytest.fixture(autouse=True)
def seeded(empty_db_conn):
    _seed_map(empty_db_conn)
    _seed_products(empty_db_conn)
    return empty_db_conn


def _rows(conn, pid=P):
    return [(r['bsn_unit'], r['ratio']) for r in conn.execute(
        "SELECT bsn_unit, ratio FROM unit_conversions WHERE product_id=? ORDER BY id",
        (pid,))]


def _ledger_state(conn):
    return (conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                         (P,)).fetchone()[0])


def _bill(conn, table, pid, unit, *, synced=0, code='TESTCODE'):
    if table == 'sales_transactions':
        conn.execute(
            "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, "
            "  bsn_code, product_name_raw, customer, customer_code, qty, unit, "
            "  unit_price, vat_type, discount, total, net, synced_to_stock) "
            "VALUES ('2026-09-01', 'IV-T-1', 'IV-T', ?, ?, 'raw', 'C', 'C1', 1, ?, "
            "  10, 0, 0, 10, 10, ?)", (pid, code, unit, synced))
    else:
        conn.execute(
            "INSERT INTO purchase_transactions (date_iso, doc_no, bsn_code, unit, qty, "
            "  net, product_id, synced_to_stock) VALUES ('2026-09-01', 'RR-T-1', ?, ?, "
            "  1, 10, ?, ?)", (code, unit, pid, synced))
    conn.commit()


def _resolve(conn, pid, unit):
    import price_lookup
    return price_lookup.resolve_price(conn, product_id=pid, unit=unit)


# ── ok ────────────────────────────────────────────────────────────────────

def test_a_catalogue_word_is_stored_as_the_word_and_the_resolver_answers(seeded):
    c = seeded
    with pytest.raises(ValueError):          # the bug, before
        _resolve(c, P, 'กล.')
    before = _ledger_state(c)

    result = models.add_catalogue_unit_conversion(P, ' กล. ', 4)

    assert result == {'ok': True, 'stored_as': 'กล่อง', 'typed': 'กล.',
                      'product_name': 'สีทดสอบ 1 แกลลอน', 'unit_type': 'แกลลอน'}
    assert _rows(c) == [('กล่อง', 4.0)]
    out = _resolve(c, P, 'กล.')
    assert out['unit']['ratio'] == 4.0
    assert out['unit']['ratio_source'] == 'unit_conversions'
    assert out['list']['list_for_unit'] == 400.0
    assert _ledger_state(c) == before == (1, 7)


def test_a_word_the_map_does_not_know_is_stored_as_typed(seeded):
    """CONTROL for the normalisation: an unknown word is not dropped or
    rewritten, it is stored exactly as typed (trimmed)."""
    result = models.add_catalogue_unit_conversion(P, 'ชุดใหญ่', 3)
    assert result['ok'] is True and result['stored_as'] == 'ชุดใหญ่'
    assert _rows(seeded) == [('ชุดใหญ่', 3.0)]


# ── exists: never overwrite ───────────────────────────────────────────────

@pytest.mark.parametrize('typed', ['กล่อง', 'กล.'])
def test_an_existing_ratio_is_never_overwritten(seeded, typed):
    c = seeded
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กล่อง', 6)", (P,))
    c.commit()

    result = models.add_catalogue_unit_conversion(P, typed, 12)

    assert result == {'exists': True, 'stored_as': 'กล่อง', 'ratio': 6.0}
    assert _rows(c) == [('กล่อง', 6.0)]


def test_a_twin_spelling_of_the_same_word_counts_as_existing(seeded):
    """A legacy row spelled `กล.` already answers an ask in `กล่อง` (the
    resolver falls back through the map). A second row under the word with a
    different ratio would silently change what asks resolve to, without the
    re-sync the edit button runs."""
    c = seeded
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กล.', 6)", (P,))
    c.commit()
    assert _resolve(c, P, 'กล่อง')['unit']['ratio'] == 6.0   # already answerable

    result = models.add_catalogue_unit_conversion(P, 'กล่อง', 12)

    assert result == {'exists': True, 'stored_as': 'กล.', 'ratio': 6.0}
    assert _rows(c) == [('กล.', 6.0)]


def test_the_insert_itself_cannot_overwrite(seeded, monkeypatch):
    """Check-then-write: a row that appears after the pre-checks and before the
    INSERT (injected at the last seam before the write) must still not be
    overwritten — the INSERT is DO NOTHING, and rowcount 0 reads as exists."""
    real = bsn_sync.cross_unit_hazard

    def _hazard_then_race(conn, product_id, bsn_unit, **kw):
        conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                     "VALUES (?, ?, 6)", (product_id, bsn_unit))
        return real(conn, product_id, bsn_unit, **kw)

    monkeypatch.setattr(bsn_sync, 'cross_unit_hazard', _hazard_then_race)

    result = models.add_catalogue_unit_conversion(P, 'กล.', 12)

    assert result == {'exists': True, 'stored_as': 'กล่อง', 'ratio': 6.0}
    # The stand-in row was written inside the function's own transaction, so
    # it rolls back with it; what matters is that no 12 was ever committed.
    assert _rows(seeded) == []


# ── is_base_unit ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('typed', ['กล่อง', 'กล.'])
def test_the_products_own_unit_needs_no_row(seeded, typed):
    result = models.add_catalogue_unit_conversion(BOXED, typed, 12)
    assert result == {'is_base_unit': True, 'unit_type': 'กล่อง'}
    assert _rows(seeded, BOXED) == []


# ── pending_bills ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('table', ['sales_transactions', 'purchase_transactions'])
@pytest.mark.parametrize('bill_unit', ['กล.', 'กล่อง'])
def test_a_unit_still_waiting_on_bills_belongs_to_the_pending_table(seeded, table, bill_unit):
    c = seeded
    _bill(c, table, P, bill_unit)

    result = models.add_catalogue_unit_conversion(P, 'กล.', 4)

    assert result == {'pending_bills': 1}
    assert _rows(c) == []


def test_synced_and_non_stock_bills_do_not_count_as_pending(seeded):
    """CONTROL for both clauses of the pending check: a synced bill, and a
    non-stock billable line (permanently unsynced by design, never shown on
    the pending table), do not block."""
    c = seeded
    _bill(c, 'sales_transactions', P, 'กล่อง', synced=1)
    _bill(c, 'purchase_transactions', P, 'กล่อง', code='888ค8888')

    result = models.add_catalogue_unit_conversion(P, 'กล.', 4)

    assert result['ok'] is True
    assert _rows(c) == [('กล่อง', 4.0)]


# ── blocked ───────────────────────────────────────────────────────────────

def test_pack_piece_blocks_a_real_ratio(seeded):
    result = models.add_catalogue_unit_conversion(LONEPACK, 'ตัว', 12)
    assert result == {'blocked': {'kind': 'pack_piece', 'product_unit': 'แผง',
                                  'product_id': LONEPACK, 'bsn_unit': 'ตัว',
                                  'product_name': 'กลอนแผงทดสอบ'}}
    assert _rows(seeded, LONEPACK) == []


def test_pack_piece_allows_ratio_one(seeded):
    """CONTROL: the same hazard admits ratio 1 (a unit alias), as it does for
    every other writer."""
    result = models.add_catalogue_unit_conversion(LONEPACK, 'ตัว', 1)
    assert result['ok'] is True
    assert _rows(seeded, LONEPACK) == [('ตัว', 1.0)]


def test_a_pack_pair_blocks_any_ratio(seeded):
    models.upsert_pack_unpack_pair(PACK, LOOSE, 2, 'both')
    result = models.add_catalogue_unit_conversion(PACK, 'ตัว', 1)
    assert result['blocked']['kind'] == 'pair'
    assert result['blocked']['partner_id'] == LOOSE
    assert _rows(seeded, PACK) == []


@pytest.fixture
def impatient_alert_connection(monkeypatch):
    """The alert helper's own fresh connection gives up on a lock in 0.2s
    (same seam as tests/test_conversion_role_alert_durability.py)."""
    import database

    def _quick():
        conn = sqlite3.connect(database.DATABASE_PATH, timeout=0.2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr(system_alerts, 'get_connection', _quick)


def test_a_malformed_formula_blocks_and_its_alert_survives(seeded, impatient_alert_connection):
    """This writer calls cross_unit_hazard while holding BEGIN IMMEDIATE, so a
    fresh connection could never file the alert (#389). It must land."""
    c = seeded
    card = 965607
    c.execute("INSERT INTO products (id, product_name, unit_type) VALUES (?, 'card', 'แผง')", (card,))
    fid = c.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty) VALUES (?,?,1)",
        ('[แพ็ค] มือจับแผงทดสอบ ⟵ 1 ตัว + card', PACK)).lastrowid
    for pid in (LOOSE, card):
        c.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity, role)"
                  " VALUES (?,?,1,NULL)", (fid, pid))
    c.commit()
    open_alerts = ("SELECT dedupe_key FROM system_alerts "
                   "WHERE kind='conversion_role_error' AND resolved_at IS NULL")
    assert c.execute(open_alerts).fetchall() == []

    result = models.add_catalogue_unit_conversion(PACK, 'อัน', 1)

    assert result['blocked']['kind'] == 'configuration_error'
    assert [r[0] for r in c.execute(open_alerts)] == [str(fid)]
    assert _rows(c, PACK) == []


# ── no_product ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('pid', [999999999, OFF])
def test_a_missing_or_inactive_product_is_refused(seeded, pid):
    assert models.add_catalogue_unit_conversion(pid, 'กล่อง', 4) == {'no_product': True}
    assert _rows(seeded, pid) == []


@pytest.mark.parametrize('typed,ratio', [
    ('', 4), ('   ', 4), ('กล่อง', 0), ('กล่อง', -1),
    ('กล่อง', math.inf), ('กล่อง', math.nan),
])
def test_the_model_refuses_input_the_route_should_have_stopped(seeded, typed, ratio):
    with pytest.raises(ValueError):
        models.add_catalogue_unit_conversion(P, typed, ratio)
    assert _rows(seeded) == []


# ── the route ─────────────────────────────────────────────────────────────

def _client(role):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


def _flashes(client):
    with client.session_transaction() as s:
        return list(s.get('_flashes', []))


def test_route_staff_adds_a_word_and_is_told_what_it_was_stored_as(seeded):
    client = _client('staff')
    resp = client.post('/unit-conversions/add',
                       data={'product_id': str(P), 'unit': 'กล.', 'ratio': '4'})

    assert resp.status_code == 302
    assert _rows(seeded) == [('กล่อง', 4.0)]
    (cat, msg), = _flashes(client)
    assert cat == 'success'
    assert '"กล."' in msg and '"กล่อง"' in msg and 'แกลลอน' in msg, msg


def test_route_existing_ratio_is_kept_and_points_at_the_edit_button(seeded):
    c = seeded
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กล่อง', 6)", (P,))
    c.commit()
    client = _client('admin')

    resp = client.post('/unit-conversions/add',
                       data={'product_id': str(P), 'unit': 'กล่อง', 'ratio': '12',
                             'q': 'สี'})

    assert _rows(c) == [('กล่อง', 6.0)]
    (cat, msg), = _flashes(client)
    assert cat == 'warning' and 'แก้ไข' in msg and str(P) in msg, (cat, msg)
    # the page's search filter survives the round trip
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(resp.headers['Location']).query) == {'q': ['สี']}


def test_route_blocked_uses_the_shared_hazard_flash(seeded):
    client = _client('admin')
    client.post('/unit-conversions/add',
                data={'product_id': str(LONEPACK), 'unit': 'ตัว', 'ratio': '12'})

    assert _rows(seeded, LONEPACK) == []
    (cat, msg), = _flashes(client)
    assert cat == 'danger' and 'บันทึกได้เฉพาะ ratio 1' in msg, (cat, msg)


def test_route_pending_bills_is_refused_with_a_reason(seeded):
    _bill(seeded, 'sales_transactions', P, 'กล.')
    client = _client('admin')
    client.post('/unit-conversions/add',
                data={'product_id': str(P), 'unit': 'กล.', 'ratio': '4'})
    assert _rows(seeded) == []
    (cat, msg), = _flashes(client)
    assert cat == 'warning' and 'รอ' in msg, (cat, msg)


@pytest.mark.parametrize('form', [
    {'product_id': str(P), 'unit': 'กล.', 'ratio': '0'},
    {'product_id': str(P), 'unit': 'กล.', 'ratio': '-3'},
    {'product_id': str(P), 'unit': 'กล.', 'ratio': 'inf'},
    {'product_id': str(P), 'unit': 'กล.', 'ratio': 'nan'},
    {'product_id': str(P), 'unit': 'กล.', 'ratio': 'abc'},
    {'product_id': str(P), 'unit': 'กล.'},
    {'product_id': str(P), 'unit': '  ', 'ratio': '4'},
    {'product_id': str(P), 'ratio': '4'},
    {'product_id': 'x', 'unit': 'กล.', 'ratio': '4'},
    {'unit': 'กล.', 'ratio': '4'},
])
def test_route_rejects_bad_input_without_writing(seeded, form):
    client = _client('admin')
    resp = client.post('/unit-conversions/add', data=form)
    assert resp.status_code == 302
    assert seeded.execute("SELECT COUNT(*) FROM unit_conversions").fetchone()[0] == 0
    (cat, _msg), = _flashes(client)
    assert cat == 'danger'


@pytest.mark.parametrize('role', ['shareholder', 'general'])
def test_route_refuses_whoever_the_edit_route_refuses(seeded, role):
    """Same gate as /unit-conversions/edit: whatever that route answers a role
    it refuses, this one answers identically, and writes nothing."""
    from app import app as flask_app
    # CONTROL: an unknown URL is refused by the same gate too, so equality
    # below means something only once the route really exists.
    assert 'bsn.unit_conversions_add' in {r.endpoint for r in flask_app.url_map.iter_rules()}
    seeded.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                   "VALUES (?, 'ถัง', 2)", (P,))
    seeded.commit()

    edit_client = _client(role)
    edit = edit_client.post('/unit-conversions/edit',
                            data={'product_id': str(P), 'bsn_unit': 'ถัง', 'ratio': '3'})
    add_client = _client(role)
    add = add_client.post('/unit-conversions/add',
                          data={'product_id': str(P), 'unit': 'กล.', 'ratio': '4'})

    assert _rows(seeded) == [('ถัง', 2.0)]      # neither route wrote
    assert (add.status_code, add.headers.get('Location')) == \
           (edit.status_code, edit.headers.get('Location'))
    assert _flashes(add_client) == _flashes(edit_client)
    assert add.status_code in (302, 403)


# ── the page ──────────────────────────────────────────────────────────────

def test_page_offers_the_add_form_with_known_words(seeded):
    client = _client('staff')
    resp = client.get('/unit-conversions')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    start = html.index('id="add-catalogue-unit"')
    form = html[start:html.index('</form>', start)]
    assert 'action="/unit-conversions/add"' in form
    assert 'name="csrf_token"' in form
    for name in ('product_id', 'unit', 'ratio'):
        assert f'name="{name}"' in form, name
    datalist = html[html.index('<datalist id="known-unit-words"'):]
    datalist = datalist[:datalist.index('</datalist>')]
    # a word from the map, a product unit_type, and never a raw code
    assert '<option value="กล่อง">' in datalist
    assert '<option value="แกลลอน">' in datalist
    assert 'value="กล."' not in datalist
