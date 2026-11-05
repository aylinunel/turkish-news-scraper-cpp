# Turkish News Scraper with C++ Acceleration

Based on [turkish-news-scraper](https://github.com/aylinunel/turkish-news-scraper). This version includes optional pybind11 acceleration and fixes for quota candidate loss and RSS enrichment timeouts. A prebuilt extension is included for Linux x86-64 / CPython 3.12; other systems use Python automatically and can build their own extension.

Scheduled collection is disabled in this repository; workflows can be run manually.

Small Python 3.11+ command-line collector for **every one of the 53 publishers** in the supplied workbook's **Kayıt Defteri** sheet. `sources.json` contains the complete registry and its 117 supplied RSS addresses, plus Anadolu Ajansı's general feed taken from fundus's publisher registry (118 in total). No sources are filtered by the workbook's previous yes/no availability labels.

Built using the [Ponytail coding workflow](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md): reuse existing libraries, keep one straightforward script, and test parsing and persistence. Ponytail is an agent instruction set, not a news scraping library; it is not a runtime dependency or an installed plugin here.

## Run

Clone the repository, then:

```bash
git clone https://github.com/aylinunel/turkish-news-scraper-cpp.git
cd turkish-news-scraper-cpp
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python news.py > news.jsonl
```

On Windows, activate with `.venv\Scripts\activate`.

Poll continuously, waiting three minutes after each round:

```bash
python news.py --interval 180 >> news.jsonl
```

Read your original workbook directly, including updated registry entries:

```bash
python news.py --sources "Turk_Haber_Analizi_Kaynak_Listesi(1).xlsx" --interval 180 >> news.jsonl
```

The workbook path is relative to your current directory. It is not included in this package because the extracted registry is sufficient to run it.

For RSS feeds, also request article pages for body text:

```bash
python news.py --full-text --limit 20 --interval 180 >> news.jsonl
```

Full-text retrieval runs **when a URL is first collected**. Use a separate database if you previously collected those URLs without enrichment:

```bash
python news.py --full-text --db news_full.sqlite3 > news_full.jsonl
```

Target specific publishers or increase the number of items per round:

```bash
python news.py --only hurriyet,sozcu,t24 --limit 50 > focused.jsonl
```

## Backfill a past window

`news.py` polls live feeds, which only hold the latest items. To collect a past window, `backfill.py` walks each publisher's own sitemaps (listed in its `robots.txt`, or at common sitemap paths), keeps URLs dated inside the window, and reads each article page for its title and publication date:

```bash
python backfill.py --from 2026-09-01 --to 2026-10-10 > backfill.jsonl
```

Restrict to specific publishers with `--only`, e.g. the 29 outlets used by Çapraz Manşet:

```bash
python backfill.py --from 2026-09-01 --to 2026-10-10 \
  --only ahaber,akit,aydinlik,birgun,cnnturk,cumhuriyet,diken,ensonhaber,gercekgundem,haber7,haberler,haberturk,halktv,hurriyet,karar,milligazete,milliyet,mynet,ntv,sabah,sozcu,star,t24,takvim,trthaber,turkiyegz,tv100,yenicag,yenisafak \
  > backfill.jsonl
```

* Records go to `backfill.sqlite3` (same schema as `news.sqlite3`) and to standard output as JSON Lines. `method` is `sitemap` when a Google News sitemap supplied the title, `sitemap+html` when it was read from the article page.
* `backfill_status.json` reports, per publisher, how many sitemaps were read, how many URLs were candidates, and how many were kept, skipped by `robots.txt`, or outside the window.
* The run is **resumable**: stop it any time and run the same command again with `>> backfill.jsonl`; stored URLs and pages already confirmed outside that date window are skipped. `--max-pages` limits unchecked candidates per run, so reruns advance to the next batch. Failed requests remain retryable. SQLite is the authoritative dataset.
* Day boundaries use Türkiye time (UTC+3), so a story published at 00:30 on 1 September is included.
* Coverage depends on each publisher's sitemaps. Monthly or daily archive sitemaps (e.g. Sözcü's `sitemapposts?year=2026&month=9`) are followed directly. Undated child sitemaps are traversed even when another map has already returned candidates. `lastmod` is an edit date: entries edited after the window and entries without dates are checked against article publication metadata. A publisher whose sitemaps list only recent stories cannot be fully backfilled this way. `ok` means the eligible sitemap traversal finished; it does not prove that the publisher exposes a complete archive.
* Expect a long run: large publishers publish several hundred stories a day, and requests to each publisher are spaced (`--delay`, default 0.6 s). Publishers run in parallel.

### Run on GitHub without a connected computer

