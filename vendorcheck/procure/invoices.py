"""Service 2: Procurement invoice intake (Zoho Vendor Portal "Invoices > Uploaded Documents",
Payables "Inbox", and "Bills").

Flow, as in Zoho's help:
  vendor uploads an invoice (image / PDF, <= 5 MB) in the portal  -> document PENDING
  org reviews it: Accept (Convert to Bill) or Reject (reason)      -> ACCEPTED / REJECTED
  the bill:  DRAFT -> PENDING_APPROVAL -> OPEN -> PARTIALLY_PAID -> PAID      (VOID; OVERDUE is shown when an open bill is past due)

Identity, per the problem statement ("each invoice paid exactly once"):
  doc_uuid      uuid4, minted at upload. Names one uploaded FILE. Every upload gets one, even a resend.
  invoice_uuid  uuid5(INVOICE_NS, "<vendor_id>:<normalised invoice no>"). Names the INVOICE. Deterministic,
                so "INV/2026/0451" and "INV-2026-451" from the same vendor get the SAME uuid, on any
                machine, on any day. It becomes the bill's id (bill_uuid) and is UNIQUE in proc_bills,
                so the database itself refuses a second bill for the same invoice.
  content sha256  the same file uploaded twice is the same document (the upload is idempotent).

Design pattern: Chain of Responsibility for intake. Each handler does one job (file guard, vendor
guard, exact-file dedupe, read, identify, duplicate-invoice, PO match, bank, GSTIN) and either
passes the context on, adds a check, or stops the chain. Reading the file is a Strategy chosen
by content type (text / PDF / photo). The bill's lifecycle is a State machine (BILL_FLOW).
"""
import base64
import datetime
import hashlib
import json
import os
import sqlite3
import subprocess
import uuid

from .. import ap, store
from .core import BUS, ensure, INVOICE_NS, DomainError, StateMachine, audit, dumps, new_id, now, row, rows, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS proc_documents (
  doc_uuid TEXT PRIMARY KEY,
  vendor_id TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  file_name TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  status TEXT NOT NULL,                 -- pending | accepted | rejected
  invoice_uuid TEXT, invoice_key TEXT,
  extracted_json TEXT, checks_json TEXT,
  duplicate_of TEXT, bill_uuid TEXT, reason TEXT, decided_by TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE (vendor_id, sha256)
);
CREATE TABLE IF NOT EXISTS proc_bills (
  bill_uuid TEXT PRIMARY KEY,           -- = invoice_uuid
  invoice_key TEXT NOT NULL UNIQUE,     -- vendor_id:normalised invoice no; one invoice, one bill
  vendor_id TEXT NOT NULL,
  bill_no TEXT NOT NULL, po_no TEXT,
  bill_date TEXT, due_date TEXT,
  amount REAL NOT NULL CHECK (amount > 0),
  balance REAL NOT NULL CHECK (balance >= 0 AND balance <= amount),
  status TEXT NOT NULL,
  doc_uuid TEXT, vendor_bank TEXT, note TEXT,
  created_by TEXT, approved_by TEXT, reason TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""

BILL_FLOW = StateMachine("proc_bills", "bill_uuid", {
    "draft":            {"submit": "pending_approval", "void": "void"},
    "pending_approval": {"approve": "open", "reject": "rejected"},
    "rejected":         {"submit": "pending_approval", "void": "void"},
    "open":             {"part_pay": "partially_paid", "pay": "paid", "void": "void"},
    "partially_paid":   {"part_pay": "partially_paid", "pay": "paid"},
    "paid":             {},
    "void":             {"convert_to_draft": "draft"},
})

MAX_BYTES = 5 * 1024 * 1024     # Zoho vendor portal limit
TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
         "text/plain": ".txt", "message/rfc822": ".eml"}


def uploads_dir():
    d = os.environ.get("PROCURE_UPLOADS") or os.path.join(os.path.dirname(__file__), "..", "..", "data", "services", "uploads")
    os.makedirs(d, exist_ok=True)
    return d


def invoice_uuid(vendor_id, invoice_no):
    n = ap.norm_invoice_no(invoice_no)
    if not n:
        return None, None
    key = f"{vendor_id}:{n}"
    return str(uuid.uuid5(INVOICE_NS, key)), key


