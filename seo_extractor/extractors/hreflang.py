"""hreflang extractor."""
from __future__ import annotations

from bs4 import BeautifulSoup

from seo_extractor.extractors.base import attr, build_param, classify_link, is_inert, key_from
from seo_extractor.models import ExtractedParam


def extract_hreflang(soup: BeautifulSoup) -> list[ExtractedParam]:
    """RULE: <link> with rel token ``alternate`` AND an ``hreflang`` attribute (x-default included).

    Column key = ``hreflang_`` + code lowercased with non-alphanumerics -> ``_``
    (en-US -> hreflang_en_us, x-default -> hreflang_x_default). An empty code becomes
    ``hreflang_empty``. Value = href. The original code stays in attrs["hreflang"] for validation.
    """
    out: list[ExtractedParam] = []
    for tag in soup.find_all("link"):
        if is_inert(tag) or classify_link(tag) != "hreflang":
            continue
        code = (attr(tag, "hreflang") or "").strip()
        out.append(build_param("hreflang", f"hreflang_{key_from(code) or 'empty'}", tag,
                               (attr(tag, "href") or "").strip()))
    return out
