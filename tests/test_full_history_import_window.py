"""No production path imports full Express history into the main ledger (#648).

The hazard, measured on a copy of prod on 2026-09-23. A full-history purchase
run re-points the #582 BELCO line (HP6900041 / `528ด8655`) from pid 2092 back to
1305, because the code still maps to 1305 and `bsn_line.field_diff` compares
`product_id`. That replaces the row with a new id, overwrites its
`change_source` from 'manual' to 'import', wipes `change_actor`/`change_reason`
so a second run can no longer tell a hand fix was lost, and drops
`products.cost_price` for 1305 from ฿24.50 to ฿7.00 — the #546 zero-stock branch
that #582 exists to undo. Three more lines move the same way (IV6901138-7,
IV6900674-1, IV6701854-2), and the same run would INSERT 131,616 sales and
16,731 purchase lines Sendy deliberately does not hold, since its era starts
2024-01-01 while the DBF book runs further back.

Why a window guard rather than a change to the importer (Put, 2026-09-23): the
DBF route takes `commit_express_dbf`'s default 60-day window with no UI control,
and the one caller that asks for full history writes `vat_book.db`, a different
database. So the WINDOW is the invariant worth pinning, rather than a new rule
inside the highest-blast-radius file in the app.

⚠ CORRECTED 2026-09-25, and the correction is the important part. The first
version of this docstring said the revert was not reachable at all, because "the
text path refuses a history export in BOTH preview and commit". That is FALSE,
and it was my claim, not the code's. `parse_weekly.is_history_export` measures a
file's filter start against THAT FILE'S OWN report date — its docstring says so
outright, "We measure start-vs-report, NOT the filter span" — so an ARCHIVED
weekly passes cleanly. Measured: a purchase weekly with report date 20/07/69 and
filter start 13 ก.ค. 2569 gives `is_history_export=False` and
`_reject_history_export` ACCEPTS it, while the same filter start on a file dated
25/09/69 is refused. That archived file covers 2026-07-17 and therefore contains
HP6900041. `/import-data/confirm` also has no export-date watermark — the DBF
route's `_claim_export_date` / `forced_older` logic has no counterpart there — so
an older file is accepted silently.

So the live vector is a re-upload of an archived weekly text export, not a
full-history dump. `test_an_archived_weekly_is_accepted_and_is_the_live_vector`
below pins that as an executable fact rather than leaving it in prose, and
`test_the_text_confirm_path_has_no_export_date_watermark` pins the asymmetry that
lets it through. Neither is a wish: they assert what the code does TODAY, so if
someone closes the door they go red and have to be rewritten deliberately.

What this canNOT see, stated so nobody reads it as more than it is:
  * a window passed in a variable or `**kwargs` — the sweep records the source
    text and the allowlist has to adjudicate it, which is why every entry
    carries a reason rather than just a number;
  * a caller that reaches `models.import_weekly` directly, bypassing
    `commit_express_dbf` entirely. That is how the reproduction for #648 was
    run, and `test_import_weekly_is_not_called_outside_the_router` below is the
    part that covers it;
  * anything under `tests/`, deliberately — a test asking for full history
    against its own fixture DB is the normal way to test the builders.

Prior art: tests/test_last_purchase_population_coverage.py (AST extraction plus
a per-shape anti-vacuity block) and tests/test_revenue_filter_coverage.py
(allowlist entries that must carry a written reason).
"""
from __future__ import annotations

import ast
import inspect
import os

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCANNED = ("inventory_app", "scripts")

