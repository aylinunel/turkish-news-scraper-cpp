"""Article metadata and body extraction for Turkish news pages.

Techniques adapted from MIT-licensed open-source news extractors, rewritten for
lxml alone and Turkish publishers:

* newspaper / newspaper4k (codelucas, AndyTheFactory): gravity scoring of text
  nodes by stopword count with Turkish stopwords, link-density filtering,
  boilerplate node removal, publication-date meta names, byline parsing, title
  suffix removal and top image selection.
* fundus (flairNLP): publisher-specific paragraph selectors (Habertürk, NTV,
  Anadolu Ajansı) and JSON-LD author parsing (name or given/family names).
* news-fetch (santhoshse7en): extraction provenance (``content_source``) and
  URL dates as a last-resort publication date.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

try:
    import _news_native
except ImportError:
    _news_native = None

# newspaper4k's resources/text/stopwords-tr.txt (MIT).
STOPWORDS_TR = frozenset("""
acaba altmış altı ama ancak arada aslında ayrıca bana bazı belki ben benden beni benim beri beş bile
bin bir birçok biri birkaç birkez birşey birşeyi biz bize bizden bizi bizim böyle böylece bu buna bunda
bundan bunlar bunları bunların bunu bunun burada çok çünkü da daha dahi de defa değil diğer diye doksan
dokuz dolayı dolayısıyla dört edecek eden ederek edilecek ediliyor edilmesi ediyor eğer elli en etmesi
etti ettiği ettiğini gibi göre halen hangi hatta hem henüz hep hepsi her herhangi herkes herkesin hiç
hiçbir için iki ile ilgili ise işte itibaren itibariyle kadar karşın katrilyon kendi kendilerine kendini
kendisi kendisine kendisini kez ki kim kimden kime kimi kimse kırk milyar milyon mu mü mı nasıl ne neden
nedenle nerde nerede nereye niye niçin o olan olarak oldu olduğu olduğunu olduklarını olmadı olmadığı
olmak olması olmayan olmaz olsa olsun olup olur olursa oluyor on ona ondan onlar onlardan onları onların
onu onun otuz oysa öyle pek rağmen sadece sanki sekiz seksen sen senden seni senin siz sizden sizi sizin
şey şeyden şeyi şeyler şöyle şu şuna şunda şundan şunları şunu tarafından trilyon tüm üç üzere var vardı
ve veya ya yani yapacak yapılan yapılması yapıyor yapmak yaptı yaptığı yaptığını yaptıkları yedi yerine
yetmiş yine yirmi yoksa yüz zaten
""".split())

# Publication-date meta names, most specific first (newspaper4k defines.py,
# without modification dates, which never stand in for publication).
PUBLISHED_META = ("article:published_time", "datepublished", "og:published_time", "published_time",
                  "published_date", "publish_date", "publishdate", "publish-date", "pubdate", "pub_date",
                  "dc.date.issued", "dcterms.created", "dcterms.date", "dc.date", "rnews:datepublished",
                  "prism.publicationdate", "parsely-pub-date", "sailthru.date", "publication_date",
                  "originalpublicationdate", "original-publish-date", "article.published",
                  "cxenseparse:publishtime", "publishtime", "displaydate", "date")
MODIFIED_META = ("article:modified_time", "datemodified", "og:updated_time", "modified_time",
                 "dc.date.modified", "last-modified")
AUTHOR_META = ("author", "article:author", "dc.creator", "parsely-author", "sailthru.author", "byl",
               "articleauthor", "citation_author")
MONTHS_TR = {"ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
             "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12}

# Article body selectors from fundus's Turkish parsers, plus T24 and common
# Turkish CMS containers. Keys are hostnames without "www.".
PUBLISHER_BODY = {
    "haberturk.com": '//article//p',
    "ntv.com.tr": '//div[contains(@class,"content-news-tag-selector")]/p | '
                  '//div[@property="articleBody"]//p | //div[contains(@class,"content")]/p[text()]',
    "aa.com.tr": '//div[@class="detay-icerik"]//p | //div[@dir]/p',
    "t24.com.tr": '//*[@id="story-content-wrapper"]//p',
}
# fundus's Anadolu Ajansı byline: "Ad Soyad |" before the date.
PUBLISHER_AUTHOR = {"aa.com.tr": '//div[contains(@class,"detay-bg")]/div/div/div/span[1]'}
BODY_CLASSES = ("article-body", "articlebody", "article-content", "article-text", "news-content",
                "news-text", "news-detail-text", "haber-text", "haber-detay", "haber-icerik",
                "haber_metni", "detail-content", "content-text", "entry-content", "post-content",
                "story-body", "text-content")
BODY_XPATH = ('//*[@itemprop="articleBody"] | //*[@property="articleBody"] | //*[@id="story-content-wrapper"] | '
              + " | ".join(f'//*[contains(concat(" ",normalize-space(@class)," ")," {c} ")]' for c in BODY_CLASSES))

# Containers that never hold the story (newspaper's cleaner, plus Turkish
# class names such as ilgili/benzer haberler, yorum, paylaş, reklam, etiket).
# Matched against each class/id token's start or end ("ilgili-haberler", "news-share"),
# so a body class such as "content-news-tag-selector" is not mistaken for tags.
BOILERPLATE = re.compile(
    r"^(?:comments?|yorum(?:lar)?|share|sharing|social|paylas|related|ilgili|benzer|"
    r"recommend|oneri|popular|populer|most-read|cok-okunan|newsletter|abone|subscribe|advert\w*|ads?|"
    r"reklam|banner|sponsor|sidebar|breadcrumbs?|tags?|etiket(?:ler)?|cookie|cerez|popup|modal|"
    r"navbar|menu|footer|pagination|author-box|yazar-kutu)(?:[_-]|$)|[_-](?:comments?|yorum(?:lar)?|share|social|"
    r"paylas|related|ilgili|benzer|haberler|ads|reklam|banner|sidebar|tags|etiketler|menu|footer)$", re.I)
BOILERPLATE_TEXT = re.compile(
    r"^(?:reklam|ilgili haber|benzer haber|haberin devam|abone ol|yorum yap|paylaş|tıklayın|"
    r"bizi .{0,30}takip ed|.{0,40}(?:google news|whatsapp|telegram).{0,30}(?:takip|kanal|abone)|"
    r"öne çıkanlar|son dakika haberleri için)", re.I)
WORD = re.compile(r"\w+")
ASCII_TR = str.maketrans("çğıöşüâîû", "cgiosuaiu")
# Words allowed around a publisher name in a title suffix ("Hürriyet Gazetesi").
GENERIC = {"haber", "haberi", "haberler", "haberleri", "gazete", "gazetesi", "son", "dakika", "com", "tr", "net",
           "tv", "online", "guncel", "turkiye", "turk", "ajansi", "news", "en", "sondakika"}
SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " » ", " / ")


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def plain(value: str | None) -> str:
    if not value:
        return ""
    try:
        return clean(html.fragment_fromstring(value, create_parent=True).text_content())
    except (etree.ParserError, ValueError):
        return clean(value)


def lower_tr(value: str) -> str:
    """Lowercase with Turkish dotted/dotless I."""
    return value.replace("I", "ı").replace("İ", "i").lower()


def fold(value: str) -> str:
    """ASCII key for comparisons (Türkiye'nin -> turkiye nin)."""
    return re.sub(r"\W+", " ", lower_tr(value).translate(ASCII_TR)).strip()


def date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError, IndexError):
            return turkish_date(value)
    # Keep publisher timestamps without a zone as naive: do not invent UTC.
    return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.isoformat()


