"""Offline tests for the three procurement services (vendor onboarding, invoice upload -> bills,
payments made). No API key, no credits: the Crustdata vendor check is switched off.

    python3 -m unittest tests.test_procure -v
"""
import base64
import os
import shutil
import tempfile
import threading
import unittest

try:
    from . import offline  # noqa: F401
except ImportError:
    import offline  # noqa: F401
os.environ["PROCURE_TRUST_CHECK"] = "0"
from vendorcheck import pipeline, store
from vendorcheck.procure import api, invoices, payables
from vendorcheck.procure.core import DomainError, tx


def gstin(body14):
    for c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if pipeline.gstin_valid(body14 + c):
            return body14 + c
    raise ValueError


GST = gstin("27AABCK1234M1Z")
PROFILE = {"legal_name": "Konkan Freight Private Limited", "gstin": GST, "website": "konkanfreight.in", "director": "Meera Joshi",
           "contact_name": "Accounts", "phone": "+91-22-5550-0101", "billing_address": "Plot 4, MIDC, Pune 411019",
           "bank_account": "912010045678901", "ifsc": "UTIB0000123", "bank_holder": "Konkan Freight Private Limited"}


def b64(s):
    return base64.b64encode(s.encode()).decode()


def invoice_text(no, amount, po=None, account=None):
    return (f"Konkan Freight Private Limited\nTax Invoice\nInvoice No: {no}\nDate: 05 Nov 2026\nGSTIN: {GST}\n"
            + (f"PO No: {po}\n" if po else "") + f"Total: INR {amount:,.2f}\n"
            + (f"Account No: {account}\nIFSC: UTIB0000999\n" if account else ""))


class ProcureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "t.db")
        os.environ["PROCURE_UPLOADS"] = os.path.join(self.tmp, "up")
        self.V, self.I, self.P = api.services()
        self.n = 0

    def tearDown(self):
        os.environ.pop("PROCURE_CRASH_AFTER_TRANSFER", None)
        store._conn = None
        shutil.rmtree(self.tmp)

    def call(self, method, path, body=None, key=None):
        return api.route(method, path, body or {}, {"Idempotency-Key": key} if key else {}, "Priya (AP lead)")

    # ---------- helpers ----------
    def onboard(self, **over):
        inv = self.V.invite("Konkan Freight", "accounts@konkanfreight.in", "Priya")
        tok = inv["portal_link"].split("t=")[1]
        self.V.portal_submit(tok, {**PROFILE, **over})
        return inv["vendor_id"], tok

    def open_bill(self, vid, no="KF/2026/0042", amount=50000.0):
        up = self.I.upload(vid, f"inv{no}.txt", b64(invoice_text(no, amount)))
        self.I.accept(up["doc_uuid"], "Priya", note="non-PO freight, agreed by ops")
        bill = up["document"]["invoice_uuid"]
        self.I.bill_action(bill, "approve", "Priya")
        return bill

    def key(self):
        self.n += 1
        return f"k-{self.n}"

    # ---------- 1. vendor onboarding ----------
    def test_onboarding_flow(self):
        vid, tok = self.onboard()
        v = self.V.view(vid)
        self.assertEqual(v["status"], "approval_pending")
        self.assertFalse([c for c in v["checks"] if c["result"] == "fail"], v["checks"])
        from vendorcheck import ap
        self.assertNotIn(vid, ap.vendors())        # not payable before approval
        self.V.request_more_details(vid, "Upload cancelled cheque", "Priya")
        self.assertEqual(self.V.view(vid)["status"], "pending_review")
        self.V.portal_submit(tok, {})
        self.V.approve(vid, "Priya")
        self.assertEqual(ap.vendors()[vid]["bank"]["account_no"], PROFILE["bank_account"])
        with self.assertRaises(DomainError):
            self.V.approve(vid, "Priya")                    # state machine: active cannot be approved again

    def test_onboarding_blocks_fronts(self):
        vid, _ = self.onboard(gstin=GST[:-1] + ("A" if GST[-1] != "A" else "B"), bank_holder="Ramesh Kumar")
        names = {c["name"]: c["result"] for c in self.V.view(vid)["checks"]}
        self.assertEqual(names["gstin"], "fail")
        self.assertEqual(names["bank_holder"], "fail")
        with self.assertRaises(DomainError) as e:
            self.V.approve(vid, "Priya")
        self.assertEqual(e.exception.code, 422)

    def test_duplicate_bank_account_across_vendors(self):
        vid, _ = self.onboard()
        self.V.approve(vid, "Priya")
        inv = self.V.invite("Konkan Logistics", "ap@konkanlogistics.in", "Priya")
        self.V.portal_submit(inv["portal_link"].split("t=")[1], {**PROFILE, "legal_name": "Konkan Logistics Private Limited",
                                                                 "gstin": gstin("27AABCK9999M1Z"), "bank_holder": "Konkan Logistics Private Limited"})
        names = {c["name"]: c for c in self.V.view(inv["vendor_id"])["checks"]}
        self.assertEqual(names["duplicate_vendor"]["result"], "fail")

    # ---------- 2. invoice upload: uuid per invoice ----------
    def test_invoice_uuid_and_dedupe(self):
        vid, _ = self.onboard()
        self.V.approve(vid, "Priya")
        a = self.I.upload(vid, "a.txt", b64(invoice_text("KF/2026/0042", 50000)))
        again = self.I.upload(vid, "a-copy.txt", b64(invoice_text("KF/2026/0042", 50000)))
        self.assertTrue(again["deduplicated"])                         # same bytes -> same document
        self.assertEqual(again["doc_uuid"], a["doc_uuid"])
        b = self.I.upload(vid, "b.txt", b64(invoice_text("KF-2026-42", 50000) + "\nresent\n"))
        self.assertNotEqual(b["doc_uuid"], a["doc_uuid"])               # every upload gets its own doc uuid
        self.assertEqual(b["document"]["invoice_uuid"], a["document"]["invoice_uuid"])   # ...but the SAME invoice uuid
        self.assertEqual(invoices.invoice_uuid(vid, "KF/2026/0042")[0], a["document"]["invoice_uuid"])
        dup = {c["name"]: c["result"] for c in b["document"]["checks"]}
        self.assertEqual(dup["duplicate_invoice"], "fail")
        self.I.accept(a["doc_uuid"], "Priya", note="non-PO")
        with self.assertRaises(DomainError):
            self.I.accept(b["doc_uuid"], "Priya", note="try anyway")    # one invoice, one bill

    def test_invoice_rules(self):
        vid, tok = self.onboard()
        self.V.approve(vid, "Priya")
        with self.assertRaises(DomainError):
            self.I.upload(vid, "x.bin", base64.b64encode(b"\x00\x01\x02\xff" * 10).decode())     # not a PDF/image/text
        r = self.I.upload(vid, "bank.txt", b64(invoice_text("KF/77", 9000, account="999988887777")))
        res = {c["name"]: c["result"] for c in r["document"]["checks"]}
        self.assertEqual(res["bank_details"], "fail")                   # invoice asks for another account
        with self.assertRaises(DomainError):
            self.I.accept(r["doc_uuid"], "Priya", note="looks fine")
        ok = self.I.upload(vid, "typed.txt", b64(invoice_text("KF/88", 12000)), {"invoice_no": "KF-88", "amount_inr": 12000})
        self.assertNotIn("typed_vs_document", {c["name"] for c in ok["document"]["checks"]})   # same values, other format
        bad = self.I.upload(vid, "typed2.txt", b64(invoice_text("KF/89", 12000)), {"amount_inr": 21000})
        self.assertEqual({c["name"]: c["result"] for c in bad["document"]["checks"]}["typed_vs_document"], "fail")
        code, _ = self.call("POST", "/api/portal/documents", {"t": "nope", "name": "a.txt", "data": b64("x")})
        self.assertEqual(code, 404)                                     # portal needs a valid invite token

    # ---------- 3. payables: exactly once ----------
    def test_idempotency_key_replay_and_misuse(self):
        vid, _ = self.onboard()
        self.V.approve(vid, "Priya")
        bill = self.open_bill(vid)
        body = {"vendor_id": vid, "allocations": [{"bill_uuid": bill, "amount": 50000}], "submit": True}
        code, p1 = self.call("POST", "/api/procure/payments", body, key="click-1")
        self.assertEqual(code, 201)
        code, p2 = self.call("POST", "/api/procure/payments", body, key="click-1")      # double click / retry
        self.assertEqual(p2["payment_id"], p1["payment_id"])
        self.assertTrue(p2.get("idempotent_replay"))
        code, err = self.call("POST", "/api/procure/payments", {**body, "allocations": [{"bill_uuid": bill, "amount": 10}]}, key="click-1")
        self.assertEqual(code, 422)                                     # same key, different request
        code, err = self.call("POST", "/api/procure/payments", body, key="click-2")     # new key, same bill
        self.assertEqual(code, 422)                                     # bill already promised to PM-00001
        self.assertIn("pay it twice", err["error"])
        code, _ = self.call("POST", "/api/procure/payments", body)
        self.assertEqual(code, 400)                                     # key is mandatory
        k = self.key()
        code, a1 = self.call("POST", f"/api/procure/payments/{p1['payment_id']}/approve", {}, key=k)
        code, a2 = self.call("POST", f"/api/procure/payments/{p1['payment_id']}/approve", {}, key=k)
        self.assertEqual(a1["status"], "paid")
        self.assertTrue(a2.get("idempotent_replay"))
        self.assertEqual(store.db().execute("SELECT COUNT(*) FROM sim_bank_transfers").fetchone()[0], 1)
        self.assertEqual(self.I.bill(bill)["status"], "paid")
        code, again = self.call("POST", f"/api/procure/payments/{p1['payment_id']}/approve", {}, key=self.key())
        self.assertEqual(code, 409)                                     # a paid payment cannot be approved again

    def test_crash_after_bank_transfer_resumes_once(self):
        vid, _ = self.onboard()
        self.V.approve(vid, "Priya")
        bill = self.open_bill(vid)
        _, p = self.P.create({"vendor_id": vid, "allocations": [{"bill_uuid": bill, "amount": 20000}], "submit": True}, idem_key="c1", by="Priya")
        os.environ["PROCURE_CRASH_AFTER_TRANSFER"] = "1"
        with self.assertRaises(SystemExit):
            self.P.approve({}, p["payment_id"], idem_key="c2", by="Priya")
        os.environ.pop("PROCURE_CRASH_AFTER_TRANSFER")
        self.assertEqual(self.P.view(p["payment_id"])["status"], "processing")
        self.P.resume()
        self.P.resume()
        self.assertEqual(self.P.view(p["payment_id"])["status"], "paid")
        self.assertEqual(store.db().execute("SELECT COUNT(*) FROM sim_bank_transfers").fetchone()[0], 1)
        b = self.I.bill(bill)
        self.assertEqual((b["status"], b["balance"]), ("partially_paid", 30000.0))

    def test_concurrent_payments_for_one_bill(self):
        vid, _ = self.onboard()
        self.V.approve(vid, "Priya")
        bill = self.open_bill(vid)
        out = []

        def go(k):
            out.append(self.call("POST", "/api/procure/payments", {"vendor_id": vid, "allocations": [{"bill_uuid": bill, "amount": 50000}]}, key=k)[0])
        ts = [threading.Thread(target=go, args=(f"race-{i}",)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(out), 6, out)                              # every request got an answer, none crashed
        self.assertEqual(out.count(201), 1, out)                # exactly one payment recorded for the bill

    def test_bank_change_after_approval_holds(self):
        vid, tok = self.onboard()
        self.V.approve(vid, "Priya")
        bill = self.open_bill(vid)
        _, p = self.P.create({"vendor_id": vid, "allocations": [{"bill_uuid": bill, "amount": 50000}], "submit": True}, idem_key="b1", by="Priya")
        self.V.portal_submit(tok, {"bank_account": "123456789012", "ifsc": "ICIC0000001"})     # fraudster edits bank
        self.assertEqual(self.V.view(vid)["status"], "approval_pending")
        code, out = self.call("POST", f"/api/procure/payments/{p['payment_id']}/approve", {}, key="b2")
        self.assertEqual(code, 422)                                     # vendor not payable until re-approved
        self.assertEqual(store.db().execute("SELECT COUNT(*) FROM sim_bank_transfers").fetchone()[0], 0)

    def test_payables_respects_vendor_status_between_approval_and_transfer(self):
        vid, tok = self.onboard()
        self.V.approve(vid, "Priya")
        bill = self.open_bill(vid)
        _, p = self.P.create({"vendor_id": vid, "allocations": [{"bill_uuid": bill, "amount": 50000}], "submit": True}, idem_key="h1", by="Priya")
        with tx() as c:
            payables.PAYMENT_FLOW.move(c, p["payment_id"], "pending_approval", "approve", pay_to_account="000", pay_to_ifsc="X", pay_to_name="Old")
        r = payables.PayCommand(p["payment_id"]).execute()             # snapshot no longer matches the vendor master
        self.assertTrue(r.get("held"))
        self.assertEqual(self.P.view(p["payment_id"])["status"], "pending_approval")


if __name__ == "__main__":
    unittest.main()
