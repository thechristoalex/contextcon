"""Service 3: Payables (Zoho "Payments Made").

Flow, as in Zoho's help:
  + New payment (vendor, bills, amount applied per bill, mode, paid through)   -> DRAFT
  Submit for Approval                                                          -> PENDING_APPROVAL
  Approve -> money moves                                                       -> PROCESSING -> PAID
  Reject (reason) -> REJECTED -> Edit + Resubmit;  Void (draft / pending / rejected)

Problem statement: "pay each invoice exactly once, through crashes and bank-detail fraud".
HighRadius's controls against duplicate payments are matching (2-way / 3-way), vendor master
hygiene, approval workflows and automatic flagging of duplicates. We implement those as rules
and make the money movement itself idempotent at three layers:

 1. Request layer (Decorator `idempotent`): POST /payments and POST /payments/<id>/approve need an
    Idempotency-Key header. The key, a hash of the request body and the stored response are kept in
    proc_idempotency. Same key + same body -> the first response is replayed, nothing runs again
    (double-click, browser retry, network timeout). Same key + different body -> 422. Same key while
    the first request is still running -> 409.
 2. Domain layer (rules + database guards): an amount applied to a bill can never exceed the bill's
    unreserved balance (other payments in flight count against it); the balance is reduced with a
    compare-and-set UPDATE that cannot go below zero (CHECK constraint as the last line); a bill is
    keyed by its invoice uuid, so the same invoice can never become two bills.
 3. Execution layer (Command `PayCommand`): the payment's own uuid is the idempotency key sent to the
    bank. The intent (PROCESSING) is committed BEFORE the bank call; the bank keeps one transfer per
    key. A crash anywhere is finished by `resume()`, which repeats the command: the bank returns the
    original transfer, nothing is debited twice.

Bank-detail fraud: the vendor's verified account is snapshotted at approval and compared again just
before the bank call. Any change since (or the vendor no longer active) sends the payment back to
PENDING_APPROVAL with the reason.
"""
import functools
import hashlib
import json
import os
import time

from .. import store
from .core import BUS, ensure, DomainError, StateMachine, audit, dumps, new_id, now, row, rows, tx
from .invoices import InvoiceService
from .onboarding import VendorOnboardingService

SCHEMA = """
CREATE TABLE IF NOT EXISTS proc_payments (
  payment_id TEXT PRIMARY KEY,          -- uuid4; also the idempotency key sent to the bank
  payment_no TEXT NOT NULL UNIQUE,
  vendor_id TEXT NOT NULL,
  amount REAL NOT NULL CHECK (amount > 0),
  mode TEXT, paid_through TEXT, payment_date TEXT, reference TEXT,
  status TEXT NOT NULL,
  created_by TEXT, approved_by TEXT, reason TEXT, approver_note TEXT,
  pay_to_account TEXT, pay_to_ifsc TEXT, pay_to_name TEXT,    -- snapshot at approval
  bank_ref TEXT, checks_json TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, paid_at REAL
);
CREATE TABLE IF NOT EXISTS proc_payment_allocations (
  payment_id TEXT NOT NULL,
  bill_uuid TEXT NOT NULL,
  amount REAL NOT NULL CHECK (amount > 0),
  PRIMARY KEY (payment_id, bill_uuid)
);
CREATE TABLE IF NOT EXISTS proc_idempotency (
  idem_key TEXT NOT NULL,
  scope TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL,                 -- in_progress | completed
  response_code INTEGER, response_json TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY (idem_key, scope)
);
CREATE TABLE IF NOT EXISTS sim_bank_transfers (            -- stands in for the bank / payout API
  idem_key TEXT PRIMARY KEY,            -- the bank's own dedupe: one transfer per key
  bank_ref TEXT NOT NULL,
  account TEXT NOT NULL, ifsc TEXT NOT NULL, beneficiary TEXT NOT NULL,
  amount REAL NOT NULL,
  at REAL NOT NULL
);
"""

PAYMENT_FLOW = StateMachine("proc_payments", "payment_id", {
    "draft":            {"submit": "pending_approval", "void": "void", "edit": "draft"},
    "pending_approval": {"approve": "processing", "reject": "rejected", "void": "void", "hold": "pending_approval"},
    "rejected":         {"submit": "pending_approval", "void": "void", "edit": "rejected"},
    "processing":       {"settle": "paid", "hold": "pending_approval"},
    "paid":             {},
    "void":             {"convert_to_draft": "draft"},
})
IN_FLIGHT = ("draft", "pending_approval", "rejected", "processing")


# ---------------- layer 1: idempotent requests (Decorator) ----------------

