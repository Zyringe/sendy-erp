"""xp5_mapping_pipeline — Codex R4 regressions: human rows are never
modified or re-proposed; auto rows refresh as a derived layer; layer-2
signatures require unit agreement."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import xp5_mapping_pipeline as pl  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'main.db')
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE xp5_product_mapping (
            xp5_code TEXT PRIMARY KEY,
            product_id INTEGER,
            xp5_name TEXT NOT NULL DEFAULT '',
            match_layer TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'auto',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
    """)
    c.execute("INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer, note) "
              "VALUES ('X1', 10, 'reviewed', 'manual', 'Put เคาะเอง')")
    c.execute("INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer) "
              "VALUES ('X2', NULL, 'ignored', 'manual')")
    c.execute("INSERT INTO xp5_product_mapping (xp5_code, product_id, status, match_layer, evidence_count) "
              "VALUES ('X3', 99, 'auto', 'dualkey', 2)")
    c.commit()
    yield c
    c.close()


def _row(code, pid, layer='dualkey', ev=3):
    return {'xp5_code': code, 'product_id': pid, 'xp5_name': '',
            'match_layer': layer, 'evidence_count': ev}


def test_apply_never_touches_reviewed_or_ignored(conn):
    pl.apply_auto(conn, [_row('X1', 20), _row('X4', 7)], {})
    r = conn.execute("SELECT product_id, status, note FROM xp5_product_mapping "
                     "WHERE xp5_code='X1'").fetchone()
    assert (r['product_id'], r['status'], r['note']) == (10, 'reviewed', 'Put เคาะเอง')
    assert conn.execute("SELECT status FROM xp5_product_mapping "
                        "WHERE xp5_code='X2'").fetchone()['status'] == 'ignored'


def test_apply_refreshes_auto_layer_as_derived_cache(conn):
    # stale auto X3 not in the new batch → gone; new autos land field-exact
    pl.apply_auto(conn, [_row('X4', 7, 'code+name', 1)], {})
    assert conn.execute("SELECT COUNT(*) FROM xp5_product_mapping "
                        "WHERE xp5_code='X3'").fetchone()[0] == 0
    r = conn.execute("SELECT product_id, match_layer, evidence_count, status "
                     "FROM xp5_product_mapping WHERE xp5_code='X4'").fetchone()
    assert tuple(r) == (7, 'code+name', 1, 'auto')


def test_locked_codes_excluded_from_proposals(conn):
    assert pl.load_locked_codes(conn) == {'X1', 'X2'}


def test_layer2_signature_requires_same_unit(monkeypatch, tmp_path):
    """Same qty+price in DIFFERENT units must never produce a product pair."""
    import express_dbf_source as eds
    import datetime
    d = datetime.date(2026, 3, 1)

    def fake_open(dbf_dir, name):
        which = os.path.basename(dbf_dir)
        if name == 'ARTRN':
            doc = 'IV2600001' if which == 'xp5' else 'IV6900001'
            return [{'DOCNUM': doc, 'RECTYP': '3', 'DOCDAT': d, 'AFTDISC': 100.0}]
        lines = {'xp5': [{'DOCNUM': 'IV2600001', 'STKCOD': 'XC', 'TRNQTY': 1,
                          'UNITPR': 100.0, 'TQUCOD': 'ผง'}],      # แผง
                 'bsn': [{'DOCNUM': 'IV6900001', 'STKCOD': 'BC', 'TRNQTY': 1,
                          'UNITPR': 100.0, 'TQUCOD': 'ตว'}]}      # ตัว
        return lines[which]

    monkeypatch.setattr(eds, 'open_table', fake_open)
    auto, review, stats = pl.layer2(
        str(tmp_path / 'xp5'), str(tmp_path / 'bsn'),
        {'BC': (55, 'ชื่อ')}, {55: 'ชื่อ'}, set())
    assert stats['doc_pairs'] == 1            # docs DO pair (date+amount)
    assert auto == [] and review == []        # but the lines never match


def test_layer2_signature_same_unit_pairs(monkeypatch, tmp_path):
    import express_dbf_source as eds
    import datetime

    def fake_open(dbf_dir, name):
        which = os.path.basename(dbf_dir)
        if name == 'ARTRN':
            out = []
            for i in (1, 2):                  # two independent doc pairs
                doc = f'IV260000{i}' if which == 'xp5' else f'IV690000{i}'
                out.append({'DOCNUM': doc, 'RECTYP': '3',
                            'DOCDAT': datetime.date(2026, 3, i), 'AFTDISC': 100.0 * i})
            return out
        rows = []
        for i in (1, 2):
            doc = f'IV260000{i}' if which == 'xp5' else f'IV690000{i}'
            code = 'XC' if which == 'xp5' else 'BC'
            rows.append({'DOCNUM': doc, 'STKCOD': code, 'TRNQTY': i,
                         'UNITPR': 100.0, 'TQUCOD': 'ตว'})
        return rows

    monkeypatch.setattr(eds, 'open_table', fake_open)
    auto, review, stats = pl.layer2(
        str(tmp_path / 'xp5'), str(tmp_path / 'bsn'),
        {'BC': (55, 'ชื่อ')}, {55: 'ชื่อ'}, set())
    assert [a['xp5_code'] for a in auto] == ['XC']
    assert auto[0]['evidence_count'] == 2
