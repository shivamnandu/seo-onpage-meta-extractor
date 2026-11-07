"""Deterministic unit tests for extraction, validation, URL handling and output columns.

Run:  python -m unittest discover -s tests -v
"""
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seo_extractor.fetcher import detect_challenge  # noqa: E402
from seo_extractor.models import PageResult  # noqa: E402
from seo_extractor.output_writer import (  # noqa: E402
    build_columns,
    data_row,
    page_row,
    page_to_json,
    write_csv,
    write_data_csv,
)
from seo_extractor.parser import HTMLParser  # noqa: E402
from seo_extractor.rate_limiter import RateLimiter  # noqa: E402
from seo_extractor.url_source import finalize, parse_sitemap_xml, read_url_entries  # noqa: E402
from seo_extractor.utils import host_matches, normalize_url  # noqa: E402
from seo_extractor.validators import parse_robots  # noqa: E402

PAGE = "https://example.com/page/"


def parse(html: str, url: str = PAGE):
    return HTMLParser().parse(html.encode("utf-8"), {url})


def by_key(params):
    out = {}
    for p in params:
        out.setdefault(p.key, []).append(p)
    return out


class Disambiguation(unittest.TestCase):
    HTML = """<html><head>
      <title>Best SEO Tools 2025</title>
      <meta name="description" content="A real meta description that is comfortably long enough to pass the length check.">
      <meta property="og:description" content="OG description">
      <meta name="twitter:description" content="Twitter description">
      <meta itemprop="description" content="schema.org description">
      <meta property="og:title" content="OG title">
      <meta name="twitter:title" content="Twitter title">
      <meta property="twitter:image" content="https://example.com/t.png">
      <meta name="og:image" content="https://example.com/wrong-attribute.png">
      <link rel="canonical" href="https://example.com/page/">
      <link rel="amphtml" href="https://example.com/amp/">
      <link rel="alternate" type="application/rss+xml" href="https://example.com/feed">
      <link rel="alternate" hreflang="en-US" href="https://example.com/page/">
      <link rel="alternate" hreflang="x-default" href="https://example.com/">
    </head><body><h1 class="t">Best <b>SEO</b> Tools</h1></body></html>"""

    def test_no_cross_contamination(self):
        k = by_key(parse(self.HTML))
        self.assertEqual(len(k["meta_description"]), 1)
        self.assertIn('name="description"', k["meta_description"][0].raw)
        self.assertEqual(k["og_description"][0].value, "OG description")
        self.assertEqual(k["twitter_description"][0].value, "Twitter description")
        self.assertEqual(k["og_title"][0].value, "OG title")
        self.assertEqual(k["twitter_title"][0].value, "Twitter title")
        self.assertNotIn("og_image", k)                    # name="og:image" is not Open Graph
        self.assertEqual(k["twitter_image"][0].value, "https://example.com/t.png")  # property= variant
        self.assertEqual(len(k["canonical"]), 1)            # amphtml / rss never canonical
        self.assertEqual(sorted(x for x in k if x.startswith("hreflang_")),
                         ["hreflang_en_us", "hreflang_x_default"])

    def test_raw_capture_and_values(self):
        k = by_key(parse(self.HTML))
        self.assertEqual(k["title"][0].raw, "<title>Best SEO Tools 2025</title>")
        self.assertEqual(k["canonical"][0].raw, '<link rel="canonical" href="https://example.com/page/">')
        self.assertEqual(k["h1"][0].raw, '<h1 class="t">Best <b>SEO</b> Tools</h1>')
        self.assertEqual(k["h1"][0].value, "Best SEO Tools")
        self.assertEqual(k["og_title"][0].raw, '<meta property="og:title" content="OG title">')

    def test_all_valid_page_is_parsable(self):
        statuses = {p.key: p.status for p in parse(self.HTML)}
        for key in ("title", "meta_description", "canonical", "og_title", "twitter_title",
                    "hreflang_en_us", "hreflang_x_default", "h1"):
            self.assertEqual(statuses[key], "parsable", key)


