#!/usr/bin/env python3
"""Minimal Crustdata API client (stdlib only).

- Sends the required headers (Bearer auth, x-api-version 2025-11-01, JSON).
- Retries 429 after x-ratelimit-reset / Retry-After seconds (+ jitter).
- Retries 5xx with backoff only for read-style calls (never for creates under
  /batch, /watch, /agent, which could run twice).
- Raises immediately on 400/401/402/403: retrying those cannot succeed.
- Appends one JSON line per HTTP attempt to a cost ledger: endpoint, status,
  x-credits-used, x-request-id, latency. The body is hashed, not stored, so
  personal data never lands in the ledger.

Library use:
    from crustdata_client import CrustdataClient
    cd = CrustdataClient(ledger_path="logs/crustdata_ledger.jsonl")
    rows = cd.post("/company/identify", {"domains": ["retool.com"]})
    for company in cd.paginate("/company/search", body, "companies"): ...
    print(cd.credits_spent)

CLI use:
    export CRUSTDATA_API_KEY=...
    python3 crustdata_client.py GET /account/credits
    python3 crustdata_client.py POST /company/identify '{"domains":["retool.com"]}'
    python3 crustdata_client.py --ledger ledger.jsonl POST /web/search/live '{"query":"x","sources":["news"]}'
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("CRUSTDATA_BASE_URL", "https://api.crustdata.com")
API_VERSION = "2025-11-01"
NON_IDEMPOTENT_PREFIXES = ("/batch/", "/watch/", "/agent/")


class CrustdataError(Exception):
    def __init__(self, status, body, request_id=None):
        self.status, self.body, self.request_id = status, body, request_id
        super().__init__(f"HTTP {status}: {_error_message(body)} (request_id={request_id})")


class BadRequest(CrustdataError): pass            # 400: fix the request
class AuthError(CrustdataError): pass             # 401: bad/missing key
class InsufficientCredits(CrustdataError): pass   # 402: stop, alert a human
class PermissionDenied(CrustdataError): pass      # 403: endpoint or field not enabled
class RateLimited(CrustdataError): pass           # 429 after retries exhausted
class ServerError(CrustdataError): pass           # 5xx after retries exhausted


def _error_message(body):
    """Error bodies come in several shapes; normalise to one string."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return f"{err.get('type')}: {err.get('message')}"
        if err:
            return f"{err}: {body.get('description') or body.get('reason') or ''}".strip()
        if body.get("message"):
            return str(body["message"])
    return str(body)[:500]


def _key_from_dotenv(start=None):
    """Find CRUSTDATA_API_KEY in the nearest .env at or above the working directory."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        path = os.path.join(d, ".env")
        if os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    k, sep, v = line.strip().partition("=")
                    if sep and k.strip().removeprefix("export ").strip() == "CRUSTDATA_API_KEY":
                        return v.strip().strip("'\"")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


class CrustdataClient:
    def __init__(self, api_key=None, ledger_path=None, max_retries=4, timeout=120):
        self.api_key = api_key or os.environ.get("CRUSTDATA_API_KEY") or _key_from_dotenv()
        if not self.api_key:
            raise AuthError(401, {"message": "Set CRUSTDATA_API_KEY (env var or .env file)"})
        self.ledger_path = ledger_path
        self.max_retries = max_retries
        self.timeout = timeout
        self.credits_spent = 0.0
        self.last_headers = {}  # lower-cased headers of the last response (x-request-id, x-credits-used, …)

    # -- public API -------------------------------------------------------
    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body=None):
        return self.request("POST", path, body=body if body is not None else {})

    def paginate(self, path, body, items_key):
        """Yield items across cursor pages, keeping the body otherwise unchanged
        (changing filters/sorts/search between pages invalidates the cursor)."""
        body = dict(body)
        body.pop("cursor", None)
        while True:
            page = self.post(path, body)
            yield from page.get(items_key) or []
            cursor = page.get("next_cursor")
            if not cursor:
                return
            body["cursor"] = cursor

    def request(self, method, path, body=None, params=None):
        url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "x-api-version": API_VERSION,
            "accept": "application/json",
        }
        if data is not None:
            headers["content-type"] = "application/json"
        retry_5xx = not (method == "POST" and path.startswith(NON_IDEMPOTENT_PREFIXES))

        for attempt in range(self.max_retries + 1):
            started = time.time()
            status, resp_headers, payload = self._send(method, url, data, headers)
            self.last_headers = resp_headers
            self._record(method, path, data, status, resp_headers, started, attempt)
            request_id = resp_headers.get("x-request-id")

            if 200 <= status < 300:
                return payload
            if status == 429 and attempt < self.max_retries:
                wait = _num(resp_headers.get("retry-after")) or _num(resp_headers.get("x-ratelimit-reset")) or 2 ** attempt
                time.sleep(wait + random.uniform(0, 1))
                continue
            if status >= 500 and retry_5xx and attempt < self.max_retries:
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            raise _ERRORS.get(status, ServerError if status >= 500 else CrustdataError)(status, payload, request_id)

    # -- internals --------------------------------------------------------
    def _send(self, method, url, data, headers):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _lower(resp.headers), _parse(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _lower(e.headers), _parse(e.read())

    def _record(self, method, path, data, status, headers, started, attempt):
        credits = _num(headers.get("x-credits-used")) or 0.0
        if 200 <= status < 300:
            self.credits_spent += credits
        if not self.ledger_path:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": method,
            "path": path,
            "status": status,
            "credits_used": credits,
            "request_id": headers.get("x-request-id"),
            "ratelimit_remaining": headers.get("x-ratelimit-remaining"),
            "latency_ms": int((time.time() - started) * 1000),
            "attempt": attempt,
            "body_sha256": hashlib.sha256(data).hexdigest()[:16] if data else None,
        }
        os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)), exist_ok=True)
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(entry) + "\n")


_ERRORS = {400: BadRequest, 401: AuthError, 402: InsufficientCredits, 403: PermissionDenied, 429: RateLimited}


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()} if headers else {}


def _parse(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw.decode(errors="replace")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main():
    p = argparse.ArgumentParser(description="Call the Crustdata API with retries and a cost ledger.")
    p.add_argument("method", choices=["GET", "POST", "PATCH", "DELETE"])
    p.add_argument("path", help="e.g. /company/identify or /account/endpoints")
    p.add_argument("body", nargs="?", help="JSON body (POST/PATCH) or query string for GET, e.g. 'path=/company/enrich'")
    p.add_argument("--ledger", default=os.environ.get("CRUSTDATA_LEDGER"), help="JSONL cost ledger path")
    args = p.parse_args()

    client = CrustdataClient(ledger_path=args.ledger)
    try:
        if args.method == "GET":
            params = dict(urllib.parse.parse_qsl(args.body)) if args.body else None
            out = client.get(args.path, params)
        else:
            out = client.request(args.method, args.path, body=json.loads(args.body) if args.body else None)
    except CrustdataError as e:
        print(json.dumps({"status": e.status, "request_id": e.request_id, "error": e.body}, indent=2), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(out, indent=2))
    print(f"credits_used={client.credits_spent}", file=sys.stderr)


if __name__ == "__main__":
    main()
