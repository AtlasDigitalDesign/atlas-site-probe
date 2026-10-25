"""Each fixture carries a known defect. The tests assert the probe reports it,
and that the clean page reports nothing it shouldn't."""

import http.server
import threading
import unittest
from pathlib import Path

from atlas_site_probe import analyze, probe
from atlas_site_probe.hubspot import company_name, note_body, push_rows

FIX = Path(__file__).parent / "fixtures"


def page(name, url="https://shop.test/", headers=None, status=200):
    return analyze(url, url, status, headers or {}, (FIX / name).read_text())


class AnalyzeTests(unittest.TestCase):
    def test_clean_page_has_no_findings(self):
        r = page("clean.html")
        self.assertEqual(r.findings, [])
        self.assertTrue(r.canonical_matches)
        self.assertEqual(r.jsonld_blocks, 2)
        self.assertEqual(r.jsonld_broken, 0)
        self.assertEqual(r.schema_types, ["IndividualProduct", "Organization", "Product", "WebSite"])
        self.assertEqual(r.tags["ga4"], ["G-ABC1234567"])

    def test_broken_jsonld_is_caught(self):
        r = page("broken_jsonld.html", url="https://shop.test/broken")
        self.assertEqual(r.jsonld_broken, 1)
        self.assertIn("1 structured data block(s) do not parse", r.findings)

    def test_canonical_pointing_elsewhere_and_duplicates(self):
        r = page("canonical_elsewhere.html", url="https://shop.test/products/plan-a")
        self.assertFalse(r.canonical_matches)
        self.assertIn("canonical points to a different URL", r.findings)
        self.assertIn("2 canonical tags on one page", r.findings)
        self.assertIn("2 H1 headings (expected 1)", r.findings)
        self.assertEqual(r.tags["google_ads"], ["AW-123456789"])

    def test_noindex_in_meta(self):
        r = page("noindex_meta.html", url="https://shop.test/hidden")
        self.assertTrue(r.noindex)
        self.assertIn("page is marked noindex", r.findings)
        self.assertTrue(r.canonical_matches)  # relative canonical resolves to itself

    def test_noindex_in_header_only(self):
        r = page("clean.html", headers={"x-robots-tag": "noindex"})
        self.assertTrue(r.noindex)
        self.assertIn("page is marked noindex", r.findings)

    def test_trailing_slash_is_not_a_mismatch(self):
        r = analyze("https://shop.test", "https://shop.test", 200, {}, (FIX / "clean.html").read_text())
        self.assertTrue(r.canonical_matches)

    def test_missing_tags_and_platform(self):
        r = page("shopify_like.html")
        self.assertEqual(r.platform, "shopify")
        self.assertEqual(r.tags["meta_pixel"], ["1234567890123"])
        self.assertTrue(any(f.startswith("no GA4 or GTM tag") for f in r.findings))

    def test_http_error_status(self):
        r = page("clean.html", status=404)
        self.assertIn("page returned HTTP 404", r.findings)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/noindex-header":
            body = (FIX / "clean.html").read_bytes()
            self.send_response(200)
            self.send_header("X-Robots-Tag", "noindex")
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_header_noindex_over_the_wire(self):
        r = probe(self.base + "/noindex-header")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.noindex)

    def test_404_over_the_wire(self):
        r = probe(self.base + "/missing")
        self.assertEqual(r.status, 404)
        self.assertIn("page returned HTTP 404", r.findings)

    def test_unreachable_host(self):
        r = probe("http://127.0.0.1:9/")
        self.assertIsNone(r.status)
        self.assertTrue(r.findings[0].startswith("could not fetch"))


class HubSpotDryRunTests(unittest.TestCase):
    def test_threshold_and_dry_run(self):
        rows = [page("clean.html").to_row(),
                page("canonical_elsewhere.html", url="https://www.shop.test/products/plan-a").to_row()]
        log = push_rows(rows, min_findings=2, dry_run=True)
        self.assertEqual(log[0]["action"], "skipped (below threshold)")
        self.assertTrue(log[1]["action"].startswith("would push shop.test"))

    def test_company_name_from_title(self):
        self.assertEqual(company_name("Atlas Digital Design · Growth engineering and software", "x.test"), "Atlas Digital Design")
        self.assertEqual(company_name("Atlas of Plant Based: the PCT on plant based food", "x.test"), "Atlas of Plant Based")
        self.assertEqual(company_name("Shop | Home", "x.test"), "Shop")
        self.assertEqual(company_name("", "x.test"), "x.test")

    def test_note_lists_each_finding(self):
        row = page("canonical_elsewhere.html", url="https://shop.test/x").to_row()
        html = note_body(row)
        for f in row["findings"].split(" | "):
            self.assertIn(f, html)


if __name__ == "__main__":
    unittest.main()
