"""Every writer of cost must be a decision on the record (#590, design §7).

Two censuses, each shaped so a NEW unsigned writer goes red:

1. SCHEMA: every column in `data/schema.sql` whose name says cost or WACC is
   either guarded — shown by an unsigned write being REFUSED and a signed one
   landing, never by reading trigger text (an audit trigger naming the same
   columns satisfies a text match with the guard deleted) — or allowlisted here
   with its reason.
2. WRITERS: every SQL write to a cost table (or to a table named at runtime) in
   `inventory_app/` and `scripts/` is pinned per file, verb and table, with how
   it is signed. A file-level allowlist would answer the wrong question ("this
   file is known") — a new write inside a known file must go red too.

⛔ WHAT THE WRITER SWEEP CANNOT SEE, so nobody reads it as proof:
    · SQL assembled at runtime from pieces that are never adjacent literals,
      a table name arriving as an argument, SQL in a non-`.py` file;
    · a products UPDATE whose SET list is built from a variable the sweep
      cannot read (those are pinned by name below and their allowlists named).
The DB guard is what refuses an unsigned write. This only moves the failure
into CI, and names the writers a reviewer has to think about.
"""
import os
import re
import sqlite3

import pytest

import actor
from tests.test_source_doc_writer_coverage import normalise

HERE = os.path.dirname(__file__)
REPO = os.path.abspath(os.path.join(HERE, '..'))
APP = os.path.join(REPO, 'inventory_app')
SCRIPTS = os.path.join(REPO, 'scripts')
SCHEMA = os.path.join(REPO, 'data', 'schema.sql')
PUT = actor.Actor(source='manual', who='put', kind='script', detail='census')


# ── 1. schema census ─────────────────────────────────────────────────────────

GUARDED = {
    ('products', 'cost_price'), ('products', 'opening_cost'),
    ('product_cost_ledger', 'unit_cost'), ('product_cost_ledger', 'wacc_after'),
    ('conversion_cost_log', 'total_input_cost'), ('conversion_cost_log', 'unit_cost'),
}
ALLOWLISTED = {
    ('pending_product_suggestions', 'suggested_cost'):
        'a suggestion shown while a code is still unmapped; it becomes a cost '
        'only when a product is created, which is itself a products INSERT (ceiling)',
}
COST_NAME = re.compile(r'cost|wacc', re.I)


@pytest.fixture
def fresh(tmp_path):
    path = str(tmp_path / 'census.db')
    raw = sqlite3.connect(path)
    with open(SCHEMA, encoding='utf-8') as f:
        raw.executescript(f.read())
    raw.close()
    seed = actor.install(sqlite3.connect(path), PUT)
    seed.execute("INSERT INTO products (id, product_name, cost_price, opening_cost)"
                 " VALUES (1, 'ทดสอบ', 10, 10)")
    seed.execute("INSERT INTO product_cost_ledger (product_id, event_type, event_date,"
                 " qty_change, unit_cost, stock_after, wacc_after)"
                 " VALUES (1, 'INITIAL', '2026-03-03', 1, 10, 1, 10)")
    seed.execute("INSERT INTO conversion_cost_log (output_product_id, event_date, output_qty,"
                 " total_input_cost, unit_cost) VALUES (1, '2026-09-01', 1, 10, 10)")
    seed.commit()
    seed.close()
    actor.set_fallback(None)
    return path


def _cost_columns(path):
    conn = sqlite3.connect(path)
    out = set()
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for col in conn.execute(f'PRAGMA table_info("{table}")'):
            if COST_NAME.search(col[1]):
                out.add((table, col[1]))
    return out


def test_every_cost_column_is_guarded_or_allowlisted(fresh):
    found = _cost_columns(fresh)
    assert ('products', 'cost_price') in found, 'control: the column scan is broken'
    unknown = found - GUARDED - set(ALLOWLISTED)
    assert not unknown, f'a cost column with no guard and no recorded reason: {sorted(unknown)}'
    assert not (GUARDED | set(ALLOWLISTED)) - found, 'a listed column no longer exists'
    for key, reason in ALLOWLISTED.items():
        assert len(reason.split()) >= 8, f'{key}: not a reason'


def _writes(table, column):
    return {
        'UPDATE': f'UPDATE {table} SET {column} = {column} + 1',
        'INSERT': None if table == 'products' else (
            "INSERT INTO product_cost_ledger (product_id, event_type, event_date, qty_change,"
            " unit_cost, stock_after, wacc_after) VALUES (1, 'X', '2026-09-02', 1, 1, 1, 1)"
            if table == 'product_cost_ledger' else
            "INSERT INTO conversion_cost_log (output_product_id, event_date, output_qty,"
            " total_input_cost, unit_cost) VALUES (1, '2026-09-02', 1, 1, 1)"),
        'DELETE': None if table == 'products' else f'DELETE FROM {table}',
    }


