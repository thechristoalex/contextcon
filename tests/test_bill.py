"""Reading an Indian GST invoice from what the photo reader saw. No network: Jev is replaced where a test needs it.

    python3 -m unittest tests.test_bill -v

The fixtures in data/fixtures/ocr/ are the reader's output (text, confidence, position) for real phone
photos of a TallyPrime e-invoice, a Vyapar bill and a blurred shot. They hold no image.
"""
try:
    from . import offline  # noqa: F401  keeps tests off live APIs; must come before vendorcheck
except ImportError:
    import offline  # noqa: F401
import json
import os
import unittest

from vendorcheck import bill, jev, pipeline

OCR = os.path.join(os.path.dirname(__file__), "..", "data", "fixtures", "ocr")


def load(name):
    with open(os.path.join(OCR, name + ".json")) as f:
        d = json.load(f)
    return bill.parse(d["lines"], d["size"])


def val(b, key):
    f = b["fields"].get(key)
    return f["value"] if f else None


class TallyEInvoiceTest(unittest.TestCase):
    """TallyPrime e-invoice: values sit in the cell below their labels; seller, consignee and buyer each have a GSTIN."""

    @classmethod
    def setUpClass(cls):
        cls.b = load("tally_einvoice")

    def test_document_type(self):
        self.assertEqual(self.b["doc_type"], "tax_invoice")

    def test_invoice_number_and_date_are_read_from_the_cells_below_their_labels(self):
        self.assertEqual(val(self.b, "invoice_no"), "SHB/456/20")
        self.assertEqual(val(self.b, "invoice_date"), "2020-12-20")

    def test_seller_is_the_issuer_not_the_buyer(self):
        self.assertEqual(val(self.b, "seller.name"), "Surabhi Hardwares, Bangalore")
        self.assertEqual(val(self.b, "seller.gstin"), "29AACCT3705E000")
        self.assertEqual(val(self.b, "seller.state"), "Karnataka (29)")

    def test_a_gstin_that_fails_the_format_is_shown_as_printed_and_marked_invalid(self):
        f = self.b["fields"]["seller.gstin"]
        self.assertFalse(f["valid"])
        self.assertEqual(f["status"], "invalid")
        self.assertIn("not a valid GSTIN", f["note"])

    def test_buyer_and_consignee_get_their_own_gstin(self):
        self.assertEqual(val(self.b, "buyer.name"), "Kiran Enterprises")
        self.assertEqual(val(self.b, "buyer.gstin"), "29AAFFC8126N1ZZ")
        self.assertEqual(val(self.b, "consignee.name"), "Kiran Enterprises")
        self.assertEqual(val(self.b, "consignee.gstin"), "29AAFFC8126N1ZZ")
        self.assertEqual(self.b["fields"]["buyer.gstin"]["valid"], pipeline.gstin_valid("29AAFFC8126N1ZZ"))

    def test_e_invoice_reference_numbers(self):
        irn = val(self.b, "irn")
        self.assertTrue(irn.startswith("fef1df90406b928db26a62f816debc9bb5256d9375e6"), irn)
        self.assertTrue(irn.endswith("0dc4226653cc23a8c595"), irn)       # O read for 0 in a hex number, fixed
        self.assertEqual(val(self.b, "ack_no"), "112010036563310")
        self.assertEqual(val(self.b, "ack_date"), "2020-12-21")

    def test_line_item_is_read_column_by_column(self):
        self.assertEqual(len(self.b["items"]), 1)
        it = self.b["items"][0]
        self.assertEqual((it["description"], it["hsn"], it["qty"], it["unit"], it["rate"], it["amount"]),
                         ("12MM**", "1005", 7.0, "No", 500.0, 3500.0))

    def test_tax_and_total(self):
        self.assertEqual(val(self.b, "taxable_value"), 3500.0)
        self.assertEqual(val(self.b, "cgst"), 315.0)
        self.assertEqual(val(self.b, "sgst"), 315.0)
        self.assertEqual(val(self.b, "cgst_rate"), 9.0)
        self.assertIsNone(val(self.b, "igst"))
        self.assertEqual(val(self.b, "total"), 4130.0)
        self.assertEqual(val(self.b, "total_in_words"), 4130.0)

    def test_total_is_confirmed_by_the_words_and_the_tax_arithmetic(self):
        checks = {c["name"]: c for c in self.b["checks"]}
        self.assertTrue(checks["total_matches_words"]["ok"])
        self.assertTrue(checks["taxable_plus_tax_equals_total"]["ok"])
        self.assertTrue(checks["cgst_equals_sgst"]["ok"])
        self.assertTrue(checks["items_add_up_to_taxable"]["ok"])
        self.assertEqual(self.b["fields"]["total"]["status"], "checked")


