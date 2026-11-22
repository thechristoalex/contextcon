"""Import before vendorcheck in every test: the tests never call a live model or API,
even when a key is set in the shell. Jev falls back to the rules, as it does with no key."""
import os

os.environ["JEV_ENABLED"] = "0"
os.environ.pop("TYPESAFE_API_KEY", None)
