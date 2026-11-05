import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urljoin

from aiohttp import web

import news

SOURCE = {"name": "Test Gazete", "slug": "test", "url": "https://example.com/", "feeds": []}
RSS = b'''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><item><title>First story</title><link>https://example.com/story?utm_source=x</link>
<pubDate>Mon, 12 Oct 2026 12:00:00 +0300</pubDate><description>&lt;p&gt;A summary&lt;/p&gt;</description>
<content:encoded><![CDATA[<p>Body text</p>]]></content:encoded></item>
<item><title>Offsite</title><link>https://other.com/story</link></item></channel></rss>'''
ARTICLE = b'''<html><head><meta property="og:type" content="article">
<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","headline":"A headline",
"datePublished":"2026-10-12T12:00:00+03:00","articleBody":"The complete body"}]}</script>
</head><body><h1>A headline</h1></body></html>'''


class ParsingTests(unittest.TestCase):
    def test_rss_atom_and_date(self):
        article, = news.parse_feed(RSS, SOURCE, SOURCE["url"])
        self.assertEqual(article.url, "https://example.com/story")
        self.assertEqual(article.published_at, "2026-10-12T09:00:00+00:00")
        self.assertEqual((article.summary, article.text), ("A summary", "Body text"))
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title>
        <link rel="self" href="https://example.com/feed"/><link href="/atom-story"/>
        <published>2026-10-12T10:00:00Z</published></entry></feed>'''
        self.assertEqual(news.parse_feed(atom, SOURCE, SOURCE["url"])[0].url, "https://example.com/atom-story")
        self.assertEqual(news.parse_feed(b'<html><body>Error</body></html>', SOURCE, SOURCE['url']), [])
        self.assertIsNone(news.date("not-a-date"))
        self.assertEqual(news.date("2026-10-12T12:00:00"), "2026-10-12T12:00:00")

    def test_article_and_discovery(self):
        article = news.parse_article(ARTICLE, SOURCE, "https://example.com/story")
        self.assertEqual(article.text, "The complete body")
        self.assertEqual(article.title, "A headline")
        turkish = '<meta property="og:type" content="article"><h1>Türkiye için özgürlük</h1>'.encode()
        self.assertEqual(news.parse_article(turkish, SOURCE, SOURCE['url']).title, 'Türkiye için özgürlük')
        self.assertIsNone(news.parse_article(b'<h1>Category page</h1>', SOURCE, SOURCE['url']))
        page = b'''<link type="application/rss+xml" href="/rss.xml">
        <a href="/story-with-a-long-enough-slug">A headline with enough words to be a story</a>
        <a href="https://other.com/long-story">Another long headline outside the publisher</a>'''
        self.assertEqual(news.discover_feeds(page, SOURCE['url']), ['https://example.com/rss.xml'])
        self.assertEqual(news.page_candidates(page, SOURCE['url'], 5), ['https://example.com/story-with-a-long-enough-slug'])
        maps = b'''<sitemapindex><sitemap><loc>https://example.com/archive.xml</loc></sitemap>
        <sitemap><loc>https://example.com/news.xml</loc></sitemap></sitemapindex>'''
        self.assertEqual(news.sitemap_links(maps, SOURCE['url'], 5)[1][0], 'https://example.com/news.xml')

    def test_urls_and_complete_registry(self):
        self.assertEqual(news.canonical('/a?q=x&utm_medium=z#frag', SOURCE['url']), 'https://example.com/a?q=x')
        self.assertEqual(news.canonical('https://haberglobal.com.tr/rss'), 'https://haberglobal.com/rss')
        for url in ('javascript:alert(1)', 'http://localhost/a', 'http://127.0.0.1/a', 'https://user@example.com/a'):
            with self.assertRaises(ValueError):
                news.canonical(url)
        sources = news.load_sources(Path(__file__).with_name('sources.json'))
        self.assertEqual(len(sources), 53)
        self.assertEqual(sum(len(s["feeds"]) for s in sources), 122)  # 118 + 4 audit-verified feeds (28 Oct 2026)
        self.assertIn('instacp', {s['slug'] for s in sources})
        self.assertIn('tele1', {s['slug'] for s in sources})
        self.assertEqual(news.normalize('KAYIT DEFTERİ'), 'kayit defteri')


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_feed_preserves_already_fetched_stories(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(db=Path(directory)/'db.sqlite3', concurrency=2,
                                   site_timeout=0.02, limit=2, full_text=False)
            collector = news.Collector(args)
            async def fetch(session, url, cache=True):
                if url.endswith('/slow'):
                    await asyncio.sleep(1)
                return RSS, url
            source = dict(SOURCE, feeds=['https://example.com/rss', 'https://example.com/slow'])
            with patch.object(collector, 'fetch', side_effect=fetch), contextlib.redirect_stdout(io.StringIO()):
                result = await collector.collect(None, source)
            self.assertEqual((result['status'], result['found'], result['new']), ('partial', 1, 1))
            self.assertTrue(collector.seen('test', 'https://example.com/story'))
            collector.db.close()

    async def test_conditional_polling_restart_and_source_failure_isolation(self):
        calls = []
        async def feed(request):
            calls.append(request.headers.get('If-None-Match'))
            if calls[-1] == 'v1':
                return web.Response(status=304)
            body = RSS.replace(b'https://example.com', base.encode())
            return web.Response(body=body, headers={'ETag': 'v1'}, content_type='application/rss+xml')
        app = web.Application()
        app.router.add_get('/rss.xml', feed)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        source = dict(SOURCE, url=base + '/', feeds=[base + '/rss.xml'])
        broken = {'name': 'Broken', 'slug': 'broken', 'url': 'http://127.0.0.1:1/', 'feeds': []}
        try:
            with tempfile.TemporaryDirectory() as directory:
                args = SimpleNamespace(db=Path(directory)/'db.sqlite3', status=Path(directory)/'status.json',
                                       concurrency=2, delay=0, timeout=1, site_timeout=5,
                                       limit=2, full_text=False, interval=0)
                collector = news.Collector(args)
                # Loopback exists only in this controlled test; production URL checks remain strict.
                with patch('news.canonical', side_effect=lambda u, b='': urljoin(b, u)):
                    async with news.aiohttp.ClientSession(timeout=news.aiohttp.ClientTimeout(total=1)) as session:
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            first = await collector.collect(session, source)
                            second = await collector.collect(session, source)
                        self.assertEqual((first['new'], second['new']), (1, 0))
                        self.assertEqual(calls, [None, 'v1'])
                        self.assertEqual(len(output.getvalue().splitlines()), 1)
                        collector.db.close()
                        restarted = news.Collector(args)
                        self.assertTrue(restarted.seen('test', json.loads(output.getvalue())['url']))
                        restarted.db.close()
                    final = news.Collector(args)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(await final.run([source, broken]), 0)
                    statuses = json.loads(args.status.read_text())['sources']
                    self.assertEqual([s['source'] for s in statuses], ['test', 'broken'])
                    self.assertEqual(statuses[0]['status'], 'ok')
                    self.assertIn(statuses[1]['status'], {'no_articles', 'timeout'})
                    self.assertTrue(statuses[1]['errors'])
        finally:
            await runner.cleanup()


if __name__ == '__main__':
    unittest.main()
