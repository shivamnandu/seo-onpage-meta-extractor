"""Engine-level tests: stop/resume journal, Excel report, pasted-text parsing, GUI construction.

Run:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seo_extractor.journal import Journal, needs_retry  # noqa: E402
from seo_extractor.main import build_parser, execute  # noqa: E402
from seo_extractor.models import PageResult  # noqa: E402
from seo_extractor.parser import HTMLParser  # noqa: E402
from seo_extractor.url_source import load_from_text, parse_url_text  # noqa: E402
from seo_extractor.utils import setup_logging, shutdown_logging  # noqa: E402

HITS: Counter = Counter()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS[self.path] += 1
        body = (f"<html><head><title>T {self.path}</title></head><body><h1>=H {self.path}</h1></body></html>"
                .encode())
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TextParsing(unittest.TestCase):
    def test_comments_and_fragments(self):
        text = "# header\nexample.com  # note\nhttps://a.com/p#frag\n\n   https://b.com/x   \n"
        self.assertEqual(parse_url_text(text), ["example.com", "https://a.com/p#frag", "https://b.com/x"])
        s = load_from_text(text)
        self.assertEqual(s.urls, ["https://example.com/", "https://a.com/p", "https://b.com/x"])


class JournalTest(unittest.TestCase):
    def test_roundtrip_and_retry_rules(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "journal.jsonl")
            params = HTMLParser().parse(b"<head><title>A</title></head><body><h1>x</h1></body>", {"https://a.com/"})
            ok = PageResult(url="https://a.com/", http_status=200, fetched_ok=True, params=params)
            net = PageResult(url="https://b.com/", http_status=None, fetch_error="Timeout")
            blocked = PageResult(url="https://c.com/", http_status=403, waf_blocked=True)
            missing = PageResult(url="https://d.com/", http_status=404, fetch_error="HTTP 404")
            j = Journal(path, resume=False)
            for p in (ok, net, blocked, missing):
                j.add(p)
            j.close()
            with path.open("a", encoding="utf-8") as fh:
                fh.write('{"url": "https://half-written')          # crash mid-write
            j2 = Journal(path, resume=True)
            self.assertEqual(set(j2.records), {"https://a.com/", "https://d.com/"})   # net + blocked retried
            self.assertEqual(j2.get("https://a.com/").params[0].raw, params[0].raw)
            self.assertTrue(needs_retry(net) and needs_retry(blocked) and not needs_retry(missing))
            j2.close()
            j3 = Journal(path, resume=False)   # fresh run truncates
            self.assertEqual(j3.records, {})
            j3.close()


class EngineTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def args(self, out, urls_file, *extra):
        return build_parser().parse_args(
            ["--urls-file", str(urls_file), "--output-dir", str(out), "--rps", "50", "--concurrency", "1", *extra])

    async def test_stop_then_resume_fetches_each_url_once_and_writes_excel(self):
        HITS.clear()
        base = f"http://127.0.0.1:{self.port}"
        with tempfile.TemporaryDirectory() as d:
            out = Path(d, "out")
            out.mkdir()
            urls = Path(d, "u.txt")
            urls.write_text("\n".join(f"{base}/p{i}" for i in range(8)) + "\nnot a url\n", encoding="utf-8")

            stop = threading.Event()
            setup_logging(out)
            first = await execute(self.args(out, urls), out, on_status=lambda s: None,
                                  on_progress=lambda d_, t, p: stop.set() if d_ >= 2 else None, stop=stop)
            shutdown_logging()
            self.assertTrue(first.interrupted)
            self.assertTrue(2 <= len(first.pages) < 8, len(first.pages))
            self.assertTrue(first.csv_path.exists() and first.xlsx_path.exists())   # partial results are saved

            from openpyxl import load_workbook

            def sheet_rows(path, name="Extracted Data"):
                ws = load_workbook(path)[name]
                header = [c.value for c in ws[1]]
                return header, [dict(zip(header, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]

            _, rows = sheet_rows(first.xlsx_path)
            self.assertEqual({r["url"] for r in rows if r["result"] != "INVALID URL"}, {f"{base}/p{i}" for i in range(8)})
            self.assertTrue(any(r["result"] == "NOT CRAWLED" for r in rows))   # unreached URLs are listed, not lost
            self.assertTrue(any(r["result"] == "INVALID URL" and r["url"] == "not a url" for r in rows))

            setup_logging(out, append=True)
            second = await execute(self.args(out, urls, "--resume"), out, on_status=lambda s: None)
            shutdown_logging()
            self.assertFalse(second.interrupted)
            self.assertEqual(len(second.pages), 8)
            self.assertEqual(max(HITS.values()), 1, HITS)            # nothing was fetched twice
            self.assertEqual(sum(HITS.values()), 8)
            self.assertEqual(second.summary["successful"], 8)

            wb = load_workbook(second.xlsx_path)
            self.assertEqual(wb.sheetnames, ["Summary", "Extracted Data", "Audit", "Raw HTML", "URLs", "Issues"])
            header, data = sheet_rows(second.xlsx_path)
            crawled = [r for r in data if r["result"] != "INVALID URL"]
            self.assertEqual(len(crawled), 8)                                  # one row per URL
            self.assertEqual([r["url"] for r in crawled], [f"{base}/p{i}" for i in range(8)])   # input order
            self.assertTrue(all(r["title"] and r["h1"] for r in crawled))      # one cell per parameter
            self.assertEqual((crawled[0]["title"], crawled[0]["h1"]), ("T /p0", "=H /p0"))   # pure values, no HTML
            self.assertFalse([c for c in header if c.endswith(("_status", "_reason")) and c != "http_status"])  # no verdicts here
            self.assertNotIn("h1_1", header)                                   # no per-instance columns
            _, audit = sheet_rows(second.xlsx_path, "Audit")
            self.assertEqual(len([r for r in audit if r["result"] != "INVALID URL"]), 8)
            self.assertTrue(all(r["title_status"] == r["h1_status"] == "parsable" for r in audit if r["result"] == "OK"))
            _, raw = sheet_rows(second.xlsx_path, "Raw HTML")
            self.assertEqual(raw[0]["title"], "<title>T /p0</title>")
            self.assertEqual(len(raw), 9)                                       # 8 URLs + the invalid entry
            self.assertEqual(wb["URLs"].max_row, 9)

    async def test_excel_never_turns_text_into_formulas(self):
        from openpyxl import load_workbook
        from seo_extractor.excel_report import write_excel
        summary = {"source_type": "t", "source": "t", "total_urls": 1, "successful": 0, "failed": 1,
                   "waf_blocked": 0, "average_response_time_ms": 0, "elapsed_seconds": 0,
                   "input": {"invalid_skipped": 0, "duplicates_removed": 0},
                   "rate_limit": {"rps_limit": 1, "max_requests_in_any_1s_window": 0, "rps_limit_respected": True},
                   "interrupted": False, "parameters": {"by_group": {}, "missing_title_or_h1": 0}}
        with tempfile.TemporaryDirectory() as d:
            p = write_excel([PageResult(url="https://a.com/", fetch_error="=1+1")], summary, Path(d, "x.xlsx"))
            ws = load_workbook(p)["Audit"]
            header = [c.value for c in ws[1]]
            cell = ws.cell(row=2, column=header.index("fetch_error") + 1)
            self.assertEqual((cell.value, cell.data_type), ("=1+1", "s"))

    async def test_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            urls = Path(d, "u.txt")
            urls.write_text("a.com\na.com\nnot a url\n", encoding="utf-8")
            setup_logging(out)
            o = await execute(self.args(out, urls, "--dry-run"), out, on_status=lambda s: None)
            shutdown_logging()
            self.assertTrue(o.dry_run)
            self.assertEqual((len(o.urlset.urls), o.urlset.duplicate_count, o.urlset.invalid_count), (1, 1, 1))


class GuiSmoke(unittest.TestCase):
    def test_gui_builds_argv(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter unavailable")
        import seo_tool

        captured = {}
        try:
            import tkinter
            root_probe = tkinter.Tk()
            root_probe.destroy()
        except tkinter.TclError:
            self.skipTest("no display available")

        # Build the real App but stop before mainloop by patching it out.
        orig = tk.Tk.mainloop
        tk.Tk.mainloop = lambda self, n=0: captured.setdefault("app", self) and None
        try:
            seo_tool.run_gui()
        finally:
            tk.Tk.mainloop = orig
        app = captured["app"]
        try:
            with tempfile.TemporaryDirectory() as d:
                app.text_box.delete("1.0", "end")
                app.text_box.insert("1.0", "example.com\nhttps://a.com/x\n")
                app.rps.set(3)
                app.waf_mode.set(True)
                app.sensitive.set("*.bank.com")
                app.excel.set(False)
                argv = app._build_argv(Path(d))
                args = build_parser().parse_args(argv)
                self.assertEqual((args.rps, args.waf_mode, args.sensitive_domains, args.no_excel),
                                 (3, True, "*.bank.com", True))
                self.assertEqual(Path(args.urls_file).read_text(encoding="utf-8").split(), ["example.com", "https://a.com/x"])
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()

