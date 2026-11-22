"""Scan a paper bill: photo in -> text read on this machine -> the normal invoice checks.

    photo bytes -> data/fixtures/scans/scan_<hash>.jpg     (kept as evidence)
                -> data/fixtures/scans/scan_<hash>.json    (every line read: text, confidence, position)
                -> data/fixtures/inbox/scan_<hash>.txt     (rows rebuilt from the lines)
                -> ap.process_file(txt)                    (same checks, duplicate catch and pay-once as email)

The reader is free and offline: Apple Vision on a Mac (compiled once from vision_ocr.swift),
else Tesseract. The photo never leaves the machine. Each step's output is a file named by the
photo's hash, so a repeat or a restart reuses it: the same photo is never read twice.
"""
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import unicodedata

from . import ap, bill, store
from .config import CONFIG

HERE = os.path.dirname(__file__)
SWIFT_SRC = os.path.join(HERE, "vision_ocr.swift")
SWIFT_BIN = os.path.join(HERE, "..", "data", "bin", "vision_ocr")
MAX_BYTES = 15 * 1024 * 1024
NAME_RE = re.compile(r"scan_[0-9a-f]{16}")
FIELDS = ("invoice_no", "invoice_date", "gstin", "po_no", "amount_inr", "qty", "account_no", "ifsc", "account_name", "vendor_name_text")
READER_LABEL = {"apple_vision": "Apple Vision", "tesseract": "Tesseract"}
_compile_lock = threading.Lock()
# the rupee sign is often read as a look-alike currency sign; only fixed directly before a number
_SIGNS = "".join(c for c in map(chr, range(0x20, 0x20D0)) if unicodedata.category(c) == "Sc" and c != "₹")
RUPEE_MISREAD = re.compile("[" + re.escape(_SIGNS) + r"](?=\s?\d)")
# ...or as a bare letter R glued to an Indian-grouped amount (R1,58,400.00); the grouping keeps codes like R123 untouched
# ...or as a stray mark (· * ' etc.) between a space and an Indian-grouped amount
RUPEE_AS_MARK = re.compile(r"(?<=\s)[^\w\s,.₹](?=\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b)")
# a label's colon read as a semicolon ("Qty; 120"): glued to a word and followed by a space
COLON_AS_SEMI = re.compile(r"(?<=[A-Za-z.]);(?=\s)")
RUPEE_AS_R = re.compile(r"(?<![A-Za-z0-9])R(?=\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b)")


class ScanError(ValueError):
    """The upload cannot be read as a bill photo (wrong type, too large, or no reader available)."""


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def scans_dir():
    return os.path.join(ap.FIX, "scans")


def photo_path(name):
    """Path of a stored photo, or None if the name is not one this module wrote."""
    if not re.fullmatch(NAME_RE.pattern + r"\.(jpg|png|heic)", name or ""):
        return None
    return os.path.join(scans_dir(), name)


def _photo_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return ".heic"
    raise ScanError("Not a photo. Send a JPEG, PNG or HEIC picture of the bill.")


