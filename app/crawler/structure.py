"""Структура страницы для SEO-разбора: мета-теги, H1, оглавление, основной текст.

Отдельно от текста для сравнения (diff.extract_text): тот склеивает весь видимый
текст и по нему ищутся изменения, менять его нельзя — иначе следующий обход объявит
изменёнными все страницы. Структура нужна для другого (обратная связь SEO-специалиста,
2026-10-19): понять, о чём статья конкурента и под какой запрос она написана, —
заголовок, описание, оглавление и сам текст статьи без меню, подвала и сайдбаров.

Как ищется основной текст (проверено на skysmart.ru/articles, onlineschool-1.ru/blog,
/questions, /glossary, og1.ru/polza, /novosti, /videouroki):
1. блок с itemprop="articleBody" (разметка статьи по schema.org);
2. иначе <article> с H1 страницы, а если такого нет — самый большой <article>:
   карточки «похожих статей» тоже бывают <article>, поэтому берётся только тот, где
   своего текста (не ссылок) хватает;
3. иначе <main>;
4. иначе блок с самым большим количеством текста в абзацах <p>;
5. иначе вся страница.
Из найденного блока убираются меню, формы, кнопки, вложенные карточки, «хлебные
крошки», «поделиться», комментарии и блоки, почти целиком состоящие из ссылок.
"""

import re
from dataclasses import asdict, dataclass, field

from bs4 import BeautifulSoup, Tag

from app.crawler.diff import strip_related_blocks, strip_review_blocks

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "canvas", "video", "audio")
# Внутри основного блока — то, что к тексту статьи не относится.
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form", "button", "select", "dialog")
# Классы/id служебных блоков: крошки, «поделиться», комментарии, подписка, реклама.
_CHROME_CLASS_RE = re.compile(
    r"breadcrumb|share|social|comment|subscri|banner|advert|promo|popup|modal|cookie|"
    r"related|recommend|similar|read-?also|more-?posts|sidebar|widget|rating|tags?(?:$|[-_])|"
    r"toc(?:$|[-_])|table-of-contents|oglavlenie|soderzhanie|author-card|post-nav|pagination",
    re.IGNORECASE,
)
# Свой текст (без ссылок) кандидата в основной блок — меньше этого считаем карточкой.
_MIN_ARTICLE_TEXT = 500
_MIN_MAIN_TEXT = 200
_MIN_H1_ARTICLE_TEXT = 50
# Блок, где ссылки — больше этой доли текста, это меню или подборка ссылок.
_LINK_HEAVY_SHARE = 0.6
_LINK_HEAVY_MIN_TEXT = 40
_HEADING_MAX = 300
_MAIN_TEXT_MAX = 100_000


@dataclass
class PageStructure:
    meta_title: str | None = None
    meta_description: str | None = None
    h1: str | None = None
    # Оглавление по порядку: (уровень 2 или 3, текст заголовка).
    headings: list[tuple[int, str]] = field(default_factory=list)
    main_text: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["headings"] = [[level, text] for level, text in self.headings]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PageStructure":
        return cls(
            meta_title=data.get("meta_title"),
            meta_description=data.get("meta_description"),
            h1=data.get("h1"),
            headings=[(int(level), str(text)) for level, text in data.get("headings") or []],
            main_text=data.get("main_text") or "",
        )


def _clean(text: str | None) -> str | None:
    text = " ".join((text or "").split())
    return text or None


def _meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str | None:
    tag = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
    if tag is None:
        # name="Description" тоже встречается
        wanted = (name or prop or "").lower()
        for candidate in soup.find_all("meta"):
            key = (candidate.get("name") or candidate.get("property") or "").lower()
            if key == wanted:
                tag = candidate
                break
    return _clean(tag.get("content")) if tag is not None else None


def _text_len(el: Tag) -> int:
    return len(el.get_text(" ", strip=True))


def _own_text_len(el: Tag) -> int:
    """Текст блока без текста ссылок — у карточек и меню он почти нулевой."""
    links = sum(len(a.get_text(" ", strip=True)) for a in el.find_all("a"))
    return _text_len(el) - links


def _densest_block(body: Tag) -> Tag | None:
    """Блок, в котором больше всего текста в собственных абзацах <p>."""
    scores: dict[int, tuple[int, Tag]] = {}
    for p in body.find_all("p"):
        parent = p.parent
        if not isinstance(parent, Tag):
            continue
        length = len(p.get_text(" ", strip=True))
        score, _ = scores.get(id(parent), (0, parent))
        scores[id(parent)] = (score + length, parent)
    if not scores:
        return None
    score, block = max(scores.values(), key=lambda item: item[0])
    return block if score >= _MIN_MAIN_TEXT else None


def _find_main(soup: BeautifulSoup) -> Tag:
    body = soup.body or soup
    for el in soup.find_all(attrs={"itemprop": "articleBody"}):
        if _own_text_len(el) >= _MIN_ARTICLE_TEXT:
            return el
    all_articles = soup.find_all("article")
    # <article> с заголовком страницы — сама статья, даже короткая (термин глоссария
    # onlineschool-1.ru — пара строк, а вокруг в <main> карусель отзывов и форма заявки).
    with_h1 = [el for el in all_articles if el.find("h1") and _own_text_len(el) >= _MIN_H1_ARTICLE_TEXT]
    if with_h1:
        return max(with_h1, key=_own_text_len)
    articles = [el for el in all_articles if _own_text_len(el) >= _MIN_ARTICLE_TEXT]
    if articles:
        return max(articles, key=_own_text_len)
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main is not None and _own_text_len(main) >= _MIN_MAIN_TEXT:
        return main
    return _densest_block(body) or body


