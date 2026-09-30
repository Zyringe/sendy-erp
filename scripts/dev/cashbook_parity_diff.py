"""Card F differential: the cashbook seam scenario on BASE vs this tree (plan §3f).

    python scripts/dev/cashbook_parity_diff.py <base-sha> [--keep]

Adds a detached worktree at <base-sha>, copies THIS tree's
`tests/cashbook_seam_scenario.py` into it, and runs
`cashbook_seam_scenario.main(<fresh db>)` from `inventory_app/` in both trees
(`SKIP_DB_INIT=1`, `PYTHONPATH=inventory_app:tests`). Each tree builds its own
DB from its own `data/schema.sql`. Refuses to compare when the two dump headers
disagree on the migration level. Prints the unified diff of the two dumps and
exits 0 only when it is empty.

Nothing here touches either tree's instance/ DB: `main` points DATA_DIR at a
throwaway directory before `import app`.
"""
import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCENARIO = os.path.join('tests', 'cashbook_seam_scenario.py')


def _run_tree(tree, out_dir, label):
    db = os.path.join(out_dir, f'{label}.db')
    env = dict(os.environ, SKIP_DB_INIT='1', PYTHONPATH='.' + os.pathsep + os.path.join('..', 'tests'))
    proc = subprocess.run(
        [sys.executable, '-c', f'import cashbook_seam_scenario as s; s.main({db!r})'],
        cwd=os.path.join(tree, 'inventory_app'), env=env, capture_output=True, text=True)
    with open(os.path.join(out_dir, f'{label}.err'), 'w', encoding='utf-8') as f:
        f.write(proc.stderr)
    if proc.returncode != 0:
        sys.exit(f'{label}: scenario failed (exit {proc.returncode}), see {out_dir}/{label}.err\n'
                 + proc.stderr[-2000:])
    with open(os.path.join(out_dir, f'{label}.json'), 'w', encoding='utf-8') as f:
        f.write(proc.stdout)
    return json.loads(proc.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('base')
    ap.add_argument('--keep', action='store_true', help='keep the base worktree and outputs')
    args = ap.parse_args()

    out_dir = tempfile.mkdtemp(prefix='cashbook-parity-')
    base_tree = os.path.join(out_dir, 'base-tree')
    subprocess.run(['git', '-C', REPO, 'worktree', 'add', '--detach', base_tree, args.base],
                   check=True, capture_output=True)
    try:
        base_sha = subprocess.run(['git', '-C', base_tree, 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True).stdout.strip()
        head_sha = subprocess.run(['git', '-C', REPO, 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True).stdout.strip()
        shutil.copy(os.path.join(REPO, SCENARIO), os.path.join(base_tree, SCENARIO))
        base = _run_tree(base_tree, out_dir, 'base')
        head = _run_tree(REPO, out_dir, 'head')
        print(f'base {base_sha}  header {json.dumps(base["header"], sort_keys=True)}')
        dirty = subprocess.run(['git', '-C', REPO, 'status', '--porcelain', '--untracked-files=no'],
                               check=True, capture_output=True, text=True).stdout.strip()
        print(f'head {head_sha}{" +UNCOMMITTED EDITS" if dirty else ""}  header {json.dumps(head["header"], sort_keys=True)}')
        print(f'kind_counts base {base["kind_counts"]}  head {head["kind_counts"]}')
        if (base['header']['migrations_max'], base['header']['migrations_count']) != \
                (head['header']['migrations_max'], head['header']['migrations_count']):
            sys.exit('REFUSED: the two trees sit at different migration levels')
        a = json.dumps(base, ensure_ascii=False, indent=1, sort_keys=True).splitlines()
        b = json.dumps(head, ensure_ascii=False, indent=1, sort_keys=True).splitlines()
        diff = list(difflib.unified_diff(a, b, 'base', 'head', lineterm=''))
        print(f'dump lines: base {len(a)}, head {len(b)}; diff lines: {len(diff)}')
        for line in diff:
            print(line)
        print('RESULT: IDENTICAL' if not diff else 'RESULT: DIFFERENT')
        return 0 if not diff else 1
    finally:
        if args.keep:
            print(f'kept: {out_dir}')
        else:
            subprocess.run(['git', '-C', REPO, 'worktree', 'remove', '--force', base_tree],
                           capture_output=True)
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
