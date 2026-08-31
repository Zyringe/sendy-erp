"""Cross-cutting sweep: every consumer of payroll_items.net_pay (which can no
longer go negative) or the new carried_in / carried_out columns.

Why this exists (`.claude/rules/erp-engineering-discipline.md`, "Cross-cutting
change: enumerate EVERY site before patching the first one"): the plan's own
first-draft grep was scoped to `inventory_app/` only, which CANNOT see the
external accounting toolchain in the separate brain repo
(`~/Sendai-Boonsawat/Operations/11_accounting/_tools/`) — a real consumer of
`payroll_items` that a narrower sweep would have missed entirely (scrutinize
2026-08-31, plan.md "Review history").

This is written FIRST in P1a, before `hr.py` is touched, per the plan's
"Cross-cutting sweep (do this FIRST in P1a, before patching anything)".

Two independent sweeps, each re-derived LIVE at test time (never hardcoded —
a stale allowlist hides a future miss the same way an absent one does):

1. `APP_DECISIONS` — every `inventory_app/**/*.py|*.html` file that mentions
   `net_pay`, `carried_in` or `carried_out` (word-boundary, so `net_payout`
   — a completely unrelated marketplace-settlement term — does not false-hit).
   Each site is tagged with which phase of Release 1 patches it (P1a/b/c) or
   which later release (P2), per plan.md's own cross-cutting-sweep table.
   P1a patches nothing outside `hr.py` itself — the rest are real, deliberate
   decisions to defer, not misses.

2. `BRAIN_DECISIONS` — every file in the accounting toolchain / the HR agent's
   own doc that mentions `payroll_items` or `net_pay` (the plan's own second
   grep command, broader than sweep 1's word-boundary tokens on purpose,
   since it is also checking for anyone reading the TABLE, not just the money
   column). Every one of these is EXEMPT with a written reason — none of them
   are patched by this ERP repo; `gen_payroll_report.py` computes a STATUTORY
   net where advances (and, by the same reasoning, carry) are excluded by
   design, so adding carry there would misstate wages reported to สรรพากร.

Every entry must be independently traced by whoever last touched this file.
Do not un-exempt or re-scope an entry without re-reading the site.
"""
import os
import re

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(REPO, "inventory_app")
BRAIN = os.path.expanduser("~/Sendai-Boonsawat")
BRAIN_DIRS = (
    os.path.join(BRAIN, "Operations", "11_accounting", "_tools"),
    os.path.join(BRAIN, ".claude", "skills", "accounting-pack"),
    os.path.join(BRAIN, ".claude", "agents"),
)

# word-boundary so `net_payout` (marketplace settlement — a different concept
# entirely) never false-matches `net_pay`.
_APP_TOKEN_RE = re.compile(r"\bnet_pay\b|\bcarried_in\b|\bcarried_out\b")
_BRAIN_TOKEN_RE = re.compile(r"payroll_items|net_pay")  # plan's own 2nd grep

_SKIP_DIRS = ("__pycache__", "instance", "static", ".pytest_cache", ".git")


