"""Offline tests for the payables desk. The live vendor check is replaced by a canned verdict per vendor,
so no API key and no credits are needed.

    python3 -m unittest tests.test_payables -v
"""
import copy
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

VERDICTS = {  # what the live vendor check returned for each fixture vendor on 22 Nov 2026
    "Shadowfax Technologies Limited": "CLEAR",
    "TCI Express Limited": "CLEAR",
    "Shreeji Cargo Movers Private Limited": "CANNOT_VERIFY",
}


def fake_run_request(req):
    fake_run_request.calls.append(req)
    if fake_run_request.down:
        raise crust.ApiError(503, {"error": "down"})
    verdict = VERDICTS.get(req["vendor_name"], "CANNOT_VERIFY")
    return {"verdict": verdict, "summary": f"{req['vendor_name']}: {verdict}", "facts": [], "flags": [],
            "cannot_verify": [] if verdict == "CLEAR" else [f"no company record carries the domain {req['website']}"],
            "cost": {"credits": 0}}


class PayablesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "t.db")
        self.real_run = pipeline.run_request
        pipeline.run_request = fake_run_request
        fake_run_request.calls, fake_run_request.down = [], False

    def tearDown(self):
        pipeline.run_request = self.real_run
        store._conn = None
        shutil.rmtree(self.tmp)

    def states(self):
        return {os.path.basename(r["source_file"])[:2]: r["state"] for r in ap.process_inbox()}

    def test_inbox_outcomes(self):
        s = self.states()
        self.assertEqual(s["01"], "awaiting_approval")   # clean invoice waits for a person
        self.assertEqual(s["02"], "held")                # front company: cannot verify the vendor
        self.assertEqual(s["03"], "awaiting_approval")   # common-name director, vendor clear
        self.assertEqual(s["04"], "duplicate")           # INV-2026-451 resent as INV/2026/0451
        self.assertEqual(s["05"], "held")                # "we changed our bank" email
        self.assertEqual(s["06"], "held")                # new bank account, PO already billed
        self.assertEqual(s["07"], "held")                # look-alike sender shadowfaxx.in

    def test_rerun_inbox_changes_nothing(self):
        self.states()
        again = ap.process_inbox()
        self.assertTrue(all("already processed" in r.get("note", "") for r in again))

    def test_held_invoice_cites_reason_and_drafts_callback(self):
        self.states()
        inv = {r["source_file"][-30:]: r for r in ap.invoices()}
        bank_change = next(r for k, r in inv.items() if k.endswith("05_tci_bank_change.eml"))
        import json
        d = json.loads(bank_change["decision_json"])
        self.assertIn("call-back", d["reason"])
        self.assertTrue(d["draft_message"])               # drafted, never sent

    def test_pay_exactly_once(self):
        self.states()
        r = ap.approve("V001:2026-451", "AP lead")
        self.assertTrue(r["ok"])
        self.assertFalse(ap.approve("V001:2026-451", "AP lead")["ok"])   # second approval is refused
        self.assertTrue(ap.pay("V001:2026-451").get("already_paid"))      # second pay is a no-op
        self.assertEqual(ap.resume(), [])
        self.assertEqual(len(ap.payments()), 1)

    def test_crash_between_approval_and_payment(self):
        self.states()
        v = ap.vendors()["V001"]
        # what CRASH_AFTER_APPROVE leaves behind: approved, not yet paid
        store.db().execute("UPDATE invoices SET state='approved', vendor_hash_at_approval=?, "
                           "decision_json=json_set(decision_json,'$.approved_by','AP lead') WHERE invoice_key='V001:2026-451'",
                           (ap.vendor_hash(v),))
        self.assertEqual(len(ap.payments()), 0)
        first = ap.resume()
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]["ok"])
        self.assertEqual(ap.resume(), [])                 # a second restart pays nothing
        self.assertEqual(len(ap.payments()), 1)

    def test_vendor_record_changed_after_approval_is_held(self):
        self.states()
        store.db().execute("UPDATE invoices SET state='approved', vendor_hash_at_approval='old' WHERE invoice_key='V001:2026-451'")
        r = ap.pay("V001:2026-451")
        self.assertFalse(r["ok"])
        self.assertEqual(ap.payments(), [])

    def test_changed_vendor_record_is_checked_again(self):
        real = ap.vendors
        try:
            self.states()
            n = len(fake_run_request.calls)
            v = copy.deepcopy(real()["V001"])
            self.assertEqual(ap.trust_job_id(v), ap.trust_job_id(copy.deepcopy(v)))
            v["director"] = "Someone Else"
            ap.check_trust(v)
            self.assertEqual(len(fake_run_request.calls), n + 1)           # a new check ran
            self.assertNotEqual(fake_run_request.calls[-1]["request_id"], ap.trust_job_id(real()["V001"]))
        finally:
            ap.vendors = real

    def test_unusual_layouts_are_held_never_filed_or_guessed(self):
        bill = ("From: accounts@shadowfax.in\nSHADOWFAX TECHNOLOGIES LIMITED\nBill No 2026/0612  Dt 05-11-2026\n"
                "GSTIN 29AAMCS1234A1ZM\nLast mile delivery Oct   1,12,000\nNet payable                 1,12,000\nRef PO-1001")
        x = ap.extract(bill)
        self.assertEqual((x["doc_type"], x["invoice_no"], x["amount_inr"], x["po_no"]), ("invoice", "2026/0612", 112000.0, "PO-1001"))
        self.assertEqual(ap.process_file("/t/bill.txt", text=bill)["state"], "held")   # amount differs from the PO
        no_amount = "From: accounts@shadowfax.in\nShadowfax Technologies Limited\nInvoice No: INV/2026/0700\nPlease pay."
        r = ap.process_file("/t/noamt.txt", text=no_amount)
        self.assertEqual(r["state"], "held")
        self.assertIn("Could not read amount", r["reason"])
        memo = "From: accounts@shadowfax.in\nShadowfax Technologies Limited\nPlease find our statement for October."
        self.assertEqual(ap.process_file("/t/memo.txt", text=memo)["state"], "held")
        receipt = "From: accounts@shadowfax.in\nPayment receipt for INR 2,36,000. Thank you."
        self.assertEqual(ap.process_file("/t/receipt.txt", text=receipt)["state"], "rejected")  # filed: nothing to pay
        self.assertEqual(ap.payments(), [])

    def test_source_down_holds_instead_of_passing(self):
        fake_run_request.down = True
        s = self.states()
        self.assertEqual(s["01"], "held")
        self.assertEqual(ap.payments(), [])


if __name__ == "__main__":
    unittest.main()
