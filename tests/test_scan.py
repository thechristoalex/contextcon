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

    def test_tilted_page_keeps_neighbouring_printed_lines_apart(self):
        # a hand-held photo tilted 4 degrees: each value sits as low as the next line's label, so grouping by
        # height alone mixes the lines. The reader's corner points give the tilt, which is taken out first.
        import math
        W, H, a = 1000, 1400, math.radians(4)

        def item(text, x, y, w, h=24):
            pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            rot = [(px * math.cos(a) - py * math.sin(a), px * math.sin(a) + py * math.cos(a)) for px, py in pts]
            xs, ys = [p[0] for p in rot], [p[1] for p in rot]
            return {"text": text, "conf": 1.0, "box": [min(xs) / W, min(ys) / H, (max(xs) - min(xs)) / W, (max(ys) - min(ys)) / H],
                    "quad": [c for px, py in rot for c in (px / W, py / H)]}
        lines = [item("Bill To", 80, 240, 90), item("Invoice No:", 560, 240, 140), item("TCIE/26-27/3390", 760, 240, 200),
                 item("Swiftline Logistics Pvt Ltd", 80, 280, 330), item("Invoice Date:", 560, 280, 150), item("16 Nov 2026", 760, 280, 140),
                 item("Grand Total:", 460, 590, 150), item("₹1,58,400.00", 760, 590, 200)]
        self.assertEqual([r["text"] for r in scan.rows_from_lines(lines, size=[W, H])],
                         ["Bill To  Invoice No:  TCIE/26-27/3390", "Swiftline Logistics Pvt Ltd  Invoice Date:  16 Nov 2026",
                          "Grand Total:  ₹1,58,400.00"])

    def test_stray_mark_read_for_the_rupee_sign_before_a_grouped_amount_becomes_rupee(self):
        row = lambda txt: scan.rows_from_lines([{"text": txt, "conf": 1.0, "box": [0.1, 0.5, 0.3, 0.02]}])[0]["text"]
        self.assertEqual(row("Grand Total:  ·1,58,400.00"), "Grand Total:  ₹1,58,400.00")
        self.assertEqual(row("Grand Total:  *2,36,000"), "Grand Total:  ₹2,36,000")
        self.assertEqual(row("Item 3-1,500 units"), "Item 3-1,500 units")    # not after a space: left alone
        self.assertEqual(ap.extract(row("Grand Total:  ·1,58,400.00"))["amount_inr"], 158400.0)

    def test_colon_misread_as_semicolon_after_a_label_is_a_colon(self):
        # read from a dark, tilted photo: "(Qty; 120)"
        row = lambda txt: scan.rows_from_lines([{"text": txt, "conf": 1.0, "box": [0.1, 0.5, 0.3, 0.02]}])[0]["text"]
        self.assertEqual(row("Freight services (Qty; 120)"), "Freight services (Qty: 120)")
        self.assertEqual(ap.extract(row("Freight services (Qty; 120)"))["qty"], 120.0)
        self.assertEqual(row("a;b ; c"), "a;b ; c")             # not glued to a word and followed by a space: left alone

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

    def tally_reader(self):
        with open(os.path.join(FIX, "ocr", "tally_einvoice.json")) as f:
            d = json.load(f)
        return lambda path: {"reader": "fake", "lines": d["lines"], "size": d["size"]}

    def test_indian_bill_is_read_into_the_invoice_with_the_sellers_gstin(self):
        scan.read_image = self.tally_reader()
        out = scan.scan_bytes(JPEG)
        _, x = self.checks(out)
        self.assertEqual(x["invoice_no"], "SHB/456/20")
        self.assertEqual(x["amount_inr"], 4130.0)
        self.assertEqual(x["gstin"], "29AACCT3705E000")                    # the seller's, not the buyer's 29AAFFC8126N1ZZ
        self.assertEqual(x["vendor_name_text"], "Surabhi Hardwares, Bangalore")
        self.assertEqual(x["bill"]["fields"]["buyer.gstin"]["value"], "29AAFFC8126N1ZZ")
        self.assertEqual(x["scan"]["fields"]["amount_inr"]["status"], "checked")

    def test_rules_and_jev_disagreeing_holds_the_bill(self):
        from vendorcheck import bill, jev
        scan.read_image = self.tally_reader()
        real = jev.ask
        seen = {}
        real_review = bill.review_with_jev

        def review(b, job):
            seen["bill"] = b
            return real_review(b, job)

        def ask(job_id, state, questions):
            if not all(q.get("type") == "choice" and "o1" in q.get("criteria", {}) for q in questions.values()):
                raise jev.Unavailable("only the bill review is faked here")
            # Jev picks a candidate other than the rules' pick, every time
            return {k: {"choice": next(o for o in q["criteria"] if o not in ("none", bill.jev_rule_choice(seen["bill"], k))),
                        "confidence": 0.95} for k, q in questions.items()}, {"model": "jev-test"}
        bill.review_with_jev = review
        self.addCleanup(setattr, bill, "review_with_jev", real_review)
        jev.ask = ask
        try:
            out = scan.scan_bytes(JPEG)
        finally:
            jev.ask = real
        checks, x = self.checks(out)
        self.assertEqual(out["state"], "held")
        self.assertEqual(checks["legibility"]["result"], "warn")
        self.assertIn("Jev", checks["legibility"]["reasoning"])

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

    def test_hand_held_shots_read_every_field_of_the_bill(self):
        # tilted 7 degrees; shot at an angle (keystone); dark and tilted -5 degrees
        for photo, source in (("10_hand_tilt7", "01_shadowfax_invoice"), ("11_hand_keystone", "02_shreeji_invoice"),
                              ("12_hand_dark_tilt", "06_tci_invoice_new_account")):
            want = ap.extract(open(os.path.join(FIX, "inbox", source + ".txt")).read())
            r = scan.read_image(os.path.join(BILLS, photo + ".jpg"))
            got = ap.extract("\n".join(x["text"] for x in scan.rows_from_lines(r["lines"], size=r.get("size"))))
            with self.subTest(photo=photo):
                for k in ("invoice_no", "po_no", "amount_inr", "qty", "gstin", "account_no", "ifsc"):
                    self.assertEqual(got[k], want[k], k)

    def test_blurred_dark_photo_is_held_as_cannot_read(self):
        with open(os.path.join(BILLS, "99_unreadable.jpg"), "rb") as f:
            out = scan.scan_bytes(f.read())
        self.assertEqual(out["state"], "held")
        self.assertIn("Not guessed", out["reason"])


if __name__ == "__main__":
    unittest.main()
