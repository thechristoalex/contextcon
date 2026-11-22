"""Offline tests for bank-statement posting. Crustdata is replaced by canned responses, so no credits are used.

    python3 -m unittest tests.test_bank -v
"""
import os
import shutil
import tempfile
import time
import unittest

from vendorcheck import bank, crust, store

FIX = os.path.join(os.path.dirname(__file__), "..", "data", "fixtures")


def company(name, domain, employees, cid):
    return {"confidence_score": 1.0, "company_data": {"crustdata_company_id": cid, "basic_info": {
        "name": name, "primary_domain": domain, "employee_count_range": employees}}}


IDENTIFY = {
    "SWIGGY": [company("Swiggy", "swiggy.com", "10001+", 1), company("Swiggy", "swiggy.in", "1-10", 2),
               company("Swiggy", "swiggyclone.io", "11-50", 3)],
    "SHARMA": [company("Sharma", f"sharma{i}.com", "11-50", 10 + i) for i in range(4)],
    "DELHIVERY": [company("Delhivery", "delhivery.com", "10001+", 20)],
}
PAGES = {
    "https://swiggy.com": "<html><footer>&copy; 2026 Swiggy Limited. All rights reserved.</footer></html>",
    "https://delhivery.com": "<html><p>About us</p><p>Corporate travel partner: Bundl Technologies Private Limited</p></html>",
}


