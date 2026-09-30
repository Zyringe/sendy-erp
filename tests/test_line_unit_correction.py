"""แก้หน่วยบรรทัด (#692): preview, apply and cancel on one sales line.

The line under test is IV6900001-1, 2 โหล of a product whose base unit is
หลอด (โหล = 12): the ledger holds -24 and the correction makes it -2.
"""
import datetime

import pytest

from tests import unit_correction_scenario as sc

LINE = 'IV6900001-1'


@pytest.fixture
def db(empty_db):
    sc.seed_company(empty_db)
    return empty_db


@pytest.fixture
def line(db, monkeypatch):
    """The standard book imported: (db path, product id)."""
    pid = sc.seed_product(db)
    sc.run_zip(monkeypatch, sc.standard_book())
    assert sc.sale_ledger(db, LINE, pid)[0][1] == -24
    assert sc.stock(db, pid) == 176
    return db, pid


def _apply(path, mode, *, doc_no=LINE, code=sc.CODE, unit='หลอด', reason=sc.REASON):
    import line_unit_correction as luc
    c = sc.conn(path)
    try:
        return luc.apply(c, doc_no, code, unit, mode, reason, 'put')
    finally:
        c.close()


def _cancel(path, correction_id, reason='ยกเลิกเพราะแก้ผิดบรรทัด'):
    import line_unit_correction as luc
    c = sc.conn(path)
    try:
        luc.cancel(c, correction_id, reason, 'put')
    finally:
        c.close()


def _preview(path, *, doc_no=LINE, code=sc.CODE, unit='หลอด'):
    import line_unit_correction as luc
    c = sc.conn(path)
    try:
        return luc.preview(c, doc_no, code, unit)
    finally:
        c.close()


# ── apply ────────────────────────────────────────────────────────────────────

def test_hold_corrects_the_ledger_row_in_place_and_offsets_it_at_the_sale(line):
    path, pid = line
    (ledger_id, _qty), = sc.sale_ledger(path, LINE, pid)
    cost_before = sc.cost_ledger(path, pid)
    assert len(cost_before) == 2

    cid = _apply(path, 'hold')

    assert sc.sales_row(path, LINE)['unit'] == 'หลอด'
    assert sc.sales_row(path, LINE)['synced_to_stock'] == 1
    assert sc.sale_ledger(path, LINE, pid) == [(ledger_id, -2)]
    offset, = sc.offsets(path, pid)
    assert offset['quantity_change'] == -22
    assert offset['created_at'] == '2026-04-01 00:00:00'
    assert offset['reference_no'] is None
    assert sc.stock(path, pid) == 176
    correction, = sc.corrections(path)
    assert (correction['id'], correction['status'], correction['stock_mode']) == \
        (cid, 'active', 'hold')
    assert correction['offset_txn_id'] == offset['id']
    assert (correction['express_unit_raw'], correction['express_unit'],
            correction['corrected_unit']) == ('โหล', 'โหล', 'หลอด')
    assert (correction['qty'], correction['unit_price'], correction['net']) == \
        (2.0, 49.0, 98.0)
    assert sc.cost_ledger(path, pid) == cost_before


def test_hold_on_a_return_measures_the_opposite_sign(db, monkeypatch):
    pid = sc.seed_product(db)
    sc.run_zip(monkeypatch, sc.standard_book().sale(
        'SR6900001', [(1, sc.CODE, 2.0, 'โหล', 49.0)], datetime.date(2026, 4, 10)))
    (ledger_id, qty), = sc.sale_ledger(db, 'SR6900001-1', pid)
    assert qty == 24
    before = sc.stock(db, pid)

    _apply(db, 'hold', doc_no='SR6900001-1')

    assert sc.sale_ledger(db, 'SR6900001-1', pid) == [(ledger_id, 2)]
    offset, = sc.offsets(db, pid)
    assert offset['quantity_change'] == 22
    assert offset['created_at'] == '2026-04-10 00:00:00'
    assert sc.stock(db, pid) == before


def test_move_posts_no_offset_and_shifts_the_later_purchase_cost(line):
    path, pid = line
    wacc_before = sc.cost_ledger(path, pid)[-1][5]
    assert wacc_before == pytest.approx((76 * 10 + 100 * 20) / 176)

    _apply(path, 'move')

    assert sc.offsets(path, pid) == []
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -2
    assert sc.stock(path, pid) == 198
    assert sc.corrections(path)[0]['offset_txn_id'] is None
    assert sc.cost_ledger(path, pid)[-1][5] == pytest.approx((98 * 10 + 100 * 20) / 198)