# site -> (since_days as written, db_path as written, why)
# "<default>" means the argument was not passed at all.
ALLOWED = {
    ("inventory_app/blueprints/bsn.py", "express_dbf_upload"): (
        "<default>", "config.DATABASE_PATH",
        "The daily DBF upload. Takes the 60-day default deliberately: a daily "
        "upload only needs the recent window, and that window is what keeps the "
        "import fast and what stops it re-resolving old hand-corrected lines "
        "(#648). There is no UI control for the window and there must not be."),
    ("inventory_app/vat_book_builder.py", "_build"): (
        "None", "db_path",
        "The VAT-book rebuild genuinely wants the whole book. It is safe not "
        "because of the FILENAME — it writes its subprocess's own "
        "config.DATABASE_PATH — but because `_guard_subprocess_target()` runs "
        "first and refuses unless VAT_BOOK_BUILD=1 and the target does not yet "
        "exist. The live ledger always exists, so this can never reach it. "
        "test_the_full_history_site_is_guarded_before_it_imports pins that."),
}

MAIN_DB_MARKERS = ("config.DATABASE_PATH", "DATABASE_PATH")


def _py_files():
    for root in SCANNED:
        for dirpath, _dirs, names in os.walk(os.path.join(REPO, root)):
            for n in sorted(names):
                if n.endswith(".py"):
                    p = os.path.join(dirpath, n)
                    yield os.path.relpath(p, REPO).replace(os.sep, "/"), p


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _arg_text(node: ast.Call, name: str, position: int) -> str:
    """The argument as WRITTEN, '<default>' when absent, '<**kwargs>' when
    it could be hiding in a splat. Source text, never a resolved value: a
    sweep that pretended to evaluate `since_days=w` would be lying."""
    for kw in node.keywords:
        if kw.arg == name:
            return ast.unparse(kw.value)
        if kw.arg is None:
            return "<**kwargs>"
    if len(node.args) > position:
        return ast.unparse(node.args[position])
    return "<default>"


def _sites(src: str, rel: str, callee: str = "commit_express_dbf"):
    """(rel, enclosing function) -> (since_days text, db_path text) per call."""
    found = {}

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, scope + [child.name])
            elif isinstance(child, ast.ClassDef):
                walk(child, scope + [child.name])
            else:
                if isinstance(child, ast.Call) and _callee(child) == callee:
                    # signature: (dataset_dir, db_path, since_days, ...)
                    found[(rel, ".".join(scope) or "<module>")] = (
                        _arg_text(child, "since_days", 2),
                        _arg_text(child, "db_path", 1))
                walk(child, scope)

    walk(ast.parse(src), [])
    return found


def _census(callee: str = "commit_express_dbf"):
    out = {}
    for rel, path in _py_files():
        with open(path, encoding="utf-8") as f:
            out.update(_sites(f.read(), rel, callee))
    return out


# ── the invariant ───────────────────────────────────────────────────────────

def test_the_default_window_is_sixty_days_not_full_history():
    """A None default would make every caller that omits it a #648 vector."""
    import import_router

    default = inspect.signature(import_router.commit_express_dbf).parameters["since_days"].default
    assert default == 60, (
        f"commit_express_dbf's since_days default is {default!r}. 60 is what "
        "keeps the daily upload from re-resolving old hand-corrected lines.")


def test_every_production_call_site_is_declared():
    census = _census()
    declared = {site: (w, db) for site, (w, db, _why) in ALLOWED.items()}
    assert census == declared, (
        "A call site of commit_express_dbf changed, appeared or moved. Each one "
        "must be listed in ALLOWED with the window it asks for, the database it "
        "writes and WHY that combination is safe. See #648: full history into "
        "the main ledger silently reverts hand re-points.\n"
        f"  measured: {census}\n  declared: {declared}")


@pytest.mark.parametrize("site", sorted(ALLOWED))
def test_every_entry_carries_a_reason(site):
    assert len(ALLOWED[site][2]) > 60, f"{site}: say why this window is safe here"


