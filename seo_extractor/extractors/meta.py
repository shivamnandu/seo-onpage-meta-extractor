"""<meta name="description"> and <meta name="robots|googlebot|bingbot"> extractors."""
from __future__ import annotations

from bs4 import BeautifulSoup

from seo_extractor.extractors.base import attr, build_param, classify_meta, is_inert
from seo_extractor.models import ExtractedParam


def _extract(soup: BeautifulSoup, group: str) -> list[ExtractedParam]:
    out: list[ExtractedParam] = []
    for tag in soup.find_all("meta"):
        if is_inert(tag):
            continue
        classified = classify_meta(tag)  # single-family assignment lives in base.py
        if classified and classified[0] == group:
            content = attr(tag, "content")
            out.append(build_param(group, classified[1], tag, (content or "").strip()))
    return out


def extract_meta_description(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: <meta name="description"> ONLY.

    og:description / twitter:description are claimed by the OG / Twitter families first and
    <meta itemprop="description"> has no ``name`` attribute, so none of them can appear here.
    """
    return _extract(soup, "meta_description")


def extract_meta_robots(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: <meta name="robots"> -> key meta_robots; <meta name="googlebot"> ->
    meta_robots_googlebot; <meta name="bingbot"> -> meta_robots_bingbot (kept separate)."""
    return _extract(soup, "meta_robots")
