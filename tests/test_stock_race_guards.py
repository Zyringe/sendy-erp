"""Guards for the two check-then-write sites on the stock ledger.

Both sites read stock, decide, then write — with nothing holding the two
together. An interleaving write (another worker; Railway runs gunicorn -w 2)
lands between them and the decision is applied to a world that no longer
exists. Reproduced 2026-08-01 against a clone of the live DB:

  stock_adjust    "set stock to 12" landed on 9   (a -3 sale interleaved)
  run_conversion  input stock driven to -2        (a consumer interleaved)

and, with no idempotency at all, a double-tapped ยืนยันแปลงสินค้า consumed
4 and produced 2 where one click's worth is 2 and 1.

The interleaving is injected at the exact seam rather than raced with
threads: the write is real, the code under test is untouched, and the test
cannot flake.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')
import sqlite3
import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _q(db, sql, *a):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = c.execute(sql, a).fetchall()
    c.close()
    return rows


def _stock(db, pid):
    r = _q(db, "SELECT quantity FROM stock_levels WHERE product_id=?", pid)
    return r[0]['quantity'] if r else 0


def _write_movement(db, pid, qty, note):
    """A concurrent request committing its own movement."""
    c = sqlite3.connect(db, timeout=10)
    c.execute("INSERT INTO transactions(product_id,txn_type,quantity_change,unit_mode,note)"
              " VALUES (?,?,?,?,?)", (pid, 'OUT' if qty < 0 else 'IN', qty, 'unit', note))
    c.commit()
    c.close()


def _force_stock(db, pid, target):
    cur = _stock(db, pid)
    if cur != target:
        _write_movement(db, pid, round(target - cur, 4), 'test baseline')


def _a_product(db):
    r = _q(db, "SELECT id FROM products WHERE is_active=1 ORDER BY id LIMIT 1")
    if not r:
        pytest.skip("no active products in live DB clone")
    return r[0]['id']


def _a_formula(db):
    """An active formula with exactly one input, so 'consumed' is unambiguous."""
    r = _q(db, """
        SELECT cf.id, cf.output_product_id AS out_pid, cf.output_qty,
               MIN(cfi.product_id) AS in_pid, MIN(cfi.quantity) AS in_qty,
               COUNT(cfi.id) AS n_inputs
          FROM conversion_formulas cf
          JOIN conversion_formula_inputs cfi ON cfi.formula_id = cf.id
         WHERE cf.is_active = 1
         GROUP BY cf.id HAVING n_inputs = 1
         ORDER BY cf.id LIMIT 1""")
    if not r:
        pytest.skip("no single-input active conversion formula in live DB clone")
    return r[0]


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return c


# ── P4 — stock count ─────────────────────────────────────────────────────────

def test_stock_count_decides_on_a_value_read_inside_the_write(tmp_db, admin_client, monkeypatch):
    """'ตั้งเป็น 12' must mean 12: the diff has to be computed from stock read
    inside the transaction that writes it, never from a value read earlier.

    The monkeypatch is load-bearing — it makes any stock value read OUTSIDE
    the write a lie, which is exactly what a second gunicorn worker does to
    the value this request is holding. Without it the pre-fix code reads the
    fresh 7 for itself and the test passes for the wrong reason.
    """
    import models
    pid = _a_product(tmp_db)
    _force_stock(tmp_db, pid, 10)
    _write_movement(tmp_db, pid, -3, 'concurrent sale')     # real stock is now 7
    monkeypatch.setattr(models, 'get_current_stock', lambda p: 10)   # ...but out-of-band reads still say 10

    r = admin_client.post(f'/products/{pid}/adjust',
                          data={'new_quantity': '12', 'reason': 'count'})
    assert r.status_code in (200, 302)
    assert _stock(tmp_db, pid) == 12


def test_stock_count_noop_when_already_at_target(tmp_db, admin_client):
    """The no-change path must stay a no-change path (no zero-qty ledger row)."""
    import models
    pid = _a_product(tmp_db)
    _force_stock(tmp_db, pid, 7)
    before = _q(tmp_db, "SELECT COUNT(*) c FROM transactions WHERE product_id=?", pid)[0]['c']
    admin_client.post(f'/products/{pid}/adjust',
                      data={'new_quantity': '7', 'reason': 'count'})
    after = _q(tmp_db, "SELECT COUNT(*) c FROM transactions WHERE product_id=?", pid)[0]['c']
    assert after == before
    assert _stock(tmp_db, pid) == 7


def test_stock_count_honours_backdated_created_at(tmp_db, admin_client):
    """Backdating (reason != count) must survive the rewrite."""
    pid = _a_product(tmp_db)
    _force_stock(tmp_db, pid, 5)
    admin_client.post(f'/products/{pid}/adjust',
                      data={'new_quantity': '8', 'reason': 'damaged',
                            'adjust_date': '2026-01-15'})
    row = _q(tmp_db, "SELECT quantity_change, created_at, note FROM transactions"
                     " WHERE product_id=? ORDER BY id DESC LIMIT 1", pid)[0]
    assert row['created_at'] == '2026-01-15 00:00:00'
    assert row['quantity_change'] == 3
    assert _stock(tmp_db, pid) == 8


# ── P3b — conversion shortage check ──────────────────────────────────────────

def test_conversion_shortage_check_is_atomic_with_its_write(tmp_db, monkeypatch):
    """A consumer landing after the shortage check must not yield negative stock.

    RED before the fix: the check passes against stock that is gone by the
    time the OUT rows are inserted, and the input goes to -in_qty.
    """
    import models
    from models import conversions as conv_mod
    f = _a_formula(tmp_db)
    in_pid, need = f['in_pid'], f['in_qty']
    _force_stock(tmp_db, in_pid, need)      # exactly enough for one run

    real_wacc = conv_mod.get_current_wacc
    landed = []

    def racing_wacc(pid, conn=None):
        """Fires once, at the seam between the shortage check and the OUT rows.

        A short timeout turns 'the write lock excluded me' into a fast, certain
        answer instead of a 10s stall: SQLite's locking decides this, not luck.
        """
        if not landed:
            c = sqlite3.connect(tmp_db, timeout=0.5)
            try:
                c.execute("INSERT INTO transactions(product_id,txn_type,quantity_change,"
                          "unit_mode,note) VALUES (?,?,?,?,?)",
                          (in_pid, 'OUT', -need, 'unit', 'concurrent consumer'))
                c.commit()
                landed.append(True)         # got in between check and write
            except sqlite3.OperationalError:
                landed.append(False)        # excluded by the write lock — correct
            finally:
                c.close()
        return real_wacc(pid, conn)

    monkeypatch.setattr(conv_mod, 'get_current_wacc', racing_wacc)
    ok, msg, _ = models.run_conversion(f['id'], 1)

    assert landed, "the interleaving never fired — the test proves nothing"
    assert landed == [False], \
        "a concurrent movement landed between the shortage check and its write"
    assert _stock(tmp_db, in_pid) >= 0, f"input stock went negative: {_stock(tmp_db, in_pid)}"
    assert ok, f"the run should succeed once it holds the lock: {msg}"


def test_conversion_happy_path_still_works(tmp_db):
    """The guard must not break an ordinary run."""
    import models
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], f['in_qty'] * 5)
    before_in, before_out = _stock(tmp_db, f['in_pid']), _stock(tmp_db, f['out_pid'])

    ok, msg, info = models.run_conversion(f['id'], 2)

    assert ok, msg
    assert _stock(tmp_db, f['in_pid']) == before_in - f['in_qty'] * 2
    assert _stock(tmp_db, f['out_pid']) == before_out + f['output_qty'] * 2


def test_conversion_shortage_still_refuses(tmp_db):
    """An honest shortage must still be refused, with nothing written."""
    import models
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], 0)
    before = _q(tmp_db, "SELECT COUNT(*) c FROM transactions WHERE product_id=?", f['in_pid'])[0]['c']

    ok, msg, _ = models.run_conversion(f['id'], 1)

    assert not ok
    assert 'สต็อกไม่พอ' in msg
    assert _q(tmp_db, "SELECT COUNT(*) c FROM transactions WHERE product_id=?",
              f['in_pid'])[0]['c'] == before


# ── P3a — replayed POST ──────────────────────────────────────────────────────

def test_replayed_conversion_post_runs_once(tmp_db, admin_client):
    """The same submission twice must convert once.

    RED before the fix: nothing dedups, so the ledger takes both.
    """
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], f['in_qty'] * 10)
    before_in, before_out = _stock(tmp_db, f['in_pid']), _stock(tmp_db, f['out_pid'])

    payload = {'multiplier': '1', 'run_token': 'CONV-test-replay-token'}
    r1 = admin_client.post(f"/conversions/{f['id']}/run", data=payload)
    r2 = admin_client.post(f"/conversions/{f['id']}/run", data=payload)

    assert r1.status_code in (200, 302) and r2.status_code in (200, 302)
    assert _stock(tmp_db, f['in_pid']) == before_in - f['in_qty'], "input consumed twice"
    assert _stock(tmp_db, f['out_pid']) == before_out + f['output_qty'], "output produced twice"


def test_run_page_renders_a_fresh_token_each_time(tmp_db, admin_client):
    """Two page loads must not hand out the same token, or the second real run
    of the day would be refused as a replay."""
    f = _a_formula(tmp_db)
    import re
    seen = set()
    for _ in range(2):
        html = admin_client.get(f"/conversions/{f['id']}/run").get_data(as_text=True)
        m = re.search(r'name="run_token"\s+value="([^"]+)"', html)
        assert m, 'run.html renders no run_token field'
        seen.add(m.group(1))
    assert len(seen) == 2, f'token was reused across renders: {seen}'


def test_a_failed_run_does_not_burn_its_token(tmp_db):
    """A run refused for shortage writes nothing, so the same token must still
    work once stock arrives — otherwise the operator has to reload to retry."""
    import models
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], 0)
    token = 'CONV-test-retry-token'

    ok1, msg1, _ = models.run_conversion(f['id'], 1, run_token=token)
    assert not ok1 and 'สต็อกไม่พอ' in msg1

    _force_stock(tmp_db, f['in_pid'], f['in_qty'] * 3)
    ok2, msg2, _ = models.run_conversion(f['id'], 1, run_token=token)
    assert ok2, f'the token was burned by a failed run: {msg2}'


def test_replayed_post_runs_once_even_with_a_typed_reference_no(tmp_db, admin_client):
    """The replay key is the form token, independent of the business document
    number. One submission re-sent carries BOTH the same token AND the same
    typed เลขที่เอกสาร — the presence of a document number must not switch the
    guard off (Codex, PR #345 review)."""
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], f['in_qty'] * 10)
    before_in, before_out = _stock(tmp_db, f['in_pid']), _stock(tmp_db, f['out_pid'])

    payload = {'multiplier': '1', 'run_token': 'CONV-test-typed-replay',
               'reference_no': 'ใบผลิต-001'}
    admin_client.post(f"/conversions/{f['id']}/run", data=dict(payload))
    admin_client.post(f"/conversions/{f['id']}/run", data=dict(payload))

    assert _stock(tmp_db, f['in_pid']) == before_in - f['in_qty'], "input consumed twice"
    assert _stock(tmp_db, f['out_pid']) == before_out + f['output_qty'], "output produced twice"


