"""Read an Indian GST invoice from the lines a photo reader saw (text, confidence, position).

Accuracy before speed. Three layers:

1. Read candidates with the page layout in mind. A label's value is on its right, or in the cell below it
   (Tally, Busy and Vyapar all do both). A GSTIN belongs to the party whose block it sits in: under
   "Bill to"/"Buyer" it is the buyer's, under "Ship to"/"Consignee" the consignee's, otherwise the seller's.
   The items table is read column by column from its header.
2. Check the candidates against each other: the GSTIN check character, taxable value + CGST + SGST
   (or IGST) = total, the total in words = the total in figures, CGST = SGST, the tax rate against the
   taxable value, the IFSC format. The total is chosen by this evidence, never by position alone.
3. Ask Jev (review_with_jev) to choose among the candidates code found: which GSTIN is the seller's,
   which name is the seller, which amount is the total, which number is the invoice number. Jev sees no
   tax ID, account or phone number. Agreement confirms a field. Disagreement holds the field for a
   person, unless printed evidence (words, arithmetic, check character) already settles it.

Every value keeps the photo line it came from, so the case page can outline it on the photo.
Nothing is guessed: a field that cannot be read is absent, and one that fails its format is shown as
printed and marked invalid.
"""
import datetime as dt
import math
import re
import statistics

from . import pipeline

# ---------------- geometry: straighten the page, then rows and columns ----------------


def tilt(lines, W, H):
    """The page's tilt in radians: median slope of the long lines' top edges, in pixels."""
    angles = []
    for ln in lines:
        q = ln.get("quad")
        if q and abs(q[2] - q[0]) > 0.15:
            angles.append(math.atan2((q[3] - q[1]) * H, (q[2] - q[0]) * W))
    return statistics.median(angles) if angles else 0.0


def _items(lines, size):
    W, H = size or (1, 1)
    a = tilt(lines, W, H) if size else 0.0
    ca, sa = math.cos(-a), math.sin(-a)
    out = []
    for i, ln in enumerate(lines):
        x, y, w, h = ln["box"]
        q = ln.get("quad")
        cx, cy = (x + w / 2) * W, (y + h / 2) * H
        if size and q:
            width = math.hypot((q[2] - q[0]) * W, (q[3] - q[1]) * H)
            lh = math.hypot((q[6] - q[0]) * W, (q[7] - q[1]) * H)
        else:
            width, lh = w * W, h * H
        rx, ry = cx * ca - cy * sa, cx * sa + cy * ca
        out.append({"i": i, "text": ln["text"].strip(), "conf": ln["conf"], "box": ln["box"],
                    "x0": rx - width / 2, "x1": rx + width / 2, "y": ry, "h": max(lh, 1e-9)})
    return out, a


def _rows(items, straight):
    k = 0.6 if straight else 0.75
    rows = []
    for it in sorted(items, key=lambda t: t["y"]):
        if rows and abs(it["y"] - rows[-1]["y"]) < k * min(it["h"], rows[-1]["h"]):
            r = rows[-1]
            r["items"].append(it)
            r["y"] = sum(t["y"] for t in r["items"]) / len(r["items"])
        else:
            rows.append({"y": it["y"], "h": it["h"], "items": [it]})
    for n, r in enumerate(rows):
        r["items"].sort(key=lambda t: t["x0"])
        r["n"] = n
        for t in r["items"]:
            t["row"] = n
        r["text"] = "  ".join(t["text"] for t in r["items"])
    return rows


# ---------------- text helpers ----------------

RUPEE = r"(?:₹|rs\.?|inr|[^\w\s,.%()])?"
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\d,]*\d)(?!\s*%)")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def numbers(text):
    return [float(m.replace(",", "")) for m in NUM_RE.findall(text or "")]


def parse_date(s):
    """Day-first Indian dates: 20-Dec-20, 29 Mar 2026, 10/11/2026, 10.11.26, 2026-11-10 -> ISO, or None."""
    s = (s or "").strip()
    for pat, order in ((r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", "ymd"), (r"(\d{1,2})[-/. ]+([A-Za-z]{3,9})[-/., ]+(\d{2,4})", "dMy"),
                       (r"([A-Za-z]{3,9})[ .]+(\d{1,2}),?[ ]+(\d{4})", "Mdy"), (r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", "dmy")):
        m = re.search(pat, s)
        if not m:
            continue
        g = m.groups()
        try:
            if order == "ymd":
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            elif order == "dMy":
                d, mo, y = int(g[0]), MONTHS.get(g[1][:3].lower()), int(g[2])
            elif order == "Mdy":
                mo, d, y = MONTHS.get(g[0][:3].lower()), int(g[1]), int(g[2])
            else:
                d, mo, y = int(g[0]), int(g[1]), int(g[2])
            if not mo:
                continue
            y = y + 2000 if y < 100 else y
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def show_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {MON[d.month]} {d.year}"


def inr(v):
    """2,36,000.00 (Indian grouping)."""
    neg, v = v < 0, abs(v)
    whole, frac = f"{v:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1]) if head else ""
    return ("-" if neg else "") + (head + "," if head else "") + tail + "." + frac


_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                     "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALE = {"hundred": 100, "thousand": 1000, "lakh": 100000, "lakhs": 100000, "lac": 100000, "lacs": 100000,
          "crore": 10000000, "crores": 10000000, "million": 1000000}


def _words_int(tokens):
    total, cur, seen = 0, 0, False
    for t in tokens:
        if t in _ONES:
            cur += _ONES[t]
        elif t in _TENS:
            cur += _TENS[t]
        elif t == "hundred":
            cur = (cur or 1) * 100
        elif t in _SCALE:
            total += (cur or 1) * _SCALE[t]
            cur = 0
        elif t == "and":
            continue
        else:
            return None
        seen = True
    return total + cur if seen else None


def words_to_number(s):
    """'Indian Rupee Four Thousand One Hundred Thirty Only' -> 4130; lakh and crore; '... and Fifty Paise' -> .50."""
    t = re.sub(r"[^a-z ]", " ", (s or "").lower().replace("-", " "))
    m = re.search(r"(?:indian\s+)?(?:rupees?|inr|rs)\s+(.*?)\s*(?:only|$)", t) or re.search(r"in\s+words\s+(.*?)\s*(?:only|$)", t)
    if not m:
        return None
    body = m.group(1)
    paise = None
    if re.search(r"\bpaise\b", body):
        pm = re.match(r"(.*?)\s*\band\s+([a-z ]+?)\s+paise", body) or re.match(r"()([a-z ]+?)\s+paise", body)
        if pm:
            paise = _words_int(pm.group(2).split())
            body = pm.group(1)
    whole = _words_int(body.split())
    if whole is None:
        return None
    return whole + (paise or 0) / 100 if paise else whole


def _norm_name(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(r"\b(m/?s|messrs|pvt|private|ltd|limited|llp|co|company|the|and)\b", " ", s)
    return " ".join(s.split())


def same_name(a, b):
    a, b = _norm_name(a), _norm_name(b)
    return bool(a and b) and (a == b or a in b or b in a)


# ---------------- GSTIN, IFSC ----------------

_TO_DIGIT = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"})
_TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "7": "T", "4": "A"})
GSTIN_SHAPE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")


def fix_gstin(printed):
    """Fix letters and digits read in a position that must be the other kind, only if the check character then passes."""
    s = re.sub(r"[\s:.\-]", "", (printed or "").upper())
    if len(s) != 15 or pipeline.gstin_valid(s):
        return s, None
    f = list(s)
    for i in (0, 1, 7, 8, 9, 10):
        f[i] = f[i].translate(_TO_DIGIT)
    for i in (2, 3, 4, 5, 6, 11):
        f[i] = f[i].translate(_TO_LETTER)
    if f[13] == "2":
        f[13] = "Z"
    fixed = "".join(f)
    if fixed != s and pipeline.gstin_valid(fixed):
        return fixed, f"read from the photo as {s}; a letter and digit were misread in fixed positions, corrected and the check character now passes"
    return s, None


def gstin_problem(g):
    if len(g) != 15:
        return f"{len(g)} characters; a GSTIN has 15"
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g):
        return "does not follow the GSTIN pattern (2 digits, PAN, entity number, Z, check character)"
    if not pipeline.gstin_valid(g):
        return "check character does not match"
    return None


