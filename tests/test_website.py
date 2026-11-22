"""Offline tests for the vendor website fetch: a site that blocks automated fetches is retried with human_mode
instead of holding an honest vendor for 'website did not load'.

    python3 -m unittest tests.test_website -v
"""
import os
import shutil
import tempfile
import time
import unittest

from vendorcheck import crust, pipeline, store

FULL = "<html><body><h1>Delhivery</h1><p>" + "India's largest fully integrated logistics provider. " * 10 + "</p></body></html>"
SHELL = "<html><body><div id='root'></div></body></html>"


class FakeFetch:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, job_id, endpoint, body, use_cache=True):
        assert endpoint == "/web/enrich/live"
        key = (body["urls"][0], bool(body.get("human_mode")))
        self.calls.append(key)
        content = self.pages.get(key)
        meta = {"retrieved_at": time.time(), "cache_hit": False, "endpoint": endpoint, "req_hash": "t"}
        return [{"success": content is not None, "url": body["urls"][0], "title": "Delhivery" if content else None, "content": content}], meta


class WebsiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store._conn = None
        store.DB_PATH = os.path.join(self.tmp, "t.db")
        self.real = crust.call
        self.job = pipeline.Job({"vendor_name": "Delhivery", "website": "delhivery.com", "request_id": "t"})

    def tearDown(self):
        crust.call = self.real
        store._conn = None
        shutil.rmtree(self.tmp)

    def fetch(self, pages):
        crust.call = f = FakeFetch(pages)
        return self.job.s_website("https://delhivery.com"), f.calls

    def test_open_site_needs_one_call(self):
        w, calls = self.fetch({("https://delhivery.com", False): FULL})
        self.assertTrue(w["ok"])
        self.assertEqual(w["fetched_with"], "standard")
        self.assertEqual(len(calls), 1)                      # no extra credits for a site that loads

    def test_blocked_site_is_retried_with_human_mode(self):
        w, calls = self.fetch({("https://delhivery.com", False): SHELL, ("https://delhivery.com", True): FULL})
        self.assertTrue(w["ok"])
        self.assertGreaterEqual(len(w["text"]), 200)
        self.assertEqual(w["fetched_with"], "human_mode")
        self.assertEqual(calls, [("https://delhivery.com", False), ("https://delhivery.com", True)])

    def test_www_host_tried_last(self):
        w, calls = self.fetch({("https://www.delhivery.com", True): FULL})
        self.assertTrue(w["ok"])
        self.assertEqual(w["url"], "https://www.delhivery.com")
        self.assertEqual(len(calls), 3)

    def test_site_that_never_loads_still_reports_failure(self):
        w, calls = self.fetch({("https://delhivery.com", False): SHELL})
        self.assertLess(len(w["text"]), 200)                 # the caller flags it; no guess that the site is fine
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
