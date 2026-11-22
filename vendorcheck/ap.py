r"""Payables desk: invoice in -> PAID once, or HELD with a cited reason.

State machine per invoice (each transition logged, each step idempotent):
  received -> extracted -> matched -> verified -> awaiting_approval -> approved -> paid
                                   \-> held | duplicate | rejected

Payment is a separate, explicit step keyed by `vendor_id:normalised_invoice_no`; the
payments table has that key as PRIMARY KEY, so one invoice is paid at most once even if
the process dies between approval and payment and is restarted.
"""
import hashlib
import json
import os
import re
import time

from . import crust, pipeline, store
from .config import CONFIG

ROOT = os.path.join(os.path.dirname(__file__), "..")
FIX = os.environ.get("VENDORCHECK_DATA", os.path.join(ROOT, "data", "fixtures"))


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def vendors():
    return {v["vendor_id"]: v for v in load("vendors.json")}


def vendor_hash(v):
    keep = {k: v.get(k) for k in ("vendor_id", "legal_name", "gstin", "bank", "domain")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


# ---------------- extraction (deterministic: same text -> same fields) ----------------

AMOUNT_RE = re.compile(r"(?:total|amount(?: due| payable)?|grand total|invoice value)\s*[:\-]?\s*(?:inr|rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)", re.I)


def _find(pat, text, flags=re.I):
    m = re.search(pat, text, flags)
    return m.group(1).strip() if m else None


def norm_invoice_no(s):
    if not s:
        return None
    # INV/2026/0451 == INV-2026-451 == 2026 451: drop prefixes, compare number groups without leading zeros
    toks = re.findall(r"[A-Za-z]+|\d+", s.upper())
    toks = [t for t in toks if t not in ("INV", "INVOICE", "NO", "BILL", "TAX", "TI")]
    return "-".join(str(int(t)) if t.isdigit() else t for t in toks)


def classify(text):
    t = text.lower()
    has_inv_no = bool(re.search(r"invoice\s*(no|number|#)", t))
    if re.search(r"(chang(ed|ing)|new|update[d]?|switch(ed)?)\s+(our\s+|of\s+)?bank|bank (account )?details have changed|new account details", t) and not has_inv_no:
        return "bank_change_request"
    if has_inv_no or "tax invoice" in t:
        return "invoice"
    if "receipt" in t:
        return "receipt"
    if "reminder" in t or "overdue" in t:
        return "reminder"
    return "unknown"


def extract(text):
    amt = None
    m = AMOUNT_RE.search(text)
    if m:
        amt = float(m.group(1).replace(",", ""))
    sender = _find(r"^from:\s*.*?<?([\w.+-]+@[\w.-]+)>?\s*$", text, re.I | re.M)
    return {
        "doc_type": classify(text),
        "sender": sender.lower() if sender else None,
        "sender_domain": sender.split("@")[1].lower() if sender else None,
        "invoice_no": _find(r"invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9/\-_.]+)", text),
        "invoice_date": _find(r"(?:invoice\s*)?date\s*[:\-]\s*([0-9]{1,2}[\s/\-][A-Za-z0-9]{1,9}[\s/\-][0-9]{2,4})", text),
        "gstin": (_find(r"gstin\s*(?:no\.?)?\s*[:\-]?\s*([0-9A-Z]{15})", text) or "").upper() or None,
        "po_no": _find(r"\b(?:p\.?o\.?|purchase order)\s*(?:no\.?|number|#)?\s*[:\-]\s*([A-Za-z0-9/\-]+)", text),
        "amount_inr": amt,
        "qty": float(_find(r"(?:qty|quantity)\s*[:\-]?\s*([\d.]+)", text) or 0) or None,
        "account_no": _find(r"(?:a/?c|account)\s*(?:no\.?|number)\s*[:\-]?\s*([0-9]{9,18})", text),
        "ifsc": (_find(r"ifsc\s*(?:code)?\s*[:\-]?\s*([A-Za-z]{4}0[A-Za-z0-9]{6})", text) or "").upper() or None,
        "account_name": _find(r"(?:account name|beneficiary(?: name)?|a/?c name)\s*[:\-]\s*(.+)", text),
        "vendor_name_text": _find(r"^(?:vendor|supplier|from company|billed by)\s*[:\-]\s*(.+)$", text, re.I | re.M),
    }


# ---------------- checks: each returns an evidenced verdict ----------------

def chk(name, result, reasoning, evidence=None, weight=0):
    return {"check": name, "result": result, "reasoning": reasoning, "evidence": evidence or [],
            "weight": weight, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def identify_vendor(x, vmap):
    if x.get("gstin"):
        for v in vmap.values():
            if v.get("gstin") == x["gstin"]:
                return v, "gstin"
    if x.get("sender_domain"):
        for v in vmap.values():
            if v.get("domain") == x["sender_domain"]:
                return v, "sender_domain"
    best, score = None, 0
    blob = (x.get("vendor_name_text") or "") + " " + (x.get("account_name") or "")
    for v in vmap.values():
        s = pipeline.name_similarity(blob, v["legal_name"])
        if s > score:
            best, score = v, s
    if best and score >= 0.5:
        return best, "name"
    return None, None


def check_sender(x, v):
    d = x.get("sender_domain")
    if not d:
        return chk("sender_domain", "warn", "No sender address on the document", weight=5)
    if d == v.get("domain"):
        return chk("sender_domain", "pass", f"Sent from {d}, the domain on the vendor record", [{"source": "vendor master", "field": "domain", "value": v.get("domain")}])
    dist = edit_distance(d, v.get("domain") or "")
    if dist <= 3 or any(t in d for t in pipeline.norm_tokens(v["legal_name"])):
        return chk("sender_domain", "fail", f"Look-alike domain: sent from {d}, vendor's domain is {v.get('domain')} (edit distance {dist}). Possible impersonation.",
                   [{"source": "vendor master", "field": "domain", "value": v.get("domain")}, {"source": "email header", "field": "From", "value": x.get("sender")}], weight=40)
    return chk("sender_domain", "warn", f"Sent from {d}, which is not the vendor's domain {v.get('domain')}", weight=10)


def check_duplicate(key, x, v):
    row = store.db().execute("SELECT invoice_key, state FROM invoices WHERE invoice_key=?", (key,)).fetchone()
    if row:
        return chk("duplicate", "fail", f"Invoice {x.get('invoice_no')} from {v['legal_name']} was already received (state: {row['state']}). Same number after normalising ({norm_invoice_no(x.get('invoice_no'))}).",
                   [{"source": "invoice ledger", "field": "invoice_key", "value": key}], weight=100), row["invoice_key"]
    # same vendor, same amount, same date, different number -> likely re-issued duplicate
    for r in store.db().execute("SELECT invoice_key, extracted_json, state FROM invoices WHERE vendor_id=?", (v["vendor_id"],)):
        e = json.loads(r["extracted_json"])
        if e.get("amount_inr") == x.get("amount_inr") and e.get("invoice_date") == x.get("invoice_date") and e.get("po_no") == x.get("po_no") and r["state"] not in ("rejected",):
            return chk("duplicate", "fail", f"Same vendor, amount ₹{x.get('amount_inr'):,.2f}, date {x.get('invoice_date')} and PO as invoice {r['invoice_key']} (state {r['state']}): a re-issued duplicate under a new number.",
                       [{"source": "invoice ledger", "field": "invoice_key", "value": r["invoice_key"]}], weight=100), r["invoice_key"]
    return chk("duplicate", "pass", "No earlier invoice with this number, or with the same vendor, amount, date and PO", weight=0), None


def check_po(x, v):
    pos = {p["po_no"]: p for p in load("pos.json")}
    grns = {g["po_no"]: g for g in load("grns.json")}
    po = pos.get(x.get("po_no"))
    if not po:
        return chk("three_way_match", "fail", f"PO {x.get('po_no')} not found in purchase orders", weight=40)
    ev = [{"source": "purchase orders", "field": "po_no", "value": po["po_no"]}]
    if po["vendor_id"] != v["vendor_id"]:
        return chk("three_way_match", "fail", f"PO {po['po_no']} was raised on {po['vendor_id']}, not {v['vendor_id']}", ev, weight=40)
    amt = x.get("amount_inr") or 0
    if abs(amt - po["amount_inr"]) > max(1.0, 0.01 * po["amount_inr"]):
        return chk("three_way_match", "fail", f"Invoice amount ₹{amt:,.2f} does not match PO amount ₹{po['amount_inr']:,.2f}", ev, weight=40)
    billed = 0.0
    for r in store.db().execute("SELECT extracted_json FROM invoices WHERE vendor_id=? AND state IN ('awaiting_approval','approved','paid','held')", (v["vendor_id"],)):
        e = json.loads(r["extracted_json"])
        if e.get("po_no") == po["po_no"]:
            billed += e.get("amount_inr") or 0
    if billed + amt > po["amount_inr"] * 1.01:
        return chk("three_way_match", "fail", f"PO {po['po_no']} (₹{po['amount_inr']:,.2f}) is already invoiced for ₹{billed:,.2f}; this invoice would bill it twice", ev, weight=60)
    g = grns.get(po["po_no"])
    if not g:
        return chk("three_way_match", "fail", f"No goods receipt recorded against PO {po['po_no']}", ev, weight=30)
    ev.append({"source": "goods receipts", "field": "grn_no", "value": g["grn_no"]})
    if x.get("qty") and x["qty"] > g["qty_received"]:
        return chk("three_way_match", "fail", f"Invoice bills {x['qty']} but only {g['qty_received']} received ({g['grn_no']})", ev, weight=30)
    return chk("three_way_match", "pass", f"Matches PO {po['po_no']} (₹{po['amount_inr']:,.2f}) and goods receipt {g['grn_no']}", ev)


def check_bank(x, v):
    b = v.get("bank") or {}
    ev = [{"source": "vendor master (verified at onboarding)", "field": "bank", "value": f"{b.get('account_no')} / {b.get('ifsc')}", "verified_on": b.get("verified_on")}]
    if not x.get("account_no"):
        return chk("bank_details", "pass", "No bank details on the invoice; will pay the verified account on file", ev)
    if x["account_no"] != b.get("account_no") or (x.get("ifsc") and x["ifsc"] != b.get("ifsc")):
        ev.append({"source": "invoice", "field": "bank", "value": f"{x['account_no']} / {x.get('ifsc')}"})
        return chk("bank_details", "fail", f"Invoice asks for payment to account {x['account_no']} ({x.get('ifsc')}), but the verified account on file is {b.get('account_no')} ({b.get('ifsc')}). Possible payment diversion.", ev, weight=60)
    if x.get("account_name") and pipeline.name_similarity(x["account_name"], v["legal_name"]) < 0.5:
        return chk("bank_details", "fail", f"Account holder '{x['account_name']}' does not match vendor legal name '{v['legal_name']}'", ev, weight=50)
    return chk("bank_details", "pass", "Bank account matches the verified account on file", ev)


def check_trust(v):
    """Vendor check (5.1 + 5.2) on live data, cached per vendor."""
    req = {"vendor_name": v["legal_name"], "website": v.get("domain"), "director": v.get("director"),
           "request_id": f"trust:{v['vendor_id']}"}
    try:
        rep = pipeline.run_request(req)
    except (crust.ApiError, OSError, TimeoutError, RuntimeError) as e:
        return chk("vendor_trust", "unavailable", f"Source unavailable after retries: {e}. Not the same as 'no record'; holding until it can be checked.", weight=50), None
    ev = [{"claim": f["claim"], "value": f["value"], "source": f["source"]} for f in rep["facts"]]
    if rep["verdict"] == "CLEAR":
        return chk("vendor_trust", "pass", rep["summary"], ev), rep
    if rep["verdict"] == "CANNOT_VERIFY":
        return chk("vendor_trust", "fail", "Cannot verify the vendor: " + "; ".join(rep["cannot_verify"]), ev, weight=70), rep
    reasons = "; ".join(f["reason"] for f in rep["flags"])
    red = any(f["severity"] == "red" for f in rep["flags"])
    return chk("vendor_trust", "fail" if red else "warn", reasons, ev, weight=60 if red else 20), rep


# ---------------- drafts (never sent) ----------------

def draft_bank_verification(v, x):
    c = v.get("contact") or {}
    return {
        "status": "DRAFT - not sent. A person must send it after checking the recipient.",
        "to": c.get("email"), "phone_to_call_back": c.get("phone"),
        "note": "Recipient and phone come from the vendor master, never from the request itself.",
        "subject": f"Confirming a bank detail change request for {v['legal_name']}",
        "body": (f"Hello {c.get('name') or ''},\n\nWe received a request to change the bank account we pay {v['legal_name']} into"
                 f"{' (new account ending ' + x['account_no'][-4:] + ')' if x.get('account_no') else ''}. Before we change anything, "
                 f"please confirm by calling us back from the number we have on file. Until then we will keep paying only the verified account on record.\n\nAccounts Payable"),
    }


# ---------------- orchestration ----------------

def process_file(path, text=None):
    text = text if text is not None else open(path, encoding="utf-8").read()
    chash = hashlib.sha256(text.encode()).hexdigest()[:16]
    seen = store.db().execute("SELECT * FROM files_seen WHERE source_file=? AND content_hash=?", (path, chash)).fetchone()
    if seen:
        return {"source_file": path, "invoice_key": seen["invoice_key"], "outcome": seen["outcome"], "note": "already processed (same file, same content)"}
    x = extract(text)
    vmap = vendors()
    log = lambda m: store.log(f"inv:{os.path.basename(path)}", m)
    log(f"received; classified as {x['doc_type']}")
    checks = [chk("classify", "pass", f"Document type: {x['doc_type']}")]

    v, how = identify_vendor(x, vmap)
    if not v:
        outcome = _finish(path, chash, None, "unknown", x, "held", checks + [chk("vendor_identified", "fail", "No vendor on record matches the GSTIN, sender domain or name", weight=80)])
        return outcome
    checks.append(chk("vendor_identified", "pass", f"{v['legal_name']} ({v['vendor_id']}) matched by {how}", [{"source": "vendor master", "field": how, "value": v.get(how if how != 'name' else 'legal_name')}]))

    if x["doc_type"] == "bank_change_request":
        checks.append(check_sender(x, v))
        checks.append(chk("bank_change", "fail", "Request to change bank details. Bank details are only changed after an out-of-band call-back to the number on file.", weight=60))
        key = f"{v['vendor_id']}:BANKCHG:{chash}"
        return _finish(path, chash, key, v["vendor_id"], x, "held", checks, draft=draft_bank_verification(v, x))

    if x["doc_type"] != "invoice":
        key = f"{v['vendor_id']}:{x['doc_type']}:{chash}"
        return _finish(path, chash, key, v["vendor_id"], x, "rejected", checks + [chk("payable", "fail", f"A {x['doc_type']} is not a payable document; filed, nothing to pay")])

    key = f"{v['vendor_id']}:{norm_invoice_no(x['invoice_no'])}"
    dup, orig = check_duplicate(key, x, v)
    checks.append(dup)
    if dup["result"] == "fail":
        return _finish(path, chash, key + f"#dup-{chash}", v["vendor_id"], x, "duplicate", checks, duplicate_of=orig)
    checks += [check_sender(x, v), check_po(x, v), check_bank(x, v)]
    tc, trust_report = check_trust(v)
    checks.append(tc)
    fails = [c for c in checks if c["result"] in ("fail", "unavailable")]
    warns = [c for c in checks if c["result"] == "warn"]
    draft = draft_bank_verification(v, x) if any(c["check"] == "bank_details" and c["result"] == "fail" for c in checks) else None
    if fails or warns:
        return _finish(path, chash, key, v["vendor_id"], x, "held", checks, draft=draft, trust=trust_report)
    return _finish(path, chash, key, v["vendor_id"], x, "awaiting_approval", checks, trust=trust_report)


def _score(checks):
    rows = [{"signal": c["check"], "result": c["result"], "weight": c["weight"],
             "contribution": c["weight"] if c["result"] in ("fail", "unavailable") else (c["weight"] // 2 if c["result"] == "warn" else 0)} for c in checks]
    return {"risk_score": min(100, sum(r["contribution"] for r in rows)), "breakdown": rows}


def _finish(path, chash, key, vendor_id, x, state, checks, draft=None, duplicate_of=None, trust=None):
    score = _score(checks)
    reason = "; ".join(c["reasoning"] for c in checks if c["result"] in ("fail", "unavailable", "warn")) or "All checks passed"
    decision = {"state": state, "reason": reason, "checks": checks, **score, "draft_message": draft, "duplicate_of": duplicate_of,
                "trust_report": trust, "cost_credits": trust["cost"]["credits"] if trust else 0}
    now = time.time()
    with store._lock:
        store.db().execute("""INSERT INTO invoices(invoice_key, source_file, doc_type, vendor_id, extracted_json, state, decision_json, created_at, updated_at)
                              VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(invoice_key) DO NOTHING""",
                           (key or f"unknown:{chash}", path, x["doc_type"], vendor_id, json.dumps(x), state, json.dumps(decision), now, now))
        store.db().execute("INSERT OR REPLACE INTO files_seen VALUES (?,?,?,?,?)", (path, chash, key, state, now))
    store.log(f"inv:{os.path.basename(path)}", f"{state.upper()}: {reason[:160]}")
    if state == "awaiting_approval" and x.get("amount_inr") and x["amount_inr"] <= CONFIG.get("auto_approve_below_inr", 0):
        approve(key, "policy:auto-approve-limit")
    return {"source_file": path, "invoice_key": key, "state": state, "reason": reason, "risk_score": score["risk_score"]}


def approve(key, who):
    inv = store.db().execute("SELECT * FROM invoices WHERE invoice_key=?", (key,)).fetchone()
    if not inv or inv["state"] != "awaiting_approval":
        return {"ok": False, "error": f"not awaiting approval (state: {inv['state'] if inv else 'missing'})"}
    v = vendors()[inv["vendor_id"]]
    with store._lock:
        store.db().execute("UPDATE invoices SET state='approved', vendor_hash_at_approval=?, updated_at=?, decision_json=json_set(decision_json,'$.approved_by',?) WHERE invoice_key=? AND state='awaiting_approval'",
                           (vendor_hash(v), time.time(), who, key))
    store.log(f"inv:{key}", f"APPROVED by {who}")
    if os.environ.get("CRASH_AFTER_APPROVE"):  # demo hook: simulate a kill between approval and payment
        os._exit(9)
    return pay(key)


def pay(key):
    """Exactly-once payment to the simulated ledger. Safe to call any number of times."""
    inv = store.db().execute("SELECT * FROM invoices WHERE invoice_key=?", (key,)).fetchone()
    if not inv:
        return {"ok": False, "error": "missing"}
    if inv["state"] == "paid":
        p = store.db().execute("SELECT * FROM payments WHERE idempotency_key=?", (key,)).fetchone()
        return {"ok": True, "already_paid": True, "payment": dict(p) if p else None}
    if inv["state"] != "approved":
        return {"ok": False, "error": f"state is {inv['state']}"}
    v = vendors()[inv["vendor_id"]]
    if vendor_hash(v) != inv["vendor_hash_at_approval"]:
        with store._lock:
            store.db().execute("UPDATE invoices SET state='held', updated_at=?, decision_json=json_set(decision_json,'$.reason',?) WHERE invoice_key=?",
                               (time.time(), "Vendor master record (bank / GSTIN / domain) changed between approval and payment. Held for re-approval.", key))
        store.log(f"inv:{key}", "HELD: vendor master changed after approval")
        return {"ok": False, "error": "vendor record changed after approval; held"}
    x = json.loads(inv["extracted_json"])
    d = json.loads(inv["decision_json"])
    b = v["bank"]
    ref = "SIMPAY-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()
    with store._lock:
        conn = store.db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT OR IGNORE INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (key, key, v["vendor_id"], x["amount_inr"], b["account_no"], b["ifsc"], v["legal_name"], d.get("approved_by", "?"), time.time(), ref))
            conn.execute("UPDATE invoices SET state='paid', updated_at=? WHERE invoice_key=?", (time.time(), key))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    store.log(f"inv:{key}", f"PAID ₹{x['amount_inr']:,.2f} to {v['legal_name']} a/c {b['account_no']} ref {ref}")
    return {"ok": True, "ref": ref}


def resume():
    """On start-up: finish any payment that was approved but not yet paid (crash recovery)."""
    done = []
    for r in store.db().execute("SELECT invoice_key FROM invoices WHERE state='approved'").fetchall():
        store.log(None, f"resume: approved but unpaid -> paying {r['invoice_key']}")
        done.append(pay(r["invoice_key"]))
    return done


def process_inbox(folder=None):
    folder = folder or os.path.join(FIX, "inbox")
    resume()
    out = []
    for f in sorted(os.listdir(folder)):
        if f.endswith((".txt", ".eml")):
            out.append(process_file(os.path.join(folder, f)))
    return out


def invoices():
    return [dict(r) for r in store.db().execute("SELECT invoice_key, source_file, doc_type, vendor_id, state, updated_at, decision_json, extracted_json FROM invoices ORDER BY created_at")]


def payments():
    return [dict(r) for r in store.db().execute("SELECT * FROM payments ORDER BY paid_at")]


def recheck(key):
    """Replay: re-run every check for a HELD invoice (e.g. after a source was unavailable). Never touches paid ones."""
    inv = store.db().execute("SELECT * FROM invoices WHERE invoice_key=?", (key,)).fetchone()
    if not inv or inv["state"] not in ("held",):
        return {"ok": False, "error": f"only held invoices can be re-checked (state: {inv['state'] if inv else 'missing'})"}
    before = json.loads(inv["decision_json"])
    if inv["vendor_id"]:
        # drop the cached trust verdict if it ended in an outage, so it is fetched again
        store.db().execute("DELETE FROM jobs WHERE job_id=? AND status!='done'", (f"trust:{inv['vendor_id']}",))
    with store._lock:
        store.db().execute("DELETE FROM invoices WHERE invoice_key=?", (key,))
        store.db().execute("DELETE FROM files_seen WHERE source_file=?", (inv["source_file"],))
    after = process_file(inv["source_file"])
    return {"ok": True, "before": {"state": before["state"], "reason": before["reason"]}, "after": after}
