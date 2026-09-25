"""Сравнение находки у конкурента с нашим собственным сайтом.

Отвечает на главный вопрос владельца: «есть ли такое у нас, а если похожее —
чем именно отличается и чего нам не хватает».

Кандидаты для сравнения подбираются дёшево — без векторных баз, эмбеддингов и
внешних сервисов: обычное сравнение слов (TF-IDF + косинусная близость) с грубым
отсечением русских окончаний, иначе «курсы» и «курс» не совпадут. Точность
подбора тут не важна: вывод всё равно делает ИИ, а подбор лишь сужает выбор до
трёх наших страниц, которые поместятся в один промпт.

ИИ вызывается через Claude Code CLI (`claude -p`), как и весь остальной анализ —
платный Anthropic API в проекте не используется (см. CLAUDE.md).
"""

import asyncio
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass

from app.ai.analyze import extract_json_object, run_claude_cli
from app.config import settings
from app.crawler.diff import ChangeType, PageDiff

logger = logging.getLogger(__name__)

VERDICT_EXACT = "exact"
VERDICT_SIMILAR = "similar"
VERDICT_NONE = "none"

VERDICT_LABELS = {
    VERDICT_EXACT: "У нас такое есть",
    VERDICT_SIMILAR: "У нас похожее есть",
    VERDICT_NONE: "У нас такого нет",
}

_WORD_RE = re.compile(r"[а-яёa-z0-9]+")

# Слова, которые встречаются на любой странице и только зашумляют сравнение.
_STOPWORDS = frozenset(
    """
    без более больше будет будто был была были было быть вам вас ваш ваша ваши вдруг ведь весь вот все
    всего всех всю вы где да даже для до его ему если есть ещё еще же за здесь или им их как какой когда
    кто ли лучше между меня мне много может можно мой мы на над надо наш наша наши не него неё нее нет
    ни них но ну об однако он она они оно от очень перед по под после потом потому почти при про раз
    разве сам свой своя себе себя сейчас скидка так такой там те тем теперь то тоже только том тот три
    тут ты уже хоть чего чем через что чтобы эти этой этом этот эту явно
    """.split()
)

# Грубое отсечение окончаний вместо морфологического анализатора: тянуть pymorphy
# ради подбора трёх кандидатов не стоит. Порядок важен — сначала длинные окончания.
_ENDINGS = tuple(
    sorted(
        (
            "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ость", "ация", "ение", "ания",
            "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ей", "ам", "ям", "ах", "ях",
            "ов", "ев", "ию", "ия", "ии", "ем", "ом", "ут", "ют", "ат", "ят", "ла", "ло", "ли",
            "а", "я", "о", "е", "у", "ю", "ы", "и", "ь", "й",
        ),
        key=len,
        reverse=True,
    )
)

# Короче четырёх букв основу не режем: иначе «курс» превратится в «кур».
_MIN_STEM_LENGTH = 4

# Заголовок описывает страницу точнее, чем случайное слово в подвале, поэтому
# слова из заголовка считаются несколько раз.
_TITLE_WEIGHT = 3


@dataclass(frozen=True)
class OwnPage:
    """Страница нашего сайта — кандидат на сравнение."""

    url: str
    title: str | None
    text: str


@dataclass(frozen=True)
class OwnSite:
    """Наш сайт с уже загруженными страницами (готов к подбору кандидатов)."""

    base_url: str
    pages: list[OwnPage]


@dataclass(frozen=True)
class SimilarPage:
    page: OwnPage
    score: float


@dataclass
class ComparisonResult:
    verdict: str
    our_url: str | None
    differences: str | None
    missing: str | None
    raw_response: dict

    @property
    def label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, VERDICT_LABELS[VERDICT_NONE])


def stem(word: str) -> str:
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= _MIN_STEM_LENGTH:
            return word[: -len(ending)]
    return word


