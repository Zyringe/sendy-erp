"""Two migrations must never quietly share a number.

Why this exists
---------------
On 2026-08-11 two branches were merged independently within two minutes of each
other and both had claimed **155**:

    155_nonstock_billable_codes.sql     UPDATE product_code_mapping, DELETE unit_conversions
    155_sso_ceiling_2569.sql            UPDATE hr_config

Nothing broke, and that is the point — the collision is invisible. The runner is
keyed on FILENAME, so both applied cleanly and both are stamped on prod and
local. It surfaced only because someone happened to `ls` the directory.

What it costs when it does bite:

  * `data/schema.sql` and a fresh rebuild replay migrations in FILENAME order,
    which is not necessarily the order they really ran in. Prod applied
    `155_sso_ceiling_2569` first (12:47:58) and `155_nonstock_billable_codes`
    second (12:49:05); a rebuild would do the reverse. Harmless for these two
    because they touch disjoint tables, but that is luck, not design — two
    colliding migrations that touch the SAME table would rebuild differently
    from production and nobody would be told.
  * "roll back 155" stops being an unambiguous instruction.

Why the obvious fix is the dangerous one
----------------------------------------
Do NOT renumber an already-applied migration. `database.py::init_db()` decides
what to run by filename and does not re-check the file's hash (see
`sendy_erp/CLAUDE.md`), so renaming `155_sso_ceiling_2569.sql` to `158_...`
makes every environment — prod included — see a brand-new migration and RUN IT
AGAIN. Re-running its `UPDATE hr_config` would re-apply the SSO ceiling on top
of itself. The tidy-looking fix is the only move here that can cause damage;
the messy filenames cannot.

So the existing collision is allowlisted below, and this test exists to stop the
NEXT one happening silently.

Picking the next number: derive it from `origin/main`, never from
`ls data/migrations` — the shared checkout can be sitting on an unmerged branch
whose migrations are not on main yet. That is exactly how 155 was claimed twice.
"""
import collections
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(REPO, 'data', 'migrations')

# Numbers used by more than one forward migration. Each entry must say why it is
# not being fixed — an entry is a decision on the record; a silent duplicate is
# an oversight. Adding one is fine. Leaving a new collision unlisted is not.
KNOWN_DUPLICATE_NUMBERS = {
    '155': 'Claimed twice on 2026-08-11 by two branches merged minutes apart '
           '(nonstock_billable_codes + sso_ceiling_2569). Both are stamped on '
           'prod and local, and they touch disjoint tables (product_code_mapping'
           '/unit_conversions vs hr_config), so the collision is inert. NOT '
           'renamed on purpose: the runner is filename-keyed and does not check '
           'hashes, so renaming an applied migration re-runs it everywhere.',
}


def _forward_migrations():
    """Every NNN_*.sql that is not a .rollback.sql."""
    return sorted(f for f in os.listdir(MIGRATIONS)
                  if f.endswith('.sql') and not f.endswith('.rollback.sql'))


def _by_number():
    grouped = collections.defaultdict(list)
    for name in _forward_migrations():
        m = re.match(r'^(\d+)_', name)
        assert m, f'migration filename must start with its number: {name}'
        grouped[m.group(1)].append(name)
    return grouped


def test_no_unlisted_duplicate_migration_numbers():
    grouped = _by_number()

    # Control first: prove the sweep actually read the directory, so a green
    # result cannot mean "found nothing to check".
    assert len(_forward_migrations()) > 100, 'migration sweep found almost nothing'

    duplicates = {n: files for n, files in grouped.items() if len(files) > 1}
    unlisted = {n: files for n, files in duplicates.items()
                if n not in KNOWN_DUPLICATE_NUMBERS}

    assert not unlisted, (
        'two migrations share a number:\n'
        + '\n'.join(f'  {n}: {", ".join(files)}' for n, files in sorted(unlisted.items()))
        + '\n\nPick the next free number from origin/main (not from `ls`), and do '
          'NOT renumber one that has already been applied anywhere — the runner '
          'is filename-keyed, so a rename re-runs it on every environment. See '
          'this module\'s docstring.')


def test_the_duplicate_allowlist_has_not_gone_stale():
    """An allowlist that outlives the thing it excuses becomes a lie people
    trust. If a listed number is no longer duplicated, the entry must go."""
    duplicated = {n for n, files in _by_number().items() if len(files) > 1}

    stale = sorted(set(KNOWN_DUPLICATE_NUMBERS) - duplicated)

    assert not stale, (
        f'these numbers are no longer duplicated: {stale} — '
        'remove them from KNOWN_DUPLICATE_NUMBERS')


def test_every_allowlist_entry_gives_a_reason():
    """The reason is what makes an entry a decision rather than a shrug."""
    assert KNOWN_DUPLICATE_NUMBERS, 'control: the allowlist is not empty'
    for number, reason in KNOWN_DUPLICATE_NUMBERS.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 40, (
            f'allowlist entry {number} needs a real reason, got: {reason!r}')
