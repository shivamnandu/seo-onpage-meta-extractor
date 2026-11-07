"""<link rel="canonical"> extractor."""
from __future__ import annotations

from bs4 import BeautifulSoup

from seo_extractor.extractors.base import attr, build_param, classify_link, is_inert
from seo_extractor.models import ExtractedParam


def extract_canonical(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: <link> whose ``rel`` token list contains ``canonical`` (case-insensitive).

    rel="alternate" (hreflang, feeds) and rel="amphtml" are never returned. Value = href.
    """
    out: list[ExtractedParam] = []
    for tag in soup.find_all("link"):
        if is_inert(tag) or classify_link(tag) != "canonical":
            continue
        out.append(build_param("canonical", "canonical", tag, (attr(tag, "href") or "").strip()))
    return out
