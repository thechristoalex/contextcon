"""Post bank-statement lines to the right payee and ledger account.

Input: CSV with columns date, description, amount (INR). For each line:
  - payee from the vendor master (aliases), else Crustdata Company Identify on the cleaned
    descriptor; a payee is named only when exactly one company matches strongly
  - the payee's legal name is taken from the company's own website when the page states it
    (e.g. "Swiggy Limited"); otherwise the Crustdata name is used and the report says so
  - ledger account from the client's chart of accounts (data/fixtures/accounts.json)
  - a bank transfer (NEFT/RTGS/IMPS) to a vendor on file must match a payment this desk made;
    money that left with no approved payment behind it is HELD as possibly unauthorised
  - a line with the same date, payee/descriptor and amount as an earlier line is HELD as a possible duplicate
  - no source settles the payee -> CANNOT_VERIFY, held for a person (never a guess)
Posting is idempotent by content: a line is keyed by its date, description, amount and how many
identical lines came before it in the statement, so re-running the file, uploading it under a
different name or importing an overlapping statement never posts the same bank line twice.
"""
import csv
import hashlib
import json
import os
import re
import time

from . import ap, crust, pipeline, store

PREFIXES = re.compile(r"^(RAZ|RZP|PAYTM|PYTM|PAYU|CCAVENUE|POS|UPI|NEFT|RTGS|IMPS|ACH|ECS|BIL|INB)[\*/:\- ]+", re.I)
TRANSFER = re.compile(r"^(NEFT|RTGS|IMPS)\b", re.I)
CITY = {"BLR", "BANGALORE", "BENGALURU", "MUM", "MUMBAI", "DEL", "DELHI", "GGN", "GURGAON", "HYD", "CHN", "PUNE", "IN", "IND", "INDIA", "EMEA", "APAC"}
TRAIL = {"TRD", "TRADERS", "ENT", "PVT", "LTD", "SERVICES", "SVCS"}
_W = r"(?!(?:Limited|Ltd|Private|Pvt)\b)[A-Z][A-Za-z0-9&\.\-]*"
LEGAL_RE = re.compile(rf"\b({_W}(?:\s+{_W}){{0,5}}\s+(?:Private\s+|Pvt\.?\s+)?(?:Limited|Ltd\.?))(?![A-Za-z])")

SCHEMA = """CREATE TABLE IF NOT EXISTS bank_lines (
  line_key TEXT PRIMARY KEY, source_file TEXT, line_no INTEGER, date TEXT, description TEXT, amount_inr REAL,
  result TEXT, payee TEXT, account TEXT, reason TEXT, evidence_json TEXT, payment_key TEXT, at REAL)"""


def clean_descriptor(d):
    s = PREFIXES.sub("", d.strip())
    toks = [t for t in re.split(r"[\s\*/]+", s.upper()) if t and t not in CITY]
    core = [t for t in toks if t not in TRAIL]
    return " ".join(core or toks)


def line_key(date, desc, amt, occurrence):
    """Content key: the n-th identical line of a statement is always the same bank line."""
    d = re.sub(r"\s+", " ", desc.strip().upper())
    raw = f"{date.strip().lower()}|{d}|{amt:.2f}|{occurrence}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


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


