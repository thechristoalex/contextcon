"""Service 1: Vendor Onboarding (Zoho Procurement "Vendors" + Vendor Portal sign-up).

Flow, as in Zoho's help (Vendors > Invite Vendor):
  org invites (display name + email)          -> INVITED            (cancel_invite -> CANCELLED)
  vendor opens the portal link, fills Business,
  Address, Bank and Other details, submits     -> APPROVAL_PENDING
  org: Approve                                 -> ACTIVE             (vendor master; payable)
  org: More > Request More Details             -> PENDING_REVIEW     (vendor resubmits -> APPROVAL_PENDING)
  org: Reject (reason)                         -> REJECTED           (org edits + resubmits -> APPROVAL_PENDING)
  org: Mark as Inactive / Active               -> INACTIVE / ACTIVE
Org-created vendors start as DRAFT and are submitted for approval the same way.

Design pattern: State. Every status lives in one transition table (VENDOR_FLOW); no code path
can move a vendor except through it, and every move is a compare-and-set with an audit row.
The checks that run on submission are a list of small Strategy functions (CHECKS) so a new
check is one function, not an edit to the flow.

What we add to Zoho's flow, for the problem statement (5.1 / 6.1):
- the vendor's bank account becomes the ONLY account the payables service will ever pay; a
  bank change made later from the portal sends the vendor back to APPROVAL_PENDING, which
  blocks payments until a person re-approves (bank-detail fraud).
- the same GSTIN or bank account cannot belong to two vendors (front companies, vendor master hygiene).
- the Crustdata vendor check (real, active, run by its director) runs on submission.
"""
import json
import os
import re
import time
import secrets
import threading

