"""<h1> extractor."""
from __future__ import annotations

from bs4 import BeautifulSoup

from seo_extractor.extractors.base import build_param, is_inert, text_of
from seo_extractor.models import ExtractedParam


def extract_h1(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: every <h1> in the document (not only the first), in document order.

    raw = the whole element including attributes, nested markup and the closing tag;
    value = its visible text, whitespace-collapsed. No <h1> at all -> a placeholder
    (present=False) so the validator can report "Missing <h1>".
    """
    tags = [t for t in soup.find_all("h1") if not is_inert(t)]
    if not tags:
        return [ExtractedParam(group="h1", key="h1", present=False, location="body")]
    return [build_param("h1", "h1", t, text_of(t)) for t in tags]
