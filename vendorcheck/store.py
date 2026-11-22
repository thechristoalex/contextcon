"""Durable state: jobs, step checkpoints, HTTP response cache, cost ledger, actions.

Everything lives in one SQLite file so a killed process can restart and resume:
- a step that finished is never run again (steps table)
- an API call already paid for is answered from cache (http_cache), never re-billed
- an irreversible action runs at most once (actions.idempotency_key is UNIQUE)
"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("VENDORCHECK_DB", os.path.join(os.path.dirname(__file__), "..", "data", "vendorcheck.db"))

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  request_json TEXT NOT NULL,
  status TEXT NOT NULL,              -- running | done | failed
  verdict TEXT,
  report_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  job_id TEXT NOT NULL,
  step TEXT NOT NULL,
  result_json TEXT NOT NULL,
  finished_at REAL NOT NULL,
  PRIMARY KEY (job_id, step)
);
CREATE TABLE IF NOT EXISTS http_cache (
  req_hash TEXT PRIMARY KEY,
  endpoint TEXT NOT NULL,
  body_json TEXT NOT NULL,
  status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  credits REAL NOT NULL,
  retrieved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  endpoint TEXT NOT NULL,
  req_hash TEXT NOT NULL,
  cache_hit INTEGER NOT NULL,
  credits REAL NOT NULL,
  latency_ms INTEGER NOT NULL,
  http_status INTEGER,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  idempotency_key TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- add_to_vendor_master
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,              -- pending_approval | approved | executed | rejected
  decided_by TEXT,
  decided_at REAL,
  executed_at REAL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS vendor_master (
  vendor_key TEXT PRIMARY KEY,       -- crustdata_company_id
  name TEXT NOT NULL,
  job_id TEXT NOT NULL,
  added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
  invoice_key TEXT PRIMARY KEY,      -- vendor_id + normalised invoice number (idempotency key for payment)
  source_file TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  vendor_id TEXT,
  extracted_json TEXT NOT NULL,
  state TEXT NOT NULL,               -- received | extracted | matched | verified | awaiting_approval | approved | paid | held | duplicate | rejected
  decision_json TEXT,
  vendor_hash_at_approval TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files_seen (
  source_file TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  invoice_key TEXT,
  outcome TEXT,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  idempotency_key TEXT PRIMARY KEY,  -- one invoice = at most one payment
  invoice_key TEXT NOT NULL,
  vendor_id TEXT NOT NULL,
  amount_inr REAL NOT NULL,
  account TEXT NOT NULL,
  ifsc TEXT NOT NULL,
  payee TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  paid_at REAL NOT NULL,
  ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  level TEXT NOT NULL,
  msg TEXT NOT NULL,
  at REAL NOT NULL
);
"""


def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


_conn = None


def db():
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


def log(job_id, msg, level="info"):
    with _lock:
        db().execute("INSERT INTO events(job_id, level, msg, at) VALUES (?,?,?,?)", (job_id, level, msg, time.time()))
    print(f"[{time.strftime('%H:%M:%S')}] [{job_id or '-'}] {msg}", flush=True)


# ---------- jobs / steps ----------

def get_job(job_id):
    r = db().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def upsert_job(job_id, request, status, verdict=None, report=None):
    now = time.time()
    with _lock:
        db().execute(
            """INSERT INTO jobs(job_id, request_json, status, verdict, report_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
                 verdict=COALESCE(excluded.verdict, jobs.verdict),
                 report_json=COALESCE(excluded.report_json, jobs.report_json),
                 updated_at=excluded.updated_at""",
            (job_id, json.dumps(request), status, verdict, json.dumps(report) if report is not None else None, now, now),
        )


