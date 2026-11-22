"""Tests for scanning a bill photo into an invoice. No credits: the live vendor check is stubbed.

    python3 -m unittest tests.test_scan -v

The unit tests replace the text reader with a fake, so they run anywhere. The photo tests at the
bottom use the real reader on this machine (Apple Vision on a Mac, else Tesseract) and the test
photos in data/fixtures/bills/; they are skipped when neither is available.
"""
try:
    from . import offline  # noqa: F401  keeps tests off live APIs; must come before vendorcheck
except ImportError:
    import offline  # noqa: F401
import json
import os
import shutil
import tempfile
import unittest

from vendorcheck import ap, scan, store

FIX = os.path.join(os.path.dirname(__file__), "..", "data", "fixtures")
JPEG = b"\xff\xd8\xff\xe0" + b"test photo"

BILL_01 = """TAX INVOICE
Vendor: Shadowfax Technologies Limited
GSTIN: 29AAMCS1234A1ZM
Invoice No: INV/2026/0451
Invoice Date: 10 Nov 2026
PO No: PO-1001
Qty: 400
Total Amount Payable: INR 2,36,000.00
Account No: 50200011112222  IFSC: HDFC0000123
Account Name: Shadowfax Technologies Limited"""


def lines_of(text, conf=None):
    """Fake reader output: one OCR line per text line, top to bottom. conf maps a substring to a confidence."""
    out = []
    for i, t in enumerate(text.splitlines()):
        c = next((v for k, v in (conf or {}).items() if k in t), 1.0)
        out.append({"text": t, "conf": c, "box": [0.05, 0.04 * i + 0.02, 0.6, 0.03]})
    return out


class FakeReader:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return {"reader": "fake", "lines": self.lines}


def trust_pass(v):
    return ap.chk("vendor_trust", "pass", "stubbed in tests"), None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "t.db")
        self.fix = os.path.join(self.tmp, "fixtures")
        os.makedirs(os.path.join(self.fix, "inbox"))
        for f in ("vendors.json", "pos.json", "grns.json"):
            shutil.copy(os.path.join(FIX, f), self.fix)
        self.saved = (ap.FIX, ap.check_trust, scan.read_image)
        ap.FIX = self.fix
        ap.check_trust = trust_pass

    def tearDown(self):
        ap.FIX, ap.check_trust, scan.read_image = self.saved
        store._conn = None
        shutil.rmtree(self.tmp)

    def checks(self, out):
        r = store.db().execute("SELECT decision_json, extracted_json FROM invoices WHERE invoice_key=?", (out["invoice_key"],)).fetchone()
        return {c["check"]: c for c in json.loads(r["decision_json"])["checks"]}, json.loads(r["extracted_json"])


class RowsTest(unittest.TestCase):
    def test_label_and_value_read_apart_on_one_line_become_one_row(self):
        lines = [
            {"text": "INV/2026/0451", "conf": 1.0, "box": [0.60, 0.101, 0.2, 0.02]},
            {"text": "Invoice No:", "conf": 1.0, "box": [0.40, 0.100, 0.15, 0.02]},
            {"text": "GSTIN: 29AAMCS1234A1ZM", "conf": 0.5, "box": [0.05, 0.150, 0.4, 0.02]},
        ]
        rows = scan.rows_from_lines(lines)
        self.assertEqual([r["text"] for r in rows], ["Invoice No:  INV/2026/0451", "GSTIN: 29AAMCS1234A1ZM"])
        self.assertEqual(rows[1]["conf"], 0.5)
        self.assertEqual(len(rows[0]["boxes"]), 2)

    def test_slight_tilt_keeps_a_label_and_its_value_together(self):
        lines = [{"text": "Total:", "conf": 1.0, "box": [0.40, 0.500, 0.1, 0.02]},
                 {"text": "Rs. 4,12,500.00", "conf": 1.0, "box": [0.75, 0.508, 0.2, 0.02]}]
        self.assertEqual([r["text"] for r in scan.rows_from_lines(lines)], ["Total:  Rs. 4,12,500.00"])

    def test_tilted_total_and_its_value_stay_on_one_row(self):
        # positions read from 03_tci_invoice after a browser re-encode: the value sits 0.0126 below its label
        lines = [{"text": "996812", "conf": 1.0, "box": [0.5641, 0.2980, 0.0631, 0.0189]},
                 {"text": "Grand Total:", "conf": 1.0, "box": [0.4635, 0.3500, 0.1124, 0.0178]},
                 {"text": "₹1,58,400.00", "conf": 1.0, "box": [0.7803, 0.3596, 0.1178, 0.0237]}]
        self.assertEqual([r["text"] for r in scan.rows_from_lines(lines)], ["996812", "Grand Total:  ₹1,58,400.00"])

    def test_rupee_sign_misread_as_letter_r_is_fixed_only_before_a_grouped_amount(self):
        # the browser re-encodes a photo before upload, and the reader then saw R1,58,400.00 instead of ₹1,58,400.00
        row = lambda txt: scan.rows_from_lines([{"text": txt, "conf": 1.0, "box": [0.1, 0.5, 0.3, 0.02]}])[0]["text"]
        self.assertEqual(row("Grand Total: R1,58,400.00"), "Grand Total: ₹1,58,400.00")
        self.assertEqual(row("R2,36,000"), "₹2,36,000")
        self.assertEqual(row("Ref R123"), "Ref R123")            # no grouping: a code, not money
        self.assertEqual(row("TR1,500"), "TR1,500")              # R inside a word stays
        self.assertEqual(ap.extract(row("Grand Total: R1,58,400.00"))["amount_inr"], 158400.0)