def _fingerprint(scope, body):
    return hashlib.sha256((scope + "\n" + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


def idempotent(scope):
    """Wrap a service call `fn(self, body, ...)` that returns (code, obj). Needs `idem_key=`."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, body, *a, idem_key=None, **kw):
            if not idem_key or len(idem_key) > 200:
                raise DomainError("Idempotency-Key header is required for this request (any unique value, e.g. a uuid, reused on retry)", 400)
            sc = scope.format(*a)
            h = _fingerprint(sc, body)
            with tx() as c:
                ins = c.execute("INSERT OR IGNORE INTO proc_idempotency(idem_key, scope, request_hash, status, created_at) VALUES (?,?,?, 'in_progress', ?)",
                                (idem_key, sc, h, now())).rowcount
                old = None if ins else dict(c.execute("SELECT * FROM proc_idempotency WHERE idem_key=? AND scope=?", (idem_key, sc)).fetchone())
            if old:
                if old["request_hash"] != h:
                    raise DomainError("this Idempotency-Key was already used with a different request; use a new key for a new payment", 422)
                if old["status"] == "in_progress":
                    raise DomainError("the first request with this Idempotency-Key is still running; retry shortly", 409)
                store.log(f"idem:{idem_key}", f"replayed stored response for {sc}")
                return old["response_code"], {**json.loads(old["response_json"]), "idempotent_replay": True}
            try:
                code, out = fn(self, body, *a, **kw)
            except DomainError as e:
                code, out = e.code, {"error": str(e), **e.extra}
            except Exception:
                with tx() as c:       # unexpected failure: free the key so the client can retry
                    c.execute("DELETE FROM proc_idempotency WHERE idem_key=? AND scope=?", (idem_key, sc))
                raise
            with tx() as c:           # business answers (incl. 4xx) are final for this key
                c.execute("UPDATE proc_idempotency SET status='completed', response_code=?, response_json=? WHERE idem_key=? AND scope=?",
                          (code, dumps(out), idem_key, sc))
            return code, out
        return wrapper
    return deco


# ---------------- layer 2: duplicate-payment rules (Strategy list) ----------------

def _reserved(bill_uuid, exclude_payment=None):
    """Money already promised to this bill by other payments still in flight."""
    return row(f"""SELECT COALESCE(SUM(a.amount),0) s FROM proc_payment_allocations a JOIN proc_payments p USING(payment_id)
                   WHERE a.bill_uuid=? AND p.status IN ({','.join('?' * len(IN_FLIGHT))}) AND p.payment_id<>?""",
               (bill_uuid, *IN_FLIGHT, exclude_payment or ""))["s"]


def rule_bill_open(p, allocs, bills):
    out = []
    for a in allocs:
        b = bills.get(a["bill_uuid"])
        if not b:
            out.append(("fail", "bill_open", f"Bill {a['bill_uuid']} does not exist"))
        elif b["vendor_id"] != p["vendor_id"]:
            out.append(("fail", "bill_open", f"Bill {b['bill_no']} belongs to {b['vendor_id']}, not {p['vendor_id']}"))
        elif b["status"] not in ("open", "partially_paid"):
            out.append(("fail", "bill_open", f"Bill {b['bill_no']} is {b['status'].replace('_', ' ')}; only approved, unpaid bills can be paid"))
    return out


def rule_not_more_than_owed(p, allocs, bills):
    out = []
    for a in allocs:
        b = bills.get(a["bill_uuid"])
        if not b:
            continue
        other = _reserved(a["bill_uuid"], p.get("payment_id"))
        if a["amount"] > b["balance"] - other + 0.001:
            out.append(("fail", "duplicate_payment",
                        f"Bill {b['bill_no']}: balance ₹{b['balance']:,.2f}, ₹{other:,.2f} already in other payments; ₹{a['amount']:,.2f} would pay it twice"))
    return out


def rule_same_vendor_amount(p, allocs, bills):
    """HighRadius-style flag: same vendor, same amount, recent -> likely a duplicate under another number."""
    hit = row(f"""SELECT payment_no, status FROM proc_payments WHERE vendor_id=? AND abs(amount-?)<0.01 AND payment_id<>?
                  AND status IN ('paid', {','.join('?' * len(IN_FLIGHT))}) AND created_at>?""",
              (p["vendor_id"], p["amount"], p.get("payment_id") or "", *IN_FLIGHT, now() - 30 * 86400))
    return [("warn", "same_vendor_amount", f"{hit['payment_no']} ({hit['status'].replace('_', ' ')}) pays this vendor the same amount within 30 days")] if hit else []


def rule_vendor_payable(p, allocs, bills):
    v = VENDORS.payable_vendor_any(p["vendor_id"])
    if not v:
        return [("fail", "vendor_payable", f"Vendor {p['vendor_id']} is not active (pending re-approval, inactive or unknown): no payment")]
    if not v["bank"]["account_no"] or not v["bank"].get("verified_on"):
        return [("fail", "vendor_payable", "Vendor has no verified bank account")]
    changed = [b["bill_no"] for b in bills.values() if b.get("vendor_bank") and b["vendor_bank"] != f"{v['bank']['account_no']}|{v['bank']['ifsc']}"]
    if changed:
        return [("warn", "bank_changed_since_bill", f"Vendor's bank account changed after bill(s) {', '.join(changed)} were accepted; confirm by call-back before approving")]
    return []


RULES = [rule_vendor_payable, rule_bill_open, rule_not_more_than_owed, rule_same_vendor_amount]


def evaluate(p, allocs):
    bills = {a["bill_uuid"]: row("SELECT * FROM proc_bills WHERE bill_uuid=?", (a["bill_uuid"],)) for a in allocs}
    bills = {k: v for k, v in bills.items() if v}
    return [{"result": r, "name": n, "reason": m} for rule in RULES for (r, n, m) in rule(p, allocs, bills)]


# ---------------- layer 3: exactly-once execution (Command) ----------------

class SimBank:
    """A payout API that, like real ones, dedupes on the idempotency key the caller sends."""

    @staticmethod
    def transfer(idem_key, account, ifsc, beneficiary, amount):
        ref = "SIMNEFT-" + hashlib.sha256(idem_key.encode()).hexdigest()[:12].upper()
        with tx() as c:
            c.execute("INSERT OR IGNORE INTO sim_bank_transfers(idem_key, bank_ref, account, ifsc, beneficiary, amount, at) VALUES (?,?,?,?,?,?,?)",
                      (idem_key, ref, account, ifsc, beneficiary, amount, now()))
            t = dict(c.execute("SELECT * FROM sim_bank_transfers WHERE idem_key=?", (idem_key,)).fetchone())
        if abs(t["amount"] - amount) > 0.001 or t["account"] != account:
            raise DomainError("bank refused: idempotency key reused with different details", 409)
        return t


class PayCommand:
    def __init__(self, payment_id):
        self.pid = payment_id

    def execute(self):
        p = row("SELECT * FROM proc_payments WHERE payment_id=?", (self.pid,))
        if not p:
            raise DomainError("payment not found", 404)
        if p["status"] == "paid":
            return {"already_paid": True, "bank_ref": p["bank_ref"]}
        if p["status"] != "processing":
            raise DomainError(f"payment is {p['status'].replace('_', ' ')}, not approved", 409)
        v = VENDORS.payable_vendor_any(p["vendor_id"])
        if not v or v["bank"]["account_no"] != p["pay_to_account"] or v["bank"]["ifsc"] != p["pay_to_ifsc"]:
            sent = row("SELECT 1 FROM sim_bank_transfers WHERE idem_key=?", (self.pid,))
            if not sent:   # money has not moved: stop and send back to a person
                reason = "Vendor's bank details or status changed after approval; held for re-approval. Confirm by call-back to the contact on file."
                with tx() as c:
                    PAYMENT_FLOW.move(c, self.pid, "processing", "hold", reason=reason, approved_by=None)
                store.log(f"pay:{self.pid}", "HELD before bank call: " + reason, "warn")
                return {"held": True, "reason": reason}
        t = SimBank.transfer(self.pid, p["pay_to_account"], p["pay_to_ifsc"], p["pay_to_name"], p["amount"])
        if os.environ.get("PROCURE_CRASH_AFTER_TRANSFER"):   # demo/test hook: die after the bank moved money
            raise SystemExit(9)
        with tx() as c:
            cur = c.execute("SELECT status FROM proc_payments WHERE payment_id=?", (self.pid,)).fetchone()
            if cur["status"] == "paid":
                return {"already_paid": True, "bank_ref": t["bank_ref"]}
            for a in c.execute("SELECT bill_uuid, amount FROM proc_payment_allocations WHERE payment_id=?", (self.pid,)).fetchall():
                InvoiceService.apply_payment(c, a["bill_uuid"], a["amount"], self.pid)
            PAYMENT_FLOW.move(c, self.pid, "processing", "settle", bank_ref=t["bank_ref"], paid_at=now())
        store.log(f"pay:{self.pid}", f"PAID ₹{p['amount']:,.2f} to {p['pay_to_name']} a/c {p['pay_to_account']} ref {t['bank_ref']}")
        BUS.publish("payment.paid", {"payment_id": self.pid, "vendor_id": p["vendor_id"], "amount": p["amount"], "bank_ref": t["bank_ref"]})
        return {"paid": True, "bank_ref": t["bank_ref"]}


# ---------------- vendor lookup across fixtures + onboarding ----------------

class _Vendors:
    def payable_vendor_any(self, vendor_id):
        from .. import ap
        return ap.vendors().get(vendor_id)


VENDORS = _Vendors()


# ---------------- service ----------------

class PayablesService:
    def __init__(self):
        ensure("payables", SCHEMA)

    def view(self, payment_id):
        p = row("SELECT * FROM proc_payments WHERE payment_id=?", (payment_id,))
        if not p:
            raise DomainError("payment not found", 404)
        p["allocations"] = rows("""SELECT a.bill_uuid, a.amount, b.bill_no, b.amount bill_amount, b.balance, b.status bill_status
                                   FROM proc_payment_allocations a LEFT JOIN proc_bills b USING(bill_uuid) WHERE a.payment_id=?""", (payment_id,))
        p["checks"] = json.loads(p.pop("checks_json") or "[]")
        p["allowed_actions"] = sorted(a for a in PAYMENT_FLOW.t.get(p["status"], {}) if a not in ("settle", "hold", "edit"))
        p["history"] = audit("proc_payments", payment_id)
        p["bank_transfer"] = row("SELECT * FROM sim_bank_transfers WHERE idem_key=?", (payment_id,))
        from .. import ap
        p["vendor_name"] = (ap.vendors().get(p["vendor_id"]) or {}).get("legal_name")
        return p

    def list(self, status=None, vendor_id=None):
        q, a = "SELECT payment_id FROM proc_payments WHERE 1=1", []
        if status:
            q += " AND status=?"; a.append(status)
        if vendor_id:
            q += " AND vendor_id=?"; a.append(vendor_id)
        return [self.view(r["payment_id"]) for r in rows(q + " ORDER BY created_at DESC", a)]

    def unpaid_bills(self, vendor_id):
        out = []
        for b in rows("SELECT * FROM proc_bills WHERE vendor_id=? AND status IN ('open','partially_paid') ORDER BY due_date", (vendor_id,)):
            b["reserved"] = _reserved(b["bill_uuid"])
            b["payable_now"] = round(max(0.0, b["balance"] - b["reserved"]), 2)
            out.append(b)
        return out

    @idempotent("create_payment")
    def create(self, body, by="AP"):
        vid = body.get("vendor_id")
        allocs = [{"bill_uuid": a["bill_uuid"], "amount": round(float(a["amount"]), 2)} for a in body.get("allocations") or [] if float(a.get("amount") or 0) > 0]
        if not vid or not allocs:
            raise DomainError("choose a vendor and apply an amount to at least one bill")
        if len({a["bill_uuid"] for a in allocs}) != len(allocs):
            raise DomainError("each bill can appear once in a payment")
        total = round(sum(a["amount"] for a in allocs), 2)
        if body.get("amount") is not None and abs(float(body["amount"]) - total) > 0.001:
            raise DomainError(f"Payment Made ₹{float(body['amount']):,.2f} differs from the total applied to bills ₹{total:,.2f}; excess payments are not supported")
        p = {"vendor_id": vid, "amount": total}
        checks = evaluate(p, allocs)
        fails = [c for c in checks if c["result"] == "fail"]
        if fails:
            raise DomainError("payment not recorded: " + "; ".join(c["reason"] for c in fails), 422, checks=checks)
        pid, t = new_id(), now()
        with tx() as c:
            n = c.execute("SELECT COUNT(*) n FROM proc_payments").fetchone()["n"] + 1
            c.execute("""INSERT INTO proc_payments(payment_id, payment_no, vendor_id, amount, mode, paid_through, payment_date, reference, status,
                         created_by, checks_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'draft', ?,?,?,?)""",
                      (pid, f"PM-{n:05d}", vid, total, body.get("mode") or "Bank Transfer", body.get("paid_through") or "Current Account",
                       body.get("payment_date") or time.strftime("%Y-%m-%d"), body.get("reference"), by, dumps(checks), t, t))
            for a in allocs:
                c.execute("INSERT INTO proc_payment_allocations(payment_id, bill_uuid, amount) VALUES (?,?,?)", (pid, a["bill_uuid"], a["amount"]))
            # re-check inside the write lock: a racing payment for the same bill cannot slip in between
            for a in allocs:
                b = c.execute("SELECT balance, bill_no FROM proc_bills WHERE bill_uuid=?", (a["bill_uuid"],)).fetchone()
                res = c.execute(f"""SELECT COALESCE(SUM(a.amount),0) s FROM proc_payment_allocations a JOIN proc_payments p USING(payment_id)
                                    WHERE a.bill_uuid=? AND p.status IN ({','.join('?' * len(IN_FLIGHT))})""", (a["bill_uuid"], *IN_FLIGHT)).fetchone()["s"]
                if res > b["balance"] + 0.001:
                    raise DomainError(f"bill {b['bill_no']} would be paid more than its balance by payments in flight", 409)
            c.execute("INSERT INTO proc_audit(entity, entity_id, action, from_status, to_status, detail, at) VALUES ('proc_payments',?,?,?,?,?,?)",
                      (pid, "create", None, "draft", dumps({"by": by, "allocations": allocs}), t))
        store.log(f"pay:{pid}", f"payment recorded by {by}: ₹{total:,.2f} to {vid}")
        if body.get("submit"):
            self.submit(pid, by)
        return 201, self.view(pid)

    def submit(self, payment_id, by):
        p = self.view(payment_id)
        checks = evaluate(p, p["allocations"])
        fails = [c for c in checks if c["result"] == "fail"]
        if fails:
            raise DomainError("cannot submit: " + "; ".join(c["reason"] for c in fails), 422, checks=checks)
        with tx() as c:
            PAYMENT_FLOW.move(c, payment_id, p["status"], "submit", checks_json=dumps(checks), reason=None)
        return self.view(payment_id)

    @idempotent("approve_payment:{0}")
    def approve(self, body, payment_id, by="AP"):
        """Approve = the irreversible step. Snapshot the verified account, commit the intent, then pay once."""
        p = self.view(payment_id)
        if p["status"] != "pending_approval":
            raise DomainError(f"payment is {p['status'].replace('_', ' ')}; only a payment pending approval can be approved", 409)
        checks = evaluate(p, p["allocations"])
        fails = [c for c in checks if c["result"] == "fail"]
        if fails:
            raise DomainError("cannot approve: " + "; ".join(c["reason"] for c in fails), 422, checks=checks)
        warns = [c for c in checks if c["result"] == "warn"]
        note = (body.get("note") or "").strip()
        if warns and not note:
            raise DomainError("checks need attention: add an approver note", 422, checks=checks)
        v = VENDORS.payable_vendor_any(p["vendor_id"])
        with tx() as c:
            PAYMENT_FLOW.move(c, payment_id, "pending_approval", "approve", approved_by=by, approver_note=note or None, checks_json=dumps(checks),
                              pay_to_account=v["bank"]["account_no"], pay_to_ifsc=v["bank"]["ifsc"], pay_to_name=v["legal_name"])
        store.log(f"pay:{payment_id}", f"APPROVED by {by}")
        res = PayCommand(payment_id).execute()
        return 200, {**self.view(payment_id), "result": res}

    def reject(self, payment_id, reason, by):
        if not reason:
            raise DomainError("a reason is required")
        p = self.view(payment_id)
        with tx() as c:
            PAYMENT_FLOW.move(c, payment_id, p["status"], "reject", reason=reason)
        return self.view(payment_id)

    def void(self, payment_id, reason, by):
        p = self.view(payment_id)
        with tx() as c:
            PAYMENT_FLOW.move(c, payment_id, p["status"], "void", reason=reason or "voided")
        return self.view(payment_id)

    def convert_to_draft(self, payment_id, by):
        p = self.view(payment_id)
        allocs = p["allocations"]
        checks = evaluate({**p}, allocs)
        if any(c["result"] == "fail" for c in checks):
            raise DomainError("cannot reopen: " + "; ".join(c["reason"] for c in checks if c["result"] == "fail"), 422)
        with tx() as c:
            PAYMENT_FLOW.move(c, payment_id, "void", "convert_to_draft", reason=None)
        return self.view(payment_id)

    def resume(self):
        """After a crash: finish every approved payment exactly once (the bank dedupes on the payment id)."""
        return [{"payment_id": r["payment_id"], **PayCommand(r["payment_id"]).execute()}
                for r in rows("SELECT payment_id FROM proc_payments WHERE status='processing'")]


def _on_vendor_bank_changed(evt):
    """Observer: a vendor's bank changed -> every payment of theirs not yet sent goes back to a person."""
    for p in rows("SELECT payment_id, status FROM proc_payments WHERE vendor_id=? AND status='processing'", (evt["vendor_id"],)):
        PayCommand(p["payment_id"]).execute()   # holds it, because the account no longer matches the snapshot


BUS.subscribe("vendor.bank_changed", _on_vendor_bank_changed)
