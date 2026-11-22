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
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata

from . import ap, store
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
    r = subprocess.run([_apple_bin(), path], capture_output=True, text=True, timeout=180, check=True)
    return json.loads(r.stdout)


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
            return {"reader": name, "lines": _apple(path) if name == "apple_vision" else _tesseract(path)}
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

def rows_from_lines(lines):
    """Group the pieces read on one printed line (a label and its value are often read apart),
    left to right, joined by two spaces so the extractor can tell columns apart."""
    rows = []
    for ln in sorted(lines, key=lambda l: l["box"][1] + l["box"][3] / 2):
        cy, h = ln["box"][1] + ln["box"][3] / 2, ln["box"][3]
        # 0.75 of a line height: a tilted photo puts a value up to ~70% of a line below its label; the next printed line is 1.2+ heights away
        if rows and abs(cy - rows[-1]["cy"]) < 0.75 * min(h, rows[-1]["h"]):
            r = rows[-1]
            r["items"].append(ln)
            r["cy"] = sum(i["box"][1] + i["box"][3] / 2 for i in r["items"]) / len(r["items"])
        else:
            rows.append({"cy": cy, "h": h, "items": [ln]})
    out = []
    for r in rows:
        items = sorted(r["items"], key=lambda l: l["box"][0])
        text = "  ".join(i["text"].strip() for i in items)
        out.append({"text": RUPEE_AS_R.sub("₹", RUPEE_MISREAD.sub("₹", text)), "conf": min(i["conf"] for i in items),
                    "boxes": [i["box"] for i in items]})
    return out


def _text(rec):
    return f"Scanned-From: {rec['photo']}\n" + "\n".join(r["text"] for r in rec["rows"]) + "\n"


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
    for f in FIELDS:
        if x.get(f) is not None:
            i = _row_for(f, x[f], rec["rows"])
            if i is not None:
                r = rec["rows"][i]
                fields[f] = {"row": i + 1, "text": r["text"], "conf": r["conf"], "boxes": r["boxes"]}
    x["scan"] = {"photo": rec["photo"], "reader": rec["reader"], "read_ms": rec["ms"], "fields": fields}


def check_legibility(x):
    s = x["scan"]
    ev = [{"source": f"photo {s['photo']}", "field": k, "value": f"row {c['row']}: \"{c['text']}\" (confidence {c['conf']:.2f})"}
          for k, c in s["fields"].items()]
    if x["doc_type"] in ("invoice", "unknown"):
        missing = [label for label, ok in (("invoice number", x.get("invoice_no")), ("amount", x.get("amount_inr")),
                                           ("GSTIN or vendor name", x.get("gstin") or x.get("vendor_name_text"))) if not ok]
        if missing:
            return ap.chk("legibility", "fail", f"Could not read the {', '.join(missing)} from the photo {s['photo']}. Not guessed: "
                          "rescan the bill in better light, or key it in.", ev, weight=80)
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
    else:
        t0 = time.time()
        r = read_image(photo)
        rec = {"photo": name + ext, "reader": r["reader"], "ms": int((time.time() - t0) * 1000),
               "lines": r["lines"], "rows": rows_from_lines(r["lines"])}
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
