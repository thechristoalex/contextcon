"""Jev, TypeSafe AI's decision model: typed yes/no, choice and score answers about text we already hold.

Jev is never a source. It answers questions about a document or a fetched page, and that text keeps
its own citation. Rules turn Jev's answers into checks through thresholds in config. A Jev answer can
add a hold or a flag, or point at the sentence to quote, but it never clears anything a rule raised.

Every call is pinned to one model version, cached by request hash (a re-run gives the same answer and
costs nothing), written to the ledger with 0 Crustdata credits, and logged with Jev's request id.
If Jev is turned off, has no key, or still fails after retries, `ask` raises Unavailable and the caller
keeps its rule: no job ever waits on Jev.
"""
import hashlib
import json
import os
import re
import textwrap
import time
import urllib.error
import urllib.request

from . import store
from .config import CONFIG

URL = "https://api.typesafe.ai/v1/systemone"
ENDPOINT = "jev:/v1/systemone"


class Unavailable(Exception):
    pass


# ---------- data minimisation: Jev never sees tax IDs, bank or phone numbers, or email addresses ----------

_REDACT = [
    (re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b", re.I), "[GSTIN]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", re.I), "[IFSC]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\+?\d[\d -]{7,}\d"), "[number]"),  # bank accounts, phones, Aadhaar
]


def redact(text):
    for pat, rep in _REDACT:
        text = pat.sub(rep, text or "")
    return text


# ---------- the call ----------

def ask(job_id, state, questions):
    """Ask Jev `questions` about `state`. Returns (answers, meta). Raises Unavailable."""
    if not CONFIG["jev_enabled"]:
        raise _unavailable(job_id, "turned off in settings")
    model = CONFIG["jev_model"]
    req_hash = hashlib.sha256((model + json.dumps(state, sort_keys=True) + json.dumps(questions, sort_keys=True)).encode()).hexdigest()[:32]
    c = store.cache_get(req_hash)
    if c:
        out = json.loads(c["response_json"])
        store.ledger_add(job_id, ENDPOINT, req_hash, True, 0.0, 0, c["status"])
        return out["answers"], _meta(out, req_hash, True)
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise _unavailable(job_id, "TYPESAFE_API_KEY not set")
    body = {"model": model, "state": state, "questions": questions}
    data = json.dumps(body).encode()
    for attempt in range(CONFIG["jev_retries"] + 1):
        req = urllib.request.Request(URL, data=data, method="POST",
                                     headers={"authorization": f"Bearer {key}", "content-type": "application/json"})
        t0 = time.time()
        backoff = min(5.0, 0.5 * 2 ** attempt)
        try:
            with urllib.request.urlopen(req, timeout=CONFIG["jev_timeout_s"]) as r:
                out = json.loads(r.read())
                out["_request_id"] = r.headers.get("x-typesafe-request-id")
            if not isinstance(out.get("answers"), dict) or set(out["answers"]) != set(questions):
                raise _unavailable(job_id, "response did not answer every question")
            ms = int((time.time() - t0) * 1000)
            store.cache_put(req_hash, ENDPOINT, body, 200, out, 0.0)
            store.ledger_add(job_id, ENDPOINT, req_hash, False, 0.0, ms, 200)
            store.log(job_id, f"jev {out.get('model')} {out['_request_id']}: {len(questions)} question(s), "
                              f"{(out.get('usage') or {}).get('input_tokens')} input tokens, {ms} ms")
            return out["answers"], _meta(out, req_hash, False)
        except urllib.error.HTTPError as e:
            store.ledger_add(job_id, ENDPOINT, req_hash, False, 0.0, int((time.time() - t0) * 1000), e.code)
            if e.code in (429, 529) or e.code >= 500:
                ra = (e.headers.get("retry-after") or "") if e.headers else ""
                wait = min(float(ra), 5.0) if ra.replace(".", "").isdigit() else backoff
                store.log(job_id, f"jev -> {e.code}, retry {attempt + 1} in {wait:.1f}s", "warn")
                time.sleep(wait)
                continue
            # 401: bad key. 400/422: a malformed question (a bug on our side). Neither gets better by retrying.
            raise _unavailable(job_id, f"HTTP {e.code} {e.read()[:160]!r}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            store.log(job_id, f"jev network error {e}, retry {attempt + 1}", "warn")
            time.sleep(backoff)
        except ValueError:
            raise _unavailable(job_id, "response was not JSON")
    raise _unavailable(job_id, "retries exhausted")


def _unavailable(job_id, reason):
    store.log(job_id, f"jev unavailable ({reason}); fell back to rule", "warn")
    return Unavailable(reason)


def _meta(out, req_hash, cache_hit):
    return {"model": out.get("model"), "request_id": out.get("_request_id"), "req_hash": req_hash, "cache_hit": cache_hit}


# ---------- reading answers: thresholds live in config, so a live fix is a settings change ----------

def yes(a):
    """noul -> True, False, or None for 'cannot tell'. A 0.5 means 'cannot tell', not 'medium'."""
    p = a.get("noul")
    if p is None:
        return None
    return True if p >= CONFIG["jev_yes"] else False if p <= CONFIG["jev_no"] else None


def pick(a):
    """choice -> the option, or None when Jev is not confident enough."""
    return a.get("choice") if a.get("confidence", 0) >= CONFIG["jev_choice_min"] else None


def level(a):
    """score -> index of the most likely level, or None unless that level clears the bar. A level is not a measurement."""
    probs = a.get("probabilities") or {}
    if not probs:
        return None
    k, p = max(probs.items(), key=lambda kv: kv[1])
    return int(k) if p >= CONFIG["jev_score_min"] else None


def describe(a):
    """One short human-readable line for an answer."""
    if a.get("type") == "noul":
        p = a["noul"]
        return f"{'yes' if yes(a) else 'no' if yes(a) is False else 'cannot tell'} ({p:.2f})"
    if a.get("type") == "score":
        k, p = max((a.get("probabilities") or {"0": 0}).items(), key=lambda kv: kv[1])
        return f"level {k} of {len(a.get('legend') or {}) - 1}: {(a.get('legend') or {}).get(k, '')} ({p:.2f})"
    return f"{a.get('choice')} ({a.get('confidence', 0):.2f})"


def evidence(meta, question, a):
    """How a Jev decision appears next to a check. It is a decision about the text, not a source."""
    return {"jev": True, "source": f"Jev {meta['model']}", "field": question, "value": describe(a),
            "request_id": meta["request_id"], "cache_hit": meta["cache_hit"]}


# ---------- citations: Jev points at a sentence by id, code quotes it verbatim ----------

def sentences(text, limit=40):
    """Candidate sentences, split in code so a quote is always text that is really in the document."""
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\s*\n\s*", text or ""):
        for part in textwrap.wrap(s.strip(), 280) if len(s) > 280 else [s.strip()]:
            if len(part) >= 12 and part not in out:
                out.append(part)
    return out[:limit]


def quote_question(instructions, sents):
    crit = {f"s{i}": redact(s) for i, s in enumerate(sents)}
    crit["none"] = "None of the sentences does this"
    return {"type": "choice", "instructions": instructions + " Choose the sentence that does, or none.", "criteria": crit}


def quoted(a, sents, text):
    """The sentence Jev picked, verbatim, if it really appears in `text`; else None."""
    k = pick(a)
    if not k or not k.startswith("s") or not k[1:].isdigit():
        return None
    i = int(k[1:])
    s = sents[i] if i < len(sents) else None
    return s if s and s in (text or "") else None