def test_preview_measures_both_modes_and_proposes_move_without_a_later_adjust(line):
    path, pid = line
    before = sc.written_state(path)

    effect = _preview(path)

    assert (effect.old_effect, effect.new_effect) == (-24, -2)
    assert (effect.stock_now, effect.stock_after_hold, effect.stock_after_move) == \
        (176, 176, 198)
    assert effect.proposed_mode == 'move' and effect.adjust_seen is None
    assert sc.written_state(path) == before


def test_preview_proposes_hold_and_names_the_adjust_it_saw(line):
    import models
    path, pid = line
    models.set_stock_to(pid, 150, note='นับสต๊อก')

    effect = _preview(path)

    assert effect.proposed_mode == 'hold'
    assert effect.adjust_seen['note'] == 'นับสต๊อก'
    assert effect.adjust_seen['id'] == sc.ledger_rows(path, pid)[-1]['id']
    assert (effect.stock_after_hold, effect.stock_after_move) == (150, 172)


def test_preview_counts_purchases_costed_under_water_when_the_sale_grows(db, monkeypatch):
    sc.seed_product(db)
    sc.run_zip(monkeypatch, sc.Book()
               .purchase('RR6900001', [(1, sc.CODE, 10.0, 'หลอด', 10.0)],
                         datetime.date(2026, 3, 10))
               .sale('IV6900001', [(1, sc.CODE, 2.0, 'หลอด', 5.0)])
               .purchase('RR6900002', [(1, sc.CODE, 10.0, 'หลอด', 20.0)],
                         datetime.date(2026, 5, 1)))

    effect = _preview(db, unit='โหล')

    assert (effect.old_effect, effect.new_effect) == (-2, -24)
    assert effect.exposure_before == {
        'min_stock': 0, 'purchases_below_zero': 0, 'purchases_at_zero': 1}
    assert effect.exposure_after == {
        'min_stock': -14, 'purchases_below_zero': 1, 'purchases_at_zero': 1}


# ── cancel ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('mode', ['hold', 'move'])
def test_cancel_restores_the_pre_correction_state(line, mode):
    path, pid = line
    before = sc.semantic_state(path, pid)
    audit_before = sc.audit_count(path)
    cid = _apply(path, mode)
    assert sc.semantic_state(path, pid) != before

    _cancel(path, cid)

    assert sc.semantic_state(path, pid) == before
    correction, = sc.corrections(path)
    assert (correction['status'], correction['end_cause'], correction['ended_by'],
            correction['end_reason']) == \
        ('cancelled', 'cancelled', 'put', 'ยกเลิกเพราะแก้ผิดบรรทัด')
    assert correction['ended_at'] is not None
    assert sc.audit_count(path) > audit_before
    row = sc.sales_row(path, LINE)
    assert (row['change_source'], row['change_actor'], row['change_reason']) == \
        ('manual', 'put', 'ยกเลิกเพราะแก้ผิดบรรทัด')


def test_cancel_refuses_when_the_express_unit_lost_its_ratio(line):
    import line_unit_correction as luc
    path, pid = line
    cid = _apply(path, 'hold')
    c = sc.raw(path)
    c.execute("DELETE FROM unit_conversions WHERE product_id=? AND bsn_unit='โหล'", (pid,))
    c.commit()
    c.close()
    before = sc.written_state(path)

    with pytest.raises(luc.Refused) as exc:
        _cancel(path, cid)

    assert exc.value.code == 'express_unit_no_ratio'
    assert sc.written_state(path) == before
    assert sc.corrections(path)[0]['status'] == 'active'


def test_cancel_refuses_a_correction_that_is_not_active(line):
    import line_unit_correction as luc
    path, pid = line
    cid = _apply(path, 'hold')
    _cancel(path, cid)
    before = sc.written_state(path)

    with pytest.raises(luc.Refused) as exc:
        _cancel(path, cid)

    assert exc.value.code == 'not_active'
    assert sc.written_state(path) == before


def test_a_line_can_be_corrected_again_after_a_cancel(line):
    path, pid = line
    first = _apply(path, 'hold')
    _cancel(path, first)

    second = _apply(path, 'move')

    assert [(r['id'], r['status']) for r in sc.corrections(path)] == \
        [(first, 'cancelled'), (second, 'active')]


