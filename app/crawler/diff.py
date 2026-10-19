"""Движок диффа по content-hash: сравнение предыдущего и текущего обхода конкурента.

Работает на чистых структурах (url -> нормализованный текст), не зависит от БД —
это позволяет тестировать классификацию new/changed/removed на фикстурах.
"""

import difflib
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum

from bs4 import BeautifulSoup

_NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "nav", "header", "footer")


def extract_text(html: str) -> str:
    """Извлекает видимый текст страницы, отбрасывая скрипты/стили/разметку."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = (line.strip() for line in text.splitlines())
    normalized = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", normalized)


# Шум, который меняется при каждой загрузке и не несёт смысла для слежки за
# конкурентом. Сохранённый текст не трогаем — только сравниваем без него, поэтому
# старые снимки не нужно пересобирать.
# Плашка «мы используем cookies»: то показывается, то нет. Текст плашки бывает
# разбит на строки: «…вы соглашаетесь с тем, что мы используем» + «cookies».
_COOKIE_LINE_RE = re.compile(
    r"cookie|куки|соглашаетесь с тем, что мы используем$", re.IGNORECASE
)
_COOKIE_LINE_MAX = 300
_CONSENT_BUTTONS = {"ok", "ок", "принять", "хорошо", "понятно", "согласен", "согласна"}
# Строка из одного числа — счётчик просмотров у статьи: «5313», «2736.3K», «1,2 тыс.».
_BARE_NUMBER_RE = re.compile(r"^\d[\d \u00a0]*(?:[.,]\d+)?\s?(?:[KkMmКкМм]|тыс\.?)?$")
# ...но если рядом валюта или «от/до», это цена или скидка, её сравниваем честно.
_PRICE_NEIGHBOUR_RE = re.compile(r"^(?:₽|руб|р\.|%|\$|€)|(?:^|\s)(?:от|до|за)$", re.IGNORECASE)
# Тикающие счётчики: «Уже 1 053 человек записались за 24 часа», «Уже идёт 20 дней».
_TICKER_RE = re.compile(r"^Уже\b.*\d", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d[\d \u00a0]*")
# «Уже идёт 5 дней» -> «Уже идёт 21 день»: после замены цифр разнится ещё и окончание.
_TIME_UNIT_RE = re.compile(r"\b(?:дн(?:ей|я|ь)|день|час(?:а|ов)?|минут[аы]?|секунд[аы]?)\b", re.IGNORECASE)
# Таймер обратного отсчёта «04:52» / «1:04:52» и подписи к его цифрам («дня», «минуты»).
_TIMER_RE = re.compile(r"^\d{1,2}(?::\d{2}){1,2}$")
# Время чтения статьи «12 мин» — пересчитывается при малейшей правке текста.
_READING_TIME_RE = re.compile(r"^\d+\s?мин\.?$", re.IGNORECASE)


def _is_price_part(lines: list[str], index: int) -> bool:
    neighbours = lines[max(0, index - 1) : index] + lines[index + 1 : index + 2]
    return any(_PRICE_NEIGHBOUR_RE.search(line) for line in neighbours)


# Подборки статей под статьёй и в блоге («Похожие статьи», «Популярные статьи»,
# «Новые статьи», «Читайте также»): при каждой загрузке в них другие статьи и другие
# счётчики, а о конкуренте они ничего не говорят. Блок — заголовок и идущие за ним
# карточки, каждая карточка заканчивается датой («23 сентября 2026»). Блок кончается,
# когда даты перестают встречаться. Вырезаем по тексту, а не по HTML, — так одинаково
# чистятся и новые, и уже сохранённые снимки, без пересборки базы.
_RELATED_HEADING_RE = re.compile(
    r"^(?:похожие|популярные|новые|последние|свежие|другие|интересные|рекомендуемые)"
    r"\s+(?:статьи|материалы|записи|публикации|новости)$"
    r"|^(?:читайте|читать)\s+(?:также|ещё|еще)$"
    r"|^(?:вам может быть интересно|может быть интересно|рекомендуем прочитать)$",
    re.IGNORECASE,
)
_CARD_DATE_RE = re.compile(
    r"^\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября"
    r"|ноября|декабря)\s+\d{4}(?:\s*г\.?)?$|^\d{2}\.\d{2}\.\d{4}$",
    re.IGNORECASE,
)
# Сколько строк может быть в одной карточке (рубрика, заголовок, время чтения,
# просмотры, дата — 5). Если до первой/следующей даты дальше — это уже не подборка.
_CARD_MAX_LINES = 8


def strip_related_blocks(text: str) -> str:
    """Убирает из текста страницы подборки «похожих/популярных/новых статей»."""
    lines = text.splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        if not _RELATED_HEADING_RE.match(lines[index].strip()):
            result.append(lines[index])
            index += 1
            continue
        block_end = None  # индекс последней даты в блоке
        cursor = index + 1
        while cursor < len(lines) and cursor - (block_end if block_end is not None else index) <= _CARD_MAX_LINES:
            if _CARD_DATE_RE.match(lines[cursor].strip()):
                block_end = cursor
            cursor += 1
        if block_end is None:
            # Похоже на заголовок, но карточек с датами за ним нет — не трогаем.
            result.append(lines[index])
            index += 1
            continue
        index = block_end + 1
    return "\n".join(result)


# Карусель отзывов (skysmart.ru, октябрь 2026): «Отзывы о преподавателях, ведущих
# курсы», дальше карточки «имя — дата — оценка — текст — Отзыв о преподавателе —
# Наталье — на курс — Подготовка к ЕГЭ». При каждой загрузке в ней другие отзывы, и
# она одна давала больше сотни «изменений» за обход. SEO-специалисту новые отзывы
# конкурента не нужны (обратная связь 2026-10-19) — вырезаем блок из сравнения.
_REVIEWS_HEADING_RE = re.compile(
    r"^(?:отзывы|что говорят|что пишут|мнения)\b.{0,80}$|^\d[\d  ]*\s+отзыв\w*$", re.IGNORECASE
)
# Сколько строк может быть между датами соседних карточек отзывов (и от заголовка
# до первой даты: перед ней бывают рейтинг, число оценок, имя автора).
_REVIEW_CARD_MAX_LINES = 15
# Счётчик отзывов и оценок: «1503» + «отзыва», «1 503 отзыва», «Отзывы (1503)».
_REVIEW_WORD_RE = re.compile(r"^(?:отзыв(?:ов|а|ы)?|оцен(?:ок|ки|ка))$", re.IGNORECASE)
_REVIEW_COUNT_RE = re.compile(
    r"^\d[\d  ]*\s*(?:отзыв|оцен)\w*$|^(?:отзыв|оцен)\w*\s*\(?\s*\d[\d  ]*\s*\)?$", re.IGNORECASE
)


def _review_block_end(lines: list[str], dates: list[int]) -> int:
    """Индекс последней строки ленты отзывов (в ней не меньше двух карточек с датами).

    Конец последней карточки по дате не виден, а длина карточек разная: текст отзыва
    бывает в несколько строк. Поэтому ищем «якорь» — строку, которая есть ровно один
    раз в каждой карточке и всегда на одном расстоянии до даты следующей («на курс»
    у skysmart). Между якорем и следующей датой — хвост карточки и шапка следующей
    (буква-аватар и имя автора); шапку вычитаем, остаток после якоря и есть хвост.
    """
    spans = [(dates[k], dates[k + 1]) for k in range(len(dates) - 1)]
    head = 2 if all(len(lines[date - 2].strip()) <= 2 for date in dates if date >= 2) else 1

    anchors: dict[str, int] | None = None
    for start, end in spans:
        counts = Counter(line.strip() for line in lines[start + 1 : end])
        distances = {
            lines[pos].strip(): end - pos for pos in range(start + 1, end) if counts[lines[pos].strip()] == 1
        }
        anchors = distances if anchors is None else {
            text: dist for text, dist in anchors.items() if distances.get(text) == dist
        }
    last = dates[-1]
    # Якорь должен быть и в последней карточке; ближайший к её концу — точнее всего.
    in_last = {}
    for pos in range(last + 1, min(len(lines), last + _REVIEW_CARD_MAX_LINES)):
        in_last.setdefault(lines[pos].strip(), pos)
    found = [(distance, in_last[text]) for text, distance in (anchors or {}).items() if text in in_last]
    if not found:
        return last
    distance, pos = min(found)
    return min(len(lines) - 1, pos + max(0, distance - 1 - head))


def strip_review_blocks(text: str) -> str:
    """Убирает из текста страницы ленты отзывов с датами (карусели на страницах курсов)."""
    lines = text.splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        if not _REVIEWS_HEADING_RE.match(lines[index].strip()):
            result.append(lines[index])
            index += 1
            continue
        dates: list[int] = []
        cursor = index + 1
        while cursor < len(lines) and cursor - (dates[-1] if dates else index) <= _REVIEW_CARD_MAX_LINES:
            if _CARD_DATE_RE.match(lines[cursor].strip()):
                dates.append(cursor)
            cursor += 1
        if len(dates) < 2:
            # Одна дата после «Отзывы» — скорее обычный текст (старт курса), не лента.
            result.append(lines[index])
            index += 1
            continue
        index = _review_block_end(lines, dates) + 1
    return "\n".join(result)


def strip_rotating_blocks(text: str) -> str:
    """Без блоков, которые меняются от загрузки к загрузке: подборки статей и отзывы."""
    return strip_review_blocks(strip_related_blocks(text))


def normalize_for_compare(text: str) -> str:
    """Текст страницы без шума — для решения «страница изменилась или нет»."""
    lines = strip_rotating_blocks(text).splitlines()
    result = []
    for index, line in enumerate(lines):
        if _REVIEW_COUNT_RE.match(line):
            result.append("# отзывы")
            continue
        if _REVIEW_WORD_RE.match(line) and index and _BARE_NUMBER_RE.match(lines[index - 1]):
            result.append("отзывы")
            continue
        if _COOKIE_LINE_RE.search(line) and len(line) <= _COOKIE_LINE_MAX:
            continue
        if line.lower() in _CONSENT_BUTTONS:
            continue
        if _BARE_NUMBER_RE.match(line) and not _is_price_part(lines, index):
            result.append("#")
            continue
        if _TICKER_RE.match(line):
            result.append(_TIME_UNIT_RE.sub("…", _DIGITS_RE.sub("#", line)))
            continue
        if _TIMER_RE.match(line) or _READING_TIME_RE.match(line):
            result.append("#")
            continue
        if _TIME_UNIT_RE.fullmatch(line):
            result.append("…")
            continue
        result.append(line)
    return "\n".join(result)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unified_diff(old_text: str, new_text: str, context_lines: int = 2) -> str:
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile="было",
        tofile="стало",
        lineterm="",
        n=context_lines,
    )
    return "\n".join(diff)


class ChangeType(str, Enum):
    NEW = "new"
    CHANGED = "changed"
    REMOVED = "removed"


@dataclass(frozen=True)
class PageDiff:
    url: str
    change_type: ChangeType
    old_text: str | None = None
    new_text: str | None = None
    # Только то, что поменялось именно на этой странице, без сквозных блоков (см.
    # app/crawler/sitewide.py) — для ИИ. None — сквозных блоков на странице нет.
    own_diff_text: str | None = None

    @property
    def diff_text(self) -> str | None:
        if self.change_type is not ChangeType.CHANGED:
            return None
        # Без подборок статей и лент отзывов: иначе ИИ и отчёт видят их перетасовку как «изменение».
        return unified_diff(strip_rotating_blocks(self.old_text or ""), strip_rotating_blocks(self.new_text or ""))


def diff_crawl(
    previous: dict[str, str],
    current: dict[str, str],
) -> list[PageDiff]:
    """Сравнивает предыдущий и текущий обход конкурента.

    previous / current: {url: нормализованный_текст_страницы}.
    Возвращает список изменений: new (появился url), removed (пропал url),
    changed (url есть в обоих, и текст отличается не только шумом — см.
    normalize_for_compare). Страницы без изменений в результат не попадают.
    """
    results: list[PageDiff] = []

    previous_urls = set(previous)
    current_urls = set(current)

    for url in sorted(current_urls - previous_urls):
        results.append(PageDiff(url=url, change_type=ChangeType.NEW, new_text=current[url]))

    for url in sorted(previous_urls - current_urls):
        results.append(PageDiff(url=url, change_type=ChangeType.REMOVED, old_text=previous[url]))

    for url in sorted(previous_urls & current_urls):
        old_text = previous[url]
        new_text = current[url]
        if normalize_for_compare(old_text) != normalize_for_compare(new_text):
            results.append(
                PageDiff(url=url, change_type=ChangeType.CHANGED, old_text=old_text, new_text=new_text)
            )

    return results
