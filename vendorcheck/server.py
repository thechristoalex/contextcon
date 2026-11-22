"""HTTP API + UI (stdlib only).

API
  GET  /api/invoices              queue
  GET  /api/invoices/<key>        case (decision, checks, evidence, events)
  POST /api/process               process the inbox folder
  POST /api/upload {name, text}   drop a new document into the inbox and process it
  POST /api/approve {key, by}     approve a payment (irreversible step), then pay once
  POST /api/recheck {key}         replay checks on a held invoice
  GET  /api/payments              simulated payment ledger
  GET  /api/events                activity log
  POST /cases {vendor_name, website, director}   vendor check on its own (5.1 + 5.2)
  GET  /cases/<id>
"""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import time
import urllib.request

from . import ap, pipeline, store
from .config import CONFIG

HERE = os.path.dirname(__file__)
UI = os.path.join(HERE, "ui.html")
LANDING = os.path.join(HERE, "landing.html")
_busy = threading.Lock()

# ---- auth: Supabase sessions (Google or email link). The CLI never needs this. ----
SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "1" if SUPA_URL else "0") == "1"
_tok_cache = {}


def verify_token(token):
    """Ask Supabase who this access token belongs to. Cached for 5 minutes."""
    if not token:
        return None
    hit = _tok_cache.get(token)
    if hit and hit[1] > time.time():
        return hit[0]
    req = urllib.request.Request(SUPA_URL + "/auth/v1/user", headers={"apikey": SUPA_KEY, "authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            u = json.loads(r.read())
    except Exception:
        return None
    user = {"id": u.get("id"), "email": u.get("email")}
    _tok_cache[token] = (user, time.time() + 300)
    return user


def record_approval(token, user, key, amount):
    """Write the approval to Supabase (row-level security: a user can only insert rows as themselves)."""
    if not (SUPA_URL and token):
        return
    body = json.dumps({"invoice_key": key, "amount_inr": amount, "approver_email": user.get("email")}).encode()
    req = urllib.request.Request(SUPA_URL + "/rest/v1/approvals", data=body, method="POST", headers={
        "apikey": SUPA_KEY, "authorization": "Bearer " + token, "content-type": "application/json", "prefer": "return=minimal"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        store.log(None, f"approval mirror to Supabase failed: {e}", "warn")


def _inv_row(r):
    d = json.loads(r["decision_json"] or "{}")
    x = json.loads(r["extracted_json"] or "{}")
    return {"invoice_key": r["invoice_key"], "file": os.path.basename(r["source_file"]), "doc_type": r["doc_type"],
            "vendor_id": r["vendor_id"], "state": r["state"], "reason": d.get("reason"), "risk_score": d.get("risk_score"),
            "amount_inr": x.get("amount_inr"), "invoice_no": x.get("invoice_no"), "updated_at": r["updated_at"]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, default=str, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _page(self, path):
        b = open(path, "rb").read()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        return self.wfile.write(b)

    def _user(self):
        """None if auth is required and the request has no valid session."""
        if not AUTH_REQUIRED:
            return {"email": None, "id": None, "local": True}
        h = self.headers.get("authorization") or ""
        self._token = h[7:] if h.lower().startswith("bearer ") else None
        return verify_token(self._token)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._page(LANDING)
        if p in ("/app", "/app/"):
            return self._page(UI)
        if p == "/config.js":
            js = f"window.SUPA={json.dumps({'url': SUPA_URL, 'key': SUPA_KEY, 'required': AUTH_REQUIRED}) if SUPA_URL else 'null'};".encode()
            self.send_response(200)
            self.send_header("content-type", "text/javascript")
            self.end_headers()
            return self.wfile.write(js)
        if (p.startswith("/api/") or p.startswith("/cases")) and not self._user():
            return self._json({"error": "sign in required"}, 401)
        if p == "/api/me":
            return self._json(self._user())
        if p == "/api/settings":
            return self._json({k: CONFIG[k] for k in ("max_credits_per_job", "max_seconds_per_job", "auto_approve_below_inr", "min_age_years", "min_headcount", "max_people_screened", "inr_per_credit", "jev_enabled", "jev_model", "jev_yes", "jev_no", "jev_choice_min", "jev_score_min") if k in CONFIG})
        if p.startswith("/static/"):
            name = os.path.basename(p)
            fp = os.path.join(os.path.dirname(__file__), "static", name)
            if not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".woff": "font/woff", ".otf": "font/otf", ".ttf": "font/ttf", ".svg": "image/svg+xml"}.get(os.path.splitext(name)[1], "application/octet-stream")
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if p == "/api/invoices":
            return self._json([_inv_row(r) for r in ap.invoices()])
        if p.startswith("/api/invoices/"):
            key = urllib.parse.unquote(p[len("/api/invoices/"):])
            r = store.db().execute("SELECT * FROM invoices WHERE invoice_key=?", (key,)).fetchone()
            if not r:
                return self._json({"error": "not found"}, 404)
            v = ap.vendors().get(r["vendor_id"]) if r["vendor_id"] else None
            pay = store.db().execute("SELECT * FROM payments WHERE idempotency_key=?", (key,)).fetchone()
            ev = [dict(e) for e in store.db().execute(
                "SELECT * FROM events WHERE job_id IN (?,?,?) ORDER BY id", (f"inv:{os.path.basename(r['source_file'])}", f"inv:{key}", f"trust:{r['vendor_id']}"))]
            return self._json({"invoice": dict(r), "decision": json.loads(r["decision_json"] or "{}"), "extracted": json.loads(r["extracted_json"]),
                               "vendor": v, "payment": dict(pay) if pay else None, "events": ev})
        if p == "/api/bank":
            from . import bank
            store.db().execute(bank.SCHEMA)
            return self._json([dict(r) for r in store.db().execute("SELECT * FROM bank_postings ORDER BY at DESC, line_no")])
        if p == "/api/payments":
            return self._json(ap.payments())
        if p == "/api/events":
            return self._json([dict(e) for e in store.db().execute("SELECT * FROM events ORDER BY id DESC LIMIT 300")])
        if p == "/api/vendors":
            out = []
            for v in ap.vendors().values():
                j = store.get_job(f"trust:{v['vendor_id']}")
                out.append({**v, "trust": j["verdict"] if j else None})
            return self._json(out)
        if p.startswith("/cases/"):
            j = store.get_job(p[len("/cases/"):])
            return self._json({**j, "report": json.loads(j["report_json"] or "null")} if j else {"error": "not found"}, 200 if j else 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        user = self._user()
        if not user:
            return self._json({"error": "sign in required"}, 401)
        try:
            if p == "/api/settings":
                for k in ("max_credits_per_job", "max_seconds_per_job", "auto_approve_below_inr", "min_age_years", "min_headcount"):
                    if k in b:
                        CONFIG[k] = float(b[k])
                for k in ("jev_yes", "jev_no", "jev_choice_min", "jev_score_min"):
                    if k in b:
                        CONFIG[k] = min(1.0, max(0.0, float(b[k])))
                if "jev_enabled" in b:
                    CONFIG["jev_enabled"] = str(b["jev_enabled"]).strip().lower() in ("1", "true", "yes", "on")
                store.log(None, f"settings changed by {user.get('email') or 'local'}: {b}")
                return self._json({"ok": True})
            if p == "/api/process":
                with _busy:
                    return self._json(ap.process_inbox())
            if p == "/api/upload":
                name = os.path.basename(b.get("name") or "upload.txt")
                path = os.path.join(ap.FIX, "inbox", name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(b["text"])
                with _busy:
                    return self._json(ap.process_file(path))
            if p == "/api/approve":
                # the approver is the signed-in identity, not a typed name
                who = user.get("email") or b.get("by") or "local approver"
                r = ap.approve(b["key"], who)
                if r.get("ok"):
                    inv = store.db().execute("SELECT extracted_json FROM invoices WHERE invoice_key=?", (b["key"],)).fetchone()
                    record_approval(getattr(self, "_token", None), user, b["key"], json.loads(inv["extracted_json"]).get("amount_inr"))
                return self._json(r)
            if p == "/api/bank":
                from . import bank
                name = os.path.basename(b.get("name") or f"statement_{int(time.time())}.csv")
                path = os.path.join(ap.FIX, "statements", name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(b["csv"])
                with _busy:
                    return self._json(bank.post_file(path))
            if p == "/api/recheck":
                with _busy:
                    return self._json(ap.recheck(b["key"]))
            if p == "/cases":
                with _busy:
                    return self._json(pipeline.run_request(b))
        except Exception as e:
            return self._json({"error": repr(e)}, 500)
        self._json({"error": "not found"}, 404)


def serve(port=8080):
    store.db()
    done = ap.resume()  # crash recovery: pay anything approved but unpaid, exactly once
    if done:
        print(f"resumed {len(done)} approved payment(s)")
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Vendor Trust + Payables Desk on http://127.0.0.1:{port}")
    srv.serve_forever()