@pytest.mark.parametrize('act', ['apply', 'cancel'])
def test_the_document_is_rescanned_for_review_flags(line, act):
    path, pid = line
    cid = _apply(path, 'hold') if act == 'cancel' else None
    c = sc.raw(path)
    c.execute("INSERT OR REPLACE INTO txn_review_docs (doc_base, date_iso, line_count,"
              " flag_count, scanned_at) VALUES ('IV6900001', '2026-04-01', 1, 1,"
              " '2000-01-01 00:00:00')")
    c.commit()
    c.close()

    if act == 'apply':
        _apply(path, 'hold')
    else:
        _cancel(path, cid)

    c = sc.raw(path)
    row = c.execute("SELECT scanned_at FROM txn_review_docs"
                    " WHERE doc_base='IV6900001'").fetchone()
    c.close()
    assert row is None or row['scanned_at'] != '2000-01-01 00:00:00'


# ── refusals: each names its rule and writes nothing ─────────────────────────

def _twin_doc_no(path, pid, monkeypatch):
    c = sc.conn(path)
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id)"
              " VALUES ('TWIN', 'twin', ?)", (pid,))
    c.commit()
    c.close()
    book = sc.standard_book()
    book.add_line('IV6900001', 1, 'TWIN', 1.0, 'หลอด', 5.0)
    sc.run_zip(monkeypatch, book)
    return {}


def _duplicate_key(path, pid, monkeypatch):
    c = sc.raw(path)
    c.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id,"
        " bsn_code, qty, unit, unit_price, net, synced_to_stock)"
        " SELECT date_iso, doc_no, doc_base, NULL, bsn_code, qty, unit, unit_price,"
        " net, 0 FROM sales_transactions WHERE doc_no=?", (LINE,))
    c.commit()
    c.close()
    return {}


def _no_ledger_row(path, pid, monkeypatch):
    c = sc.raw(path)
    c.execute("DELETE FROM transactions WHERE reference_no=?", (LINE,))
    c.commit()
    c.close()
    return {}


def _unsynced(path, pid, monkeypatch):
    c = sc.raw(path)
    c.execute("UPDATE sales_transactions SET synced_to_stock=0 WHERE doc_no=?", (LINE,))
    c.commit()
    c.close()
    return {}


def _platform_deduction(path, pid, monkeypatch):
    c = sc.raw(path)
    c.execute("INSERT INTO platform_stock_deductions (source_table, source_id,"
              " platform_sku_id, units) VALUES ('sales_transactions', ?, 1, 2)",
              (sc.sales_row(path, LINE)['id'],))
    c.commit()
    c.close()
    return {}


def _non_fixed_point(path, pid, monkeypatch):
    import line_unit_correction as luc
    c = sc.conn(path)
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
              " VALUES (?, 'หด', 1)", (pid,))
    c.commit()
    assert 'หด' in luc.allowed_units(c, pid)
    c.execute("INSERT INTO unit_map (book, spelling, word) VALUES ('BSN5657', 'หด', 'หลอด')")
    c.commit()
    c.close()
    return {'unit': 'หด'}


def _already_active(path, pid, monkeypatch):
    c = sc.conn(path)
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
              " VALUES (?, 'กล่อง', 6)", (pid,))
    c.commit()
    c.close()
    _apply(path, 'hold')
    return {'unit': 'กล่อง'}


def _sql(path, statement, *params):
    c = sc.raw(path)
    c.execute(statement, params)
    c.commit()
    c.close()
    return {}


_DECLARED = "change_source='import', change_actor='hand', change_token='hand-1'"

REFUSALS = [
    ('unmapped', lambda path, pid, mp: _sql(
        path, f"UPDATE sales_transactions SET product_id=NULL, {_DECLARED}"
              " WHERE doc_no=?", LINE)),
    ('history_import', lambda path, pid, mp: _sql(
        path, "UPDATE sales_transactions SET batch_id='history_import' WHERE doc_no=?",
        LINE)),
    ('ledger_mismatch', lambda path, pid, mp: _sql(
        path, "UPDATE transactions SET quantity_change=-23 WHERE reference_no=?", LINE)),
    ('bad_mode', lambda path, pid, mp: {'mode': 'guess'}),

    ('twin_doc_no', _twin_doc_no),
    ('duplicate_key', _duplicate_key),
    ('ledger_rows', _no_ledger_row),
    ('unsynced', _unsynced),
    ('platform_deduction', _platform_deduction),
    ('unit_not_allowed', lambda path, pid, mp: {'unit': 'กล่อง'}),
    ('unit_not_allowed', _non_fixed_point),
    ('same_unit', lambda path, pid, mp: {'unit': 'โหล'}),
    ('already_active', _already_active),
    ('reason_too_short', lambda path, pid, mp: {'reason': 'สั้นไป'}),
    ('not_found', lambda path, pid, mp: {'doc_no': 'IV6900001-9'}),
]