def test_typed_reference_no_is_never_deduped(tmp_db):
    """Put, 2026-08-01: reusing a typed เลขที่เอกสาร across two real runs must
    keep working. Two real runs come from two page loads, so they carry two
    DIFFERENT tokens — only the token decides."""
    import models
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], f['in_qty'] * 10)
    before_in = _stock(tmp_db, f['in_pid'])

    ok1, msg1, _ = models.run_conversion(f['id'], 1, reference_no='ใบผลิต-001',
                                         run_token='CONV-tok-A')
    ok2, msg2, _ = models.run_conversion(f['id'], 1, reference_no='ใบผลิต-001',
                                         run_token='CONV-tok-B')

    assert ok1, msg1
    assert ok2, f'a typed reference_no was wrongly deduped: {msg2}'
    assert _stock(tmp_db, f['in_pid']) == before_in - f['in_qty'] * 2


def test_typed_reference_no_is_still_what_lands_on_the_ledger(tmp_db):
    """The token is bookkeeping for the guard, not a document number. What the
    operator typed must remain the reference_no on the rows."""
    import models
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], f['in_qty'] * 4)
    ok, msg, _ = models.run_conversion(f['id'], 1, reference_no='ใบผลิต-777',
                                       run_token='CONV-tok-visible')
    assert ok, msg
    row = _q(tmp_db, "SELECT reference_no FROM transactions WHERE product_id=?"
                     " ORDER BY id DESC LIMIT 1", f['in_pid'])[0]
    assert row['reference_no'] == 'ใบผลิต-777', \
        f"the token leaked into the document number: {row['reference_no']}"


def test_web_post_without_a_token_is_refused(tmp_db, admin_client):
    """A conversion POST carrying no token cannot be deduped, so the route must
    not run it — same stance the app already takes for a missing CSRF token.
    A stale tab gets one reload, not a silent unguarded conversion."""
    f = _a_formula(tmp_db)
    _force_stock(tmp_db, f['in_pid'], f['in_qty'] * 4)
    before_in = _stock(tmp_db, f['in_pid'])

    admin_client.post(f"/conversions/{f['id']}/run", data={'multiplier': '1'})

    assert _stock(tmp_db, f['in_pid']) == before_in, "a tokenless POST converted"