from .. import pipeline, store
from .core import BUS, ensure, DomainError, StateMachine, audit, dumps, new_id, now, row, rows, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS proc_vendors (
  vendor_id TEXT PRIMARY KEY,
  invite_token TEXT UNIQUE,
  status TEXT NOT NULL,
  display_name TEXT NOT NULL,
  email TEXT NOT NULL,
  legal_name TEXT, gstin TEXT, website TEXT, director TEXT, currency TEXT DEFAULT 'INR',
  contact_name TEXT, phone TEXT, billing_address TEXT, shipping_address TEXT,
  bank_account TEXT, ifsc TEXT, bank_holder TEXT, bank_verified_on TEXT,
  checks_json TEXT, trust_job_id TEXT, trust_verdict TEXT,
  reason TEXT, invited_by TEXT, approved_by TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, submitted_at REAL, approved_at REAL
);
"""

VENDOR_FLOW = StateMachine("proc_vendors", "vendor_id", {
    "draft":            {"submit": "approval_pending"},
    "invited":          {"submit": "approval_pending", "cancel_invite": "cancelled"},
    "approval_pending": {"approve": "active", "request_more_details": "pending_review", "reject": "rejected"},
    "pending_review":   {"submit": "approval_pending"},
    "rejected":         {"submit": "approval_pending"},
    "active":           {"mark_inactive": "inactive", "bank_change": "approval_pending"},
    "inactive":         {"mark_active": "active"},
    "cancelled":        {},
})

PORTAL_FIELDS = ("legal_name", "gstin", "website", "director", "currency", "contact_name", "phone",
                 "billing_address", "shipping_address", "bank_account", "ifsc", "bank_holder")
REQUIRED = ("legal_name", "gstin", "billing_address", "bank_account", "ifsc", "bank_holder")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def _domain(s):
    s = (s or "").lower().strip()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return s[4:] if s.startswith("www.") else s or None


# ---------------- checks (Strategy: each returns {name, result, reason}) ----------------

def _c(name, result, reason):
    return {"name": name, "result": result, "reason": reason}


def chk_required(v):
    miss = [f for f in REQUIRED if not (v.get(f) or "").strip()]
    return _c("required_fields", "fail" if miss else "pass", f"Missing: {', '.join(miss)}" if miss else "All required details given")


def chk_gstin(v):
    g = (v.get("gstin") or "").upper()
    if not g:
        return _c("gstin", "fail", "No GSTIN")
    if not pipeline.gstin_valid(g):
        return _c("gstin", "fail", f"GSTIN {g} fails the check-character test: not a real GST number")
    legal = (v.get("legal_name") or "").lower()
    if any(w in legal for w in ("limited", "ltd", "pvt", "private")) and g[5] != "C":
        return _c("gstin", "fail", f"GSTIN's PAN is type '{g[5]}', but {v.get('legal_name')} is a company (expected 'C')")
    return _c("gstin", "pass", f"GSTIN valid; PAN {g[2:12]}; state {pipeline.GST_STATES.get(g[:2], '?')}")


def chk_ifsc(v):
    i = (v.get("ifsc") or "").upper()
    return _c("ifsc", "pass" if IFSC_RE.match(i) else "fail", f"IFSC {i} is well formed" if IFSC_RE.match(i) else f"IFSC '{i}' is not a valid IFSC (4 letters, 0, 6 characters)")


def chk_bank_holder(v):
    h, legal = v.get("bank_holder") or "", v.get("legal_name") or ""
    if not h or not legal:
        return _c("bank_holder", "warn", "Account holder or legal name missing")
    s = pipeline.name_similarity(h, legal)
    if s < 0.5:
        return _c("bank_holder", "fail", f"Account holder '{h}' does not match legal name '{legal}' (similarity {s:.2f}): payment could go to a third party")
    return _c("bank_holder", "pass", f"Account holder matches legal name (similarity {s:.2f})")


def chk_email_domain(v):
    web, mail = _domain(v.get("website")), (v.get("email") or "").split("@")[-1].lower()
    if not web:
        return _c("email_domain", "warn", "No website given; sender domain cannot be bound to the vendor")
    if mail in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com"):
        return _c("email_domain", "warn", f"Contact uses a free mailbox ({mail}), not the company domain {web}")
    if mail != web and not mail.endswith("." + web):
        return _c("email_domain", "warn", f"Contact email domain {mail} differs from website {web}")
    return _c("email_domain", "pass", f"Contact email is on the company domain {web}")


def _existing_vendors(exclude):
    from .. import ap
    out = [{"vendor_id": x["vendor_id"], "name": x["legal_name"], "gstin": x.get("gstin"), "bank_account": (x.get("bank") or {}).get("account_no")}
           for x in ap.fixture_vendors()]
    out += [{"vendor_id": r["vendor_id"], "name": r["legal_name"] or r["display_name"], "gstin": r["gstin"], "bank_account": r["bank_account"]}
            for r in rows("SELECT * FROM proc_vendors WHERE vendor_id<>? AND status NOT IN ('rejected','cancelled')", (exclude,))]
    return out


def chk_duplicates(v):
    """Vendor master hygiene: one GSTIN, one bank account, one vendor."""
    for o in _existing_vendors(v["vendor_id"]):
        if v.get("gstin") and o["gstin"] and v["gstin"].upper() == o["gstin"].upper():
            return _c("duplicate_vendor", "fail", f"GSTIN already belongs to {o['name']} ({o['vendor_id']})")
        if v.get("bank_account") and o["bank_account"] and v["bank_account"] == o["bank_account"]:
            return _c("duplicate_vendor", "fail", f"Bank account already belongs to {o['name']} ({o['vendor_id']}): two vendors cannot share an account")
    return _c("duplicate_vendor", "pass", "No other vendor has this GSTIN or bank account")


CHECKS = [chk_required, chk_gstin, chk_ifsc, chk_bank_holder, chk_email_domain, chk_duplicates]


def run_checks(v):
    return [c(v) for c in CHECKS]


def _trust_enabled():
    return os.environ.get("PROCURE_TRUST_CHECK", "1") != "0" and bool(os.environ.get("CRUSTDATA_API_KEY") or _cfg_key())


def _cfg_key():
    try:
        from ..config import CONFIG
        return CONFIG.get("crustdata_api_key")
    except Exception:
        return None


def _run_trust(vendor_id, req):
    try:
        rep = pipeline.run_request(req)
        verdict = (rep or {}).get("verdict") or (rep or {}).get("status")
        with tx() as c:
            c.execute("UPDATE proc_vendors SET trust_job_id=?, trust_verdict=?, updated_at=? WHERE vendor_id=?",
                      ((rep or {}).get("job_id") or req["request_id"], verdict, now(), vendor_id))
        store.log(f"vendor:{vendor_id}", f"vendor check finished: {verdict}")
    except Exception as e:  # a source outage is "cannot verify", never a pass
        with tx() as c:
            c.execute("UPDATE proc_vendors SET trust_verdict='CANNOT_VERIFY', updated_at=? WHERE vendor_id=?", (now(), vendor_id))
        store.log(f"vendor:{vendor_id}", f"vendor check failed: {e!r}", "warn")


# ---------------- service ----------------

class VendorOnboardingService:
    def __init__(self):
        ensure("onboarding", SCHEMA)

    # --- reads
    def get(self, vendor_id):
        v = row("SELECT * FROM proc_vendors WHERE vendor_id=?", (vendor_id,))
        if not v:
            raise DomainError("vendor not found", 404)
        return v

    def view(self, vendor_id):
        v = self.get(vendor_id)
        v["checks"] = json.loads(v.pop("checks_json") or "[]")
        v["allowed_actions"] = sorted(VENDOR_FLOW.t.get(v["status"], {}))
        v["history"] = audit("proc_vendors", vendor_id)
        tok = v.pop("invite_token", None)
        v["portal_link"] = f"/portal#t={tok}" if tok and v["status"] not in ("cancelled",) else None
        return v

    def list(self, status=None):
        q = "SELECT vendor_id, status, display_name, legal_name, email, gstin, website, bank_account, ifsc, trust_verdict, checks_json, updated_at FROM proc_vendors"
        out = rows(q + (" WHERE status=?" if status else "") + " ORDER BY updated_at DESC", (status,) if status else ())
        for o in out:
            cs = json.loads(o.pop("checks_json") or "[]")
            o["failed_checks"] = sum(1 for c in cs if c["result"] == "fail")
            o["checks_run"] = len(cs)
        return out

    def by_token(self, token):
        v = row("SELECT * FROM proc_vendors WHERE invite_token=?", (token or "",))
        if not v:
            raise DomainError("invite link is not valid", 404)
        return v

    def portal_view(self, token):
        v = self.by_token(token)
        out = {k: v[k] for k in ("vendor_id", "status", "display_name", "email", "reason") + PORTAL_FIELDS}
        out["can_edit"] = VENDOR_FLOW.can(v["status"], "submit") or v["status"] == "active"
        return out

    # --- org actions
    def invite(self, display_name, email, by):
        if not display_name or not email or "@" not in email:
            raise DomainError("display name and a valid email are needed")
        if row("SELECT 1 FROM proc_vendors WHERE lower(email)=lower(?) AND status NOT IN ('rejected','cancelled')", (email,)):
            raise DomainError(f"{email} is already invited or onboarded", 409)
        vid, tok, t = "PV-" + new_id()[:8].upper(), secrets.token_urlsafe(18), now()
        with tx() as c:
            c.execute("INSERT INTO proc_vendors(vendor_id, invite_token, status, display_name, email, invited_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (vid, tok, "invited", display_name.strip(), email.strip().lower(), by, t, t))
            c.execute("INSERT INTO proc_audit(entity, entity_id, action, from_status, to_status, detail, at) VALUES ('proc_vendors',?,?,?,?,?,?)",
                      (vid, "invite", None, "invited", dumps({"by": by, "email": email}), t))
        store.log(f"vendor:{vid}", f"invited {display_name} <{email}> by {by}")
        return {**self.view(vid), "portal_link": f"/portal#t={tok}"}

    def create(self, data, by):
        """Org adds a vendor directly (Vendors > + New); it still needs approval before it can be paid."""
        vid, t = "PV-" + new_id()[:8].upper(), now()
        vals = {k: (data.get(k) or "").strip() or None for k in PORTAL_FIELDS}
        with tx() as c:
            c.execute(f"INSERT INTO proc_vendors(vendor_id, status, display_name, email, invited_by, created_at, updated_at, {', '.join(PORTAL_FIELDS)}) VALUES (?,?,?,?,?,?,?{', ?' * len(PORTAL_FIELDS)})",
                      (vid, "draft", data.get("display_name") or vals["legal_name"] or "New vendor", (data.get("email") or "").lower(), by, t, t, *vals.values()))
        return self.view(vid)

    def _act(self, vendor_id, action, by, **cols):
        v = self.get(vendor_id)
        with tx() as c:
            VENDOR_FLOW.move(c, vendor_id, v["status"], action, **cols)
        store.log(f"vendor:{vendor_id}", f"{action} by {by}" + (f": {cols.get('reason')}" if cols.get("reason") else ""))
        return self.view(vendor_id)

    def cancel_invite(self, vendor_id, by):
        return self._act(vendor_id, "cancel_invite", by)

    def submit_draft(self, vendor_id, by):
        v = self.get(vendor_id)
        return self._submit(v, {}, by)

    def approve(self, vendor_id, by):
        v = self.get(vendor_id)
        fails = [c for c in json.loads(v["checks_json"] or "[]") if c["result"] == "fail"]
        if fails:
            raise DomainError("cannot approve: " + "; ".join(c["reason"] for c in fails), 422, checks=fails)
        if not by:
            raise DomainError("approver name is required")
        out = self._act(vendor_id, "approve", by, approved_by=by, approved_at=now(), reason=None,
                        bank_verified_on=time.strftime("%Y-%m-%d"))
        BUS.publish("vendor.approved", {"vendor_id": vendor_id, "legal_name": out["legal_name"], "by": by})
        return out

    def request_more_details(self, vendor_id, reason, by):
        if not reason:
            raise DomainError("say which details are needed")
        return self._act(vendor_id, "request_more_details", by, reason=reason)

    def reject(self, vendor_id, reason, by):
        if not reason:
            raise DomainError("a reason is required to reject")
        return self._act(vendor_id, "reject", by, reason=reason)

    def mark_inactive(self, vendor_id, by):
        out = self._act(vendor_id, "mark_inactive", by)
        BUS.publish("vendor.inactive", {"vendor_id": vendor_id, "by": by})
        return out

    def mark_active(self, vendor_id, by):
        return self._act(vendor_id, "mark_active", by)

    # --- vendor (portal) actions
    def portal_submit(self, token, data):
        v = self.by_token(token)
        if v["status"] == "active":
            return self._bank_change(v, data)
        return self._submit(v, data, f"vendor <{v['email']}>")

    def _submit(self, v, data, by):
        vals = {k: v.get(k) for k in PORTAL_FIELDS}
        vals.update({k: str(data[k]).strip() for k in PORTAL_FIELDS if k in data and data[k] is not None})
        for k in ("gstin", "ifsc"):
            vals[k] = (vals.get(k) or "").upper() or None
        vals["bank_account"] = re.sub(r"\s", "", vals.get("bank_account") or "") or None
        checks = run_checks({**vals, "vendor_id": v["vendor_id"], "email": v["email"]})
        with tx() as c:
            VENDOR_FLOW.move(c, v["vendor_id"], v["status"], "submit", checks_json=dumps(checks), submitted_at=now(), reason=None, **vals)
        store.log(f"vendor:{v['vendor_id']}", f"submitted for approval by {by}; {sum(1 for x in checks if x['result'] == 'fail')} check(s) failed")
        self._start_trust(v["vendor_id"], vals)
        BUS.publish("vendor.submitted", {"vendor_id": v["vendor_id"]})
        return self.view(v["vendor_id"])

    def _bank_change(self, v, data):
        """An active vendor edits its bank details: back to approval, and no payment until a person re-approves."""
        new = {k: data.get(k) for k in ("bank_account", "ifsc", "bank_holder") if data.get(k)}
        if not new or all(str(new[k]).strip().upper() == str(v.get(k) or "").upper() for k in new):
            raise DomainError("no bank detail changed", 400)
        vals = {k: v.get(k) for k in PORTAL_FIELDS}
        vals.update({k: str(x).strip() for k, x in new.items()})
        vals["ifsc"] = (vals.get("ifsc") or "").upper()
        checks = run_checks({**vals, "vendor_id": v["vendor_id"], "email": v["email"]})
        checks.append(_c("bank_change", "warn", f"Bank account changed from {v['bank_account']} ({v['ifsc']}) to {vals['bank_account']} ({vals['ifsc']}). "
                                               f"Confirm by calling the contact on file ({v.get('phone') or v['email']}), never the requester."))
        with tx() as c:
            VENDOR_FLOW.move(c, v["vendor_id"], "active", "bank_change", checks_json=dumps(checks), submitted_at=now(),
                             bank_account=vals["bank_account"], ifsc=vals["ifsc"], bank_holder=vals.get("bank_holder"), bank_verified_on=None)
        store.log(f"vendor:{v['vendor_id']}", "bank details changed from the portal: payments blocked until re-approved", "warn")
        BUS.publish("vendor.bank_changed", {"vendor_id": v["vendor_id"]})
        return self.view(v["vendor_id"])

    def _start_trust(self, vendor_id, vals):
        if not _trust_enabled() or not vals.get("legal_name"):
            return
        req = {"vendor_name": vals["legal_name"], "website": vals.get("website"), "director": vals.get("director"),
               "gstin": vals.get("gstin"), "request_id": f"onb-{vendor_id}-{int(now())}"}
        with tx() as c:
            c.execute("UPDATE proc_vendors SET trust_job_id=?, trust_verdict='RUNNING' WHERE vendor_id=?", (req["request_id"], vendor_id))
        threading.Thread(target=_run_trust, args=(vendor_id, req), daemon=True).start()

    # --- for the other services
    def payable_vendor(self, vendor_id):
        """The vendor record payables may use, or None if this vendor must not be paid now."""
        v = row("SELECT * FROM proc_vendors WHERE vendor_id=?", (vendor_id,))
        return v if v and v["status"] == "active" else None

    def as_master(self):
        return active_master()


def active_master():
    """Active onboarded vendors, in the vendor-master shape the invoice pipeline already reads."""
    ensure("onboarding", SCHEMA)
    return [{"vendor_id": v["vendor_id"], "legal_name": v["legal_name"], "domain": _domain(v["website"]), "director": v["director"],
                 "gstin": v["gstin"], "bank": {"account_no": v["bank_account"], "ifsc": v["ifsc"], "verified_on": v["bank_verified_on"]},
                 "contact": {"name": v["contact_name"], "email": v["email"], "phone": v["phone"]}, "onboarded": True}
            for v in rows("SELECT * FROM proc_vendors WHERE status='active'")]