# ---------------- labels ----------------

L = {
    "invoice_no": r"(?:tax\s*)?invoice\s*(?:no|number|num|#)\.?|inv\.?\s*(?:no|#)\.?|bill\s*(?:no|number)\.?|voucher\s*no\.?",
    "invoice_date": r"(?:invoice|inv\.?|bill)\s*date|date\s*of\s*(?:invoice|issue)|dated|date",
    "due_date": r"due\s*date|payment\s*due(?:\s*date)?",
    "ack_no": r"ack\.?\s*(?:no|number)\.?",
    "ack_date": r"ack\.?\s*date",
    "irn": r"irn(?:\s*no\.?)?",
    "eway_bill": r"e-?\s*way\s*bill\s*(?:no|number)?\.?",
    "po_no": r"buyer'?s\s*order\s*(?:no|number)\.?|purchase\s*order\s*(?:no|number)?\.?|p\.?\s*o\.?\s*(?:no|number|#)\.?|order\s*no\.?",
    "place_of_supply": r"place\s*of\s*supply",
    "gstin": r"gstin(?:\s*/\s*uin)?(?:\s*no\.?)?|gst\s*(?:no|number|in)\.?|gst\s*reg(?:istration)?\.?\s*no\.?",
    "pan": r"pan(?:\s*(?:no|number))?\.?",
    "state": r"state\s*name|state\s*code|state(?=\s*[:\-])",
    "seller_label": r"vendor|supplier|seller|billed\s*by|sold\s*by|from\s*company",
    "taxable_value": r"taxable\s*(?:amount|value)|total\s*taxable(?:\s*value)?|taxable",
    "round_off": r"round(?:ed|ing)?\s*off|rounding(?:\s*off)?",
    "qty": r"qty|quantity|total\s*qty",
    "bank.account_no": r"(?:bank\s*)?(?:a/?c|acc(?:ount)?)\.?\s*(?:no|number|num|#)\.?",
    "bank.ifsc": r"ifsc(?:\s*code)?|rtgs\s*/?\s*neft\s*ifsc",
    "bank.account_holder": r"(?:account|a/?c)\s*(?:holder(?:'?s)?\s*name|holder|name)|beneficiary(?:\s*name)?",
    "bank.name": r"bank\s*name|bank",
    "bank.branch": r"branch(?:\s*name)?",
}
LRE = {k: re.compile(r"(?<![a-z])(?:" + v + r")(?![a-z])\s*[:.\-]?\s*", re.I) for k, v in L.items()}
TAX_RE = {"cgst": r"cgst|central\s*(?:goods\s*and\s*services\s*)?tax", "sgst": r"sgst|state\s*(?:goods\s*and\s*services\s*)?tax",
          "utgst": r"utgst|union\s*territory\s*(?:goods\s*and\s*services\s*)?tax",
          "igst": r"igst|integrated\s*(?:goods\s*and\s*services\s*)?tax", "cess": r"cess|gst\s*compensation\s*cess|compensation\s*cess"}
TAX_LRE = {k: re.compile(r"^(?:output\s*)?(?:" + v + r")\b", re.I) for k, v in TAX_RE.items()}
TOTAL_RE = re.compile(r"^(?:grand\s*total|total\s*amount(?:\s*(?:payable|due|after\s*tax))?|total\s*invoice\s*(?:value|amount)|invoice\s*(?:total|value|amount)|"
                      r"net\s*(?:amount|payable|total)|amount\s*(?:payable|due)|total\s*payable|balance\s*due|total)\b", re.I)
NOT_TOTAL_RE = re.compile(r"sub\s*total|total\s*tax|tax\s*amount|total\s*qty|taxable|in\s*words|previous|current|received|total\s*discount", re.I)
HEAD_CONSIGNEE = re.compile(r"\b(ship(?:ped)?\s*to|consignee|deliver(?:y|ed)?\s*(?:to|address)|details\s*of\s*consignee)\b", re.I)
HEAD_BUYER = re.compile(r"\b(bill(?:ed)?\s*to|buyer|customer|sold\s*to|details\s*of\s*receiver|receiver)\b", re.I)
HEAD_NOT = re.compile(r"order|gstin|state|code|no\.|number|date|pan|address\s*:", re.I)
SIGN_RE = re.compile(r"^(?:for|authori[sz]ed\s+signat\w*\s+for|signature\s+for)\s*[:\-]?\s*(.*)$", re.I)

# anything that reads as a label on its own: never taken as a value
LABEL_WORDS = re.compile(r"^(?:" + "|".join(L.values()) + r"|delivery\s*note(?:\s*date)?|mode\s*/?\s*terms\s*of\s*payment|\w*de/terms\s*of\s*payment|"
                         r"terms\s*of\s*(?:payment|delivery)|reference\s*no\.?(?:\s*&\s*date\.?)?|other\s*references?|dispatch(?:ed)?\s*(?:doc\s*no\.?|through)|"
                         r"destination|code|hsn(?:\s*/\s*sac)?|sac|rate|per|amount|total|disc\.?\s*%?|description(?:\s*of\s*goods)?|items?|"
                         r"particulars|sl\.?|si|no\.?|bill\s*to|ship\s*to|consignee(?:\s*\(ship\s*to\))?|buyer(?:\s*\(bill\s*to\))?|vehicle\s*no\.?|"
                         r"bank\s*details|central\s*tax|state\s*tax|tax\s*amount|value|e\.?\s*&\s*o\.?\s*e|name|address|contact\s*no\.?|phone\s*no\.?|mobile|email(?:\s*id)?)\s*[:.\-]?$", re.I)


