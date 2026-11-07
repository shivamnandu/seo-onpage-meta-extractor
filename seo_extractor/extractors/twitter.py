"""Twitter Card extractor."""
from __future__ import annotations

from bs4 import BeautifulSoup

from seo_extractor.extractors.base import attr, build_param, classify_meta, is_inert
from seo_extractor.models import ExtractedParam


def extract_twitter(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: every <meta> whose ``name`` OR ``property`` starts with ``twitter:``.

    Both spellings are valid in the wild. A tag that carries ``property="og:*"`` is Open Graph
    (base.classify_meta gives OG precedence), so twitter:title can never leak into og_title
    columns and vice-versa.
    """
    out: list[ExtractedParam] = []
    for tag in soup.find_all("meta"):
        if is_inert(tag):
            continue
        classified = classify_meta(tag)
        if classified and classified[0] == "twitter":
            out.append(build_param("twitter", classified[1], tag, (attr(tag, "content") or "").strip()))
    return out
