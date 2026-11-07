# On-Page SEO Extractor (WAF-aware)

Bulk-extracts the on-page SEO parameters of many URLs: `<title>`, meta description, meta robots
(+googlebot/bingbot), canonical, Open Graph, Twitter Cards, hreflang and `<h1>` — **every** occurrence,
the raw element captured with its tags, and each one classified **parsable / unparsable** with a specific
reason by fixed rules (no heuristics, no AI). Tuned for banking / WAF-protected sites.

## What it does

- On launch, shows a **dialog box** asking how to supply the URLs:
  1. **Enter / paste URLs manually**
  2. **Sitemap URL** – `sitemap.xml`, a sitemap index (followed recursively) or `.xml.gz`
  3. **Select a file** (`.txt` / `.csv`; one URL per line, or a CSV with a `url` column; `#` comments,
     trailing ` # notes` and bare domains are handled)
- Lets you set requests/second, concurrency, timeout, redirects, **WAF-proof mode** (all URLs, or only
  domains you list such as `*.bank.com`), **Resume**, dry run and the output folder.
- Shows live progress (bar + one log line per URL) and has a **Stop** button that finishes in-flight
  requests and still saves the partial results.
- Writes, into the output folder:

| File | Content |
|---|---|
| `seo_extracted_data.csv` | the **pure extracted data** only: one row per URL, one cell per parameter (title, description, robots, canonical, OG, Twitter, hreflang, H1) — no HTML, no verdicts |
| `seo_audit.csv` | the **audit**: same layout, raw tags plus `<parameter>_status` / `<parameter>_reason` next to each |
| `seo_audit.json` | per-URL objects with the per-tag detail (raw, value, status, reason for every tag) |
| `seo_report_<timestamp>.xlsx` | **Summary**; **Extracted Data** (the pure data); **Audit** (parsable / unparsable + reason for every parameter, coloured); **Raw HTML** (the captured tags); **URLs** (OK / ISSUES / FAILED / WAF-BLOCKED); **Issues** (one row per unparsable tag or warning). Every requested URL has a row in the first three table sheets, parsable or not — failed, blocked, never-reached and invalid URLs included || `crawl_summary.json` | totals, avg response time, parameter counts, proof the `--rps` ceiling held |
| `requests.log` | every request: URL, HTTP status, ms, WAF mode, bytes (+ redirect chains) |
| `errors.log` / `waf_blocks.log` / `parse_warnings.log` | network errors + invalid URLs / blocks and challenges / unparsable elements and warnings |
| `journal.jsonl` | per-URL results, the source of truth for **Resume** |

## Setup

```powershell
# 1. Open the project folder in VS Code
#    File > Open Folder... > the cloned `seo-onpage-meta-extractor` folder

# 2. Open the integrated terminal:  Ctrl + `  (backtick)
cd "seo-onpage-meta-extractor"

# 3. (Recommended) create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
#    If activation is blocked by execution policy, run once:
#    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 4. Install dependencies
pip install -r requirements.txt
```

If `Activate.ps1` is blocked and you don't want to change the policy, use the venv Python directly:
`.\.venv\Scripts\python.exe seo_tool.py`. Python 3.10+ is required (3.12+ recommended on Windows).

## Run

### GUI (default)

```powershell
python seo_tool.py
```

Or in VS Code: open `seo_tool.py` and press **F5** (a "SEO Extractor (GUI dialog)" launch config is
included), after selecting your `.venv` interpreter (bottom-right status bar, or
`Python: Select Interpreter` from `Ctrl+Shift+P`).

### Command line

```powershell
python seo_tool.py --urls-file sample_urls.txt                       # URL list / CSV
python seo_tool.py --sitemap https://example.com/sitemap.xml         # sitemap or sitemap index
python seo_tool.py --upload uploaded.csv                             # user-supplied file (hardened, see below)
python seo_tool.py --urls-file urls.txt --dry-run                    # just resolve, dedupe and count
python seo_tool.py --urls-file urls.txt --resume                     # continue an interrupted/blocked run