@pytest.mark.parametrize('code,arrange', REFUSALS,
                         ids=[f'{i}-{c}' for i, (c, _a) in enumerate(REFUSALS)])
def test_apply_refuses_and_writes_nothing(line, monkeypatch, code, arrange):
    import line_unit_correction as luc
    path, pid = line
    overrides = arrange(path, pid, monkeypatch)
    before = sc.written_state(path)
    assert before['sales'] and before['ledger']

    with pytest.raises(luc.Refused) as exc:
        _apply(path, overrides.pop('mode', 'hold'), **overrides)

    assert exc.value.code == code
    assert sc.written_state(path) == before


CANCEL_REFUSALS = [
    ('line_missing', lambda path, pid, offset_id: _sql(
        path, "DELETE FROM sales_transactions WHERE doc_no=?", LINE)),
    ('line_changed', lambda path, pid, offset_id: _sql(
        path, f"UPDATE sales_transactions SET unit='โหล', {_DECLARED} WHERE doc_no=?",
        LINE)),
    ('product_changed', lambda path, pid, offset_id: _sql(
        path, f"UPDATE sales_transactions SET product_id=?, {_DECLARED} WHERE doc_no=?",
        sc.seed_product(path, 'OTHER', name='อื่น'), LINE)),
    ('ledger_rows', lambda path, pid, offset_id: _sql(
        path, "DELETE FROM transactions WHERE reference_no=?", LINE)),
    ('offset_missing', lambda path, pid, offset_id: _sql(
        path, "DELETE FROM transactions WHERE id=?", offset_id)),
    ('reason_too_short', lambda path, pid, offset_id: {'reason': 'สั้นไป'}),
    ('express_unit_no_ratio', lambda path, pid, offset_id: _sql(
        path, "UPDATE unit_conversions SET ratio=0 WHERE product_id=? AND bsn_unit='โหล'",
        pid)),
]


@pytest.mark.parametrize('code,arrange', CANCEL_REFUSALS,
                         ids=[c for c, _a in CANCEL_REFUSALS])
def test_cancel_refuses_and_writes_nothing(line, code, arrange):
    import line_unit_correction as luc
    path, pid = line
    cid = _apply(path, 'hold')
    overrides = arrange(path, pid, sc.offsets(path, pid)[0]['id'])
    before = sc.written_state(path)

    with pytest.raises(luc.Refused) as exc:
        _cancel(path, cid, **overrides)

    assert exc.value.code == code
    assert sc.written_state(path) == before
    assert sc.corrections(path)[0]['status'] == 'active'


def test_a_cost_failure_rolls_the_whole_correction_back(line, monkeypatch):
    from models import wacc
    path, pid = line
    before = sc.written_state(path)

    def boom(conn, product_ids, operation=None):
        raise wacc.WaccIdentityError('forced by the test', product_id=pid,
                                     reference_no=LINE, operation=operation)
    monkeypatch.setattr(wacc, 'preflight_batch', boom)

    with pytest.raises(wacc.WaccIdentityError):
        _apply(path, 'hold')

    assert sc.written_state(path) == before
    alert, = sc.alerts(path, 'wacc_identity')
    assert 'unit_correction' in alert['context_json']


def test_badges_return_the_latest_correction_of_each_line(line):
    import line_unit_correction as luc
    path, pid = line
    first = _apply(path, 'hold')
    _cancel(path, first)
    second = _apply(path, 'move')

    c = sc.conn(path)
    try:
        badges = luc.badges_for_doc(c, 'IV6900001')
        assert luc.badges_for_doc(c, 'IV6900002') == {}
    finally:
        c.close()

    assert list(badges) == [(LINE, sc.CODE)]
    assert badges[(LINE, sc.CODE)]['id'] == second
    assert badges[(LINE, sc.CODE)]['reason'] == sc.REASON
