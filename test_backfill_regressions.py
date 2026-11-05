import asyncio
import contextlib
import gzip
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import backfill
from test_backfill import page

BASE = 'https://example.com'
SOURCE = dict(slug='example', name='Example', url=BASE, feeds=[])


def index(*paths):
    return ('<sitemapindex>' + ''.join(f'<sitemap><loc>{BASE}{p}</loc></sitemap>' for p in paths) + '</sitemapindex>').encode()


def urls(*entries):
    return ('<urlset>' + ''.join(f'<url><loc>{BASE}{p}</loc><lastmod>{modified}</lastmod></url>'
                               for p, modified in entries) + '</urlset>').encode()


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = SimpleNamespace(db=Path(self.tmp.name) / 'b.sqlite3', status=Path(self.tmp.name) / 'status.json',
                                    concurrency=2, delay=0, timeout=5, site_timeout=10, max_sitemaps=100,
                                    max_undated=1, max_pages=100, lastmod_slack=3, full_text=False,
                                    limit=100, interval=0, start=date(2026, 9, 1), end=date(2026, 10, 10))

    async def run_maps(self, maps, robots=None):
        hits = []
        async def fetch(_collector, _session, url, cache=False):
            path = url.removeprefix(BASE)
            hits.append(path)
            if path == '/robots.txt':
                value = robots if robots is not None else f'Sitemap: {BASE}/sitemap.xml\n'.encode()
            else:
                value = maps.get(path)
            if isinstance(value, Exception):
                raise value
            return (value, url) if value is not None else None
        output = io.StringIO()
        with patch.object(backfill.Backfill, 'fetch', fetch), contextlib.redirect_stdout(output):
            code = await backfill.Backfill(self.args).run([SOURCE])
        status = json.loads(self.args.status.read_text())['sources'][0]
        return code, [json.loads(line) for line in output.getvalue().splitlines()], status, hits

    async def test_pending_quota_does_not_discard_valid_candidates(self):
        self.args.per_day = 1
        self.args.per_site_concurrency = 2
        maps = {'/sitemap.xml': urls(('/outside', '2026-09-01'),
                                     ('/valid', '2026-09-01')),
                '/outside': page('Outside', '2026-08-01'),
                '/valid': page('Valid', '2026-09-01')}
        async def fetch(_collector, _session, url, cache=False):
            path = url.removeprefix(BASE)
            if path == '/outside':
                await asyncio.sleep(0.02)
            body = (f'Sitemap: {BASE}/sitemap.xml\n'.encode()
                    if path == '/robots.txt' else maps.get(path))
            return (body, url) if body is not None else None
        output = io.StringIO()
        with patch.object(backfill.Backfill, 'fetch', fetch), contextlib.redirect_stdout(output):
            code = await backfill.Backfill(self.args).run([SOURCE])
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)['title'] for line in output.getvalue().splitlines()], ['Valid'])

    async def test_all_undated_archives_with_candidates_and_deep_indexes(self):
        maps = {'/sitemap.xml': index('/2026-09.xml', '/archive.xml'),
                '/2026-09.xml': urls(('/first', '2026-09-02')),
                '/archive.xml': index('/level2.xml'), '/level2.xml': index('/level3.xml'),
                '/level3.xml': index('/level4.xml'), '/level4.xml': urls(('/updated', '2026-10-20'), ('/undated', '')),
                '/first': page('First', '2026-09-02'), '/updated': page('Updated', '2026-09-03'),
                '/undated': page('Undated', '2026-09-04')}
        code, rows, status, hits = await self.run_maps(maps)
        self.assertEqual(code, 0)
        self.assertEqual({r['title'] for r in rows}, {'First', 'Updated', 'Undated'})
        self.assertIn('/level4.xml', hits)
        self.assertEqual(status['status'], 'ok')

    async def test_resume_advances_past_saved_and_out_of_window_pages(self):
        maps = {'/sitemap.xml': urls(('/event-2026-08-01', '2026-10-20'), ('/outside', '2026-10-20'), ('/third', '')),
                '/event-2026-08-01': page('September story', '2026-09-05'),
                '/outside': page('August story', '2026-08-02'), '/third': page('Third', '2026-09-06')}
        self.args.max_pages = 1
        results = [await self.run_maps(maps) for _ in range(4)]
        self.assertEqual([r[0] for r in results], [3, 3, 0, 0])
        self.assertEqual([len(r[1]) for r in results], [1, 0, 1, 0])
        self.assertNotIn('/event-2026-08-01', results[1][3])
        self.assertNotIn('/outside', results[2][3])
        self.assertNotIn('/third', results[3][3])

    async def test_sitemap_limit_reports_partial(self):
        self.args.max_sitemaps = 1
        code, _, status, _ = await self.run_maps({'/sitemap.xml': index('/archive.xml')})
        self.assertEqual(code, 3)
        self.assertEqual(status['status'], 'partial')
        self.assertTrue(any('--max-sitemaps' in e['error'] for e in status['errors']))

    async def test_robots_disallows_sitemap_requests(self):
        code, rows, status, hits = await self.run_maps({'/secret.xml': urls(('/first', '2026-09-02'))},
            f'User-agent: *\nDisallow: /secret.xml\nSitemap: {BASE}/secret.xml\n'.encode())
        self.assertEqual(code, 3)
        self.assertFalse(rows)
        self.assertNotIn('/secret.xml', hits)
        self.assertEqual(status['skipped_robots'], 1)

    async def test_robots_blocked_articles_do_not_consume_page_budget(self):
        self.args.max_pages = 1
        maps = {'/sitemap.xml': urls(('/blocked', '2026-09-02'), ('/allowed', '2026-09-03')),
                '/allowed': page('Allowed', '2026-09-03')}
        code, rows, status, hits = await self.run_maps(maps,
            f'User-agent: *\nDisallow: /blocked\nSitemap: {BASE}/sitemap.xml\n'.encode())
        self.assertEqual(code, 0)
        self.assertEqual([r['title'] for r in rows], ['Allowed'])
        self.assertNotIn('/blocked', hits)
        self.assertEqual(status['skipped_robots'], 1)

    async def test_unavailable_robots_blocks_origin_once(self):
        code, rows, status, hits = await self.run_maps({'/sitemap.xml': urls(('/first', '2026-09-02'))},
                                                      aiohttp.ClientConnectionError('offline'))
        self.assertEqual(code, 3)
        self.assertFalse(rows)
        self.assertEqual(hits, ['/robots.txt'])
        self.assertEqual(len(status['errors']), 1)

    async def test_transient_article_failure_is_retried_on_resume(self):
        maps = {'/sitemap.xml': urls(('/first', '2026-09-02')), '/first': aiohttp.ClientConnectionError('offline')}
        self.assertEqual((await self.run_maps(maps))[0], 3)
        maps['/first'] = page('Recovered', '2026-09-02')
        code, rows, _, hits = await self.run_maps(maps)
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]['title'], 'Recovered')
        self.assertIn('/first', hits)

    async def test_bad_article_does_not_stop_other_candidates(self):
        maps = {'/sitemap.xml': urls(('/bad', '2026-09-02'), ('/good', '2026-09-03')),
                '/bad': b'invalid', '/good': page('Good', '2026-09-03')}
        import news
        original = news.parse_article
        def parse(body, source, url):
            if body == b'invalid':
                raise ValueError('broken article')
            return original(body, source, url)
        with patch('news.parse_article', side_effect=parse):
            code, rows, status, _ = await self.run_maps(maps)
        self.assertEqual(code, 3)
        self.assertEqual([r['title'] for r in rows], ['Good'])
        self.assertEqual(status['unparsed'], 1)
        maps['/bad'] = page('Repaired', '2026-09-02')
        self.assertEqual((await self.run_maps(maps))[1][0]['title'], 'Repaired')

    async def test_gzipped_archive(self):
        body = urls(('/first', '2026-09-02'))
        self.assertEqual(backfill.parse_sitemap(gzip.compress(body), BASE), backfill.parse_sitemap(body, BASE))