# Banking / finance: WAF-proof for everything, slow and polite
python seo_tool.py --urls-file urls.txt --waf-mode --rps 1 --concurrency 1
# WAF-proof only for the sensitive domains, standard mode for the rest
python seo_tool.py --urls-file urls.txt --sensitive-domains "*.bank.com,portal.agency.gov"
```

`python -m seo_extractor ...` is equivalent. Default output folder: `./seo_audit_output`.

| Flag | Default | |
|---|---|---|
| `--urls-file` / `--sitemap` / `--upload` | — | exactly one required |
| `--output-dir` | `./seo_audit_output` | |
| `--rps` | 2 | hard ceiling on requests/second in **any** 1 s window |
| `--concurrency` | 5 | max in-flight requests |
| `--timeout` | 15 | seconds per request |
| `--max-redirects` | 5 | |
| `--waf-mode` | off | WAF-proof mode for every URL |
| `--sensitive-domains` | — | `*.bank.com` matches `bank.com` and all subdomains; other patterns are exact/fnmatch (`bank.com` does **not** match `www.bank.com`) |
| `--user-agent` | Chrome 120 / Windows | in WAF mode this replaces the generated UA (which can weaken the fingerprint match) |
| `--resume` | off | skip URLs already in `journal.jsonl`; network errors and WAF blocks are retried |
| `--no-excel` | off | skip the `.xlsx` (CSV/JSON are always written) |
| `--dry-run` | off | for `--sitemap` the sitemap is still fetched |

`--upload` behaves like `--urls-file` but is hardened for user-supplied files: 20 MB / 100,000 URL /
2,048-character caps, and `localhost` or literal private/loopback IPs are rejected.

## WAF-sensitive design

- **Standard mode:** `httpx` with a realistic Chrome header set.
- **WAF-proof mode:** `curl_cffi` impersonating `chrome120` / `chrome124` (TLS/JA3/HTTP2 fingerprint) with a
  matching `sec-ch-ua` set sent in Chrome's header order, a random **2–5 s gap** between requests to the
  same registrable domain, and challenge detection (Cloudflare, Akamai, DataDome, Imperva, PerimeterX,
  plain 403/429). A block is logged to `waf_blocks.log`, retried **once** with the other fingerprint and a
  fresh cookie jar, then skipped. Hard cap: 2 retries per URL.
- **Rate limiting:** `Throttler(rate_limit=rps, period=1.0)` + `ThrottlerSimultaneous(count=concurrency)`.
  Redirects are followed manually so **every hop and every retry** is throttled.

> WAF-proof mode makes requests look like Chrome at the TLS, HTTP/2 and header level. It cannot solve
> JavaScript or CAPTCHA challenges, and sites can still block on IP reputation or behaviour. Blocked URLs
> are reported (`WAF-BLOCKED`), never silently dropped. Use it only on sites you are permitted to crawl.
> JavaScript-injected tags are not seen; only the served HTML is parsed.

## Output columns

One row per URL and **one cell per parameter**: a cell holds every occurrence of that parameter, one per
line (all Open Graph tags together in `og`, all `<h1>`s together in `h1`, and so on).

**Extracted Data** (`seo_extracted_data.csv`): `url, http_status, result, final_url`, then `title,
meta_description, meta_robots, canonical, og, twitter, hreflang, h1` holding the values only:

| parameter | cell content |
|---|---|
| `title`, `meta_description`, `canonical`, `h1` | the text / URL, one per line |
| `og`, `twitter`, `hreflang`, `meta_robots` | `name: value`, one per line — `og:title: Welcome`, `twitter:card: summary`, `en-US: https://a.com/en`, `googlebot: noindex` |

