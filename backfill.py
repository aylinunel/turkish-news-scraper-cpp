#!/usr/bin/env python3
"""Backfill a past date window from each publisher's own sitemaps.

news.py is a live poller: RSS feeds hold only the latest items, so it cannot
reach last month. Publishers do, however, list their archive in sitemaps that
they publish for crawlers. This script walks those sitemaps for a date window,
keeps the URLs that fall inside it, and reads each article page for its title
and publication date. Results go to the same SQLite schema and JSON Lines
output as news.py, with method "sitemap" (title taken from a Google News
sitemap) or "sitemap+html" (title read from the article page).

    python backfill.py --from 2026-09-01 --to 2026-10-10 > backfill.jsonl

Only publisher-offered sitemaps and article pages are requested, robots.txt is
honoured, and requests to one publisher are spaced (--delay). The run is
resumable: URLs already in the database are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import gzip
import io
from collections import deque
import logging
import re
import sys
import time
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp
from lxml import etree

import news

LOG = logging.getLogger("backfill")
COMMON_MAPS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap/index.xml", "/sitemaps/sitemap.xml",
               "/sitemap-index.xml", "/news-sitemap.xml", "/sitemap_news.xml",
               # Google News maps used by fundus for Habertürk and NTV.
               "/sitemap_google_news.xml", "/sitemaps/news-sitemap.xml")
# Child sitemaps that never list news articles.
SKIP_MAP = re.compile(r"(?:image|video|gallery|galeri|foto|tags?(?![a-z])|etiket|categor|kategori|author|"
                      r"sitemappages|static|eczane|namaz|hava-durumu|astroloji|burc|sozluk|bilmece|"
                      r"sondeprem|sans-oyun|ruya|webstories|amp-stories)", re.I)
# Article paths outside general news. Used by --news-only to spend the page
# budget on agenda, politics, economy and world coverage.
NON_NEWS_PATH = re.compile(r"/(?:spor|sport|futbol|basketbol|voleybol|magazin|yasam|life|saglik|saglikli|"
                           r"teknoloji|tekno|otomobil|oto|astroloji|burc|galeri|foto|video|yemek|tarif|kadin|"
                           r"seyahat|turizm|emlak|moda|unluler|dizi|tv-rehberi|ruya|bilmece|sans-oyun|"
                           r"hava-durumu|namaz|eczane|ilan|resmi-ilan|webtv|podcast)(?:/|-|$)", re.I)
MONTHS_TR = {"ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
             "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12}


def month_span(start: Date, end: Date) -> set[tuple[int, int]]:
    out, d = set(), start.replace(day=1)
    while d <= end:
        out.add((d.year, d.month))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def url_dates(url: str) -> tuple[set[tuple[int, int]], set[Date]]:
    """Months and days a URL names, e.g. ?year=2026&month=9, /2026/09/15/, 2026-09, 20260915."""
    s = news.normalize(url)
    months, days = set(), set()
    m = re.search(r"year=(\d{4}).*?month=(\d{1,2})|month=(\d{1,2}).*?year=(\d{4})", s)
    if m:
        y, mo = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        months.add((int(y), int(mo)))
    for y, mo, d in re.findall(r"(?<!\d)(20\d\d)[-/_.]?(0[1-9]|1[0-2])[-/_.]?(0[1-9]|[12]\d|3[01])(?!\d)", s):
        try:
            days.add(Date(int(y), int(mo), int(d)))
            months.add((int(y), int(mo)))
        except ValueError:
            pass
    for y, mo in re.findall(r"(?<!\d)(20\d\d)[-/_](0?[1-9]|1[0-2])(?![\d])", s):
        months.add((int(y), int(mo)))
    for name, mo in MONTHS_TR.items():
        for y in re.findall(rf"{name}[-_/]?(20\d\d)|(?<!\d)(20\d\d)[-_/]?{name}", s):
            year = y[0] or y[1]
            months.add((int(year), mo))
    return months, days


def parse_day(value: str | None) -> Date | None:
    stamp = news.date(value) if value else None
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if dt.tzinfo:  # publisher timestamps: judge the day in Turkey's time zone
        dt = dt.astimezone(timezone(timedelta(hours=3)))
    return dt.date()


def parse_sitemap(body: bytes, base: str) -> tuple[str, list[dict]]:
    """Return ("index"|"urlset"|"", entries) with loc, lastmod and Google News fields."""
    if body.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            body = stream.read(40 * 1024 * 1024 + 1)
        if len(body) > 40 * 1024 * 1024:
            raise ValueError("Decompressed sitemap exceeds 40 MiB")
    root = etree.fromstring(body, etree.XMLParser(resolve_entities=False, no_network=True, recover=True,
                                                  huge_tree=True))
    if root is None:
        return "", []
    kind = etree.QName(root).localname
    if kind not in {"sitemapindex", "urlset"}:
        return "", []
    tag = "sitemap" if kind == "sitemapindex" else "url"
    entries = []
    for node in root.xpath(f'./*[local-name()="{tag}"]'):
        def text(xpath):
            found = node.xpath(xpath)
            return news.clean("".join(found[0].itertext())) if found else ""
        try:
            loc = news.canonical(text('./*[local-name()="loc"]'), base)
        except ValueError:
            continue
        if not news.same_site(loc, base):
            continue
        entries.append({"loc": loc, "lastmod": text('./*[local-name()="lastmod"]'),
                        "title": text('.//*[local-name()="news"]/*[local-name()="title"]'),
                        "published": text('.//*[local-name()="news"]/*[local-name()="publication_date"]')})
    return ("index" if kind == "sitemapindex" else "urlset"), entries


def robots_sitemaps(body: bytes) -> list[str]:
    return re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", body.decode("utf-8", "replace"))


class Backfill(news.Collector):
    def __init__(self, args):
        super().__init__(args)
        self.max_bytes = 40 * 1024 * 1024  # monthly archive sitemaps can be large
        self.start, self.end = args.start, args.end
        self.months = month_span(self.start, self.end)
        self.request_delays = {}
        self.db.execute("""CREATE TABLE IF NOT EXISTS backfill_checked (
            source TEXT, url TEXT, start TEXT, end TEXT,
            PRIMARY KEY(source, url, start, end))""")

    def checked(self, source, url):
        return self.db.execute(
            "SELECT 1 FROM backfill_checked WHERE source=? AND url=? AND start=? AND end=?",
            (source, url, str(self.start), str(self.end))).fetchone() is not None

    def mark_checked(self, source, url):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO backfill_checked VALUES (?, ?, ?, ?)",
                            (source, url, str(self.start), str(self.end)))

    async def run(self, sources):
        code = await super().run(sources)
        statuses = json.loads(self.args.status.read_text())["sources"]
        return 3 if any(s["status"] in {"partial", "timeout"} for s in statuses) else code

    def in_window(self, day: Date | None) -> bool:
        return day is not None and self.start <= day <= self.end

    def stale_child(self, entry: dict) -> bool:
        """A child sitemap last modified before the window (minus --lastmod-slack days) cannot
        list a story published inside it. Only used when its URL names no date."""
        lastmod = parse_day(entry["lastmod"])
        slack = timedelta(days=getattr(self.args, "lastmod_slack", 3))
        return lastmod is not None and lastmod < self.start - slack

    def child_relevant(self, entry: dict) -> bool | None:
        """True: names a month in the window. False: names only other months or is
        clearly not news. None: no date information."""
        if SKIP_MAP.search(urlsplit(entry["loc"]).path + "?" + urlsplit(entry["loc"]).query):
            return False
        months, days = url_dates(entry["loc"])
        if days:
            return any(self.in_window(d) for d in days)
        if months:
            return bool(months & self.months)
        return None

    async def collect(self, session, source):
        started = time.monotonic()
        status = {"source": source["slug"], "name": source["name"], "url": source["url"],
                  "checked_at": news.now(), "status": "no_articles", "window": [str(self.start), str(self.end)],
                  "sitemaps": 0, "candidates": 0, "found": 0, "new": 0, "skipped_robots": 0,
                  "out_of_window": 0, "unparsed": 0, "section_skipped": 0, "sitemaps_pruned": 0,
                  "methods": [], "errors": []}

        policies = {}
        policy_locks = {}
        incomplete = False

        roots = []

        async def load_policy(url):
            nonlocal incomplete
            origin = urlsplit(url).scheme + "://" + urlsplit(url).netloc
            if origin not in policies:
                try:
                    policy = RobotFileParser()
                    try:
                        result = await self.fetch(session, origin + "/robots.txt")
                    except aiohttp.ClientResponseError as exc:
                        if exc.status not in {404, 410}:
                            raise
                        result = None
                    policy.parse(result[0].decode("utf-8", "replace").splitlines() if result else [])
                    policies[origin] = policy
                    spacing = policy.crawl_delay(news.USER_AGENT) or 0
                    rate = policy.request_rate(news.USER_AGENT)
                    if rate and rate.requests:
                        spacing = max(spacing, rate.seconds / rate.requests)
                    host = urlsplit(origin).hostname.removeprefix("www.")
                    self.request_delays[host] = max(self.request_delays.get(host, 0), spacing)
                    if result:
                        roots.extend(robots_sitemaps(result[0]))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    policies[origin] = None
                    incomplete = True
                    status["errors"].append({"url": origin + "/robots.txt",
                                             "error": f"{type(exc).__name__}: {exc}"[:300]})
            return policies[origin]

        async def policy_for(url):
            origin = urlsplit(url).scheme + "://" + urlsplit(url).netloc
            # Concurrent sitemap/article work shares a single robots request.
            async with policy_locks.setdefault(origin, asyncio.Lock()):
                return await load_policy(url)

        async def get(url, required=True):
            nonlocal incomplete
            try:
                url = news.canonical(url)
                if not news.same_site(url, source["url"]):
                    raise ValueError("Cross-publisher request")
                policy = await policy_for(url)
                if policy is None:
                    return None
                if not policy.can_fetch(news.USER_AGENT, url):
                    status["skipped_robots"] += 1
                    if required:
                        incomplete = True
                    return None
                return await self.fetch(session, url)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, etree.Error) as exc:
                status["errors"].append({"url": url, "error": f"{type(exc).__name__}: {exc}"[:300]})
                if required:
                    incomplete = True
                return None

        try:
            async with asyncio.timeout(self.args.site_timeout):
                # Load robots and its advertised roots before guessing common paths.
                # A robots failure blocks that origin, rather than silently allowing it.
                await policy_for(source["url"])
                # Sitemaps verified for this publisher (registry "sitemaps") are read like advertised ones.
                roots += source.get("sitemaps", [])
                advertised = set(roots)
                roots += [urljoin(source["url"], p) for p in COMMON_MAPS]
                queue, visited, candidates = deque(), set(), {}
                for url in dict.fromkeys(roots):
                    try:
                        queue.append((news.canonical(url), False, url in advertised))
                    except ValueError:
                        pass
                async def read_map(item):
                    nonlocal incomplete
                    url, scoped, required = item
                    result = await get(url, required=required)
                    if not result:
                        return item, None, []
                    try:
                        kind, entries = parse_sitemap(*result)
                    except (etree.Error, ValueError, OSError, EOFError) as exc:
                        status["errors"].append({"url": url, "error": str(exc)[:300]})
                        incomplete |= required
                        return item, None, []
                    return item, kind, entries

                while queue and len(visited) < self.args.max_sitemaps:
                    batch = []
                    while queue and len(batch) < self.site_concurrency and len(visited) < self.args.max_sitemaps:
                        item = queue.popleft()
                        url = item[0]
                        if url in visited or not news.same_site(url, source["url"]):
                            continue
                        visited.add(url)
                        batch.append(item)
                    async with news.aclosing(news.bounded_map(read_map, batch, self.site_concurrency)) as results:
                        async for (url, scoped, required), kind, entries in results:
                            if kind is None:
                                continue
                            if not kind:
                                incomplete |= required
                                if required:
                                    status["errors"].append({"url": url, "error": "Advertised sitemap could not be read as a sitemap index or URL set"})
                                continue
                            status["sitemaps"] += 1
                            if kind == "index":
                                children, stale = [], []
                                prune = getattr(self.args, "prune_sitemaps", True)
                                for entry in entries:
                                    rel = self.child_relevant(entry)
                                    if rel is False:
                                        continue
                                    child = (entry["loc"], scoped or rel is True, True)
                                    (stale if prune and rel is None and self.stale_child(entry) else children).append(child)
                                # An index whose every child looks stale is more likely to carry
                                # wrong lastmod values than to be empty: visit them all then.
                                if children:
                                    status["sitemaps_pruned"] += len(stale)
                                else:
                                    children = stale
                                # Prioritize dated archives, but always visit undated children too.
                                children.sort(key=lambda item: not item[1])
                                queue.extend(children)
                                continue
                            for entry in entries:
                                published = parse_day(entry["published"])
                                lastmod = parse_day(entry["lastmod"])
                                # lastmod is an edit date, never a publication date or upper bound.
                                # URLs may name an event date: only publication metadata decides inclusion.
                                keep = self.in_window(published) if published else not (lastmod and lastmod < self.start)
                                if keep:
                                    previous = candidates.get(entry["loc"])
                                    if previous is None or (entry["published"] and not previous["published"]):
                                        candidates[entry["loc"]] = entry
                                else:
                                    status["out_of_window"] += 1
                if any(item[0] not in visited for item in queue):
                    incomplete = True
                    status["errors"].append({"error": "--max-sitemaps reached; raise it to traverse remaining archives"})
                # Publisher feeds hold only recent items, but for an outlet without a news sitemap they
                # are the only route; they go through the same robots check as everything else.
                for feed in source.get("feeds", []):
                    result = await get(feed, required=False)
                    if not result:
                        continue
                    try:
                        items = news.parse_feed(result[0], source, feed)
                    except (etree.Error, ValueError) as exc:
                        status["errors"].append({"url": feed, "error": f"{type(exc).__name__}: {exc}"[:300]})
                        continue
                    status["feeds"] = status.get("feeds", 0) + 1
                    for item in items:
                        if not self.in_window(parse_day(item.published_at)):
                            status["out_of_window"] += 1
                            continue
                        entry = candidates.setdefault(item.url, {"loc": item.url, "lastmod": "", "title": item.title,
                                                                 "published": item.published_at, "method": "rss"})
                        entry["summary"] = entry.get("summary") or item.summary  # sitemaps carry no lede
                if getattr(self.args, "news_only", False):
                    kept = {u: e for u, e in candidates.items() if not NON_NEWS_PATH.search(urlsplit(u).path)}
                    status["section_skipped"] = len(candidates) - len(kept)
                    candidates = kept
                status["candidates"] = len(candidates)
                per_day = getattr(self.args, "per_day", 0) or 0
                days = [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]
                counts = dict.fromkeys(days, 0)
                if per_day:
                    for (stamp,) in self.db.execute("SELECT published_at FROM articles WHERE source=?", (source["slug"],)):
                        day = parse_day(stamp)
                        if day in counts:
                            counts[day] += 1

                # Compute date hints once per candidate, rather than reparsing
                # publication dates and URL dates in every sort and quota check.
                hints = {}
                for url, entry in candidates.items():
                    published = parse_day(entry["published"])
                    if self.in_window(published):
                        hints[url] = (0, published)
                        continue
                    url_day = next((d for d in sorted(url_dates(url)[1]) if self.in_window(d)), None)
                    lastmod = parse_day(entry["lastmod"])
                    hints[url] = ((1, url_day) if url_day else
                                  (2, lastmod) if self.in_window(lastmod) else (3, None))
                ordered = sorted(candidates.items(), key=lambda item: hints[item[0]][0])
                if per_day:
                    # Round-robin across days so a capped run covers the whole window evenly
                    # instead of filling the first days and timing out on the rest.
                    rank, by_day = {}, {}
                    for url, entry in ordered:
                        day = hints[url][1]
                        rank[url] = (by_day.get(day, 0), 0 if day else 1)
                        by_day[day] = by_day.get(day, 0) + 1
                    ordered.sort(key=lambda item: (rank[item[0]][1], rank[item[0]][0], hints[item[0]][0]))

                saved = {row[0] for row in self.db.execute("SELECT url FROM articles WHERE source=?", (source["slug"],))}
                checked = {row[0] for row in self.db.execute(
                    "SELECT url FROM backfill_checked WHERE source=? AND start=? AND end=?",
                    (source["slug"], str(self.start), str(self.end)))}

                attempted = 0
                page_limit = False
                # Serialize candidates sharing a date hint; different days can overlap.
                day_locks = {day: asyncio.Lock() for day in days}
                full_days = 0

                async def check_page(item):
                    url, entry = item
                    if per_day:
                        if full_days == len(days):
                            return
                        day = hints[url][1]
                        if day is not None and url not in saved and url not in checked:
                            # A request may fail or reveal a different publication day.
                            # Wait for provisional work before deciding the quota is full.
                            async with day_locks[day]:
                                if counts[day] >= per_day:
                                    return
                                return await check_one(url, entry)
                    return await check_one(url, entry)

                async def check_one(url, entry):
                    nonlocal attempted, incomplete, page_limit, full_days
                    if url in saved:
                        status["found"] += 1
                        return
                    if url in checked or page_limit:
                        return
                    # Robots also applies when metadata means no page request is needed.
                    policy = await policy_for(url)
                    if policy is None:
                        return
                    if not policy.can_fetch(news.USER_AGENT, url):
                        status["skipped_robots"] += 1
                        return
                    if attempted >= self.args.max_pages:
                        incomplete = True
                        if not page_limit:
                            status["errors"].append({"error": "--max-pages reached; rerun to resume"})
                        page_limit = True
                        return
                    attempted += 1
                    if entry["title"] and entry["published"] and not self.args.full_text:
                        article = news.Article(source["slug"], source["name"], url, news.plain(entry["title"]),
                                               news.date(entry["published"]), summary=entry.get("summary", ""),
                                               method=entry.get("method", "sitemap"))
                    else:
                        result = await get(url)
                        try:
                            article = news.parse_article(result[0], source, url) if result else None
                        except (ValueError, TypeError, etree.Error) as exc:
                            status["errors"].append({"url": url, "error": f"{type(exc).__name__}: {exc}"[:300]})
                            article = None
                        if article:
                            article.method = "sitemap+html"
                            if not article.published_at and entry["published"]:
                                article.published_at = news.date(entry["published"])
                    if not article:
                        status["unparsed"] += 1
                        incomplete = True
                        return
                    article_day = parse_day(article.published_at)
                    if not article_day:
                        status["unparsed"] += 1
                        incomplete = True
                        return
                    if not self.in_window(article_day):
                        status["out_of_window"] += 1
                        self.mark_checked(source["slug"], url)
                        return
                    if per_day and counts[article_day] >= per_day:
                        return
                    status["found"] += 1
                    if article.method not in status["methods"]:
                        status["methods"].append(article.method)
                    status["new"] += self.save(article)
                    # Saved articles already have durable restart deduplication.
                    # Avoid a second SQLite commit for every accepted story.
                    if per_day:
                        counts[article_day] += 1
                        if counts[article_day] == per_day:
                            full_days += 1
                # Quota runs consume candidates in order to avoid overfetching
                # filled days. Full backfills overlap bounded article requests.
                if per_day:
                    full_days = sum(counts[d] >= per_day for d in days)
                workers = self.site_concurrency
                def remaining():
                    for item in ordered:
                        if page_limit:
                            return
                        yield item
                async with news.aclosing(news.bounded_map(check_page, remaining(), workers)) as results:
                    async for _ in results:
                        if per_day and full_days == len(days):
                            break
                        # On a page limit, drain requests already inside the budget;
                        # remaining() stops scheduling more candidates.
                if per_day:
                    status["per_day"] = per_day
                    status["days_full"] = sum(counts[d] >= per_day for d in days)
                    status["days_empty"] = sum(counts[d] == 0 for d in days)
                    if all(counts[d] >= per_day for d in days):
                        status["found"] = max(status["found"], sum(counts.values()))
                if status["found"]:
                    status["status"] = "ok"
                if incomplete and not (per_day and all(counts[d] >= per_day for d in days)):
                    status["status"] = "partial"
        except TimeoutError:
            status["status"] = "partial" if status["found"] else "timeout"
            status["errors"].append({"error": "Publisher time budget exceeded; rerun to resume"})
        status["seconds"] = round(time.monotonic() - started, 1)
        LOG.info("%-18s %-11s sitemaps=%d candidates=%d found=%d new=%d", source["slug"], status["status"],
                 status["sitemaps"], status["candidates"], status["found"], status["new"])
        return status


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start", required=True, type=Date.fromisoformat, help="First day, YYYY-MM-DD")
    p.add_argument("--to", dest="end", required=True, type=Date.fromisoformat, help="Last day, YYYY-MM-DD")
    p.add_argument("--sources", type=Path, default=Path(__file__).with_name("sources.json"))
    p.add_argument("--db", type=Path, default=Path("backfill.sqlite3"))
    p.add_argument("--status", type=Path, default=Path("backfill_status.json"))
    p.add_argument("--only", help="Comma-separated source slugs")
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--per-site-concurrency", type=int, default=2, help="Concurrent requests per publisher; --delay and robots pacing still apply")
    p.add_argument("--cache-mb", type=int, default=32, help="Maximum HTTP body cache in MiB (0 disables it)")
    p.add_argument("--timeout", type=float, default=20)
    p.add_argument("--site-timeout", type=float, default=14400, help="Budget per publisher in seconds (default 4 h; rerun to resume)")
    p.add_argument("--delay", type=float, default=0.6, help="Minimum spacing between requests to one publisher")
    p.add_argument("--max-sitemaps", type=int, default=120)
    p.add_argument("--max-undated", type=int, default=6, help="Deprecated; all undated children are now traversed")
    p.add_argument("--max-pages", type=int, default=30000, help="Article pages per publisher")
    p.add_argument("--lastmod-slack", type=int, default=3,
                   help="Days of slack when skipping child sitemaps last modified before the window "
                        "(story edit dates never exclude stories)")
    p.add_argument("--no-prune-sitemaps", dest="prune_sitemaps", action="store_false",
                   help="Visit every child sitemap, even ones last modified before the window")
    p.add_argument("--full-text", action="store_true", help="Read article pages even when the sitemap has titles")
    p.add_argument("--per-day", type=int, default=0,
                   help="Keep at most N stories per publisher per day, spread evenly over the window (0 = all)")
    p.add_argument("--news-only", action="store_true",
                   help="Skip sport, magazine, lifestyle and other non-news sections")
    args = p.parse_args()
    if args.end < args.start:
        p.error("--to is before --from")
    if args.delay < 0 or args.cache_mb < 0 or min(args.concurrency, args.per_site_concurrency, args.timeout, args.site_timeout,
                             args.max_sitemaps, args.max_pages, args.max_undated) <= 0 or args.lastmod_slack < 0:
        p.error("Limits and timeouts must be positive; delay and slack cannot be negative")
    args.limit, args.interval = args.max_pages, 0
    sources = news.load_sources(args.sources)
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {s["slug"] for s in sources}
        if unknown:
            p.error("Unknown sources: " + ", ".join(sorted(unknown)))
        sources = [s for s in sources if s["slug"] in wanted]
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        return asyncio.run(Backfill(args).run(sources))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
