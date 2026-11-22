"""GST registry lookup (gstin.cloud) and how the invoice check uses it. No network: urlopen is replaced
by canned answers in the shape gstin.cloud documents.

    python3 -m unittest tests.test_gstreg -v
"""
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error

try:
    from . import offline  # noqa: F401
except ImportError:
    import offline  # noqa: F401
import time

from vendorcheck import ap, crust, gstreg, pipeline, store

G = "29AAMCS1234A1ZM"  # Shadowfax in the fixture vendor master; passes the check character
VENDOR = {"vendor_id": "V001", "legal_name": "Shadowfax Technologies Limited", "gstin": G}


def body(status="Active", legal="SHADOWFAX TECHNOLOGIES LIMITED", gstin=G, sandbox=False, **kw):
    d = {"gstin": gstin, "legal_name": legal, "trade_name": "SHADOWFAX", "status": status, "constitution": "Public Limited Company",
         "taxpayer_type": "Regular", "registration_date": "2017-07-01", "state": "Karnataka"} | kw
    return {"success": True, "sandbox": sandbox, "cached": False, "credits_remaining": 9, "data": d}


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class GstRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "t.db")
        os.environ["GSTIN_API_KEY"] = "test-key"
        self.calls, self.answer = [], (200, body())
        self.real = gstreg.urllib.request.urlopen

        def fake(req, timeout=None):
            self.calls.append((req.full_url, req.get_header("X-api-key")))
            code, b = self.answer
            if isinstance(b, Exception):
                raise b
            if code != 200:
                raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(json.dumps(b).encode()))
            return Resp(json.dumps(b).encode())
        gstreg.urllib.request.urlopen = fake

    def tearDown(self):
        gstreg.urllib.request.urlopen = self.real
        os.environ["GSTIN_API_KEY"] = ""
        store._conn = None
        shutil.rmtree(self.tmp)

    def check(self):
        return ap.check_gstin({"gstin": G}, VENDOR)

    def test_active_and_name_matches_passes_with_citation(self):
        c = self.check()
        self.assertEqual(c["result"], "pass")
        self.assertIn("Active with GSTN since 2017-07-01", c["reasoning"])
        self.assertTrue(any("gstin.cloud" in e["source"] for e in c["evidence"]))
        self.assertEqual(self.calls, [(f"https://gstin.cloud/api/v1/verify/{G}", "test-key")])

    def test_cancelled_registration_fails(self):
        self.answer = (200, body(status="Cancelled", cancellation_date="2025-03-31"))
        c = self.check()
        self.assertEqual(c["result"], "fail")
        self.assertIn("Cancelled on 2025-03-31", c["reasoning"])

    def test_not_registered_fails(self):
        self.answer = (404, {"success": False, "error": "GSTIN not found"})
        c = self.check()
        self.assertEqual(c["result"], "fail")
        self.assertIn("not registered with GSTN", c["reasoning"])

    def test_registered_to_someone_else_fails(self):
        self.answer = (200, body(legal="SHREEJI CARGO MOVERS PRIVATE LIMITED"))
        c = self.check()
        self.assertEqual(c["result"], "fail")
        self.assertIn("borrowed identity", c["reasoning"])

    def test_answer_cached_including_not_registered(self):
        self.check()
        self.check()
        self.assertEqual(len(self.calls), 1)
        self.answer = (404, {"success": False})
        g2 = "06AAACT1234B1Z3"
        gstreg.lookup(g2)
        r = gstreg.lookup(g2)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(r["ok"] and not r["registered"] and r["source"]["cache_hit"])

    def test_sandbox_sample_data_never_decides(self):
        self.answer = (200, body(status="Cancelled", sandbox=True))
        c = self.check()
        self.assertEqual(c["result"], "pass")
        self.assertIn("sandbox key", c["reasoning"])
        self.check()
        self.assertEqual(len(self.calls), 2, "sample data is not cached")

    def test_out_of_credits_and_outage_say_not_checked(self):
        for answer, words in [((402, {"success": False}), "out of credits"), ((200, OSError("down")), "not reachable")]:
            store.db().execute("DELETE FROM http_cache")
            self.answer = answer
            c = self.check()
            self.assertEqual(c["result"], "pass", "local checks stand")
            self.assertIn(words, c["reasoning"])

    def test_answer_for_another_gstin_is_ignored(self):
        self.answer = (200, body(gstin="27AAACX0000X1Z5", status="Cancelled"))
        self.assertEqual(self.check()["result"], "pass")

    def test_no_key_means_no_call_and_old_wording(self):
        os.environ["GSTIN_API_KEY"] = ""
        c = self.check()
        self.assertEqual(c["result"], "pass")
        self.assertTrue(c["reasoning"].endswith("matches vendor record"))
        self.assertEqual(self.calls, [])

    def run_check(self, name):
        real = crust.call
        crust.call = lambda job_id, endpoint, body, use_cache=True: ([], {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": "t"})
        try:
            return pipeline.run_request({"vendor_name": name, "gstin": G, "request_id": "t-" + name})
        finally:
            crust.call = real

    def test_vendor_check_cites_registration_and_flags_cancelled_or_other_name(self):
        rep = self.run_check("Shadowfax Technologies Limited")
        fact = [f for f in rep["facts"] if f["claim"] == "GST registration (GSTN)"]
        self.assertTrue(fact and fact[0]["value"].startswith("Active since 2017-07-01"))
        self.assertFalse([f for f in rep["flags"] if "GSTN" in f["reason"] or "registered to" in f["reason"]])
        store.db().execute("DELETE FROM http_cache")
        self.answer = (200, body(status="Cancelled", legal="SHREEJI CARGO MOVERS PRIVATE LIMITED", trade_name=None))
        rep = self.run_check("Shadowfax Logistics")
        reasons = " | ".join(f["reason"] for f in rep["flags"] if f["severity"] == "red")
        self.assertIn("Cancelled per GSTN", reasons)
        self.assertIn("registered to 'SHREEJI", reasons)


if __name__ == "__main__":
    unittest.main()
