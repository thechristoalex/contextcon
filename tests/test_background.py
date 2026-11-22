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
