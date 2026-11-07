"""Shared DOM helpers for the extractors.

Disambiguation lives HERE (``classify_meta`` / ``classify_link``): every <meta>/<link> element is
assigned to *at most one* parameter family, so a tag can never land in two sets of columns.
"""
from __future__ import annotations

import re

from bs4.element import NavigableString, Tag
from bs4.formatter import HTMLFormatter

from seo_extractor.models import ExtractedParam
from seo_extractor.utils import collapse_ws

# Content inside these containers is inert (not rendered / not evaluated by crawlers as page
# markup), so meta/link/h1 elements found there are ignored.
_INERT_ANCESTORS = frozenset({"noscript", "template"})

_KEY_RE = re.compile(r"[^a-z0-9]+")

# <meta name="..."> values that map to the robots family -> column key.
ROBOTS_NAMES = {
    "robots": "meta_robots",
    "googlebot": "meta_robots_googlebot",
    "bingbot": "meta_robots_bingbot",
}


class _SourceOrderFormatter(HTMLFormatter):
    """bs4's stock formatters sort attributes alphabetically; keep source order instead."""

    def attributes(self, tag):
        return list(tag.attrs.items())


# Same settings as bs4's built-in "html5" formatter: void elements print without "/>".
_FORMATTER = _SourceOrderFormatter(void_element_close_prefix=None, empty_attributes_are_booleans=True)


def raw_html(tag: Tag) -> str:
    """Element serialised from the parsed DOM, opening + closing tags included.

    Void elements print without a trailing slash, so a canonical link reads
    ``<link rel="canonical" href="...">`` exactly like the source. Attribute order is preserved;
    entities are normalised (this is a re-serialisation of the DOM, not a byte-slice of the
    source).
    """
    return tag.decode(formatter=_FORMATTER)


def attr(tag: Tag, name: str) -> str | None:
    """Attribute value as a string (bs4 returns lists for rel/class): None if absent."""
    value = tag.get(name)
    if value is None:
        return None
    return " ".join(value) if isinstance(value, list) else str(value)


def all_attrs(tag: Tag) -> dict[str, str]:
    return {k: (" ".join(v) if isinstance(v, list) else str(v)) for k, v in tag.attrs.items()}


def location_of(tag: Tag) -> str:
    """Where the element lives: inside <head>, inside <body>, or neither."""
    for parent in tag.parents:
        if parent.name == "head":
            return "head"
        if parent.name == "body":
            return "body"
    return "other"


def is_inert(tag: Tag) -> bool:
    return any(parent.name in _INERT_ANCESTORS for parent in tag.parents)


def text_of(tag: Tag) -> str:
    """Visible text of an element, whitespace-collapsed.

    Only plain text nodes count (comments, <script>/<style> strings are separate bs4 classes),
    and <br> becomes a space so ``a<br>b`` reads "a b" rather than "ab".
    """
    parts: list[str] = []
    for node in tag.descendants:
        if type(node) is NavigableString:
            parts.append(str(node))
        elif isinstance(node, Tag) and node.name == "br":
            parts.append(" ")
    return collapse_ws("".join(parts))


def key_from(name: str) -> str:
    """``og:image:secure_url`` -> ``og_image_secure_url`` (lowercase, [a-z0-9_] only)."""
    return _KEY_RE.sub("_", name.lower()).strip("_")


def classify_meta(tag: Tag) -> tuple[str, str] | None:
    """Assign a <meta> element to exactly one family -> ``(group, key)`` or None.

    Precedence (first match wins, so nothing is ever double-counted):
      1. ``property`` starting with ``og:``             -> Open Graph
      2. ``name`` OR ``property`` starting ``twitter:`` -> Twitter Card
      3. ``name`` == description                        -> Meta Description
      4. ``name`` in robots / googlebot / bingbot       -> Meta Robots
    ``itemprop="description"`` (schema.org) has no ``name`` so it can never match rule 3, and
    ``og:description`` / ``twitter:description`` are consumed by rules 1-2 before rule 3 runs.
    <meta name="og:title"> (wrong attribute) is deliberately NOT treated as Open Graph.
    """
    name = (attr(tag, "name") or "").strip().lower()
    prop = (attr(tag, "property") or "").strip().lower()
    if prop.startswith("og:"):
        return "og", key_from(prop)
    for token in (name, prop):
        if token.startswith("twitter:"):
            return "twitter", key_from(token)
    if name == "description":
        return "meta_description", "meta_description"
    if name in ROBOTS_NAMES:
        return "meta_robots", ROBOTS_NAMES[name]
    return None


def classify_link(tag: Tag) -> str | None:
    """Assign a <link> element to exactly one family: "canonical", "hreflang" or None.

    ``rel`` is a whitespace-separated, ASCII-case-insensitive token list.
      * canonical -> token ``canonical`` present.
      * hreflang  -> token ``alternate`` present AND an ``hreflang`` attribute present.
    ``rel="amphtml"`` and plain ``rel="alternate"`` (feeds, media alternates) match neither.
    """
    tokens = {t.lower() for t in (attr(tag, "rel") or "").split()}
    if "canonical" in tokens:
        return "canonical"
    if "alternate" in tokens and tag.has_attr("hreflang"):
        return "hreflang"
    return None


def build_param(group: str, key: str, tag: Tag, value: str) -> ExtractedParam:
    return ExtractedParam(
        group=group,
        key=key,
        raw=raw_html(tag),
        value=value,
        attrs=all_attrs(tag),
        location=location_of(tag),
    )