Open **Actions → Test and backfill → Run workflow** after this change is merged (or select the fix branch if the workflow is already on the default branch). Enter the inclusive date range and optionally source slugs. Download **backfill-results** from the completed run for the JSON Lines, SQLite database, and per-source diagnostics. Tests run before collection.

For a continuation, enter the previous workflow run ID in **resume_run**, using the same dates and sources. The workflow restores its database and appends only new records. Artifacts are retained for 30 days. GitHub must allow Actions for this repository; a publisher can still reject GitHub's servers.

### Quick parallel backfill

**Actions → Test and backfill → Run workflow** with **mode = quick** splits the publishers across parallel runners (2 per job by default), keeps at most `per_day` stories per publisher per day (default 30), spreads them evenly over the whole window, and skips sport, magazine, lifestyle and other non-news sections (`--news-only`). A final job merges every part into the same **backfill-results** artifact the full workflow produces, so either workflow can resume the other via `resume_run`. Locally: `python backfill.py --from 2026-09-01 --to 2026-10-10 --news-only --per-day 30`. `backfill_status.json` reports `days_full` and `days_empty` per publisher.

Exit codes: `0` means the selected traversal finished and found records; `2` means no records were found; `3` means at least one publisher was incomplete. Limits, failures, and timeouts are reported explicitly. A partial Actions run fails visibly but still uploads its results. Increase `--max-sitemaps` if that limit was reached; rerunning alone cannot cross the same sitemap cap. `--max-undated` remains accepted for compatibility and no longer restricts discovery. Child sitemaps whose own `lastmod` is more than `--lastmod-slack` days (default 3) before the window, and whose URL names no date, are skipped (a file last changed before the window cannot list a story published in it); the count is reported as `sitemaps_pruned`. If every child of an index looks stale, all are visited. `--no-prune-sitemaps` turns this off. A story's own `lastmod` (an edit date) never excludes it. Quota runs (`--per-day`) fetch story pages with the per-site concurrency, reserving a day's slot before each request, so filled days are not overfetched.

Robots rules apply separately to each origin, including sitemap requests and publisher subdomains. An unavailable or forbidden `robots.txt` blocks that origin for the run; HTTP 404/410 means no robots file exists. Sitemap `.gz` archives are supported with a decompressed size limit.

## Output

* **Standard output:** only newly observed stories, one UTF-8 JSON object per line. Redirect to a file or pipe into another program.
* **Standard error:** per-publisher progress and round totals.
* **`news.sqlite3`:** persistent article records, used to avoid duplicates across rounds and restarts. It is the authoritative retained dataset. The package contains no prepopulated database, so your first run starts fresh.
* **`source_status.json`:** atomically replaced diagnostic report for every selected publisher on every round. `ok` means the publisher returned recognizable stories; `partial` means its time budget expired after returning some stories; `no_articles`, `timeout`, and `parse_error` distinguish unsuccessful attempts. Errors from failed feed candidates remain visible even if another candidate succeeds.

Each story includes `source`, `source_name`, `url`, `title`, `published_at`, `summary`, `text`, `method`, and `fetched_at`, plus:

| Field | Meaning |
| --- | --- |
| `authors` | Names from JSON-LD, author meta tags, RSS `dc:creator`, or Turkish bylines (`Muhabir:`, `Yazan:`). URLs and the publisher's own name are dropped. |
| `keywords` | JSON-LD keywords, `article:tag`, `news_keywords`/`keywords` meta, or RSS categories. |
| `image` | Lead image: JSON-LD image, `og:image`, `twitter:image`, or RSS media/enclosure. |
| `language`, `section` | Page language (`tr`) and section, e.g. `Gündem`, when declared. |
| `modified_at` | Declared last-modified time. Never used as `published_at`. |
| `content_source` | Where `text` came from: `rss`, `json-ld`, `publisher` (site-specific selector), `selector` (known body container), `gravity` (stopword scoring) or `article`. Empty when no body was found. |

Lists are empty and strings empty or `null` when a page declares nothing. Older records keep their original fields.

Query stored articles without additional dependencies:

```python
import json
import sqlite3

with sqlite3.connect("news.sqlite3") as db:
    articles = [json.loads(row[0]) for row in db.execute(
        "SELECT payload FROM articles ORDER BY fetched_at DESC"
    )]
```

## Collection behavior