class VyaparBillTest(unittest.TestCase):
    """Vyapar-style bill: labels with values on the same line, ₹ misread as other currency signs, decoy balances."""

    @classmethod
    def setUpClass(cls):
        cls.b = load("vyapar_bill")

    def test_invoice_and_due_date_are_not_mixed_up(self):
        self.assertEqual(val(self.b, "invoice_no"), "S10")
        self.assertEqual(val(self.b, "invoice_date"), "2026-03-29")
        self.assertEqual(val(self.b, "due_date"), "2026-04-13")

    def test_gstin_under_bill_to_is_the_buyers_and_is_flagged_invalid(self):
        self.assertEqual(val(self.b, "buyer.name"), "Shyam Fruits")
        self.assertEqual(val(self.b, "buyer.gstin"), "XYZ12345XA12")
        self.assertFalse(self.b["fields"]["buyer.gstin"]["valid"])
        self.assertIsNone(val(self.b, "seller.gstin"))                   # the seller's GSTIN is not printed

    def test_seller_name_comes_from_the_signatory_line(self):
        self.assertEqual(val(self.b, "seller.name"), "Balaji Dry Fruits")
        self.assertEqual(val(self.b, "consignee.name"), "Shyam Fruits")
        self.assertEqual(val(self.b, "place_of_supply"), "Rajasthan")

    def test_three_line_items(self):
        got = [(i["description"], i["hsn"], i["qty"], i["unit"], i["rate"], i["amount"]) for i in self.b["items"]]
        self.assertEqual(got, [("Almond", "8135010", 1.0, "KG", 904.76, 950.0),
                               ("Cashew", "8135010", 1.0, "Piece", 857.14, 900.0),
                               ("Raisin", "8135010", 1.0, "KG", 428.57, 450.0)])

    def test_total_is_the_invoice_total_not_a_balance(self):
        self.assertEqual(val(self.b, "taxable_value"), 2190.47)
        self.assertEqual(val(self.b, "cgst"), 54.77)
        self.assertEqual(val(self.b, "sgst"), 54.77)                      # printed "8 54.77": the 8 is a misread ₹
        self.assertEqual(val(self.b, "cgst_rate"), 2.5)
        self.assertEqual(val(self.b, "total"), 2300.0)
        self.assertEqual(val(self.b, "total_in_words"), 2300.0)
        checks = {c["name"]: c for c in self.b["checks"]}
        self.assertTrue(checks["taxable_plus_tax_equals_total"]["ok"])   # 2190.47 + 54.77 + 54.77 = 2300.01
        self.assertTrue(checks["total_matches_words"]["ok"])

    def test_bank_details(self):
        self.assertEqual(val(self.b, "bank.account_holder"), "Balaji Fruits Wholesalers")
        self.assertEqual(val(self.b, "bank.account_no"), "1234567890")
        self.assertEqual(val(self.b, "bank.name"), "State Bank Of India")
        self.assertEqual(val(self.b, "bank.branch"), "Jaipur")
        self.assertEqual(val(self.b, "bank.upi"), "9981278197@hdfcbank")
        self.assertEqual(val(self.b, "bank.ifsc"), "SBIN000123")
        self.assertFalse(self.b["fields"]["bank.ifsc"]["valid"])          # 10 characters; an IFSC has 11

    def test_the_bank_name_is_not_taken_for_a_state(self):
        for role in ("seller", "buyer", "consignee"):
            self.assertNotIn("Bank", str(val(self.b, f"{role}.state")))

    def test_payee_name_differing_from_the_seller_is_reported(self):
        checks = {c["name"]: c for c in self.b["checks"]}
        self.assertFalse(checks["account_holder_is_seller"]["ok"])