def is_label(text):
    raw = (text or "").strip()
    if raw.endswith(":") and len(raw) < 45:
        return True
    t = raw.strip(" :.-·")
    return bool(t) and bool(LABEL_WORDS.match(t))


# ---------------- the reader ----------------

class _Page:
    def __init__(self, lines, size):
        self.items, a = _items(lines, size)
        self.rows = _rows(self.items, bool(a))
        hs = [t["h"] for t in self.items] or [1]
        self.lh = statistics.median(hs)
        self.W = (size or (1, 1))[0]
        self.H = (size or (1, 1))[1]

    def right(self, it):
        return [t for t in self.rows[it["row"]]["items"] if t["x0"] > it["x0"] + 1e-9 and t is not it]

    def below(self, it, depth=3.2):
        def overlap(t):
            return min(t["x1"], it["x1"]) - max(t["x0"], it["x0"]) > -0.5 * self.lh
        return sorted([t for t in self.items if t["y"] > it["y"] + 0.5 * it["h"] and t["y"] - it["y"] < depth * self.lh and overlap(t)],
                      key=lambda t: t["y"])


def _label_hits(page, key):
    """(item, remainder-after-label) for every place the label appears; the remainder stops at the next label or a column gap."""
    out = []
    for it in page.items:
        for m in LRE[key].finditer(it["text"]):
            if key == "invoice_date" and re.search(r"(due|ack|note|order|delivery)\s*$", it["text"][:m.start()], re.I):
                continue
            if key == "bank.name" and re.match(r"\s*details", it["text"][m.end():], re.I):
                continue
            if key in ("gstin", "pan") and m.start() > 0 and it["text"][m.start() - 1].isalnum():
                continue
            rest = it["text"][m.end():]
            nxt = min([mm.start() for k2 in L for mm in LRE[k2].finditer(rest) if mm.start() > 0 and k2 != key] + [len(rest)])
            rest = re.split(r"\s{2,}", rest[:nxt])[0].strip(" :.-")
            out.append((it, rest, m))
    return out


def _value(page, it, rest, ok, below=True):
    """The label's value: after the label in the same piece, else the next piece to the right, else the cell below."""
    if rest and ok(rest) and not is_label(rest):
        return rest, [it]
    for r in page.right(it)[:1]:
        t = r["text"].strip(" :.-")
        if r["x0"] - it["x1"] < 0.22 * page.W and not is_label(r["text"]) and ok(t):
            return t, [r]
    if below:
        for b in page.below(it):
            t = b["text"].strip(" :.-")
            if is_label(t):
                continue
            if ok(t):
                return t, [b]
    return None, []


def _amount_right(page, it, rest):
    """Right-most figure on the label's row, right of the label (a total, a tax amount)."""
    nums = [(n, it) for n in numbers(re.sub(PCT_RE, " ", rest))]
    for r in page.right(it):
        nums += [(n, r) for n in numbers(re.sub(PCT_RE, " ", r["text"]))]
    return nums[-1] if nums else (None, None)


def _field(value, src, status="read", printed=None, valid=None, note=None, how=None):
    src = [s for s in src if s]
    return {"value": value, "printed": printed if printed is not None else (src[0]["text"] if src else None),
            "valid": valid, "status": status, "conf": min([s["conf"] for s in src] or [0.0]),
            "boxes": [s["box"] for s in src], "rows": sorted({s.get("row", 0) for s in src}), "note": note, "how": how, "jev": None}


def _sections(page):
    heads = []
    for it in page.items:
        t = it["text"]
        if len(t) > 45 or HEAD_NOT.search(t.replace("(", " ").replace(")", " ")):
            continue
        if HEAD_CONSIGNEE.search(t):
            heads.append(("consignee", it))
        elif HEAD_BUYER.search(t):
            heads.append(("buyer", it))
    return heads


def _role(page, heads, it):
    """The party block an item sits in: the nearest buyer/consignee heading above it in the same column, else the seller."""
    best = None
    for role, h in heads:
        dy = it["y"] - h["y"]
        if 0 <= dy < 0.32 * page.H and h["x0"] - 2 * page.lh <= it["x0"] <= h["x0"] + 0.35 * page.W:
            if not best or dy < best[0]:
                best = (dy, role, h)
    return (best[1], best[2]) if best else ("seller", None)


