"""
Shared pytest fixtures.

GOLDEN RULE: tests NEVER touch the live DB.
Every fixture that needs a database copies inventory.db to a tmp_path
and monkeypatches config.DATABASE_PATH so models/database use the copy.

The inventory_app modules use bare imports (`from database import ...`).
pytest.ini adds inventory_app/ to pythonpath so those imports work.
"""
import os

# Inject dummy secrets BEFORE any test imports config. The app's config.py
# now requires SECRET_KEY / ADMIN_PASSWORD env vars (no fallback defaults),
# so without these the test collection phase blows up. test_config_secrets.py
# uses monkeypatch.setattr(os, 'environ', ...) which replaces os.environ
# wholesale for the test duration, so it isn't affected by these defaults.
os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_PASSWORD', 'test-only-admin')

import shutil
import sqlite3

import pytest

# Disable CSRF for the test client so the ~400 existing POST tests don't need
# to be rewritten to include a csrf_token. tests/test_csrf_protection.py opts
# back in per-fixture to assert the gate works on a real request path.
#
# Unconditional override (NOT setdefault) — a developer with WTF_CSRF_ENABLED=True
# exported in their shell would otherwise tank the entire test suite. Tests
# must NEVER run with CSRF on by accident; opt-in via csrf_client is the
# supported path.
os.environ['WTF_CSRF_ENABLED'] = 'False'

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LIVE_DB   = os.path.join(REPO_ROOT, 'inventory_app', 'instance', 'inventory.db')

# Worktrees don't carry the instance/inventory.db — fall back to the main
# workspace's live DB so the schema-clone fixture finds it. Read-only at the
# fixture level (URI mode=ro), but we avoid symlinking the file into the
# worktree because that turns `python app.py` from the worktree into a
# live-DB-write footgun.
if not os.path.exists(LIVE_DB):
    _WORKSPACE_LIVE_DB = os.path.expanduser(
        '~/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db'
    )
    if os.path.exists(_WORKSPACE_LIVE_DB):
        LIVE_DB = _WORKSPACE_LIVE_DB


