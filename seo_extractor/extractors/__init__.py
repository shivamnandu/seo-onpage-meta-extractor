"""One module per parameter family. Extractors only *capture*; validators.py *classifies*."""
from seo_extractor.extractors.canonical import extract_canonical
from seo_extractor.extractors.h1 import extract_h1
from seo_extractor.extractors.hreflang import extract_hreflang
from seo_extractor.extractors.meta import extract_meta_description, extract_meta_robots
from seo_extractor.extractors.og import extract_og
from seo_extractor.extractors.title import extract_title
from seo_extractor.extractors.twitter import extract_twitter

__all__ = [
    "extract_title",
    "extract_meta_description",
    "extract_meta_robots",
    "extract_canonical",
    "extract_og",
    "extract_twitter",
    "extract_hreflang",
    "extract_h1",
]