class WebIndiaFilingsTest(unittest.TestCase):
    """Public sample (IndiaFilings / LEDGERS): taxes spelled out in full, seller name above its address."""

    @classmethod
    def setUpClass(cls):
        cls.b = load("web_indiafilings")

    def test_fields(self):
        self.assertEqual(val(self.b, "seller.name"), "VERVE Financial Services Private Limited")
        self.assertEqual(val(self.b, "seller.gstin"), "33AABCL398G2ZH")
        self.assertFalse(self.b["fields"]["seller.gstin"]["valid"])        # printed with 14 characters
        self.assertEqual(val(self.b, "buyer.name"), "Ajith Kumar")
        self.assertEqual(val(self.b, "invoice_no"), "2017-40")
        self.assertEqual(val(self.b, "invoice_date"), "2017-07-20")
        self.assertEqual(val(self.b, "due_date"), "2017-07-20")
        self.assertEqual(val(self.b, "taxable_value"), 4152.0)
        self.assertEqual(val(self.b, "cgst"), 373.68)
        self.assertEqual(val(self.b, "sgst"), 373.68)
        self.assertEqual(val(self.b, "total"), 4899.36)
        checks = {c["name"]: c for c in self.b["checks"]}
        self.assertTrue(checks["taxable_plus_tax_equals_total"]["ok"])


class WebGstZenTest(unittest.TestCase):
    """Public sample (GSTZen): inter-state IGST in the items table, two-line items, words without 'Rupees'."""

    @classmethod
    def setUpClass(cls):
        cls.b = load("web_gstzen")

    def test_parties(self):
        self.assertEqual(val(self.b, "seller.name"), "CloudZen Software Labs Pvt. Ltd.")
        self.assertEqual(val(self.b, "seller.gstin"), "20QXOCC9424D1Z5")
        self.assertEqual(val(self.b, "buyer.name"), "Cipla Ltd")
        self.assertEqual(val(self.b, "buyer.gstin"), "08AKOCX6349P1ZL")
        self.assertEqual(val(self.b, "consignee.gstin"), "08AKOCX6349P1ZL")

    def test_invoice(self):
        self.assertEqual(val(self.b, "invoice_no"), "17-18/JH/97")
        self.assertEqual(val(self.b, "invoice_date"), "2017-07-26")

    def test_amounts_from_the_items_table_total_row(self):
        self.assertEqual(val(self.b, "taxable_value"), 38991.0)
        self.assertEqual(val(self.b, "igst"), 8933.68)
        self.assertEqual(val(self.b, "round_off"), 0.32)
        self.assertEqual(val(self.b, "total"), 47925.0)
        self.assertEqual(val(self.b, "total_in_words"), 47925.0)
        checks = {c["name"]: c for c in self.b["checks"]}
        self.assertTrue(checks["taxable_plus_tax_equals_total"]["ok"])
        self.assertTrue(checks["total_matches_words"]["ok"])
        self.assertTrue(checks["tax_type_matches_states"]["ok"])           # Jharkhand -> Rajasthan: IGST

    def test_two_line_items(self):
        got = [(i["hsn"], i["qty"], i["unit"], i["rate"], i["taxable"], i["amount"]) for i in self.b["items"]]
        self.assertEqual(got[0], ("521222", 9.0, "GMS", 1344.0, 12096.0, 14273.28))
        self.assertEqual(len(got), 5)
        self.assertTrue(self.b["items"][2]["description"].startswith("GLAZIERS"))


class BlankTemplateTest(unittest.TestCase):
    def test_an_empty_form_reads_no_values(self):
        b = load("web_blank_template")
        self.assertFalse(b["readable"])
        for k, f in b["fields"].items():
            self.assertNotRegex(str(f["value"]), r"^(Name|Address|State|Date)$", k)


