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


def _is_price_part(lines: list[str], index: int) -> bool:
    neighbours = lines[max(0, index - 1) : index] + lines[index + 1 : index + 2]
    return any(_PRICE_NEIGHBOUR_RE.search(line) for line in neighbours)


def normalize_for_compare(text: str) -> str:
    """Текст страницы без шума — для решения «страница изменилась или нет»."""
    lines = text.splitlines()
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
            result.append(_DIGITS_RE.sub("#", line))
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
        return unified_diff(self.old_text or "", self.new_text or "")


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
