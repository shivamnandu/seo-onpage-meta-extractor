"""Shared helpers: URL normalisation, domain matching, text cleaning, logging.

Nothing in this module makes network calls or parses HTML.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Realistic desktop Chrome 120 on Windows (the spec's default UA).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Logger names. Each one is routed to its own file by setup_logging().
LOG_ERRORS = "seo.errors"          # network errors, timeouts, DNS failures, invalid URLs
LOG_WAF = "seo.waf"                # 403/429, challenge pages, blocked fingerprints
LOG_PARSE = "seo.parse"            # parsability issues and warnings
LOG_REQUESTS = "seo.requests"      # one line per HTTP request (rate-limit audit trail)

_LOG_FILES = {
    LOG_ERRORS: "errors.log",
    LOG_WAF: "waf_blocks.log",
    LOG_PARSE: "parse_warnings.log",
    LOG_REQUESTS: "requests.log",
}

_WS_RE = re.compile(r"\s+")
# ASCII control characters (incl. NUL, CR, LF, TAB) are never legal inside a URL.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def setup_logging(output_dir: Path, append: bool = False) -> None:
    """Create the four log files in ``output_dir`` (truncated on every run unless ``append``)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, filename in _LOG_FILES.items():
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        handler = logging.FileHandler(output_dir / filename, mode="a" if append else "w",
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        logger.addHandler(handler)


def shutdown_logging() -> None:
    for name in _LOG_FILES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collapse_ws(text: str) -> str:
    """Trim and collapse every whitespace run (incl. NBSP-free newlines/tabs) to one space."""
    return _WS_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------- URLs
def normalize_url(raw: str) -> tuple[str | None, str | None]:
    """Return ``(normalized_url, None)`` or ``(None, reason)``.

    Rules (all deterministic):
      1. strip whitespace and control characters at the edges; reject if any remain inside;
      2. add a scheme if missing (``//host`` -> https, ``host/path`` -> https);
      3. only http/https are accepted;
      4. drop the #fragment;
      5. lowercase scheme and host; an empty path becomes ``/``;
      6. host must be non-empty and the port (if any) must be a valid integer.
    """
    if raw is None:
        return None, "empty value"
    text = _CONTROL_RE.sub("", raw.strip())
    if not text:
        return None, "empty value"
    if re.search(r"\s", text):
        return None, "contains whitespace"

    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        # "mailto:x", "javascript:x" etc. have a scheme but no "://": reject explicitly.
        head = text.split("/", 1)[0]
        if ":" in head and not re.fullmatch(r"[^:/@]+:\d+", head):
            return None, f"unsupported scheme in '{text[:40]}'"
        text = "https://" + text

    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        port = parts.port  # raises ValueError for a non-numeric / out-of-range port
    except ValueError as exc:
        return None, f"invalid URL syntax ({exc})"

    if parts.scheme.lower() not in ("http", "https"):
        return None, f"unsupported scheme '{parts.scheme}'"
    if not hostname:
        return None, "missing host"

    host = hostname.lower()
    if ":" in host:  # IPv6 literal must be re-bracketed
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    if parts.username is not None:
        userinfo = parts.username + (f":{parts.password}" if parts.password is not None else "")
        netloc = f"{userinfo}@{netloc}"

    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, "")), None


def hostname_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def registrable_domain(host: str) -> str:
    """Site-level key used for per-domain politeness delays (``a.bank.com`` -> ``bank.com``).

    Uses tldextract's *bundled* public-suffix snapshot (no network access). Falls back to the
    hostname itself if tldextract is unavailable or the host has no registrable domain (IPs).
    """
    try:
        import tldextract

        extractor = _tld_extractor()
        ext = extractor(host)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}".lower()
    except Exception:  # noqa: BLE001 - politeness key must never break a crawl
        pass
    return host


_TLD = None


def _tld_extractor():
    global _TLD
    if _TLD is None:
        import tldextract

        _TLD = tldextract.TLDExtract(suffix_list_urls=())  # () = never fetch, use snapshot
    return _TLD


def parse_sensitive_domains(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip().lower() for p in value.split(",") if p.strip()]


def host_matches(host: str, patterns: list[str]) -> bool:
    """True if ``host`` matches any ``--sensitive-domains`` pattern.

    * ``*.bank.com`` matches ``bank.com`` itself and every subdomain.
    * Any other pattern is an fnmatch glob against the full hostname (``bank.com`` is exact).
    """
    host = host.lower()
    for pattern in patterns:
        if pattern.startswith("*."):
            base = pattern[2:]
            if host == base or host.endswith("." + base):
                return True
        elif fnmatch.fnmatchcase(host, pattern):
            return True
    return False
