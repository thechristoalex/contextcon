"""Post bank-statement lines to the right payee and ledger account.

Input: CSV with columns date, description, amount (INR). For each line:
  - payee from the vendor master (aliases), else Crustdata Company Identify on the cleaned
    descriptor; a payee is named only when exactly one company matches strongly
  - ledger account from the client's chart of accounts (data/fixtures/accounts.json)
  - a line with the same date, payee/descriptor and amount as an earlier line is HELD as a possible duplicate
  - no source settles the payee -> CANNOT_VERIFY, held for a person (never a guess)
Posting is idempotent: each (file, line number) is posted once, however often the file is re-run.
"""
import csv
import hashlib
import json
import os
import re
import time

from . import ap, crust, pipeline, store

PREFIXES = re.compile(r"^(RAZ|RZP|PAYTM|PYTM|PAYU|CCAVENUE|POS|UPI|NEFT|IMPS|ACH|ECS|BIL|INB)[\*/:\- ]+", re.I)
CITY = {"BLR", "BANGALORE", "BENGALURU", "MUM", "MUMBAI", "DEL", "DELHI", "GGN", "GURGAON", "HYD", "CHN", "PUNE", "IN", "IND", "INDIA", "EMEA", "APAC"}
TRAIL = {"TRD", "TRADERS", "ENT", "PVT", "LTD", "SERVICES", "SVCS"}

SCHEMA = """CREATE TABLE IF NOT EXISTS bank_postings (
  line_key TEXT PRIMARY KEY, source_file TEXT, line_no INTEGER, date TEXT, description TEXT, amount_inr REAL,
  result TEXT, payee TEXT, account TEXT, reason TEXT, evidence_json TEXT, at REAL)"""


def clean_descriptor(d):
    s = PREFIXES.sub("", d.strip())
    toks = [t for t in re.split(r"[\s\*/]+", s.upper()) if t and t not in CITY]
    core = [t for t in toks if t not in TRAIL]
    return " ".join(core or toks)


def accounts():
    try:
        return ap.load("accounts.json")
    except FileNotFoundError:
        return {}


def account_for(payee):
    low = (payee or "").lower()
    for k, v in accounts().items():
        if k in low:
            return v
    return None


def resolve_payee(job_id, desc):
    core = clean_descriptor(desc)
    # 1. client's own vendor master
    for v in ap.vendors().values():
        names = [v["legal_name"]] + v.get("aliases", [])
        if any(pipeline.name_similarity(core, n) >= 0.5 for n in names):
            return {"payee": v["legal_name"], "via": "vendor master", "evidence": [{"source": "vendor master", "field": "legal_name", "value": v["legal_name"]}]}
    # 2. live: Crustdata Company Identify by name, accepted only if exactly one strong, exact-name match
    if len(core) < 3:
        return {"payee": None, "reason": f"descriptor '{desc}' has no usable name"}
    resp, meta = crust.call(job_id, "/company/identify", {"names": [core]})
    rows = pipeline.first_list(resp)
    ms = rows[0].get("matches", []) if rows else []
    strong = [m for m in ms if m.get("confidence_score", 0) >= 0.95
              and pipeline.name_similarity(core, (m.get("company_data") or {}).get("basic_info", {}).get("name", "")) >= 0.99]
    if len(strong) == 1:
        bi = strong[0]["company_data"]["basic_info"]
        return {"payee": bi.get("name"), "via": "crustdata", "evidence": [crust.source_ref(meta, "matches[0].company_data.basic_info.name") | {"value": bi.get("name"), "domain": bi.get("primary_domain")}]}
    if len(strong) > 1:
        # brand rule: card/UPI descriptors name established merchants. Accept only if exactly one strong match
        # owns the domain named after the descriptor (swiggy -> swiggy.com) and is an established company.
        label = re.sub(r"[^a-z0-9]", "", core.lower())
        brand = [m for m in strong if (m["company_data"]["basic_info"].get("primary_domain") or "").split(".")[0] == label
                 and m["company_data"]["basic_info"].get("employee_count_range") in ("1001-5000", "5001-10000", "10001+")]
        if len(brand) == 1:
            bi = brand[0]["company_data"]["basic_info"]
            return {"payee": bi.get("name"), "via": f"crustdata (only one of {len(strong)} same-name records owns {bi.get('primary_domain')} and is an established company)",
                    "evidence": [crust.source_ref(meta, "matches[].company_data.basic_info.{name,primary_domain,employee_count_range}") | {"value": bi.get("name"), "domain": bi.get("primary_domain"), "employees": bi.get("employee_count_range")}]}
        return {"payee": None, "reason": f"'{core}' matches {len(strong)} different companies equally well; no source identifies which one was paid"}
    return {"payee": None, "reason": f"no source identifies a company called '{core}'"}


def post_file(path):
    store.db().execute(SCHEMA)
    job_id = "bank:" + os.path.basename(path)
    out, seen = [], []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, 1):
        key = hashlib.sha256(f"{os.path.abspath(path)}:{i}".encode()).hexdigest()[:16]
        prev = store.db().execute("SELECT * FROM bank_postings WHERE line_key=?", (key,)).fetchone()
        date, desc = row["date"].strip(), row["description"].strip()
        amt = float(str(row["amount"]).replace(",", "").replace("₹", ""))
        sig = (date, clean_descriptor(desc), amt)
        if prev:
            out.append(dict(prev) | {"note": "already posted; not posted again"})
            seen.append(sig)
            continue
        if sig in seen:
            res = {"result": "HELD", "payee": None, "account": None,
                   "reason": "Same descriptor, amount and date as an earlier line: held for approval as a possible duplicate charge", "evidence": []}
        else:
            try:
                r = resolve_payee(job_id, desc)
            except (crust.ApiError, OSError, RuntimeError) as e:
                r = {"payee": None, "reason": f"source unavailable ({e}); not the same as no record"}
            if r.get("payee"):
                acct = account_for(r["payee"])
                res = {"result": "POSTED" if acct else "HELD", "payee": r["payee"], "account": acct,
                       "reason": f"payee from {r['via']}" + ("" if acct else "; no ledger account mapped for this payee, held for a person"), "evidence": r["evidence"]}
            else:
                res = {"result": "CANNOT_VERIFY", "payee": None, "account": None, "reason": r["reason"] + ". Held for a person.", "evidence": []}
        seen.append(sig)
        with store._lock:
            store.db().execute("INSERT OR IGNORE INTO bank_postings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (key, path, i, date, desc, amt, res["result"], res["payee"], res["account"], res["reason"], json.dumps(res["evidence"]), time.time()))
        store.log(job_id, f"line {i} {desc} ₹{amt:,.0f}: {res['result']} {res['payee'] or ''} {res['account'] or ''}")
        out.append({"line": i, "date": date, "description": desc, "amount_inr": amt, **res})
    led = store.ledger_for(job_id)
    return {"file": path, "lines": out, "cost": {"credits": led["total_credits"], "paid_calls": led["paid_calls"]}}
