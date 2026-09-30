"""แก้หน่วยบรรทัด (#692): with no correction in the table the hooks change
nothing.

The same zip sequence runs on two identical databases. On one the module is
as shipped. On the other `active_by_line_key` answers "none" without reading
the table and every other hook entry point raises if it is reached. Importer
stats, ledger, stock, cost ledger and drift findings must be equal.
"""
import datetime

from tests import unit_correction_scenario as sc

EXPORT_AT = datetime.datetime(2026, 9, 30, 8, 0)


def _first():
    return (sc.standard_book()
            .sale('IV6900002', [(1, sc.CODE, 1.0, 'โหล', 49.0),
                                (2, 'OTHER', 3.0, 'หลอด', 5.0),
                                (3, sc.CODE, 1.0, 'แพ็ค', 30.0)],
                  datetime.date(2026, 4, 2))
            .sale('SR6900001', [(1, sc.CODE, 1.0, 'โหล', 49.0)],
                  datetime.date(2026, 4, 5)))


def _second():
    """One line changed, one removed, one new, the rest as they were."""
    return (sc.standard_book()
            .sale('IV6900002', [(1, sc.CODE, 4.0, 'โหล', 49.0),
                                (3, sc.CODE, 1.0, 'แพ็ค', 30.0)],
                  datetime.date(2026, 4, 2))
            .sale('SR6900001', [(1, sc.CODE, 1.0, 'โหล', 49.0)],
                  datetime.date(2026, 4, 5))
            .sale('IV6900003', [(1, 'OTHER', 2.0, 'หลอด', 5.0)],
                  datetime.date(2026, 4, 6)))


def _third():
    """Never imported: Express as the drift scan sees it after a source edit."""
    book = _second()
    book.stcrd[1]['TRNQTY'] = 5.0
    return book


def _observe(path, monkeypatch, *, hooks):
    import express_dbf_source as eds
    import line_unit_correction as luc
    with sc.on_db(monkeypatch, path) as m:
        if not hooks:
            def unreachable(*args, **kwargs):
                raise AssertionError('a unit-correction hook ran on an empty table')
            m.setattr(luc, 'active_by_line_key', lambda conn: {})
            for name in ('decide', 'retire', 'sendy_side_unit'):
                m.setattr(luc, name, unreachable)
        sc.seed_company(path)
        pids = [sc.seed_product(path), sc.seed_product(path, 'OTHER', ratios=(), name='อื่น')]
        runs = [sc.run_zip(m, book, detect_drift=True, export_at=EXPORT_AT)
                for book in (_first(), _second())]
        t = _third().tables()
        c = sc.raw(path)
        try:
            drift = eds.detect_document_drift(
                t['ARTRN'], t['APTRN'], t['STCRD'], t['ARMAS'], t['APMAS'], c,
                export_at=EXPORT_AT).findings
        finally:
            c.close()
    return {
        'runs': runs,
        'state': [sc.semantic_state(path, pid, across_dbs=True) for pid in pids],
        'drift': drift,
    }


def test_an_empty_corrections_table_changes_nothing(empty_db, tmp_path, monkeypatch):
    twin = sc.clone_db(empty_db, str(tmp_path / 'twin.db'))

    shipped = _observe(empty_db, monkeypatch, hooks=True)
    bare = _observe(twin, monkeypatch, hooks=False)

    second = shipped['runs'][1]['sales']
    assert (second['overwritten'], second['removed'], second['imported']) == (1, 1, 2)
    assert second['unit_corrections_retired'] == 0
    assert shipped['runs'][1]['doc_drift']['findings'] == []
    assert [f['doc_no'] for f in shipped['drift']] == ['IV6900001']
    assert len(shipped['state'][0]['ledger']) == 5
    assert len(shipped['state'][0]['cost_ledger']) == 2
    assert sc.corrections(empty_db) == []
    assert shipped == bare
