"""CSV + JSON writers.

Flat layouts: ONE ROW PER URL, ONE CELL PER PARAMETER (every occurrence of it in that cell,
one per line, e.g. all Open Graph tags of a page together in the ``og`` cell).

  * seo_extracted_data.csv / Excel "Extracted Data": the PURE extracted values only.
  * seo_audit.csv: raw HTML + verdict per parameter:
        <parameter>          raw tags, one per line
        <parameter>_status   parsable | unparsable   (unparsable if ANY tag of it is unparsable)
        <parameter>_reason   why, one line per failing tag; blank when parsable
    The Excel workbook splits this into "Audit" (status/reason) and "Raw HTML" sheets.

The per-tag breakdown is kept in seo_audit.json and in the Excel "Issues" sheet.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from seo_extractor.models import (
    GROUP_ORDER,
    PARSABLE,
    ROBOTS_KEY_ORDER,
    UNPARSABLE,
    ExtractedParam,
    PageResult,
)

BASE_COLUMNS = ["url", "http_status", "result", "crawl_timestamp", "waf_mode", "final_url", "fetch_error"]
_PARAM_SUFFIXES = ("", "_status", "_reason")
COLUMNS = (
    BASE_COLUMNS
    + [f"{g}{s}" for g in GROUP_ORDER for s in _PARAM_SUFFIXES]
    + ["warnings"]
)
# Pure extracted data (no HTML, no status): what the page says.
DATA_COLUMNS = ["url", "http_status", "result", "final_url"] + list(GROUP_ORDER)
# Excel "Audit" sheet: verdicts only. The raw tags live in the "Raw HTML" sheet.
AUDIT_COLUMNS = (
    BASE_COLUMNS
    + [f"{g}{s}" for g in GROUP_ORDER for s in ("_status", "_reason")]
    + ["warnings"]
)
RAW_COLUMNS = ["url", "http_status", "result"] + list(GROUP_ORDER)

# Spreadsheet apps execute cells that start with these characters as formulas. Crawled text is
# untrusted, so such CSV cells get a leading apostrophe (the JSON output is untouched).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def build_columns(pages: list[PageResult] | None = None) -> list[str]:
    """The header is fixed: it does not depend on how many tags any page has."""
    return list(COLUMNS)


def page_result_label(page: PageResult) -> str:
    if page.waf_blocked:
        return "WAF-BLOCKED"
    if not page.fetched_ok:
        return "FAILED"
    if any(p.status == UNPARSABLE for p in page.params):
        return "ISSUES"
    return "OK"


def group_cells(items: list[ExtractedParam]) -> tuple[str, str, str]:
    """(raw text, status, reason) for all elements of one parameter on one page.

    raw    - every element's raw HTML, one per line (missing-title/h1 placeholders add nothing)
    status - "unparsable" if any element is unparsable, else "parsable"; "" if none exist
    reason - the failing elements' reasons, prefixed with the element name when there are
             several elements (e.g. "og_image_2: Missing content attribute")
    """
    if not items:
        return "", "", ""
    raw = "\n".join(p.raw for p in items if p.present and p.raw)
    failing = [p for p in items if p.status == UNPARSABLE]
    if not failing:
        return raw, PARSABLE, ""
    if len(items) == 1:
        return raw, UNPARSABLE, failing[0].reason
    return raw, UNPARSABLE, "\n".join(f"{p.key}_{p.index}: {p.reason}" for p in failing)


def page_row(page: PageResult) -> dict[str, str]:
    """One flat row (column name -> text) for a crawled page, without spreadsheet escaping."""
    row: dict[str, str] = {
        "url": page.url,
        "http_status": "" if page.http_status is None else str(page.http_status),
        "result": page_result_label(page),
        "crawl_timestamp": page.crawl_timestamp,
        "waf_mode": str(page.waf_mode).lower(),
        "final_url": page.final_url if page.final_url != page.url else "",
        "fetch_error": page.fetch_error,
        "warnings": "\n".join(page.all_warnings()),
    }
    by_group: dict[str, list[ExtractedParam]] = {g: [] for g in GROUP_ORDER}
    for p in page.params:
        by_group[p.group].append(p)
    for group, items in by_group.items():
        row[group], row[f"{group}_status"], row[f"{group}_reason"] = group_cells(items)
    return row


def _tag_name(p: ExtractedParam) -> str:
    """The tag's own name as written: og:title, twitter:card, robots, googlebot, ..."""
    a = p.attrs
    if p.group == "og":
        return a.get("property", "").strip()
    if p.group == "twitter":
        return (a.get("name") or a.get("property") or "").strip()
    if p.group == "meta_robots":
        return a.get("name", "").strip().lower()
    if p.group == "hreflang":
        return a.get("hreflang", "").strip()
    return ""