class ExtractTest(unittest.TestCase):
    def test_vendor_and_account_name_stop_at_a_column_gap(self):
        x = ap.extract("Vendor: Shadowfax Technologies Limited  Invoice No:  INV/1\nAccount Name: TCI Express Limited  IFSC: UTIB0000456")
        self.assertEqual(x["vendor_name_text"], "Shadowfax Technologies Limited")
        self.assertEqual(x["account_name"], "TCI Express Limited")
        self.assertEqual(x["invoice_no"], "INV/1")


class ScanTest(Base):
    def test_scanned_bill_runs_the_invoice_checks(self):
        scan.read_image = FakeReader(lines_of(BILL_01))
        out = scan.scan_bytes(JPEG)
        self.assertEqual(out["state"], "awaiting_approval")
        self.assertTrue(out["photo"].startswith("scan_") and out["photo"].endswith(".jpg"))
        checks, x = self.checks(out)
        self.assertEqual(checks["sender_domain"]["result"], "n/a")       # paper bill: no email sender
        self.assertEqual(checks["legibility"]["result"], "pass")
        self.assertEqual(checks["three_way_match"]["result"], "pass")
        self.assertEqual(x["scan"]["photo"], out["photo"])
        amount = x["scan"]["fields"]["amount_inr"]
        self.assertIn("2,36,000.00", amount["text"])                      # cited to the photo line it came from
        self.assertEqual(amount["conf"], 1.0)

    def test_photo_and_its_reading_are_kept_as_evidence(self):
        scan.read_image = FakeReader(lines_of(BILL_01))
        out = scan.scan_bytes(JPEG)
        self.assertTrue(os.path.isfile(scan.photo_path(out["photo"])))
        with open(scan.photo_path(out["photo"]), "rb") as f:
            self.assertEqual(f.read(), JPEG)

    def test_same_photo_twice_is_read_once_and_processed_once(self):
        scan.read_image = reader = FakeReader(lines_of(BILL_01))
        first = scan.scan_bytes(JPEG)
        second = scan.scan_bytes(JPEG)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(second["invoice_key"], first["invoice_key"])
        self.assertIn("already processed", second.get("note", ""))
        self.assertEqual(store.db().execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"], 1)

    def test_second_photo_of_the_same_bill_is_a_duplicate(self):
        scan.read_image = FakeReader(lines_of(BILL_01))
        scan.scan_bytes(JPEG)
        again = scan.scan_bytes(JPEG + b" taken again")
        self.assertEqual(again["state"], "duplicate")

    def test_unreadable_amount_is_held_and_not_guessed(self):
        scan.read_image = FakeReader(lines_of(BILL_01.replace("Total Amount Payable: INR 2,36,000.00", "T0ta1 Am0unt ~~ smudge")))
        out = scan.scan_bytes(JPEG)
        self.assertEqual(out["state"], "held")
        self.assertIn("amount", out["reason"])
        self.assertIn("Not guessed", out["reason"])
        self.assertEqual(store.db().execute("SELECT COUNT(*) c FROM payments").fetchone()["c"], 0)

    def test_low_confidence_reading_is_held_for_a_person(self):
        scan.read_image = FakeReader(lines_of(BILL_01, conf={"Total Amount": 0.3}))
        out = scan.scan_bytes(JPEG)
        self.assertEqual(out["state"], "held")
        checks, _ = self.checks(out)
        self.assertEqual(checks["legibility"]["result"], "warn")
        self.assertIn("amount_inr", checks["legibility"]["reasoning"])

    def test_misread_gstin_is_not_called_fake(self):
        scan.read_image = FakeReader(lines_of(BILL_01.replace("29AAMCS1234A1ZM", "29AAMCS1234AIZM")))
        out = scan.scan_bytes(JPEG)
        checks, _ = self.checks(out)
        self.assertEqual(checks["gstin_binding"]["result"], "fail")
        self.assertIn("misread", checks["gstin_binding"]["reasoning"])

    def test_typed_scan_header_does_not_skip_the_sender_check(self):
        # someone pastes text that claims to be a scan: it is treated as an email/text document
        path = os.path.join(self.fix, "inbox", "scan_0123456789abcdef.txt")
        with open(path, "w") as f:
            f.write("Scanned-From: scan_0123456789abcdef.jpg\nFrom: x <a@shadowfaxx.in>\n" + BILL_01)
        out = ap.process_file(path)
        checks, x = self.checks(out)
        self.assertNotIn("scan", x)
        self.assertEqual(checks["sender_domain"]["result"], "fail")

    def test_not_a_photo_is_refused(self):
        with self.assertRaises(scan.ScanError):
            scan.scan_bytes(b"%PDF-1.7 not an image")

    def test_scan_is_recorded_in_the_cost_ledger_at_zero(self):
        scan.read_image = FakeReader(lines_of(BILL_01))
        out = scan.scan_bytes(JPEG)
        rows = store.ledger_for("inv:" + out["photo"].rsplit(".", 1)[0] + ".txt")["calls"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["endpoint"].startswith("local_ocr/"))
        self.assertEqual(rows[0]["credits"], 0)


BILLS = os.path.join(FIX, "bills")
PHOTOS = ["01_shadowfax_invoice", "02_shreeji_invoice", "03_tci_invoice", "04_shadowfax_invoice_resent", "06_tci_invoice_new_account"]


@unittest.skipUnless(scan.reader_name() and os.path.isdir(BILLS), "no text reader on this machine, or no test photos")
class RealPhotoTest(Base):
    """Photos of the paper version of each inbox invoice end where the emailed text version ends."""

    def outcome(self, out):
        checks, _ = self.checks(out)
        bad = {k for k, c in checks.items() if c["result"] in ("fail", "warn", "unavailable")} - {"sender_domain", "legibility"}
        return out["state"], bad

    def test_photos_reach_the_same_decision_as_the_emailed_text(self):
        text_side = []
        for name in PHOTOS:
            shutil.copy(os.path.join(FIX, "inbox", name + ".txt"), os.path.join(self.fix, "inbox"))
            text_side.append(self.outcome(ap.process_file(os.path.join(self.fix, "inbox", name + ".txt"))))
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "photos.db")
        for name, expected in zip(PHOTOS, text_side):
            with open(os.path.join(BILLS, name + ".jpg"), "rb") as f:
                out = scan.scan_bytes(f.read())
            with self.subTest(photo=name):
                got = self.outcome(out)
                checks, x = self.checks(out)
                self.assertEqual(checks["legibility"]["result"], "pass", checks["legibility"]["reasoning"])
                self.assertEqual(got, expected, x)

    def test_blurred_dark_photo_is_held_as_cannot_read(self):
        with open(os.path.join(BILLS, "99_unreadable.jpg"), "rb") as f:
            out = scan.scan_bytes(f.read())
        self.assertEqual(out["state"], "held")
        self.assertIn("Not guessed", out["reason"])


if __name__ == "__main__":
    unittest.main()
