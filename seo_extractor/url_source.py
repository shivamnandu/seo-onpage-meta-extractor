"""URL sources: plain-text / CSV file, user upload, XML sitemap (index-aware, .gz-aware)."""
from __future__ import annotations

import csv
import io
import ipaddress
import logging
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from seo_extractor.utils import LOG_ERRORS, hostname_of, normalize_url

log_errors = logging.getLogger(LOG_ERRORS)

# CSV header names accepted for the URL column (case-insensitive).
_URL_HEADERS = {"url", "urls", "loc", "address", "link", "page url", "page_url"}

# --upload hardening limits.
UPLOAD_MAX_BYTES = 20 * 1024 * 1024
UPLOAD_MAX_URLS = 100_000
UPLOAD_MAX_URL_LENGTH = 2048

SITEMAP_MAX_DEPTH = 5
SITEMAP_MAX_BYTES = 50 * 1024 * 1024   # applies to the decompressed size (gzip-bomb guard)


class InputError(Exception):
    """Fatal problem with the URL source (missing file, unreachable sitemap...)."""


@dataclass
class UrlSet:
    urls: list[str]
    source_type: str        # "urls-file" | "sitemap" | "upload"
    source: str
    raw_count: int          # entries seen before validation / dedup
    invalid_count: int
    duplicate_count: int
    invalid: list[tuple[str, str]] = field(default_factory=list)   # (raw entry, reason)


# ------------------------------------------------------------------- shared pipeline
def finalize(raw_entries: list[str], source_type: str, source: str, *, upload: bool = False) -> UrlSet:
    """normalise -> validate -> (upload hardening) -> deduplicate keeping first-seen order."""
    urls: list[str] = []
    seen: set[str] = set()
    invalid = duplicates = 0
    rejected: list[tuple[str, str]] = []
    for raw in raw_entries:
        normalized, problem = normalize_url(raw)
        if normalized is not None and upload:
            problem = _upload_problem(normalized)
            if problem:
                normalized = None
        if normalized is None:
            invalid += 1
            rejected.append((raw[:500], problem or "invalid URL"))
            log_errors.error("INVALID_URL | %s | %s | %r", source_type, problem, raw[:200])
            continue
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        urls.append(normalized)
    return UrlSet(urls, source_type, source, len(raw_entries), invalid, duplicates, rejected)


def _upload_problem(url: str) -> str | None:
    """Extra checks for user-supplied files: length, localhost and literal private/loopback IPs.

    Only literal addresses can be checked offline; hostnames that *resolve* to private ranges
    are not detected here.
    """
    if len(url) > UPLOAD_MAX_URL_LENGTH:
        return f"URL longer than {UPLOAD_MAX_URL_LENGTH} characters"
    host = hostname_of(url)
    if host == "localhost" or host.endswith(".localhost"):
        return "localhost is not allowed in uploaded files"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return f"non-public IP address {host} is not allowed in uploaded files"
    return None


# ------------------------------------------------------------------------ file input
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


def parse_url_text(text: str) -> list[str]:
    """One URL per line (pasted text or a .txt file).

    Blank lines and ``#`` comment lines are skipped; a trailing `` # note`` is removed. A ``#``
    with no space before it is a URL fragment and is kept (normalisation drops it later).
    """
    entries = []
    for line in text.splitlines():
        line = _INLINE_COMMENT_RE.sub("", line.strip())
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def load_from_text(text: str, source: str = "pasted") -> UrlSet:
    return finalize(parse_url_text(text), "urls-file", source)