class MultipleInstances(unittest.TestCase):
    def test_numbering_and_conflicts(self):
        html = """<head><title>A</title><title>B</title>
        <link rel="canonical" href="https://a.com/1"><link rel="CANONICAL" href="https://a.com/2">
        </head><body><h1>One</h1><h1>Two</h1><h1>One</h1></body>"""
        k = by_key(parse(html))
        self.assertEqual([p.index for p in k["title"]], [1, 2])
        self.assertTrue(all(p.status == "unparsable" and "conflicting" in p.reason for p in k["title"]))
        self.assertEqual(len(k["canonical"]), 2)
        self.assertTrue(all(p.status == "unparsable" and "conflicting" in p.reason for p in k["canonical"]))
        self.assertEqual([p.index for p in k["h1"]], [1, 2, 3])
        self.assertTrue(all(p.status == "parsable" for p in k["h1"]))      # multiple h1 = warning
        self.assertTrue(any("Duplicate <h1>" in w for w in k["h1"][2].warnings))

    def test_identical_titles_are_parsable_with_warning(self):
        k = by_key(parse("<head><title>Same</title><title>Same</title></head><body><h1>x</h1></body>"))
        self.assertTrue(all(p.status == "parsable" for p in k["title"]))
        self.assertTrue(k["title"][0].warnings)

    def test_missing_title_and_h1_are_explicit(self):
        k = by_key(parse("<html><head></head><body><p>hi</p></body></html>"))
        self.assertEqual(k["title"][0].status, "unparsable")
        self.assertIn("Missing", k["title"][0].reason)
        self.assertEqual(k["h1"][0].status, "unparsable")
        self.assertEqual(k["h1"][0].raw, "")

    def test_inert_containers_ignored(self):
        html = '<head><noscript><meta name="description" content="hidden"></noscript></head><body><h1>x</h1></body>'
        self.assertNotIn("meta_description", by_key(parse(html)))


class MetaDescription(unittest.TestCase):
    def test_rules(self):
        long = "x" * 80
        k = by_key(parse(f'<head><title>t</title><meta name="description" content="{long}">'
                         f'<meta name="description" content="{long}">'
                         '<meta name="description"><meta name="description" content="  "></head><body><h1>h</h1></body>'))
        d = k["meta_description"]
        self.assertEqual(d[0].status, "parsable")
        self.assertEqual((d[1].status, d[1].reason.split(" (")[0]), ("unparsable", "Duplicate identical meta description"))
        self.assertEqual(d[2].reason, "Missing content attribute")
        self.assertEqual(d[3].reason, "Empty content attribute")

    def test_short_description_is_warning_only(self):
        k = by_key(parse('<head><meta name="description" content="short"></head>'))
        self.assertEqual(k["meta_description"][0].status, "parsable")
        self.assertTrue(k["meta_description"][0].warnings)