def parse(lines, size=None):
    page = _Page(lines, size)
    F, C, checks = {}, {}, []
    all_text = "\n".join(r["text"] for r in page.rows)
    heads = _sections(page)

    def has_digit(s):
        return bool(re.search(r"\d", s))

    # ---- document type
    low = all_text.lower()
    doc = ("credit_note" if "credit note" in low else "debit_note" if "debit note" in low else
           "proforma" if "proforma" in low or "pro forma" in low else "quotation" if re.search(r"\bquotation\b|\bestimate\b", low) else
           "delivery_challan" if "delivery challan" in low else "bill_of_supply" if "bill of supply" in low else
           "tax_invoice" if re.search(r"tax\s*invoice|e-?invoice", low) else "receipt" if re.search(r"\breceipt\b", low) and "invoice" not in low else
           "invoice" if re.search(r"invoice|\bbill\s*no", low) else "unknown")

    # ---- invoice number, dates, references
    def ok_invno(s):
        return 0 < len(s) <= 30 and has_digit(s) and not parse_date(s) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/\-_.#]*", s.replace(" ", "")))
    cands = []
    for it, rest, _ in _label_hits(page, "invoice_no"):
        v, src = _value(page, it, rest, ok_invno)
        if v:
            cands.append((v.replace(" ", ""), src))
    if cands:
        F["invoice_no"] = _field(cands[0][0], cands[0][1], how="label")
    C["invoice_no"] = cands

    for key in ("invoice_date", "due_date", "ack_date"):
        dc = []
        for it, rest, m in _label_hits(page, key):
            v, src = _value(page, it, rest, lambda s: bool(parse_date(s)))
            if v:
                spec = 2 if key != "invoice_date" or re.match(r"(invoice|inv|bill)", m.group(0), re.I) else 1 if m.group(0).lower().startswith("dated") else 0
                dc.append((spec, -it["y"], parse_date(v), v, src))
        dc.sort(key=lambda t: (-t[0], -t[1]))
        if dc:
            F[key] = _field(dc[0][2], dc[0][4], printed=dc[0][3], how="label")
        C[key] = [(d[2], d[4]) for d in dc]

    for it, rest, _ in _label_hits(page, "ack_no")[:1]:
        v, src = _value(page, it, rest, lambda s: bool(re.fullmatch(r"\d{10,20}", s.replace(" ", ""))))
        if v:
            F["ack_no"] = _field(v.replace(" ", ""), src, how="label")
    for it, rest, _ in _label_hits(page, "eway_bill")[:1]:
        v, src = _value(page, it, rest, lambda s: bool(re.fullmatch(r"\d{12}", s.replace(" ", ""))))
        if v:
            F["eway_bill"] = _field(v.replace(" ", ""), src, how="label")
    for it, rest, _ in _label_hits(page, "irn")[:1]:
        v, src = _value(page, it, rest, lambda s: len(re.sub(r"[^0-9a-fA-FoOlI]", "", s)) >= 20, below=False)
        if v:
            parts, last = [v], src[0]
            for b in page.below(last, depth=1.6):
                if re.fullmatch(r"[0-9a-fA-FoOlI\-]{8,}", b["text"]) and abs(b["x0"] - last["x0"]) < 4 * page.lh:
                    parts.append(b["text"])
                    src.append(b)
                    break
            hexs = "".join(parts).translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"}))
            hexs = re.sub(r"[^0-9a-fA-F]", "", hexs).lower()
            F["irn"] = _field(hexs, src, valid=len(hexs) == 64, status="read" if len(hexs) == 64 else "invalid",
                              note=None if len(hexs) == 64 else f"{len(hexs)} hex characters; an IRN has 64", how="label")
    for it, rest, _ in _label_hits(page, "po_no")[:3]:
        v, src = _value(page, it, rest, lambda s: has_digit(s) and len(s) <= 30 and not parse_date(s))
        if v:
            F["po_no"] = _field(v.replace(" ", ""), src, how="label")
            break
    for it, rest, _ in _label_hits(page, "place_of_supply")[:1]:
        v, src = _value(page, it, rest, lambda s: bool(re.search(r"[A-Za-z]{3}", s)))
        if v:
            F["place_of_supply"] = _field(re.split(r"\s*[(,\-]\s*(?:code|\d)", v, flags=re.I)[0].strip(), src, printed=v, how="label")

    # ---- parties: GSTINs by block, names
    gst = []
    seen_items = set()
    for it, rest, _ in _label_hits(page, "gstin"):
        v, src = _value(page, it, rest, lambda s: len(re.sub(r"[^A-Za-z0-9]", "", s)) >= 10)
        if v:
            token = max(re.findall(r"[A-Za-z0-9]{10,}", v.replace(" ", "")) or [v], key=len).upper()
            gst.append((token, src, it))
            seen_items.add(src[0]["i"])
    for it in page.items:                                 # unlabelled, but shaped exactly like a GSTIN
        if it["i"] not in seen_items:
            for g in GSTIN_SHAPE.findall(it["text"].upper()):
                gst.append((g, [it], it))
    C["gstin"] = []
    for token, src, lab in gst:
        role, head = _role(page, heads, lab)
        fixed, note = fix_gstin(token)
        prob = gstin_problem(fixed)
        C["gstin"].append({"role": role, "value": fixed, "src": src, "head": head})
        key = f"{role}.gstin"
        if key in F:
            continue
        F[key] = _field(fixed, src, printed=token, valid=prob is None, status="checked" if prob is None else "invalid",
                        note=note if prob is None else f"printed as {token}: not a valid GSTIN ({prob})", how=f"in the {role} block")

    for role, h in heads:                                 # buyer / consignee names: the first line under the heading
        key = f"{role}.name"
        if key in F:
            continue
        rest = re.sub(r"^(?:(?:bill(?:ed)?|ship(?:ped)?|sold|deliver(?:y|ed)?)\s*to|consignee|buyer|customer|receiver|details\s*of|[()\s:\-])+", "",
                      h["text"], flags=re.I).strip()
        if len(rest) > 2 and not is_label(rest):
            F[key] = _field(rest, [h], how="under the heading")
            continue
        for b in page.below(h, depth=2.6):
            if not is_label(b["text"]) and re.search(r"[A-Za-z]{3}", b["text"]) and not LRE["gstin"].match(b["text"]):
                F[key] = _field(b["text"].strip(" :"), [b], how="under the heading")
                break

    names = []                                            # seller name candidates, strongest first
    for it in page.items:
        m = SIGN_RE.match(it["text"])
        if m and not re.search(r"your\s+business|thank", it["text"], re.I):
            nm = m.group(1).strip(" :")
            src = [it]
            if not nm:
                nxt = page.below(it, depth=1.8)
                if nxt and not is_label(nxt[0]["text"]):
                    nm, src = nxt[0]["text"].strip(" :"), [it, nxt[0]]
            if len(nm) > 2:
                names.append(("signatory", nm, src))
    for it, rest, _ in _label_hits(page, "seller_label"):
        v, src = _value(page, it, rest, lambda s: bool(re.search(r"[A-Za-z]{3}", s)) and not LRE["gstin"].match(s))
        if v:
            names.append(("label", v, src))
    sg = next((c for c in C["gstin"] if c["role"] == "seller"), None)
    if sg:                                                # top line of the block the seller's GSTIN sits in
        lab = sg["src"][0]
        above = sorted([t for t in page.items if 0 < lab["y"] - t["y"] < 12 * page.lh and abs(t["x0"] - lab["x0"]) < 2.5 * page.lh], key=lambda t: -t["y"])
        top, last_y = None, lab["y"]
        for t in above:                                   # contiguous lines only: stop at a gap, a label or a heading
            if last_y - t["y"] > 2.4 * page.lh or is_label(t["text"]) or re.match(r"(irn|ack|tax\s*invoice|e-?invoice|invoice|state|phone|email|tel|pan)\b", t["text"], re.I) \
                    or re.match(r"\s*:", t["text"]) or HEAD_BUYER.search(t["text"]) or HEAD_CONSIGNEE.search(t["text"]):
                break
            top, last_y = t, t["y"]
        if top and re.search(r"[A-Za-z]{3}", top["text"]):
            names.append(("block", top["text"].strip(" :"), [top]))
    for it in page.items:                                # "Authorised Signatory" with the company name printed under it
        if re.fullmatch(r"authori[sz]ed\s+signator(?:y|ies)\.?", it["text"].strip(), re.I):
            nxt = page.below(it, depth=1.8)
            if nxt and not is_label(nxt[0]["text"]) and not re.search(r"computer|generated|invoice|thank|declar", nxt[0]["text"], re.I) \
                    and re.search(r"[A-Za-z]{3}", nxt[0]["text"]):
                names.append(("signatory", nxt[0]["text"].strip(" :"), [it, nxt[0]]))
    names.sort(key=lambda n: {"signatory": 0, "label": 1, "block": 2}[n[0]])
    C["seller.name"] = names
    if names:
        how, nm, src = names[0]
        agree = [n for n in names[1:] if same_name(n[1], nm)]
        F["seller.name"] = _field(nm, src, status="checked" if agree else "read",
                                  note=f"same name on the {agree[0][0]} line" if agree else None, how=how)

    for role in ("seller", "buyer", "consignee"):         # state: "State Name : X, Code : 29", else the GSTIN's state code
        g = F.get(f"{role}.gstin")
        st = None
        for it, rest, _ in _label_hits(page, "state"):
            r, _h = _role(page, heads, it)
            if r != role:
                continue
            v, src = _value(page, it, rest, lambda s: bool(re.search(r"[A-Za-z]{3}", s)), below=False)
            if v:
                code = re.search(r"code\s*[:\-]?\s*(\d{2})", v + " " + " ".join(t["text"] for t in page.right(it)), re.I)
                name = re.split(r"\s*,|\s+code", v, flags=re.I)[0].strip()
                st = _field(f"{name} ({code.group(1)})" if code else name, src, how="label")
                break
        if not st and g and re.match(r"\d{2}", g["value"] or "") and g["value"][:2] in pipeline.GST_STATES:
            st = _field(f"{pipeline.GST_STATES[g['value'][:2]]} ({g['value'][:2]})", [], printed=g["value"][:2], status="computed", how="from the GSTIN")
        if st:
            F[f"{role}.state"] = st

    # ---- bank
    for it, rest, _ in _label_hits(page, "bank.account_no")[:2]:
        v, src = _value(page, it, rest, lambda s: bool(re.fullmatch(r"\d{6,20}", re.sub(r"[\s\-]", "", s.split("  ")[0]))))
        if v:
            acc = re.sub(r"[\s\-]", "", v.split("  ")[0])
            F["bank.account_no"] = _field(acc, src, valid=9 <= len(acc) <= 18, status="read" if 9 <= len(acc) <= 18 else "invalid",
                                          note=None if 9 <= len(acc) <= 18 else f"{len(acc)} digits; Indian account numbers have 9 to 18", how="label")
            break
    for it, rest, _ in _label_hits(page, "bank.ifsc")[:1]:
        v, src = _value(page, it, rest, lambda s: bool(re.fullmatch(r"[A-Za-z0-9]{8,12}", s.split()[0] if s.split() else "")))
        if v:
            raw = v.split()[0].upper()
            f = raw[:4] + (raw[4:5].replace("O", "0")) + raw[5:] if len(raw) > 4 else raw
            okf = bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", f))
            F["bank.ifsc"] = _field(f, src, printed=raw, valid=okf, status="checked" if okf else "invalid",
                                    note=None if okf else f"printed as {raw}: not a valid IFSC ({len(raw)} characters; an IFSC is 4 letters, 0, then 6 characters)", how="label")
    for key in ("bank.account_holder", "bank.name", "bank.branch"):
        for it, rest, _ in _label_hits(page, key):
            v, src = _value(page, it, rest, lambda s: bool(re.search(r"[A-Za-z]{3}", s)) and not re.match(r"details?$", s, re.I), below=key != "bank.name")
            if v:
                F[key] = _field(v, src, how="label")
                break
    for it in page.items:
        m = re.search(r"\b([\w.\-]{3,}@[a-z]{3,})\b(?!\.)", it["text"])
        if m:
            F["bank.upi"] = _field(m.group(1), [it], how="pattern")
            break

    # ---- items table
    items, items_meta = _table(page)

    # ---- tax, totals, amount in words
    totals_row = (items_meta or {}).get("totals", {})
    for key in ("taxable_value", "round_off"):
        below_c = None
        for it, rest, _ in _label_hits(page, key):
            n, src = _amount_right(page, it, rest)
            if n is None and key == "taxable_value" and below_c is None:
                for b in page.below(it):
                    if numbers(b["text"]):
                        below_c = (numbers(b["text"])[-1], [it, b])
                        break
            if n is not None:
                if key == "round_off" and re.search(r"\(-\)|-\s*\d|less", it["text"] + " " + src["text"], re.I):
                    n = -n
                F[key] = _field(n, [it, src], printed=src["text"], how="label")
                break
        if key == "taxable_value" and key not in F:
            if "taxable" in totals_row:
                n, src = totals_row["taxable"]
                F[key] = _field(n, [src], printed=src["text"], how="items table total row")
            elif items and all(i.get("taxable") is not None for i in items):
                F[key] = _field(round(sum(i["taxable"] for i in items), 2), [], status="computed", printed="", how="sum of the line items")
            elif below_c:
                F[key] = _field(below_c[0], below_c[1], printed=below_c[1][1]["text"], how="label (cell below)")
    def heading_row(it):
        return sum(1 for t in page.rows[it["row"]]["items"] if _kind(t["text"])) >= 3
    for kind, rx in TAX_LRE.items():
        for it in page.items:
            m = rx.match(it["text"])
            if not m or heading_row(it):
                continue
            rest = it["text"][m.end():]
            n, src = _amount_right(page, it, rest)
            if n is None:
                continue
            F[kind] = _field(n, [it, src], printed=src["text"], how="label")
            pct = PCT_RE.search(it["text"])
            if pct:
                F[kind + "_rate"] = _field(float(pct.group(1)), [it], how="printed")
            break

    for kind in ("igst", "cgst", "sgst", "cess"):
        if kind not in F and kind in totals_row:
            n, src = totals_row[kind]
            F[kind] = _field(n, [src], printed=src["text"], how="items table total row")
        if kind + "_rate" not in F and items and all(i.get("tax_rates", {}).get(kind) for i in items):
            rates = {i["tax_rates"][kind] for i in items}
            if len(rates) == 1:
                F[kind + "_rate"] = _field(rates.pop(), [], status="read", printed="", how="items table")
    words, tax_words = [], []
    for r in page.rows:
        for it in r["items"]:
            v = words_to_number(it["text"])
            if v is None:
                continue
            ctx = it["text"] + " " + " ".join(t["text"] for t in page.items if 0 < it["y"] - t["y"] < 1.6 * page.lh)
            (tax_words if re.search(r"tax\s*amount|gst\s*amount", ctx, re.I) else words).append((v, it))
    if words:
        F["total_in_words"] = _field(float(words[0][0]), [words[0][1]], how="amount in words")
    if tax_words:
        F["tax_in_words"] = _field(float(tax_words[0][0]), [tax_words[0][1]], how="amount in words")

    taxes = sum(F[k]["value"] for k in ("cgst", "sgst", "utgst", "igst", "cess") if k in F)
    expected = F["taxable_value"]["value"] + taxes + (F["round_off"]["value"] if "round_off" in F else 0) if "taxable_value" in F and taxes else None
    tw = F.get("total_in_words", {}).get("value")
    totals = []
    for it in page.items:
        if not TOTAL_RE.match(it["text"]) or NOT_TOTAL_RE.search(it["text"]):
            continue
        rest = TOTAL_RE.sub("", it["text"], count=1)
        n, src = _amount_right(page, it, rest)
        if n is None or n <= 0:
            continue
        score = 0.0
        if tw is not None and abs(n - tw) < 0.5:
            score += 4
        if expected is not None and abs(n - expected) <= 1.0:
            score += 3
        if "tax_in_words" in F and abs(n - F["tax_in_words"]["value"]) < 0.5 and not (tw is not None and abs(n - tw) < 0.5):
            score -= 5
        if re.match(r"grand|total\s*amount|.*payable|net|invoice\s*(total|value)", it["text"], re.I):
            score += 1
        totals.append((score, n, it, src))
    totals.sort(key=lambda t: (-t[0], -t[1]))
    C["total"] = [(t[1], [t[2], t[3]]) for t in totals]
    if totals:
        s, n, it, src = totals[0]
        hard = s >= 3
        F["total"] = _field(n, [it, src], printed=src["text"], status="checked" if hard else "read", how="label",
                            note="matches the amount in words" + (" and taxable value + tax" if expected is not None and abs(n - expected) <= 1 else "")
                            if tw is not None and abs(n - tw) < 0.5 else ("equals taxable value + tax" if hard else None))
        F["total"]["hard"] = hard
    elif tw is not None:
        F["total"] = _field(float(tw), [words[0][1]], status="read", how="amount in words", note="only the amount in words was readable")

    if "taxable_value" in F:                              # tax rate when only the amount is printed
        for kind in ("cgst", "sgst", "igst"):
            if kind in F and kind + "_rate" not in F and F["taxable_value"]["value"]:
                F[kind + "_rate"] = _field(round(F[kind]["value"] / F["taxable_value"]["value"] * 100, 2), [], status="computed",
                                           printed="", how="tax amount / taxable value")
    if items and "qty" not in F:
        units = {i["unit"] for i in items if i["qty"] is not None}
        if len(units) == 1 and all(i["qty"] is not None for i in items):
            F["qty"] = _field(sum(i["qty"] for i in items), [], status="computed", printed="", how="sum of the line items")
    if "qty" not in F:
        for it, rest, _ in _label_hits(page, "qty"):
            m = re.match(r"\s*(\d+(?:\.\d+)?)", rest)
            if m:
                F["qty"] = _field(float(m.group(1)), [it], how="label")
                break

    # ---- cross-checks
    def chk(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})
    t = F.get("total", {}).get("value")
    if t is not None and tw is not None:
        chk("total_matches_words", abs(t - tw) < 0.5, f"₹{inr(t)} in figures, ₹{inr(tw)} in words")
    if t is not None and expected is not None:
        chk("taxable_plus_tax_equals_total", abs(t - expected) <= 1.0, f"₹{inr(F['taxable_value']['value'])} + tax ₹{inr(taxes)} = ₹{inr(expected)}; total ₹{inr(t)}")
    if "cgst" in F and "sgst" in F:
        chk("cgst_equals_sgst", abs(F["cgst"]["value"] - F["sgst"]["value"]) < 0.02, f"CGST ₹{inr(F['cgst']['value'])}, SGST ₹{inr(F['sgst']['value'])}")
    for kind in ("cgst", "sgst", "igst"):
        r = F.get(kind + "_rate")
        if r and r["status"] != "computed" and "taxable_value" in F:
            want = F["taxable_value"]["value"] * r["value"] / 100
            chk(f"{kind}_rate_matches", abs(want - F[kind]["value"]) <= 1.0, f"{r['value']}% of ₹{inr(F['taxable_value']['value'])} = ₹{inr(want)}; printed ₹{inr(F[kind]['value'])}")
    amts = [i["taxable"] for i in items] if items and all(i.get("taxable") is not None for i in items) else [i["amount"] for i in items if i["amount"] is not None]
    if amts and ("taxable_value" in F or t is not None):
        s = sum(amts)
        tv = F.get("taxable_value", {}).get("value")
        ok = (tv is not None and abs(s - tv) <= 1.0) or (t is not None and abs(s - t) <= 1.0)
        chk("items_add_up_to_taxable", ok, f"line items add up to ₹{inr(s)}" + (" (tax inclusive)" if tv is not None and abs(s - tv) > 1 and t is not None and abs(s - t) <= 1 else ""))
    sg, pos = F.get("seller.gstin"), F.get("place_of_supply")
    if sg and sg["valid"] and ("igst" in F or "cgst" in F):
        dest = (F.get("buyer.gstin") or {}).get("value") or ""
        same = None
        if pos:
            same = pipeline.GST_STATES.get(sg["value"][:2], "").lower() in pos["value"].lower()
        elif re.match(r"\d{2}", dest):
            same = dest[:2] == sg["value"][:2]
        if same is not None:
            chk("tax_type_matches_states", (("cgst" in F) == same), ("same state: CGST + SGST expected" if same else "different states: IGST expected")
                + ("; printed CGST + SGST" if "cgst" in F else "; printed IGST"))
    holder, seller = F.get("bank.account_holder"), F.get("seller.name")
    if holder and seller:
        chk("account_holder_is_seller", same_name(holder["value"], seller["value"]), f"account holder '{holder['value']}', seller '{seller['value']}'")
    for key in ("seller.gstin", "buyer.gstin", "consignee.gstin", "bank.ifsc"):
        if key in F:
            chk(key.replace(".", "_") + "_valid", bool(F[key]["valid"]), F[key]["note"] or f"{F[key]['value']} passes its format check")

    # a figure confirmed by printed evidence
    for key in ("taxable_value", "cgst", "sgst", "igst"):
        if key in F and any(c["ok"] for c in checks if c["name"] in ("taxable_plus_tax_equals_total", "cgst_equals_sgst")):
            F[key]["status"] = "checked"

    found = [k for k in ("invoice_no", "total", "seller.gstin", "seller.name", "invoice_date") if k in F]
    readable = len(found) >= 2 or ("total" in F and "invoice_no" in F)
    note = None
    if not readable:
        avg = statistics.mean([len(t["text"]) for t in page.items]) if page.items else 0
        note = ("Could not read the invoice details. The photo looks blurred, too far away or too dark: hold the phone steady, "
                "fill the frame with the bill, use good light, and try again." if len(page.items) < 60 or avg < 12 else
                "Could not find invoice details (number, total, GSTIN) in this photo. Is it an invoice?")
    return {"doc_type": doc, "fields": F, "items": items, "checks": checks, "readable": readable, "quality_note": note,
            "tilt_deg": round(math.degrees(tilt(lines, *(size or (1, 1)))) if size else 0.0, 2), "jev": None,
            "_cand": C, "_page_text": all_text}


