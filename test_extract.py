import json
import unittest

import extract
import news

HURRIYET = {"name": "Hürriyet", "slug": "hurriyet", "url": "https://www.hurriyet.com.tr/", "feeds": []}
STORY = "https://www.hurriyet.com.tr/ekonomi/merkez-bankasi-faizi-sabit-tuttu-42000000"
PARAGRAPHS = [
    "Türkiye Cumhuriyet Merkez Bankası, politika faizini yüzde 40 seviyesinde sabit tuttu ve piyasaların "
    "beklentisi doğrultusunda bir karar aldı.",
    "Kurul, enflasyondaki düşüş eğiliminin sürmesi için sıkı para politikası duruşunun korunacağını ve "
    "gerekirse ek adımlar atılacağını belirtti.",
    "Ekonomistler ise kararın daha önce beklendiği için piyasalarda önemli bir etki yaratmadığını ifade etti "
    "ve bunun olumlu olduğunu söyledi.",
]
# No known body class: the gravity scorer must find the story among boilerplate.
UNMARKED = f'''<html lang="tr-TR"><head><title>Merkez Bankası faizi sabit tuttu - Son Dakika Haberleri</title>
<meta property="og:type" content="article">
<meta property="og:title" content="Merkez Bankası faizi sabit tuttu | Hürriyet">
<meta property="og:image" content="/img/a.jpg">
<meta property="article:tag" content="Ekonomi"><meta property="article:tag" content="Faiz">
<meta name="keywords" content="faiz, ekonomi, TCMB"></head>
<body><nav><a href="/">Anasayfa</a></nav><div class="page-wrapper has-sidebar">
<h1>Merkez Bankası faizi sabit tuttu</h1><span class="tarih">27 Ekim 2026, 14:35</span>
<div class="byline">Muhabir: Ayşe Yılmaz ve Mehmet Demir</div>
<div class="main"><p>{PARAGRAPHS[0]}</p><p>{PARAGRAPHS[1]}</p>
<p>Bizi Google News'ten takip edin, son dakika gelişmelerini kaçırmayın.</p><p>{PARAGRAPHS[2]}</p></div>
<div class="ilgili-haberler"><h3>İlgili Haberler</h3>
<p>Dolar güne yükselişle başladı ve bu da oldukça uzun bir ilgili haber başlığıdır.</p></div>
<div class="comments"><p>Bu karar bence çok yanlış ve hiç de doğru değil, ama yine de bekleyip ne olacağını görelim.</p></div>
</div></body></html>'''.encode()


