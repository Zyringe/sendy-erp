"""Who is writing: the one channel that names the actor of a database write (#590).

Design: docs/specs/2026-09-19-590-cost-audit-actor-design.md §A1 (branch
feat/590-cost-audit-actor).

Every connection the app opens registers the SQL function `sendy_actor(field)`
(see `install`). Persistent triggers call it, and Python reads the same answer
by asking the connection, so there is exactly one resolver.

It reads three things, lowest precedence first:
  1. the test default (`set_fallback`, conftest only);
  2. the identity bound to the connection (`database.script_connection`);
  3. the frames on the current context: a request's root frame, pushed by
     app.py, and any `acting_as` scopes.
A ROOT frame names a new identity and hides everything beneath it. A PARTIAL
frame adds to the identity below it: the engine says WHAT ran
(`wacc:<operation>`) while WHO stays the request's or the script's.

No Flask here: app.py owns the request hooks and passes plain values in.
"""
import contextvars
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Actor:
    source: Optional[str] = None  # audit_log.change_source: manual / import / migration / test
    who: Optional[str] = None     # session username, script operator, 'deploy', 'pytest'
    kind: Optional[str] = None    # ui / script / migration / system / test
    detail: Optional[str] = None  # endpoint, script + reason, engine operation; chained ' > '

    @property
    def reason(self):
        if self.kind is None:
            return self.detail
        return f'{self.kind}:{self.detail or ""}'


_FIELDS = ('who', 'source', 'reason')

# Each frame is (is_root, Actor). A tuple, so a reset token restores it exactly.
_frames = contextvars.ContextVar('sendy_actor_frames', default=())
_fallback = None


def _compose(base, partial):
    base = base or Actor()
    if partial.detail and base.detail:
        detail = f'{base.detail} > {partial.detail}'
    else:
        detail = partial.detail or base.detail
    return Actor(source=partial.source or base.source,
                 who=partial.who or base.who,
                 kind=partial.kind or base.kind,
                 detail=detail)


def current(bound=None):
    """The actor a write on a connection bound to `bound` would carry now."""
    frames = _frames.get()
    start = next((i for i in range(len(frames) - 1, -1, -1) if frames[i][0]), None)
    if start is None:
        resolved, rest = bound or _fallback, frames
    else:
        resolved, rest = frames[start][1], frames[start + 1:]
    for _root, frame in rest:
        resolved = _compose(resolved, frame)
    return resolved


@contextmanager
def acting_as(kind=None, who=None, source=None, detail=None):
    """Declare who is acting for the code inside the block. Scopes nest.

    Naming a `kind` declares a new identity (a root) and requires `who`.
    Without one, the scope only adds `detail`/`source` to whoever is already
    acting — which is how the WACC engine records its operation.
    """
    root = kind is not None
    if root and not (who or '').strip():
        raise ValueError('a new actor needs a who: an unnamed identity is what #590 removes')
    token = _frames.set(_frames.get() + ((root, Actor(source, who, kind, detail)),))
    try:
        yield
    finally:
        _frames.reset(token)


def push_request(who, endpoint):
    """Root frame for one HTTP request. Returns the token `pop_request` needs."""
    return _frames.set(((True, Actor(source='manual', who=who, kind='ui', detail=endpoint)),))


def pop_request(token):
    try:
        _frames.reset(token)
    except ValueError:
        # the token belongs to another context; never let a request inherit it
        _frames.set(())


def set_fallback(default):
    """Tests only: the lowest-precedence actor (tests/conftest.py sets it per test)."""
    if 'pytest' not in sys.modules:
        raise RuntimeError('the actor fallback is for tests; production must declare')
    global _fallback
    _fallback = default


def _sql(bound, field):
    # Called from inside SQLite, including from triggers: an exception here
    # would fail the write it was only meant to label, so it answers NULL.
    try:
        if field not in _FIELDS:
            return None
        resolved = current(bound)
        return None if resolved is None else getattr(resolved, field)
    except Exception:
        return None


def install(conn, bound=None):
    """Register `sendy_actor(field)` on `conn`. Declares nobody by itself."""
    conn.create_function('sendy_actor', 1, lambda field: _sql(bound, field))
    return conn
