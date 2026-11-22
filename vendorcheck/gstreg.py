"""GST registry lookup: is this GSTIN registered, active, and in whose name.

The GST portal (services.gst.gov.in) has no public API: its search sits behind a captcha and its internal
endpoints reject scripted calls. GSTN data is served to software through GST Suvidha Providers and resellers.
We use gstin.cloud (GET /api/v1/verify/{GSTIN}, data from GSTN): Google sign-in issues a key with 10 free
credits and a sandbox key, then about Rs 0.10-0.20 a lookup. Its terms allow scripted access only through
this API, and forbid reselling the data.

Set GSTIN_API_KEY in .env. With no key the lookup is off and every caller behaves as before.
A sandbox key returns sample data: we report it and never decide on it.
lookup() never raises. Answers are cached (http_cache) for 7 days, a "not registered" answer included,
so a re-run or restart does not pay again.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store

TTL_S = 7 * 86400
ENDPOINT = "/verify"


def _base():
    return os.environ.get("GSTIN_API_BASE", "https://gstin.cloud/api/v1").rstrip("/")


def enabled():
    return bool(os.environ.get("GSTIN_API_KEY", "").strip())


def _source(retrieved_at, cache_hit):
    return {"type": "api", "api": "gstin.cloud GET /api/v1/verify (GSTN registration data)", "field": "status, legal_name",
            "record_date": None, "cache_hit": cache_hit,
            "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(retrieved_at))}


def _shape(status, body, retrieved_at, cache_hit, gstin):
    src = _source(retrieved_at, cache_hit)
    if status == 404:
        return {"ok": True, "registered": False, "gstin": gstin, "source": src}
    d = (body or {}).get("data") or {}
    if (body or {}).get("sandbox"):
        return {"ok": False, "reason": "GST registry key is a sandbox key: it returns sample data, which is not used to decide"}
    if (d.get("gstin") or "").upper() != gstin:
        return {"ok": False, "reason": "GST registry answered for a different GSTIN; answer not used"}
    status_txt = (d.get("status") or "").strip()
    return {"ok": True, "registered": True, "gstin": gstin, "active": status_txt.lower() == "active", "status": status_txt or "unknown",
            "legal_name": d.get("legal_name"), "trade_name": d.get("trade_name"), "constitution": d.get("constitution"),
            "taxpayer_type": d.get("taxpayer_type"), "registration_date": d.get("registration_date"),
            "cancellation_date": d.get("cancellation_date"), "state": d.get("state"), "source": src}


def lookup(gstin, job_id=None):
    """{"ok": True, "registered": bool, "active", "status", "legal_name", ..., "source"} or {"ok": False, "reason"}."""
    if not enabled():
        return {"ok": False, "off": True, "reason": "GST registry lookup not set up (GSTIN_API_KEY)"}
    gstin = (gstin or "").upper().strip()
    req_hash = "gstreg:" + hashlib.sha256(gstin.encode()).hexdigest()[:24]
    c = store.cache_get(req_hash)
    if c and time.time() - c["retrieved_at"] < TTL_S:
        return _shape(c["status"], json.loads(c["response_json"]), c["retrieved_at"], True, gstin)

    url = f"{_base()}{ENDPOINT}/{urllib.parse.quote(gstin)}"
    req = urllib.request.Request(url, headers={"X-API-Key": os.environ["GSTIN_API_KEY"].strip(), "Accept": "application/json",
                                               "User-Agent": "payables-desk/1.0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, body = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
    except Exception as e:  # network, timeout, bad JSON
        store.log(job_id, f"GST registry lookup for {gstin} failed: {e}", "warn")
        return {"ok": False, "reason": f"GST registry not reachable ({type(e).__name__})"}
    ms = int((time.time() - t0) * 1000)
    store.ledger_add(job_id, "gstin.cloud" + ENDPOINT, req_hash, False, 0.0, ms, status)
    if status == 200 and (body or {}).get("success", True) and not (body or {}).get("sandbox"):
        store.cache_put(req_hash, "gstin.cloud" + ENDPOINT, {"gstin": gstin}, 200, body, 0.0)
    elif status == 404:
        store.cache_put(req_hash, "gstin.cloud" + ENDPOINT, {"gstin": gstin}, 404, body, 0.0)
    elif status != 200:
        why = {401: "key rejected", 402: "out of credits", 403: "key not allowed", 422: "GSTIN refused as malformed",
               429: "rate limit reached"}.get(status, f"HTTP {status}")
        store.log(job_id, f"GST registry lookup for {gstin}: {why}", "warn")
        return {"ok": False, "reason": f"GST registry not checked: {why}"}
    return _shape(status, body, time.time(), False, gstin)


def name_matches(reg, *names, trade=True):
    """Best name similarity between the registered legal name (and trade name, if trade) and any name given (0..1)."""
    from .pipeline import name_similarity
    regs = [n for n in (reg.get("legal_name"), reg.get("trade_name") if trade else None) if n]
    return max((name_similarity(a, b) for a in regs for b in names if b), default=0.0)