class BlurryPhotoTest(unittest.TestCase):
    def test_blurred_photo_reads_nothing_and_says_why(self):
        b = load("blurry_photo")
        self.assertIsNone(val(b, "total"))
        self.assertIsNone(val(b, "invoice_no"))
        self.assertFalse(b["readable"])
        self.assertIn("blurr", b["quality_note"].lower())


class WordsTest(unittest.TestCase):
    def test_indian_amounts_in_words(self):
        self.assertEqual(bill.words_to_number("Indian Rupee Four Thousand One Hundred Thirty Only"), 4130)
        self.assertEqual(bill.words_to_number("INR Two Thousand Three Hundred Only"), 2300)
        self.assertEqual(bill.words_to_number("Rupees Two Lakh Thirty Six Thousand Only"), 236000)
        self.assertEqual(bill.words_to_number("Rupees One Crore Five Lakh and Fifty Paise Only"), 10500000.5)
        self.assertEqual(bill.words_to_number("Rupees Ninety-Nine Only"), 99)
        self.assertIsNone(bill.words_to_number("Thank you for your business"))


class GstinTest(unittest.TestCase):
    def test_a_letter_read_for_a_digit_is_fixed_only_when_the_check_character_then_passes(self):
        good = "29AAFFC8126N1ZZ"
        if not pipeline.gstin_valid(good):
            good = next(g for g in ("27AAPFU0939F1ZV", "29AAGCB7383J1Z4") if pipeline.gstin_valid(g))
        misread = good[:2].replace("2", "Z") + good[2:]                  # 2 read as Z in a digit position
        fixed, note = bill.fix_gstin(misread)
        self.assertEqual(fixed, good)
        self.assertIn("misread", note)
        self.assertEqual(bill.fix_gstin("XYZ12345XA12"), ("XYZ12345XA12", None))


class JevReviewTest(unittest.TestCase):
    """Jev picks among candidates that code found. Rules decide; Jev confirms or raises a conflict."""

    def setUp(self):
        self.real = jev.ask

    def tearDown(self):
        jev.ask = self.real

    def fake(self, choose):
        def ask(job_id, state, questions):
            self.seen = (state, questions)
            return {k: {"choice": choose(k, q), "confidence": 0.95} for k, q in questions.items()}, {"model": "jev-test", "request_id": "req_test", "cache_hit": False}
        jev.ask = ask

    def test_agreement_marks_fields_confirmed_and_jev_never_sees_tax_or_bank_numbers(self):
        b = load("tally_einvoice")
        self.fake(lambda k, q: bill.jev_rule_choice(b, k))
        bill.review_with_jev(b, "test")
        self.assertEqual(b["fields"]["seller.gstin"]["jev"], "agrees")
        self.assertEqual(b["fields"]["total"]["jev"], "agrees")
        sent = json.dumps(self.seen)
        for secret in ("29AACCT3705E000", "29AAFFC8126N1ZZ", "112010036563310"):
            self.assertNotIn(secret, sent)

    def test_disagreement_without_other_evidence_holds_the_field_for_a_person(self):
        b = load("tally_einvoice")
        self.fake(lambda k, q: [o for o in q["criteria"] if o not in (bill.jev_rule_choice(b, k), "other", "none")][0])
        bill.review_with_jev(b, "test")
        self.assertEqual(b["fields"]["seller.name"]["status"], "conflict")
        self.assertEqual(b["fields"]["total"]["status"], "checked")       # words and arithmetic outweigh Jev

    def test_jev_unavailable_keeps_the_rules(self):
        b = load("vyapar_bill")
        jev.ask = lambda *a: (_ for _ in ()).throw(jev.Unavailable("off"))
        bill.review_with_jev(b, "test")
        self.assertEqual(val(b, "total"), 2300.0)
        self.assertEqual(b["jev"], "unavailable: off")


if __name__ == "__main__":
    unittest.main()
