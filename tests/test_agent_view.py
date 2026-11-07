"""Agent View fixtures: each one carries a problem a person would not see and a
machine would. The tests check the probe names it, and that the clean body is clean."""

import json
import tempfile
import unittest
from pathlib import Path

from atlas_site_probe.agent_view import (analyze_capture, check_body, check_feed, check_llms,
                                         check_product_page, check_robots, diff_findings, html_to_text,
                                         append_catch_candidates, census_surfaces, census_table, detect_platform)

FIX = Path(__file__).parent / "fixtures" / "agent_view"


def body(name):
    return (FIX / name).read_text()


class BodyTests(unittest.TestCase):
    def test_cta_first_is_flagged_and_reads_first_shows_it(self):
        f, first = check_body("Sandy Dune N | Elevated Contemporary Coastal House Plan", body("cta_first.html"))
        self.assertIn("product description opens with a call to action before the product", f)
        self.assertTrue(first.startswith("Would you like to customize this plan?"))

    def test_product_named_before_a_quote_word_is_clean(self):
        # A study set that names itself first, then says "builder quotes", is not CTA-first.
        html = ("<p>The Casa Azul Study Set includes a fully dimensioned first and second floor plan, "
                "the roof plan, and exterior elevations from the front, rear, left and right, enough to get "
                "builder quotes and check the design against your lot. Not for construction.</p>")
        f, _ = check_body("Casa Azul - Study Set", html)
        self.assertFalse(any("call to action" in x for x in f), f)

    def test_css_first_is_flagged(self):
        f, first = check_body("House Plans", body("css_first.html"), kind="collection")
        self.assertIn("collection description carries CSS that reaches plain-text readers", f)
        self.assertIn("{", first)  # the CSS really is what a plain-text reader gets first

    def test_placeholder_is_flagged(self):
        f, _ = check_body("Little Oak", body("placeholder_first.html"))
        self.assertIn("product description carries an unrendered template placeholder", f)

    def test_clean_body_has_no_findings(self):
        f, first = check_body("Sandy Dune N | Elevated Contemporary Coastal House Plan", body("clean.html"))
        self.assertEqual(f, [])
        self.assertTrue(first.startswith("Sandy Dune N House Plan"))

    def test_empty_body(self):
        f, _ = check_body("Anything", "<p></p>")
        self.assertIn("product description is empty or near-empty", f)

    def test_html_to_text_keeps_style_text_like_a_naive_reader(self):
        text, has_style, _ = html_to_text(body("css_first.html"))
        self.assertTrue(has_style)
        self.assertTrue(text.startswith(".cd p{"))


class SiteFileTests(unittest.TestCase):
    def test_llms_wrong_count(self):
        f, info = check_llms(body("llms_wrong_count.txt"), live_count=68)
        self.assertEqual(f, ["llms.txt says 56 plans, live catalog has 68"])
        self.assertIn(56, info["stated_counts"])

    def test_llms_ignores_cents_and_collection_counts(self):
        text = ("# Store\nPDF sets start at $1,040.95 for house plans and $926.95 for garage plans.\n"
                "Thirty-eight plans sit in the Elevated collection; 38 plans are drawn on pilings and 21 plans have an elevator.")
        f, info = check_llms(text, live_count=68)
        self.assertEqual(f, [])
        self.assertNotIn(95, info["stated_counts"])
        # one stated count above the catalog is still a finding
        self.assertEqual(check_llms("We sell 80 plans.", live_count=68)[0], ["llms.txt says 80 plans, live catalog has 68"])

    def test_llms_soft_404_counts_as_missing(self):
        page = "<!DOCTYPE html><html><head><title>House Plans</title></head><body>Not found</body></html>"
        self.assertEqual(check_llms(page, live_count=10)[0], ["no llms.txt"])
        self.assertFalse(census_surfaces({"llms_txt": page})["llms_txt"])

    def test_llms_right_count_and_missing(self):
        self.assertEqual(check_llms(body("llms_wrong_count.txt"), live_count=56)[0], [])
        self.assertEqual(check_llms(None, 10)[0], ["no llms.txt"])

    def test_robots_blocks_gptbot_and_noindex_page(self):
        f, info = check_robots(body("robots_blocks_gptbot.txt"), noindex_paths=["/products/sandy-dune-study-set", "/products/sandy-dune"])
        self.assertIn("robots.txt blocks GPTBot", f)
        self.assertEqual(info["ai_bots"]["ClaudeBot"], "allowed")
        self.assertEqual(info["blocked_noindex"], ["/products/sandy-dune-study-set"])
        self.assertTrue(any(x.startswith("1 noindex page(s) are blocked") for x in f))

    def test_feed_wrong_price(self):
        products = [{"handle": "sandy-dune-n", "variants": [{"price": "1895.95"}]},
                    {"handle": "little-oak", "variants": [{"price": "1372.95"}, {"price": "1622.95"}]}]
        f, info = check_feed(body("feed_wrong_price.xml"), products)
        self.assertEqual(info["matched"], 2)
        self.assertEqual(info["mismatched"], 1)
        self.assertEqual(info["examples"][0]["handle"], "sandy-dune-n")
        self.assertIn("feed price differs from the store on 1 of 2 rows (50%)", f)

    def test_product_page_jsonld_cta_and_price(self):
        f, info = check_product_page(body("product_page_cta_jsonld.html"), live_price=1895.95)
        self.assertIn("structured data description opens with a call to action", f)
        self.assertFalse(any("differs from live price" in x for x in f))
        f2, _ = check_product_page(body("product_page_cta_jsonld.html"), live_price=1995.00)
        self.assertTrue(any("differs from live price" in x for x in f2))
        self.assertTrue(info["meta_description"].startswith("Sandy Dune N:"))


