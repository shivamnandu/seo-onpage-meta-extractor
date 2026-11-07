"""Rule-based parsability validation. Pure functions, no I/O, no heuristics.

Every extracted element ends up with status ``parsable`` or ``unparsable``; every unparsable one
carries a specific human-readable reason. Non-fatal observations go to ``warnings`` instead
(they are logged to parse_warnings.log and included in the JSON / CSV ``warnings`` column).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from seo_extractor.models import PARSABLE, UNPARSABLE, ExtractedParam
from seo_extractor.utils import collapse_ws, normalize_url


def _fail(p: ExtractedParam, reason: str) -> None:
    p.status, p.reason = UNPARSABLE, reason


def _ok(p: ExtractedParam) -> None:
    p.status, p.reason = PARSABLE, ""


def url_problem(href: str) -> str | None:
    """Why ``href`` is not a valid ABSOLUTE http(s) URL, or None if it is."""
    if re.search(r"\s", href):
        return "Invalid URL syntax (contains whitespace)"
    try:
        parts = urlsplit(href)
        _ = parts.port  # raises ValueError on a bad port
    except ValueError as exc:
        return f"Invalid URL syntax ({exc})"
    if not parts.scheme:
        if parts.netloc:
            return "Protocol-relative URL (must be absolute http/https)"
        return "Relative URL (must be absolute http/https)"
    if parts.scheme.lower() not in ("http", "https"):
        return f"Unsupported URL scheme '{parts.scheme}' (must be http/https)"
    if not parts.hostname:
        return "Invalid URL syntax (missing host)"
    return None


# ------------------------------------------------------------------------------- title
def validate_title(items: list[ExtractedParam]) -> None:
    """PARSABLE: non-empty text.
    UNPARSABLE: no <title> in <head> (placeholder), empty text, or several <title> elements whose
    texts differ ("conflicting"). Several identical titles stay parsable + warning.
    """
    present = [p for p in items if p.present]
    if not present:
        for p in items:
            _fail(p, "Missing <title> tag in <head>")
        return
    distinct = {p.value for p in present if p.value}
    for p in present:
        if not p.value:
            _fail(p, "Empty <title> tag")
        elif len(distinct) > 1:
            _fail(p, f"Multiple conflicting <title> tags ({len(distinct)} distinct texts)")
        else:
            _ok(p)
        if len(present) > 1:
            p.warnings.append(f"{len(present)} <title> tags in document")


# ------------------------------------------------------------------- meta description
def validate_meta_description(items: list[ExtractedParam]) -> None:
    """PARSABLE: ``content`` attribute present, non-empty and not a repeat of an earlier tag.
    UNPARSABLE: content attribute missing, content empty, or identical text already seen in an
    earlier <meta name="description"> (duplicate identical). Length outside 50-160 characters is
    a WARNING only (still parsable).
    """
    seen: dict[str, int] = {}
    for p in items:
        content = p.attrs.get("content")
        if content is None:
            _fail(p, "Missing content attribute")
            continue
        text = collapse_ws(content)
        if not text:
            _fail(p, "Empty content attribute")
        elif text in seen:
            _fail(p, f"Duplicate identical meta description (same as instance {seen[text]})")
        else:
            seen[text] = p.index
            _ok(p)
            if not 50 <= len(text) <= 160:
                p.warnings.append(f"Length {len(text)} chars is outside the recommended 50-160")
    if len(items) > 1:
        for p in items:
            p.warnings.append(f"{len(items)} meta description tags in document")


# ------------------------------------------------------------------------ meta robots
# Directives accepted in <meta name="robots|googlebot|bingbot" content="...">.
# The 13 listed in the spec + ``all`` and ``none`` (documented by Google and Bing).
_NO_VALUE_DIRECTIVES = {
    "index", "noindex", "follow", "nofollow", "noarchive", "nosnippet",
    "notranslate", "indexifembedded", "noimageindex", "all", "none",
}
_VALUE_DIRECTIVES = {"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"}
_KNOWN_DIRECTIVES = _NO_VALUE_DIRECTIVES | _VALUE_DIRECTIVES
_IMAGE_PREVIEW_VALUES = {"none", "standard", "large"}
# (set A, set B): having a member of each set in one content string is contradictory.
_CONFLICTS = (
    ({"index", "all"}, {"noindex", "none"}),
    ({"follow", "all"}, {"nofollow", "none"}),
)
_RFC850_FORMATS = ("%A, %d-%b-%y %H:%M:%S", "%A, %d-%b-%Y %H:%M:%S")


def _valid_date(value: str) -> bool:
    """unavailable_after accepts RFC 822/1123, RFC 850 and ISO 8601 dates."""
    value = value.strip()
    if not value:
        return False
    try:
        parsedate_to_datetime(value)
        return True
    except (TypeError, ValueError, IndexError):
        pass
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    without_tz = value.rsplit(" ", 1)[0] if re.search(r"\s[A-Za-z]{2,5}$|\s[+-]\d{4}$", value) else value
    for fmt in _RFC850_FORMATS:
        try:
            datetime.strptime(without_tz, fmt)
            return True
        except ValueError:
            continue
    return False


def parse_robots(content: str) -> tuple[list[tuple[str, str | None]], list[str], list[str]]:
    """Split a robots content string -> (directives, errors, warnings).

    Comma-separated, case-insensitive. ``unavailable_after`` dates may themselves contain commas
    (RFC 850: "Monday, 25-Jun-10 ..."), so tokens that are not known directives directly after an
    ``unavailable_after`` are re-attached to its value.
    """
    directives: list[tuple[str, str | None]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for token in content.split(","):
        token = token.strip()
        if not token:
            warnings.append("Empty directive (stray comma)")
            continue
        name, sep, value = token.partition(":")
        name = name.strip().lower()
        if directives and directives[-1][0] == "unavailable_after" and name not in _KNOWN_DIRECTIVES:
            directives[-1] = ("unavailable_after", f"{directives[-1][1]}, {token}")
            continue
        directives.append((name, value.strip() if sep else None))

    for name, value in directives:
        if name not in _KNOWN_DIRECTIVES:
            errors.append(f"Unknown directive '{name}'")
        elif name in _NO_VALUE_DIRECTIVES:
            if value is not None:
                errors.append(f"Directive '{name}' does not take a value")
        elif not value:
            errors.append(f"Directive '{name}' requires a value")
        elif name in ("max-snippet", "max-video-preview"):
            if not re.fullmatch(r"-?\d+", value) or int(value) < -1:
                errors.append(f"Invalid value '{value}' for {name} (expected integer >= -1)")
        elif name == "max-image-preview":
            if value.lower() not in _IMAGE_PREVIEW_VALUES:
                errors.append(f"Invalid value '{value}' for max-image-preview (none|standard|large)")
        elif name == "unavailable_after":
            if not _valid_date(value):
                errors.append(f"Unparseable date '{value}' for unavailable_after")

    names = {n for n, _ in directives}
    for set_a, set_b in _CONFLICTS:
        hit_a, hit_b = sorted(names & set_a), sorted(names & set_b)
        if hit_a and hit_b:
            errors.append(f"Conflicting directives: {hit_a[0]} vs {hit_b[0]}")
    values_by_name: dict[str, set[str | None]] = defaultdict(set)
    for name, value in directives:
        values_by_name[name].add(value)
    for name, values in sorted(values_by_name.items()):
        if len(values) > 1 and name in _VALUE_DIRECTIVES:
            errors.append(f"Directive '{name}' repeated with different values")
    return directives, errors, warnings


def validate_meta_robots(items: list[ExtractedParam]) -> None:
    """PARSABLE: content present and every directive valid (see parse_robots), no conflicts.
    UNPARSABLE: content missing/empty, unknown directive, malformed directive value or
    contradictory directives (index+noindex, follow+nofollow, all+noindex, none+index, ...).
    Each of robots / googlebot / bingbot is validated independently (items share one key).
    """
    for p in items:
        content = p.attrs.get("content")
        if content is None:
            _fail(p, "Missing content attribute")
        elif not content.strip():
            _fail(p, "Empty content attribute")
        else:
            _, errors, warnings = parse_robots(content)
            p.warnings.extend(warnings)
            if errors:
                _fail(p, "; ".join(errors))
            else:
                _ok(p)
    if len(items) > 1:
        for p in items:
            p.warnings.append(f"{len(items)} <meta> tags with the same robots name")


# ------------------------------------------------------------------------ canonical
def validate_canonical(items: list[ExtractedParam]) -> None:
    """PARSABLE: href present, absolute http(s) URL with a host, valid syntax, and not in
    conflict with another canonical on the page.
    UNPARSABLE: href missing/empty, relative or protocol-relative URL (also logged as a warning),
    non-http(s) scheme, invalid syntax, or several canonicals with DIFFERENT hrefs.
    Several canonicals with identical hrefs stay parsable + warning.
    """
    for p in items:
        href = p.attrs.get("href")
        if href is None:
            _fail(p, "Missing href attribute")
        elif not href.strip():
            _fail(p, "Empty href attribute")
        else:
            problem = url_problem(href.strip())
            if problem:
                _fail(p, problem)
                if problem.startswith(("Relative", "Protocol-relative")):
                    p.warnings.append(problem)
            else:
                _ok(p)
    valid = [p for p in items if p.status == PARSABLE]
    distinct = {p.value for p in valid}
    if len(distinct) > 1:
        for p in valid:
            _fail(p, f"Multiple conflicting canonicals ({len(distinct)} distinct URLs)")
    if len(items) > 1:
        for p in items:
            p.warnings.append(f"{len(items)} canonical tags in document")


# ------------------------------------------------------------------------ Open Graph
# Official Open Graph protocol namespace (https://ogp.me) plus the four Facebook-documented
# extras that live under og:. Anything else under og: is unknown -> unparsable.
OG_PROPERTIES = frozenset({
    "og:title", "og:type", "og:image", "og:url", "og:description", "og:site_name",
    "og:determiner", "og:locale", "og:locale:alternate", "og:audio", "og:video",
    "og:image:url", "og:image:secure_url", "og:image:type", "og:image:width",
    "og:image:height", "og:image:alt",
    "og:video:url", "og:video:secure_url", "og:video:type", "og:video:width", "og:video:height",
    "og:audio:url", "og:audio:secure_url", "og:audio:type",
    "og:updated_time", "og:see_also", "og:rich_attachment", "og:ttl",
})
# Properties whose content must be an absolute http(s) URL.
_OG_URL_PROPERTIES = frozenset({
    "og:url", "og:image", "og:image:url", "og:image:secure_url", "og:video", "og:video:url",
    "og:video:secure_url", "og:audio", "og:audio:url", "og:audio:secure_url", "og:see_also",
})


def validate_og(items: list[ExtractedParam]) -> None:
    """PARSABLE: ``property`` is exactly one official ogp.me property (lowercase, no stray
    whitespace), ``content`` present and non-empty, and URL-typed properties hold absolute URLs.
    UNPARSABLE: malformed property (case/whitespace/empty name), property outside the official
    namespace, content missing/empty, or a non-absolute URL in a URL-typed property.
    """
    for p in items:
        prop = p.attrs.get("property", "")
        content = p.attrs.get("content")
        if prop != prop.strip() or prop != prop.lower() or prop == "og:":
            _fail(p, f"Malformed property '{prop}' (must be lowercase 'og:<name>' with no whitespace)")
        elif prop not in OG_PROPERTIES:
            _fail(p, f"Unknown Open Graph property '{prop}' (not in the ogp.me namespace)")
        elif content is None:
            _fail(p, "Missing content attribute")
        elif not content.strip():
            _fail(p, "Empty content attribute")
        elif prop in _OG_URL_PROPERTIES and (problem := url_problem(content.strip())):
            _fail(p, f"{prop} content is not a valid URL: {problem}")
        else:
            _ok(p)


# ------------------------------------------------------------------------ Twitter Card
_TWITTER_CARD_TYPES = frozenset({"summary", "summary_large_image", "app", "player"})


def validate_twitter(items: list[ExtractedParam]) -> None:
    """PARSABLE: name/property starts with ``twitter:``, content present and non-empty, and for
    twitter:card the value is one of summary, summary_large_image, app, player.
    UNPARSABLE: content missing/empty, unknown card type, or a bare ``twitter:`` name.
    """
    for p in items:
        token = (p.attrs.get("name") or "").strip().lower()
        if not token.startswith("twitter:"):
            token = (p.attrs.get("property") or "").strip().lower()
        content = p.attrs.get("content")
        if token == "twitter:":
            _fail(p, "Malformed name 'twitter:' (no property after the prefix)")
        elif content is None:
            _fail(p, "Missing content attribute")
        elif not content.strip():
            _fail(p, "Empty content attribute")
        elif token == "twitter:card" and content.strip() not in _TWITTER_CARD_TYPES:
            _fail(p, f"Unknown twitter:card type '{content.strip()}' "
                     "(expected summary, summary_large_image, app or player)")
        else:
            _ok(p)


# ------------------------------------------------------------------------ hreflang
_BCP47_SHAPE = re.compile(r"[A-Za-z0-9]{1,8}(-[A-Za-z0-9]{1,8})*")
_BCP47_FALLBACK = re.compile(
    r"(?i)[a-z]{2,3}(-[a-z]{3}){0,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?(-([a-z\d]{5,8}|\d[a-z\d]{3}))*"
)


def valid_language_tag(code: str) -> bool:
    """BCP-47 validity. Uses the ``langcodes`` registry check; without it, falls back to the
    RFC 5646 structural pattern. Underscores (en_US) are invalid in both."""
    # Hyphen-separated ASCII alphanumerics only. langcodes would silently accept "en_GB", but
    # hreflang (and BCP-47) require the hyphen.
    if not _BCP47_SHAPE.fullmatch(code):
        return False
    try:
        import langcodes
    except ImportError:
        return bool(_BCP47_FALLBACK.fullmatch(code))
    return langcodes.tag_is_valid(code)


def validate_hreflang(items: list[ExtractedParam], page_urls: set[str]) -> None:
    """PARSABLE: hreflang is a valid BCP-47 tag or ``x-default``, and href is a valid absolute
    http(s) URL.
    UNPARSABLE: empty/invalid language code, missing/empty href, or an invalid/relative href.
    FLAGS (warnings, status unchanged): the set has no self-referencing entry; the same code
    points at several different URLs. Cross-page reciprocity ("return links") is checked after
    the crawl in main.py because it needs the other pages.
    """
    for p in items:
        code = p.attrs.get("hreflang", "").strip()
        href = p.attrs.get("href")
        if not code:
            _fail(p, "Empty hreflang attribute")
        elif code.lower() != "x-default" and not valid_language_tag(code):
            _fail(p, f"Invalid BCP-47 language code '{code}'")
        elif href is None:
            _fail(p, "Missing href attribute")
        elif not href.strip():
            _fail(p, "Empty href attribute")
        elif problem := url_problem(href.strip()):
            _fail(p, f"Invalid href: {problem}")
        else:
            _ok(p)

    valid = [p for p in items if p.status == PARSABLE]
    if valid:
        normalised = {normalize_url(p.value)[0] for p in valid}
        if not normalised & page_urls:
            valid[0].warnings.append("No self-referencing hreflang entry for this page")
    targets: dict[str, set[str]] = defaultdict(set)
    for p in valid:
        targets[p.attrs["hreflang"].strip().lower()].add(p.value)
    for p in valid:
        if len(targets[p.attrs["hreflang"].strip().lower()]) > 1:
            p.warnings.append(f"hreflang '{p.attrs['hreflang']}' points to different URLs")


# ------------------------------------------------------------------------ H1
def validate_h1(items: list[ExtractedParam]) -> None:
    """PARSABLE: <h1> inside <body> with non-empty text.
    UNPARSABLE: no <h1> at all (placeholder), <h1> outside <body>, or <h1> without text.
    WARNINGS (status unchanged): more than one <h1>; identical text repeated in another <h1>.
    """
    present = [p for p in items if p.present]
    if not present:
        for p in items:
            _fail(p, "Missing <h1> tag")
        return
    first_seen: dict[str, int] = {}
    for p in present:
        if p.location != "body":
            _fail(p, "<h1> is not inside <body>")
        elif not p.value:
            _fail(p, "Empty <h1> (no text content)")
        else:
            _ok(p)
            if p.value in first_seen:
                p.warnings.append(f"Duplicate <h1> text (same as h1_{first_seen[p.value]})")
            else:
                first_seen[p.value] = p.index
        if len(present) > 1:
            p.warnings.append(f"{len(present)} <h1> tags on page")


# ------------------------------------------------------------------------ entry point
def validate_all(params: list[ExtractedParam], page_urls: set[str]) -> None:
    """Validate a whole page. ``page_urls`` = normalised requested + final URL (self-hreflang)."""
    by_group: dict[str, list[ExtractedParam]] = defaultdict(list)
    for p in params:
        by_group[p.group].append(p)

    validate_title(by_group["title"])
    validate_meta_description(by_group["meta_description"])
    # robots / googlebot / bingbot are separate tags with separate semantics.
    robots_by_key: dict[str, list[ExtractedParam]] = defaultdict(list)
    for p in by_group["meta_robots"]:
        robots_by_key[p.key].append(p)
    for group_items in robots_by_key.values():
        validate_meta_robots(group_items)
    validate_canonical(by_group["canonical"])
    # og / twitter rules are per element, so grouping by key is unnecessary.
    validate_og(by_group["og"])
    validate_twitter(by_group["twitter"])
    validate_hreflang(by_group["hreflang"], page_urls)
    validate_h1(by_group["h1"])

    for p in params:
        # Search engines only honour these in <head>; a body placement is worth flagging.
        if p.present and p.group != "h1" and p.location == "body":
            p.warnings.append("Element found in <body>; search engines expect it in <head>")
        if not p.status:  # defensive: nothing may leave without a classification
            _ok(p)
