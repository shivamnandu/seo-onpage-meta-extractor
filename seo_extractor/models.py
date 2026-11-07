"""Dataclasses shared by every layer (fetch -> parse -> validate -> write)."""
from __future__ import annotations

from dataclasses import dataclass, field

PARSABLE = "parsable"
UNPARSABLE = "unparsable"

# Output order of parameter groups in the CSV/JSON ("grouped by parameter type").
GROUP_ORDER = (
    "title",
    "meta_description",
    "meta_robots",
    "canonical",
    "og",
    "twitter",
    "hreflang",
    "h1",
)

# Fixed key order inside the meta_robots group; every other group sorts keys alphabetically.
ROBOTS_KEY_ORDER = ("meta_robots", "meta_robots_googlebot", "meta_robots_bingbot")


@dataclass
class ExtractedParam:
    """One occurrence of one on-page parameter.

    ``group`` is the parameter family (see GROUP_ORDER); ``key`` is the column stem inside the
    family (e.g. group ``og`` -> key ``og_title``; group ``hreflang`` -> key ``hreflang_en_us``).
    """

    group: str
    key: str
    raw: str = ""                 # element re-serialised with opening + closing tag
    value: str = ""               # clean value (text for title/h1, attribute for meta/link)
    attrs: dict[str, str] = field(default_factory=dict)  # every attribute of the element
    present: bool = True          # False = placeholder for a *missing* title / h1
    location: str = "head"        # "head" | "body" | "other" (where the element sits in the DOM)
    index: int = 0                # 1-based instance number within (group, key)
    status: str = ""              # PARSABLE | UNPARSABLE (set by validators.py)
    reason: str = ""              # why it is unparsable ("" when parsable)
    warnings: list[str] = field(default_factory=list)  # non-fatal flags

    def to_json(self) -> dict:
        out = {"raw": self.raw, "value": self.value, "status": self.status, "reason": self.reason}
        if self.warnings:
            out["warnings"] = list(self.warnings)
        if self.location != "head":
            out["location"] = self.location
        return out


@dataclass
class FetchResult:
    url: str                                   # normalised URL that was requested
    final_url: str = ""
    status: int | None = None                  # final HTTP status (None = no response at all)
    content: bytes = b""
    content_type: str = ""
    charset: str | None = None
    redirect_chain: list[dict] = field(default_factory=list)   # [{"url":..., "status":...}, ...]
    elapsed_ms: float = 0.0                    # wall time for the whole fetch (incl. pacing waits)
    request_ms: float = 0.0                    # sum of network time of every request sent
    bytes_received: int = 0
    waf_mode: bool = False
    error: str | None = None                   # network / redirect / block error text
    waf_block: str | None = None               # challenge vendor if the final response was a block
    attempts: int = 0                          # HTTP requests actually sent for this URL


@dataclass
class PageResult:
    url: str
    final_url: str = ""
    http_status: int | None = None
    crawl_timestamp: str = ""
    waf_mode: bool = False
    fetch_error: str = ""
    response_time_ms: float = 0.0
    bytes_received: int = 0
    redirect_chain: list[dict] = field(default_factory=list)
    waf_blocked: bool = False
    fetched_ok: bool = False                   # 2xx and not blocked
    params: list[ExtractedParam] = field(default_factory=list)
    page_warnings: list[str] = field(default_factory=list)

    def grouped(self) -> dict[str, list[ExtractedParam]]:
        """``{key: [instances in document order]}`` for every extracted key."""
        out: dict[str, list[ExtractedParam]] = {}
        for p in self.params:
            out.setdefault(p.key, []).append(p)
        return out

    def all_warnings(self) -> list[str]:
        msgs = list(self.page_warnings)
        for p in self.params:
            for w in p.warnings:
                msgs.append(f"{p.key}_{p.index}: {w}")
        return msgs
