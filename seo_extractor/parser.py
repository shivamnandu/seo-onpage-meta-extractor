"""HTML -> validated ExtractedParam list. Synchronous on purpose (CPU-bound, fast with lxml)."""
from __future__ import annotations

from collections import defaultdict

from bs4 import BeautifulSoup

from seo_extractor.extractors import (
    extract_canonical,
    extract_h1,
    extract_hreflang,
    extract_meta_description,
    extract_meta_robots,
    extract_og,
    extract_title,
    extract_twitter,
)
from seo_extractor.models import ExtractedParam
from seo_extractor.validators import validate_all

# Extractor order == output group order (see models.GROUP_ORDER).
_EXTRACTORS = (
    extract_title,
    extract_meta_description,
    extract_meta_robots,
    extract_canonical,
    extract_og,
    extract_twitter,
    extract_hreflang,
    extract_h1,
)


class HTMLParser:
    """BeautifulSoup + lxml. No regular expressions are ever applied to HTML."""

    def parse(
        self,
        content: bytes | str,
        page_urls: set[str],
        charset: str | None = None,
    ) -> list[ExtractedParam]:
        """Extract every parameter, number repeated keys, then validate.

        ``content`` is passed as bytes when possible so that lxml/UnicodeDammit can honour a
        BOM or <meta charset>; ``charset`` (from the HTTP header) is tried first if given.
        """
        if isinstance(content, bytes):
            soup = BeautifulSoup(content, "lxml", from_encoding=charset)
        else:
            soup = BeautifulSoup(content, "lxml")

        params: list[ExtractedParam] = []
        for extractor in _EXTRACTORS:
            params.extend(extractor(soup))

        # Instance numbers: 1-based, per column key, in document order (title_1, title_2, ...).
        counters: dict[str, int] = defaultdict(int)
        for p in params:
            counters[p.key] += 1
            p.index = counters[p.key]

        validate_all(params, page_urls)
        return params