class Robots(unittest.TestCase):
    def status(self, content):
        k = by_key(parse(f'<head><meta name="robots" content="{content}"></head>'))
        p = k["meta_robots"][0]
        return p.status, p.reason

    def test_valid(self):
        for c in ("index, follow", "noindex,nofollow", "max-snippet:-1, max-image-preview:large, max-video-preview:0",
                  "NOINDEX", "none", "unavailable_after: 25 Jun 2030 15:00:00 GMT",
                  "unavailable_after: Monday, 25-Jun-30 15:00:00 PST, noarchive", "unavailable_after: 2030-06-25T15:00:00Z"):
            self.assertEqual(self.status(c)[0], "parsable", c)

    def test_invalid(self):
        self.assertIn("Conflicting", self.status("index, noindex")[1])
        self.assertIn("Conflicting", self.status("follow, nofollow")[1])
        self.assertIn("Conflicting", self.status("all, noindex")[1])
        self.assertIn("Unknown directive 'noodp'", self.status("noodp")[1])
        self.assertIn("requires a value", self.status("max-snippet")[1])
        self.assertIn("Invalid value", self.status("max-image-preview:huge")[1])
        self.assertIn("Unparseable date", self.status("unavailable_after: soon")[1])
        self.assertIn("does not take a value", self.status("noindex:1")[1])

    def test_bots_are_separate_columns(self):
        k = by_key(parse('<head><meta name="robots" content="index"><meta name="googlebot" content="noindex">'
                         '<meta name="bingbot" content="nofollow"></head>'))
        self.assertEqual(sorted(x for x in k if x.startswith("meta_robots")),
                         ["meta_robots", "meta_robots_bingbot", "meta_robots_googlebot"])

    def test_date_tokens_with_commas_stay_attached(self):
        d, errs, _ = parse_robots("noindex, unavailable_after: Monday, 25-Jun-30 15:00:00 PST, nofollow")
        self.assertEqual([n for n, _ in d], ["noindex", "unavailable_after", "nofollow"])
        self.assertEqual(errs, [])


class Canonical(unittest.TestCase):
    def status(self, tag):
        p = by_key(parse(f"<head>{tag}</head>"))["canonical"][0]
        return p.status, p.reason

    def test_rules(self):
        self.assertEqual(self.status('<link rel="canonical" href="https://a.com/x">')[0], "parsable")
        self.assertIn("Relative", self.status('<link rel="canonical" href="/x">')[1])
        self.assertIn("Missing href", self.status('<link rel="canonical">')[1])
        self.assertIn("scheme", self.status('<link rel="canonical" href="ftp://a.com/x">')[1])
        self.assertIn("Empty href", self.status('<link rel="canonical" href="">')[1])
        self.assertIn("Invalid URL syntax", self.status('<link rel="canonical" href="https://a.com:notaport/">')[1])


class OpenGraphAndTwitter(unittest.TestCase):
    def test_og(self):
        k = by_key(parse('<head><meta property="og:title" content="t"><meta property="og:bogus" content="x">'
                         '<meta property="og:image" content="/relative.png"><meta property="og:type">'
                         '<meta property="OG:Site_Name" content="x"><meta property="og:image" content="https://a.com/i.png"></head>'))
        self.assertEqual(k["og_title"][0].status, "parsable")
        self.assertIn("Unknown Open Graph property", k["og_bogus"][0].reason)
        self.assertIn("not a valid URL", k["og_image"][0].reason)
        self.assertEqual(k["og_image"][1].status, "parsable")
        self.assertEqual([p.index for p in k["og_image"]], [1, 2])
        self.assertEqual(k["og_type"][0].reason, "Missing content attribute")
        self.assertIn("Malformed property", k["og_site_name"][0].reason)

    def test_twitter(self):
        k = by_key(parse('<head><meta name="twitter:card" content="summary_large_image">'
                         '<meta name="twitter:card" content="bigpicture"><meta name="twitter:title" content="">'
                         '<meta name="twitter:site" content="@x"></head>'))
        self.assertEqual(k["twitter_card"][0].status, "parsable")
        self.assertIn("Unknown twitter:card", k["twitter_card"][1].reason)
        self.assertEqual(k["twitter_title"][0].reason, "Empty content attribute")
        self.assertEqual(k["twitter_site"][0].status, "parsable")