# ── sweep 1: inside the ERP app ─────────────────────────────────────────────
# Decision = which phase of the plan patches this site. "P1a (THIS phase)" is
# the only one this dispatch actually implements; the rest are real deferrals
# to later phases/releases of the SAME plan, recorded so nobody "completes"
# them under the wrong assumption that P1a should have.
APP_DECISIONS = {
    "hr.py":
        "P1a — the engine itself: _recompute_totals subtracts carried_in "
        "and clamps net_pay at 0 (never negative); _build_item derives "
        "carried_in from the most recent FINALIZED prior run's carried_out; "
        "generate_run's INSERT and update_payroll_item's UPDATE both persist "
        "carried_out so a regenerate/admin-edit doesn't drop it. "
        "P1b — finalize_run(confirm_carry=...) + CarryForwardWarning + "
        "pending_carry_forward()/carry_forward_note(). "
        "P1d — _build_item's CarryChronologyError (the chronological-"
        "finalize invariant); reopen_run(confirm_carry_break=...) + "
        "CarryConsumedWarning + carry_consumed_by()/carry_consumed_note(); "
        "departing_employee_outstanding()/departing_employee_note().",
    "hr_queries.py":
        "P1c — get_payroll_runs SUMs pi.net_pay for the run-list total "
        "(feeds payroll.html + dashboard.html); get_employee_payslips feeds "
        "/me/payslip. Put's ruling (plan.md decision #3): the run total "
        "keeps meaning cash-to-transfer (net_pay unchanged), a carry total "
        "is shown BESIDE it, not folded into the same figure.",
    "blueprints/hr.py":
        "P1c — payroll_export (CSV) writes 16 headers off gross/net_pay with "
        "no carry columns today; Codex review flagged an exported ฿0 net as "
        "unexplainable without them. Add carried_in + carried_out columns.",
    "blueprints/cashbook.py":
        "P2 (Release 2) — advance_history returns net_pay today (the advance-"
        "entry form's informational read); P2 extends this same JSON "
        "endpoint with the collectable-ceiling fields. Not touched in P1a — "
        "the ceiling arithmetic is a separate release.",
    "templates/hr/payroll_detail.html":
        "P1c — display carried_in + carried_out on the run-detail table. "
        "The `net_pay <= 0` branch (renders ไม่มียอดโอน) stays CORRECT as-is: "
        "net is now clamped to exactly 0 rather than going negative, so the "
        "branch still fires exactly when nothing was transferred — a "
        "decision recorded in plan.md, not an oversight.",
    "templates/hr/payslip.html":
        "P1c — Put's decision #1 (plan.md): the employee payslip shows BOTH "
        "the carry-in deduction row (ยกยอดมาจากเดือนก่อน) and the carry-out "
        "remainder footer line (ยกไปหักรอบหน้า).",
    "templates/me/payslip_list.html":
        "P1c — employee-facing payslip list; same display decision as the "
        "admin payslip template above.",
    "templates/cashbook/new.html":
        "P2 (Release 2) — reads d.net_pay from the advance_history JSON the "
        "client renders for the entry form; changes only when P2 extends "
        "that JSON's shape with the ceiling fields.",
}

# ── sweep 2: the accounting toolchain OUTSIDE this repo ─────────────────────
# Every entry here is EXEMPT — nothing in the separate brain repo is patched
# by an ERP PR. Recorded so a later reader doesn't "complete" a sweep that was
# already traced clean.
BRAIN_DECISIONS = {
    "Operations/11_accounting/_tools/gen_payroll_report.py":
        "EXEMPT — traced 2026-08-31: selects only pi.salary_rate, "
        "pi.sso_employee, pi.wht_amount and never reads net_pay. "
        "payroll_logic.py's compute_net() is a STATUTORY net whose own "
        "docstring says 'Advances excluded by design'. Carry is a "
        "cash-timing matter between employer and employee, not a wage "
        "figure — this report reaches สรรพากร via the external accountant, "
        "so adding carry to it would misstate wages.",
    "Operations/11_accounting/_tools/payroll_logic.py":
        "EXEMPT — same reasoning as gen_payroll_report.py: this IS the "
        "statutory net_pay computation, and advances (by the same logic, "
        "carry) are excluded by design, not by omission.",
    "Operations/11_accounting/_tools/check_reports.py":
        "EXEMPT — cross-check tool; reads payroll_items.wht_amount only "
        "(the single source of truth per migration 157), same statutory-net "
        "reasoning as the two files above.",
    "Operations/11_accounting/_tools/test_payroll_logic.py":
        "EXEMPT — comments only, referencing wht_amount as the single "
        "source of truth; no computation over net_pay or carry.",
    "Operations/11_accounting/_tools/company_profile.json":
        "EXEMPT — a config `_note` field mentioning payroll_items.wht_amount "
        "in prose to steer a future editor; not read as data by any script.",
    ".claude/skills/accounting-pack/SKILL.md":
        "EXEMPT — prose describing the payroll report workflow; no "
        "computation. Re-read after Release 1 ships to confirm nothing in "
        "it now reads false (e.g. references to net_pay never going "
        "negative).",
    ".claude/agents/yor.md":
        "EXEMPT — prose table listing HR tables (including payroll_items) "
        "for agent routing; no computation.",
}


