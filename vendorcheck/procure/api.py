"""HTTP routes for the three services. server.py hands every /api/procure/* and /api/portal/* request here.

Org side (the app)                                    Vendor side (the portal, authorised by the invite token)
  GET  /api/procure/vendors[?status=]                   GET  /api/portal/me?t=<token>
  GET  /api/procure/vendors/<id>                        POST /api/portal/profile        {t, ...details}   submit / resubmit / bank change
  POST /api/procure/vendors/invite {display_name,email} GET  /api/portal/documents?t=
  POST /api/procure/vendors       {...}  (+ New)        POST /api/portal/documents      {t, name, data, invoice_no, amount_inr, po_no, ...}
  POST /api/procure/vendors/<id>/<action> {reason}      GET  /api/portal/invoices?t=    bills + payments received
       action: submit approve request_more_details reject mark_inactive mark_active cancel_invite

  GET  /api/procure/documents[?status=]   GET /api/procure/documents/<uuid>   GET /api/procure/documents/<uuid>/file
  POST /api/procure/documents {vendor_id, name, data, ...}     (Payables > Inbox > Upload)
  POST /api/procure/documents/<uuid>/accept {note, due_date}   (Convert to Bill)
  POST /api/procure/documents/<uuid>/reject {reason}

  GET  /api/procure/bills[?status=]   GET /api/procure/bills/<uuid>
  POST /api/procure/bills/<uuid>/<action> {reason}   action: submit approve reject void convert_to_draft

  GET  /api/procure/payments[?status=]   GET /api/procure/payments/<id>   GET /api/procure/unpaid-bills?vendor_id=
  POST /api/procure/payments                 Idempotency-Key required  {vendor_id, allocations:[{bill_uuid, amount}], mode, paid_through, payment_date, submit}
  POST /api/procure/payments/<id>/approve    Idempotency-Key required  {note}
  POST /api/procure/payments/<id>/<action>   action: submit reject void convert_to_draft
  POST /api/procure/payments/resume          finish payments interrupted by a crash
"""
import os
import urllib.parse

from .core import DomainError
from .invoices import InvoiceService
from .onboarding import VendorOnboardingService
from .payables import PayablesService

def services():
    """The services are stateless (state lives in SQLite); constructing them makes sure their tables exist."""
    return VendorOnboardingService(), InvoiceService(), PayablesService()


class FileResponse:
    def __init__(self, path, ctype):
        self.path, self.ctype = path, ctype