def legal_name_from_site(job_id, brand, domain):
    """Fetch the company's own website and take the legal name it states (footer, terms).
    Accepted only if exactly one distinct '<Brand> ... Limited' name appears on the page."""
    if not domain:
        return None
    resp, meta = crust.call(job_id, "/web/enrich/live", {"urls": [f"https://{domain}"]})
    pages = [p for p in pipeline.first_list(resp) if p.get("success")]
    if not pages:
        return None
    page = pages[0]
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page.get("content") or "")))
    brand_toks = set(pipeline.norm_tokens(brand))
    found = {}
    for m in LEGAL_RE.finditer(text):
        words = re.sub(r"\s+", " ", m.group(1)).strip().split(" ")
        # drop leading words before the brand ("Copyright Swiggy Limited" -> "Swiggy Limited");
        # the legal name must start with the brand, so "Bundl Technologies Private Limited" is never taken for Swiggy
        while words and not (set(pipeline.norm_tokens(words[0])) & brand_toks):
            words.pop(0)
        if len(words) >= 2:
            found.setdefault(" ".join(words), text[max(0, m.start() - 80): m.end() + 40].strip())
    if len(found) != 1:
        return None
    name, passage = next(iter(found.items()))
    return {"name": name, "source": {"type": "web", "url": page.get("url") or f"https://{domain}", "field": "page text",
                                     "passage": passage, "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta["retrieved_at"]))}}


def resolve_payee(job_id, desc):
    core = clean_descriptor(desc)
    # 1. client's own vendor master
    for v in ap.vendors().values():
        names = [v["legal_name"]] + v.get("aliases", [])
        if any(pipeline.name_similarity(core, n) >= 0.5 for n in names):
            return {"payee": v["legal_name"], "vendor_id": v["vendor_id"], "via": "vendor master",
                    "evidence": [{"source": "vendor master", "field": "legal_name", "value": v["legal_name"]}]}
    # 2. live: Crustdata Company Identify by name, accepted only if exactly one strong, exact-name match
    if len(core) < 3:
        return {"payee": None, "reason": f"descriptor '{desc}' has no usable name"}
    resp, meta = crust.call(job_id, "/company/identify", {"names": [core]})
    rows = pipeline.first_list(resp)
    ms = rows[0].get("matches", []) if rows else []
    strong = [m for m in ms if m.get("confidence_score", 0) >= 0.95
              and pipeline.name_similarity(core, (m.get("company_data") or {}).get("basic_info", {}).get("name", "")) >= 0.99]
    pick, via = None, None
    if len(strong) == 1:
        pick, via = strong[0], "crustdata"
    elif len(strong) > 1:
        # brand rule: card/UPI descriptors name established merchants. Accept only if exactly one strong match
        # owns the domain named after the descriptor (swiggy -> swiggy.com) and is an established company.
        label = re.sub(r"[^a-z0-9]", "", core.lower())
        brand = [m for m in strong if (m["company_data"]["basic_info"].get("primary_domain") or "").split(".")[0] == label
                 and m["company_data"]["basic_info"].get("employee_count_range") in ("1001-5000", "5001-10000", "10001+")]
        if len(brand) == 1:
            bi = brand[0]["company_data"]["basic_info"]
            pick, via = brand[0], f"crustdata (only one of {len(strong)} same-name records owns {bi.get('primary_domain')} and is an established company)"
        else:
            return {"payee": None, "reason": f"'{core}' matches {len(strong)} different companies equally well; no source identifies which one was paid"}
    if not pick:
        return {"payee": None, "reason": f"no source identifies a company called '{core}'"}
    bi = pick["company_data"]["basic_info"]
    evidence = [crust.source_ref(meta, "matches[].company_data.basic_info.{name,primary_domain,employee_count_range}")
                | {"value": bi.get("name"), "domain": bi.get("primary_domain"), "employees": bi.get("employee_count_range")}]
    payee = bi.get("name")
    try:
        legal = legal_name_from_site(job_id, payee, bi.get("primary_domain"))
    except (crust.ApiError, crust.BudgetExceeded, OSError, RuntimeError):
        legal = None
    if legal:
        payee = legal["name"]
        evidence.append(legal["source"] | {"claim": "legal name", "value": legal["name"]})
        via += f"; legal name as stated on {bi.get('primary_domain')}"
    else:
        via += "; legal name not confirmed (the website does not state one), trading name used"
    return {"payee": payee, "vendor_id": None, "via": via, "evidence": evidence}


def unmatched_payment(vendor_id, amt):
    """A payment this desk made to the vendor for this amount that no bank line has claimed yet."""
    return store.db().execute(
        """SELECT * FROM payments WHERE vendor_id=? AND ABS(amount_inr-?)<0.5
           AND idempotency_key NOT IN (SELECT payment_key FROM bank_lines WHERE payment_key IS NOT NULL)
           ORDER BY paid_at LIMIT 1""", (vendor_id, amt)).fetchone()


def decide(job_id, date, desc, amt):
    try:
        r = resolve_payee(job_id, desc)
    except crust.BudgetExceeded as e:
        r = {"payee": None, "reason": f"{e}; stopped rather than guess"}
    except (crust.ApiError, OSError, RuntimeError) as e:
        r = {"payee": None, "reason": f"source unavailable ({e}); not the same as no record"}
    if not r.get("payee"):
        return {"result": "CANNOT_VERIFY", "payee": None, "account": None, "reason": r["reason"] + ". Held for a person.", "evidence": []}
    acct = account_for(r["payee"])
    res = {"result": "POSTED" if acct else "HELD", "payee": r["payee"], "account": acct,
           "reason": f"payee from {r['via']}" + ("" if acct else "; no ledger account mapped for this payee, held for a person"),
           "evidence": r["evidence"]}
    if r.get("vendor_id") and TRANSFER.match(desc.strip()):
        p = unmatched_payment(r["vendor_id"], amt)
        if p:
            res["payment_key"] = p["idempotency_key"]
            res["reason"] += f"; matches payment {p['ref']} for invoice {p['invoice_key']}, approved by {p['approved_by']}"
            res["evidence"].append({"source": "payments ledger", "field": "ref", "value": p["ref"], "invoice": p["invoice_key"], "approved_by": p["approved_by"]})
        else:
            res["result"] = "HELD"
            res["reason"] += (f"; ₹{amt:,.2f} left the account to {r['payee']} but no approved payment on our ledger matches it. "
                              "Possible unauthorised or duplicate payment, held for a person")
    return res


def post_file(path):
    store.db().execute(SCHEMA)
    base = "bank:" + os.path.basename(path)
    out, seen, occ = [], [], {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, 1):
        date, desc = row["date"].strip(), row["description"].strip()
        amt = float(str(row["amount"]).replace(",", "").replace("₹", "").strip())
        ident = (date.lower(), re.sub(r"\s+", " ", desc.upper()), round(amt, 2))
        occ[ident] = occ.get(ident, 0) + 1
        key = line_key(date, desc, amt, occ[ident])
        sig = (date, clean_descriptor(desc), amt)
        prev = store.db().execute("SELECT * FROM bank_lines WHERE line_key=?", (key,)).fetchone()
        if prev:
            out.append(dict(prev) | {"line": i, "note": "already posted; not posted again"})
            seen.append(sig)
            continue
        if sig in seen:
            res = {"result": "HELD", "payee": None, "account": None,
                   "reason": "Same descriptor, amount and date as an earlier line: held for approval as a possible duplicate charge", "evidence": []}
        else:
            # one job per line: the cost limit applies to each line, and repeated merchants are answered from cache
            res = decide(f"{base}:{i}", date, desc, amt)
        seen.append(sig)
        with store._lock:
            store.db().execute("INSERT OR IGNORE INTO bank_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (key, path, i, date, desc, amt, res["result"], res["payee"], res["account"], res["reason"],
                                json.dumps(res["evidence"]), res.get("payment_key"), time.time()))
        store.log(base, f"line {i} {desc} ₹{amt:,.0f}: {res['result']} {res['payee'] or ''} {res['account'] or ''}")
        out.append({"line": i, "date": date, "description": desc, "amount_inr": amt, **res})
    led = store.db().execute("SELECT COALESCE(SUM(credits),0) AS c, COALESCE(SUM(1-cache_hit),0) AS n FROM ledger WHERE job_id LIKE ?",
                             (base + ":%",)).fetchone()
    return {"file": path, "lines": out, "cost": {"credits": round(led["c"], 3), "paid_calls": led["n"]}}