class Hreflang(unittest.TestCase):
    def test_codes_and_hrefs(self):
        k = by_key(parse('<head><link rel="alternate" hreflang="en-US" href="https://a.com/en">'
                         '<link rel="alternate" hreflang="en_GB" href="https://a.com/gb">'
                         '<link rel="alternate" hreflang="fr" href="/relative">'
                         '<link rel="alternate" hreflang="de">'
                         '<link rel="alternate" hreflang="" href="https://a.com/x">'
                         '<link rel="alternate" hreflang="X-Default" href="https://a.com/"></head>', url="https://a.com/en"))
        self.assertEqual(k["hreflang_en_us"][0].status, "parsable")
        self.assertIn("Invalid BCP-47", k["hreflang_en_gb"][0].reason)
        self.assertIn("Invalid href", k["hreflang_fr"][0].reason)
        self.assertEqual(k["hreflang_de"][0].reason, "Missing href attribute")
        self.assertEqual(k["hreflang_empty"][0].reason, "Empty hreflang attribute")
        self.assertEqual(k["hreflang_x_default"][0].status, "parsable")

    def test_self_reference_flag(self):
        k = by_key(parse('<head><link rel="alternate" hreflang="fr" href="https://a.com/fr"></head>', url="https://a.com/en"))
        self.assertTrue(any("self-referencing" in w for w in k["hreflang_fr"][0].warnings))


class UrlHandling(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_url("  Example.COM/Path#frag ")[0], "https://example.com/Path")
        self.assertEqual(normalize_url("http://Example.com")[0], "http://example.com/")
        self.assertEqual(normalize_url("//cdn.example.com/a")[0], "https://cdn.example.com/a")
        self.assertEqual(normalize_url("http://a.com:8080/x?q=1#f")[0], "http://a.com:8080/x?q=1")
        for bad in ("", "   ", "javascript:alert(1)", "ftp://a.com/x", "http://", "http://a.com:abc/", "a b.com", "mailto:x@y.z"):
            self.assertIsNone(normalize_url(bad)[0], bad)

    def test_dedup_preserves_order(self):
        s = finalize(["b.com", "a.com", "https://b.com/#x", "bad url", "a.com"], "urls-file", "t")
        self.assertEqual(s.urls, ["https://b.com/", "https://a.com/"])
        self.assertEqual((s.invalid_count, s.duplicate_count), (1, 2))

    def test_upload_blocks_private_hosts(self):
        s = finalize(["http://127.0.0.1/", "http://localhost/x", "http://10.0.0.5/", "https://ok.com/"], "upload", "t", upload=True)
        self.assertEqual(s.urls, ["https://ok.com/"])

    def test_file_formats(self):
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d, "a.txt"); txt.write_text("# c\nhttps://a.com/1\n\nhttps://a.com/2,3\n", encoding="utf-8")
            self.assertEqual(read_url_entries(txt), ["https://a.com/1", "https://a.com/2,3"])
            csv1 = Path(d, "b.csv"); csv1.write_text("id,URL,note\n1,https://a.com/x,n\n2,https://a.com/y,m\n", encoding="utf-8")
            self.assertEqual(read_url_entries(csv1), ["https://a.com/x", "https://a.com/y"])
            csv2 = Path(d, "c.csv"); csv2.write_text("https://a.com/p,foo\nhttps://a.com/q,bar\n", encoding="utf-8")
            self.assertEqual(read_url_entries(csv2), ["https://a.com/p", "https://a.com/q"])

    def test_sensitive_domains(self):
        pats = ["*.bank.com", "gov.example", "sec*.corp.io"]
        self.assertTrue(host_matches("www.bank.com", pats))
        self.assertTrue(host_matches("bank.com", pats))
        self.assertFalse(host_matches("notbank.com", pats))
        self.assertTrue(host_matches("gov.example", pats))
        self.assertFalse(host_matches("www.gov.example", pats))
        self.assertTrue(host_matches("secure.corp.io", pats))

    def test_sitemap_parsing(self):
        idx = b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc> https://a.com/s1.xml </loc></sitemap></sitemapindex>'
        urls = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.com/1</loc></url><url><loc>https://a.com/2</loc></url></urlset>'
        self.assertEqual(parse_sitemap_xml(idx), ("index", ["https://a.com/s1.xml"]))
        self.assertEqual(parse_sitemap_xml(urls), ("urlset", ["https://a.com/1", "https://a.com/2"]))
        import gzip
        self.assertEqual(parse_sitemap_xml(gzip.compress(urls))[0], "urlset")