class ExtractionTests(unittest.TestCase):
    def test_gravity_body_and_metadata_without_known_containers(self):
        article = news.parse_article(UNMARKED, HURRIYET, STORY)
        self.assertEqual(article.title, "Merkez Bankası faizi sabit tuttu")
        self.assertEqual(article.text, "\n\n".join(PARAGRAPHS))
        self.assertEqual(article.content_source, "gravity")
        self.assertEqual(article.published_at, "2026-10-27T14:35:00")
        self.assertEqual(article.authors, ["Ayşe Yılmaz", "Mehmet Demir"])
        self.assertEqual(article.keywords, ["Ekonomi", "Faiz", "TCMB"])
        self.assertEqual(article.image, "https://www.hurriyet.com.tr/img/a.jpg")
        self.assertEqual(article.language, "tr")
        self.assertEqual(json.loads(json.dumps(article.__dict__))["authors"], article.authors)

    def test_json_ld_fields(self):
        ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Seçim takvimi açıklandı",
              "datePublished": "2026-10-27T10:00:00+03:00", "dateModified": "2026-10-27T12:00:00+03:00",
              "author": [{"@type": "Person", "givenName": "Zeynep", "familyName": "Kaya"},
                         {"@type": "Organization", "name": "Hürriyet"}, "https://www.hurriyet.com.tr/yazar"],
              "keywords": ["seçim", "YSK"], "articleSection": "Gündem", "inLanguage": "tr",
              "image": {"@type": "ImageObject", "url": "https://i.example.com/secim.jpg"},
              "articleBody": " ".join(PARAGRAPHS)}
        page = f'<script type="application/ld+json">{json.dumps(ld)}</script><h1>Başka</h1>'.encode()
        article = news.parse_article(page, HURRIYET, STORY)
        self.assertEqual(article.title, "Seçim takvimi açıklandı")
        self.assertEqual((article.published_at, article.modified_at),
                         ("2026-10-27T07:00:00+00:00", "2026-10-27T09:00:00+00:00"))
        # Publisher-as-author and URL authors are dropped; given/family names are joined.
        self.assertEqual(article.authors, ["Zeynep Kaya"])
        self.assertEqual((article.keywords, article.section, article.language), (["seçim", "YSK"], "Gündem", "tr"))
        self.assertEqual(article.image, "https://i.example.com/secim.jpg")
        self.assertEqual(article.content_source, "json-ld")

    def test_publisher_selector_from_fundus(self):
        ntv = {"name": "NTV", "slug": "ntv", "url": "https://www.ntv.com.tr/", "feeds": []}
        page = f'''<meta property="og:type" content="article"><meta property="og:title" content="Deprem | NTV">
        <meta name="articleAuthor" content="Ali Veli"><div class="news-list"><p>{PARAGRAPHS[2]}</p></div>
        <div class="content-news-tag-selector"><p>{PARAGRAPHS[0]}</p><p>{PARAGRAPHS[1]}</p></div>'''.encode()
        article = news.parse_article(page, ntv, "https://www.ntv.com.tr/turkiye/deprem,abc")
        self.assertEqual(article.text, "\n\n".join(PARAGRAPHS[:2]))
        self.assertEqual((article.content_source, article.title, article.authors), ("publisher", "Deprem", ["Ali Veli"]))

    def test_titles_dates_and_urls(self):
        names = ["Sabah", "Hürriyet"]
        self.assertEqual(extract.strip_site_suffix("Başlık burada - Hürriyet Gazetesi", names), "Başlık burada")
        self.assertEqual(extract.strip_site_suffix("Hürriyet | Başlık burada", names), "Başlık burada")
        self.assertEqual(extract.strip_site_suffix("Başlık burada - Gündem Haberleri", names), "Başlık burada")
        # A publisher name inside the headline itself stays.
        self.assertEqual(extract.strip_site_suffix("Bakan açıklama yaptı - Sabah'a konuştu", names),
                         "Bakan açıklama yaptı - Sabah'a konuştu")
        self.assertEqual(extract.turkish_date("Güncelleme: 3 Kasım 2026 Salı 09:05"), "2026-11-03T09:05:00")
        self.assertEqual(extract.turkish_date("27.10.2026 - 14:35"), "2026-10-27T14:35:00")
        self.assertEqual(extract.turkish_date("1 Eylül 2026"), "2026-09-01")
        self.assertIsNone(extract.turkish_date("32 Ekim 2026"))
        self.assertEqual(extract.url_date("https://example.com/2026/09/15/haber"), "2026-09-15")
        self.assertIsNone(extract.url_date("https://example.com/secim-2026-09-15-sonuclari"))
        self.assertEqual(news.canonical("https://example.com/a?id=5&fbclid=x&_ga=1&igshid=2"), "https://example.com/a?id=5")
        page = b'''<a href="/iletisim/bize-ulasin-sayfasi"><h3>Bize ulasin icin iletisim sayfasi</h3></a>
        <a href="/kategori/ekonomi-haberleri-listesi">Ekonomi haberlerinin tamami burada listede</a>
        <a href="/ekonomi/faiz-karari-aciklandi-123456">Merkez Bankasi faiz kararini acikladi bugun</a>'''
        self.assertEqual(news.page_candidates(page, "https://example.com/", 5),
                         ["https://example.com/ekonomi/faiz-karari-aciklandi-123456"])

    def test_feed_authors_categories_and_images(self):
        rss = '''<rss xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/">
        <channel><item><title>Haber</title><link>https://example.com/haber-1</link>
        <dc:creator>Ece Demir</dc:creator><category>Gündem</category><category>Siyaset</category>
        <media:content url="https://cdn.example.com/1.jpg" medium="image"/></item></channel></rss>'''.encode()
        source = {"name": "Test", "slug": "test", "url": "https://example.com/", "feeds": []}
        article, = news.parse_feed(rss, source, source["url"])
        self.assertEqual((article.authors, article.keywords, article.image),
                         (["Ece Demir"], ["Gündem", "Siyaset"], "https://cdn.example.com/1.jpg"))


if __name__ == "__main__":
    unittest.main()
