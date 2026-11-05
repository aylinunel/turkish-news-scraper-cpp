"""Reproducible offline CPU benchmark; no network or publisher speed claims."""
import argparse
import statistics
import timeit
from unittest.mock import patch

import extract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pages', type=int, default=100)
    args = parser.parse_args()
    if args.pages <= 0:
        parser.error('--pages must be positive')
    native = extract._news_native
    if native is None:
        parser.error('Build the optional extension first; see README.md')
    text = ('Türkiye için bugün yapılan açıklamada bu ve diğer haberler ile ilgili olarak '
            'yeni gelişmeler olduğu bildirildi. ') * 8
    body = ('<meta property="og:type" content="article"><h1>Gündem</h1><div>' +
            ''.join('<p>' + text + '</p>' for _ in range(60)) + '</div>').encode()
    workloads = {
        'word count': (lambda: extract.word_count(text), 10000),
        'stopword count': (lambda: extract.stopword_count(text), 10000),
        'page extraction (60 paragraphs)': (
            lambda: extract.extract(body, 'https://example.com/story'), args.pages),
    }
    for name, (operation, number) in workloads.items():
        timings = {}
        for label, backend in [('Python', None), ('C++', native)]:
            with patch.object(extract, '_news_native', backend):
                timings[label] = statistics.median(timeit.repeat(operation, number=number, repeat=5))
        print(f'{name}: {number} iterations; Python={timings["Python"]:.4f}s '
              f'C++={timings["C++"]:.4f}s speedup={timings["Python"]/timings["C++"]:.2f}x')


if __name__ == '__main__':
    main()