@pytest.mark.parametrize('table,column', sorted(GUARDED))
def test_each_guarded_column_refuses_an_unsigned_write_and_takes_a_signed_one(fresh, table, column):
    for verb, sql in _writes(table, column).items():
        if sql is None:
            continue           # products INSERT/DELETE are the stated ceiling (design §5)
        anon = actor.install(sqlite3.connect(fresh), None)
        with pytest.raises(sqlite3.IntegrityError, match=actor.REFUSAL_MARK):
            anon.execute(sql)
        anon.rollback()
        anon.close()
        signed = actor.install(sqlite3.connect(fresh), PUT)
        signed.execute(sql)                                     # control
        signed.rollback()
        signed.close()


# ── 2. writer census ─────────────────────────────────────────────────────────

_DYN = r'(?:\{[a-z_.]*\}|%s)'
_TABLE = rf'("?(?:products|product_cost_ledger|conversion_cost_log)\b"?|{_DYN})'
_QUAL = r'(?:[a-z_]+\.)?'
_UPDATE = re.compile(rf'\bUPDATE\s+(?:OR\s+\w+\s+)?{_QUAL}{_TABLE}(?:\s+(?:AS\s+)?[a-z]\w*)?'
                     rf'\s+SET\s+(.*?)(?:\s+WHERE\b|\s+RETURNING\b|["\';]|$)', re.I)
_OTHER = (('INSERT', re.compile(rf'\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+{_QUAL}{_TABLE}', re.I)),
          ('REPLACE', re.compile(rf'\bREPLACE\s+INTO\s+{_QUAL}{_TABLE}', re.I)),
          ('DELETE', re.compile(rf'\bDELETE\s+FROM\s+{_QUAL}{_TABLE}', re.I)))
# A products UPDATE is a cost write only when it SETs a cost column, or when its
# SET list is built at runtime (`{set_clause}`), which the sweep cannot read.
_COST_SET = re.compile(r'\b(?:cost_price|opening_cost)\b|\{', re.I)


def findings(src):
    flat = normalise(src)
    out = set()
    for m in _UPDATE.finditer(flat):
        table = m.group(1).strip('"').lower()
        if table == 'products' and not _COST_SET.search(m.group(2)):
            continue
        out.add(('UPDATE', table))
    for verb, rx in _OTHER:
        for m in rx.finditer(flat):
            out.add((verb, m.group(1).strip('"').lower()))
    return out


def _scan(root, prefix=''):
    found = {}
    for dp, _, files in os.walk(root):
        if '__pycache__' in dp:
            continue
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(dp, name)
            with open(path, encoding='utf-8') as f:
                hit = findings(f.read())
            if hit:
                found[prefix + os.path.relpath(path, root)] = hit
    return found


# (findings, how it is signed or why it is not a cost write). ⛔ Name the path.
APP_WRITERS = {
    'models/wacc.py': ({('DELETE', 'product_cost_ledger'), ('INSERT', 'product_cost_ledger'),
                        ('UPDATE', 'products')},
                       'the WACC engine: actor.require before its first write, then runs inside '
                       'acting_as(detail=wacc:<op>) on the caller\'s signed connection'),
    'models/conversions.py': ({('INSERT', 'conversion_cost_log')},
                              'run_conversion: require_actor_or_alert on its own connection before '
                              'BEGIN IMMEDIATE; the trigger stamps written_by'),
    'models/products.py': ({('INSERT', 'products'), ('UPDATE', 'products')},
                           'update_product preflights when cost keys are present; the INSERTs are '
                           'create_product/create_structured_product, the products INSERT ceiling'),
    'database.py': ({('INSERT', 'conversion_cost_log'), ('INSERT', 'product_cost_ledger'),
                     ('UPDATE', 'products')},
                    'the _UNSIGNED_PROBES of prepare_staged_db: deliberately unsigned writes that '
                    'MUST be refused on a staged file, always rolled back'),
    'blueprints/admin.py': ({('DELETE', '{table}'), ('INSERT', '{table}')},
                            'the master upload replaces whole tables incl. products; signed by the '
                            'request, refused unsigned, and audited inside its own transaction (C3)'),
    'naming_cascade.py': ({('UPDATE', 'products')},
                          'save_product: SET list comes from _EDITABLE_TEXT plus brand_id, which '
                          'holds no cost column'),
    'vat_book_builder.py': ({('INSERT', 'products')},
                            'seed_products_from_stmas into the VAT book being built; a products '
                            'INSERT (ceiling), and the build runs as --uploader'),
    'express_registers.py': ({('DELETE', '{table_name}'), ('INSERT', '{table.name}')},
                             'Express register tables named by express_registers.TABLES; none is '
                             'a cost table'),
    'models/_shared.py': ({('DELETE', '{table}'), ('UPDATE', '{table}')},
                          'declared_update/declared_delete: SOURCE_DOC_TABLES only (sales and '
                          'purchase), checked by a ValueError before the SQL'),
    'models/bsn_sync.py': ({('DELETE', '{table}'), ('UPDATE', '{table}'), ('UPDATE', '{t}')},
                           'sales/purchase source rows only (synced_to_stock, unit); the cost step '
                           'runs through the engine after update_unit_conversion_ratio preflights'),
    'models/imports.py': ({('DELETE', '{table}'), ('INSERT', '{table}'), ('UPDATE', '{table}')},
                          'import_weekly on sales/purchase source rows, after '
                          'require_actor_or_alert at its entry'),
    'models/mapping.py': ({('UPDATE', '{table}')},
                          'repoint_bsn_code resets synced_to_stock on sales/purchase after its '
                          'actor preflight'),
}

