"""HTTP layer: StandardFetcher (httpx), WAFProofFetcher (curl_cffi) and a per-URL dispatcher.

Design points
* Redirects are followed MANUALLY. Every hop and every retry is a real HTTP request, so each one
  must pass through the RateLimiter; letting the HTTP library follow redirects would silently
  break the --rps guarantee.
* Block/challenge detection runs in both modes (it is logged to waf_blocks.log). Only the
  WAF-proof mode retries (once, with a different TLS fingerprint); never more than 2 retries/URL.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from seo_extractor.models import FetchResult
from seo_extractor.rate_limiter import RateLimiter
from seo_extractor.utils import (
    DEFAULT_USER_AGENT,
    LOG_ERRORS,
    LOG_REQUESTS,
    LOG_WAF,
    host_matches,
    hostname_of,
    normalize_url,
    registrable_domain,
)

log_errors = logging.getLogger(LOG_ERRORS)
log_waf = logging.getLogger(LOG_WAF)
log_requests = logging.getLogger(LOG_REQUESTS)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_BODY_BYTES = 10 * 1024 * 1024   # <head> and all headings are far earlier than this
MAX_WAF_RETRIES_PER_URL = 2         # hard ceiling required by the spec


class FetchError(Exception):
    """Transport-level failure (timeout, DNS, TLS, connection reset...)."""


@dataclass
class RawResponse:
    status: int
    headers: dict[str, str]   # lowercase names
    content: bytes


# ------------------------------------------------------------------ block detection
# (vendor, body substrings). Matching is plain substring search on lowercased bytes -- this is
# fingerprinting a challenge page, not parsing HTML.
_BODY_FINGERPRINTS = (
    ("cloudflare", ("cf-chl", "just a moment...", "challenge-platform", "cf-browser-verification",
                    "attention required! | cloudflare", "cf-error-details")),
    ("akamai", ("errors.edgesuite.net", "akamai reference", "akamaighost")),
    ("datadome", ("captcha-delivery.com", "geo.captcha-delivery", "datadome")),
    ("imperva", ("_incapsula_resource", "incapsula incident id")),
    ("perimeterx", ("px-captcha", "_pxhd", "human security")),
)
_SMALL_PAGE_BYTES = 60_000  # challenge interstitials are tiny; big 200 pages are never scanned


def detect_challenge(status: int, headers: dict[str, str], body: bytes) -> str | None:
    """Return the block vendor ("cloudflare", "akamai", "datadome", ..., "rate_limit",
    "http_403") or None. Deterministic: header checks first, then body fingerprints."""
    server = headers.get("server", "").lower()
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return "cloudflare"
    if status >= 400:
        if "x-datadome" in headers or "x-datadome-cid" in headers or server == "datadome":
            return "datadome"
        if "akamaighost" in server or "akamainetstorage" in server:
            if status in (403, 429):
                return "akamai"
        if headers.get("x-iinfo") or "incap_ses" in headers.get("set-cookie", "").lower():
            if status in (403, 429):
                return "imperva"
    if status >= 400 or len(body) < _SMALL_PAGE_BYTES:
        text = body[:200_000].decode("utf-8", "ignore").lower()
        for vendor, needles in _BODY_FINGERPRINTS:
            if any(n in text for n in needles):
                # A 2xx page containing e.g. the word "datadome" in a blog post is not a block;
                # for 2xx require the strongest Cloudflare/PX/DataDome interstitial markers.
                if status < 400 and not _strong_marker(vendor, text):
                    continue
                return vendor
    if status == 429:
        return "rate_limit"
    if status == 403:
        return "http_403"
    return None


def _strong_marker(vendor: str, text: str) -> bool:
    return (
        (vendor == "cloudflare" and ("cf-chl" in text or "<title>just a moment..." in text))
        or (vendor == "datadome" and "captcha-delivery.com" in text)
        or (vendor == "perimeterx" and "px-captcha" in text)
        or (vendor == "imperva" and "incapsula incident id" in text)
    )


def parse_content_type(value: str) -> tuple[str, str | None]:
    """``"text/html; charset=UTF-8"`` -> ``("text/html", "utf-8")``."""
    mime, _, rest = value.partition(";")
    charset = None
    for param in rest.split(";"):
        key, _, val = param.strip().partition("=")
        if key.strip().lower() == "charset" and val:
            charset = val.strip().strip("\"'").lower()
    return mime.strip().lower(), charset


# ------------------------------------------------------------------------- base
class BaseFetcher:
    waf_mode = False

    def __init__(self, limiter: RateLimiter, timeout: float, max_redirects: int,
                 user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.limiter = limiter
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent

    async def open(self) -> None: ...
    async def close(self) -> None: ...

    # hooks -------------------------------------------------------------------
    def _new_state(self):
        return None

    async def _close_state(self, state) -> None: ...

    async def _pace(self, url: str) -> None:
        """Runs BEFORE the rate-limiter slot (so sleeping never occupies a slot)."""

    async def _send(self, url: str, state) -> RawResponse:
        raise NotImplementedError

    def _describe(self, state) -> str:
        return ""

    def _wants_retry(self, raw: RawResponse | None, block: str | None,
                     hop_retried: bool, retries_used: int) -> bool:
        """Standard mode: one retry on a transport error, none on blocks."""
        return raw is None and not hop_retried and retries_used < 1

    async def _prepare_retry(self, state) -> None: ...

    # main loop ---------------------------------------------------------------
    async def fetch(self, url: str) -> FetchResult:
        result = FetchResult(url=url, final_url=url, waf_mode=self.waf_mode)
        started = time.monotonic()
        state = self._new_state()
        retries_used = 0
        current = url
        seen = {url}
        try:
            for _hop in range(self.max_redirects + 1):
                hop_retried = False
                while True:
                    result.attempts += 1
                    await self._pace(current)
                    raw: RawResponse | None = None
                    error: str | None = None
                    t0 = time.monotonic()
                    try:
                        async with self.limiter.slot():
                            t0 = time.monotonic()  # network time only, not time queued for a slot
                            raw = await asyncio.wait_for(
                                self._send(current, state), timeout=self.timeout * 2
                            )
                    except FetchError as exc:
                        error = str(exc)
                    except asyncio.TimeoutError:
                        error = f"Timeout after {self.timeout * 2:.0f}s (total request deadline)"
                    ms = (time.monotonic() - t0) * 1000
                    result.request_ms += ms

                    block = detect_challenge(raw.status, raw.headers, raw.content) if raw else None
                    self._log_request(current, raw, ms, error, result.attempts, state)
                    retry = self._wants_retry(raw, block, hop_retried, retries_used)
                    if block:
                        log_waf.warning(
                            "%s | HTTP %s | vendor=%s | waf_mode=%s | %s | attempt=%d | action=%s",
                            current, raw.status, block, self.waf_mode, self._describe(state),
                            result.attempts, "retry-with-new-fingerprint" if retry else "skip",
                        )
                    if not retry:
                        break
                    retries_used += 1
                    hop_retried = True
                    await self._prepare_retry(state)

                if raw is None:
                    result.error = error
                    break
                result.status = raw.status
                result.bytes_received += len(raw.content)
                result.redirect_chain.append({"url": current, "status": raw.status})

                if block:
                    result.waf_block = block
                    result.error = f"Blocked by {block} (HTTP {raw.status})"
                    result.final_url = current
                    break

                location = raw.headers.get("location")
                if raw.status in REDIRECT_STATUSES and location:
                    target, _ = normalize_url(urldefrag(urljoin(current, location.strip()))[0])
                    if target is None:
                        result.error = f"Invalid redirect Location '{location[:120]}'"
                        break
                    if target in seen:
                        result.error = f"Redirect loop at {target}"
                        break
                    seen.add(target)
                    current = target
                    continue

                result.final_url = current
                result.content = raw.content
                result.content_type, result.charset = parse_content_type(
                    raw.headers.get("content-type", "")
                )
                break
            else:
                result.error = f"Too many redirects (limit {self.max_redirects})"
        finally:
            await self._close_state(state)
            result.elapsed_ms = (time.monotonic() - started) * 1000

        if len(result.redirect_chain) > 1:
            log_requests.info(
                "REDIRECT_CHAIN %s", " -> ".join(f"{h['url']} [{h['status']}]" for h in result.redirect_chain)
            )
        if result.error and not result.waf_block:
            log_errors.error("%s | %s", url, result.error)
        return result

    def _log_request(self, url, raw, ms, error, attempt, state) -> None:
        # Required audit line: URL, HTTP status, response time (ms), WAF mode, bytes received.
        log_requests.info(
            "url=%s status=%s time_ms=%.0f waf_mode=%s bytes=%d attempt=%d%s%s",
            url,
            raw.status if raw else "ERR",
            ms,
            self.waf_mode,
            len(raw.content) if raw else 0,
            attempt,
            f" {self._describe(state)}" if self._describe(state) else "",
            f" error={error}" if error else "",
        )


# ------------------------------------------------------------------ standard (httpx)
class StandardFetcher(BaseFetcher):
    """Plain async httpx client with a realistic desktop Chrome header set."""

    async def open(self) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.AsyncClient(
            follow_redirects=False,                       # manual, rate-limited hops
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=self.limiter.concurrency * 2),
            headers={
                "User-Agent": self.user_agent,
                "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                           "image/avif,image/webp,image/apng,*/*;q=0.8"),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _send(self, url: str, state) -> RawResponse:
        httpx = self._httpx
        try:
            async with self._client.stream("GET", url) as resp:
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BODY_BYTES:
                        break  # truncated; head and headings are long since parsed
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return RawResponse(resp.status_code, headers, b"".join(chunks))
        except httpx.TimeoutException as exc:
            raise FetchError(f"Timeout ({type(exc).__name__}) after {self.timeout:.0f}s") from exc
        except httpx.ConnectError as exc:
            text = str(exc).lower()
            if any(s in text for s in ("getaddrinfo", "name or service", "nodename", "name resolution")):
                raise FetchError(f"DNS failure: {exc}") from exc
            raise FetchError(f"Connection error: {exc}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- WAF-proof (curl_cffi)
# Impersonation profiles. The sec-ch-ua brand list MUST agree with the TLS/JA3/HTTP2
# fingerprint that curl_cffi presents; a mismatch is itself a bot signal.
_PROFILES = {
    "chrome120": {"major": 120,
                  "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'},
    "chrome124": {"major": 124,
                  "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'},
}
_PLATFORMS = (
    ("Windows", "Windows NT 10.0; Win64; x64"),
    ("macOS", "Macintosh; Intel Mac OS X 10_15_7"),
)
_ACCEPT_LANGUAGES = (
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.9,es;q=0.8",
)
_REFERERS = ("https://www.google.com/", "https://www.bing.com/", "https://duckduckgo.com/")
_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
           "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")


def build_chrome_headers(url: str, impersonate: str, user_agent_override: str | None,
                         rng: random.Random) -> dict[str, str]:
    """Full Chrome navigation header set, inserted in the exact order Chrome sends them.

    Host and Connection are listed to match the spec; libcurl turns Host into :authority and
    drops Connection on HTTP/2, exactly as a browser's h2 stack does.
    """
    profile = _PROFILES[impersonate]
    platform, os_token = rng.choice(_PLATFORMS)
    major = profile["major"]
    ua = user_agent_override or (
        f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    return {
        "Host": urlsplit(url).netloc,
        "Connection": "keep-alive",
        "sec-ch-ua": profile["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": ua,
        "Accept": _ACCEPT,
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Referer": rng.choice(_REFERERS),
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": rng.choice(_ACCEPT_LANGUAGES),
    }


class _WafState:
    """Per-URL state: which fingerprint is active and its own cookie jar (session)."""

    def __init__(self, impersonate: str) -> None:
        self.impersonate = impersonate
        self.session = None


class WAFProofFetcher(BaseFetcher):
    waf_mode = True

    def __init__(self, *args, min_delay: float = 2.0, max_delay: float = 5.0,
                 rng: random.Random | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.min_delay, self.max_delay = min_delay, max_delay
        self._rng = rng or random.Random()
        self._rotation = itertools.cycle(sorted(_PROFILES))   # chrome120, chrome124, ...
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_next_ok: dict[str, float] = {}
        self._default_ua = self.user_agent == DEFAULT_USER_AGENT

    async def open(self) -> None:
        try:
            from curl_cffi.requests import AsyncSession
            from curl_cffi.requests import exceptions as cexc
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("WAF-proof mode needs curl_cffi: pip install curl_cffi") from exc
        self._AsyncSession, self._cexc = AsyncSession, cexc

    def _new_state(self) -> _WafState:
        return _WafState(next(self._rotation))

    async def _close_state(self, state: _WafState) -> None:
        if state.session is not None:
            await state.session.close()
            state.session = None

    def _describe(self, state: _WafState) -> str:
        return f"impersonate={state.impersonate}"

    async def _pace(self, url: str) -> None:
        """Random 2-5 s gap between requests to the same registrable domain.

        The lock serialises callers per domain; the next permitted time is drawn when a request
        is released, so consecutive requests are always >= the drawn delay apart.
        """
        domain = registrable_domain(hostname_of(url))
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            wait = self._domain_next_ok.get(domain, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._domain_next_ok[domain] = time.monotonic() + self._rng.uniform(
                self.min_delay, self.max_delay
            )

    def _wants_retry(self, raw, block, hop_retried, retries_used) -> bool:
        """Retry ONCE per hop (transport error or block) with another fingerprint; total
        retries per URL never exceed MAX_WAF_RETRIES_PER_URL."""
        if hop_retried or retries_used >= MAX_WAF_RETRIES_PER_URL:
            return False
        return raw is None or block is not None

    async def _prepare_retry(self, state: _WafState) -> None:
        others = [p for p in sorted(_PROFILES) if p != state.impersonate]
        state.impersonate = self._rng.choice(others)   # different TLS fingerprint
        await self._close_state(state)                 # and a clean cookie jar

    async def _send(self, url: str, state: _WafState) -> RawResponse:
        cexc = self._cexc
        if state.session is None:
            state.session = self._AsyncSession(impersonate=state.impersonate)
        headers = build_chrome_headers(
            url, state.impersonate, None if self._default_ua else self.user_agent, self._rng
        )
        try:
            resp = await state.session.get(
                url, headers=headers, timeout=self.timeout, allow_redirects=False,
                impersonate=state.impersonate,
            )
        except Exception as exc:  # curl_cffi raises RequestException subclasses / curl error codes
            code = getattr(exc, "code", None)
            if isinstance(exc, getattr(cexc, "Timeout", ())) or code == 28:
                raise FetchError(f"Timeout after {self.timeout:.0f}s") from exc
            if code == 6:
                raise FetchError(f"DNS failure: {exc}") from exc
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        return RawResponse(
            resp.status_code,
            {k.lower(): v for k, v in resp.headers.items()},
            resp.content[:MAX_BODY_BYTES],
        )


# ----------------------------------------------------------------------- dispatcher
class FetchDispatcher:
    """Routes each URL to the standard or WAF-proof fetcher."""

    def __init__(self, limiter: RateLimiter, timeout: float, max_redirects: int,
                 user_agent: str, waf_mode: bool, sensitive_domains: list[str]) -> None:
        self.waf_mode = waf_mode
        self.sensitive_domains = sensitive_domains
        common = dict(limiter=limiter, timeout=timeout, max_redirects=max_redirects,
                      user_agent=user_agent)
        self.standard = StandardFetcher(**common)
        # curl_cffi is only required (and only imported) when WAF-proof mode can be triggered.
        self.waf = WAFProofFetcher(**common) if (waf_mode or sensitive_domains) else None

    def uses_waf(self, url: str) -> bool:
        return self.waf_mode or host_matches(hostname_of(url), self.sensitive_domains)

    async def __aenter__(self) -> "FetchDispatcher":
        await self.standard.open()
        if self.waf:
            await self.waf.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.standard.close()
        if self.waf:
            await self.waf.close()

    async def fetch(self, url: str) -> FetchResult:
        fetcher = self.waf if (self.waf and self.uses_waf(url)) else self.standard
        return await fetcher.fetch(url)
