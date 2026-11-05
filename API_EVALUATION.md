# News API feasibility check

Checked 14 October 2026 against the official documentation and this repository's
53-publisher registry. These are commercial API services with free tiers;
open-source SDKs do not make the hosted data or quotas unrestricted.

## Recommendation

Evaluate **World News API as a supplementary batch collector**. Its documented
Turkish-language filter and article text could replace many individual page
downloads for publishers it actually covers. Keep direct RSS for timely polling
and retain the scraper for publishers and archive dates missing from the API.
NewsAPI is useful to test headline discovery, but its free development tier
does not fit frequent, current, full-text collection.

No hosted API has been made the default, no publisher has been automatically
added, and no API credential is committed. A source advertised by an API is not
proof of complete coverage or reliable historical publication dates.

## Provider comparison

| Feature | World News API | NewsAPI.org |
| --- | --- | --- |
| Turkish coverage | Advertises 51 Turkey-based sources and over 2,144 items daily | Documents Turkey headlines using `country=tr`; actual publisher coverage needs a key-based check |
| Turkish article search | Documents `language=tr` and `source-country=tr` | `/everything` does not list `tr` as a supported language value; test registered domains without that language filter |
| Article bodies | `/search-news` documents article text; availability and extraction quality need testing | Search `content` is truncated to 200 characters; full bodies require page retrieval |
| Batch size | Up to 100 articles, with `number` and `offset` pagination | Up to 100 articles, with `pageSize` and `page` pagination |
| Free allowance | 50 points/day; search costs 1 point plus 0.01 per result | 100 requests/day, development/testing only |
| Free freshness/history | One month of history; verify actual indexing latency | 24-hour delay and one month of history |
| Longer historical access | Paid Reporter plan starts at $39/month; archived data advertised from January 2022 | Business plan listed at $449/month, with search up to five years back |
| Free concurrent requests | One request at a time | Quota is the main documented restriction considered here |

Sources: [World News API Turkey coverage](https://worldnewsapi.com/docs/news-sources/turkey-news-api/),
[search endpoint](https://worldnewsapi.com/docs/search-news/),
[pricing](https://worldnewsapi.com/pricing/),
[quota rules](https://worldnewsapi.com/docs/quotas-and-rate-limiting/),
[NewsAPI Everything](https://newsapi.org/docs/endpoints/everything),
[NewsAPI Turkey example](https://newsapi.org/s/turkey-news-api), and
[NewsAPI pricing](https://newsapi.org/pricing).

The advertised number of World News API sources is **51, not 51 additional
publishers**. The registry already has 53. Their overlap must be measured by
domain before proposing additions. NewsAPI's
[source catalog](https://newsapi.org/docs/endpoints/sources) describes only the
publishers supported by its top-headlines endpoint, not the complete
`/everything` index. An empty catalog or an absent source in a 100-result sample
cannot establish that article search does not cover that publisher.

## What could make collection faster

1. Query multiple publishers in one API call, requesting 100 items per page.
   World News API supports comma-separated `news-sources` URLs; NewsAPI supports
   comma-separated `domains`. Avoid one API call per publisher.
2. Retrieve a bounded time window, paginate every needed page, and deduplicate
   with the existing canonical `(source, URL)` keys. A single page sorted newest
   first can be dominated by large publishers; it is not balanced coverage of
   all 53 sources.
3. Persist a collection cursor only after all pages in the window finish.
   Re-query an overlap window to catch late indexing. A cursor based solely on
   publication time can miss articles indexed later.
4. Retain provider provenance and distinguish a NewsAPI excerpt from full text.
   Import World News API text when usable; fetch publisher pages only for
   missing text. Audit a sample against publisher timestamps and page content
   before relying on API fields for a historical analysis.
5. Use direct RSS frequently and the supplementary API less often, fitting its
   quota. Respect API errors and reported remaining points rather than silently
   treating an exhausted quota as an empty result.

The multiple-source World News API query is documented in its
[media guide](https://worldnewsapi.com/docs/media/). These are design
recommendations, not a measured API speedup.

### Quota implications

Using the advertised 2,144 daily Turkish items as an illustration, a full
100-item-page sweep would need about **22 requests and 43.44 search points**
(`22 + 0.01 × 2144`). That nearly uses the free 50-point allowance once a day.
Actual country/language results, duplicates, empty pages, and repeated polling
change the cost. Polling every three minutes is **480 calls/day even before
pagination**, which exceeds both free tiers. A NewsAPI domain batch would reduce
request count but would still return delayed excerpts on its free tier.

The repository's earlier example backfill, **1 September–10 October 2026**,
extends beyond both services' one-month free history window as of this check.
A paid plan or the existing publisher sitemap backfill is needed for the start
of that window. No subscription or paid overage has been enabled.

## Reproducible coverage probe

`check_apis.py` uses the current registry without modifying it. It makes at most
**three NewsAPI requests** or **two World News API requests**, serially and at
least one second apart. It reports response time, observed registry publishers,
new publisher candidates, text availability, and World News API quota headers.
It stops on authentication, quota, access, or rate-limit failures. It does not
ingest articles, paginate automatically, retry paid calls, or forward credentials
through redirects. The default article-search window ends 25 hours ago to be
compatible with NewsAPI's delayed development results.

On Bash, supply the appropriate key without including it in shell history:

```bash
read -r -s -p 'NewsAPI key: ' NEWSAPI_KEY
export NEWSAPI_KEY
python check_apis.py --provider newsapi --output work/newsapi-coverage.json
unset NEWSAPI_KEY
```

For World News API, use its **separate service-issued key**:

```bash
read -r -s -p 'World News API key: ' WORLDNEWSAPI_KEY
export WORLDNEWSAPI_KEY
python check_apis.py --provider worldnewsapi --output work/worldnewsapi-coverage.json
unset WORLDNEWSAPI_KEY
```

Both services support header authentication; the checker uses `X-Api-Key` rather
than placing credentials in URLs. See [NewsAPI authentication](https://newsapi.org/docs/authentication)
and [World News API authentication](https://worldnewsapi.com/docs/authentication/).

### Validation and live-check limits

The checker has fixture tests for publisher matching, tracking-URL
deduplication, batch parameters, source-catalog limitations, text availability,
credential redaction, disabled redirects, and stopping on auth/quota errors.
These exercise local logic; they do not validate either provider's live data or
the account's plan.

The key was confirmed to belong to NewsAPI.org. At
**2026-10-14T21:21:52 UTC**, the checker attempted the Turkey source-catalog
request once, with the key supplied through masked input and kept only in
process memory and the authentication header. The environment proxy returned
HTTP 403 (`ClientHttpProxyError`) before provider authentication; the checker
stopped immediately. The key was not written to source files or the JSON report.

The enforced outbound policy excludes `newsapi.org` and `api.worldnewsapi.com`.
Therefore **the key's validity, its plan, live API speed, and additional indexed
publishers remain unverified**. World News API was not called because a key for
that service was not supplied. Allowing the API hosts in environment settings
and supplying the appropriate service-specific credentials enables the bounded
checks above. Public documentation was read through the web research tool.
