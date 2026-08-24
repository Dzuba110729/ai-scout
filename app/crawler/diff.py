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
    changed (url есть в обоих, но content_hash отличается). Страницы без
    изменений в результат не попадают.
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
        if content_hash(old_text) != content_hash(new_text):
            results.append(
                PageDiff(url=url, change_type=ChangeType.CHANGED, old_text=old_text, new_text=new_text)
            )

    return results
