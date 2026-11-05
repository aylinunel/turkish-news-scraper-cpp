import asyncio
import contextlib
import io
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backfill
import news
from test_backfill import page
from test_backfill_regressions import index, urls
from test_news import RSS, SOURCE


class Response:
    def __init__(self, url, body=b"body", status=200, headers=None, wait=None):
        self.url, self.body, self.status = url, body, status
        self.headers, self.wait, self.content = headers or {}, wait, self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise news.aiohttp.ClientResponseError(None, (), status=self.status)

    async def iter_chunked(self, size):
        if self.wait:
            await self.wait()
        yield self.body


class ParsingCompatibility(unittest.TestCase):
    def test_microdata_and_plain_body_container(self):
        body = b'''<article itemscope itemtype="https://schema.org/NewsArticle">
          <meta itemprop="headline" content="Microdata headline">
          <time itemprop="datePublished" datetime="2026-09-02T10:00:00+03:00"></time>
          <div itemprop="articleBody">An article without paragraph tags.</div></article>'''
        article = news.parse_article(body, SOURCE, SOURCE['url'] + 'story')
        self.assertEqual(article.title, 'Microdata headline')
        self.assertEqual(article.published_at, '2026-09-02T07:00:00+00:00')
        self.assertEqual(article.text, 'An article without paragraph tags.')

    def test_qualified_schema_and_specific_body_excludes_related_content(self):
        body = b'''<script type="application/ld+json">{"@type":"https://schema.org/ReviewNewsArticle",
          "headline":"Headline","datePublished":"2026-09-02"}</script><article>
          <div class="article-body"><p>The actual article has enough words to extract correctly.</p></div>
          <p>Related stories should never be included in the extracted body.</p></article>'''
        self.assertEqual(news.parse_article(body, SOURCE, SOURCE['url']).text,
                         'The actual article has enough words to extract correctly.')

    def test_image_links_and_short_numeric_urls(self):
        body = b'''<a href="/haber/12345"><img alt="A short headline"></a>
          <a href="/haber/67890" title="Another short headline"><h2></h2></a>
          <a href="/etiket/12345"><img alt="An excluded tag page"></a>'''
        self.assertEqual(news.page_candidates(body, SOURCE['url'], 10),
                         ['https://example.com/haber/12345', 'https://example.com/haber/67890'])


class EfficiencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = SimpleNamespace(db=Path(self.tmp.name)/'db.sqlite3', concurrency=4,
            per_site_concurrency=2, delay=0, limit=1, full_text=False, site_timeout=2,
            start=date(2026, 9, 1), end=date(2026, 9, 3), max_sitemaps=100, max_pages=100,
            status=Path(self.tmp.name)/'status.json', timeout=2, interval=0)

    def collector(self):
        collector = news.Collector(self.args)
        self.addCleanup(collector.db.close)
        return collector

    async def test_enrichment_timeout_preserves_parsed_feed(self):
        self.args.full_text = True
        self.args.site_timeout = 0.02
        collector = self.collector()
        async def fetch(session, url, cache=False):
            if cache:
                return RSS, url
            await asyncio.sleep(1)
        with patch.object(collector, 'fetch', fetch), contextlib.redirect_stdout(io.StringIO()):
            status = await collector.collect(None, dict(SOURCE))
        self.assertEqual(status['status'], 'partial')
        self.assertEqual(status['new'], 1)
        self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 1)

    async def test_same_host_downloads_overlap_but_starts_are_spaced(self):
        self.args.delay = 0.02
        collector, started, both = self.collector(), [], asyncio.Event()
        class Session:
            def get(self, url, headers):
                started.append(time.monotonic())
                if len(started) == 2:
                    both.set()
                return Response(url, wait=both.wait)
        await asyncio.wait_for(asyncio.gather(
            collector.fetch(Session(), 'https://example.com/one'),
            collector.fetch(Session(), 'https://example.com/two')), 0.5)
        self.assertGreaterEqual(started[1] - started[0], 0.019)

    async def test_retry_backoff_releases_global_slot(self):
        self.args.concurrency = 1
        collector, first = self.collector(), asyncio.Event()
        attempts = 0
        class Session:
            def get(self, url, headers):
                nonlocal attempts
                if 'example.com' in url:
                    attempts += 1
                    first.set()
                    return Response(url, status=503 if attempts == 1 else 200,
                                    headers={'Retry-After': '1'})
                return Response(url)
        retry = asyncio.create_task(collector.fetch(Session(), 'https://example.com/slow'))
        try:
            await first.wait()
            await asyncio.wait_for(collector.fetch(Session(), 'https://other.com/fast'), 0.2)
        finally:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)

    async def test_feed_sample_skips_category_requests_and_all_feeds_opt_in(self):
        collector, hits = self.collector(), []
        async def fetch(session, url, cache=False):
            hits.append(url)
            return RSS, url
        source = dict(SOURCE, feeds=['https://example.com/rss', 'https://example.com/category'])
        with patch.object(collector, 'fetch', fetch), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual((await collector.collect(None, source))['new'], 1)
            self.assertEqual(hits, [source['feeds'][0]])
            hits.clear()
            self.args.all_feeds = True
            self.assertEqual((await collector.collect(None, source))['new'], 0)
            self.assertEqual(set(hits), set(source['feeds']))

    async def test_working_homepage_route_avoids_guessed_feeds_and_bad_page_isolation(self):
        collector, hits = self.collector(), []
        home = b'''<a href="/bad-page-with-a-long-enough-name">A malformed article with enough text</a>
          <a href="/good-page-with-a-long-enough-name">A valid article with enough text</a>'''
        async def fetch(session, url, cache=False):
            hits.append(url)
            if url == SOURCE['url']:
                return home, url
            if '/bad-page-' in url:
                return b'broken', url
            if '/good-page-' in url:
                return page('Good', '2026-09-02'), url
            return None
        original = news.parse_article
        def parse(body, source, url):
            if body == b'broken':
                raise ValueError('bad page')
            return original(body, source, url)
        source = dict(SOURCE)
        with patch.object(collector, 'fetch', fetch), patch('news.parse_article', parse), contextlib.redirect_stdout(io.StringIO()):
            status = await collector.collect(None, source)
            self.assertEqual((status['status'], status['new']), ('ok', 1))
            self.assertTrue(any('bad page' in e['error'] for e in status['errors']))
            hits.clear()
            await collector.collect(None, source)
            self.assertFalse(any('/rss' in url or '/feed' in url for url in hits))

    async def test_cache_limit_and_conditional_body_reuse(self):
        collector = self.collector()
        collector.cache_limit = 6
        headers_seen = []
        class Session:
            def get(self, url, headers):
                headers_seen.append(headers)
                return Response(url, body=b'abcd', status=304 if headers else 200,
                                headers={'ETag': 'v1'})
        session = Session()
        one, two = 'https://example.com/one', 'https://example.com/two'
        await collector.fetch(session, one, cache=True)
        self.assertEqual((await collector.fetch(session, one, cache=True))[0], b'abcd')
        self.assertEqual(headers_seen[-1], {'If-None-Match': 'v1'})
        await collector.fetch(session, two, cache=True)
        self.assertNotIn(one, collector.cache)
        self.assertEqual(collector.cache_size, 4)

    async def test_oversized_refresh_removes_stale_cached_body(self):
        collector = self.collector()
        collector.cache_limit = 6
        calls = []
        class Session:
            def get(self, url, headers):
                calls.append(headers)
                return Response(url, body=b'1234' if len(calls) == 1 else b'123456789',
                                headers={'ETag': 'v1'})
        url = 'https://example.com/feed'
        session = Session()
        for _ in range(3):
            await collector.fetch(session, url, cache=True)
        self.assertEqual(calls, [{}, {'If-None-Match': 'v1'}, {}])
        self.assertEqual(collector.cache_size, 0)

    async def test_non_article_homepage_links_still_try_sitemaps(self):
        collector = self.collector()
        base = 'https://example.com'
        maps = {'/': b'<a href="/a-category-with-a-long-enough-name">Category with a long enough label</a>',
                '/a-category-with-a-long-enough-name': b'<h1>A category</h1>',
                '/sitemap_news.xml': urls(('/good-story', '2026-09-02')),
                '/good-story': page('Sitemap story', '2026-09-02')}
        async def fetch(session, url, cache=False):
            body = maps.get(url.removeprefix(base))
            return (body, url) if body else None
        with patch.object(collector, 'fetch', fetch), contextlib.redirect_stdout(io.StringIO()):
            status = await collector.collect(None, dict(SOURCE))
        self.assertEqual((status['status'], status['new']), ('ok', 1))
        self.assertTrue(collector.seen(SOURCE['slug'], base + '/good-story'))

    async def test_dead_feeds_have_a_short_cooldown_and_recover(self):
        collector, hits = self.collector(), []
        source = dict(SOURCE, feeds=['https://example.com/dead', 'https://example.com/rss'])
        async def fetch(session, url, cache=False):
            hits.append(url)
            if url.endswith('/dead'):
                raise news.aiohttp.ClientResponseError(SimpleNamespace(real_url=url), (), status=404)
            return RSS, url
        with patch.object(collector, 'fetch', fetch), contextlib.redirect_stdout(io.StringIO()):
            await collector.collect(None, source)
            await collector.collect(None, source)
            self.assertEqual(hits.count(source['feeds'][0]), 1)
            collector.endpoint_cooldowns[source['feeds'][0]] = 0
            await collector.collect(None, source)
            self.assertEqual(hits.count(source['feeds'][0]), 2)

    async def test_bounded_work_is_cancelled_on_early_exit(self):
        started, cancelled = [], []
        async def job(value):
            started.append(value)
            if value == 0:
                await asyncio.sleep(0)
                return value
            try:
                await asyncio.Future()
            finally:
                cancelled.append(value)
        async with contextlib.aclosing(news.bounded_map(job, range(1000), 2)) as results:
            async for result in results:
                self.assertEqual(result, 0)
                break
        self.assertEqual(started, [0, 1])
        self.assertEqual(cancelled, [1])

    async def test_page_budget_drains_slow_inflight_story_and_robots_is_shared(self):
        self.args.max_pages = 1
        base, hits = 'https://example.com', []
        source = dict(SOURCE, url=base)
        maps = {'/robots.txt': f'Sitemap: {base}/sitemap.xml'.encode(),
                '/sitemap.xml': urls(('/slow', '2026-09-02'), ('/second', '2026-09-02')),
                '/slow': page('Slow', '2026-09-02')}
        async def fetch(collector, session, url, cache=False):
            path = url.removeprefix(base)
            hits.append(path)
            if path == '/slow':
                await asyncio.sleep(0.02)
            return (maps[path], url) if path in maps else None
        with patch.object(backfill.Backfill, 'fetch', fetch), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await backfill.Backfill(self.args).run([source]), 3)
        status = json.loads(self.args.status.read_text())['sources'][0]
        self.assertEqual(status['new'], 1)
        self.assertEqual(hits.count('/robots.txt'), 1)
        self.assertNotIn('/second', hits)


if __name__ == '__main__':
    unittest.main()
