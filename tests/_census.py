"""One census walker for the "every X goes through the shared helper" sweeps.

Five coverage tests used to carry their own copy: test_revenue_filter_coverage,
test_purchase_total_coverage, test_627_sales_returns_coverage,
test_591_purchase_returns_coverage, test_cogs_basis_coverage (plus the new
test_document_kind_census). They all ask the same question -- which functions
in inventory_app/ (and scripts/) build SQL of a given shape -- and differ only in
the pattern, the table and their own exemption list with reasons. Those stay in
each test; the walking lives here so a fix to the walker is one edit, not five.

Deliberately NOT here: patterns, exemption lists, expected counts.
"""
import ast
import io
import os
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'inventory_app')
SCRIPTS = os.path.join(ROOT, 'scripts')

_SKIP_DIRS = ('__pycache__', 'instance', 'static')


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def py_files(include_scripts=False):
    """(relative path, absolute path) for every .py under inventory_app/, and
    under scripts/ (rel prefixed 'scripts/') when asked."""
    bases = [(APP, '')] + ([(SCRIPTS, 'scripts/')] if include_scripts else [])
    for base, prefix in bases:
        for root, _dirs, names in os.walk(base):
            if any(part in root for part in _SKIP_DIRS):
                continue
            for n in names:
                if n.endswith('.py'):
                    path = os.path.join(root, n)
                    yield prefix + os.path.relpath(path, base).replace(os.sep, '/'), path


def code_only(src):
    """`src` with comments and docstrings removed.

    A guard token found in prose is not a guard. Writing the words
    "excludes_revenue = 1" into a docstring -- which is exactly what the
    2026-08-31 fix to models/payments.py did, while explaining that it uses a
    DIFFERENT filter -- silently marked the whole file as participating.
    """
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.line.lstrip().startswith(('"""', "'''")):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src          # unparseable: fall back to the raw text, never skip
    return '\n'.join(out)


def render(node):
    """Source text of a string expression, f-string holes kept as {expr}. A
    `+` chain renders its non-string operands as holes too, so a query built
    as `"... SUM(" + sales_filters.base_qty_sql() + ") ..."` is read whole.
    (The 591 and purchase-total walkers used to return None for a chain with a
    non-string operand and so skipped it; the 627 walker rendered it. This is
    the 627 behaviour, the superset.)"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant)
                       else '{' + ast.unparse(v.value) + '}' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = render(node.left), render(node.right)
        if left is None and right is None:
            return None
        return ((left if left is not None else '{' + ast.unparse(node.left) + '}')
                + (right if right is not None else '{' + ast.unparse(node.right) + '}'))
    return None


def queries(src):
    """(function qualname, text) for every string VALUE in `src`. A docstring
    or any other bare string statement is prose, never a query."""
    out = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child.name])
            elif isinstance(child, ast.Expr) and render(child.value) is not None:
                continue
            elif render(child) is not None:
                out.append(('.'.join(scope) or '<module>', render(child)))
            else:
                visit(child, scope)

    visit(ast.parse(src), [])
    return out


def per_function(src, pattern, table, keep=None):
    """{function: hits of `pattern`} over the queries of `src` that mention
    `table`; `keep(func, sql)` narrows further."""
    counts = {}
    for func, sql in queries(src):
        if table not in sql or (keep is not None and not keep(func, sql)):
            continue
        n = len(pattern.findall(sql))
        if n:
            counts[func] = counts.get(func, 0) + n
    return counts


def app_counts(pattern, table, keep=None, include_scripts=False):
    """{'file::function': hits} across the whole tree."""
    out = {}
    for rel, path in py_files(include_scripts):
        for func, n in per_function(read(path), pattern, table, keep).items():
            out[f'{rel}::{func}'] = n
    return out