def _write(path, data):
    """Atomic write: a kill leaves either the old file or the new one, never half a file."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---------------- readers ----------------

def reader_name():
    """Which free reader this machine has: apple_vision, tesseract, or None."""
    forced = os.environ.get("SCAN_READER")
    if forced:
        return forced
    if sys.platform == "darwin" and (os.path.exists(SWIFT_BIN) or shutil.which("swiftc")):
        return "apple_vision"
    if shutil.which("tesseract"):
        return "tesseract"
    return None


def _apple_bin():
    with _compile_lock:
        if not os.path.exists(SWIFT_BIN) or os.path.getmtime(SWIFT_BIN) < os.path.getmtime(SWIFT_SRC):
            os.makedirs(os.path.dirname(SWIFT_BIN), exist_ok=True)
            subprocess.run(["swiftc", "-O", SWIFT_SRC, "-o", SWIFT_BIN], check=True, capture_output=True, timeout=300)
    return SWIFT_BIN


def _apple(path):
    """Lines read by Apple Vision, and the image size in pixels (for taking out the page's tilt)."""
    r = subprocess.run([_apple_bin(), path], capture_output=True, text=True, timeout=180, check=True)
    out = json.loads(r.stdout)
    return (out["lines"], out.get("size")) if isinstance(out, dict) else (out, None)


def _tesseract(path):
    out = subprocess.run(["tesseract", path, "stdout", "tsv"], capture_output=True, text=True, timeout=180, check=True).stdout
    W = H = 1.0
    groups = {}
    for w in csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE):
        if w["level"] == "1":
            W, H = float(w["width"]), float(w["height"])
        if w["level"] == "5" and (w.get("text") or "").strip():
            groups.setdefault((w["block_num"], w["par_num"], w["line_num"]), []).append(w)
    lines = []
    for ws in groups.values():
        l, t = min(int(w["left"]) for w in ws), min(int(w["top"]) for w in ws)
        r, b = max(int(w["left"]) + int(w["width"]) for w in ws), max(int(w["top"]) + int(w["height"]) for w in ws)
        lines.append({"text": " ".join(w["text"] for w in ws), "conf": min(float(w["conf"]) for w in ws) / 100,
                      "box": [l / W, t / H, (r - l) / W, (b - t) / H]})
    return lines


def read_image(path):
    """Read every line of text in the photo. Tries Apple Vision, then Tesseract."""
    order = [n for n in (reader_name(), "tesseract") if n]
    last = None
    for name in dict.fromkeys(order):
        try:
            lines, size = _apple(path) if name == "apple_vision" else (_tesseract(path), None)
            return {"reader": name, "lines": lines, "size": size}
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            last = e
            store.log(None, f"scan: {name} could not read {os.path.basename(path)}: {e}", "warn")
    raise ScanError(f"No text reader worked on this machine ({last}). Install Tesseract (brew install tesseract / apt install tesseract-ocr) or run on a Mac.")


def warm():
    """Load the reader once at start-up: Apple Vision's first read takes ~45 s, later ones ~0.5 s."""
    if reader_name() == "apple_vision":
        try:
            subprocess.run([_apple_bin(), "--warm"], capture_output=True, timeout=300)
        except Exception as e:
            store.log(None, f"scan: reader warm-up failed: {e}", "warn")


# ---------------- lines -> rows ----------------

def _tilt(lines, W, H):
    """The page's tilt in radians: median slope of the long lines' top edges, in pixels."""
    angles = []
    for ln in lines:
        q = ln.get("quad")
        if q and abs(q[2] - q[0]) > 0.15:
            angles.append(math.atan2((q[3] - q[1]) * H, (q[2] - q[0]) * W))
    return statistics.median(angles) if angles else 0.0


def rows_from_lines(lines, size=None):
    """Group the pieces read on one printed line (a label and its value are often read apart),
    left to right, joined by two spaces so the extractor can tell columns apart.

    With the image size and the reader's corner points, the page's tilt is taken out first, so a
    hand-held photo does not mix a value with the next line's label. Without them (Tesseract, tests),
    rows are grouped on the boxes as read."""
    W, H = size or (1, 1)
    a = _tilt(lines, W, H) if size else 0.0
    ca, sa = math.cos(-a), math.sin(-a)
    placed = []
    for ln in lines:
        x, y, w, h = ln["box"]
        cx, cy = (x + w / 2) * W, (y + h / 2) * H
        q = ln.get("quad")
        lh = math.hypot((q[6] - q[0]) * W, (q[7] - q[1]) * H) if (size and q) else h * H
        placed.append((cx * sa + cy * ca, cx * ca - cy * sa, lh, ln))   # (y, x) after taking out the tilt
    # straightened: label and value share a height; the next printed line is 1.2+ heights away.
    # as read (no tilt known): allow 0.75 of a line, since a slight tilt puts a value up to ~70% lower
    k = 0.6 if a else 0.75
    rows = []
    for ry, rx, lh, ln in sorted(placed, key=lambda t: t[0]):
        if rows and abs(ry - rows[-1]["cy"]) < k * min(lh, rows[-1]["h"]):
            r = rows[-1]
            r["items"].append((rx, ln))
            r["ys"].append(ry)
            r["cy"] = sum(r["ys"]) / len(r["ys"])
        else:
            rows.append({"cy": ry, "h": lh, "ys": [ry], "items": [(rx, ln)]})
    out = []
    for r in rows:
        items = [ln for _, ln in sorted(r["items"], key=lambda t: t[0])]
        text = "  ".join(i["text"].strip() for i in items)
        text = RUPEE_AS_R.sub("₹", RUPEE_AS_MARK.sub("₹", RUPEE_MISREAD.sub("₹", text)))
        text = COLON_AS_SEMI.sub(":", text)
        out.append({"text": text, "conf": min(i["conf"] for i in items), "boxes": [i["box"] for i in items]})
    return out


