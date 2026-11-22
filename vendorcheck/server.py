"""HTTP API + UI (stdlib only).

API
  GET  /api/invoices              queue
  GET  /api/invoices/<key>        case (decision, checks, evidence, events)
  POST /api/process               process the inbox folder
  POST /api/upload {name, text}   drop a new document into the inbox and process it
  POST /api/approve {key, by}     approve a payment (irreversible step), then pay once
  POST /api/recheck {key}         replay checks on a held invoice
  POST /api/scan {image}          photo of a paper bill (data URL or base64): read on this machine, then checked
  GET  /api/scan/<photo>          the stored photo, for the case view
  GET  /api/payments              simulated payment ledger
  GET  /api/events                activity log
  POST /cases {vendor_name, website, director}   vendor check on its own (5.1 + 5.2)
  GET  /cases/<id>
  /api/procure/*, /api/portal/*   vendor onboarding, invoice upload -> bills, payments made (see procure/api.py)
  GET  /portal                     the vendor portal (sign-up + invoice upload)
"""
import base64
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
PORTAL = os.path.join(HERE, "portal.html")
_busy = threading.Lock()

def _latest_trust(vendor_id):
    """Most recent finished vendor check for this vendor, whatever version of its record it ran on."""
    r = store.db().execute("SELECT * FROM jobs WHERE (job_id=? OR job_id LIKE ?) AND status='done' ORDER BY updated_at DESC LIMIT 1",
                           (f"trust:{vendor_id}", f"trust:{vendor_id}:%")).fetchone()
    return dict(r) if r else None


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
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        return self.wfile.write(b)

    def _procure(self, method, body=None):
        from .procure import api as procure_api
        r = procure_api.route(method, self.path, body, self.headers, CONFIG.get("approver_name", "AP lead"))
        if r is None:
            return False
        code, obj = r
        if isinstance(obj, procure_api.FileResponse):
            b = open(obj.path, "rb").read()
            self.send_response(200)
            self.send_header("content-type", obj.ctype)
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self._json(obj, code)
        return True

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p.startswith(("/api/procure", "/api/portal")):
            try:
                if self._procure("GET"):
                    return
            except Exception as e:
                return self._json({"error": repr(e)}, 500)
        if p in ("/portal", "/portal/"):
            return self._page(PORTAL)
        if p in ("/", "/index.html"):
            return self._page(LANDING)
        if p in ("/app", "/app/"):
            return self._page(UI)
        if p == "/api/me":
            return self._json({"name": CONFIG.get("approver_name", "AP lead")})
        if p == "/api/settings":
            return self._json({k: CONFIG[k] for k in ("max_credits_per_job", "max_seconds_per_job", "auto_approve_below_inr", "min_age_years", "min_headcount", "max_people_screened", "inr_per_credit", "jev_enabled", "jev_model", "jev_yes", "jev_no", "jev_choice_min", "jev_score_min", "min_read_confidence", "approver_name") if k in CONFIG})
        if p.startswith("/static/"):
            name = os.path.basename(p)
            fp = os.path.join(os.path.dirname(__file__), "static", name)
            if not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".woff": "font/woff", ".otf": "font/otf", ".ttf": "font/ttf", ".svg": "image/svg+xml"}.get(os.path.splitext(name)[1], "application/octet-stream")
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("cache-control", "no-cache")
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
                "SELECT * FROM events WHERE job_id IN (?,?,?) ORDER BY id", (f"inv:{os.path.basename(r['source_file'])}", f"inv:{key}", ap.trust_job_id(v) if v else ""))]
            return self._json({"invoice": dict(r), "decision": json.loads(r["decision_json"] or "{}"), "extracted": json.loads(r["extracted_json"]),
                               "vendor": v, "payment": dict(pay) if pay else None, "events": ev})
        if p.startswith("/api/scan/"):
            from . import scan
            fp = scan.photo_path(urllib.parse.unquote(p[len("/api/scan/"):]))
            if not fp or not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("content-type", {".png": "image/png", ".heic": "image/heic"}.get(os.path.splitext(fp)[1], "image/jpeg"))
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if p == "/api/bank":
            from . import bank
            store.db().execute(bank.SCHEMA)
            return self._json([dict(r) for r in store.db().execute("SELECT * FROM bank_lines ORDER BY at DESC, line_no")])
        if p == "/api/payments":
            return self._json(ap.payments())
        if p == "/api/events":
            return self._json([dict(e) for e in store.db().execute("SELECT * FROM events ORDER BY id DESC LIMIT 300")])
        if p == "/api/vendors":
            out = []
            for v in ap.vendors().values():
                j = store.get_job(ap.trust_job_id(v)) or _latest_trust(v["vendor_id"])
                out.append({**v, "trust": j["verdict"] if j else None, "trust_job": j["job_id"] if j else None})
            return self._json(out)
        if p.startswith("/cases/"):
            jid = urllib.parse.unquote(p[len("/cases/"):])
            j = store.get_job(jid) or (_latest_trust(jid.split(":")[1]) if jid.startswith("trust:") and jid.count(":") == 1 else None)
            return self._json({**j, "report": json.loads(j["report_json"] or "null")} if j else {"error": "not found"}, 200 if j else 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        try:
            if p.startswith(("/api/procure", "/api/portal")) and self._procure("POST", b):
                return
            if p == "/api/settings":
                for k in ("max_credits_per_job", "max_seconds_per_job", "auto_approve_below_inr", "min_age_years", "min_headcount", "min_read_confidence"):
                    if k in b:
                        CONFIG[k] = float(b[k])
                for k in ("jev_yes", "jev_no", "jev_choice_min", "jev_score_min"):
                    if k in b:
                        CONFIG[k] = min(1.0, max(0.0, float(b[k])))
                if "jev_enabled" in b:
                    CONFIG["jev_enabled"] = str(b["jev_enabled"]).strip().lower() in ("1", "true", "yes", "on")
                if b.get("approver_name"):
                    CONFIG["approver_name"] = str(b["approver_name"])[:80]
                store.log(None, f"settings changed by {CONFIG.get('approver_name', 'AP lead')}: {b}")
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
                    return self._json(ap.process_file(path, recheck_held=True))
            if p == "/api/approve":
                who = b.get("by") or CONFIG.get("approver_name", "AP lead")
                return self._json(ap.approve(b["key"], who))
            if p == "/api/bank":
                from . import bank
                name = os.path.basename(b.get("name") or f"statement_{int(time.time())}.csv")
                path = os.path.join(ap.FIX, "statements", name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(b["csv"])
                with _busy:
                    return self._json(bank.post_file(path))
            if p == "/api/scan":
                from . import scan
                try:
                    data = base64.b64decode((b.get("image") or "").split(",", 1)[-1])
                    with _busy:
                        return self._json(scan.scan_bytes(data))
                except (scan.ScanError, ValueError) as e:
                    return self._json({"error": str(e)}, 400)
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
    from .procure import api as procure_api
    again = procure_api.services()[2].resume()  # payments made: finish any approved payment interrupted by a crash
    if again:
        print(f"resumed {len(again)} payment(s) made")
    from . import scan
    threading.Thread(target=scan.warm, daemon=True).start()  # load the bill reader before the first scan
    # the default listen backlog is 5; a screen that loads 20 documents at once must not have connections refused
    srv = type("Server", (ThreadingHTTPServer,), {"request_queue_size": 128, "daemon_threads": True})(("127.0.0.1", port), H)
    print(f"Vendor Trust + Payables Desk on http://127.0.0.1:{port}")
    srv.serve_forever()