# ── DB fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """
    Copy the live inventory.db into tmp_path and point config.DATABASE_PATH at it.

    Yields the absolute path to the temp DB.
    Any module that re-reads config.DATABASE_PATH (database.get_connection does)
    will hit the temp file, not the live one.
    """
    if not os.path.exists(LIVE_DB):
        pytest.skip(f"Live DB not found at {LIVE_DB} — skipping integration test")

    dst = tmp_path / "inventory.db"
    shutil.copy2(LIVE_DB, dst)

    # Some sessions also leave -wal/-shm sidecars; copy if present so the snapshot is consistent.
    for suffix in ('-wal', '-shm'):
        side = LIVE_DB + suffix
        if os.path.exists(side):
            shutil.copy2(side, str(dst) + suffix)

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', str(dst))
    # database.py + hr.py both `from config import DATABASE_PATH` (snapshot at
    # module load); patch their snapshots too so routes that open their own
    # connections (e.g. `payroll_reopen` → `hr._connect(None)`) hit the temp DB.
    import database
    monkeypatch.setattr(database, 'DATABASE_PATH', str(dst))
    import hr
    monkeypatch.setattr(hr, 'DATABASE_PATH', str(dst))
    # commission.py also `from config import DATABASE_PATH` at module load, so
    # its helpers called WITHOUT an explicit db_path (e.g. record_payout via the
    # /commission/payout route) would otherwise write to the LIVE DB. Patch its
    # snapshot too — same reason as database/hr above.
    import commission
    monkeypatch.setattr(commission, 'DATABASE_PATH', str(dst))
    # cashflow.py / payments_alloc.py / revenue.py also `from config import
    # DATABASE_PATH` at module load — and models/__init__.py transitively
    # imports cashflow (for BSN_AR_PREDICATE), so `import models` anywhere in
    # the test session permanently snapshots cashflow.DATABASE_PATH to
    # whatever tmp_db happened to be active at that FIRST import. Every route
    # that calls these modules without an explicit db_path (e.g. /ar's
    # `cf_mod.ar_aging()`) would then silently hit a stale/torn-down tmp path
    # from an earlier, unrelated test — order-dependent "unable to open
    # database file" crashes. Patch all three snapshots too.
    import cashflow
    monkeypatch.setattr(cashflow, 'DATABASE_PATH', str(dst))
    import payments_alloc
    monkeypatch.setattr(payments_alloc, 'DATABASE_PATH', str(dst))
    import revenue
    monkeypatch.setattr(revenue, 'DATABASE_PATH', str(dst))

    return str(dst)


@pytest.fixture
def tmp_db_conn(tmp_db):
    """sqlite3 connection on the temp DB (autocommit-style usage; tests may commit)."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def tmp_db_conn_hr_clean(tmp_db):
    """Like `tmp_db_conn` but wipes payroll/leave state from the copied live
    DB so HR-engine tests can generate runs for any month without colliding
    with real production data. Preserves employees, salary history,
    leave_types, hr_config, entitlements (tests rely on the seeded EMP001/
    EMP002 + 5 leave types + 4 hr_config rows from mig 054).
    """
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # FK order: cashbook_transactions references payroll_items/payroll_runs
    # (salary pay-events, ADR 0006) and salary_advances (ADR 0008) — the live
    # DB carries such rows since 2026-06-30, so clear those links first, then
    # payroll_items + salary_advances (both reference payroll_runs), then runs.
    conn.executescript("""
        DELETE FROM cashbook_transactions
         WHERE payroll_item_id IS NOT NULL
            OR payroll_run_id  IS NOT NULL
            OR salary_advance_id IS NOT NULL;
        DELETE FROM payroll_items;
        DELETE FROM salary_advances;
        DELETE FROM payroll_runs;
        DELETE FROM leave_requests;
    """)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """
    A data-less DB carrying the FULL live schema (every table/index/trigger/
    view, zero rows). Use when a test wants a clean slate.

    Built by cloning the LIVE schema (not via init_db). `database.init_db()` on
    a brand-new DB now builds from the checked-in `data/schema.sql` baseline and
    works (it no longer replays the 014/018/019 historical chain that used to
    raise FOREIGN KEY / CHECK errors — see test_fresh_db_build.py). We still
    clone from the live DB here so the fixture reflects the EXACT current live
    schema, including any drift not yet folded into schema.sql — keep it that
    way. Do NOT revert this to init_db() — see tests/test_empty_db_fixture.py.
    """
    if not os.path.exists(LIVE_DB):
        pytest.skip(f"Live DB not found at {LIVE_DB} — skipping schema-clone fixture")

    db_path = tmp_path / "fresh.db"

    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    try:
        # tables → indexes → triggers → views so dependencies exist in order
        objects = src.execute(
            """SELECT sql FROM sqlite_master
                WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
                ORDER BY CASE type
                    WHEN 'table' THEN 0 WHEN 'index' THEN 1
                    WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END"""
        ).fetchall()
    finally:
        src.close()

    dst = sqlite3.connect(str(db_path))
    try:
        dst.execute("PRAGMA foreign_keys = OFF")
        for (sql,) in objects:
            dst.execute(sql)
        dst.commit()
    finally:
        dst.close()

    import config
    monkeypatch.setattr(config, 'DATABASE_PATH', str(db_path))
    # database.py imports DATABASE_PATH at module load time — patch there too.
    import database
    monkeypatch.setattr(database, 'DATABASE_PATH', str(db_path))

    return str(db_path)


@pytest.fixture
def empty_db_conn(empty_db):
    conn = sqlite3.connect(empty_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# ── Synthesized BSN sample data ──────────────────────────────────────────────

# Keep these in sync with the real format observed in
# inventory_app/imports/ซื้อ_24.4.69.csv and ยอดขาย_แยกตามลูกค้า_15.4.69.csv.
# Lines are CSV-quoted, encoded cp874, use \xa0 as in-line padding.

PURCHASE_SAMPLE_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                            หน้า   :        1"',
    '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"   สินค้า  วันที่  เลขที่เอกสาร       จำนวน   คืน  ราคาต่อหน่วย\xa0VAT\xa0\xa0 ส่วนลด       รวมเงิน  ส่วนลดรวม     ยอดซื้อสุทธิ อ้างถึง"',
    '"-------------------------------------------------------------------------------------------------------------------------------------"',
    '"  ย้งเจริญการพิมพ์\xa0/ย้ง"',
    '"   กล่องในปุ๊ก#7\xa0/Pกล่อง3"',
    '"        24/04/69   HP6900023       22965.00 กล            0.69  0                 15845.85                 15845.85 PO0000227-  1"',
    '"   ใบตัดเพชร\xa04\\"\xa0/031บ4120"',
    '"        23/04/68   RR6900061          12.00 อน           70.00  2      25+5%        598.50                   598.50"',
]