def turkish_date(value: str | None) -> str | None:
    """Parse '27 Ekim 2026 14:35', '27.10.2026 - 14:35' and similar page text.

    Page text has no zone, so the result stays naive like other zoneless dates.
    """
    if not value or len(value) > 200:
        return None
    s = lower_tr(value).translate(ASCII_TR)
    m = re.search(r"(?<!\d)(\d{1,2})\.?\s+(" + "|".join(MONTHS_TR) + r")\s*,?\s*(20\d\d)(?!\d)", s)
    if m:
        day, month, year = int(m.group(1)), MONTHS_TR[m.group(2)], int(m.group(3))
    else:
        m = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](20\d\d)(?!\d)", s)
        if not m:
            return None
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    clock = re.search(r"(?<![\d.])([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d.])", s[m.end():])
    try:
        dt = datetime(year, month, day, *(int(g) for g in clock.groups()) if clock else (0, 0))
    except ValueError:
        return None
    return dt.isoformat() if clock else dt.date().isoformat()


def url_date(url: str) -> str | None:
    """A /2026/10/27/ permalink date (news-fetch's last resort).

    Dates inside slugs (secim-2026-10-27) can name events, so only path segments count.
    """
    m = re.search(r"/(20\d\d)/(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/", urlsplit(url).path)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).date().isoformat()
    except ValueError:
        return None


def document(body: bytes):
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return html.fromstring(body)
    return html.fromstring(body, parser=html.HTMLParser(encoding="utf-8"))


def word_count(text: str) -> int:
    if _news_native is not None:
        return _news_native.word_count(text)
    return len(WORD.findall(text))


