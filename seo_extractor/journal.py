"""Append-only per-URL journal so an interrupted / blocked run can be resumed (--resume).

One JSON line per finished URL, written as soon as the URL is done. On resume, finished URLs are
loaded from the journal instead of being fetched again; URLs that ended in a network error or a
WAF block are retried because those outcomes are usually transient.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from seo_extractor.models import ExtractedParam, PageResult


def page_from_dict(d: dict) -> PageResult:
    d = dict(d)
    params = [ExtractedParam(**p) for p in d.pop("params", [])]
    return PageResult(params=params, **d)


def needs_retry(page: PageResult) -> bool:
    """Outcomes worth re-attempting on resume: no HTTP response at all, or a WAF block."""
    return page.http_status is None or page.waf_blocked


class Journal:
    def __init__(self, path: Path, resume: bool) -> None:
        self.path = path
        self.records: dict[str, PageResult] = {}
        if resume and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    page = page_from_dict(json.loads(line))
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue  # a half-written last line after a crash is simply skipped
                self.records[page.url] = page
            # rewrite without the entries that will be retried, so the file stays consistent
            self.records = {u: p for u, p in self.records.items() if not needs_retry(p)}
            path.write_text("".join(self._line(p) for p in self.records.values()), encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")
        self._fh = path.open("a", encoding="utf-8")

    @staticmethod
    def _line(page: PageResult) -> str:
        return json.dumps(dataclasses.asdict(page), ensure_ascii=False) + "\n"

    def get(self, url: str) -> PageResult | None:
        return self.records.get(url)

    def add(self, page: PageResult) -> None:
        self.records[page.url] = page
        self._fh.write(self._line(page))
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