def list_jobs(limit=50):
    return [dict(r) for r in db().execute("SELECT job_id, request_json, status, verdict, updated_at FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,))]


def get_step(job_id, step):
    r = db().execute("SELECT result_json FROM steps WHERE job_id=? AND step=?", (job_id, step)).fetchone()
    return json.loads(r["result_json"]) if r else None


def save_step(job_id, step, result):
    with _lock:
        db().execute("INSERT OR REPLACE INTO steps(job_id, step, result_json, finished_at) VALUES (?,?,?,?)",
                     (job_id, step, json.dumps(result), time.time()))


# ---------- http cache + ledger ----------

def cache_get(req_hash):
    r = db().execute("SELECT * FROM http_cache WHERE req_hash=?", (req_hash,)).fetchone()
    return dict(r) if r else None


def cache_put(req_hash, endpoint, body, status, response, credits):
    with _lock:
        db().execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?)",
                     (req_hash, endpoint, json.dumps(body), status, json.dumps(response), credits, time.time()))


def ledger_add(job_id, endpoint, req_hash, cache_hit, credits, latency_ms, http_status):
    with _lock:
        db().execute("INSERT INTO ledger(job_id, endpoint, req_hash, cache_hit, credits, latency_ms, http_status, at) VALUES (?,?,?,?,?,?,?,?)",
                     (job_id, endpoint, req_hash, int(cache_hit), credits, latency_ms, http_status, time.time()))


def ledger_for(job_id):
    rows = [dict(r) for r in db().execute("SELECT endpoint, cache_hit, credits, latency_ms, http_status, at FROM ledger WHERE job_id=? ORDER BY id", (job_id,))]
    total = sum(r["credits"] for r in rows)
    return {"calls": rows, "total_credits": round(total, 3), "paid_calls": sum(1 for r in rows if not r["cache_hit"]),
            "cache_hits": sum(1 for r in rows if r["cache_hit"])}


def job_credits(job_id):
    r = db().execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE job_id=?", (job_id,)).fetchone()
    return r["c"]


# ---------- actions (exactly-once) ----------

def propose_action(key, job_id, kind, payload):
    """Insert a pending action. If it already exists (restart), keep the existing row."""
    with _lock:
        db().execute("INSERT OR IGNORE INTO actions(idempotency_key, job_id, kind, payload_json, status) VALUES (?,?,?,?, 'pending_approval')",
                     (key, job_id, kind, json.dumps(payload)))
    return get_action(key)


def get_action(key):
    r = db().execute("SELECT * FROM actions WHERE idempotency_key=?", (key,)).fetchone()
    return dict(r) if r else None


def actions_for(job_id):
    return [dict(r) for r in db().execute("SELECT * FROM actions WHERE job_id=?", (job_id,))]


def pending_actions():
    return [dict(r) for r in db().execute("SELECT * FROM actions WHERE status IN ('pending_approval','approved')")]


def decide_action(key, approve, who, note=None):
    """Person approves or rejects. Only a pending action can be decided."""
    with _lock:
        cur = db().execute("UPDATE actions SET status=?, decided_by=?, decided_at=?, note=? WHERE idempotency_key=? AND status='pending_approval'",
                           ("approved" if approve else "rejected", who, time.time(), note, key))
    return cur.rowcount == 1


def execute_action(key):
    """Run an approved action exactly once. Safe to call repeatedly / after a crash."""
    a = get_action(key)
    if not a or a["status"] not in ("approved", "executed"):
        return a
    if a["status"] == "executed":
        return a
    payload = json.loads(a["payload_json"])
    with _lock:
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if a["kind"] == "add_to_vendor_master":
                conn.execute("INSERT OR IGNORE INTO vendor_master(vendor_key, name, job_id, added_at) VALUES (?,?,?,?)",
                             (str(payload["crustdata_company_id"]), payload["name"], a["job_id"], time.time()))
            conn.execute("UPDATE actions SET status='executed', executed_at=? WHERE idempotency_key=? AND status='approved'", (time.time(), key))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_action(key)


def vendor_master():
    return [dict(r) for r in db().execute("SELECT * FROM vendor_master ORDER BY added_at")]