SCRIPT_WRITERS = {
    'scripts/merge_product.py': ({('UPDATE', '{t}')},
                                 'LIVE tool: re-points product_id on every table carrying one '
                                 '(incl. the ledger) through database.script_connection', 'script'),
    'scripts/2026_09_19_gross_to_piece.py': (
        {('UPDATE', '%s'), ('UPDATE', 'product_cost_ledger'), ('UPDATE', 'products')},
        'dated, but rebase() is an engine another script loads: main() opens '
        'database.script_connection with --operator/--reason', 'script'),
    'scripts/2026_08_17_bolt_dozen_to_piece.py': ({('UPDATE', 'product_cost_ledger')},
        'dated one-off applied 2026-08-17; raw connection, aborts if re-run (accepted)', 'dated'),
    'scripts/2026_09_19_fix_pack_ratios_592.py': ({('UPDATE', '%s')},
        'dated one-off applied 2026-09-19; resets synced_to_stock then recalculates, aborts if re-run', 'dated'),
    'scripts/2026_09_19_split_belco_582.py': ({('UPDATE', '%s')},
        'dated one-off applied 2026-09-19; resets synced_to_stock then recalculates, aborts if re-run', 'dated'),
    'scripts/backfill_opening_cost_20260617.py': ({('UPDATE', 'products')},
        'dated one-off 2026-06-17 backfilling opening_cost; aborts if re-run (accepted)', 'dated'),
    'scripts/hammer_bundle_datafix.py': ({('UPDATE', 'products')},
        'dated Phase 1 datafix of the 2026-08-14 hammer bundle plan; aborts if re-run', 'dated'),
    'scripts/apply_decision_remaps.py': ({('INSERT', 'products'), ('UPDATE', '{t}')},
        'dated remap one-off; clones products (INSERT ceiling) and re-points product_id', 'dated'),
    'scripts/cleanup_split_mapping_stubs.py': ({('UPDATE', '{t}')},
        'DEPRECATED 2026-05-20 one-off re-pointing product_id; aborts if re-run', 'dated'),
    'scripts/p2p3_split_hinges.py': ({('UPDATE', '{table}')},
        'dated split one-off re-pointing sales/purchase rows, not a cost table', 'dated'),
    'scripts/reimport_2026_04_28/run.py': (
        {('DELETE', '{table}'), ('INSERT', '{table}'), ('UPDATE', '{table}')},
        'dated 2026-04-28 reimport of sales/purchase source rows, not a cost table', 'dated'),
    'scripts/import_express.py': ({('DELETE', '{child_table}'), ('DELETE', '{header_table}')},
        'Express document header/child tables, not a cost table', 'not-cost'),
    'scripts/apply_normalize_round1.py': ({('UPDATE', 'products')},
        'SET list built from APPLY_FIELDS, which holds naming columns only', 'not-cost'),
    'scripts/apply_product_naming.py': ({('UPDATE', 'products')},
        'SET column comes from _FIELD_WHITELIST, naming columns only', 'not-cost'),
    'scripts/apply_platform_overview_mapping.py': ({('INSERT', 'products')},
        'creates zero-cost stub products; a products INSERT (ceiling)', 'ceiling'),
    'scripts/import_listing_mapping_csv.py': ({('INSERT', 'products')},
        'creates zero-cost stub products; a products INSERT (ceiling)', 'ceiling'),
}


