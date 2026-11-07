"""Open Graph extractor."""
from __future__ import annotations

from bs4 import BeautifulSoup

from seo_extractor.extractors.base import attr, build_param, classify_meta, is_inert
from seo_extractor.models import ExtractedParam


def extract_og(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: every <meta> whose ``property`` starts with ``og:`` (case-insensitive prefix).

    Column key = property with ``:`` -> ``_`` (og:image:width -> og_image_width). Repeated
    properties (e.g. several og:image) are numbered later by the parser (_1, _2, ...).
    Only ``property`` counts: <meta name="og:title"> is not Open Graph and is not returned.
    """
    out: list[ExtractedParam] = []
    for tag in soup.find_all("meta"):
        if is_inert(tag):
            continue
        classified = classify_meta(tag)
        if classified and classified[0] == "og":
            out.append(build_param("og", classified[1], tag, (attr(tag, "content") or "").strip()))
    return out
