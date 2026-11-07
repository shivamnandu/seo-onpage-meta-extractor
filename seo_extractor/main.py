"""CLI entry point: load URLs -> fetch (rate-limited) -> parse + validate -> CSV/JSON/Excel/logs.

``execute()`` is the reusable engine entry (used by the CLI here and by the GUI in seo_tool.py):
it reports progress through callbacks and honours a ``threading.Event`` for "Stop".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):  # allow `python seo_extractor/main.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seo_extractor.fetcher import FetchDispatcher
from seo_extractor.journal import Journal
from seo_extractor.models import GROUP_ORDER, PARSABLE, UNPARSABLE, FetchResult, PageResult
from seo_extractor.output_writer import write_csv, write_data_csv, write_json
from seo_extractor.parser import HTMLParser
from seo_extractor.rate_limiter import RateLimiter
from seo_extractor.url_source import InputError, UrlSet, load_from_file, load_from_sitemap
from seo_extractor.utils import (
    DEFAULT_USER_AGENT,
    LOG_ERRORS,
    LOG_PARSE,
    normalize_url,
    parse_sensitive_domains,
    setup_logging,
    shutdown_logging,
    utc_now_iso,
)

log_errors = logging.getLogger(LOG_ERRORS)
log_parse = logging.getLogger(LOG_PARSE)

_HTML_TYPES = ("text/html", "application/xhtml+xml", "")

StatusCb = Callable[[str], None]
ProgressCb = Callable[[int, int, PageResult], None]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seo_extractor",
        description="Bulk-extract on-page SEO parameters (title, meta, canonical, OG, Twitter, "
                    "hreflang, h1) with deterministic parsability validation.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--urls-file", help="text file (one URL per line) or CSV with a 'url' column")
    src.add_argument("--sitemap", help="sitemap URL (.xml / .xml.gz, sitemap indexes supported)")
    src.add_argument("--upload", help="user-uploaded URL file (sanitised like --urls-file, stricter)")
    p.add_argument("--output-dir", default="./seo_audit_output")
    p.add_argument("--rps", type=int, default=2, help="max requests per second (default 2)")
    p.add_argument("--concurrency", type=int, default=5, help="max in-flight requests (default 5)")
    p.add_argument("--timeout", type=float, default=15.0, help="request timeout, seconds (default 15)")
    p.add_argument("--waf-mode", action="store_true", help="force WAF-proof mode for every URL")
    p.add_argument("--sensitive-domains", default=None,
                   help="comma-separated domains that use WAF-proof mode, wildcards allowed "
                        "(e.g. '*.bank.com,secure.example.gov')")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="override the User-Agent (default Chrome 120)")
    p.add_argument("--dry-run", action="store_true", help="validate input, print the summary, exit")
    p.add_argument("--max-redirects", type=int, default=5, help="redirect chain limit (default 5)")
    p.add_argument("--resume", action="store_true",
                   help="continue a previous run in --output-dir (skips URLs already in journal.jsonl; "
                        "network errors and WAF blocks are retried)")
    p.add_argument("--no-excel", action="store_true", help="skip the .xlsx report (CSV/JSON are always written)")
    return p


# ------------------------------------------------------------------------- crawling
def _log_parse_issues(page: PageResult) -> None:
    for p in page.params:
        label = f"{p.key}_{p.index}"
        if p.status == UNPARSABLE:
            log_parse.warning("%s | %s | UNPARSABLE | %s", page.url, label, p.reason)
        for w in p.warnings:
            log_parse.warning("%s | %s | WARNING | %s", page.url, label, w)


def build_page(fetch: FetchResult, parser: HTMLParser) -> PageResult:
    page = PageResult(
        url=fetch.url,
        final_url=fetch.final_url or fetch.url,
        http_status=fetch.status,
        crawl_timestamp=utc_now_iso(),
        waf_mode=fetch.waf_mode,
        response_time_ms=fetch.request_ms,
        bytes_received=fetch.bytes_received,
        redirect_chain=fetch.redirect_chain,
        waf_blocked=fetch.waf_block is not None,
    )
    if fetch.error:
        page.fetch_error = fetch.error
        return page

    page.fetched_ok = fetch.status is not None and 200 <= fetch.status < 300
    if not page.fetched_ok:
        page.fetch_error = f"HTTP {fetch.status}"
        log_errors.error("%s | HTTP %s", fetch.url, fetch.status)
    if fetch.content_type not in _HTML_TYPES:
        page.fetched_ok = False
        page.fetch_error = f"Non-HTML content type '{fetch.content_type}' (not parsed)"
        log_errors.error("%s | %s", fetch.url, page.fetch_error)
        return page
    if not fetch.content.strip():
        page.fetched_ok = False
        page.fetch_error = page.fetch_error or "Empty response body"
        return page

    try:
        page_urls = {u for u in (fetch.url, normalize_url(page.final_url)[0]) if u}
        page.params = parser.parse(fetch.content, page_urls, fetch.charset)
    except Exception as exc:  # noqa: BLE001 - one bad document must never stop the crawl
        page.fetched_ok = False
        page.fetch_error = f"Parse error: {type(exc).__name__}: {exc}"
        log_errors.error("%s | %s", fetch.url, page.fetch_error)
        return page
    _log_parse_issues(page)
    return page


async def crawl(
    urls: list[str],
    dispatcher: FetchDispatcher,
    workers: int,
    journal: Journal,
    on_progress: ProgressCb | None = None,
    stop: threading.Event | None = None,
) -> tuple[list[PageResult], bool]:
    """Fetch + parse every URL not already in the journal. Returns (pages in input order, stopped?).

    Each finished page is written to the journal immediately, so a crash loses at most the
    requests that were in flight.
    """
    parser = HTMLParser()
    results: dict[int, PageResult] = {}
    queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
    for idx, url in enumerate(urls):
        cached = journal.get(url)
        if cached is not None:
            results[idx] = cached
        else:
            queue.put_nowait((idx, url))
    total = len(urls)
    stopped = False

    async def worker() -> None:
        nonlocal stopped
        while True:
            if stop is not None and stop.is_set():
                stopped = True
                return
            try:
                idx, url = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            fetch = await dispatcher.fetch(url)
            page = build_page(fetch, parser)  # parsing stays synchronous
            results[idx] = page
            journal.add(page)
            if on_progress:
                on_progress(len(results), total, page)

    tasks = [asyncio.create_task(worker()) for _ in range(max(1, min(workers, queue.qsize() or 1)))]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stopped = True
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return [results[i] for i in sorted(results)], stopped


def check_hreflang_reciprocity(pages: list[PageResult]) -> None:
    """Flag hreflang entries whose target page (if crawled in this run) does not link back.

    A flag (warning) only: reciprocity can only be judged for pages present in this crawl.
    """
    sets: dict[str, set[str]] = {}
    for pg in pages:
        if not pg.fetched_ok:
            continue
        hrefs = {normalize_url(p.value)[0] for p in pg.params
                 if p.group == "hreflang" and p.status == PARSABLE}
        for u in (pg.url, normalize_url(pg.final_url)[0]):
            if u:
                sets[u] = hrefs
    for pg in pages:
        own = {pg.url, normalize_url(pg.final_url)[0]}
        for p in pg.params:
            if p.group != "hreflang" or p.status != PARSABLE:
                continue
            target = normalize_url(p.value)[0]
            if target in own or target not in sets:
                continue
            msg = f"No return link: {target} does not list this page in its hreflang set"
            if not sets[target] & own and msg not in p.warnings:
                p.warnings.append(msg)
                log_parse.warning("%s | %s_%d | WARNING | %s", pg.url, p.key, p.index, msg)


# ------------------------------------------------------------------------- summary
def build_summary(pages: list[PageResult], urlset: UrlSet, limiter: RateLimiter,
                  started: float, interrupted: bool) -> dict:
    by_group = {g: Counter() for g in GROUP_ORDER}
    missing = 0
    for pg in pages:
        for p in pg.params:
            if not p.present:
                missing += 1
                continue
            by_group[p.group]["total"] += 1
            by_group[p.group][p.status] += 1
    totals = sum((c for c in by_group.values()), Counter())
    timed = [pg.response_time_ms for pg in pages if pg.http_status is not None]
    return {
        "interrupted": interrupted,
        "source_type": urlset.source_type,
        "source": urlset.source,
        "input": {
            "entries_read": urlset.raw_count,
            "invalid_skipped": urlset.invalid_count,
            "duplicates_removed": urlset.duplicate_count,
            "valid_unique_urls": len(urlset.urls),
        },
        "total_urls": len(pages),
        "successful": sum(pg.fetched_ok for pg in pages),
        "failed": sum(1 for pg in pages if not pg.fetched_ok and not pg.waf_blocked),
        "waf_blocked": sum(pg.waf_blocked for pg in pages),
        "waf_mode_urls": sum(pg.waf_mode for pg in pages),
        "average_response_time_ms": round(sum(timed) / len(timed), 1) if timed else 0.0,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "parameters": {
            "total_extracted": totals["total"],
            "parsable": totals[PARSABLE],
            "unparsable": totals[UNPARSABLE],
            "missing_title_or_h1": missing,
            "by_group": {g: dict(c) for g, c in by_group.items()},
        },
        "rate_limit": limiter.verify(),  # covers this session's requests only (not resumed ones)
    }


# ------------------------------------------------------------------------------ run
@dataclass
class RunOutcome:
    urlset: UrlSet
    dry_run: bool = False
    pages: list[PageResult] = field(default_factory=list)
    summary: dict | None = None
    interrupted: bool = False
    csv_path: Path | None = None
    data_csv_path: Path | None = None
    json_path: Path | None = None
    xlsx_path: Path | None = None
    out_dir: Path | None = None


def input_summary(urlset: UrlSet, args, sensitive: list[str], dispatcher: FetchDispatcher) -> str:
    waf_urls = sum(dispatcher.uses_waf(u) for u in urlset.urls)
    rows = [
        ("Source type", urlset.source_type),
        ("Source", urlset.source),
        ("Entries read", urlset.raw_count),
        ("Invalid (skipped)", f"{urlset.invalid_count}  (see errors.log)"),
        ("Duplicates", urlset.duplicate_count),
        ("URLs to crawl", len(urlset.urls)),
        ("WAF-proof URLs", f"{waf_urls}  (waf-mode={args.waf_mode}, sensitive patterns={len(sensitive)})"),
        ("Rate / concurrency", f"{args.rps} req/s, {args.concurrency} concurrent"),
    ]
    line = "=" * 60
    return "\n".join([line] + [f" {k:<19}: {v}" for k, v in rows] + [line])


async def execute(
    args: argparse.Namespace,
    out: Path,
    on_status: StatusCb = print,
    on_progress: ProgressCb | None = None,
    stop: threading.Event | None = None,
) -> RunOutcome:
    """Run the whole pipeline. Raises InputError for unusable input."""
    started = time.monotonic()
    sensitive = parse_sensitive_domains(args.sensitive_domains)
    limiter = RateLimiter(args.rps, args.concurrency)

    async with FetchDispatcher(limiter, args.timeout, args.max_redirects, args.user_agent,
                               args.waf_mode, sensitive) as dispatcher:
        if args.sitemap:
            on_status(f"Fetching sitemap {args.sitemap} ...")
            urlset = await load_from_sitemap(args.sitemap, dispatcher)
        else:
            urlset = load_from_file(args.urls_file or args.upload, upload=bool(args.upload))

        on_status(input_summary(urlset, args, sensitive, dispatcher))
        if args.dry_run:
            return RunOutcome(urlset, dry_run=True)
        if not urlset.urls:
            raise InputError("No valid URLs to crawl.")

        journal = Journal(out / "journal.jsonl", resume=args.resume)
        try:
            if args.resume and journal.records:
                on_status(f"Resuming: {len(journal.records)} URL(s) already done, skipping them.")
            pages, stopped = await crawl(urlset.urls, dispatcher, args.concurrency * 2,
                                         journal, on_progress, stop)
        finally:
            journal.close()

    check_hreflang_reciprocity(pages)
    outcome = RunOutcome(urlset, pages=pages, interrupted=stopped, out_dir=out)
    outcome.csv_path, outcome.json_path = out / "seo_audit.csv", out / "seo_audit.json"
    write_csv(pages, outcome.csv_path, urlset)
    outcome.data_csv_path = out / "seo_extracted_data.csv"
    write_data_csv(pages, outcome.data_csv_path, urlset)
    write_json(pages, outcome.json_path)
    outcome.summary = build_summary(pages, urlset, limiter, started, stopped)
    (out / "crawl_summary.json").write_text(json.dumps(outcome.summary, indent=2), encoding="utf-8")

    if not args.no_excel:
        try:
            from seo_extractor.excel_report import write_excel

            stamp = time.strftime("%Y%m%d_%H%M%S")
            outcome.xlsx_path = write_excel(pages, outcome.summary, out / f"seo_report_{stamp}.xlsx",
                                        urlset)
        except ImportError:
            on_status("openpyxl is not installed - skipped the Excel report "
                      "(CSV/JSON were written). Install it with: pip install openpyxl")
    return outcome


def describe_outcome(o: RunOutcome) -> str:
    s = o.summary
    rl = s["rate_limit"]
    lines = [
        f"Done{' (stopped early, partial results)' if o.interrupted else ''}: "
        f"{s['successful']} ok, {s['failed']} failed, {s['waf_blocked']} WAF-blocked "
        f"in {s['elapsed_seconds']}s",
        f"Elements: {s['parameters']['parsable']} parsable, {s['parameters']['unparsable']} unparsable",
        f"Rate check: max {rl['max_requests_in_any_1s_window']} req in any 1 s window "
        f"(limit {rl['rps_limit']}) -> {'OK' if rl['rps_limit_respected'] else 'VIOLATED'}",
        f"Data : {o.data_csv_path}",
        f"Audit: {o.csv_path}",
        f"JSON : {o.json_path}",
    ]
    if o.xlsx_path:
        lines.append(f"Excel: {o.xlsx_path}")
    lines.append(f"Logs : {o.out_dir}")
    return "\n".join(lines)


def run_async(coro):
    """curl_cffi's asyncio integration needs a selector loop; Windows defaults to Proactor."""
    if sys.platform == "win32":
        if sys.version_info >= (3, 12):
            return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


def _cli_progress(done: int, total: int, page: PageResult) -> None:
    step = 1 if total <= 50 else 25
    if done % step == 0 or done == total:
        print(f"  [{done}/{total}] {page.http_status or 'ERR'} {page.url}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("rps", "concurrency"):
        if getattr(args, name) < 1:
            print(f"--{name} must be >= 1", file=sys.stderr)
            return 2
    out = Path(args.output_dir)
    setup_logging(out, append=args.resume)
    try:
        outcome = run_async(execute(args, out, print, _cli_progress))
    except InputError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2 if "No valid" not in str(exc) else 1
    except RuntimeError as exc:  # e.g. curl_cffi missing while WAF mode requested
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Re-run with --resume to continue.", file=sys.stderr)
        return 130
    finally:
        shutdown_logging()
    if outcome.dry_run:
        print("Dry run: nothing crawled.")
        return 0
    print("\n" + describe_outcome(outcome))
    return 130 if outcome.interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