def test_no_full_history_call_writes_the_main_database():
    """The one rule that actually prevents #648."""
    offenders = []
    for (rel, func), (window, db) in _census().items():
        if window == "60":
            continue
        if window == "<default>":
            continue          # the default is pinned at 60 by the test above
        if any(m in db for m in MAIN_DB_MARKERS) or db == "<default>":
            offenders.append((rel, func, window, db))
    assert not offenders, (
        "These call sites ask for a wider-than-default window AND write the "
        "main ledger (db_path '<default>' means config.DATABASE_PATH). That is "
        "the #648 vector: it re-points hand-corrected lines and moves cost.\n"
        f"  {offenders}")


def test_the_full_history_site_is_guarded_before_it_imports():
    """The VAT-book build is the one caller that asks for full history. What
    keeps it off the live ledger is a guard, not a filename, so pin the guard.

    It must run BEFORE the import: a refusal afterwards would mean the rows had
    already landed."""
    path = os.path.join(REPO, "inventory_app", "vat_book_builder.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    build = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_build")
    order = [_callee(c) for c in ast.walk(build) if isinstance(c, ast.Call)
             if _callee(c) in ("_guard_subprocess_target", "commit_express_dbf")]
    assert order[:1] == ["_guard_subprocess_target"], (
        "_build must call _guard_subprocess_target() before it imports "
        f"anything; saw {order}")

    import vat_book_builder
    src = inspect.getsource(vat_book_builder._guard_subprocess_target)
    assert "VAT_BOOK_BUILD" in src, src
    assert "os.path.exists" in src, (
        "the guard must refuse a target that already exists — that clause is "
        "the whole reason a full-history build cannot reach the live ledger")


def test_the_history_gate_guards_both_preview_and_commit():
    """A crafted POST straight to /confirm must not slip a history dump past
    the preview, so BOTH entry points call the gate (#648 relies on this)."""
    path = os.path.join(REPO, "inventory_app", "import_router.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    callers = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(isinstance(c, ast.Call) and _callee(c) == "_reject_history_export"
                       for c in ast.walk(n))}
    for entry in ("preview_file", "commit_file"):
        assert entry in callers, (
            f"import_router.{entry} no longer calls _reject_history_export; a "
            f"full-history text export can reach the ledger. callers={callers}")


def _weekly(tmp_path, name, report, filter_start):
    """A minimal Express purchase weekly header, cp874 like the real thing. Only
    the two dates matter to the gate."""
    p = tmp_path / name
    p.write_text("\n".join([
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                                       หน้า   :        1"',
        '"  รายงานประวัติการซื้อ\xa0แยกตามผู้จำหน่าย"',
        f'"รหัสผู้จำหน่ายจาก  AA01             ถึง  ZZ99                   วันที่ : {report}"',
        f'"วันที่จาก   {filter_start}         ถึง  31\xa0ธ.ค.\xa02569"',
        '"-----------------------------------------------------------------"',
    ]) + "\n", encoding="cp874")
    return str(p)


def test_an_archived_weekly_is_accepted_and_is_the_live_vector(tmp_path):
    """The door #648 actually leaves open, pinned as a fact.

    `is_history_export` compares a file's filter start with THAT FILE'S report
    date, so an archived July weekly (7-day reach-back) is not a history export
    and is accepted — and it covers 2026-07-17, so it carries HP6900041. The same
    filter start on a file exported today is a 68-day reach-back and is refused.

    If someone closes this door, this test goes RED and must be rewritten
    deliberately. That is the point: the gap stops being a sentence in a PR body."""
    import import_router
    import parse_weekly

    archived = _weekly(tmp_path, 'archived.txt', '20/07/69', '13\xa0ก.ค.\xa02569')
    fresh = _weekly(tmp_path, 'fresh.txt', '25/09/69', '13\xa0ก.ค.\xa02569')

    assert parse_weekly.is_history_export(archived) is False, (
        'an archived weekly reads as a history export now; the #648 vector may be '
        'closed, in which case update this test and the module docstring')
    import_router._reject_history_export(archived)      # must NOT raise

    assert parse_weekly.is_history_export(fresh) is True
    with pytest.raises(import_router.HistoryExportBlocked):
        import_router._reject_history_export(fresh)


