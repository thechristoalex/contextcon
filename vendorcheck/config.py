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
    "inr_per_credit": float(os.environ.get("INR_PER_CREDIT", "25")),
}

ADVERSE_TERMS = [
    "fraud", "scam", "arrested", "arrest", "raid", "ed raid", "cbi", "money laundering", "cheating",
    "insolvency", "nclt", "liquidation", "bankrupt", "default", "wilful defaulter", "penalty",
    "sebi order", "gst evasion", "fake invoice", "tax evasion", "shut down", "shuts down", "blacklisted",
    "ponzi", "forgery", "embezzlement", "lawsuit", "sued",
]
