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

from . import ap, pipeline, store

UI = os.path.join(os.path.dirname(__file__), "ui.html")
_busy = threading.Lock()


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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            b = open(UI, "rb").read()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
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
        if p == "/api/payments":
            return self._json(ap.payments())
        if p == "/api/events":
            return self._json([dict(e) for e in store.db().execute("SELECT * FROM events ORDER BY id DESC LIMIT 300")])
        if p == "/api/vendors":
            return self._json(list(ap.vendors().values()))
        if p.startswith("/cases/"):
            j = store.get_job(p[len("/cases/"):])
            return self._json({**j, "report": json.loads(j["report_json"] or "null")} if j else {"error": "not found"}, 200 if j else 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        try:
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
                return self._json(ap.approve(b["key"], b.get("by") or "approver"))
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