def sniff(data, name):
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    ext = os.path.splitext(name or "")[1].lower()
    try:
        data.decode("utf-8")
        return "message/rfc822" if ext == ".eml" else "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


# ---------------- reading (Strategy by content type) ----------------

def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def read_pdf(path):
    try:
        return subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""   # no reader here: the vendor's typed fields are used, and the check says so


def read_image(path):
    from .. import scan
    try:
        r = scan.read_image(path)
    except Exception:
        return ""
    return "\n".join(x["text"] for x in scan.rows_from_lines(r["lines"]))


READERS = {"text/plain": read_text, "message/rfc822": read_text, "application/pdf": read_pdf,
           "image/jpeg": read_image, "image/png": read_image, "image/heic": read_image}


# ---------------- intake chain (Chain of Responsibility) ----------------

class Stop(Exception):
    """A handler ends the chain early with a ready answer (e.g. the exact file was already uploaded)."""

    def __init__(self, result):
        self.result = result


def _c(name, result, reason):
    return {"name": name, "result": result, "reason": reason}


class Handler:
    def handle(self, ctx):
        raise NotImplementedError


class FileGuard(Handler):
    def handle(self, ctx):
        if not ctx["data"]:
            raise DomainError("the file is empty")
        if len(ctx["data"]) > MAX_BYTES:
            raise DomainError("files must be 5 MB or smaller", 413)
        ctype = sniff(ctx["data"], ctx["file_name"])
        if ctype not in TYPES:
            raise DomainError("only PDF, image (JPG / PNG / HEIC) or text invoices can be uploaded", 415)
        ctx["content_type"] = ctype


class VendorGuard(Handler):
    def handle(self, ctx):
        v = ap.vendors().get(ctx["vendor_id"])
        if not v:
            raise DomainError("this vendor is not active: invoices are accepted only from approved vendors", 403)
        ctx["vendor"] = v


class ExactFileDedupe(Handler):
    """Same bytes from the same vendor = the same document. Re-uploading returns the first upload."""

    def handle(self, ctx):
        ctx["sha256"] = hashlib.sha256(ctx["data"]).hexdigest()
        old = row("SELECT doc_uuid FROM proc_documents WHERE vendor_id=? AND sha256=?", (ctx["vendor_id"], ctx["sha256"]))
        if old:
            raise Stop({"deduplicated": True, "doc_uuid": old["doc_uuid"],
                        "message": "This exact file was already uploaded; no new document was created."})


class Store(Handler):
    def handle(self, ctx):
        ctx["doc_uuid"] = new_id()
        ctx["path"] = os.path.join(uploads_dir(), ctx["doc_uuid"] + TYPES[ctx["content_type"]])
        with open(ctx["path"], "wb") as f:
            f.write(ctx["data"])


def _same(k, a, b):
    """Typed vs read: compare as amounts / invoice numbers / account numbers, not as raw text."""
    try:
        if k == "amount_inr":
            return abs(float(str(a).replace(",", "")) - float(str(b).replace(",", ""))) < 0.01
    except ValueError:
        return False
    if k == "invoice_no":
        return ap.norm_invoice_no(str(a)) == ap.norm_invoice_no(str(b))
    return str(a).replace(" ", "").upper() == str(b).replace(" ", "").upper()


class Read(Handler):
    def handle(self, ctx):
        text = READERS[ctx["content_type"]](ctx["path"])
        x = ap.extract(text) if text.strip() else {}
        typed = ctx.get("typed") or {}
        src = {}
        for k in ("invoice_no", "amount_inr", "po_no", "invoice_date", "gstin", "account_no", "ifsc"):
            if typed.get(k) not in (None, ""):
                if x.get(k) not in (None, "") and k in ("amount_inr", "invoice_no", "account_no") and not _same(k, x[k], typed[k]):
                    ctx["checks"].append(_c("typed_vs_document", "fail",
                                            f"{k.replace('_', ' ')} typed as '{typed[k]}' but the document says '{x[k]}'"))
                x.setdefault(k, None)
                if x[k] in (None, ""):
                    x[k], src[k] = typed[k], "typed by vendor"
            if x.get(k) not in (None, "") and k not in src:
                src[k] = "read from document"
        if x.get("amount_inr") is not None:
            x["amount_inr"] = float(str(x["amount_inr"]).replace(",", ""))
        x["due_date"] = typed.get("due_date")
        x["_source"] = src
        x["_text_read"] = bool(text.strip())
        ctx["x"] = x
        if not text.strip():
            ctx["checks"].append(_c("readable", "warn", "No text could be read from the file on this machine; fields are as typed by the vendor. Compare with the file."))


