import asyncio
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backfill
import merge_results
from test_backfill import page

BASE = 'https://example.com'
SOURCE = dict(slug='example', name='Example', url=BASE, feeds=[])


class QuickBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def args(self, **kw):
        base = dict(db=Path(self.tmp.name) / 'b.sqlite3', status=Path(self.tmp.name) / 'status.json',
                    concurrency=2, delay=0, timeout=5, site_timeout=10, max_sitemaps=100, max_undated=1,
                    max_pages=1000, lastmod_slack=3, full_text=False, limit=100, interval=0,
                    start=date(2026, 9, 1), end=date(2026, 9, 3), per_day=2, news_only=True)
        base.update(kw)
        return SimpleNamespace(**base)

    async def run_site(self, args, latency=0):
        # Three days, five gündem stories a day, plus sport stories that must be skipped.
        entries, pages = [], {}
        for d in (1, 2, 3):
            for i in range(5):
                path = f'/gundem/haber-{d}-{i}'
                entries.append(f'<url><loc>{BASE}{path}</loc><lastmod>2026-09-0{d}</lastmod></url>')
                pages[path] = page(f'Haber {d}-{i}', f'2026-09-0{d}T10:00:00+03:00')
            entries.append(f'<url><loc>{BASE}/spor/mac-{d}</loc><lastmod>2026-09-0{d}</lastmod></url>')
            pages[f'/spor/mac-{d}'] = page(f'Maç {d}', f'2026-09-0{d}T10:00:00+03:00')
        maps = {'/robots.txt': f'Sitemap: {BASE}/sitemap.xml\n'.encode(),
                '/sitemap.xml': ('<urlset>' + ''.join(entries) + '</urlset>').encode()}
        maps.update(pages)
        hits, inflight, self.peak = [], [0], 0
        async def fetch(_c, _s, url, cache=False):
            path = url.removeprefix(BASE)
            hits.append(path)
            page = path.startswith('/gundem/')  # story pages only, not robots or sitemaps
            inflight[0] += page
            self.peak = max(self.peak, inflight[0])
            try:
                if latency:
                    await asyncio.sleep(latency)
                return (maps[path], url) if path in maps else None
            finally:
                inflight[0] -= page
        out = io.StringIO()
        with patch.object(backfill.Backfill, 'fetch', fetch), contextlib.redirect_stdout(out):
            code = await backfill.Backfill(args).run([SOURCE])
        rows = [json.loads(x) for x in out.getvalue().splitlines()]
        status = json.loads(args.status.read_text())['sources'][0]
        return code, rows, status, hits

    async def test_per_day_cap_spreads_evenly_and_skips_sport(self):
        code, rows, status, hits = await self.run_site(self.args())
        days = sorted(r['published_at'][:10] for r in rows)
        self.assertEqual(days, ['2026-09-01'] * 2 + ['2026-09-02'] * 2 + ['2026-09-03'] * 2)
        self.assertFalse(any(h.startswith('/spor/') for h in hits))
        self.assertEqual(status['section_skipped'], 3)
        self.assertEqual((status['days_full'], status['days_empty'], status['status']), (3, 0, 'ok'))
        self.assertEqual(sum(h.startswith('/gundem/') for h in hits), 6, 'stops once every day is full')
        self.assertEqual(code, 0)

    async def test_rerun_with_higher_cap_adds_only_new_stories(self):
        await self.run_site(self.args())
        _, rows, status, _ = await self.run_site(self.args(per_day=3))
        self.assertEqual(len(rows), 3)
        self.assertEqual(status['days_full'], 3)

    async def test_quota_requests_overlap_but_never_overfetch(self):
        # Quota runs used to fetch one page at a time. They now use the per-site concurrency
        # with per-day reservations: requests overlap, and a filled day is never overfetched.
        code, rows, status, hits = await self.run_site(self.args(), latency=0.02)
        self.assertEqual(self.peak, 2, 'two story requests overlap (per-site concurrency)')
        self.assertEqual(sum(h.startswith('/gundem/') for h in hits), 6, 'reservations prevent overfetching')
        self.assertEqual(len(rows), 6)
        self.assertEqual(status['days_full'], 3)

    async def test_stale_child_sitemaps_are_pruned(self):
        index = ('<sitemapindex>'
                 f'<sitemap><loc>{BASE}/sitemap-a.xml</loc><lastmod>2026-07-01</lastmod></sitemap>'
                 f'<sitemap><loc>{BASE}/sitemap-b.xml</loc><lastmod>2026-09-02</lastmod></sitemap>'
                 f'<sitemap><loc>{BASE}/sitemap-c.xml</loc></sitemap>'
                 f'<sitemap><loc>{BASE}/sitemap-2026-09.xml</loc><lastmod>2026-07-01</lastmod></sitemap>'
                 '</sitemapindex>').encode()
        empty = b'<urlset></urlset>'
        maps = {'/robots.txt': f'Sitemap: {BASE}/sitemap.xml\n'.encode(), '/sitemap.xml': index,
                '/sitemap-a.xml': empty, '/sitemap-b.xml': empty, '/sitemap-c.xml': empty, '/sitemap-2026-09.xml': empty}
        hits = []

        async def fetch(_c, _s, url, cache=False):
            path = url.removeprefix(BASE)
            hits.append(path)
            return (maps[path], url) if path in maps else None
        args = self.args()
        with patch.object(backfill.Backfill, 'fetch', fetch), contextlib.redirect_stdout(io.StringIO()):
            await backfill.Backfill(args).run([SOURCE])
        status = json.loads(args.status.read_text())['sources'][0]
        self.assertNotIn('/sitemap-a.xml', hits, 'last modified before the window: cannot hold window stories')
        self.assertIn('/sitemap-b.xml', hits)
        self.assertIn('/sitemap-c.xml', hits, 'no lastmod: kept')
        self.assertIn('/sitemap-2026-09.xml', hits, 'URL names a window month: kept whatever its lastmod')
        self.assertEqual(status['sitemaps_pruned'], 1)

    async def test_index_with_only_stale_children_is_still_visited(self):
        index = ('<sitemapindex>'
                 f'<sitemap><loc>{BASE}/sitemap-a.xml</loc><lastmod>2020-01-01</lastmod></sitemap>'
                 '</sitemapindex>').encode()
        maps = {'/robots.txt': f'Sitemap: {BASE}/sitemap.xml\n'.encode(), '/sitemap.xml': index,
                '/sitemap-a.xml': b'<urlset></urlset>'}
        hits = []

        async def fetch(_c, _s, url, cache=False):
            path = url.removeprefix(BASE)
            hits.append(path)
            return (maps[path], url) if path in maps else None
        with patch.object(backfill.Backfill, 'fetch', fetch), contextlib.redirect_stdout(io.StringIO()):
            await backfill.Backfill(self.args()).run([SOURCE])
        self.assertIn('/sitemap-a.xml', hits)

    def test_merge_parts(self):
        parts = Path(self.tmp.name) / 'parts'
        for name, slug, n in (('part-a', 'a', 2), ('part-b', 'b', 3)):
            d = parts / name
            d.mkdir(parents=True)
            db = sqlite3.connect(d / 'backfill.sqlite3')
            db.executescript(merge_results.SCHEMA)
            for i in range(n):
                db.execute('INSERT INTO articles VALUES (?,?,?,?,?,?,?)',
                           (f'{slug}{i}', slug, f'u{i}', 't', f'2026-09-0{i + 1}', 'f', json.dumps({'source': slug, 'i': i})))
            db.execute("INSERT INTO articles VALUES ('shared','a','dup','t','2026-09-01','f','{}')") if slug == 'b' else None
            db.commit(); db.close()
            (d / 'backfill_status.json').write_text(json.dumps({'checked_at': 'x', 'sources': [{'source': slug, 'checked_at': 'x'}]}))
        out = Path(self.tmp.name) / 'results'
        result = merge_results.merge(parts, out)
        self.assertEqual(result, {'articles': 6, 'sources': 2})
        self.assertEqual(len((out / 'backfill.jsonl').read_text().splitlines()), 6)
        st = {s['source']: s for s in json.loads((out / 'backfill_status.json').read_text())['sources']}
        self.assertEqual((st['a']['stored_total'], st['b']['stored_total']), (3, 3))


if __name__ == '__main__':
    unittest.main()