**Audit** (`seo_audit.csv`, and the Excel Audit + Raw HTML sheets): `url, http_status, result,
crawl_timestamp, waf_mode, final_url, fetch_error`, then for each parameter `<parameter>` (raw tags, one per
line) · `<parameter>_status` · `<parameter>_reason`, and finally `warnings`. `_status` is `unparsable` if
**any** tag of that parameter is unparsable, otherwise `parsable` (empty when the page has none of that
parameter). `_reason` lists the failing tags, one per line, prefixed with the tag name when there are several
(`og_image_2: Missing content attribute`). In Excel the Audit sheet holds the status/reason columns and the
Raw HTML sheet the tags.

`result` is OK / ISSUES / FAILED / WAF-BLOCKED, or NOT CRAWLED / INVALID URL for requested URLs that were
never fetched (Stop pressed, or rejected during input validation). The per-tag breakdown (keys such as
`og_title`, `hreflang_en_us`) is in `seo_audit.json` and the Excel **Issues** sheet.

Raw tags are re-serialised from the parsed DOM (attribute order kept, entities normalised), not a byte
slice of the source. CSV cells starting with `= + - @` get a leading `'` so spreadsheets cannot run them as
formulas (the Excel report stores them as text; JSON is untouched). CSVs are UTF‑8 with BOM.
## Extraction rules (`seo_extractor/extractors/base.py`)

Each `<meta>`/`<link>` is assigned to **at most one** family, in this order — which is what prevents
cross-contamination:

1. `<meta property="og:*">` → Open Graph
2. `<meta name|property="twitter:*">` → Twitter
3. `<meta name="description">` → Meta description (`itemprop=description` never matches; `og:`/`twitter:description` were consumed above)
4. `<meta name="robots|googlebot|bingbot">` → Meta robots
5. `<link>` with rel token `canonical` → Canonical; rel token `alternate` **and** an `hreflang` attribute → Hreflang (`amphtml` / feeds match nothing)

`<title>` = every `<title>` inside `<head>`; `<h1>` = every `<h1>`. `<meta name="og:title">` (wrong attribute)
is not treated as Open Graph. Elements inside `<noscript>` / `<template>` are ignored.

## Parsability rules (`seo_extractor/validators.py`, every rule commented there)

| Parameter | Unparsable when |
|---|---|
| Title | none in `<head>` (explicit placeholder row), empty, or several with different text (identical repeats → warning) |
| Meta description | no `content`, empty, or exact repeat of an earlier description tag. Length ≠ 50–160 → warning |
| Meta robots | unknown directive, malformed value (`max-snippet:x`), bad `unavailable_after` date, contradictions (`index`+`noindex`, `follow`+`nofollow`, `all`+`noindex`, …). Accepted: the 13 directives in the brief plus `all` and `none` |
| Canonical | no/empty `href`, relative or protocol-relative URL (also warned), non-http(s), invalid syntax, or several with different hrefs |
| Open Graph | property not lowercase `og:` + a name from the ogp.me namespace (plus `og:updated_time/see_also/rich_attachment/ttl`), no/empty content, non-absolute URL in a URL-typed property |
| Twitter | no/empty content, `twitter:card` not in `summary, summary_large_image, app, player` |
| Hreflang | invalid BCP‑47 code (`en_US`, `english`), no/empty/relative href. Flags (warnings): no self-referencing entry; same code → different URLs; **no return link** from another crawled page |
| H1 | none (placeholder row), outside `<body>`, empty text. Multiple / duplicate `<h1>` → warning |

A missing meta description, canonical, robots, OG, Twitter or hreflang simply has no columns for that page
(empty cells) — only title and H1 emit an explicit "missing" row.

## Project layout

```
seo_tool.py            dialog (no args) / command line (with args)
seo_extractor/         engine: fetcher, parser, extractors/, validators, url_source, rate_limiter,
                       output_writer, excel_report, journal, main (CLI + execute())
tests/                 python -m unittest discover -s tests -v
sample_urls.txt        example input
```

## Notes

- Per-domain 2–5 s pacing means ~1,000 WAF-mode URLs on one domain take roughly an hour — use **Resume**
  if you stop and restart.
- The rate-limit proof in `crawl_summary.json` covers the requests made in the current session; URLs
  restored from the journal by Resume are not re-requested.

