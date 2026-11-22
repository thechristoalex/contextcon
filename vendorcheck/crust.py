"""Crustdata API client with cache, retries, backoff, and a per-call cost ledger."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from . import store
from .config import CONFIG

BASE = "https://api.crustdata.com"

# Fallback prices (credits) used only if the API omits the X-Credits-Used header.
FALLBACK_COST = {
    "/company/identify": 0.2,
    "/company/enrich": 2.0,
    "/web/search/live": 1.0,
    "/web/enrich/live": 1.0,
    "/person/enrich": 1.0,
}


class BudgetExceeded(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


def _key():
    k = os.environ.get("CRUSTDATA_API_KEY")
    if not k:
        raise RuntimeError("CRUSTDATA_API_KEY is not set (put it in .env)")
    return k


def call(job_id, endpoint, body, use_cache=True):
    """POST to Crustdata. Returns (response_json, meta). Cached by (endpoint, body)."""
    req_hash = hashlib.sha256((endpoint + json.dumps(body, sort_keys=True)).encode()).hexdigest()[:32]
    if use_cache:
        c = store.cache_get(req_hash)
        if c:
            store.ledger_add(job_id, endpoint, req_hash, True, 0.0, 0, c["status"])
            return json.loads(c["response_json"]), {"retrieved_at": c["retrieved_at"], "cache_hit": True, "endpoint": endpoint, "req_hash": req_hash}

    spent = store.job_credits(job_id) if job_id else 0
    est = FALLBACK_COST.get(endpoint, 1.0)
    if job_id and spent + est > CONFIG["max_credits_per_job"]:
        raise BudgetExceeded(f"cost limit {CONFIG['max_credits_per_job']} credits reached (spent {spent:.2f})")

    data = json.dumps(body).encode()
    last_err = None
    for attempt in range(CONFIG["max_retries"] + 1):
        req = urllib.request.Request(BASE + endpoint, data=data, method="POST", headers={
            "authorization": f"Bearer {_key()}",
            "content-type": "application/json",
            "x-api-version": "2025-11-01",
        })
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=CONFIG["http_timeout_s"]) as resp:
                raw = resp.read()
                status = resp.status
                credits = resp.headers.get("X-Credits-Used")
                credits = float(credits) if credits not in (None, "") else est
                out = json.loads(raw or b"null")
                ms = int((time.time() - t0) * 1000)
                store.cache_put(req_hash, endpoint, body, status, out, credits)
                store.ledger_add(job_id, endpoint, req_hash, False, credits, ms, status)
                return out, {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": req_hash}
        except urllib.error.HTTPError as e:
            ms = int((time.time() - t0) * 1000)
            try:
                err_body = json.loads(e.read() or b"{}")
            except Exception:
                err_body = {}
            store.ledger_add(job_id, endpoint, req_hash, False, 0.0, ms, e.code)
            if e.code == 404:
                # "no match" is a valid answer, cache it so a restart doesn't re-ask
                store.cache_put(req_hash, endpoint, body, 404, err_body, 0.0)
                return err_body, {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": req_hash, "not_found": True}
            if e.code in (429, 500, 502, 503, 504):
                reset = e.headers.get("x-ratelimit-reset") if e.headers else None
                wait = min(float(reset), 30) if reset and reset.replace(".", "").isdigit() else (2 ** attempt)
                store.log(job_id, f"{endpoint} -> {e.code}, retry {attempt + 1} in {wait:.0f}s", "warn")
                time.sleep(wait)
                last_err = ApiError(e.code, err_body)
                continue
            raise ApiError(e.code, err_body)
        except (urllib.error.URLError, TimeoutError) as e:
            store.log(job_id, f"{endpoint} network error {e}, retry {attempt + 1}", "warn")
            time.sleep(2 ** attempt)
            last_err = e
    raise last_err


def source_ref(meta, field, record_date=None, url=None):
    """A citation for a fact that came from an API response."""
    return {
        "type": "api",
        "api": f"Crustdata POST {meta['endpoint']}",
        "url": url,
        "field": field,
        "record_date": record_date,
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta["retrieved_at"])),
        "request_hash": meta["req_hash"],
    }
