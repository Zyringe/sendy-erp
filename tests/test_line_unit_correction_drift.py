"""แก้หน่วยบรรทัด (#692) and the document drift scan.

A corrected line reads as Express's unit on the SENDY side of the comparison,
so a corrected document equals Express until Express really changes.
"""
import datetime

import pytest

from tests import unit_correction_scenario as sc

LINE = 'IV6900001-1'


def _book(*, corrected_qty=2.0, control_qty=1.0, other_price=5.0):
    return (sc.standard_book()
            .sale('IV6900002', [(1, sc.CODE, control_qty, 'โหล', 49.0)],
                  datetime.date(2026, 4, 2))
            .sale('IV6900003', [(1, sc.CODE, 1.0, 'โหล', 49.0),
                                (2, 'OTHER', 1.0, 'หลอด', other_price)],
                  datetime.date(2026, 4, 3)))


def _findings(path, book, baseline=None):
    import express_dbf_source as eds
    t = book.tables()
    c = sc.raw(path)
    try:
        result = eds.detect_document_drift(
            t['ARTRN'], t['APTRN'], t['STCRD'], t['ARMAS'], t['APMAS'], c,
            baseline=baseline)
    finally:
        c.close()
    assert {'IV6900001', 'IV6900002', 'IV6900003'} <= result.compared_doc_nos
    return result.findings


def _correct(path, doc_no=LINE):
    import line_unit_correction as luc
    c = sc.conn(path)
    try:
        return luc.apply(c, doc_no, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    finally:
        c.close()


@pytest.fixture
def imported(empty_db, monkeypatch):
    sc.seed_company(empty_db)
    sc.seed_product(empty_db)
    sc.seed_product(empty_db, 'OTHER', ratios=(), name='อื่น')
    sc.run_zip(monkeypatch, _book())
    assert _findings(empty_db, _book()) == []
    return empty_db


def test_a_corrected_document_is_not_drift_while_a_drifting_one_still_is(imported):
    _correct(imported)

    findings = _findings(imported, _book(control_qty=5.0))

    assert [(f['doc_no'], f['kind']) for f in findings] == [('IV6900002', 'content')]
    assert 'unit_correction_ids' not in findings[0]


def test_an_express_edit_of_a_corrected_document_names_the_correction(imported):
    import models
    cid = _correct(imported)

    findings = _findings(imported, _book(corrected_qty=3.0))

    finding, = findings
    assert (finding['doc_no'], finding['unit_correction_ids']) == ('IV6900001', [cid])
    models.record_express_doc_drift_alerts(findings, dataset_label='BSN5657')
    alert, = sc.alerts(imported, 'express_doc_drift')
    assert 'เอกสารนี้มีการแก้หน่วยบรรทัดค้างอยู่' in alert['message']
    assert f'"unit_correction_ids": [{cid}]' in alert['context_json']


def test_a_baselined_document_keeps_its_fingerprint_when_a_line_is_corrected(imported):
    drifting = _book(other_price=6.0)
    before, = _findings(imported, drifting)
    assert before['doc_no'] == 'IV6900003'
    baseline = {'IV6900003': {'fingerprint': before['fingerprint'],
                              'reason': 'ราคาต่างจากต้นทาง อธิบายไว้แล้ว'}}
    assert _findings(imported, drifting, baseline) == []

    _correct(imported, 'IV6900003-1')

    after, = _findings(imported, drifting)
    assert after['fingerprint'] == before['fingerprint']
    assert _findings(imported, drifting, baseline) == []
