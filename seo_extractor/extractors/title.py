"""<title> extractor."""
from __future__ import annotations

from bs4 import BeautifulSoup

from seo_extractor.extractors.base import build_param, is_inert, text_of
from seo_extractor.models import ExtractedParam


def extract_title(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: every <title> element that sits inside <head>, in document order.

    A <title> elsewhere (e.g. an SVG <title> in <body>) is not the document title and is ignored.
    If there is none, a placeholder (present=False) is returned so the validator can report
    "Missing <title>" as an explicit unparsable entry.
    """
    head = soup.head
    titles = [t for t in head.find_all("title") if not is_inert(t)] if head else []
    if not titles:
        return [ExtractedParam(group="title", key="title", present=False)]
    return [build_param("title", "title", t, text_of(t)) for t in titles]