class Challenge(unittest.TestCase):
    def test_detection(self):
        cf = b"<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'></div></body></html>"
        self.assertEqual(detect_challenge(403, {"server": "cloudflare"}, cf), "cloudflare")
        self.assertEqual(detect_challenge(200, {"cf-mitigated": "challenge"}, b"x"), "cloudflare")
        self.assertEqual(detect_challenge(403, {"server": "AkamaiGHost"}, b"denied"), "akamai")
        self.assertEqual(detect_challenge(403, {"x-datadome": "protected"}, b""), "datadome")
        self.assertEqual(detect_challenge(429, {}, b""), "rate_limit")
        self.assertEqual(detect_challenge(403, {}, b"nope"), "http_403")
        self.assertIsNone(detect_challenge(200, {"server": "cloudflare"}, b"<html><title>Blog about datadome</title></html>"))
        self.assertIsNone(detect_challenge(404, {}, b"not found"))


class Output(unittest.TestCase):
    OG_PAGE = ('<head><title>A</title><meta property="og:title" content="T">'
               '<meta property="og:site_name" content="S"><meta property="og:image" content="https://a.com/1.png">'
               '<meta property="og:image"><meta name="description" content="d"></head>'
               '<body><h1>x</h1><h1>y</h1></body>')

    def make_pages(self):
        p1 = PageResult(url="https://a.com/1", http_status=200, fetched_ok=True, crawl_timestamp="t",
                        params=parse(self.OG_PAGE, "https://a.com/1"))
        p2 = PageResult(url="https://a.com/2", http_status=200, fetched_ok=True, crawl_timestamp="t",
                        params=parse("<head><title>B</title></head><body><h1>z</h1></body>", "https://a.com/2"))
        return [p1, p2]

    def test_one_column_per_parameter_fixed_header(self):
        header = build_columns()
        self.assertEqual(header[:7], ["url", "http_status", "result", "crawl_timestamp", "waf_mode", "final_url", "fetch_error"])
        for g in ("title", "meta_description", "meta_robots", "canonical", "og", "twitter", "hreflang", "h1"):
            self.assertEqual(header[header.index(g):header.index(g) + 3], [g, f"{g}_status", f"{g}_reason"])
        self.assertFalse([c for c in header if c.startswith(("og_", "h1_")) and c != "og_status" and c != "og_reason"
                          and c not in ("h1_status", "h1_reason")])   # no per-property / per-instance columns
        self.assertLess(header.index("title"), header.index("og"))
        self.assertLess(header.index("og"), header.index("h1"))

    def test_all_tags_of_a_parameter_share_one_cell(self):
        row = page_row(self.make_pages()[0])
        self.assertEqual(row["og"].split("\n"), [
            '<meta property="og:title" content="T">',
            '<meta property="og:site_name" content="S">',
            '<meta property="og:image" content="https://a.com/1.png">',
            '<meta property="og:image">',
        ])
        self.assertEqual(row["og_status"], "unparsable")                 # one bad tag flags the parameter
        self.assertEqual(row["og_reason"], "og_image_2: Missing content attribute")
        self.assertEqual(row["h1"], "<h1>x</h1>\n<h1>y</h1>")
        self.assertEqual((row["h1_status"], row["h1_reason"]), ("parsable", ""))
        self.assertEqual((row["title"], row["title_status"]), ("<title>A</title>", "parsable"))
        self.assertEqual(row["twitter"], "")                             # absent parameter -> empty cells

    def test_missing_title_is_explicit(self):
        row = page_row(PageResult(url="u", http_status=200, fetched_ok=True,
                                  params=parse("<head></head><body></body>", "u")))
        self.assertEqual((row["title"], row["title_status"], row["title_reason"]),
                         ("", "unparsable", "Missing <title> tag in <head>"))

    def test_csv_roundtrip_all_urls_and_formula_guard(self):
        from seo_extractor.url_source import finalize
        urlset = finalize(["https://a.com/1", "https://a.com/2", "https://a.com/3", "bad url"], "urls-file", "t")
        pages = self.make_pages()
        pages[1].fetch_error = "=HYPERLINK(1)"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "o.csv")
            write_csv(pages, path, urlset)
            rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        self.assertEqual([r["result"] for r in rows], ["ISSUES", "OK", "NOT CRAWLED", "INVALID URL"])
        self.assertEqual(len(rows[0]["og"].splitlines()), 4)               # multi-line cell survives CSV
        self.assertEqual(rows[1]["fetch_error"], "'=HYPERLINK(1)")        # formula neutralised
        self.assertEqual(rows[3]["url"], "bad url")

    def test_pure_data_row_has_values_only(self):
        row = data_row(self.make_pages()[0])
        self.assertEqual(row["title"], "A")
        self.assertEqual(row["meta_description"], "d")
        self.assertEqual(row["og"].split("\n"), ["og:title: T", "og:site_name: S", "og:image: https://a.com/1.png"])
        self.assertEqual(row["h1"], "x\ny")
        self.assertEqual(row["twitter"], "")
        self.assertNotIn("<", "".join(row.values()))                        # no HTML anywhere
        self.assertFalse([k for k in row if k.endswith(("_status", "_reason")) and k != "http_status"])

    def test_pure_data_formats_for_named_tags(self):
        params = parse('<head><title>t</title><meta name="robots" content="index, follow">'
                       '<meta name="GoogleBot" content="noindex"><meta name="twitter:card" content="summary">'
                       '<link rel="canonical" href="https://a.com/c">'
                       '<link rel="alternate" hreflang="en-US" href="https://a.com/en">'
                       '<link rel="alternate" hreflang="x-default" href="https://a.com/"></head><body><h1>h</h1></body>', "https://a.com/en")
        row = data_row(PageResult(url="https://a.com/en", http_status=200, fetched_ok=True, params=params))
        self.assertEqual(row["meta_robots"], "robots: index, follow\ngooglebot: noindex")
        self.assertEqual(row["twitter"], "twitter:card: summary")
        self.assertEqual(row["canonical"], "https://a.com/c")
        self.assertEqual(row["hreflang"], "en-US: https://a.com/en\nx-default: https://a.com/")

    def test_data_csv_has_every_requested_url(self):
        from seo_extractor.url_source import finalize
        urlset = finalize(["https://a.com/1", "https://a.com/2", "https://a.com/3", "bad url"], "urls-file", "t")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "d.csv")
            cols = write_data_csv(self.make_pages(), path, urlset)
            rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        self.assertEqual(cols[:4], ["url", "http_status", "result", "final_url"])
        self.assertEqual([r["result"] for r in rows], ["ISSUES", "OK", "NOT CRAWLED", "INVALID URL"])
        self.assertEqual(rows[0]["title"], "A")

    def test_json_keeps_per_element_detail(self):
        j = page_to_json(self.make_pages()[0])
        self.assertEqual(j["parameters"]["title"][0]["status"], "parsable")
        self.assertEqual(sorted(j["parameters"]["og"]), ["og_image", "og_site_name", "og_title"])
        self.assertEqual(len(j["parameters"]["og"]["og_image"]), 2)
        self.assertEqual(j["parameters"]["h1"][0]["raw"], "<h1>x</h1>")


class RateLimiterTest(unittest.IsolatedAsyncioTestCase):
    async def test_never_exceeds_rps(self):
        import asyncio
        rl = RateLimiter(rps=5, concurrency=3)

        async def hit():
            async with rl.slot():
                await asyncio.sleep(0.01)

        await asyncio.gather(*(hit() for _ in range(20)))
        v = rl.verify()
        self.assertTrue(v["rps_limit_respected"], v)
        self.assertLessEqual(v["max_requests_in_any_1s_window"], 5)


if __name__ == "__main__":
    unittest.main()