def test_the_text_confirm_path_has_no_export_date_watermark():
    """The asymmetry that lets the archived file through unnoticed.

    The DBF route claims the export timestamp (`_claim_export_date`) and makes the
    operator tick a box to import over a newer one. The text confirm path has no
    such call, so an older file is accepted silently. Pinned so that adding the
    watermark is a deliberate act that turns this red."""
    path = os.path.join(REPO, "inventory_app", "blueprints", "bsn.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    claimers = {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(isinstance(c, ast.Call) and _callee(c) == "_claim_export_date"
                        for c in ast.walk(n))}
    assert "express_dbf_upload" in claimers, (
        f"the DBF route no longer claims an export date; claimers={claimers}")
    assert "unified_import_confirm" not in claimers, (
        "the text confirm path now claims an export date — the #648 vector is "
        "narrower than this file says. Update the module docstring.")


def test_import_weekly_is_not_called_outside_the_router():
    """`commit_express_dbf` is not the only door. Anything calling
    `import_weekly` directly chooses its own entries and so its own window —
    which is exactly how #648 was reproduced."""
    census = _census("import_weekly")
    allowed = {
        ("inventory_app/import_router.py", "commit_express_dbf"),
        ("inventory_app/import_router.py", "commit_file"),
    }
    unexpected = {s for s in census if s not in allowed}
    assert not unexpected, (
        "These call import_weekly directly, bypassing the windowed router. Each "
        "decides for itself which documents to feed, so each can revert a hand "
        "re-point (#648). Declare it here with a reason, or route it through "
        f"commit_express_dbf.\n  {sorted(unexpected)}")


# ── anti-vacuity: the sweep must SEE each way a window can be written ───────
# Every entry here was checked to make the census non-empty; a sweep blind to a
# shape reads as coverage, which is worse than having none.

_SHAPES = {
    "keyword None": (
        "def f():\n    commit_express_dbf(d, db_path=p, since_days=None)\n", "None"),
    "positional third argument": (
        "def f():\n    commit_express_dbf(d, p, None)\n", "None"),
    "a variable": (
        "def f():\n    commit_express_dbf(d, db_path=p, since_days=w)\n", "w"),
    "an expression": (
        "def f():\n    commit_express_dbf(d, db_path=p, since_days=365 * 5)\n", "365 * 5"),
    "splatted kwargs": (
        "def f():\n    commit_express_dbf(d, **opts)\n", "<**kwargs>"),
    "module attribute call": (
        "def f():\n    import_router.commit_express_dbf(d, since_days=None)\n", "None"),
    "omitted": (
        "def f():\n    commit_express_dbf(d, db_path=p)\n", "<default>"),
    "inside a method": (
        "class R:\n    def f(self):\n        commit_express_dbf(d, since_days=None)\n", "None"),
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_the_sweep_sees_every_window_shape(shape):
    src, expected = _SHAPES[shape]
    sites = _sites(src, "x.py")
    assert sites, f"the sweep is blind to: {shape}"
    assert list(sites.values())[0][0] == expected, sites


def test_the_sweep_finds_the_real_call_sites():
    """CONTROL. If this comes back empty the sweep never ran and every
    assertion above is vacuously true."""
    census = _census()
    assert len(census) >= 2, census
    assert any(f == "express_dbf_upload" for _rel, f in census), census


def test_a_full_history_call_into_the_main_db_is_caught():
    """The guard's own break-it-once, as a test rather than a manual step."""
    rogue = _sites(
        "def rogue():\n"
        "    commit_express_dbf(d, db_path=config.DATABASE_PATH, since_days=None)\n",
        "inventory_app/rogue.py")
    (rel, func), (window, db) = next(iter(rogue.items()))
    assert window == "None" and "DATABASE_PATH" in db
    assert (rel, func) not in ALLOWED