KIND_ORDER = [("taxable", r"taxable"), ("tax_rate", r"(?:igst|cgst|sgst|utgst|cess|gst|tax).*(?:rate|%)|(?:rate|%).*(?:igst|cgst|sgst)|^%$"),
              ("tax_amt", r"igst|cgst|sgst|utgst|cess|tax\s*/|tax\s*amount|^gst|^tax$|^amt$"),
              ("sl", r"^(?:s\.?\s*no\.?|si|sl|sr\.?(?:\s*no\.?)?|#|no\.?)$"), ("desc", r"description|particulars|items?|goods|product|service|name"),
              ("hsn", r"hsn|sac"), ("qty", r"qty|quantity"), ("rate", r"rate|price|mrp"), ("unit", r"^(?:unit|uom|per)\b"),
              ("disc", r"disc"), ("amount", r"amount|amt|total|value")]


def _kind(label):
    for k, rx in KIND_ORDER:
        if re.search(rx, label, re.I):
            return k
    return None


def _tax_name(label):
    m = re.search(r"igst|cgst|sgst|utgst|cess", label, re.I)
    return m.group(0).lower() if m else "gst"


def _table(page):
    """Line items, read column by column from the table header (one or two header rows)."""
    rows = page.rows
    for n, r in enumerate(rows):
        kinds = {_kind(t["text"]) for t in r["items"]}
        if not ({"amount", "taxable", "tax_amt"} & kinds) or len(kinds & {"desc", "hsn", "qty", "rate"}) < 2:
            continue
        head = [r]
        def keyword_row(x):
            return x and not any(numbers(t["text"]) for t in x["items"] if not _kind(t["text"])) and \
                sum(1 for t in x["items"] if _kind(t["text"])) >= max(1, len(x["items"]) - 1)
        prev = rows[n - 1] if n > 0 and r["y"] - rows[n - 1]["y"] < 2.6 * page.lh else None
        nxt = rows[n + 1] if n + 1 < len(rows) and rows[n + 1]["y"] - r["y"] < 2.6 * page.lh else None
        if keyword_row(prev):
            head.insert(0, prev)
        if keyword_row(nxt):
            head.append(nxt)
        upper, lower = (head[0], head[1]) if len(head) >= 2 else (None, head[0])
        if len(head) == 3:
            upper, lower = head[1], head[2]
            if head[0] is prev:
                upper, lower = head[0], head[1]
        cols = []
        lower_items = lower["items"] if len(head) > 1 else head[0]["items"]
        def parent_of(t):
            """The heading above a sub-heading: nearest by centre, within the two half-widths plus a little."""
            best = None
            for u in (upper["items"] if upper else []):
                dc = abs((u["x0"] + u["x1"]) / 2 - (t["x0"] + t["x1"]) / 2)
                if dc <= (u["x1"] - u["x0"]) / 2 + (t["x1"] - t["x0"]) / 2 + 2 * page.lh and (not best or dc < best[0]):
                    best = (dc, u)
            return best[1] if best else None
        parents = set()
        for t in lower_items:
            parent = parent_of(t)
            if parent:
                parents.add(id(parent))
            label = (parent["text"] + " " + t["text"]) if parent else t["text"]
            ck, pk = _kind(t["text"]), _kind(parent["text"]) if parent else None
            kind = _kind(label) if pk in ("tax_amt", "tax_rate", "taxable") and ck in (None, "rate", "amount", "tax_amt", "tax_rate") else (ck or pk)
            cols.append({"label": label, "kind": kind or ("desc" if not cols else None), "x0": t["x0"], "x1": t["x1"]})
        for u in (upper["items"] if upper else []):     # upper headings with nothing under them are columns too
            if id(u) not in parents:
                cols.append({"label": u["text"], "kind": _kind(u["text"]), "x0": u["x0"], "x1": u["x1"]})
        cols = [c for c in cols if c["kind"]]
        cols.sort(key=lambda c: c["x0"])
        if not cols:
            continue

        def col_of(t):
            cx = (t["x0"] + t["x1"]) / 2
            inside = [c for c in cols if c["x0"] - page.lh <= cx <= c["x1"] + page.lh]
            pool = inside or cols
            return min(pool, key=lambda c: abs((c["x0"] + c["x1"]) / 2 - cx))

        out, totals = [], {}
        start = rows.index(head[-1]) + 1
        for body in rows[start:]:
            first = body["items"][0]["text"]
            stop = re.match(r"(sub\s*total|total|taxable|cgst|sgst|igst|cess|round|amount\s*chargeable|grand|less|add|invoice\s*total)\b", first, re.I) or \
                any(TAX_LRE["cgst"].match(t["text"]) or TAX_LRE["igst"].match(t["text"]) for t in body["items"]) or \
                any(re.match(r"total", t["text"], re.I) for t in body["items"])
            if stop:
                is_total_row = bool(re.match(r"(sub\s*)?total", first, re.I) or any(re.match(r"total", t["text"], re.I) for t in body["items"]))
                for t in (body["items"] if is_total_row else []):                   # the table's own total row: taxable value and tax per column
                    v = numbers(re.sub(PCT_RE, " ", t["text"]))
                    if not v or re.match(r"total|sub", t["text"], re.I):
                        continue
                    c = col_of(t)
                    if c["kind"] == "taxable":
                        totals["taxable"] = (v[-1], t)
                    elif c["kind"] == "tax_amt":
                        totals[_tax_name(c["label"])] = (v[-1], t)
                    elif c["kind"] == "amount":
                        totals["amount"] = (v[-1], t)
                break
            if body["y"] - head[-1]["y"] > 0.5 * page.H:
                break
            cells = {}
            for t in body["items"]:
                c = col_of(t)
                cells.setdefault(id(c), (c, []))[1].append(t)
            row = {"description": [], "hsn": None, "qty": None, "unit": None, "rate": None, "taxable": None, "tax": None,
                   "tax_rates": {}, "amount": None, "boxes": [t["box"] for t in body["items"]], "conf": min(t["conf"] for t in body["items"])}
            for c, ts in cells.values():
                txt = " ".join(t["text"] for t in ts)
                k = c["kind"]
                qty_like = bool(re.fullmatch(r"[\d.,]+\s*[A-Za-z]{1,8}\.?", txt))
                if k in ("desc", "sl") or (len(re.findall(r"[A-Za-z]", txt)) >= 4 and not qty_like and (k not in ("unit", "qty") or len(txt) > 12)):
                    if not re.fullmatch(r"\d{1,3}\.?", txt):
                        row["description"].append(re.sub(r"^\d{1,3}\.?\s+(?=[A-Za-z])", "", txt))
                    continue
                if k == "hsn" and re.fullmatch(r"\d{4,8}", txt.replace(" ", "")):
                    row["hsn"] = txt.replace(" ", "")
                elif k == "qty":
                    m = re.match(r"([\d.,]+)\s*([A-Za-z]+)?", txt)
                    if m and numbers(m.group(1)):
                        row["qty"], row["unit"] = numbers(m.group(1))[0], m.group(2)
                elif k == "unit" and re.fullmatch(r"[A-Za-z]{1,8}", txt):
                    row["unit"] = row["unit"] or txt
                elif k == "rate" and numbers(txt):
                    row["rate"] = numbers(txt)[-1]
                elif k == "taxable" and numbers(txt):
                    row["taxable"] = numbers(txt)[-1]
                elif k == "tax_rate":
                    p = PCT_RE.search(txt)
                    v = float(p.group(1)) if p else (numbers(txt)[-1] if numbers(txt) else None)
                    if v is not None:
                        row["tax_rates"][_tax_name(c["label"])] = v
                elif k == "tax_amt":
                    v = numbers(re.sub(PCT_RE, " ", txt))
                    if v:
                        row["tax"] = (row["tax"] or 0) + v[0]
                    p = PCT_RE.search(txt)
                    if p:
                        row["tax_rates"][_tax_name(c["label"])] = float(p.group(1))
                elif k == "amount" and numbers(txt):
                    row["amount"] = numbers(txt)[-1]
            has_value = row["amount"] is not None or row["taxable"] is not None
            if not has_value and not any(row[k] is not None for k in ("hsn", "qty", "rate")) and not row["description"]:
                continue
            if not has_value and out:                     # the item continues on this line: HSN, quantity, rate or more description
                last = out[-1]
                for k in ("hsn", "qty", "unit", "rate"):
                    if last[k] is None and row[k] is not None:
                        last[k] = row[k]
                last["description"] += row["description"]
                last["boxes"] += row["boxes"]
                continue
            if not has_value and row["qty"] is None:
                continue
            out.append(row)
        for row in out:
            row["description"] = " ".join(row["description"]).strip() or None
            if row["amount"] is None and row["taxable"] is not None:
                row["amount"] = round(row["taxable"] + (row["tax"] or 0), 2)
        return out, {"totals": totals, "columns": [c["label"] for c in cols]}
    return [], {}