1. Try the first workbook RSS address, then category feeds as needed to fill `--limit`. Use `--all-feeds` to read every configured feed before selecting the latest items across categories.
2. If none yields articles, discover feeds from the homepage. When no feeds are advertised, first test a homepage article before guessing common feed paths. Working feed and HTML routes are reused during later rounds of that process.
3. If feeds still fail, inspect homepage article links. If none returns recognizable stories, inspect a bounded number of sitemap entries. Only pages with article metadata are accepted. Category links no longer suppress sitemap fallback.
4. Remove common tracking parameters and deduplicate by publisher and URL. Different publishers carrying the same story remain separate records.

Requests run concurrently across publishers and within each publisher, with connection reuse, per-publisher request spacing, bounded response sizes, retry handling, and conditional requests using ETag/Last-Modified when provided. `--per-site-concurrency` defaults to 2; request starts still obey `--delay`, and backfill also respects robots crawl delays. Set it to 1 for serial requests. Retry waits release global request slots. An unchanged feed returning HTTP 304 reuses its cached body without emitting duplicate stories. The live body cache is bounded to 32 MiB (`--cache-mb`; 0 disables it), and endpoints returning 404/410 are retried after a five-minute cooldown during repeated polling. One unavailable publisher does not stop the other publishers. Successfully parsed feeds are retained if a later feed exceeds the publisher's time budget.

The default `--limit 20` selects up to 20 unique RSS items per publisher each round. The first feed is preferred for speed; `--all-feeds` restores the broader category scan and selection used by earlier versions. HTML fallback inspects up to `max(10, limit * 3)` homepage candidates and, if necessary, the same number of sitemap candidates, emitting at most `limit` new articles. Increase this limit for greater coverage; this is bounded live collection, not a complete historical archive or a guarantee to capture every article between polls. Sitemap entries use publisher ordering. Updates to an already collected URL are not re-emitted.

Historical backfill overlaps sitemap and article downloads with bounded tasks, computes date hints once, and loads restart deduplication records in bulk. Accepted stories need only one SQLite commit. `--per-day` keeps article checks sequential to preserve exact daily quotas and avoid extra page requests after days fill; sitemap requests still overlap. Sitemaps and pages remain bounded by the existing caps and publisher time budget.

The verified Haber Global domain migration is handled centrally: `haberglobal.com.tr` becomes `haberglobal.com`. The original workbook URLs remain unchanged. See the publisher's [migration announcement](https://haberglobal.com/bilim-teknoloji/haber-global-sitesinin-yeni-web-adresi-5-kasimdan-itibaren-haberglobalcom-487468).

`published_at` is the publisher's timestamp when it can be parsed. Zoned dates become UTC; dates without a zone retain that ambiguity. Missing or invalid dates remain `null`. `fetched_at` is the collection time, never a substitute publication date. The first run may collect older items still present in a feed; there is no automatic date cutoff.

Body extraction reads available RSS content and JSON-LD `articleBody`. When that is missing or shorter than 200 characters, the page is cleaned of navigation, comments, share bars, related-story boxes (`ilgili`/`benzer haberler`), advertising and "follow us on Google News" lines, then tried in order: site-specific selectors (Habertürk, NTV, Anadolu Ajansı, T24), common Turkish CMS body containers (`haber-detay`, `haber-icerik`, `news-content`, `articleBody`, …), newspaper-style gravity scoring by Turkish stopword density with a link-density penalty, and finally `<article>` paragraphs. The longest result wins. Titles lose publisher suffixes such as ` | Hürriyet` or ` - Son Dakika Haberleri`, and a publisher name inside the headline itself is kept. Publication dates come from JSON-LD, about 25 meta names, `itemprop`/`<time>`, visible Turkish dates (`27 Ekim 2026 14:35`, `27.10.2026 - 14:35`), and finally `/YYYY/MM/DD/` permalink segments. Qualified Schema.org types, publication `<time>` elements, image headline links, and shorter numeric story URLs are supported. Malformed article pages are reported individually so other stories can still be collected. Empty `text` means no body could be extracted; the title and summary remain usable. Paywalls, client-rendered articles, unavailable sites, and layout changes can limit coverage. No browser automation or access-control bypass is included.

## Techniques borrowed from other scrapers

These MIT-licensed projects were studied. Their techniques were reimplemented in `extract.py` on top of `lxml`, so none of them is a dependency:

