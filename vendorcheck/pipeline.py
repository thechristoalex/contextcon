"""Vendor / carrier check pipeline.

Request -> resolve entity -> company profile -> website check -> registry IDs ->
adverse media (company + directors, namesakes cleared) -> verdict -> approval gate.

Each step is checkpointed; a restarted job skips finished steps and re-uses cached
API responses, so nothing is paid for or done twice.
"""
import hashlib
import json
import re
import time
import urllib.parse
from datetime import datetime

from . import crust, store
from .config import ADVERSE_TERMS, CONFIG

SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "corp", "corporation",
            "the", "and", "of", "india", "opc", "plc", "llc", "gmbh", "sa", "sarl", "pte"}
CIN_RE = re.compile(r"\b([LU])(\d{5})([A-Z]{2})(\d{4})(PLC|PTC|OPC|NPL|FTC|GOI|SGC|ULL|ULT|GAP|GAT)(\d{6})\b")
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
GST_STATES = {"01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
              "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
              "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
              "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
              "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
              "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
              "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory"}


# ---------------- helpers ----------------

def norm_tokens(name):
    t = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    return [w for w in t if w not in SUFFIXES]


def name_similarity(a, b):
    ta, tb = set(norm_tokens(a)), set(norm_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def norm_domain(d):
    if not d:
        return None
    d = d.strip().lower()
    if "://" not in d:
        d = "http://" + d
    host = urllib.parse.urlparse(d).hostname or ""
    return host[4:] if host.startswith("www.") else host


def gstin_valid(g):
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    g = g.upper()
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g):
        return False
    s = 0
    for i, ch in enumerate(g[:14]):
        p = chars.index(ch) * (2 if i % 2 else 1)
        s += p // 36 + p % 36
    return chars[(36 - s % 36) % 36] == g[14]


def job_id_for(req):
    if req.get("request_id"):
        return str(req["request_id"])
    key = json.dumps({k: (str(v).strip().lower() if v else v) for k, v in sorted(req.items())}, sort_keys=True)
    return "vc_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def first_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for v in x.values():
            if isinstance(v, list):
                return v
    return []


def is_redirect(url):
    return bool(url) and ("google.com/goto" in url or "google.com/url?" in url)


def resolve_redirect(url):
    """Search results come as google.com/goto?url=... links. Follow them to the real page so the citation is the record itself."""
    if not url or "google.com/goto" not in url:
        return url
    import urllib.request
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.geturl()
    except Exception:
        return url


# ---------------- the job ----------------