class CaptureTests(unittest.TestCase):
    def capture(self, with_cta=True):
        return {
            "site": "shop.test", "platform": "shopify", "checked_at": "2026-11-05T20:00:00",
            "products": [{"handle": "sandy-dune-n", "title": "Sandy Dune N | Elevated Coastal House Plan",
                          "body_html": body("cta_first.html" if with_cta else "clean.html"), "variants": [{"price": "1895.95"}]}],
            "collections": [{"handle": "house-plans", "title": "House Plans", "body_html": body("css_first.html")}],
            "ucp_profile": {"ucp": {"version": "2026-08-25", "services": {}, "capabilities": {}}},
            "ucp_tools": ["search_catalog", "get_product"], "legacy_tools": ["search_shop_policies_and_faqs"],
            "searches": {"elevated coastal house plan": [{"title": "Sandy Dune N", "description": {"html": body("cta_first.html")}}]},
            "llms_txt": body("llms_wrong_count.txt"), "live_count": 68,
            "robots_txt": body("robots_blocks_gptbot.txt"), "noindex_paths": ["/products/sandy-dune-study-set"],
            "product_page_html": body("product_page_cta_jsonld.html"), "product_page_price": 1895.95,
            "feed_text": body("feed_wrong_price.xml"),
        }

    def test_full_capture_reports_every_class(self):
        r = analyze_capture(self.capture())
        joined = " | ".join(r.findings)
        for needle in ["call to action before the product", "carries CSS", "llms.txt says 56", "blocks GPTBot",
                       "noindex page(s) are blocked", "structured data description opens", "feed price differs"]:
            self.assertIn(needle, joined, needle)
        self.assertEqual(r.ucp_version, "2026-08-25")
        self.assertEqual(r.legacy_mcp_tools, ["search_shop_policies_and_faqs"])
        self.assertTrue(r.reads_first["product"].startswith("Would you like"))
        self.assertTrue(r.reads_first["search"].startswith("Would you like"))
        row = r.to_row()
        self.assertEqual(row["ai_bots_blocked"], "GPTBot")
        self.assertEqual(row["feed_mismatched"], 1)

    def test_diff_shows_the_fix(self):
        before = analyze_capture(self.capture(with_cta=True))
        after = analyze_capture(self.capture(with_cta=False))
        d = diff_findings(after.findings, before.findings)
        self.assertTrue(any("call to action before the product" in x for x in d["fixed"]))
        self.assertEqual(d["new"], [])

    def test_missing_ucp_tools_is_a_finding(self):
        cap = self.capture()
        cap["ucp_tools"] = []
        r = analyze_capture(cap)
        self.assertTrue(any(x.startswith("UCP endpoint lists no tools") for x in r.findings))

    def test_census_surfaces_and_table(self):
        cap = self.capture()
        cap["mcp_card"] = {"name": "Shop Test MCP", "tools": ["a", "b"]}
        cap["mcp_tools"] = ["search_plans", "get_plan"]
        cap["group"] = "never contacted"
        r = analyze_capture(cap)
        self.assertEqual(r.surfaces["summary"], "ucp_profile;ucp_catalog;mcp_card;llms_txt;mcp_endpoint(2 tools)")
        self.assertEqual(r.surfaces["mcp_card_name"], "Shop Test MCP")
        row = r.to_row()
        self.assertEqual(row["mcp_tools"], "search_plans;get_plan")
        self.assertEqual(row["group"], "never contacted")
        table = census_table([r])
        self.assertIn("| shop.test | never contacted | shopify | yes | yes | yes | search_plans, get_plan | yes | 2026-11-05 |", table)

    def test_census_none_for_a_plain_site(self):
        s = census_surfaces({"site": "plain.test", "platform": "wordpress", "llms_txt": None})
        self.assertEqual(s["summary"], "none")
        self.assertFalse(s["ucp_catalog"])

    def test_detect_platform(self):
        self.assertEqual(detect_platform('<script src="https://cdn.shopify.com/x.js">'), "shopify")
        self.assertEqual(detect_platform('<link href="/wp-content/plugins/woocommerce/a.css">'), "woocommerce")
        self.assertEqual(detect_platform('<link href="/wp-content/themes/divi/a.css">'), "wordpress")
        self.assertEqual(detect_platform('<p>hand built</p>'), "")

    def test_catch_log_append(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "catch.md"
            log.write_text("# Log\n\n| Date | Where |\n|---|---|\n| 2026-11-01 | x |\n")
            append_catch_candidates(log, "shop.test", ["robots.txt blocks GPTBot"], "2026-11-05T20:00:00")
            text = log.read_text()
            self.assertIn("## Probe found, not yet confirmed", text)
            self.assertIn("| 2026-11-05 | shop.test | robots.txt blocks GPTBot |", text)
            append_catch_candidates(log, "shop.test", ["second"], "2026-11-06")
            self.assertEqual(log.read_text().count("## Probe found"), 1)


if __name__ == "__main__":
    unittest.main()