def tokenize(text: str) -> list[str]:
    """Слова страницы, приведённые к грубой основе, без стоп-слов и коротышей."""
    words = _WORD_RE.findall(text.lower().replace("ё", "е"))
    return [stem(word) for word in words if len(word) > 2 and word not in _STOPWORDS]


def _term_counts(title: str | None, text: str) -> Counter:
    counts = Counter(tokenize(text))
    if title:
        for token in tokenize(title):
            counts[token] += _TITLE_WEIGHT
    return counts


@dataclass(frozen=True)
class _OwnSiteIndex:
    """Наш сайт, уже разобранный на слова: веса слов и векторы страниц."""

    idf: dict[str, float]
    vectors: list[dict[str, float]]
    norms: list[float]


# Разбор всего нашего сайта (тысячи страниц) — самая тяжёлая часть подбора. Раньше он
# повторялся для КАЖДОЙ страницы конкурента: 50 сравнений первого обхода = 50 разборов
# og1.ru подряд, и сервис на ~10 минут переставал отвечать (обходы, Telegram, веб).
# Теперь разбор один на список наших страниц; lock — чтобы параллельные потоки не
# строили его одновременно.
_index_lock = threading.Lock()
_cached_index: tuple[list[OwnPage], _OwnSiteIndex] | None = None


def _own_site_index(own_pages: list[OwnPage]) -> _OwnSiteIndex:
    global _cached_index
    with _index_lock:
        if _cached_index is not None and _cached_index[0] is own_pages:
            return _cached_index[1]

        documents = [_term_counts(page.title, page.text) for page in own_pages]
        document_frequency: Counter = Counter()
        for document in documents:
            document_frequency.update(document.keys())

        total = len(documents)
        idf = {
            token: math.log((total + 1) / (freq + 1)) + 1.0
            for token, freq in document_frequency.items()
        }
        vectors = [{token: count * idf[token] for token, count in document.items()} for document in documents]
        norms = [math.sqrt(sum(value * value for value in vector.values())) for vector in vectors]
        index = _OwnSiteIndex(idf=idf, vectors=vectors, norms=norms)
        _cached_index = (own_pages, index)
        return index


def find_similar_pages(
    title: str | None, text: str, own_pages: list[OwnPage], limit: int = 3
) -> list[SimilarPage]:
    """Наши страницы, ближе всего похожие на страницу конкурента.

    Редкие слова («репетитор») весят больше частых («страница») — это и даёт TF-IDF.
    Страницы без единого общего значимого слова не возвращаются вовсе: гнать их в
    ИИ бессмысленно.
    """
    if not own_pages or limit <= 0:
        return []

    index = _own_site_index(own_pages)

    query_counts = _term_counts(title, text)
    # Слова, которых нет ни на одной нашей странице, в сравнении не участвуют.
    query_vector = {
        token: count * index.idf[token] for token, count in query_counts.items() if token in index.idf
    }
    query_norm = math.sqrt(sum(value * value for value in query_vector.values()))
    if not query_norm:
        return []

    scored: list[SimilarPage] = []
    for page, vector, norm in zip(own_pages, index.vectors, index.norms, strict=True):
        if not norm:
            continue
        dot = sum(value * vector.get(token, 0.0) for token, value in query_vector.items())
        if dot > 0:
            scored.append(SimilarPage(page=page, score=dot / (query_norm * norm)))

    # url в ключе сортировки — чтобы порядок был одинаковым при равных оценках.
    scored.sort(key=lambda item: (-item.score, item.page.url))
    return scored[:limit]