def value_cell(items: list[ExtractedParam]) -> str:
    """PURE extracted data for one parameter on one page: no HTML, no status.

    title / meta_description / canonical / h1 -> the value, one per line.
    og / twitter / hreflang / meta_robots     -> ``<tag name>: <value>``, one per line
    (``og:title: Welcome``, ``en-US: https://a.com/en``, ``googlebot: noindex``).
    """
    lines = []
    for p in items:
        if not p.present or not p.value:
            continue
        name = _tag_name(p)
        lines.append(f"{name}: {p.value}" if name else p.value)
    return "\n".join(lines)


def data_row(page: PageResult) -> dict[str, str]:
    """Row of the pure-data table: url, http_status, result, final_url + one value cell each."""
    by_group: dict[str, list[ExtractedParam]] = {g: [] for g in GROUP_ORDER}
    for p in page.params:
        by_group[p.group].append(p)
    row = {
        "url": page.url,
        "http_status": "" if page.http_status is None else str(page.http_status),
        "result": page_result_label(page),
        "final_url": page.final_url if page.final_url != page.url else "",
    }
    row.update({g: value_cell(items) for g, items in by_group.items()})
    return row


def _rows(pages: list[PageResult], urlset, row_fn) -> list[dict[str, str]]:
    """A row for EVERY requested URL, in input order.

    Crawled URLs carry their data; URLs the run never reached (Stop pressed) are marked
    NOT CRAWLED; entries rejected during input validation are appended as INVALID URL.
    Nothing that was asked for silently disappears.
    """
    by_url = {pg.url: pg for pg in pages}
    order = list(urlset.urls) if urlset else []
    order += [u for u in by_url if u not in set(order)]
    rows: list[dict[str, str]] = []
    for url in order:
        pg = by_url.get(url)
        if pg is None:
            rows.append({"url": url, "result": "NOT CRAWLED",
                         "fetch_error": "run stopped before this URL was reached"})
        else:
            rows.append(row_fn(pg))
    for raw, why in (urlset.invalid if urlset else []):
        rows.append({"url": raw, "result": "INVALID URL", "fetch_error": f"skipped: {why}"})
    return rows


def all_rows(pages: list[PageResult], urlset=None) -> list[dict[str, str]]:
    """Audit rows: raw tags + status + reason per parameter."""
    return _rows(pages, urlset, page_row)


def all_data_rows(pages: list[PageResult], urlset=None) -> list[dict[str, str]]:
    """Pure-data rows: the extracted values only."""
    return _rows(pages, urlset, data_row)


def _write_rows(rows: list[dict[str, str]], columns: list[str], path: Path) -> None:
    # utf-8-sig: Excel needs the BOM to open non-ASCII titles correctly; pandas/others ignore it.
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, restval="", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _safe_cell(v) for k, v in row.items()})


def write_csv(pages: list[PageResult], path: Path, urlset=None) -> list[str]:
    """seo_audit.csv: raw tags + status + reason per parameter."""
    _write_rows(all_rows(pages, urlset), COLUMNS, path)
    return list(COLUMNS)


def write_data_csv(pages: list[PageResult], path: Path, urlset=None) -> list[str]:
    """seo_extracted_data.csv: the extracted values only (title, description, ...)."""
    _write_rows(all_data_rows(pages, urlset), DATA_COLUMNS, path)
    return list(DATA_COLUMNS)


# ------------------------------------------------------------------------------ JSON
def _param_list(items: list[ExtractedParam]) -> list[dict]:
    return [p.to_json() for p in items]


def page_to_json(page: PageResult) -> dict:
    """Per-element schema from the brief: single-key groups are lists; og / twitter / hreflang
    map stems -> lists."""
    grouped = page.grouped()
    parameters: dict = {}
    for group in GROUP_ORDER:
        keys = sorted({p.key for p in page.params if p.group == group})
        if group in ("title", "meta_description", "canonical", "h1"):
            parameters[group] = _param_list(grouped.get(group, []))
        elif group == "meta_robots":
            for key in ROBOTS_KEY_ORDER:
                if key in grouped:
                    parameters[key] = _param_list(grouped[key])
        elif group == "hreflang":
            # spec example: {"en_us": [...], "x_default": [...]} (prefix dropped)
            parameters[group] = {k[len("hreflang_"):]: _param_list(grouped[k]) for k in keys}
        else:  # og, twitter: keys keep their prefix (og_title, twitter_card)
            parameters[group] = {k: _param_list(grouped[k]) for k in keys}
    return {
        "url": page.url,
        "http_status": page.http_status,
        "crawl_timestamp": page.crawl_timestamp,
        "waf_mode": page.waf_mode,
        "final_url": page.final_url,
        "fetch_error": page.fetch_error or None,
        "response_time_ms": round(page.response_time_ms, 1),
        "bytes_received": page.bytes_received,
        "redirect_chain": page.redirect_chain,
        "warnings": page.all_warnings(),
        "parameters": parameters,
    }


def write_json(pages: list[PageResult], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump([page_to_json(p) for p in pages], fh, ensure_ascii=False, indent=2)