* [newspaper](https://github.com/codelucas/newspaper) and [newspaper4k](https://github.com/AndyTheFactory/newspaper4k): gravity scoring of paragraphs with newspaper4k's Turkish stopword list, link-density filtering, boilerplate node removal, the publication-date meta list, byline splitting, title suffix cleaning and lead image selection.
* [fundus](https://github.com/flairNLP/fundus): the Habertürk, NTV and Anadolu Ajansı paragraph selectors, its parsing of JSON-LD authors (`name`, or `givenName` + `familyName`), Anadolu Ajansı's RSS feed and the Habertürk/NTV Google News sitemap paths (`/sitemap_google_news.xml`, `/sitemaps/news-sitemap.xml`), which are now tried by both live sitemap fallback and backfill.
* [news-fetch](https://github.com/santhoshse7en/news-fetch): extraction provenance (`content_source`) and URL dates as a last resort.

Additional campaign parameters (`_ga`, `igshid`, `msclkid`, `mc_cid`, …) are removed from URLs. Homepage link discovery skips corporate, legal and service pages (`iletisim`, `kunye`, `kvkk`, `hava-durumu`, `canli-yayin`, …).

Site-specific selectors follow the markup fundus documented. Publishers change layouts, so the generic container and gravity steps remain the fallback. Selectors were checked against fixtures in `test_extract.py`, not against live pages from this environment.

## Optional C++ acceleration

Python remains the default installation. To accelerate Unicode word counts and
Turkish stopword counts during article extraction, build the small `pybind11`
extension with your active CPython interpreter:

```bash
python -m pip install pybind11 setuptools
python setup_native.py build_ext --inplace
python -m unittest -v test_native
python benchmark_native.py
```

This requires CPython development headers and a C++11 compiler (GCC, Clang, or
MSVC). On a GCC system whose Python configuration selects an unavailable Clang,
use `CC=gcc CXX=g++ python setup_native.py build_ext --inplace`. Build from the
repository root. Rebuild after changing Python versions or platforms; compiled
binaries are local build artifacts and are ignored by git.

`extract.py` automatically uses `_news_native` when importable and otherwise
uses the original Python implementation. Turkish lowercasing stays in Python;
native token boundaries use the same CPython Unicode predicates as `re`'s
`\w+`. The extension retains the GIL because it uses CPython strings and sets.
It does not introduce parallel extraction workers.

The offline benchmark compares both implementations on identical synthetic
Turkish text and 60-paragraph HTML pages, reporting the median of five runs.
These results measure CPU extraction only. Publisher rate limits, network waits,
and live collection speed are unaffected. Native parity tests skip when the
extension is absent; fallback tests still run. To return to Python-only operation,
remove the generated `_news_native*.so` or `_news_native*.pyd` file.

## Optional API coverage check

[API_EVALUATION.md](API_EVALUATION.md) compares World News API and NewsAPI with the
53-publisher registry, including Turkish-language support, full-text limitations,
historical access, and request costs. World News API is a candidate supplementary
batch source; direct RSS and sitemap collection remain the defaults.

With `NEWSAPI_KEY` or `WORLDNEWSAPI_KEY` set securely in your environment:

```bash
python check_apis.py --provider newsapi --output work/newsapi-coverage.json
python check_apis.py --provider worldnewsapi --output work/worldnewsapi-coverage.json
```

The checker makes at most three or two requests respectively, identifies new
publisher candidates for review, and reports observed registry coverage. It does
not add publishers, ingest articles, or infer complete coverage from one page.
API hosts must be permitted by your execution environment's network policy.

## Verify

```bash
python -m unittest -v
```

The tests exercise RSS/Atom parsing, metadata and body extraction from Turkish markup (`test_extract.py`), Turkish Unicode, timezone handling, feed/article discovery, URL validation, all 53 registry entries, conditional polling, restart deduplication, independent source failures, and preservation of partially fetched feed data. `VALIDATION.md` records the live checks performed for this package.

The included `live_source_status.json` is a dated verification report, not a running feed. Run `news.py` to retrieve current data. No external scheduler or deployment is required.

## Verified routes and continuous collection (28 Oct 2026)

`sources.json` entries may carry `sitemaps`: publisher sitemaps verified by the 28 Oct 2026 access audit
(29 of 53 publishers returned usable metadata). `backfill.py` reads them like sitemaps advertised in
`robots.txt`, and also reads each publisher's `feeds` through the same robots check, so feed-only
publishers (Hürriyet, Milliyet, CNN Türk) are collected with their ledes. Feeds hold only recent
items: those publishers can be covered from the day collection starts, not backfilled.

- **Backfill, uncapped:** *Test and backfill* → mode `quick`, `per_day` `0`, `sources` blank (all 53),
  `per_job` `1`, `to_branch` on, `all_sections` on.
- **Recent stories:** `.github/workflows/collect-recent.yml` collects the last two days from every
  publisher every six hours and commits the stories new in each run to `backfill-data/data/recent/`. Scheduled runs start once
  the workflow is on the default branch.

Publishers that refuse `robots.txt` (HTTP 403 in the audit) are not retried or worked around; the access
request pack is the route for those.