class Job:
    def __init__(self, request):
        self.req = {k: v for k, v in request.items() if v not in (None, "")}
        self.id = job_id_for(self.req)
        self.t0 = time.time()
        self.facts = []      # {claim, value, source}
        self.flags = []      # {severity: red|amber, reason, source}
        self.missing = []    # what could not be verified and why

    # each step: run once, checkpoint result
    def step(self, name, fn):
        done = store.get_step(self.id, name)
        if done is not None:
            store.log(self.id, f"step {name}: already done, skipping")
            return done
        if time.time() - self.t0 > CONFIG["max_seconds_per_job"]:
            raise TimeoutError(f"time limit {CONFIG['max_seconds_per_job']}s reached before step {name}")
        store.log(self.id, f"step {name}: running")
        out = fn()
        store.save_step(self.id, name, out)
        return out

    def fact(self, claim, value, source):
        self.facts.append({"claim": claim, "value": value, "source": source})

    def flag(self, severity, reason, source=None):
        self.flags.append({"severity": severity, "reason": reason, "source": source})

    # ---------- step 1: resolve the legal entity ----------
    def s_resolve(self):
        name = self.req.get("vendor_name")
        domain = norm_domain(self.req.get("website"))
        out = {"candidates": [], "chosen": None, "reason": None, "via": None}
        if domain:
            resp, meta = crust.call(self.id, "/company/identify", {"domains": [domain]})
            matches = (first_list(resp)[0].get("matches", []) if first_list(resp) else [])
            cands = [self._cand(m) for m in matches]
            out["candidates"] = cands
            exact = [c for c in cands if domain in c["domains"]]
            out["via"] = "domain"
            out["meta"] = meta
            if len(exact) == 1:
                out["chosen"] = exact[0]
            elif len(exact) > 1:
                # several records share the domain (duplicates / branch pages). Prefer the one whose name matches.
                ranked = sorted(exact, key=lambda c: (name_similarity(name, c["name"]) if name else 0, c["confidence"]), reverse=True)
                out["chosen"] = ranked[0]
                out["duplicates"] = [c for c in ranked[1:]]
            else:
                out["reason"] = f"no company record carries the domain {domain}"
            return out
        # name only
        resp, meta = crust.call(self.id, "/company/identify", {"names": [name]})
        matches = (first_list(resp)[0].get("matches", []) if first_list(resp) else [])
        cands = [self._cand(m) for m in matches]
        out.update(candidates=cands, via="name", meta=meta)
        strong = [c for c in cands if c["confidence"] >= CONFIG["strong_match"] and name_similarity(name, c["name"]) >= 0.99]
        if len(strong) == 1:
            out["chosen"] = strong[0]
        elif len(strong) > 1:
            out["reason"] = (f"the name '{name}' matches {len(strong)} different companies equally well ("
                             + "; ".join(f"{c['name']} {c['domains'][0] if c['domains'] else '(no website)'}" for c in strong[:5])
                             + "). A website or registry number is needed to tell them apart")
        else:
            out["reason"] = f"no company record matches the name '{name}' exactly"
        return out

    @staticmethod
    def _cand(m):
        cd = m.get("company_data", m)
        bi = cd.get("basic_info", {})
        doms = [norm_domain(d) for d in ([bi.get("primary_domain")] + (bi.get("all_domains") or [])) if d]
        return {"id": cd.get("crustdata_company_id") or bi.get("crustdata_company_id"), "name": bi.get("name"),
                "domains": list(dict.fromkeys(doms)), "confidence": m.get("confidence_score", 0)}

    # ---------- step 2: company profile ----------
    def s_profile(self, cid):
        resp, meta = crust.call(self.id, "/company/enrich", {
            "crustdata_company_ids": [cid],
            "fields": ["basic_info", "headcount", "locations", "people", "news", "funding"]})
        row = first_list(resp)
        cd = row[0]["matches"][0]["company_data"] if row and row[0].get("matches") else {}
        return {"cd": cd, "meta": meta}

    # ---------- step 3: website ----------
    def s_website(self, url):
        resp, meta = crust.call(self.id, "/web/enrich/live", {"urls": [url]})
        page = first_list(resp)[0] if first_list(resp) else {}
        content = page.get("content") or ""
        text = re.sub(r"<[^>]+>", " ", content)
        text = re.sub(r"\s+", " ", text)
        return {"ok": bool(page.get("success")), "title": page.get("title"), "text": text[:20000], "url": page.get("url") or url, "meta": meta}

    # ---------- step 4: registry identifiers from the web ----------
    # ---------- page verification: never cite a search snippet or a redirect ----------
    def verify_pages(self, urls, must_terms):
        """Fetch direct URLs and keep only pages whose text states every required term.
        Returns {url: passage}. Redirect links (google.com/goto) cannot be fetched or cited and are skipped."""
        urls = [u for u in dict.fromkeys(urls) if u and not is_redirect(u)][:10]
        if not urls:
            return {}
        resp, meta = crust.call(self.id, "/web/enrich/live", {"urls": urls})
        out = {}
        for page in first_list(resp):
            if not page.get("success"):
                continue
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.get("content") or ""))
            low = text.lower()
            if all(t.lower() in low for t in must_terms):
                i = low.find(must_terms[0].lower())
                out[page.get("url")] = {"passage": text[max(0, i - 120): i + 160].strip(), "retrieved_at": meta["retrieved_at"], "title": page.get("title")}
        return out

    def s_registry(self, legal_name):
        q = f"\"{legal_name}\" CIN"
        resp, meta = crust.call(self.id, "/web/search/live", {"query": q, "location": "IN"})
        cands = []
        toks = norm_tokens(legal_name)
        for r in (resp or {}).get("results", [])[:10]:
            blob = f"{r.get('title', '')} {r.get('snippet', '')}"
            if not toks or not all(t in blob.lower() for t in toks):
                continue
            for m in CIN_RE.finditer(blob):
                cands.append({"kind": "CIN", "value": m.group(0), "url": r.get("url")})
        redirect_only = sum(1 for c in cands if is_redirect(c["url"]))
        hits = []
        for c in cands:
            if is_redirect(c["url"]):
                continue
            pages = self.verify_pages([c["url"]], [c["value"]])
            if pages:
                url, pg = next(iter(pages.items()))
                hits.append({**c, "url": url, "passage": pg["passage"], "retrieved_at": pg["retrieved_at"]})
        return {"query": q, "hits": hits, "candidates": len(cands), "redirect_only": redirect_only, "meta": meta}

    def s_gstin_lookup(self, gstin):
        resp, meta = crust.call(self.id, "/web/search/live", {"query": f"\"{gstin}\"", "location": "IN"})
        urls = [r.get("url") for r in (resp or {}).get("results", [])[:8] if gstin.upper() in f"{r.get('title', '')} {r.get('snippet', '')}".upper()]
        pages = self.verify_pages(urls, [gstin])
        return {"results": [{"url": u, "passage": p["passage"], "title": p["title"]} for u, p in pages.items()], "meta": meta}

    # ---------- step 5: adverse media ----------
    def s_adverse(self, query, must_terms):
        resp, meta = crust.call(self.id, "/web/search/live", {"query": query, "location": "IN"})
        cand, namesakes = [], []
        for r in (resp or {}).get("results", [])[:10]:
            blob = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
            terms = [t for t in ADVERSE_TERMS if re.search(r"\b" + re.escape(t) + r"\b", blob)]
            if not terms:
                continue
            if all(t.lower() in blob for t in must_terms):
                cand.append({"url": r.get("url"), "terms": terms, "title": r.get("title"), "date": r.get("metadata")})
            else:
                # data minimisation: about a different person, so keep no name, link or passage, only why it was cleared
                namesakes.append({"terms": terms, "reason": "article does not name the vendor company"})
        # an escalation needs the article itself, fetched, naming both the person and the company
        pages = self.verify_pages([c["url"] for c in cand], must_terms)
        hits, possible = [], []
        for c in cand:
            if c["url"] in pages:
                hits.append({**c, "passage": pages[c["url"]]["passage"], "retrieved_at": pages[c["url"]]["retrieved_at"]})
            else:
                possible.append({"terms": c["terms"], "reason": "search result names both, but the article could not be retrieved to confirm"})
        return {"query": query, "hits": hits, "possible": possible, "namesakes_cleared": namesakes, "meta": meta}

    def screening_metrics(self):
        found = cleared = escalated = 0
        for k in ("adverse_company",) + tuple(f"adverse_person_{i}" for i in range(CONFIG["max_people_screened"])):
            st = store.get_step(self.id, k)
            if st:
                found += len(st["hits"]) + len(st.get("possible", [])) + len(st["namesakes_cleared"])
                cleared += len(st["namesakes_cleared"])
                escalated += len(st["hits"]) + len(st.get("possible", []))
        return {"hits_found": found, "hits_cleared": cleared, "hits_escalated": escalated}

    # ---------------- orchestration ----------------
    def run(self):
        existing = store.get_job(self.id)
        if existing and existing["status"] == "done":
            store.log(self.id, "job already finished; returning stored report")
            return json.loads(existing["report_json"])
        store.upsert_job(self.id, self.req, "running")
        try:
            report = self._run()
            store.upsert_job(self.id, self.req, "done", report["verdict"], report)
            return report
        except (crust.BudgetExceeded, TimeoutError) as e:
            self.missing.append(f"stopped early: {e}")
            report = self._report("CANNOT_VERIFY", None, f"Run stopped at a limit: {e}")
            store.upsert_job(self.id, self.req, "done", report["verdict"], report)
            return report
        except Exception as e:
            store.log(self.id, f"failed: {e!r}", "error")
            store.upsert_job(self.id, self.req, "failed")
            raise

    def _run(self):
        if not self.req.get("vendor_name") and not self.req.get("website"):
            raise ValueError("request needs vendor_name or website")
        name = self.req.get("vendor_name") or ""

        # GSTIN format check needs no API
        gstin = (self.req.get("gstin") or "").upper() or None
        if gstin:
            if gstin_valid(gstin):
                self.fact("GSTIN format and checksum", f"{gstin} is well-formed; state code {gstin[:2]} = {GST_STATES.get(gstin[:2], 'unknown')}; PAN {gstin[2:12]}",
                          {"type": "computed", "field": "GSTIN check digit (15th character)", "record_date": None})
            else:
                self.flag("red", f"GSTIN {gstin} fails the format/checksum test, so it is not a real GST number",
                          {"type": "computed", "field": "GSTIN check digit"})

        res = self.step("resolve", self.s_resolve)
        if not res.get("chosen"):
            self.missing.append(res["reason"])
            return self._report("CANNOT_VERIFY", None, f"Could not identify one legal entity: {res['reason']}.", res)
        chosen = res["chosen"]
        rmeta = res["meta"]
        self.fact("Matched company record", f"{chosen['name']} (crustdata_company_id {chosen['id']})",
                  crust.source_ref(rmeta, f"matches[].company_data.basic_info.name via {res['via']}"))
        for d in res.get("duplicates", []):
            self.flag("amber", f"Duplicate record for the same domain: {d['name']} (id {d['id']}). Used the closest name match.",
                      crust.source_ref(rmeta, "matches[].company_data.basic_info"))
        if name and res["via"] == "domain":
            sim = name_similarity(name, chosen["name"])
            if sim < 0.5:
                self.flag("red", f"Name on request '{name}' does not match the company that owns the website ('{chosen['name']}'). Possible lookalike or impersonation.",
                          crust.source_ref(rmeta, "basic_info.name"))

        # ---- profile
        prof = self.step("profile", lambda: self.s_profile(chosen["id"]))
        cd, pmeta = prof["cd"], prof["meta"]
        if not cd:
            self.missing.append("company profile not available")
            return self._report("CANNOT_VERIFY", chosen, "No company profile was returned for the matched record.")
        upd = cd.get("updated_at")
        bi = cd.get("basic_info", {}) or {}
        src = lambda f: crust.source_ref(pmeta, f, record_date=upd)
        legal = bi.get("name") or chosen["name"]
        self.fact("Company name", legal, src("basic_info.name"))
        status = (bi.get("status") or "").lower()
        if status:
            self.fact("Crustdata record status (dataset status, not registry status)", status, src("basic_info.status"))
            if status not in ("active", "operating"):
                self.flag("red", f"Company status is '{status}', not active", src("basic_info.status"))
        else:
            self.missing.append("operating status not in record")
        if bi.get("company_type"):
            self.fact("Company type", bi["company_type"], src("basic_info.company_type"))
        yf = bi.get("year_founded")
        if yf:
            age = datetime.now().year - int(yf)
            self.fact("Year founded", f"{yf} ({age} years)", src("basic_info.year_founded"))
            if age < CONFIG["min_age_years"]:
                self.flag("amber", f"Newly formed: founded {yf}", src("basic_info.year_founded"))
        else:
            self.missing.append("year founded not in record")
        hc = (cd.get("headcount") or {}).get("total")
        if hc is not None:
            self.fact("Employees", f"{hc} (largest in {(cd.get('headcount') or {}).get('largest_headcount_country')})", src("headcount.total"))
            if hc < CONFIG["min_headcount"]:
                self.flag("amber", f"Very small headcount ({hc})", src("headcount.total"))
        else:
            self.missing.append("headcount not in record")
        loc = cd.get("locations") or {}
        if loc.get("headquarters") or loc.get("country"):
            self.fact("Headquarters", f"{loc.get('headquarters') or ''} ({loc.get('country') or '?'})", src("locations.headquarters"))
        if bi.get("website"):
            self.fact("Website on record", bi["website"], src("basic_info.website"))

        people = cd.get("people") or {}
        leaders = []
        for grp in ("founders", "cxos", "decision_makers"):
            for p in people.get(grp) or []:
                pb = p.get("basic_profile") or {}
                nm = pb.get("name")
                if nm and nm not in [l["name"] for l in leaders]:
                    leaders.append({"name": nm, "title": pb.get("current_title") or pb.get("headline"), "group": grp})
        if leaders:
            self.fact("Who runs it", "; ".join(f"{l['name']} ({l['title']})" for l in leaders[:4]), src("people.founders / people.cxos"))
        else:
            self.missing.append("no founders or executives on record")
        if self.req.get("director"):
            d = self.req["director"]
            if any(name_similarity(d, l["name"]) >= 0.6 for l in leaders):
                self.fact("Named director found on record", d, src("people"))
            else:
                self.flag("amber", f"Director named on the request ('{d}') is not among the company's recorded leaders", src("people"))

        # company news already in the profile
        for n in (cd.get("news") or [])[:20]:
            t = (n.get("article_title") or "").lower()
            terms = [a for a in ADVERSE_TERMS if re.search(r"\b" + re.escape(a) + r"\b", t)]
            if terms and any(tok in t for tok in norm_tokens(legal)):
                self.flag("red", f"Adverse news: \"{n.get('article_title')}\" ({', '.join(terms)})",
                          {"type": "web", "url": n.get("article_url"), "field": "article_title", "record_date": n.get("article_publish_date"),
                           "publisher": n.get("article_publisher_name")})

        # ---- website
        site = self.req.get("website") or bi.get("website")
        if site:
            url = site if "://" in site else "https://" + site
            w = self.step("website", lambda: self.s_website(url))
            wsrc = crust.source_ref(w["meta"], "page title / text", url=w["url"])
            if not w["ok"] or len(w["text"]) < 200:
                self.flag("amber", f"Website {url} did not load or has almost no content", wsrc)
            else:
                toks = norm_tokens(legal)
                if toks and any(t in (w["title"] or "").lower() + w["text"][:5000].lower() for t in toks):
                    self.fact("Website is live and names the company", f"title: {w['title']}", wsrc)
                else:
                    self.flag("amber", f"Website {url} is live but does not mention '{legal}'", wsrc)
                if re.search(r"domain (is )?for sale|buy this domain|parked", w["text"][:3000], re.I):
                    self.flag("red", "Website looks parked / for sale", wsrc)

        # ---- registry IDs
        reg = self.step("registry", lambda: self.s_registry(legal))
        cins = [h for h in reg["hits"] if h["kind"] == "CIN"]
        vals = sorted({h["value"] for h in cins})
        if len(vals) == 1:
            h = cins[0]
            self.fact("CIN stated on a secondary public listing (not the MCA registry)", h["value"],
                      {"type": "web", "url": h["url"], "field": "page text", "passage": h["passage"], "strength": "secondary",
                       "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h["retrieved_at"]))})
            listed = "public" in (bi.get("company_type") or "").lower()
            if listed and h["value"][0] == "U":
                self.missing.append(f"CIN {h['value']} on a secondary listing is an unlisted-company number but the company is publicly traded; the listing may be stale")
        elif len(vals) > 1:
            self.missing.append(f"registry not confirmed: listings state {len(vals)} different CINs for similarly named companies ({', '.join(vals[:4])}); an MCA lookup is needed")
        elif reg.get("redirect_only"):
            self.missing.append(f"registry not confirmed: {reg['redirect_only']} search result(s) mention a CIN, but only through redirect links that cannot be fetched and cited; MCA has no public API")
        else:
            self.missing.append("registry not confirmed: no fetchable public page states a CIN for this company; MCA has no public API")
        if gstin:
            g = self.step("gstin", lambda: self.s_gstin_lookup(gstin))
            if g["results"]:
                r0 = g["results"][0]
                if any(t in (r0["passage"] or "").lower() for t in norm_tokens(legal)):
                    self.fact("GSTIN stated on a public page with this company's name", gstin,
                              {"type": "web", "url": r0["url"], "field": "page text", "passage": r0["passage"]})
                else:
                    self.flag("red", f"GSTIN {gstin} is stated on a public page under a different name", {"type": "web", "url": r0["url"], "passage": r0["passage"]})
            else:
                self.missing.append(f"GSTIN {gstin} not found in any public source; GST registration not confirmed")

        # ---- adverse media: company
        short = " ".join(norm_tokens(legal)) or legal
        adv = self.step("adverse_company", lambda: self.s_adverse(f"\"{short}\" raid OR arrested OR FIR OR NCLT OR insolvency OR \"GST evasion\" OR \"money laundering\"", [short.split()[0]]))
        for h in adv["hits"][:3]:
            self.flag("red", f"Adverse media on the company: \"{h['title']}\" ({', '.join(h['terms'])})",
                      {"type": "web", "url": h["url"], "field": "article text", "passage": h["passage"], "record_date": h.get("date")})
        if adv.get("possible"):
            self.flag("amber", f"{len(adv['possible'])} possible adverse result(s) for the company could not be retrieved to confirm; a person should review",
                      crust.source_ref(adv["meta"], f"web search: {adv['query']}"))

        # ---- adverse media: people, clearing namesakes
        # screen the director named on the request first, then the founders / CXOs on record
        subjects = []
        if self.req.get("director"):
            subjects.append({"name": self.req["director"], "title": "director named by the vendor", "group": "request"})
        for l in leaders:
            if all(name_similarity(l["name"], s["name"]) < 0.6 for s in subjects):
                subjects.append(l)
        for i, l in enumerate(subjects[:CONFIG["max_people_screened"]]):
            a = self.step(f"adverse_person_{i}", lambda l=l: self.s_adverse(f"\"{l['name']}\" arrested OR FIR OR raid OR \"fraud case\" OR \"money laundering\"", [l["name"].split()[-1], short.split()[0]]))
            for h in a["hits"][:2]:
                self.flag("red", f"Adverse media naming {l['name']} together with {legal}: \"{h['title']}\"",
                          {"type": "web", "url": h["url"], "field": "article text", "passage": h["passage"], "record_date": h.get("date")})
            if a.get("possible"):
                self.flag("amber", f"Possible adverse match for {l['name']}: a result names both the person and {legal}, but the article could not be retrieved to confirm; a person should review",
                          crust.source_ref(a["meta"], f"web search: {a['query']}"))
            if a["namesakes_cleared"]:
                self.fact(f"Namesakes cleared for {l['name']}",
                          f"{len(a['namesakes_cleared'])} adverse result(s) concern someone with this name and do not name {legal}; not counted. Their details are not stored.",
                          crust.source_ref(a["meta"], f"web search: {a['query']}"))
            else:
                self.fact(f"Adverse media screen: {l['name']}", "no adverse article links this person to the company",
                          crust.source_ref(a["meta"], f"web search: {a['query']}"))

        reds = [f for f in self.flags if f["severity"] == "red"]
        ambers = [f for f in self.flags if f["severity"] == "amber"]
        if reds:
            verdict, summary = "HOLD", f"Not cleared: {len(reds)} serious issue(s) need a person's review."
        elif ambers:
            verdict, summary = "HOLD", f"Real company, but {len(ambers)} item(s) need a person's review before clearing."
        else:
            verdict, summary = "CLEAR", "Real, active company; no adverse findings. Waiting for approval to add to the vendor master."
        return self._report(verdict, {"id": chosen["id"], "name": legal}, summary)

    def _report(self, verdict, entity, summary, extra=None):
        action = None
        if verdict == "CLEAR" and entity:
            key = f"add_to_vendor_master:{entity['id']}"
            action = store.propose_action(key, self.id, "add_to_vendor_master", {"crustdata_company_id": entity["id"], "name": entity["name"]})
        led = store.ledger_for(self.id)
        rep = {
            "job_id": self.id,
            "request": self.req,
            "verdict": verdict,
            "summary": summary,
            "entity": entity,
            "facts": self.facts,
            "flags": self.flags,
            "cannot_verify": self.missing,
            "screening": self.screening_metrics(),
            "approval": {"required_for": "adding the vendor to the approved vendor master", "action": action} if action else None,
            "cost": {"credits": led["total_credits"], "inr": round(led["total_credits"] * CONFIG["inr_per_credit"], 2),
                     "paid_calls": led["paid_calls"], "cache_hits": led["cache_hits"]},
            "elapsed_s": round(time.time() - self.t0, 1),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra and extra.get("candidates"):
            rep["candidates"] = extra["candidates"][:5]
        return rep


def run_request(request):
    return Job(request).run()
