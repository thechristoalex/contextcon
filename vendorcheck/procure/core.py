"""Shared building blocks for the three procurement services.

- DomainError    one error type that carries its HTTP status, so services never import the server
- tx()           one SQLite write transaction (BEGIN IMMEDIATE) under the store's process lock
- StateMachine   State pattern as a transition table: every status change is checked and logged
- EventBus       Observer pattern: services publish domain events, other services subscribe.
                 In-process today; the same event names map 1:1 to Kafka topics if the services split.
"""
import contextlib
import json
import time
import uuid

from .. import store

# Namespace for deterministic invoice UUIDs (uuid5). Fixed forever: changing it would give
# every existing invoice a new identity and break de-duplication.
INVOICE_NS = uuid.UUID("6f1c2b9e-4d0a-5b7e-9c3f-0a1b2c3d4e5f")


class DomainError(Exception):
    def __init__(self, msg, code=400, **extra):
        super().__init__(msg)
        self.code = code
        self.extra = extra


@contextlib.contextmanager
def tx():
    """One atomic write. Everything inside commits together or not at all."""
    with store._lock:
        conn = store.db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def now():
    return time.time()


def new_id():
    return str(uuid.uuid4())


def rows(sql, args=()):
    with store._lock:   # one shared connection: never let two threads use it at once
        return [dict(r) for r in store.db().execute(sql, args)]


def row(sql, args=()):
    with store._lock:
        r = store.db().execute(sql, args).fetchone()
    return dict(r) if r else None


def dumps(o):
    return json.dumps(o, ensure_ascii=False, default=str)


class StateMachine:
    """Allowed transitions for one entity type. `move` changes status only if the row is still in
    the expected state (compare-and-set), so two racing requests cannot both win."""

    def __init__(self, table, id_col, transitions):
        self.table, self.id_col, self.t = table, id_col, transitions

    def can(self, frm, action):
        return action in self.t.get(frm, {})

    def target(self, frm, action):
        if not self.can(frm, action):
            allowed = ", ".join(sorted(self.t.get(frm, {}))) or "none"
            raise DomainError(f"cannot {action.replace('_', ' ')} while {frm.replace('_', ' ')} (allowed: {allowed})", 409)
        return self.t[frm][action]

    def move(self, conn, ent_id, frm, action, **cols):
        to = self.target(frm, action)
        sets = ", ".join(["status=?", "updated_at=?"] + [f"{k}=?" for k in cols])
        cur = conn.execute(f"UPDATE {self.table} SET {sets} WHERE {self.id_col}=? AND status=?",
                           (to, now(), *cols.values(), ent_id, frm))
        if cur.rowcount != 1:
            raise DomainError("changed by someone else; reload and try again", 409)
        conn.execute("INSERT INTO proc_audit(entity, entity_id, action, from_status, to_status, detail, at) VALUES (?,?,?,?,?,?,?)",
                     (self.table, ent_id, action, frm, to, dumps(cols), now()))
        return to


class EventBus:
    """Observer pattern. Handlers run after the publishing transaction commits."""

    def __init__(self):
        self.subs = {}

    def subscribe(self, name, fn):
        self.subs.setdefault(name, []).append(fn)

    def publish(self, name, payload):
        store.log(f"evt:{name}", f"{name} {dumps(payload)[:300]}")
        for fn in self.subs.get(name, []):
            fn(payload)


BUS = EventBus()

SCHEMA = """
CREATE TABLE IF NOT EXISTS proc_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL,
  from_status TEXT, to_status TEXT, detail TEXT, at REAL NOT NULL
);
"""


def ensure(name, sql):
    """Create a service's tables if this database lacks them. Cheap check first, because
    executescript would commit any transaction that happens to be open."""
    import re
    first = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", sql).group(1)
    with store._lock:
        if not store.db().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (first,)).fetchone():
            store.db().executescript(SCHEMA + sql)


def audit(entity, ent_id):
    return rows("SELECT action, from_status, to_status, detail, at FROM proc_audit WHERE entity=? AND entity_id=? ORDER BY id", (entity, ent_id))