class Identify(Handler):
    def handle(self, ctx):
        x = ctx["x"]
        if not x.get("invoice_no"):
            ctx["checks"].append(_c("invoice_identity", "fail", "No invoice number on the document or the form: an invoice without a number cannot be paid exactly once"))
            return
        if not x.get("amount_inr"):
            ctx["checks"].append(_c("amount", "fail", "No invoice amount found"))
        ctx["invoice_uuid"], ctx["invoice_key"] = invoice_uuid(ctx["vendor_id"], x["invoice_no"])
        ctx["checks"].append(_c("invoice_identity", "pass", f"Invoice {x['invoice_no']} -> key {ctx['invoice_key']} -> uuid {ctx['invoice_uuid']}"))


class DuplicateInvoice(Handler):
    """Same invoice under any numbering format (exact uuid), or a near-duplicate (same vendor, amount, close date)."""

    def handle(self, ctx):
        if not ctx.get("invoice_uuid"):
            return
        b = row("SELECT bill_uuid, bill_no, status FROM proc_bills WHERE bill_uuid=?", (ctx["invoice_uuid"],))
        if b and b["status"] != "void":
            ctx["duplicate_of"] = b["bill_uuid"]
            ctx["checks"].append(_c("duplicate_invoice", "fail", f"Same invoice as bill {b['bill_no']} ({b['status'].replace('_', ' ')}): invoice uuid {b['bill_uuid']}"))
            return
        d = row("SELECT doc_uuid FROM proc_documents WHERE invoice_uuid=? AND status='pending' AND doc_uuid<>?", (ctx["invoice_uuid"], ctx["doc_uuid"]))
        if d:
            ctx["duplicate_of"] = d["doc_uuid"]
            ctx["checks"].append(_c("duplicate_invoice", "fail", f"Same invoice is already waiting for review as document {d['doc_uuid']}"))
            return
        amt = ctx["x"].get("amount_inr")
        near = row("SELECT bill_no FROM proc_bills WHERE vendor_id=? AND abs(amount-?)<0.01 AND status<>'void' AND created_at>?",
                   (ctx["vendor_id"], amt or -1, now() - 30 * 86400))
        if near:
            ctx["checks"].append(_c("duplicate_invoice", "warn", f"Different number, but bill {near['bill_no']} from this vendor has the same amount in the last 30 days: possible re-numbered duplicate"))
        else:
            ctx["checks"].append(_c("duplicate_invoice", "pass", "No earlier bill or pending document for this invoice"))


class PurchaseOrderMatch(Handler):
    """Two-way (PO) and three-way (PO + goods receipt) match; a PO can be billed only up to its value."""

    def handle(self, ctx):
        x, vid = ctx["x"], ctx["vendor_id"]
        if not x.get("po_no"):
            ctx["checks"].append(_c("po_match", "warn", "No PO number: a non-PO invoice needs the approver's note"))
            return
        po = {p["po_no"]: p for p in ap.load("pos.json")}.get(x["po_no"])
        if not po:
            ctx["checks"].append(_c("po_match", "fail", f"PO {x['po_no']} not found"))
            return
        if po["vendor_id"] != vid:
            ctx["checks"].append(_c("po_match", "fail", f"PO {po['po_no']} was raised on {po['vendor_id']}, not {vid}"))
            return
        billed = row("SELECT COALESCE(SUM(amount),0) s FROM proc_bills WHERE po_no=? AND status<>'void'", (po["po_no"],))["s"]
        amt = x.get("amount_inr") or 0
        if billed + amt > po["amount_inr"] * 1.01:
            ctx["checks"].append(_c("po_match", "fail", f"PO {po['po_no']} (₹{po['amount_inr']:,.2f}) already billed ₹{billed:,.2f}; this invoice would take it over the PO value"))
            return
        g = {g["po_no"]: g for g in ap.load("grns.json")}.get(po["po_no"])
        if not g:
            ctx["checks"].append(_c("po_match", "warn", f"Matches PO {po['po_no']}, but no goods receipt yet (two-way match only)"))
        elif x.get("qty") and x["qty"] > g["qty_received"]:
            ctx["checks"].append(_c("po_match", "fail", f"Bills {x['qty']} but only {g['qty_received']} received ({g['grn_no']})"))
        else:
            ctx["checks"].append(_c("po_match", "pass", f"Matches PO {po['po_no']} and goods receipt {g['grn_no']}"))


