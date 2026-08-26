"""scripts/price_lookup.py — subprocess-level tests for the prod-runnable CLI
over price_lookup.resolve_price / find_products / find_customers.

Every test runs the SCRIPT AS A SUBPROCESS against `tmp_db` (never imports it
directly — the whole point is proving the exact `python scripts/price_lookup.py`
invocation that will run on prod). Env: DATABASE_PATH points the script at the
tmp_db clone; SECRET_KEY/ADMIN_PASSWORD are passed through from the pytest
process's own os.environ (conftest.py sets defaults for both via
os.environ.setdefault, so they are present unless something unsets them —
still guarded per spec: skip with a clear reason if either is absent).

Force fixture state, never inherit it (verification-discipline.md): every
test clears its own throwaway product_id(s)/customer before seeding.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'price_lookup.py'

SENDAI_BRAND_ID = 3  # เซ็นได — verified against the live dev DB (test_price_lookup.py)

_pid_counter = [800000]
_doc_counter = [8800000]


def _need_env():
    missing = [k for k in ('SECRET_KEY', 'ADMIN_PASSWORD') if not os.environ.get(k)]
    if missing:
        pytest.skip(f"{' and '.join(missing)} not set in the test process env — "
                     f"can't run the CLI subprocess (see conftest.py's setdefault)")


# ── fixture builders (mirrors tests/test_price_lookup.py's style) ──────────

def _mk_product(conn, name, *, unit_type='ตัว', base=100.0, cost=60.0,
                 brand_id=SENDAI_BRAND_ID, active=1, family_id=None):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active, family_id) VALUES (?,?,?,?,?,?,?)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, brand_id, active, family_id),
    )
    conn.commit()
    return cur.lastrowid


def _clear_pid(conn, pid):
    tier_ids = [r['id'] for r in conn.execute(
        "SELECT id FROM product_price_tiers WHERE product_id = ?", (pid,))]
    if tier_ids:
        qmarks = ",".join("?" * len(tier_ids))
        conn.execute(
            f"DELETE FROM audit_log WHERE table_name = 'product_price_tiers' "
            f"AND row_id IN ({qmarks})", tier_ids)
    for table in ('sales_transactions', 'promotions', 'product_price_tiers',
                  'unit_conversions', 'product_price_history'):
        conn.execute(f"DELETE FROM {table} WHERE product_id = ?", (pid,))
    conn.commit()


def _mk_customer(conn, code, name):
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        (code, name),
    )
    conn.commit()
    return code


def _mk_family(conn, display_name):
    cur = conn.execute(
        "INSERT INTO product_families (family_code, display_name) VALUES (?, ?)",
        (f"TEST-FAM-{_pid_counter[0]}", display_name),
    )
    conn.commit()
    return cur.lastrowid


def _bill(conn, *, pid, customer_code, customer_name, date_iso, qty, unit,
          unit_price, vat_type, net, suffix=1):
    _doc_counter[0] += 1
    doc_base = f"IV{_doc_counter[0]}"
    doc_no = f"{doc_base}-{suffix}"
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, discount, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
        (date_iso, doc_no, doc_base, pid, customer_name, customer_code,
         qty, unit, unit_price, vat_type, net, net),
    )
    conn.commit()
    return doc_no


# ── running the CLI ──────────────────────────────────────────────────────────

def _run_cli(tmp_db, payload):
    _need_env()
    env = dict(os.environ)
    env['DATABASE_PATH'] = tmp_db
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert 'Traceback' not in proc.stderr, f"CLI printed a traceback: {proc.stderr}"
    return json.loads(proc.stdout), proc


# ── tests ─────────────────────────────────────────────────────────────────

def test_json_round_trip_one_resolved_line(tmp_db, tmp_db_conn):
    pid = _mk_product(tmp_db_conn, "ทดสอบราคา CLI", unit_type='ตัว', base=100.0, cost=60.0)
    _clear_pid(tmp_db_conn, pid)

    out, _proc = _run_cli(tmp_db, {"lines": [{"product_id": pid}], "today": "2026-08-20"})

    assert 'db_max_sale_date' in out
    assert 'db_path_basename' in out
    assert out['db_path_basename'] == 'inventory.db'
    assert len(out['lines']) == 1
    line = out['lines'][0]
    assert 'result' in line
    result = line['result']

    # Ties to an independent oracle: no promo/tier seeded, so list price ==
    # base × ratio (ratio 1.0, unit_type asked) == 100.0 exactly.
    assert result['answer']['price_per_unit'] == 100.0
    assert result['product']['id'] == pid

    # Null passthrough: no customer given, so customer must be JSON null,
    # not omitted and not the string "null" / "None".
    assert 'customer' in result
    assert result['customer'] is None

    # Control for the no-price case below: a normally-priced product carries
    # NEITHER key at all.
    assert 'no_list_price' not in result
    assert 'family_hint' not in result


def test_ambiguous_product_query_returns_candidates(tmp_db, tmp_db_conn):
    token = f"เอกลักษณ์กำกวม{_pid_counter[0] + 1}"
    pid_a = _mk_product(tmp_db_conn, f"{token} A", base=50.0)
    pid_b = _mk_product(tmp_db_conn, f"{token} B", base=60.0)
    _clear_pid(tmp_db_conn, pid_a)
    _clear_pid(tmp_db_conn, pid_b)

    out, _proc = _run_cli(tmp_db, {"lines": [{"product_query": token}]})

    line = out['lines'][0]
    assert 'result' not in line
    assert 'candidates' in line
    ids = {c['id'] for c in line['candidates']['products']}
    assert ids == {pid_a, pid_b}


def test_unknown_unit_returns_error_not_traceback(tmp_db, tmp_db_conn):
    pid = _mk_product(tmp_db_conn, "ทดสอบหน่วยผิด", unit_type='ตัว', base=100.0)
    _clear_pid(tmp_db_conn, pid)

    out, proc = _run_cli(tmp_db, {"lines": [{"product_id": pid, "unit": "ลังทดสอบ"}]})

    line = out['lines'][0]
    assert 'error' in line
    assert 'result' not in line
    assert isinstance(line['error'], str) and line['error']
    assert proc.stderr == '' or 'Traceback' not in proc.stderr


def test_db_max_sale_date_present_and_correct(tmp_db, tmp_db_conn):
    expected = tmp_db_conn.execute("SELECT MAX(date_iso) FROM sales_transactions").fetchone()[0]

    out, _proc = _run_cli(tmp_db, {"lines": []})

    assert out['db_max_sale_date'] == expected


def test_family_hint_present_for_no_price_product_with_priced_sibling(tmp_db, tmp_db_conn):
    family_id = _mk_family(tmp_db_conn, "ทดสอบตระกูล")
    no_price = _mk_product(tmp_db_conn, "ไม่มีราคา", unit_type='ตัว', base=0.0,
                            family_id=family_id)
    sibling = _mk_product(tmp_db_conn, "พี่น้องมีราคา", unit_type='ตัว', base=45.0,
                           family_id=family_id)
    _clear_pid(tmp_db_conn, no_price)
    _clear_pid(tmp_db_conn, sibling)
    cust = _mk_customer(tmp_db_conn, 'TESTCLI01', 'ลูกค้าทดสอบ CLI')
    _bill(tmp_db_conn, pid=sibling, customer_code=cust, customer_name='ลูกค้าทดสอบ CLI',
          date_iso='2026-08-01', qty=10, unit='ตัว', unit_price=40.0, vat_type=1, net=400.0)

    out, _proc = _run_cli(tmp_db, {"lines": [{"product_id": no_price}], "today": "2026-08-20"})

    line = out['lines'][0]
    result = line['result']
    # The trigger this feature hangs off: no real price at all.
    assert result['list']['list_for_unit'] == 0
    assert result['no_list_price'] is True
    assert 'family_hint' in result
    hints = result['family_hint']
    assert len(hints) >= 1
    sibling_hint = next(h for h in hints if h['id'] == sibling)
    assert sibling_hint['product_name'].startswith('พี่น้องมีราคา')
    assert sibling_hint['base_sell_price'] == 45.0
    assert sibling_hint['latest_b2b_cash_per_piece'] == 40.0  # net/qty = 400/10


def test_family_hint_empty_for_no_price_product_without_family(tmp_db, tmp_db_conn):
    no_price_no_family = _mk_product(tmp_db_conn, "ไม่มีราคาไม่มีตระกูล", unit_type='ตัว',
                                      base=0.0, family_id=None)
    _clear_pid(tmp_db_conn, no_price_no_family)

    out, _proc = _run_cli(tmp_db, {"lines": [{"product_id": no_price_no_family}],
                                    "today": "2026-08-20"})

    line = out['lines'][0]
    result = line['result']
    assert result['list']['list_for_unit'] == 0
    assert result['no_list_price'] is True
    assert result['family_hint'] == []


def test_ambiguous_customer_query_renames_last_seen_date(tmp_db, tmp_db_conn):
    token = f"ลูกค้ากำกวมทดสอบ{_pid_counter[0] + 1}"
    code_a = f"TESTAMB{_pid_counter[0]}A"
    code_b = f"TESTAMB{_pid_counter[0]}B"
    _mk_customer(tmp_db_conn, code_a, f"{token} A")
    _mk_customer(tmp_db_conn, code_b, f"{token} B")
    pid = _mk_product(tmp_db_conn, "ทดสอบลูกค้ากำกวม", base=10.0)
    _clear_pid(tmp_db_conn, pid)

    out, _proc = _run_cli(tmp_db, {"lines": [{"product_id": pid, "customer_query": token}]})

    line = out['lines'][0]
    assert 'result' not in line
    assert 'candidates' in line
    customers = line['candidates']['customers']
    codes = {c['code'] for c in customers}
    assert {code_a, code_b} <= codes
    for c in customers:
        assert 'last_purchase_date' not in c  # renamed away, not just added
        assert 'last_seen_date' in c


def test_nonexistent_product_id_returns_error_not_traceback(tmp_db):
    out, proc = _run_cli(tmp_db, {"lines": [{"product_id": 999999999}]})

    line = out['lines'][0]
    assert 'error' in line
    assert 'result' not in line
    assert '999999999' in line['error']
    assert 'Traceback' not in proc.stderr


def test_script_never_writes(tmp_db, tmp_db_conn):
    pid = _mk_product(tmp_db_conn, "ทดสอบไม่เขียน", base=100.0)
    _clear_pid(tmp_db_conn, pid)

    before = tmp_db_conn.execute("PRAGMA data_version").fetchone()[0]

    _run_cli(tmp_db, {"lines": [{"product_id": pid}]})

    # Independent signal: PRAGMA data_version increments on any commit to
    # this DB FILE from any connection/process (unlike total_changes, which
    # only sees writes made through the probe connection itself).
    after = tmp_db_conn.execute("PRAGMA data_version").fetchone()[0]
    assert after == before
