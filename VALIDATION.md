# Validation record

## Extraction update from newspaper, newspaper4k, fundus and news-fetch (27 October 2026)

`python -m unittest -v`: all **40 tests passed** on Python 3.13, including five
new tests in `test_extract.py` (gravity body among Turkish boilerplate,
JSON-LD authors/keywords/image/section, the fundus NTV selector, title suffixes,
Turkish dates, tracking parameters, non-article paths and RSS authors,
categories and images). `git diff --check` passed.

### Real saved pages

fundus ships saved Habertürk, NTV and Anadolu Ajansı pages with expected
outputs (`tests/resources/parser/test_data/tr`). This package's extractor was
run against all five and compared paragraph by paragraph:

| Page | Previous body (chars) | Updated body | Expected paragraphs found | Title | Byline |
| --- | ---: | ---: | ---: | --- | --- |
| AnadoluAjansi 2025-04-05 | 0 | 2,307 | 10 / 10 | match | match |
| AnadoluAjansi 2026-04-20 | 187 | 4,914 | 13 / 13 | match | match |
| Haberturk 2024-07-03 | 6,915* | 5,544 | 43 / 43 | match without "- Güncel haberler" | publisher only |
| NTVTR 2024-04-30 | 3,712 | 3,345 | 1 / 1 | match | publisher only |
| NTVTR 2025-11-10 | 4,646 | 4,481 | 15 / 15 | match | publisher only |

\* The previous version kept Habertürk's JSON-LD body, which is one flattened
string with double-escaped entities (`&Ccedil;orum`) and lost apostrophes. It
matched 1 of 43 paragraphs exactly.

With site-specific selectors disabled (the pages served from an unknown host),
the generic container and gravity steps still found all 82 expected paragraphs.
The extra paragraphs were subheadlines that fundus reports separately. On the
Anadolu Ajansı page dated 5 April 2025, this package reads `05.04.2025` as
5 April, matching the fixture's filename. fundus's expected output says 4 May.
"Publisher only" means the page names only the publisher (e.g. `NTV`) as
author. Those names are dropped deliberately, because `source_name` already
carries them.

newspaper4k's 30 saved pages (mostly non-Turkish) raised no exceptions. No page
lost body text compared with the previous version. Eight gained a body where
none was extracted before. Non-Turkish pages without known containers can
still return empty bodies, because gravity scoring uses Turkish stopwords.

Publisher sites were not reachable from this environment (connections failed),
so no live round was repeated for this update. The 14 October live figures
below still describe feed and fallback coverage.

## Efficiency and compatibility update (14 October 2026)

`python -m unittest -v`: all **35 tests passed** on Python 3.12. Syntax
compilation and `git diff --check` passed. The 13 added tests cover concurrent
downloads with spaced starts, retry slot release, cancellation, exact page
budgets with slow in-flight requests, shared robots requests, optional full
category scans, malformed-page isolation, sitemap fallback after unusable
homepage links, bounded cache eviction and stale-body removal, feed cooldown
recovery, microdata, qualified Schema.org types, specific article body
containers, and image links with short numeric article URLs.

### Live before/after check

Both runs used fresh databases and the same settings against all 53 publishers:

```bash
python news.py --limit 1 --timeout 5 --site-timeout 45 \
  --db work/check.sqlite3 --status work/check.json > work/check.jsonl
```

| Measure | Original at `3d52595` | Updated |
| --- | ---: | ---: |
| Publishers returning stories | 49 / 53 | 51 / 53 |
| Complete round | 14.09 s | 9.04 s |
| Median publisher time | 8.06 s | 3.71 s |
| New stored stories | 49 | 51 |

The updated run finished at **2026-10-14T20:21:22.335032+00:00** (see the live report's
timestamp for the exact collection time). Haberler and Sondakika were recovered
by HTML/sitemap fallback despite failed RSS endpoints. Instacp and
GazetelerdeBugün still returned proxy HTTP 403 errors in this environment.
These errors do not establish global publisher availability. Anadolu returned
stories in the final run; intermediate runs encountered a timeout and redirect
loop, demonstrating why a single successful check does not guarantee coverage.

These are sequential live observations, not a controlled network benchmark.
Publisher latency and content vary. The new default stops requesting category
feeds once the sample is filled; `--all-feeds` retains the earlier broader scan.
It is inappropriate to interpret the timing difference as a speedup while
reading every category or as evidence of complete historical coverage.

A separate fresh-database `--full-text --limit 1` check returned bodies from
Hürriyet (1,192 characters), T24 (1,705), and Oksijen (419). This checks those
three pages, not complete body extraction across every publisher.

### Controlled scheduling benchmark

A three-run fixture benchmark injected a 20 ms wait per HTTP request, used zero
request spacing, fresh databases, and the same article content in both versions:

| Scenario | Original median | Updated median | Requests | Stored stories |
| --- | ---: | ---: | ---: | ---: |
| One-story sample from eight feeds | 0.1733 s | 0.0213 s | 8 → 1 | 1 → 1 |
| Eight sitemap parts and 48 article pages | 1.3500 s | 0.7096 s | 64 → 64 | 48 → 48 |

