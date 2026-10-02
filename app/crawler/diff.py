"""Движок диффа по content-hash: сравнение предыдущего и текущего обхода конкурента.

Работает на чистых структурах (url -> нормализованный текст), не зависит от БД —
это позволяет тестировать классификацию new/changed/removed на фикстурах.
"""

import difflib
import hashlib
import re
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


def normalize_for_compare(text: str) -> str:
    """Текст страницы без шума — для решения «страница изменилась или нет»."""
    lines = strip_related_blocks(text).splitlines()
    result = []
    for index, line in enumerate(lines):
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

    @property
    def diff_text(self) -> str | None:
        if self.change_type is not ChangeType.CHANGED:
            return None
        # Без подборок статей: иначе ИИ и отчёт видят их перетасовку как «изменение».
        return unified_diff(strip_related_blocks(self.old_text or ""), strip_related_blocks(self.new_text or ""))


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