# ---------------- Jev: choose among the candidates code found ----------------

def _options(b):
    """Questions for Jev: each has options (key -> the value it stands for) and the rule's pick."""
    C, F = b["_cand"], b["fields"]
    qs = {}

    def add(key, field, values, describe, rule_value):
        opts, seen = {}, []
        for v in values:
            if v in seen:
                continue
            seen.append(v)
            opts[f"o{len(opts) + 1}"] = v
        if len(opts) < 2 or rule_value is None:
            return
        rule = next((k for k, v in opts.items() if v == rule_value), None)
        qs[key] = {"field": field, "options": opts, "rule": rule, "describe": describe}

    names = [n[1] for n in C.get("seller.name", [])] + [F[k]["value"] for k in ("buyer.name", "consignee.name") if k in F]
    add("seller_name", "seller.name", names, lambda v: f"the name '{v}'", F.get("seller.name", {}).get("value"))
    g = C.get("gstin", [])
    gids = {}
    for c in g:
        gids.setdefault(c["value"], f"GSTIN-{len(gids) + 1}")
    seller_g = F.get("seller.gstin", {}).get("value")
    add("seller_gstin", "seller.gstin", [c["value"] for c in g],
        lambda v: f"[{gids[v]}], printed in the block headed '{next((c['head']['text'] for c in g if c['value'] == v and c['head']), 'the top of the bill (no buyer or consignee heading)')}'",
        seller_g)
    add("total", "total", [c[0] for c in C.get("total", [])], lambda v: f"₹{inr(v)}", F.get("total", {}).get("value"))
    add("invoice_no", "invoice_no", [c[0] for c in C.get("invoice_no", [])], lambda v: f"'{v}'", F.get("invoice_no", {}).get("value"))
    add("invoice_date", "invoice_date", [c[0] for c in C.get("invoice_date", [])], lambda v: show_date(v), F.get("invoice_date", {}).get("value"))
    b["_jevq"] = qs
    return qs, gids