def link_density(node) -> float:
    words = word_count(node.text_content())
    if not words:
        return 1.0 if node.xpath(".//a") else 0.0
    return sum(word_count(a.text_content()) or 1 for a in node.xpath(".//a")) / words


def stopword_count(text: str) -> int:
    text = lower_tr(text)
    if _news_native is not None:
        return _news_native.stopword_count(text, STOPWORDS_TR)
    return sum(w in STOPWORDS_TR for w in WORD.findall(text))


def strings(value) -> list[str]:
    """Flatten JSON-LD values (string, list, comma list) into strings."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    if isinstance(value, dict):
        return strings(value.get("name") or value.get("url") or value.get("@id") or "")
    return []


def person_names(value) -> list[str]:
    """JSON-LD author values as names (fundus generic_author_parsing)."""
    if isinstance(value, list):
        return [n for v in value for n in person_names(v)]
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, (str, list, dict)):
            return person_names(name)
        given, family = value.get("givenName", ""), value.get("familyName", "")
        return [f"{given} {family}".strip()] if given and family else []
    return split_byline(value) if isinstance(value, str) and not re.match(r"https?:|www\.", value) else []


def split_byline(text: str) -> list[str]:
    """Split a byline into names (newspaper's _parse_byline, Turkish prefixes)."""
    text = re.sub(r"(?i)^\s*(?:by|yazan|yazar|haber|muhabir|editör|editor|derleyen|hazırlayan)\s*[:\-]\s*", "",
                  clean(text))
    return [p.strip(" .,-/") for p in re.split(r"[,;|·/&]|\s(?:ve|and|ile)\s", text)]


def filter_authors(names: list[str], publisher: str) -> list[str]:
    out = {}
    for name in names:
        name = clean(name).strip(" .,-/")
        words = name.split()
        if not 1 <= len(words) <= 5 or not 2 <= len(name) <= 60 or re.search(r"\d|https?:|www\.|@|\.com", name):
            continue
        # A publisher listed as author adds nothing beyond the source field.
        if publisher and fold(name) == fold(publisher):
            continue
        out.setdefault(fold(name), name)
    return list(out.values())


def strip_site_suffix(title: str, site_names: list[str], h1: str = "") -> str:
    """Remove ' | Hürriyet' style suffixes and prefixes (newspaper title cleaning)."""
    keys = [fold(n) for n in site_names if n and fold(n)]
    for _ in range(2):
        for sep in SEPARATORS:
            if sep not in title:
                continue
            head, tail = title.rsplit(sep, 1)
            first, rest = title.split(sep, 1)
            def is_site(part):
                part = fold(part)
                if not part:
                    return False
                for k in keys:
                    if part == k or (len(part) > 3 and part in k):
                        return True
                    if len(k) > 3 and k in part and set(part.replace(k, " ").split()) <= GENERIC:
                        return True
                # "Son Dakika Haberleri", "Gündem Haberleri", "Ekonomi Haberleri"
                return len(part.split()) <= 4 and bool(re.fullmatch(
                    r"(?:son dakika )?(?:\w+ )?(?:haber(?:ler)?i?|gazetesi)(?: \w+)?", part))
            if is_site(tail) and clean(head):
                title = clean(head)
                break
            if is_site(first) and clean(rest):
                title = clean(rest)
                break
        else:
            break
    if h1 and fold(h1) and fold(title).startswith(fold(h1)) and len(h1) >= 0.6 * len(title):
        return h1
    return title


def json_ld(doc) -> list[dict]:
    nodes = []
    def walk(value):
        if isinstance(value, dict):
            nodes.append(value)
            for key in ("@graph", "mainEntity", "itemListElement", "item"):
                walk(value.get(key))
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for script in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            walk(json.loads(script, strict=False))
        except (ValueError, RecursionError):
            continue
    return nodes


def article_type(value) -> bool:
    kind = str(value).strip().rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return kind.endswith("Article") or kind in {"BlogPosting", "LiveBlogPosting", "Report"}


def paragraphs(container) -> list[str]:
    out = []
    for node in container.xpath('.//p | .//h2 | .//h3 | .//blockquote[not(.//p)]'):
        text = clean(node.text_content())
        short = len(text) < 150
        if node.tag in {"h2", "h3"}:
            if 10 <= len(text) <= 200 and not BOILERPLATE_TEXT.match(text):
                out.append(text)
            continue
        if len(text) <= 35 or (short and BOILERPLATE_TEXT.match(text)) or link_density(node) > 0.5:
            continue
        out.append(text)
    # Leading subheadlines without a following paragraph are usually related links.
    while out and len(out[-1]) <= 200 and not out[-1].endswith((".", "!", "?", "\"", "”", "'", ")", "…", ":")):
        out.pop()
    return list(dict.fromkeys(out))


def remove_boilerplate(doc, keep: str = "") -> None:
    for node in doc.xpath('//script | //style | //noscript | //nav | //footer | //aside | //form | //iframe | '
                          '//figcaption | //*[@itemprop="caption"] | //button | //svg'):
        node.drop_tree()
    # Never drop the headline or a known body container, nor any wrapper around them
    # (a page wrapper class such as "has-sidebar" must not remove the story).
    protected = set()
    for node in doc.xpath('//h1 | //article | ' + BODY_XPATH + (" | " + keep if keep else "")):
        protected.add(node)
        protected.update(node.iterancestors())
    for node in doc.xpath('//body//*[@class or @id]'):
        if node in protected or node.tag == "p":
            continue
        if any(BOILERPLATE.search(t) for t in f'{node.get("class", "")} {node.get("id", "")}'.split()):
            node.drop_tree()


def gravity_body(doc) -> str:
    """newspaper's gravity score: stopword-rich paragraphs lift their parents."""
    scores = {}
    nodes = doc.xpath('//p | //pre | //td')
    for index, node in enumerate(nodes):
        text = clean(node.text_content())
        count = stopword_count(text)
        if count < 3 or link_density(node) > 0.33:
            continue
        score = count + math.log1p(len(text.split()))
        # Paragraphs near the end of the page are often comments or teasers.
        if len(nodes) > 15 and index > 0.75 * len(nodes):
            score *= 0.5
        parent = node.getparent()
        if parent is None:
            continue
        scores[parent] = scores.get(parent, 0) + score
        grand = parent.getparent()
        if grand is not None:
            scores[grand] = scores.get(grand, 0) + score * 0.4
    for node in list(scores):
        token = f'{node.get("class", "")} {node.get("id", "")} {node.get("itemprop", "")}'
        if node.get("itemprop") == "articleBody":
            scores[node] += 100
        elif node.tag == "article":
            scores[node] += 25
        elif re.search(r"article|story|content|detail|detay|haber|entry|post|text|body", token, re.I):
            scores[node] += 15
    for node in sorted(scores, key=scores.get, reverse=True)[:3]:
        text = "\n\n".join(paragraphs(node))
        if len(text) >= 80:
            return text
    return ""


def dom_body(doc, host: str) -> tuple[str, str]:
    """Body text from the page and the step that found it. Mutates doc."""
    selector = next((x for h, x in PUBLISHER_BODY.items() if host == h or host.endswith("." + h)), None)
    remove_boilerplate(doc, selector or "")
    if selector:
        text = "\n\n".join(dict.fromkeys(t for n in doc.xpath(selector)
                                         if len(t := clean(n.text_content())) > 35 and not BOILERPLATE_TEXT.match(t)))
        if text:
            return text, "publisher"
    text = "\n\n".join(dict.fromkeys(p for c in doc.xpath(BODY_XPATH) for p in paragraphs(c)))
    if text:
        return text, "selector"
    text = gravity_body(doc)
    if text:
        return text, "gravity"
    containers = doc.xpath('//article')
    text = "\n\n".join(dict.fromkeys(p for c in containers for p in paragraphs(c)))
    return (text or (clean(containers[0].text_content()) if containers else "")), "article"


def extract(body: bytes, url: str, publisher: str = "") -> dict | None:
    """Return article fields, or None when the page is not an article."""
    doc = document(body)
    meta, tags = {}, []
    for n in doc.xpath('//meta[@content]'):
        key = (n.get("property") or n.get("name") or n.get("itemprop") or n.get("http-equiv") or "").lower()
        value = n.get("content", "").strip()
        if key == "article:tag" and value:
            tags.append(value)
        if key and value:
            meta.setdefault(key, value)
    nodes = json_ld(doc)
    news = next((n for n in nodes if any(article_type(t) for t in
                 (n.get("@type", []) if isinstance(n.get("@type"), list) else [n.get("@type", "")]))), {})
    ld_date = news.get("datePublished") if isinstance(news.get("datePublished"), str) else None
    publication = ld_date or next((meta[k] for k in PUBLISHED_META if meta.get(k) and date(meta[k])), None)
    micro_article = bool(doc.xpath('//*[@itemtype and (contains(@itemtype,"Article") or contains(@itemtype,"BlogPosting"))]'))
    dates = doc.xpath('//*[@itemprop="datePublished"]/@datetime | //*[@itemprop="datePublished"]/@content | '
                      '//*[@itemprop="datePublished"]/text()')
    publication = publication or next((d for d in dates if date(d)), None)
    if not news and meta.get("og:type", "").lower() != "article" and not micro_article and not date(publication):
        return None
    publication = publication or next(iter(doc.xpath('//article//time/@datetime | //time[@pubdate]/@datetime')), None)
    if not date(publication):
        # Visible Turkish dates, e.g. <span class="tarih">27 Ekim 2026 14:35</span>.
        visible = doc.xpath('//*[self::time or contains(@class,"date") or contains(@class,"tarih") or '
                            'contains(@class,"time")][string-length(normalize-space()) < 80]')
        publication = next((t for n in visible[:10] if (t := clean(n.text_content())) and turkish_date(t)), None)
    published_at = date(publication) if isinstance(publication, str) else None
    published_at = published_at or url_date(url)
    modified = news.get("dateModified") if isinstance(news.get("dateModified"), str) else None
    modified = modified or next((meta[k] for k in MODIFIED_META if meta.get(k)), None)

    h1 = clean(" ".join(doc.xpath('(//h1)[1]//text()')))
    title = news.get("headline") if isinstance(news.get("headline"), str) else None
    title = title or meta.get("og:title") or meta.get("twitter:title") or meta.get("headline") or h1 \
        or clean(" ".join(doc.xpath('//title//text()')))
    if not isinstance(title, str) or not clean(title):
        return None
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    publisher_ld = news.get("publisher")
    site_names = [publisher, meta.get("og:site_name", ""), meta.get("application-name", ""),
                  host.split(".")[0], *(strings(publisher_ld)[:1] if publisher_ld else [])]
    title = strip_site_suffix(plain(title), site_names, plain(h1))

    authors = person_names(news.get("author"))
    if not authors:
        authors = [n for k in AUTHOR_META if meta.get(k) and not meta[k].startswith("http")
                   for n in split_byline(meta[k])]
    host_author = next((x for h, x in PUBLISHER_AUTHOR.items() if host == h or host.endswith("." + h)), None)
    if not authors and host_author:
        authors = [n for node in doc.xpath(host_author)[:2] for n in split_byline(node.text_content())]
    if not authors:
        bylines = doc.xpath('//*[@rel="author" or @itemprop="author" or contains(@class,"byline") or '
                            'contains(@class,"author-name") or contains(@class,"muhabir") or '
                            'contains(@class,"yazar-adi") or contains(@class,"editor-name")]')
        authors = [n for node in bylines[:3] if len(t := clean(node.text_content())) < 80 for n in split_byline(t)]
    authors = filter_authors(authors, publisher)

    keywords = {}
    for value in [*strings(news.get("keywords")), *tags, meta.get("news_keywords", ""), meta.get("keywords", "")]:
        for keyword in value.split(","):
            if 0 < len(keyword := clean(keyword)) <= 60 and fold(keyword) != fold(publisher):
                keywords.setdefault(fold(keyword), keyword)
    keywords = list(keywords.values())[:30]

    image = news.get("image")
    while isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl") or ""
    image = (image if isinstance(image, str) else "") or meta.get("og:image") or meta.get("twitter:image") \
        or next(iter(doc.xpath('//link[@rel="image_src"]/@href')), "")
    image = urljoin(url, image) if image else ""
    if not image.startswith(("http://", "https://")):
        image = ""
    language = (doc.get("lang") or meta.get("og:locale") or
                (news.get("inLanguage") if isinstance(news.get("inLanguage"), str) else "") or
                meta.get("content-language") or "")
    language = re.split(r"[-_]", language.strip().lower())[0][:3] if language else ""
    section = next(iter(strings(news.get("articleSection"))), "") or meta.get("article:section", "")

    summary = news.get("description") or meta.get("og:description") or meta.get("description") or ""
    content, content_source = news.get("articleBody"), "json-ld"
    # Some CMSs double-escape JSON-LD bodies (&amp;Ccedil;) and flatten paragraphs.
    content = clean(unescape(plain(content))) if isinstance(content, str) else ""
    dom, dom_source = dom_body(doc, host)
    # Page paragraphs keep their breaks, so they win unless clearly incomplete.
    if dom and len(dom) >= 0.8 * len(content):
        content, content_source = dom, dom_source
    return {"title": title, "published_at": published_at,
            "modified_at": date(modified) if isinstance(modified, str) else None,
            "summary": plain(summary) if isinstance(summary, str) else "", "text": content,
            "authors": authors, "keywords": keywords, "image": image, "language": language,
            "section": clean(section)[:80], "content_source": content_source if content else ""}
