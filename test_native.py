"""Optional accelerator parity; Python fallback always remains usable."""
import random
import unittest
from unittest.mock import patch

import extract


class NativeParity(unittest.TestCase):
    def test_python_fallback(self):
        with patch.object(extract, '_news_native', None):
            self.assertEqual(extract.word_count('bir iki—üç'), 3)
            self.assertEqual(extract.stopword_count('BİR İLE I'), 2)

    @unittest.skipIf(extract._news_native is None, 'optional extension is not built')
    def test_unicode_counts_match_python(self):
        rng = random.Random(42)
        samples = ['', 'BİR İLE I ı İ i', 've_ile ve-ile', 'e\u0301 ² Ⅻ 中文 😀',
                   '\ud800ve\udfff ile', 'ΟΣ ΟΣΑ', 've\x00ile', 've ' * 10000]
        samples += [''.join(chr(rng.randrange(0x110000)) for _ in range(300))
                    for _ in range(100)]
        for text in samples:
            with self.subTest(text=repr(text[:40])):
                self.assertEqual(extract.word_count(text), len(extract.WORD.findall(text)))
                self.assertEqual(extract.stopword_count(text), sum(
                    w in extract.STOPWORDS_TR for w in extract.WORD.findall(extract.lower_tr(text))))

    @unittest.skipIf(extract._news_native is None, 'optional extension is not built')
    def test_extracted_fields_match_fallback(self):
        paragraph = 'Türkiye için yapılan bu açıklama ile ilgili olarak yeni gelişmeler olduğu bildirildi. '
        body = ('<meta property="og:type" content="article"><h1>Gündem</h1><div>' +
                ''.join('<p>' + paragraph * 5 + '</p>' for _ in range(25)) + '</div>').encode()
        accelerated = extract.extract(body, 'https://example.com/story')
        with patch.object(extract, '_news_native', None):
            self.assertEqual(accelerated, extract.extract(body, 'https://example.com/story'))