class FakeCrust:
    """Stands in for crust.call; records each call in the ledger as the real client does."""

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def __call__(self, job_id, endpoint, body, use_cache=True):
        self.calls.append((endpoint, body))
        if self.fail:
            raise self.fail
        store.ledger_add(job_id, endpoint, "test", False, 0.2 if endpoint == "/company/identify" else 1.0, 1, 200)
        meta = {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": "test"}
        if endpoint == "/company/identify":
            return [{"matched_on": body["names"][0], "matches": IDENTIFY.get(body["names"][0], [])}], meta
        if endpoint == "/web/enrich/live":
            u = body["urls"][0]
            return [{"success": u in PAGES, "url": u, "content": PAGES.get(u)}], meta
        raise AssertionError(endpoint)


class BankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "t.db")
        self.statement = os.path.join(self.tmp, "oct.csv")
        shutil.copy(os.path.join(FIX, "bank_lines_oct2026.csv"), self.statement)
        self.real_call = crust.call
        crust.call = self.fake = FakeCrust()

    def tearDown(self):
        crust.call = self.real_call
        store._conn = None
        shutil.rmtree(self.tmp)

    def by_desc(self, out):
        return [(l["description"], l["result"], l["payee"], l["account"]) for l in out["lines"]]

    def test_example_statement(self):
        out = bank.post_file(self.statement)
        got = self.by_desc(out)
        self.assertEqual(got[0], ("RAZ*SWIGGY BLR", "POSTED", "Swiggy Limited", "Staff meals"))
        self.assertEqual(got[1], ("AWS EMEA", "POSTED", "Amazon Web Services EMEA SARL", "Cloud hosting"))
        self.assertEqual(got[2][1], "HELD")                       # same date, descriptor, amount: possible duplicate
        self.assertIn("duplicate", out["lines"][2]["reason"])
        self.assertEqual(got[3][1:3], ("CANNOT_VERIFY", None))     # SHARMA: four companies, no guess
        self.assertEqual(got[4][1], "HELD")                       # NEFT to Shadowfax with no approved payment behind it
        self.assertIn("no approved payment", out["lines"][4]["reason"])
        # Delhivery's page names a different company only, so the legal name is not confirmed and the trading name is kept
        self.assertEqual(got[5], ("UPI/DELHIVERY", "POSTED", "Delhivery", "Freight - express parcel"))
        self.assertIn("legal name not confirmed", out["lines"][5]["reason"])

    def test_legal_name_is_cited(self):
        line = bank.post_file(self.statement)["lines"][0]
        web = [e for e in line["evidence"] if e.get("type") == "web"]
        self.assertEqual(web[0]["url"], "https://swiggy.com")
        self.assertIn("Swiggy Limited", web[0]["passage"])
        self.assertTrue(web[0]["retrieved_at"])

    def test_legal_name_rules(self):
        cases = {
            "<div>Get the Swiggy app now © 2026 Swiggy Limited Company About Us</div>": "Swiggy Limited",  # real footer, 22 Nov 2026
            "<p>Copyright Swiggy Limited 2026</p>": "Swiggy Limited",
            "<p>Bundl Technologies Private Limited</p>": None,                      # another company: never taken
            "<p>Swiggy Limited</p><p>Swiggy Instamart Private Limited</p>": None,   # two legal names: ambiguous
            "<script>var x='Swiggy Fake Limited'</script><p>© Swiggy Limited</p>": "Swiggy Limited",
        }
        for html, want in cases.items():
            PAGES["https://swiggy.com"] = html
            got = bank.legal_name_from_site("t", "Swiggy", "swiggy.com")
            self.assertEqual(got and got["name"], want, html)
        PAGES["https://swiggy.com"] = "<html><footer>&copy; 2026 Swiggy Limited. All rights reserved.</footer></html>"

    def test_rerun_posts_nothing_twice(self):
        bank.post_file(self.statement)
        n = len(self.fake.calls)
        again = bank.post_file(self.statement)
        self.assertTrue(all(l.get("note", "").startswith("already posted") for l in again["lines"]))
        self.assertEqual(len(self.fake.calls), n)
        self.assertEqual(store.db().execute("SELECT COUNT(*) FROM bank_lines").fetchone()[0], 6)

    def test_same_statement_under_another_name_is_not_reposted(self):
        bank.post_file(self.statement)
        copy = os.path.join(self.tmp, "upload_copy.csv")
        shutil.copy(self.statement, copy)
        again = bank.post_file(copy)
        self.assertTrue(all("already posted" in l.get("note", "") for l in again["lines"]))
        self.assertEqual(store.db().execute("SELECT COUNT(*) FROM bank_lines").fetchone()[0], 6)

    def test_vendor_transfer_reconciles_to_one_payment_only(self):
        store.db().execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?)",
                           ("V001:2026-451", "V001:2026-451", "V001", 236000.0, "50200011112222", "HDFC0000123",
                            "Shadowfax Technologies Limited", "AP lead", time.time(), "SIMPAY-TEST"))
        out = bank.post_file(self.statement)
        line = out["lines"][4]
        self.assertEqual(line["result"], "POSTED")
        self.assertEqual(line["payment_key"], "V001:2026-451")
        # a second transfer of the same amount on another day cannot claim the same payment
        later = os.path.join(self.tmp, "nov.csv")
        with open(later, "w") as f:
            f.write("date,description,amount\n2 Nov 2026,NEFT/SHADOWFAX TECHNOLOGIES,236000\n")
        self.assertEqual(bank.post_file(later)["lines"][0]["result"], "HELD")

    def test_source_down_is_not_an_answer(self):
        crust.call = FakeCrust(fail=crust.ApiError(503, {"error": "down"}))
        out = bank.post_file(self.statement)
        swiggy = out["lines"][0]
        self.assertEqual(swiggy["result"], "CANNOT_VERIFY")
        self.assertIn("source unavailable", swiggy["reason"])
        self.assertEqual(out["lines"][1]["result"], "POSTED")   # vendor-master lines need no live call

    def test_cost_limit_stops_without_crashing(self):
        crust.call = FakeCrust(fail=crust.BudgetExceeded("cost limit 12 credits reached"))
        out = bank.post_file(self.statement)
        self.assertEqual(out["lines"][0]["result"], "CANNOT_VERIFY")
        self.assertIn("cost limit", out["lines"][0]["reason"])

    def test_cost_is_reported(self):
        out = bank.post_file(self.statement)
        # SWIGGY identify + site, SHARMA identify, DELHIVERY identify + site
        self.assertAlmostEqual(out["cost"]["credits"], 0.2 * 3 + 1.0 * 2)
        self.assertEqual(out["cost"]["paid_calls"], 5)


if __name__ == "__main__":
    unittest.main()
