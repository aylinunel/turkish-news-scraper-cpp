import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import export_compact


def make_db(path, rows):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE articles (id TEXT PRIMARY KEY, source TEXT, url TEXT, title TEXT, '
               'published_at TEXT, fetched_at TEXT, payload TEXT NOT NULL)')
    for i, (source, published) in enumerate(rows):
        a = dict(source=source, url=f'https://{source}.test/{i}', title=f'T{i}', published_at=published,
                 summary='', method='sitemap', body='long text')
        db.execute('INSERT INTO articles VALUES (?,?,?,?,?,?,?)',
                   (str(i), source, a['url'], a['title'], published, published, json.dumps(a)))
    db.commit()
    db.close()


class ExportCompactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / 'src'
        self.src.mkdir()
        make_db(self.src / 'backfill.sqlite3', [
            ('a', '2026-10-11T09:00:00+03:00'), ('b', '2026-10-11T10:00:00+03:00'),
            ('a', '2026-10-30T10:00:00+03:00'), ('c', '2026-10-12T10:00:00+03:00')])

    def read(self, out):
        with gzip.open(out / 'articles.jsonl.gz', 'rt', encoding='utf-8') as f:
            return [json.loads(line) for line in f]

    def test_window_and_no_bodies(self):
        out = Path(self.tmp.name) / 'all'
        export_compact.main(self.src, out, '2026-10-11', '2026-10-25')
        rows = self.read(out)
        self.assertEqual(sorted(r['source'] for r in rows), ['a', 'b', 'c'])
        self.assertNotIn('body', rows[0])

    def test_source_filter(self):
        out = Path(self.tmp.name) / 'group'
        export_compact.main(self.src, out, '2026-10-11', '2026-10-25', 'a, c')
        self.assertEqual(sorted(r['source'] for r in self.read(out)), ['a', 'c'])
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(set(summary['per_source_day']), {'a', 'c'})

    def test_since_keeps_only_newer_fetches(self):
        out = Path(self.tmp.name) / 'new'
        export_compact.main(self.src, out, '2026-10-11', '2026-10-25', '', '2026-10-11T09:30:00')
        self.assertEqual(sorted(r['source'] for r in self.read(out)), ['b', 'c'])


if __name__ == '__main__':
    unittest.main()