Backfill was approximately **1.90× faster** in this scheduling benchmark while
collecting identical stories. Real speed depends on request latency and the
publisher's required spacing; the benchmark does not predict real archive run
duration. Daily quota runs deliberately keep article checks sequential.

## Earlier validation

Checked at 2026-10-12T19:39:18.831580+00:00 (UTC).

## Automated checks

`python -m unittest -v`: all 5 tests passed.

Covered RSS/Atom, invalid dates, UTF-8 Turkish text, article/feed/sitemap discovery, URL validation, 53-source/117-feed configuration, HTTP 304 reuse, restart persistence, independent publisher failures, and partial-feed preservation on timeout.

The original XLSX loaded through `--sources` was verified to match the complete JSON configuration: 53 publishers and 117 valid feed URLs. A prose note in the workbook's RSS address column is ignored as a non-URL.

## Live run

Command:

```bash
python news.py --limit 2 --timeout 8 --site-timeout 120 --db final_check.sqlite3 --status live_source_status.json > live_sample.jsonl
```

- Attempted all 53 publishers.
- 50 returned recognizable news; 100 unique stories were stored.
- Slowest publisher completed in 111.40 seconds with 16 concurrent requests allowed across publishers.
- Unsupported or inaccessible responses: Anadolu Ajansı, Instacp, GazetelerdeBugün.
- All three unsuccessful publishers produced gateway errors or failed endpoint attempts. This does not establish that those sites are globally unavailable. Their per-request diagnostics are preserved in `live_source_status.json`.
- HTML fallback was exercised on dha, memurlar, nefes, oksijen, sondakika, t24, vatan.
- A second BirGün run against the same database found stories and emitted zero duplicates.

## Full-text checks

A separate run using `--full-text --limit 1 --only hurriyet,t24,oksijen` returned bodies from all three publishers:

| Publisher | Extracted body characters |
| --- | ---: |
| Hürriyet | 981 |
| T24 | 7232 |
| Oksijen | 1678 |

This verifies those examples, not full-text extraction for every publisher. Body extraction can be incomplete or empty on other layouts, paywalls, or dynamically rendered pages. No access-control bypass is implemented.

The dated status report is included for reproducibility. Raw article bodies and local test databases are omitted from this code package. The first user run therefore starts with an empty deduplication database. RSS endpoint availability and page layouts can change; this package cannot guarantee permanent coverage or capture every article between polling rounds.


## Historical backfill debugging (12 October 2026)

`python -m unittest -v`: all 19 tests passed on Python 3.12. The nine new
regression tests cover mixed dated/undated archives, indexes deeper than three
levels, missing dates and later edits, event dates in URLs, page-limited resume
including rejected pages, sitemap-cap diagnostics, gzip archives, robots-denied
sitemaps, unavailable robots, blocked articles not consuming page budgets, transient request retries, and malformed article
isolation. The original ten tests also passed. Syntax compilation and
`git diff --check` passed.

Live bounded run:

```bash
python backfill.py --from 2026-09-01 --to 2026-10-10 \
  --only sozcu,sabah,t24,ntv,diken --max-pages 3 --site-timeout 90 --timeout 10 \
  --db work/live.sqlite3 --status work/live_status.json > work/live.jsonl
```

- Sözcü: 6 sitemaps, 12,068 candidates, 3 retained historical stories.
- NTV: 113 sitemaps, 10,008 candidates, 3 retained historical stories.
- T24: its index was accessible, but the session proxy denied
  `media-cdn.t24.com.tr/robots.txt`; that origin was not crawled.
- Diken: 19 sitemaps read before the 90-second test budget expired.
- Sabah: 29 sitemaps and 18,425 candidates. Initial limited attempts checked
  older pages edited recently; candidate ordering was improved to prioritize
  date hints within the window without using URL dates to exclude stories.
  A subsequent bounded run retained one historical article using `sitemap+html`.

A second Sözcü run using the same database added three different historical
stories and skipped the first batch. All stored sample publication dates were
inside the requested Türkiye calendar window. The first sample uses sitemap
metadata for Sözcü/NTV; Sabah validates article-page parsing against a real site.
These are deliberately capped checks, not a complete 40-day dataset or a
verification of all 53 publishers. Each limited run correctly returned exit 3
and a partial status instead of claiming completion.

GitHub Actions API reads now succeed through the GitHub connector (no existing
workflow runs at inspection time). A manual workflow is supplied to run tests,
collect on a GitHub runner, upload records/status/database even on failure, and
resume a prior artifact. Hosted collection has not been dispatched or verified
here. Network policy, publisher access, and available sitemap archives remain
external constraints; code cannot make a blocked destination accessible.

A separate Sözcü check for `--from 2026-09-01 --to 2026-09-01 --max-pages 2`
retained two stories published on 1 September, confirming actual access to the
start of the requested historical period, rather than only October stories.
Workflow YAML also parsed successfully locally.