def jev_rule_choice(b, key):
    q = (b.get("_jevq") or {}).get(key)
    return q["rule"] if q else None


INSTRUCTIONS = {
    "seller_name": "This is the text of an Indian GST invoice read from a photo. Which option is the name of the SELLER (the business that issued the invoice and is to be paid), not the buyer or consignee?",
    "seller_gstin": "This is the text of an Indian GST invoice read from a photo; GSTINs are shown as [GSTIN-n]. Which GSTIN belongs to the SELLER who issued the invoice, not to the buyer (bill to) or the consignee (ship to)?",
    "total": "This is the text of an Indian GST invoice read from a photo. Which amount is the final invoice total the buyer must pay for THIS invoice (after tax), not a subtotal, tax amount, previous balance or running balance?",
    "invoice_no": "This is the text of an Indian GST invoice read from a photo. Which option is this invoice's own number (not an order, delivery note, e-way bill or acknowledgement number)?",
    "invoice_date": "This is the text of an Indian GST invoice read from a photo. Which date is the date this invoice was issued (not a due date, order date or acknowledgement date)?",
}


def review_with_jev(b, job_id):
    """Second opinion from Jev on every field that had more than one candidate. Rules decide; see the module docstring."""
    from . import jev
    qs, gids = _options(b)
    if not qs:
        b["jev"] = "not needed: one candidate per field"
        return b
    state = b["_page_text"]
    for v, gid in gids.items():
        state = state.replace(v, f"[{gid}]")
    state = jev.redact(state)[:12000]
    questions = {}
    for key, q in qs.items():
        crit = {k: q["describe"](v) for k, v in q["options"].items()}
        crit["none"] = "none of these"
        questions[key] = {"type": "choice", "instructions": INSTRUCTIONS[key], "criteria": crit}
    try:
        answers, meta = jev.ask(job_id, state, questions)
    except jev.Unavailable as e:
        b["jev"] = f"unavailable: {e}"
        return b
    b["jev"] = f"{meta.get('model')} {meta.get('request_id') or ''}".strip()
    for key, q in qs.items():
        f = b["fields"].get(q["field"])
        if not f:
            continue
        pick = jev.pick(answers.get(key) or {})
        if pick is None:
            f["jev"] = "unsure"
        elif pick == q["rule"]:
            f["jev"] = "agrees"
            if f["status"] == "read":
                f["status"] = "checked"
                f["note"] = ((f["note"] + "; ") if f["note"] else "") + "Jev picked the same"
        else:
            other = q["options"].get(pick, "none of the options")
            shown = q["describe"](other) if pick in q["options"] else other
            f["jev"] = f"disagrees: picked {shown}"
            if not f.get("hard") and f["status"] != "invalid":
                f["status"] = "conflict"
                f["note"] = f"rules read {q['describe'](f['value'])}, Jev picked {shown}: compare with the photo"
    return b


def public(b):
    """The bill as stored and shown: without the working candidates."""
    return {k: v for k, v in b.items() if not k.startswith("_")}