TITLES = {"tax_invoice": "TAX INVOICE", "invoice": "TAX INVOICE", "bill_of_supply": "BILL OF SUPPLY", "credit_note": "CREDIT NOTE",
          "debit_note": "DEBIT NOTE", "proforma": "PROFORMA", "quotation": "QUOTATION", "delivery_challan": "DELIVERY CHALLAN",
          "receipt": "RECEIPT", "unknown": "DOCUMENT"}
# inbox field <- bill field
FROM_BILL = {"vendor_name_text": "seller.name", "gstin": "seller.gstin", "invoice_no": "invoice_no", "invoice_date": "invoice_date",
             "po_no": "po_no", "qty": "qty", "amount_inr": "total", "account_no": "bank.account_no", "ifsc": "bank.ifsc",
             "account_name": "bank.account_holder"}


_DOMAIN_RE = re.compile(r"(?:[\w.+-]+@|\bwww\.|\b)((?:[a-z0-9][a-z0-9\-]*\.)+(?:com|in|co\.in|net|org|io|co|biz|info))\b", re.I)


def seller_domain(rec):
    """The one company web domain printed on the bill (a website such as goindigo.in or www.dtdc.com, or the domain of
    an email such as corporate@goindigo.in), so the background check can look the seller up by domain. Free-mail
    domains are ignored. If the bill shows more than one company domain, none is used: no guess about which is the seller's."""
    from .ap import FREE_MAIL
    found = set()
    for r in rec.get("rows") or []:
        for m in _DOMAIN_RE.finditer(r.get("text") or ""):
            d = m.group(1).lower().strip(".")
            d = d[4:] if d.startswith("www.") else d
            if d not in FREE_MAIL and not re.fullmatch(r"[\d.]+", d):
                found.add(d)
    return found.pop() if len(found) == 1 else None


def _text(rec):
    """The inbox document for a scan: written from the bill read off the photo, one labelled line per field, so the
    invoice checks read exactly what was confirmed (and nothing from stray text such as a browser bar in the photo)."""
    b = rec.get("bill")
    if not b:
        return f"Scanned-From: {rec['photo']}\n" + "\n".join(r["text"] for r in rec["rows"]) + "\n"
    F = b["fields"]

    def v(k):
        f = F.get(k)
        return f["value"] if f and f["value"] not in (None, "") else None
    payable = b["doc_type"] in ("tax_invoice", "invoice", "bill_of_supply", "unknown")
    out = [f"Scanned-From: {rec['photo']}", TITLES.get(b["doc_type"], "DOCUMENT")]
    if v("seller.name"):
        out.append(f"Vendor: {v('seller.name')}")
    dom = seller_domain(rec)
    if dom:
        out.append(f"Website: www.{dom}")
    if v("seller.gstin"):
        out.append(f"GSTIN: {v('seller.gstin')}")
    if v("invoice_no"):
        out.append(f"{'Invoice No' if payable else 'Document No.'}: {v('invoice_no')}")
    if v("invoice_date"):
        out.append(f"Invoice Date: {bill.show_date(v('invoice_date'))}")
    if v("po_no"):
        out.append(f"PO No: {v('po_no')}")
    first = next((i["description"] for i in b.get("items", []) if i.get("description")), None)
    if first:
        out.append(f"Description: {first}")
    if v("qty") is not None:
        out.append(f"Qty: {v('qty'):g}")
    if v("total") is not None:
        out.append(f"Total Amount Payable: INR {bill.inr(v('total'))}")
    if v("bank.account_no"):
        out.append(f"Account No: {v('bank.account_no')}" + (f"  IFSC: {v('bank.ifsc')}" if v("bank.ifsc") else ""))
    if v("bank.account_holder"):
        out.append(f"Account Name: {v('bank.account_holder')}")
    return "\n".join(out) + "\n"