class BankOnInvoice(Handler):
    def handle(self, ctx):
        r = ap.check_bank(ctx["x"], ctx["vendor"])
        ctx["checks"].append(_c("bank_details", r["result"], r["reasoning"]))


class GstinOnInvoice(Handler):
    def handle(self, ctx):
        if not ctx["x"].get("gstin"):
            ctx["checks"].append(_c("gstin", "warn", "No GSTIN on the invoice"))
            return
        r = ap.check_gstin(ctx["x"], ctx["vendor"])
        ctx["checks"].append(_c("gstin", r["result"], r["reasoning"]))


INTAKE_CHAIN = [FileGuard(), VendorGuard(), ExactFileDedupe(), Store(), Read(), Identify(),
                DuplicateInvoice(), PurchaseOrderMatch(), BankOnInvoice(), GstinOnInvoice()]

HARD_FAILS = {"duplicate_invoice", "invoice_identity", "bank_details", "typed_vs_document", "amount"}


def _vendor_bank(v):
    b = v.get("bank") or {}
    return f"{b.get('account_no')}|{b.get('ifsc')}"


def _due(bill_date_s, days=30):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


# ---------------- service ----------------

class InvoiceService:
    def __init__(self):
        ensure("invoices", SCHEMA)

    def upload(self, vendor_id, file_name, data_b64, typed=None, by="vendor"):
        try:
            data = base64.b64decode((data_b64 or "").split(",", 1)[-1], validate=False)
        except Exception:
            raise DomainError("file is not valid base64")
        ctx = {"vendor_id": vendor_id, "file_name": os.path.basename(file_name or "invoice"), "data": data,
               "typed": typed or {}, "checks": []}
        try:
            for h in INTAKE_CHAIN:
                h.handle(ctx)
        except Stop as s:
            return {**s.result, "document": self.document(s.result["doc_uuid"])}
        t = now()
        with tx() as c:
            c.execute("""INSERT INTO proc_documents(doc_uuid, vendor_id, uploaded_by, file_name, content_type, size, sha256, status,
                         invoice_uuid, invoice_key, extracted_json, checks_json, duplicate_of, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?, 'pending', ?,?,?,?,?,?,?)""",
                      (ctx["doc_uuid"], vendor_id, by, ctx["file_name"], ctx["content_type"], len(data), ctx["sha256"],
                       ctx.get("invoice_uuid"), ctx.get("invoice_key"), dumps(ctx["x"]), dumps(ctx["checks"]), ctx.get("duplicate_of"), t, t))
            c.execute("INSERT INTO proc_audit(entity, entity_id, action, from_status, to_status, detail, at) VALUES ('proc_documents',?,?,?,?,?,?)",
                      (ctx["doc_uuid"], "upload", None, "pending", dumps({"by": by, "file": ctx["file_name"], "sha256": ctx["sha256"]}), t))
        store.log(f"doc:{ctx['doc_uuid']}", f"uploaded {ctx['file_name']} by {by} for {vendor_id}; invoice uuid {ctx.get('invoice_uuid')}")
        BUS.publish("invoice.uploaded", {"doc_uuid": ctx["doc_uuid"], "vendor_id": vendor_id, "invoice_uuid": ctx.get("invoice_uuid")})
        return {"deduplicated": False, "doc_uuid": ctx["doc_uuid"], "document": self.document(ctx["doc_uuid"])}

    # --- reads
    def document(self, doc_uuid):
        d = row("SELECT * FROM proc_documents WHERE doc_uuid=?", (doc_uuid,))
        if not d:
            raise DomainError("document not found", 404)
        d["extracted"] = json.loads(d.pop("extracted_json") or "{}")
        d["checks"] = json.loads(d.pop("checks_json") or "[]")
        d["vendor_name"] = (ap.vendors().get(d["vendor_id"]) or {}).get("legal_name")
        d["history"] = audit("proc_documents", doc_uuid)
        return d

    def documents(self, status=None, vendor_id=None):
        q, a = "SELECT doc_uuid FROM proc_documents WHERE 1=1", []
        if status:
            q += " AND status=?"; a.append(status)
        if vendor_id:
            q += " AND vendor_id=?"; a.append(vendor_id)
        return [self.document(r["doc_uuid"]) for r in rows(q + " ORDER BY created_at DESC", a)]

    def file_path(self, doc_uuid):
        d = row("SELECT doc_uuid, content_type FROM proc_documents WHERE doc_uuid=?", (doc_uuid,))
        if not d:
            raise DomainError("document not found", 404)
        return os.path.join(uploads_dir(), d["doc_uuid"] + TYPES[d["content_type"]]), d["content_type"]

    def bill(self, bill_uuid):
        b = row("SELECT * FROM proc_bills WHERE bill_uuid=?", (bill_uuid,))
        if not b:
            raise DomainError("bill not found", 404)
        return self._decorate(b, history=True)

    def _decorate(self, b, history=False):
        b["display_status"] = "overdue" if b["status"] in ("open", "partially_paid") and b["due_date"] and b["due_date"] < datetime.date.today().isoformat() else b["status"]
        b["vendor_name"] = (ap.vendors().get(b["vendor_id"]) or {}).get("legal_name")
        b["allowed_actions"] = sorted(a for a in BILL_FLOW.t.get(b["status"], {}) if a not in ("pay", "part_pay"))
        if history:
            b["history"] = audit("proc_bills", b["bill_uuid"])
            b["payments"] = rows("""SELECT p.payment_id, p.payment_no, p.status, a.amount, p.payment_date, p.bank_ref
                                    FROM proc_payment_allocations a JOIN proc_payments p USING(payment_id) WHERE a.bill_uuid=? ORDER BY p.created_at""", (b["bill_uuid"],)) \
                if row("SELECT 1 FROM sqlite_master WHERE name='proc_payments'") else []
        return b

    def bills(self, status=None, vendor_id=None):
        q, a = "SELECT * FROM proc_bills WHERE 1=1", []
        if vendor_id:
            q += " AND vendor_id=?"; a.append(vendor_id)
        out = [self._decorate(b) for b in rows(q + " ORDER BY created_at DESC", a)]
        return [b for b in out if not status or b["display_status"] == status or b["status"] == status]

    # --- org decisions on an uploaded document
    def accept(self, doc_uuid, by, note=None, due_date=None):
        """Convert to Bill. The bill's id is the invoice uuid; a second bill for the same invoice is refused by the database."""
        d = self.document(doc_uuid)
        if d["status"] != "pending":
            raise DomainError(f"document is already {d['status']}", 409)
        hard = [c for c in d["checks"] if c["result"] == "fail" and (c["name"] in HARD_FAILS)]
        if hard:
            raise DomainError("cannot convert to a bill: " + "; ".join(c["reason"] for c in hard), 422, checks=hard)
        soft = [c for c in d["checks"] if c["result"] in ("fail", "warn")]
        if soft and not (note or "").strip():
            raise DomainError("checks need attention: add a note saying why this bill is still correct", 422, checks=soft)
        x, v = d["extracted"], ap.vendors().get(d["vendor_id"])
        if not v:
            raise DomainError("vendor is no longer active", 409)
        t = now()
        try:
            with tx() as c:
                c.execute("""INSERT INTO proc_bills(bill_uuid, invoice_key, vendor_id, bill_no, po_no, bill_date, due_date, amount, balance, status,
                             doc_uuid, vendor_bank, note, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?, 'pending_approval', ?,?,?,?,?,?)""",
                          (d["invoice_uuid"], d["invoice_key"], d["vendor_id"], x["invoice_no"], x.get("po_no"), x.get("invoice_date"),
                           due_date or x.get("due_date") or _due(x.get("invoice_date")), x["amount_inr"], x["amount_inr"],
                           doc_uuid, _vendor_bank(v), note, by, t, t))
                c.execute("UPDATE proc_documents SET status='accepted', bill_uuid=?, decided_by=?, updated_at=? WHERE doc_uuid=? AND status='pending'",
                          (d["invoice_uuid"], by, t, doc_uuid))
                c.execute("INSERT INTO proc_audit(entity, entity_id, action, from_status, to_status, detail, at) VALUES ('proc_documents',?,?,?,?,?,?)",
                          (doc_uuid, "accept", "pending", "accepted", dumps({"by": by, "bill_uuid": d["invoice_uuid"], "note": note}), t))
                c.execute("INSERT INTO proc_audit(entity, entity_id, action, from_status, to_status, detail, at) VALUES ('proc_bills',?,?,?,?,?,?)",
                          (d["invoice_uuid"], "convert_to_bill", None, "pending_approval", dumps({"by": by, "doc_uuid": doc_uuid}), t))
        except sqlite3.IntegrityError:
            raise DomainError(f"a bill for invoice {x.get('invoice_no')} already exists (uuid {d['invoice_uuid']})", 409)
        store.log(f"bill:{d['invoice_uuid']}", f"document {doc_uuid} converted to bill {x['invoice_no']} by {by}")
        return self.bill(d["invoice_uuid"])

    def reject_document(self, doc_uuid, reason, by):
        if not reason:
            raise DomainError("a reason is required")
        t = now()
        with tx() as c:
            n = c.execute("UPDATE proc_documents SET status='rejected', reason=?, decided_by=?, updated_at=? WHERE doc_uuid=? AND status='pending'",
                          (reason, by, t, doc_uuid)).rowcount
            if n != 1:
                raise DomainError("only a pending document can be rejected", 409)
            c.execute("INSERT INTO proc_audit(entity, entity_id, action, from_status, to_status, detail, at) VALUES ('proc_documents',?,?,?,?,?,?)",
                      (doc_uuid, "reject", "pending", "rejected", dumps({"by": by, "reason": reason}), t))
        return self.document(doc_uuid)

    # --- bill approvals
    def bill_action(self, bill_uuid, action, by, reason=None):
        if action in ("pay", "part_pay"):
            raise DomainError("bills are paid by recording a payment in Payments Made", 400)
        b = self.bill(bill_uuid)
        if action == "reject" and not reason:
            raise DomainError("a reason is required")
        cols = {"reason": reason} if reason else {}
        if action == "approve":
            cols["approved_by"] = by
        with tx() as c:
            to = BILL_FLOW.move(c, bill_uuid, b["status"], action, **cols)
        store.log(f"bill:{bill_uuid}", f"{action} by {by} -> {to}")
        if to == "open":
            BUS.publish("bill.opened", {"bill_uuid": bill_uuid, "vendor_id": b["vendor_id"], "amount": b["amount"]})
        return self.bill(bill_uuid)

    # --- used by the payables service inside ITS transaction
    @staticmethod
    def apply_payment(conn, bill_uuid, amount, payment_id):
        """Reduce the balance only if it still covers the amount (atomic guard: never below zero, never paid twice)."""
        b = conn.execute("SELECT status, balance FROM proc_bills WHERE bill_uuid=?", (bill_uuid,)).fetchone()
        if not b or b["status"] not in ("open", "partially_paid"):
            raise DomainError(f"bill {bill_uuid} is not open for payment", 409)
        cur = conn.execute("UPDATE proc_bills SET balance=round(balance-?,2), updated_at=? WHERE bill_uuid=? AND balance>=? - 0.001",
                           (amount, now(), bill_uuid, amount))
        if cur.rowcount != 1:
            raise DomainError(f"bill {bill_uuid} balance ₹{b['balance']:,.2f} does not cover ₹{amount:,.2f}: refusing to overpay", 409)
        bal = conn.execute("SELECT balance FROM proc_bills WHERE bill_uuid=?", (bill_uuid,)).fetchone()["balance"]
        BILL_FLOW.move(conn, bill_uuid, b["status"], "pay" if bal <= 0.001 else "part_pay", reason=None)
        conn.execute("UPDATE proc_audit SET detail=? WHERE id=last_insert_rowid()", (dumps({"payment_id": payment_id, "amount": amount, "balance": bal}),))
