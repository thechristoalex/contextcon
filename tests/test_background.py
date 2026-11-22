"""Offline tests for the company background check on every bill: GSTIN facts, known vendors, unknown senders.

    python3 -m unittest tests.test_background -v
"""
import json
import os
import shutil
import tempfile
import time
import unittest

try:
    from . import offline  # noqa: F401  (python -m unittest tests.x)
except ImportError:
    import offline  # noqa: F401  (python -m unittest discover tests)
from vendorcheck import ap, crust, pipeline, store

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def with_check_char(first14):
    """A GSTIN with a correct 15th (check) character, computed as gstin_valid does."""
    s = 0
    for i, ch in enumerate(first14):
        p = CHARS.index(ch) * (2 if i % 2 else 1)
        s += p // 36 + p % 36
    return first14 + CHARS[(36 - s % 36) % 36]


INDIVIDUAL_GSTIN = with_check_char("27ABCPK1234F1Z")   # PAN ABCPK1234F: 4th character P = individual
COMPANY_GSTIN = with_check_char("29AAMCS1234A1Z")      # same as the fixture Shadowfax GSTIN


class TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        store._conn = None
        shutil.rmtree(self.tmp)


class GstinProfileTest(TempStore):
    def test_profile_reads_state_pan_and_holder_type(self):
        p = pipeline.gstin_profile("06AAACT1234B1Z3")
        self.assertTrue(p["valid"])
        self.assertEqual(p["state"], "Haryana")
        self.assertEqual(p["pan"], "AAACT1234B")
        self.assertEqual(p["pan_type"], "company")
        self.assertIsNone(pipeline.gstin_profile(""))
        self.assertFalse(pipeline.gstin_profile("06AAACT1234B1Z4")["valid"])   # wrong check character

    def test_entity_type_mismatch(self):
        self.assertIn("individual", pipeline.entity_type_mismatch("Ramesh Traders Private Limited", "P"))
        self.assertIsNone(pipeline.entity_type_mismatch("TCI Express Limited", "C"))
        self.assertIsNotNone(pipeline.entity_type_mismatch("Acme Freight LLP", "C"))    # an LLP's PAN is type F
        self.assertIsNone(pipeline.entity_type_mismatch("Acme Freight LLP", "F"))
        self.assertIsNone(pipeline.entity_type_mismatch("Unlimited Logistics", "P"))    # "Unlimited" is not "Limited"
        self.assertIsNone(pipeline.entity_type_mismatch("Ramesh Traders", "P"))         # no suffix, nothing to compare

    def test_job_reports_pan_type_and_flags_mismatch(self):
        real = crust.call
        crust.call = lambda job_id, endpoint, body, use_cache=True: ([], {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": "t"})
        try:
            rep = pipeline.run_request({"vendor_name": "Ramesh Traders Private Limited", "gstin": INDIVIDUAL_GSTIN, "request_id": "t-pan"})
        finally:
            crust.call = real
        facts = {f["claim"]: f for f in rep["facts"]}
        self.assertEqual(facts["PAN holder type"]["value"], "individual")
        self.assertEqual(facts["PAN holder type"]["source"]["type"], "computed")
        self.assertTrue(any(f["severity"] == "amber" and "individual" in f["reason"] for f in rep["flags"]))


class FakeVendorCheck:
    """Stands in for pipeline.run_request: records each request and returns a report with the given verdict."""

    def __init__(self, verdict="CLEAR", entity="Delhivery Limited", flags=None, error=None):
        self.verdict, self.entity, self.flags, self.error, self.calls = verdict, entity, flags or [], error, []

    def __call__(self, req):
        self.calls.append(req)
        if self.error:
            raise self.error
        return {"job_id": req.get("request_id"), "request": req, "verdict": self.verdict,
                "summary": f"{self.entity}: {self.verdict}", "entity": {"id": 1, "name": self.entity} if self.entity else None,
                "facts": [{"claim": "Matched company record", "value": f"{self.entity} (crustdata_company_id 1)",
                           "source": {"api": "/company/identify", "retrieved_at": "2026-11-22"}}],
                "flags": self.flags, "cannot_verify": [] if self.verdict == "CLEAR" else ["no registry record"],
                "screening": {"hits_found": 0, "hits_cleared": 0, "hits_escalated": 0}, "decisions": [],
                "cost": {"credits": 9.2, "inr": 230.0}}


class PatchedCheck(TempStore):
    def use(self, fake):
        self.fake = fake
        self.real_run = pipeline.run_request
        pipeline.run_request = fake
        self.addCleanup(setattr, pipeline, "run_request", self.real_run)
        return fake


class KnownVendorTest(PatchedCheck):
    def test_vendor_master_gstin_reaches_the_vendor_check(self):
        fake = self.use(FakeVendorCheck(entity="Shadowfax Technologies Limited"))
        ap.process_file(os.path.join(ap.FIX, "inbox", "01_shadowfax_invoice.txt"))
        req = next(r for r in fake.calls if r["vendor_name"] == "Shadowfax Technologies Limited")
        self.assertEqual(req["gstin"], ap.vendors()["V001"]["gstin"])


NEW_GSTIN = with_check_char("07AAACD1234E1Z")   # a well-formed company GSTIN that no vendor on file carries


def bill(sender=None, vendor=None, gstin=None, invoice_no="DL-1", account_name=None, amount="10,000.00"):
    head = [f"From: Billing <{sender}>", "Subject: Invoice", ""] if sender else []
    body = ["TAX INVOICE"] + ([f"Vendor: {vendor}"] if vendor else []) + ([f"GSTIN: {gstin}"] if gstin else []) + \
           [f"Invoice No: {invoice_no}", "Invoice Date: 20 Nov 2026", f"Grand Total: ₹{amount}"] + \
           ([f"Account Name: {account_name}"] if account_name else [])
    return "\n".join(head + body) + "\n"


class UnknownVendorTest(PatchedCheck):
    def process(self, text, name="bill.txt"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        out = ap.process_file(path)
        row = next(r for r in ap.invoices() if r["source_file"] == path)
        d = json.loads(row["decision_json"])
        return out, d, {c["check"]: c for c in d["checks"]}

    def test_unknown_vendor_is_looked_up_and_held(self):
        fake = self.use(FakeVendorCheck("CLEAR", "Delhivery Limited"))
        out, d, checks = self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited", gstin=NEW_GSTIN))
        self.assertEqual(out["state"], "held")                                   # never paid: not a vendor on file
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["vendor_name"], "Delhivery Limited")
        self.assertEqual(fake.calls[0]["website"], "delhivery.com")
        self.assertEqual(fake.calls[0]["gstin"], NEW_GSTIN)
        bc = checks["background_check"]
        self.assertEqual((bc["result"], bc["weight"]), ("warn", 0))
        self.assertIn("not a vendor on file", bc["reasoning"])
        self.assertTrue(bc["evidence"] and bc["evidence"][0]["source"])          # facts carry their sources
        self.assertEqual(d["trust_report"]["verdict"], "CLEAR")
        self.assertEqual(checks["vendor_identified"]["result"], "fail")

    def test_unknown_vendor_flagged_by_lookup_fails_the_check(self):
        self.use(FakeVendorCheck("HOLD", "Delhivery Limited", flags=[{"severity": "red", "reason": "Company record is dormant"}]))
        out, d, checks = self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited"))
        self.assertEqual(out["state"], "held")
        self.assertEqual((checks["background_check"]["result"], checks["background_check"]["weight"]), ("fail", 20))
        self.assertIn("dormant", checks["background_check"]["reasoning"])

    def test_cannot_verify_fails_the_check(self):
        self.use(FakeVendorCheck("CANNOT_VERIFY", None))
        out, d, checks = self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited"))
        self.assertEqual(checks["background_check"]["result"], "fail")
        self.assertIn("no registry record", checks["background_check"]["reasoning"])

    def test_free_mail_is_not_a_website(self):
        fake = self.use(FakeVendorCheck())
        self.process(bill(sender="ramesh.traders@gmail.com", vendor="Ramesh Traders"))
        self.assertIsNone(fake.calls[0]["website"])
        self.assertEqual(fake.calls[0]["vendor_name"], "Ramesh Traders")

    def test_nothing_to_look_up(self):
        fake = self.use(FakeVendorCheck())
        out, d, checks = self.process(bill(sender="someone@gmail.com"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(out["state"], "held")
        self.assertEqual(checks["background_check"]["result"], "info")
        self.assertIn("nothing", checks["background_check"]["reasoning"].lower())

    def test_gstin_only_bill_gets_offline_profile_without_a_call(self):
        fake = self.use(FakeVendorCheck())
        out, d, checks = self.process(bill(gstin=INDIVIDUAL_GSTIN))
        self.assertEqual(fake.calls, [])
        self.assertEqual(out["state"], "held")
        bc = checks["background_check"]
        self.assertEqual(bc["result"], "info")
        vals = {e["field"]: e["value"] for e in bc["evidence"]}
        self.assertEqual(vals["state"], "Maharashtra")
        self.assertEqual(vals["PAN holder type"], "individual")

    def test_invalid_gstin_only_bill_fails(self):
        self.use(FakeVendorCheck())
        out, d, checks = self.process(bill(gstin=INDIVIDUAL_GSTIN[:-1] + ("0" if INDIVIDUAL_GSTIN[-1] != "0" else "1")))
        self.assertEqual((checks["background_check"]["result"], checks["background_check"]["weight"]), ("fail", 60))

    def test_lookup_error_holds_instead_of_crashing(self):
        for err in (ValueError("bad request"), crust.ApiError(503, {"error": "down"})):
            self.use(FakeVendorCheck(error=err))
            out, d, checks = self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited", invoice_no=f"E-{id(err)}"),
                                          name=f"err_{id(err)}.txt")
            self.assertEqual(out["state"], "held")
            self.assertEqual(checks["background_check"]["result"], "unavailable")

    def test_same_business_reuses_one_lookup(self):
        fake = self.use(FakeVendorCheck())
        self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited", invoice_no="DL-1"), "a.txt")
        self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited", invoice_no="DL-2"), "b.txt")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0]["request_id"], fake.calls[1]["request_id"])   # same job id, so the stored report is reused

    def test_account_name_matches_legal_name_ignoring_suffixes(self):
        self.use(FakeVendorCheck("CLEAR", "TCI Freight Limited"))
        _, _, checks = self.process(bill(sender="a@tcifreight.in", vendor="TCI Freight", account_name="TCI Freight Pvt. Ltd."), "same.txt")
        self.assertEqual(checks["account_name"]["result"], "pass")
        _, _, checks = self.process(bill(sender="a@tcifreight.in", vendor="TCI Freight", account_name="Sunil Enterprises", invoice_no="X-2"), "other.txt")
        self.assertEqual((checks["account_name"]["result"], checks["account_name"]["weight"]), ("warn", 15))

    def test_replay_after_an_outage_runs_the_lookup_again(self):
        fake = self.use(FakeVendorCheck(error=crust.ApiError(503, {"error": "down"})))
        out, _, checks = self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited"))
        self.assertEqual(checks["background_check"]["result"], "unavailable")
        fake.error = None
        r = ap.recheck(next(i["invoice_key"] for i in ap.invoices()))
        self.assertTrue(r["ok"])
        self.assertEqual(r["after"]["state"], "held")
        d = json.loads(next(i["decision_json"] for i in ap.invoices()))
        self.assertEqual({c["check"]: c["result"] for c in d["checks"]}["background_check"], "warn")
        self.assertEqual(len(fake.calls), 2)

    def test_result_carries_the_stored_key_so_the_app_can_open_the_case(self):
        self.use(FakeVendorCheck())
        out, _, _ = self.process(bill(sender="billing@delhivery.com", vendor="Delhivery Limited"))
        self.assertEqual(out["invoice_key"], next(i["invoice_key"] for i in ap.invoices()))
        self.assertTrue(out["invoice_key"].startswith("unknown:"))


class WebsiteFetchTest(TempStore):
    """delhivery.com only has a certificate for www.delhivery.com: a live site must not be flagged as dead."""

    def fetch(self, pages):
        calls = []

        def fake(job_id, endpoint, body, use_cache=True):
            u = body["urls"][0]
            calls.append(u)
            page = pages.get(u, {"success": False, "error": "INVALID_URL", "message": "URL navigation failed: net::ERR_NAME_NOT_RESOLVED"})
            return [page], {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": u}
        real = crust.call
        crust.call = fake
        try:
            return pipeline.Job({"vendor_name": "Delhivery Limited", "request_id": "t-web"}).s_website("https://delhivery.com"), calls
        finally:
            crust.call = real

    def test_www_variant(self):
        self.assertEqual(pipeline.www_variant("https://delhivery.com"), "https://www.delhivery.com")
        self.assertEqual(pipeline.www_variant("https://www.delhivery.com/about"), "https://delhivery.com/about")

    def test_bare_domain_cert_error_falls_back_to_www(self):
        live = {"success": True, "title": "Delhivery", "content": "<p>" + "Delhivery logistics and supply chain services. " * 10 + "</p>"}
        w, calls = self.fetch({"https://delhivery.com": {"success": False, "message": "URL navigation failed: net::ERR_CERT_COMMON_NAME_INVALID"},
                               "https://www.delhivery.com": live})
        self.assertEqual(calls, ["https://delhivery.com", "https://delhivery.com", "https://www.delhivery.com"])  # standard, human_mode, www
        self.assertTrue(w["ok"])
        self.assertEqual(w["url"], "https://www.delhivery.com")
        self.assertIn("ERR_CERT_COMMON_NAME_INVALID", w["tried"][0]["error"])

    def test_live_site_is_fetched_once(self):
        live = {"success": True, "title": "Delhivery", "content": "<p>" + "Delhivery logistics. " * 20 + "</p>"}
        w, calls = self.fetch({"https://delhivery.com": live})
        self.assertEqual(calls, ["https://delhivery.com"])       # no extra paid call when the first one works
        self.assertEqual(w["tried"], [])

    def test_both_fail_records_why(self):
        w, calls = self.fetch({})
        self.assertFalse(w["ok"])
        self.assertEqual(len(w["tried"]), 3)
        self.assertEqual([t["fetched_with"] for t in w["tried"]], ["standard", "human_mode", "human_mode"])
        self.assertIn("ERR_NAME_NOT_RESOLVED", w["tried"][2]["error"])

    def test_www_site_falls_back_to_the_bare_host(self):
        live = {"success": True, "title": "Acme", "content": "<p>" + "Acme freight services. " * 20 + "</p>"}
        real = crust.call
        calls = []

        def fake(job_id, endpoint, body, use_cache=True):
            calls.append(body["urls"][0])
            page = live if body["urls"][0] == "https://acme.in" else {"success": False, "message": "net::ERR_CERT_COMMON_NAME_INVALID"}
            return [page], {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": "t"}
        crust.call = fake
        try:
            w = pipeline.Job({"vendor_name": "Acme", "request_id": "t-www"}).s_website("https://www.acme.in")
        finally:
            crust.call = real
        self.assertTrue(w["ok"])
        self.assertEqual(calls[-1], "https://acme.in")