_COMPARISON_PROMPT = (
    "Ты маркетолог-аналитик. Сравниваешь страницу конкурента с нашим собственным сайтом.\n"
    "Наш сайт: {own_base_url}\n\n"
    "СТРАНИЦА КОНКУРЕНТА\n"
    "Адрес: {url}\n"
    "Текст:\n"
    "```\n"
    "{competitor_text}\n"
    "```\n\n"
    "НАШИ СТРАНИЦЫ, ПОХОЖИЕ ПО СЛОВАМ (подобраны автоматически — среди них может "
    "не оказаться подходящей, тогда так и напиши)\n"
    "{candidates}\n\n"
    "Ответь СТРОГО одним JSON-объектом, без пояснений вне JSON:\n"
    "{{\n"
    '  "verdict": "exact — у нас есть такая же страница по смыслу; '
    'similar — есть похожая, но с отличиями; none — ничего похожего у нас нет",\n'
    '  "our_url": "адрес нашей самой близкой страницы из списка выше, или null если ничего не подходит",\n'
    '  "differences": "чем страница конкурента отличается от нашей: цены, условия, '
    'блоки, формулировки, чего у них больше и чего меньше. Конкретно, 2-4 предложения. '
    'Если у нас ничего похожего нет — null",\n'
    '  "missing": "чего нам не хватает по сравнению с конкурентом и что стоит добавить. '
    '1-3 предложения, или null если добавлять нечего"\n'
    "}}\n\n"
    "Пиши простым русским языком, как для человека без маркетингового образования: "
    "без сокращений и терминов вроде «УТП», «конверсия», «оффер».\n"
)


def _format_candidates(candidates: list[SimilarPage], text_limit: int) -> str:
    blocks = []
    for number, candidate in enumerate(candidates, start=1):
        page = candidate.page
        blocks.append(
            f"--- Наша страница {number} ---\n"
            f"Адрес: {page.url}\n"
            f"Заголовок: {page.title or 'не указан'}\n"
            f"Текст:\n```\n{page.text[:text_limit]}\n```"
        )
    return "\n\n".join(blocks)


def build_comparison_prompt(
    url: str, competitor_text: str, own_site: OwnSite, candidates: list[SimilarPage]
) -> str:
    text_limit = settings.own_site_compare_text_limit
    return _COMPARISON_PROMPT.format(
        own_base_url=own_site.base_url,
        url=url,
        competitor_text=competitor_text[:text_limit],
        candidates=_format_candidates(candidates, text_limit),
    )


def parse_comparison_response(raw_text: str) -> ComparisonResult:
    data = extract_json_object(raw_text)

    our_url = data.get("our_url") or None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICT_LABELS:
        # ИИ иногда отвечает фразой вместо кода. Ссылка на нашу страницу — более
        # надёжный признак, чем неразобранный текст: раз страница названа, похожее есть.
        verdict = VERDICT_SIMILAR if our_url else VERDICT_NONE

    return ComparisonResult(
        verdict=verdict,
        our_url=our_url,
        differences=data.get("differences") or None,
        missing=data.get("missing") or None,
        raw_response=data,
    )


def competitor_text_for_comparison(page_diff: PageDiff) -> str | None:
    """Текст страницы конкурента для сравнения.

    Для изменившейся страницы берём её нынешний текст целиком, а не дифф: вопрос
    «есть ли такое у нас» задаётся про страницу, а не про правку в ней.
    """
    if page_diff.change_type is ChangeType.REMOVED:
        return None
    return page_diff.new_text or None


async def compare_with_own_site(page_diff: PageDiff, own_site: OwnSite) -> ComparisonResult | None:
    """Один вызов `claude -p` на изменение. None — сравнивать было не с чем."""
    competitor_text = competitor_text_for_comparison(page_diff)
    if not competitor_text or not own_site.pages:
        return None

    # В отдельном потоке: подбор — чистая работа процессора, и в общем event loop он
    # замораживал бы все обходы, бота и веб, пока считается.
    candidates = await asyncio.to_thread(
        find_similar_pages, None, competitor_text, own_site.pages, settings.own_site_compare_candidates
    )
    if not candidates:
        # Ни одного общего значимого слова со всем нашим сайтом — тратить вызов ИИ
        # не на что, «похожего нет» и так очевидно.
        logger.info("Нечего сравнивать с нашим сайтом для %s — нет похожих страниц", page_diff.url)
        return None

    raw_text = await run_claude_cli(build_comparison_prompt(page_diff.url, competitor_text, own_site, candidates))
    return parse_comparison_response(raw_text)