def route(method, raw_path, body, headers, actor):
    """Returns (status, obj) or None if the path is not ours. Never raises DomainError."""
    u = urllib.parse.urlparse(raw_path)
    path, q = u.path.rstrip("/"), {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    if not (path.startswith("/api/procure") or path.startswith("/api/portal")):
        return None
    try:
        return _route(method, path, q, body or {}, headers, actor)
    except DomainError as e:
        return e.code, {"error": str(e), **e.extra}
    except KeyError as e:
        return 400, {"error": f"missing field {e}"}


def _route(method, path, q, b, headers, actor):
    V, I, P = services()
    by = b.get("by") or actor
    seg = [urllib.parse.unquote(s) for s in path.split("/")[3:]]   # after /api/procure or /api/portal
    portal = path.startswith("/api/portal")
    idem = headers.get("Idempotency-Key") if headers else None

    if portal:
        tok = q.get("t") or b.get("t")
        v = V.by_token(tok)
        if method == "GET" and seg == ["me"]:
            return 200, V.portal_view(tok)
        if method == "POST" and seg == ["profile"]:
            V.portal_submit(tok, b)
            return 200, V.portal_view(tok)
        if seg == ["documents"]:
            if v["status"] != "active":
                raise DomainError("your organisation details must be approved before you can upload invoices", 403)
            if method == "GET":
                return 200, I.documents(vendor_id=v["vendor_id"])
            typed = {k: b.get(k) for k in ("invoice_no", "amount_inr", "po_no", "invoice_date", "due_date")}
            r = I.upload(v["vendor_id"], b.get("name"), b.get("data"), typed, by=f"vendor <{v['email']}>")
            return (200 if r["deduplicated"] else 201), r
        if method == "GET" and seg == ["invoices"]:
            bills = I.bills(vendor_id=v["vendor_id"])
            pays = [p for p in P.list(vendor_id=v["vendor_id"]) if p["status"] == "paid"]
            return 200, {"invoices": bills, "payments_received": pays}
        raise DomainError("not found", 404)

    head = seg[0] if seg else ""
    if head == "vendors":
        if method == "GET":
            return (200, V.view(seg[1])) if len(seg) > 1 else (200, V.list(q.get("status")))
        if seg == ["vendors", "invite"]:
            return 201, V.invite(b.get("display_name"), b.get("email"), by)
        if seg == ["vendors"]:
            return 201, V.create(b, by)
        if len(seg) == 3:
            vid, act = seg[1], seg[2]
            fn = {"submit": lambda: V.submit_draft(vid, by), "approve": lambda: V.approve(vid, by),
                  "request_more_details": lambda: V.request_more_details(vid, b.get("reason"), by),
                  "reject": lambda: V.reject(vid, b.get("reason"), by), "mark_inactive": lambda: V.mark_inactive(vid, by),
                  "mark_active": lambda: V.mark_active(vid, by), "cancel_invite": lambda: V.cancel_invite(vid, by)}.get(act)
            if fn:
                return 200, fn()
    if head == "documents":
        if method == "GET":
            if len(seg) == 3 and seg[2] == "file":
                return 200, FileResponse(*I.file_path(seg[1]))
            return (200, I.document(seg[1])) if len(seg) > 1 else (200, I.documents(q.get("status"), q.get("vendor_id")))
        if seg == ["documents"]:
            typed = {k: b.get(k) for k in ("invoice_no", "amount_inr", "po_no", "invoice_date", "due_date")}
            r = I.upload(b["vendor_id"], b.get("name"), b.get("data"), typed, by=by)
            return (200 if r["deduplicated"] else 201), r
        if len(seg) == 3 and seg[2] == "accept":
            return 200, I.accept(seg[1], by, b.get("note"), b.get("due_date"))
        if len(seg) == 3 and seg[2] == "reject":
            return 200, I.reject_document(seg[1], b.get("reason"), by)
    if head == "bills":
        if method == "GET":
            return (200, I.bill(seg[1])) if len(seg) > 1 else (200, I.bills(q.get("status"), q.get("vendor_id")))
        if len(seg) == 3:
            return 200, I.bill_action(seg[1], seg[2], by, b.get("reason"))
    if head == "unpaid-bills":
        return 200, P.unpaid_bills(q.get("vendor_id"))
    if head == "payments":
        if method == "GET":
            return (200, P.view(seg[1])) if len(seg) > 1 else (200, P.list(q.get("status"), q.get("vendor_id")))
        if seg == ["payments"]:
            return P.create(b, idem_key=idem, by=by)
        if seg == ["payments", "resume"]:
            return 200, P.resume()
        if len(seg) == 3:
            pid, act = seg[1], seg[2]
            if act == "approve":
                return P.approve(b, pid, idem_key=idem, by=by)
            fn = {"submit": lambda: P.submit(pid, by), "reject": lambda: P.reject(pid, b.get("reason"), by),
                  "void": lambda: P.void(pid, b.get("reason"), by), "convert_to_draft": lambda: P.convert_to_draft(pid, by)}.get(act)
            if fn:
                return 200, fn()
    if head == "summary" and method == "GET":
        return 200, {"vendors": {s: len(V.list(s)) for s in ("invited", "approval_pending", "pending_review", "active", "rejected")},
                     "documents_pending": len(I.documents("pending")),
                     "bills": {s: len(I.bills(s)) for s in ("pending_approval", "open", "overdue", "partially_paid", "paid")},
                     "payments_pending": len(P.list("pending_approval"))}
    raise DomainError("not found", 404)