def test_every_app_cost_writer_is_on_the_record():
    found = _scan(APP)
    assert 'models/wacc.py' in found, 'control: the engine must always appear'
    assert found == {k: v[0] for k, v in APP_WRITERS.items()}, (
        'a cost writer appeared, moved or vanished; classify it in APP_WRITERS')


def test_every_script_cost_writer_is_on_the_record():
    found = _scan(SCRIPTS, prefix='scripts/')
    assert 'scripts/merge_product.py' in found, 'control: a known writer must appear'
    assert found == {k: v[0] for k, v in SCRIPT_WRITERS.items()}, (
        'a script cost writer appeared, moved or vanished; classify it in SCRIPT_WRITERS')


def test_every_entry_carries_a_reason():
    for name, entry in {**APP_WRITERS, **SCRIPT_WRITERS}.items():
        assert len(entry[1].split()) >= 8, f'{name}: not a reason: {entry[1]!r}'


def test_scripts_recorded_as_signed_really_open_a_script_connection():
    signed = [n for n, e in SCRIPT_WRITERS.items() if e[2] == 'script']
    assert len(signed) == 2
    for name in signed:
        with open(os.path.join(REPO, name), encoding='utf-8') as f:
            body = normalise(f.read())
        assert 'script_connection(' in body, f'{name}: recorded as signed, but it is not'


# One rogue source per SQL shape. Each must be SEEN; the last must not be.
ROGUES = [
    ('literal', '"UPDATE products SET cost_price = ? WHERE id = ?"', ('UPDATE', 'products')),
    ('or-replace', '"UPDATE OR REPLACE products SET cost_price = 1"', ('UPDATE', 'products')),
    ('qualified', '"UPDATE main.products SET opening_cost = 1"', ('UPDATE', 'products')),
    ('quoted', '\'UPDATE "products" SET cost_price = 1\'', ('UPDATE', 'products')),
    ('f-string', 'f"UPDATE {table} SET x = ?"', ('UPDATE', '{table}')),
    ('percent', '"UPDATE %s SET x = ?" % table', ('UPDATE', '%s')),
    ('format', '"UPDATE {} SET x = ?".format(table)', ('UPDATE', '{}')),
    ('concat', '"UPDATE products " + "SET cost_price = ?"', ('UPDATE', 'products')),
    ('insert', '"INSERT INTO product_cost_ledger (product_id) VALUES (?)"',
     ('INSERT', 'product_cost_ledger')),
    ('insert-select', '"INSERT INTO product_cost_ledger SELECT * FROM upl.product_cost_ledger"',
     ('INSERT', 'product_cost_ledger')),
    ('copy-all-columns', 'f"INSERT INTO products ({\',\'.join(cols)}) VALUES ({ph})"',
     ('INSERT', 'products')),
    ('insert-or-replace', '"INSERT OR REPLACE INTO products (id, cost_price) VALUES (?, ?)"',
     ('INSERT', 'products')),
    ('replace-into', '"REPLACE INTO products (id, cost_price) VALUES (?, ?)"',
     ('REPLACE', 'products')),
    ('upsert', '"INSERT INTO products (id) VALUES (?) ON CONFLICT(id) DO UPDATE SET cost_price = 1"',
     ('INSERT', 'products')),
    ('delete', '"DELETE FROM conversion_cost_log WHERE id = ?"', ('DELETE', 'conversion_cost_log')),
    ('dynamic-set', 'f"UPDATE products SET {set_clause} WHERE id = ?"', ('UPDATE', 'products')),
]


@pytest.mark.parametrize('shape,src,expected', ROGUES, ids=[r[0] for r in ROGUES])
def test_the_sweep_sees_every_shape(shape, src, expected, tmp_path):
    (tmp_path / 'rogue.py').write_text(f'conn.execute({src})\n', encoding='utf-8')
    assert expected in _scan(str(tmp_path)).get('rogue.py', set())


def test_a_non_cost_products_update_is_not_a_finding(tmp_path):
    (tmp_path / 'fine.py').write_text('conn.execute("UPDATE products SET product_name = ?")\n',
                                      encoding='utf-8')
    (tmp_path / 'cost.py').write_text('conn.execute("UPDATE products SET cost_price = ?")\n',
                                      encoding='utf-8')
    found = _scan(str(tmp_path))
    assert 'cost.py' in found                                             # control
    assert 'fine.py' not in found