def _sweep(root, pattern, skip_dirs=_SKIP_DIRS, exts=None):
    out = {}
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if exts is not None and not f.endswith(exts):
                continue
            path = os.path.join(r, f)
            try:
                src = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            if pattern.search(src):
                out[os.path.relpath(path, root).replace(os.sep, "/")] = True
    return out


def _app_hits():
    return _sweep(APP, _APP_TOKEN_RE, exts=(".py", ".html"))


def _brain_hits():
    out = {}
    for d in BRAIN_DIRS:
        if os.path.isdir(d):
            out.update(_sweep(d, _BRAIN_TOKEN_RE))
    # re-key relative to BRAIN, not to whichever BRAIN_DIRS entry matched
    rekeyed = {}
    for d in BRAIN_DIRS:
        if not os.path.isdir(d):
            continue
        for rel in _sweep(d, _BRAIN_TOKEN_RE):
            abspath = os.path.join(d, rel)
            rekeyed[os.path.relpath(abspath, BRAIN).replace(os.sep, "/")] = True
    return rekeyed


_BRAIN_PRESENT = os.path.isdir(
    os.path.join(BRAIN, "Operations", "11_accounting", "_tools"))
_skip_no_brain = pytest.mark.skipif(
    not _BRAIN_PRESENT, reason="brain repo (~/Sendai-Boonsawat) not present in this environment")


# ── app-side: every hit has a decision, every decision matches a real hit ──

def test_no_unknown_app_site():
    unexpected = set(_app_hits()) - set(APP_DECISIONS)
    assert not unexpected, (
        "New net_pay/carried_in/carried_out surface(s) with no recorded "
        "decision — patch it now (if it's P1a) or add a APP_DECISIONS entry "
        "saying which phase owns it:\n  " + "\n  ".join(sorted(unexpected)))


def test_no_stale_app_decision():
    stale = sorted(set(APP_DECISIONS) - set(_app_hits()))
    assert not stale, (
        "These app-side decisions no longer match any real site (file "
        "deleted, or the term was removed) — a stale entry hides a future "
        "miss exactly like an absent one:\n  " + "\n  ".join(stale))


def test_app_sweep_positive_control():
    """The sweep would be worthless if it could pass while missing the one
    file this whole feature is about."""
    assert "hr.py" in _app_hits()


@pytest.mark.parametrize("rel", sorted(APP_DECISIONS))
def test_every_app_decision_is_substantive(rel):
    assert len(APP_DECISIONS[rel]) > 40, f"{rel}: explain the decision, in a real sentence"


# ── brain-repo (accounting toolchain): same shape, EXEMPT-only ─────────────

@_skip_no_brain
def test_no_unknown_brain_site():
    unexpected = set(_brain_hits()) - set(BRAIN_DECISIONS)
    assert not unexpected, (
        "New payroll_items/net_pay surface(s) in the accounting toolchain "
        "with no recorded decision:\n  " + "\n  ".join(sorted(unexpected)))


@_skip_no_brain
def test_no_stale_brain_decision():
    stale = sorted(set(BRAIN_DECISIONS) - set(_brain_hits()))
    assert not stale, (
        "These accounting-toolchain decisions no longer match any real "
        "site:\n  " + "\n  ".join(stale))


@_skip_no_brain
def test_brain_sweep_positive_control():
    assert ("Operations/11_accounting/_tools/gen_payroll_report.py"
            in _brain_hits())


@pytest.mark.parametrize("rel", sorted(BRAIN_DECISIONS))
def test_every_brain_exemption_has_a_reason(rel):
    reason = BRAIN_DECISIONS[rel]
    assert reason.startswith("EXEMPT"), (
        f"{rel}: every brain-repo entry must be EXEMPT — nothing there is "
        f"patched by an ERP PR")
    assert len(reason) > 60, f"{rel}: explain WHY, in a real sentence"
