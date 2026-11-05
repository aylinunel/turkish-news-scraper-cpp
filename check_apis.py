#!/usr/bin/env python3
"""Compare optional news APIs with the registry using a few bounded requests.

Credentials are read from NEWSAPI_KEY or WORLDNEWSAPI_KEY, never URL parameters.
No articles are inserted into the scraper database or added to its registry.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

import news

BASES = {"newsapi": "https://newsapi.org", "worldnewsapi": "https://api.worldnewsapi.com"}
KEY_VARS = {"newsapi": "NEWSAPI_KEY", "worldnewsapi": "WORLDNEWSAPI_KEY"}


def domain(url):
    return (urlsplit(news.canonical(url)).hostname or "").removeprefix("www.")


def match_source(url, sources):
    # Prefer the most specific registered domain, retaining existing slugs.
    candidates = [s for s in sources if news.same_site(url, s["url"])]
    return max(candidates, key=lambda s: len(domain(s["url"])), default=None)


def summarize(payload, provider, sources, catalog=False):
    """Report observed domains, never confuse a page sample with full coverage."""
    field = "sources" if catalog else "articles" if provider == "newsapi" else "news"
    records = payload.get(field)
    if not isinstance(records, list):
        raise ValueError(f"Expected a {field} array in the API response")
    matched, candidates, seen, rejected, bodies = {}, {}, set(), 0, 0
    for record in records:
        if not isinstance(record, dict):
            rejected += 1
            continue
        try:
            url = news.canonical(record.get("url") or "")
            host = domain(url)
        except (ValueError, TypeError):
            rejected += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        source = match_source(url, sources)
        if source:
            slug = source["slug"]
            matched[slug] = matched.get(slug, 0) + 1
        else:
            name = record.get("name") if catalog else (
                record.get("source", {}).get("name") if isinstance(record.get("source"), dict) else None)
            candidate = candidates.setdefault(host, {"domain": host, "url": f"https://{host}/",
                                                     "name": name or host, "observed_records": 0})
            candidate["observed_records"] += 1
        if not catalog and provider == "worldnewsapi" and isinstance(record.get("text"), str) and record["text"].strip():
            bodies += 1
    total = payload.get("available") if provider == "worldnewsapi" else payload.get("totalResults")
    return {"returned_records": len(records), "unique_valid_urls": len(seen),
            "invalid_records": rejected, "provider_total_results": total,
            "matched_publishers": dict(sorted(matched.items())),
            "unobserved_registry_slugs": sorted(s["slug"] for s in sources if s["slug"] not in matched),
            "new_publisher_candidates": [candidates[k] for k in sorted(candidates)],
            "articles_with_api_text": bodies,
            "coverage_note": ("Top-headlines catalog only, not the complete article-search index. Candidates need review." if catalog else
                              "One response page only. Unobserved publishers may still be indexed; candidates need review.")}


def queries(provider, sources, start, end):
    if provider == "newsapi":
        return [
            ("turkish_headline_source_catalog", "/v2/top-headlines/sources", {"country": "tr"}, True),
            ("turkish_headlines", "/v2/top-headlines", {"country": "tr", "pageSize": 100}, False),
            ("registered_publisher_sample", "/v2/everything", {
                "domains": ",".join(dict.fromkeys(domain(s["url"]) for s in sources)),
                "from": start, "to": end, "sortBy": "publishedAt", "pageSize": 100, "page": 1}, False)]
    dates = {"earliest-publish-date": start.replace("T", " "), "latest-publish-date": end.replace("T", " "),
             "sort": "publish-time", "sort-direction": "DESC", "number": 100, "offset": 0}
    return [
        ("turkish_article_sample", "/search-news", dict(dates, **{"source-country": "tr", "language": "tr"}), False),
        ("registered_publisher_sample", "/search-news", dict(dates, **{
            "news-sources": ",".join(s["url"] for s in sources), "language": "tr"}), False)]


def redact(value, key):
    return str(value).replace(key, "[redacted]") if key else str(value)


async def probe(session, provider, key, label, path, params, catalog, sources):
    started = time.monotonic()
    result = {"query": label, "endpoint": path, "parameters": params}
    try:
        # Never forward credentials through redirects to a different service.
        async with session.get(BASES[provider] + path, params=params,
                               headers={"X-Api-Key": key}, allow_redirects=False) as response:
            result["http_status"] = response.status
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 10 * 1024 * 1024:
                    raise ValueError("API response exceeds 10 MiB")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if response.status != 200 or payload.get("status") == "error":
                result["error"] = {"code": payload.get("code"), "message": redact(payload.get("message", "API rejected request"), key)}
            else:
                result.update(summarize(payload, provider, sources, catalog))
            if provider == "worldnewsapi":
                result["quota"] = {name: response.headers.get(name) for name in
                    ("X-API-Quota-Request", "X-API-Quota-Used", "X-API-Quota-Left")}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        result["error"] = {"code": type(exc).__name__, "message": redact(exc, key)}
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


async def run(args, sources, key):
    results = []
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=args.timeout),
                                    headers={"User-Agent": news.USER_AGENT}) as session:
        for query in queries(args.provider, sources, args.start, args.end):
            if results:
                # One in-flight request and at least one second between calls.
                await asyncio.sleep(1)
            result = await probe(session, args.provider, key, *query, sources)
            results.append(result)
            code = result.get("error", {}).get("code")
            if result.get("http_status") in {401, 402, 403, 429} or code in {
                    "apiKeyInvalid", "apiKeyDisabled", "apiKeyExhausted", "rateLimited", "ClientHttpProxyError"}:
                break
    report = {"provider": args.provider, "checked_at": news.now(), "registry_publishers": len(sources),
              "requests_attempted": len(results), "queries": results,
              "scope": "Bounded source/coverage probe; no article ingestion, automatic pagination, or registry changes."}
    # The final serialized output also guards against a provider echoing a key.
    output = redact(json.dumps(report, ensure_ascii=False, indent=2), key) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(output, encoding="utf-8")
        temporary.replace(args.output)
    else:
        print(output, end="")
    return 3 if any("error" in r for r in results) else 0 if any(r.get("unique_valid_urls") for r in results) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=BASES, required=True)
    parser.add_argument("--sources", type=Path, default=Path(__file__).with_name("sources.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    # A 25-hour lag keeps the default Everything query compatible with the
    # NewsAPI development tier's delayed results; these are UTC timestamps.
    end = datetime.now(timezone.utc) - timedelta(hours=25)
    parser.add_argument("--from", dest="start", default=(end - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"))
    parser.add_argument("--to", dest="end", default=end.strftime("%Y-%m-%dT%H:%M:%S"))
    args = parser.parse_args()
    try:
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        dates = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (args.start, args.end)]
        dates = [d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc) for d in dates]
        if dates[0] > dates[1]:
            raise ValueError("--to precedes --from")
        # Both APIs accept UTC dates; normalize offsets rather than silently
        # allowing inconsistent windows between the provider queries.
        args.start, args.end = (d.strftime("%Y-%m-%dT%H:%M:%S") for d in dates)
        sources = news.load_sources(args.sources)
        key = os.environ.get(KEY_VARS[args.provider], "").strip()
        if not key:
            raise ValueError(f"Set {KEY_VARS[args.provider]} in your environment")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        return asyncio.run(run(args, sources, key))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