def _class_text(el: Tag) -> str:
    classes = el.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    return " ".join([*classes, el.get("id") or ""])


def _strip_chrome(root: Tag) -> None:
    """Убирает из основного блока всё, что не текст статьи (на месте)."""
    total = max(1, _text_len(root))
    for el in root.find_all(_CHROME_TAGS):
        el.decompose()
    # Вложенные карточки (<article> внутри основного блока) — «похожие статьи».
    for el in root.find_all("article"):
        if _own_text_len(el) < _MIN_ARTICLE_TEXT:
            el.decompose()
    for el in root.find_all(True):
        if el.decomposed or el.attrs is None:
            continue
        # Обёртку всей статьи с «подходящим» классом не трогаем — только мелкие блоки.
        if _CHROME_CLASS_RE.search(_class_text(el)) and _text_len(el) < total * 0.3:
            el.decompose()
    for el in root.find_all(["div", "ul", "ol", "section", "p", "table"]):
        if el.decomposed or el.attrs is None:
            continue
        length = _text_len(el)
        if length < _LINK_HEAVY_MIN_TEXT or length > total * 0.8:
            continue
        links = sum(len(a.get_text(" ", strip=True)) for a in el.find_all("a"))
        if links / length > _LINK_HEAVY_SHARE:
            el.decompose()


def _lines(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


_BLOCK_TAGS = (
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "td", "th", "section",
    "article", "ul", "ol", "table", "blockquote", "dd", "dt", "figcaption", "main", "pre",
)


def _block_text(root: Tag) -> str:
    """Текст блока: абзацы — отдельными строками, а ссылки и выделение внутри абзаца
    не рвут предложение (get_text с разделителем «\\n» резал «…заниматься с |
    преподавателем | ,» на три строки)."""
    for el in root.find_all(_BLOCK_TAGS):
        el.insert_before("\n")
        el.append("\n")
    return _lines(root.get_text())


_RELATED_HEADING_RE = re.compile(
    r"^(?:похожие|популярные|новые|последние|свежие|другие|интересные|рекомендуемые|читайте также|"
    r"смотрите также|вам может быть интересно|может быть интересно|рекомендуем|комментари|содержание|оглавление|"
    r"поделиться|подпишитесь|отзывы|"
    # рекламные заголовки форм и баннеров внутри статьи
    r"попробуйте|запишитесь|записаться|оставьте заявку|получите консультацию)",
    re.IGNORECASE,
)


def _headings(root: Tag) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for el in root.find_all(["h2", "h3"]):
        text = _clean(el.get_text(" ", strip=True))
        if not text or _RELATED_HEADING_RE.match(text):
            continue
        result.append((int(el.name[1]), text[:_HEADING_MAX]))
    return result


def extract_structure(html: str) -> PageStructure:
    """Мета-теги, H1, оглавление H2/H3 и основной текст страницы."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()

    title_tag = soup.find("title")
    meta_title = _clean(title_tag.get_text()) if title_tag else None
    meta_title = meta_title or _meta(soup, prop="og:title")
    description = _meta(soup, name="description") or _meta(soup, prop="og:description")

    h1_tag = soup.find("h1")
    h1 = _clean(h1_tag.get_text(" ", strip=True)) if h1_tag else None

    root = _find_main(soup)
    _strip_chrome(root)
    headings = _headings(root)
    text = _block_text(root)
    text = strip_review_blocks(strip_related_blocks(text))

    return PageStructure(
        meta_title=meta_title,
        meta_description=description,
        h1=h1[:_HEADING_MAX] if h1 else None,
        headings=headings,
        main_text=text[:_MAIN_TEXT_MAX],
    )


_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


def structure_from_apify_item(item: dict) -> PageStructure:
    """Структура из ответа Apify (website-content-crawler).

    HTML актор по умолчанию не отдаёт, зато даёт metadata.title/description, текст уже
    без меню и подвала (его собственная чистка) и markdown, где заголовки — строки
    «# …», «## …». H1 и оглавление берём оттуда; markdown нет — только мета-теги и текст.
    """
    metadata = item.get("metadata") or {}
    h1 = None
    headings: list[tuple[int, str]] = []
    for line in (item.get("markdown") or "").splitlines():
        match = _MARKDOWN_HEADING_RE.match(line.strip())
        if not match:
            continue
        level, text = len(match.group(1)), _clean(match.group(2).replace("*", ""))
        if not text:
            continue
        if level == 1:
            h1 = h1 or text[:_HEADING_MAX]
        elif not _RELATED_HEADING_RE.match(text):
            headings.append((level, text[:_HEADING_MAX]))
    text = _lines(item.get("text") or "")
    return PageStructure(
        meta_title=_clean(metadata.get("title") or item.get("title")),
        meta_description=_clean(metadata.get("description")),
        h1=h1,
        headings=headings,
        main_text=strip_review_blocks(strip_related_blocks(text))[:_MAIN_TEXT_MAX],
    )
