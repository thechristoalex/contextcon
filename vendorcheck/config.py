"""Run limits and rules. Change these (or set env vars) when the organizers change a requirement."""
import os


def _load_env():
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

CONFIG = {
    # cost limit per job, in Crustdata credits
    "max_credits_per_job": float(os.environ.get("MAX_CREDITS_PER_JOB", "12")),
    # time limit per job, seconds
    "max_seconds_per_job": float(os.environ.get("MAX_SECONDS_PER_JOB", "180")),
    "http_timeout_s": 60,
    "max_retries": 3,
    # entity resolution: identify matches at or above this confidence count as "strong"
    "strong_match": 0.95,
    # a company founded fewer than this many years ago is flagged as newly formed
    "min_age_years": 1,
    # headcount below this is flagged
    "min_headcount": 5,
    # how many people (founders / CXOs) to screen for adverse media
    "max_people_screened": 2,
    # rupee value of 1 Crustdata credit, for the per-job cost in the report
    "auto_approve_below_inr": float(os.environ.get("AUTO_APPROVE_BELOW_INR", "0")),
    "inr_per_credit": float(os.environ.get("INR_PER_CREDIT", "25")),
    # Jev (TypeSafe decision model). Pinned version: an alias like jev-latest can move and change a re-run's verdict.
    "jev_enabled": os.environ.get("JEV_ENABLED", "1") == "1",
    "jev_model": os.environ.get("JEV_MODEL", "jev-1.14.0"),
    "jev_yes": 0.80,          # yes/no answer at or above this reads as yes
    "jev_no": 0.20,           # at or below this reads as no; in between is "cannot tell", never "medium"
    "jev_choice_min": 0.80,   # a choice counts only at this confidence
    "jev_score_min": 0.70,    # a score level counts only if its own probability clears this
    "jev_quote_min": 0.50,    # pointing at a sentence to quote (code checks the quote is really in the text)
    "jev_timeout_s": 20,
    "jev_retries": 2,
    # a field read from a bill photo below this confidence (0..1) holds the bill for a person to compare
    "approver_name": os.environ.get("APPROVER_NAME", "AP lead"),
    "min_read_confidence": float(os.environ.get("MIN_READ_CONFIDENCE", "0.5")),
}

ADVERSE_TERMS = [
    # legal / regulatory events only. Consumer complaints ("fake delivery", "scam call") are not adverse media.
    "arrested", "arrest", "raid", "raids", "fir", "chargesheet", "charge sheet", "ed", "cbi", "sfio", "money laundering",
    "insolvency", "nclt", "liquidation", "winding up", "bankruptcy", "wilful defaulter", "sebi order", "sebi bars",
    "gst evasion", "tax evasion", "fake invoices", "bogus firms", "shell company", "ponzi", "forgery", "embezzlement",
    "convicted", "debarred", "blacklisted", "fraud case", "cheating case",
]
