"""Excel report (openpyxl): Summary / Extracted Data / Audit / Raw HTML / Issues / URLs sheets.

* Extracted Data: the pure extracted values (title, description, ...) and nothing else, one row
  per requested URL, one cell per parameter.
* Audit: the verdict per parameter (parsable / unparsable + reason) and fetch details.
* Raw HTML: the tags as captured, one cell per parameter.
* URLs: fetch outcome per URL (OK / ISSUES / FAILED / WAF-BLOCKED).
* Issues: one row per unparsable element or warning, for triage.
Every requested URL has a row in the first three table sheets, parsable or not (failed, blocked,
never-reached and invalid URLs included).
Every string is written as text (never as a formula) and stripped of characters Excel rejects.
"""
from __future__ import annotations

from pathlib import Path

from seo_extractor.models import GROUP_ORDER, PARSABLE, UNPARSABLE, PageResult
from seo_extractor.output_writer import (
    AUDIT_COLUMNS,
    DATA_COLUMNS,
    RAW_COLUMNS,
    all_data_rows,
    all_rows,
    page_result_label,
)

_CELL_LIMIT = 32_000  # Excel's hard limit is 32,767 characters per cell


def write_excel(pages: list[PageResult], summary: dict, path: Path, urlset=None) -> Path:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    green = PatternFill("solid", fgColor="C6EFCE")
    red = PatternFill("solid", fgColor="FFC7CE")
    amber = PatternFill("solid", fgColor="FFEB9C")
    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(bold=True, color="FFFFFF")
    top_wrap = Alignment(vertical="top", wrap_text=True)
    result_fill = {"OK": green, "ISSUES": amber, "FAILED": red, "WAF-BLOCKED": red,
                   "NOT CRAWLED": amber, "INVALID URL": red}

    def put(ws, row: int, col: int, value):
        cell = ws.cell(row=row, column=col)
        if isinstance(value, str):
            value = ILLEGAL_CHARACTERS_RE.sub("", value)[:_CELL_LIMIT]
            cell.value = value
            cell.data_type = "s"  # "=cmd()" stays text instead of becoming a formula
        else:
            cell.value = value
        return cell

    def header_row(ws, names: list[str], widths: dict[str, int] | None = None) -> None:
        for c, name in enumerate(names, 1):
            cell = put(ws, 1, c, name)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center")
            ws.column_dimensions[get_column_letter(c)].width = (widths or {}).get(name, 18)
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(names))}1"

    wb = Workbook()

    # ---------------------------------------------------------------- Summary
    ws = wb.active
    ws.title = "Summary"
    put(ws, 1, 1, "Metric").font = Font(bold=True)
    put(ws, 1, 2, "Value").font = Font(bold=True)
    r = 2
    for label, value in (
        ("Source type", summary["source_type"]),
        ("Source", summary["source"]),
        ("URLs crawled", summary["total_urls"]),
        ("Successful (2xx)", summary["successful"]),
        ("Failed", summary["failed"]),
        ("WAF-blocked", summary["waf_blocked"]),
        ("Average response time (ms)", summary["average_response_time_ms"]),
        ("Elapsed (s)", summary["elapsed_seconds"]),
        ("Invalid URLs skipped", summary["input"]["invalid_skipped"]),
        ("Duplicates removed", summary["input"]["duplicates_removed"]),
        ("Rate limit (req/s)", summary["rate_limit"]["rps_limit"]),
        ("Busiest 1 s window", summary["rate_limit"]["max_requests_in_any_1s_window"]),
        ("Rate limit respected", summary["rate_limit"]["rps_limit_respected"]),
        ("Run interrupted", summary["interrupted"]),
    ):
        put(ws, r, 1, label)
        put(ws, r, 2, value if not isinstance(value, bool) else str(value))
        r += 1
    r += 1
    for c, name in enumerate(("Parameter", "Extracted", "Parsable", "Unparsable"), 1):
        cell = put(ws, r, c, name)
        cell.font, cell.fill = head_font, head_fill
    by_group = summary["parameters"]["by_group"]
    for group in GROUP_ORDER:
        r += 1
        counts = by_group.get(group, {})
        put(ws, r, 1, group)
        put(ws, r, 2, counts.get("total", 0))
        put(ws, r, 3, counts.get(PARSABLE, 0)).fill = green
        put(ws, r, 4, counts.get(UNPARSABLE, 0)).fill = red if counts.get(UNPARSABLE, 0) else green
    r += 1
    put(ws, r, 1, "Missing <title> / <h1> rows")
    put(ws, r, 2, summary["parameters"]["missing_title_or_h1"])
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 60
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 12

    # ------------------------------------------- table sheets (one row per URL)
    widths = {"url": 55, "http_status": 11, "result": 14, "crawl_timestamp": 21, "waf_mode": 9,
              "final_url": 40, "fetch_error": 34, "warnings": 45}
    for group in GROUP_ORDER:
        widths[group] = 60
        widths[f"{group}_status"] = 12
        widths[f"{group}_reason"] = 40

    def table_sheet(title: str, columns: list[str], rows: list[dict]) -> None:
        """Every requested URL gets a row (crawled, failed, blocked, never reached or invalid)."""
        ws = wb.create_sheet(title)
        header_row(ws, columns, widths)
        for i, row in enumerate(rows, 2):
            for c, name in enumerate(columns, 1):
                value = row.get(name, "")
                if value == "":
                    continue
                if name == "http_status" and value.isdigit():
                    value = int(value)
                cell = put(ws, i, c, value)
                if name == "result":
                    cell.fill = result_fill.get(value, amber)
                elif name.endswith("_status") and name != "http_status":
                    cell.fill = green if value == PARSABLE else red
                elif name not in ("url", "http_status", "final_url", "waf_mode", "crawl_timestamp"):
                    cell.alignment = top_wrap

    # Extracted Data: the pure values only (title, description, ...), nothing about validity.
    table_sheet("Extracted Data", DATA_COLUMNS, all_data_rows(pages, urlset))
    # Audit: the verdict for every parameter (parsable / unparsable + reason) and fetch details.
    audit_rows = all_rows(pages, urlset)
    table_sheet("Audit", AUDIT_COLUMNS, audit_rows)
    # Raw HTML: the tags exactly as captured, one cell per parameter.
    table_sheet("Raw HTML", RAW_COLUMNS, audit_rows)
    # ------------------------------------------------------------------- URLs
    ws = wb.create_sheet("URLs")
    names = ["url", "http_status", "result", "final_url", "waf_mode", "response_ms", "bytes",
             "elements", "unparsable", "warnings", "fetch_error"]
    header_row(ws, names, {"url": 60, "final_url": 50, "fetch_error": 45, "result": 14})
    for i, pg in enumerate(pages, 2):
        n_bad = sum(1 for p in pg.params if p.status == UNPARSABLE)
        n_warn = sum(len(p.warnings) for p in pg.params) + len(pg.page_warnings)
        result = page_result_label(pg)
        values = [pg.url, pg.http_status if pg.http_status is not None else "", result,
                  pg.final_url if pg.final_url != pg.url else "", str(pg.waf_mode).lower(),
                  round(pg.response_time_ms), pg.bytes_received,
                  sum(1 for p in pg.params if p.present), n_bad, n_warn, pg.fetch_error]
        for c, v in enumerate(values, 1):
            cell = put(ws, i, c, v)
            if c == 3:
                cell.fill = result_fill[result]

    # ----------------------------------------------------------------- Issues
    ws = wb.create_sheet("Issues")
    names = ["url", "parameter", "group", "type", "detail", "value", "raw"]
    header_row(ws, names, {"url": 55, "parameter": 26, "group": 16, "type": 13, "detail": 70,
                           "value": 45, "raw": 60})
    i = 2
    for pg in pages:
        for p in pg.params:
            label = f"{p.key}_{p.index}"
            events = []
            if p.status == UNPARSABLE:
                events.append(("UNPARSABLE", p.reason))
            events.extend(("warning", w) for w in p.warnings)
            for kind, detail in events:
                for c, v in enumerate([pg.url, label, p.group, kind, detail, p.value, p.raw], 1):
                    cell = put(ws, i, c, v)
                    if c == 4:
                        cell.fill = red if kind == "UNPARSABLE" else amber
                i += 1
        for w in pg.page_warnings:
            for c, v in enumerate([pg.url, "", "page", "warning", w, "", ""], 1):
                put(ws, i, c, v)
            i += 1

    wb.save(path)
    return path