SALES_SAMPLE_LINES = [
    '"(BSN)บจก.บุญสวัสดิ์นำชัย                                                                                             หน้า   :        1"',
    '"  รายงานประวัติการขาย\xa0แยกตามลูกค้า"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  สินค้า วันที่ เลขที่เอกสาร          จำนวน   คืน   ราคาต่อหน่วย\xa0VAT   ส่วนลด       รวมเงิน  ส่วนลดรวม  ยอดขายสุทธิ  อ้างอิง  หมายเหตุ"',
    '"--------------------------------------------------------------------------------------------------------------------------------------"',
    '"  ไพศาลโลหะภัณฑ์(ตลาดพลู)\xa0/01พ02"',
    '"   ใบตัดเพชร\xa04\xa0#GL-888(แดง)\xa0/031บ4120"',
    '"      04/04/69   IV6900503-  1        24.00 ใบ          160.00  1                  3840.00                  3840.00"',
    '"      04/04/69   IV6900503-  2         3.00 ใบ            0.00  1                     0.00                     0.00               ***"',
    '"  วรสวัสดิ์\xa0ฮาร์ดแวร์\xa0/01อ35"',
    '"   กลอนห้องน้ำกลาง\xa0STL#430\xa0(P)\xa0/001ก3435"',
    '"      04/04/69   IV6900501-  1        48.00 ผง           30.00  2        20%       1152.00                  1152.00"',
    # Decimal-baht line discount (ส่วนลดเป็นบาท): 50.00 - 32.00 discount = 18.00 total
    '"  ทดสอบส่วนลดทศนิยม\xa0/01ท99"',
    '"   ดจ./ปูนโรตารี่\xa08x110\xa0มิล\'GL\'\xa0/010ด7130"',
    '"      03/04/69   IV6900498-  2         1.00 ดก           50.00  1      32.00         18.00                    18.00"',
    # Doc-level discount column (ส่วนลดรวม) as percent: 1764 × 0.98 = 1728.72
    # Old regex captured only "2" (the digit before %) as net, dropping 1728.72 entirely.
    '"      04/03/69   IV6900370-  2         1.00 ลง         1960.00  1        10%       1764.00         2%       1728.72"',
    # qty!unit collision: BSN occasionally emits qty and unit glued by '!' instead of whitespace
    # (e.g. "2.00!หล"). Old regex used \s+ between qty and unit groups, so the whole row failed
    # to match and was silently dropped. ~137 such rows existed in the 2024–2026 sales export.
    '"  ทดสอบbangseparator\xa0/01บ99"',
    '"   ดจ./ปูนโรตารี่bang\xa0/010ด7131"',
    '"      19/04/68   IV6801044-  4         2.00!หล         1317.79  2        10%       2372.02                  2372.02 SO0"',
]


@pytest.fixture
def sample_purchase_file(tmp_path):
    p = tmp_path / "ซื้อ_sample.csv"
    p.write_text("\n".join(PURCHASE_SAMPLE_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def sample_sales_file(tmp_path):
    p = tmp_path / "ขาย_sample.csv"
    p.write_text("\n".join(SALES_SAMPLE_LINES) + "\n", encoding="cp874")
    return str(p)


@pytest.fixture
def patch_models_conn(monkeypatch):
    """Patch a get_connection factory everywhere the models package sees it.

    Pre-split (monolithic models.py) a single
    ``monkeypatch.setattr(models, "get_connection", ...)`` covered every model
    function. Post Phase-11/12, each models/ submodule holds its own
    ``from database import get_connection`` binding, so a facade-only patch
    silently misses moved code (the landmine guarded by
    tests/test_models_package.py). This applies the same factory to the
    facade AND every submodule that binds get_connection — exact monolith
    semantics.
    """
    import types

    def _patch(factory):
        import models as _m
        monkeypatch.setattr(_m, "get_connection", factory)
        for _name in dir(_m):
            _sub = getattr(_m, _name)
            if isinstance(_sub, types.ModuleType) and hasattr(_sub, "get_connection"):
                monkeypatch.setattr(_sub, "get_connection", factory)
    return _patch