# ---------------- citations (used by ap.process_file) ----------------

def _num(v):
    return ("%f" % v).rstrip("0").rstrip(".")


def _flat(s):
    return re.sub(r"[\s,]", "", str(s)).lower()


def _row_for(field, value, rows):
    if field == "amount_inr":
        for i, r in enumerate(rows):
            m = ap.AMOUNT_RE.search(r["text"])
            if m and float(m.group(1).replace(",", "")) == value:
                return i
    needle = _flat(_num(value) if isinstance(value, float) else value)
    return next((i for i, r in enumerate(rows) if needle and needle in _flat(r["text"])), None)


def attach(path, text, x):
    """If `path` is a scan this module wrote (its record matches the text exactly), add x["scan"]:
    the photo, the reader, and the photo row each field was read from. Typed or pasted text that
    only claims to be a scan gets nothing, so it keeps every email check."""
    m = re.fullmatch(r"(" + NAME_RE.pattern + r")\.txt", os.path.basename(path))
    rec_path = m and os.path.join(scans_dir(), m.group(1) + ".json")
    if not rec_path or not os.path.isfile(rec_path):
        return
    with open(rec_path, encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("text_sha") != _sha(text.encode()):
        return
    fields = {}
    if rec.get("bill"):
        for xk, bk in FROM_BILL.items():
            f = rec["bill"]["fields"].get(bk)
            if x.get(xk) is not None and f:
                fields[xk] = {"row": (f["rows"][0] + 1) if f.get("rows") else None, "text": f["printed"] or "", "conf": f["conf"] if f["boxes"] else 1.0,
                              "boxes": f["boxes"], "status": f["status"], "note": f.get("note"), "jev": f.get("jev")}
        x["bill"] = rec["bill"]
    else:
        for f in FIELDS:
            if x.get(f) is not None:
                i = _row_for(f, x[f], rec["rows"])
                if i is not None:
                    r = rec["rows"][i]
                    fields[f] = {"row": i + 1, "text": r["text"], "conf": r["conf"], "boxes": r["boxes"]}
    x["scan"] = {"photo": rec["photo"], "reader": rec["reader"], "read_ms": rec["ms"], "fields": fields}


def check_legibility(x):
    s = x["scan"]
    ev = [{"source": f"photo {s['photo']}", "field": k,
           "value": (f"row {c['row']}: " if c.get("row") else "") + f"\"{c['text']}\" (confidence {c['conf']:.2f})"
                    + (f"; {c['status']}" if c.get("status") else "") + (f"; Jev {c['jev']}" if c.get("jev") else "")}
          for k, c in s["fields"].items()]
    b = x.get("bill") or {}
    ev += [{"source": f"photo {s['photo']}", "field": "check: " + c["name"].replace("_", " "), "value": ("ok: " if c["ok"] else "does not hold: ") + c["detail"]}
           for c in b.get("checks", [])]
    if x["doc_type"] in ("invoice", "unknown"):
        missing = [label for label, ok in (("invoice number", x.get("invoice_no")), ("amount", x.get("amount_inr")),
                                           ("GSTIN or vendor name", x.get("gstin") or x.get("vendor_name_text"))) if not ok]
        if missing:
            hint = b.get("quality_note") or "Rescan the bill in better light, or key it in."
            return ap.chk("legibility", "fail", f"Could not read the {', '.join(missing)} from the photo {s['photo']}. Not guessed. {hint}", ev, weight=80)
    conflict = [k for k, c in s["fields"].items() if c.get("status") == "conflict"]
    if conflict:
        notes = "; ".join(f"{k}: {s['fields'][k].get('note')}" for k in conflict)
        return ap.chk("legibility", "warn", f"Rules and Jev disagree on {', '.join(conflict)} ({notes}). Held for a person to compare with the photo", ev, weight=30)
    broken = [c for c in b.get("checks", []) if c["name"] in ("taxable_plus_tax_equals_total", "total_matches_words") and not c["ok"]]
    if broken:
        return ap.chk("legibility", "warn", "The bill's own figures do not agree: " + "; ".join(c["detail"] for c in broken), ev, weight=30)
    low = [k for k, c in s["fields"].items() if c["conf"] < CONFIG["min_read_confidence"]]
    if low:
        return ap.chk("legibility", "warn", f"Read {', '.join(low)} from the photo with low confidence; compare with the photo before approving", ev, weight=30)
    return ap.chk("legibility", "pass", f"Read by {READER_LABEL.get(s['reader'], s['reader'])} on this machine in {s['read_ms']} ms; "
                  "each field is quoted from a row of the photo", ev)


# ---------------- entry point ----------------

def scan_bytes(data):
    """Store the photo, read it (once), write the rebuilt text to the inbox, run the invoice checks."""
    if len(data) > MAX_BYTES:
        raise ScanError("Photo is larger than 15 MB.")
    ext = _photo_ext(data)
    h = _sha(data)[:16]
    name = f"scan_{h}"
    os.makedirs(scans_dir(), exist_ok=True)
    photo = os.path.join(scans_dir(), name + ext)
    if not os.path.exists(photo):
        _write(photo, data)
    rec_path = os.path.join(scans_dir(), name + ".json")
    hit = os.path.exists(rec_path)
    if hit:
        with open(rec_path, encoding="utf-8") as f:
            rec = json.load(f)
        # the OCR is cached (slow, and the photo has not changed), but the field reader is re-run on the cached lines
        # (fast, local), so a fix to the reader also applies to a bill that was uploaded before the fix
        b = bill.parse(rec["lines"], rec.get("size"))
        bill.review_with_jev(b, f"inv:{name}.txt")
        rec["bill"] = bill.public(b)
        sha = _sha(_text(rec).encode())   # the text can change without the fields changing (e.g. the Website line)
        if sha != rec.get("text_sha"):
            rec["text_sha"] = sha
            _write(rec_path, json.dumps(rec).encode())
    else:
        t0 = time.time()
        r = read_image(photo)
        rec = {"photo": name + ext, "reader": r["reader"], "ms": int((time.time() - t0) * 1000), "size": r.get("size"),
               "lines": r["lines"], "rows": rows_from_lines(r["lines"], size=r.get("size"))}
        b = bill.parse(r["lines"], r.get("size"))
        bill.review_with_jev(b, f"inv:{name}.txt")   # second opinion on fields with several candidates; cached, never blocks
        rec["bill"] = bill.public(b)
        rec["text_sha"] = _sha(_text(rec).encode())
        _write(rec_path, json.dumps(rec).encode())
    store.ledger_add(f"inv:{name}.txt", f"local_ocr/{rec['reader']}", h, hit, 0.0, 0 if hit else rec["ms"], None)
    txt = os.path.join(ap.FIX, "inbox", name + ".txt")
    text = _text(rec)
    old = None
    if os.path.exists(txt):
        with open(txt, encoding="utf-8") as f:
            old = f.read()
    if old != text:
        _write(txt, text.encode())
    out = ap.process_file(txt)
    return {**out, "photo": rec["photo"], "reader": rec["reader"], "read_ms": rec["ms"], "read_again": not hit}
