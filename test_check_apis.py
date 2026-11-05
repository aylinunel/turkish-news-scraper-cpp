import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import check_apis

SOURCES = [dict(slug='known', name='Known publisher', url='https://known.com/', feeds=[]),
           dict(slug='second', name='Second publisher', url='https://second.com/', feeds=[])]


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status, self.headers = payload, status, headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield json.dumps(self.payload).encode()


class Session:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class CoverageTests(unittest.TestCase):
    def test_subdomains_deduplication_and_new_candidates(self):
        payload = {'status': 'ok', 'totalResults': 1000, 'articles': [
            {'url': 'https://www.known.com/a?utm_source=x'}, {'url': 'https://www.known.com/a'},
            {'url': 'https://regional.known.com/b'}, {'url': 'https://new.com/c', 'source': {'name': 'New'}},
            {'url': 'https://known.com.attacker.com/a'}, {'url': 'http://localhost/a'}, None]}
        result = check_apis.summarize(payload, 'newsapi', SOURCES)
        self.assertEqual(result['matched_publishers'], {'known': 2})
        self.assertEqual(result['unobserved_registry_slugs'], ['second'])
        self.assertEqual(result['invalid_records'], 2)
        self.assertEqual(result['provider_total_results'], 1000)
        self.assertEqual([s['domain'] for s in result['new_publisher_candidates']],
                         ['known.com.attacker.com', 'new.com'])
        self.assertEqual(result['articles_with_api_text'], 0)

    def test_catalog_is_not_reported_as_complete_search_coverage(self):
        result = check_apis.summarize({'sources': [{'url': 'https://known.com', 'name': 'Known'}]},
                                     'newsapi', SOURCES, catalog=True)
        self.assertIn('not the complete', result['coverage_note'])

    def test_worldnews_text_availability_and_malformed_responses(self):
        result = check_apis.summarize({'available': 10, 'news': [
            {'url': 'https://known.com/a', 'text': 'Article body'},
            {'url': 'https://known.com/b', 'text': None}]}, 'worldnewsapi', SOURCES)
        self.assertEqual(result['articles_with_api_text'], 1)
        with self.assertRaises(ValueError):
            check_apis.summarize({'news': None}, 'worldnewsapi', SOURCES)

    def test_batch_queries_use_supported_filters_and_do_not_contain_credentials(self):
        queries = check_apis.queries('newsapi', SOURCES, '2026-10-01', '2026-10-10')
        self.assertEqual(len(queries), 3)
        self.assertEqual(queries[-1][2]['domains'], 'known.com,second.com')
        self.assertNotIn('language', queries[-1][2], 'Turkish is not a supported NewsAPI language filter')
        world = check_apis.queries('worldnewsapi', SOURCES, '2026-10-01T00:00:00', '2026-10-10T00:00:00')
        self.assertEqual(len(world), 2)
        self.assertEqual(world[0][2]['source-country'], 'tr')
        self.assertEqual(world[0][2]['earliest-publish-date'], '2026-10-01 00:00:00')
        self.assertEqual(world[1][2]['news-sources'], 'https://known.com/,https://second.com/')
        for _, _, params, _ in queries + world:
            self.assertFalse(any('key' in k.lower() for k in params))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_are_headers_only_and_redirects_disabled(self):
        session = Session([Response({'status': 'ok', 'articles': []})])
        await check_apis.probe(session, 'newsapi', 'test-secret', 'sample', '/v2/everything',
                               {'domains': 'known.com'}, False, SOURCES)
        url, kwargs = session.calls[0]
        self.assertEqual(url, 'https://newsapi.org/v2/everything')
        self.assertEqual(kwargs['headers'], {'X-Api-Key': 'test-secret'})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('test-secret', url)
        self.assertNotIn('test-secret', json.dumps(kwargs['params']))

    async def test_auth_failure_stops_without_more_requests_and_redacts_echoed_key(self):
        key = 'test-secret'
        session = Session([Response({'status': 'error', 'code': 'apiKeyInvalid', 'message': f'Bad key {key}'}, 401)])
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(provider='newsapi', start='2026-10-01', end='2026-10-10',
                                   timeout=5, output=Path(tmp)/'report.json')
            with patch('check_apis.aiohttp.ClientSession', return_value=session):
                self.assertEqual(await check_apis.run(args, SOURCES, key), 3)
            report = args.output.read_text()
            self.assertNotIn(key, report)
            self.assertIn('[redacted]', report)
            self.assertEqual(json.loads(report)['requests_attempted'], 1)

    async def test_worldnews_quota_exhaustion_stops(self):
        session = Session([Response({'message': 'Quota exhausted'}, 402,
                                    {'X-API-Quota-Left': '0'})])
        args = SimpleNamespace(provider='worldnewsapi', start='2026-10-01', end='2026-10-10', timeout=5, output=None)
        output = io.StringIO()
        with patch('check_apis.aiohttp.ClientSession', return_value=session), contextlib.redirect_stdout(output):
            self.assertEqual(await check_apis.run(args, SOURCES, 'test-secret'), 3)
        report = json.loads(output.getvalue())
        self.assertEqual(report['requests_attempted'], 1)
        self.assertEqual(report['queries'][0]['quota']['X-API-Quota-Left'], '0')


if __name__ == '__main__':
    unittest.main()