def read_url_entries(path: Path, *, upload: bool = False) -> list[str]:
    """Return raw URL strings from a text or CSV file.

    * ``.csv`` (or a first line that parses as a CSV header containing a URL column name):
      the URL column is taken from the header; a header-less CSV uses its first column.
    * anything else: one URL per line; blank lines and ``#`` comments are skipped.
    A UTF-8 BOM is stripped. Undecodable bytes are replaced rather than aborting the run.
    """
    if not path.is_file():
        raise InputError(f"File not found: {path}")
    if upload and path.stat().st_size > UPLOAD_MAX_BYTES:
        raise InputError(f"Uploaded file exceeds {UPLOAD_MAX_BYTES // (1024 * 1024)} MB limit")

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    first_cells = next(csv.reader([lines[0]]), [])
    header_idx = next(
        (i for i, c in enumerate(first_cells) if c.strip().lower() in _URL_HEADERS), None
    )
    is_csv = path.suffix.lower() == ".csv" or header_idx is not None

    if is_csv:
        rows = list(csv.reader(io.StringIO("\n".join(lines))))
        col = header_idx if header_idx is not None else 0
        if header_idx is not None:
            rows = rows[1:]  # drop the detected header row
        entries = [r[col] for r in rows if len(r) > col and r[col].strip()]
    else:
        entries = parse_url_text("\n".join(lines))

    if upload and len(entries) > UPLOAD_MAX_URLS:
        raise InputError(f"Uploaded file has more than {UPLOAD_MAX_URLS} URLs")
    return entries


def load_from_file(path: str, *, upload: bool = False) -> UrlSet:
    p = Path(path)
    entries = read_url_entries(p, upload=upload)
    return finalize(entries, "upload" if upload else "urls-file", str(p), upload=upload)


# -------------------------------------------------------------------------- sitemap
def _gunzip_limited(data: bytes, limit: int) -> bytes:
    out = zlib.decompressobj(wbits=31).decompress(data, limit + 1)
    if len(out) > limit:
        raise InputError("Decompressed sitemap exceeds size limit")
    return out


def parse_sitemap_xml(data: bytes) -> tuple[str, list[str]]:
    """``(kind, locs)`` where kind is "index" (child sitemaps) or "urlset" (page URLs).

    Namespace-agnostic (matches on local names). XML entity expansion and network access are
    disabled, so hostile sitemaps cannot trigger XXE / billion-laughs.
    """
    if data[:2] == b"\x1f\x8b":
        data = _gunzip_limited(data, SITEMAP_MAX_BYTES)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)
    root = etree.fromstring(data, parser)
    if root is None:
        raise InputError("Not valid XML")
    root_name = etree.QName(root).localname.lower()
    if root_name == "sitemapindex":
        kind, item = "index", "sitemap"
    elif root_name == "urlset":
        kind, item = "urlset", "url"
    else:
        raise InputError(f"Unexpected sitemap root element <{root_name}>")
    locs: list[str] = []
    for el in root:
        if not isinstance(el.tag, str) or etree.QName(el).localname.lower() != item:
            continue
        for child in el:
            if isinstance(child.tag, str) and etree.QName(child).localname.lower() == "loc":
                if child.text and child.text.strip():
                    locs.append(child.text.strip())
                break
    return kind, locs


async def load_from_sitemap(sitemap_url: str, dispatcher) -> UrlSet:
    """Fetch a sitemap (and, recursively, every child sitemap of an index)."""
    start, problem = normalize_url(sitemap_url)
    if start is None:
        raise InputError(f"Invalid sitemap URL: {problem}")

    entries: list[str] = []
    visited: set[str] = set()

    async def walk(url: str, depth: int) -> None:
        if url in visited:
            return
        visited.add(url)
        result = await dispatcher.fetch(url)
        if result.error or result.status != 200:
            msg = result.error or f"HTTP {result.status}"
            log_errors.error("SITEMAP_FETCH | %s | %s", url, msg)
            if depth == 0:
                raise InputError(f"Could not fetch sitemap {url}: {msg}")
            return
        try:
            kind, locs = parse_sitemap_xml(result.content)
        except (InputError, etree.XMLSyntaxError, zlib.error) as exc:
            log_errors.error("SITEMAP_PARSE | %s | %s", url, exc)
            if depth == 0:
                raise InputError(f"Could not parse sitemap {url}: {exc}") from exc
            return
        if kind == "urlset":
            entries.extend(locs)
            return
        if depth >= SITEMAP_MAX_DEPTH:
            log_errors.error("SITEMAP_DEPTH | %s | index nesting deeper than %d", url, SITEMAP_MAX_DEPTH)
            return
        for child in locs:
            child_url, why = normalize_url(child)
            if child_url is None:
                log_errors.error("INVALID_URL | sitemap-child | %s | %r", why, child[:200])
                continue
            await walk(child_url, depth + 1)

    await walk(start, 0)
    return finalize(entries, "sitemap", start)
