"""Сквозные изменения: один и тот же блок поменялся сразу на многих страницах.

Пример из жизни (skysmart.ru, октябрь 2026): баннер «Скидка до 50% на все курсы,
промокод СМОГУ» и «3 урока в подарок» появились на 15 страницах курсов. Раньше это
было 15 строк отчёта и 15 вызовов ИИ с одним и тем же выводом; SEO-специалист сам
сводил их в одно. Теперь такой блок — одна находка «сквозное изменение на N
страницах» с парой примеров, а у страницы остаётся только то, что поменялось
именно на ней.

Как находим. Изменённые строки каждой страницы (без шума — normalize_for_compare)
режем на «черепицу» из трёх подряд идущих строк: текст тут мелко порезан («50»,
«%», «на все курсы»), и одиночная строка вроде «%» ничего не значит, а три подряд —
уже узнаваемый кусок. Кусок, который встретился на SITEWIDE_MIN_PAGES страницах и
больше, — сквозной. Куски с почти одинаковым набором страниц (баннер из десятка
строк) склеиваются в один блок.
"""

import difflib
import re
from dataclasses import dataclass, field

from app.crawler.diff import ChangeType, PageDiff, normalize_for_compare

_SHINGLE_SIZE = 3
# Кусок без букв («#», «%», «:») — не блок, а обломок цен и таймеров.
_MIN_SHINGLE_LETTERS = 4
# Наборы страниц, совпадающие на 80%, — один и тот же блок (на паре страниц он мог
# разойтись с соседним текстом иначе и не попасть в кусок целиком).
_MERGE_JACCARD = 0.8
# Сколько букв должно остаться у страницы помимо сквозного блока, чтобы это была
# своя находка: «Акция закончилась» (16) — да, «Узнайте свой у» (12) — нет.
_MIN_OWN_LETTERS = 15
# Сколько строк блока показывать ИИ: больше не нужно, чтобы понять, что за блок.
_GROUP_LINES_LIMIT = 60
_LETTER_RE = re.compile(r"[a-zа-яё]", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d[\d \u00a0]*")

Shingle = tuple[str, ...]


@dataclass
class SitewideGroup:
    """Сквозной блок: какие страницы задел и как выглядит (строки «+…»/«-…»)."""

    urls: list[str]
    lines: list[str]

    @property
    def diff_text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class SitewideSplit:
    groups: list[SitewideGroup] = field(default_factory=list)
    # url -> что изменилось ТОЛЬКО на этой странице (строки «+…»/«-…»), или None,
    # если всё изменение страницы — сквозное. Страниц без сквозных блоков тут нет:
    # их diff остаётся как был.
    own_changes: dict[str, str | None] = field(default_factory=dict)

    def is_fully_sitewide(self, url: str) -> bool:
        return url in self.own_changes and self.own_changes[url] is None


def _change_runs(old_text: str, new_text: str) -> list[list[str]]:
    """Подряд идущие изменённые строки: сначала удалённые «-», потом добавленные «+»."""
    old = normalize_for_compare(old_text).splitlines()
    new = normalize_for_compare(new_text).splitlines()
    runs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag == "equal":
            continue
        runs.append([f"-{line}" for line in old[i1:i2]] + [f"+{line}" for line in new[j1:j2]])
    return runs


def _shingles(run: list[str]) -> list[tuple[int, Shingle]]:
    """(позиция начала, кусок). Короткий кусок — целиком.

    Цифры в куске не различаем: «Скидка 30%, 10 400 ₽» и «Скидка 30%, 8 800 ₽» на
    разных курсах — одна и та же акция, просто цены у курсов свои.
    """
    masked = [_DIGITS_RE.sub("#", line) for line in run]
    if len(masked) <= _SHINGLE_SIZE:
        return [(0, tuple(masked))]
    return [
        (start, tuple(masked[start : start + _SHINGLE_SIZE])) for start in range(len(masked) - _SHINGLE_SIZE + 1)
    ]


def _meaningful(shingle: Shingle) -> bool:
    return len(_LETTER_RE.findall("".join(shingle))) >= _MIN_SHINGLE_LETTERS


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b)


def _drop_moved_lines(lines: list[str]) -> list[str]:
    """Строка, которая и удалена, и добавлена на той же странице, — просто сдвинулась."""
    removed = {line[1:] for line in lines if line.startswith("-")}
    added = {line[1:] for line in lines if line.startswith("+")}
    moved = removed & added
    return [line for line in lines if line[1:] not in moved]


def _group_lines(by_url: dict[str, list[str]]) -> list[str]:
    """Строки блока со всех его страниц: самые частые первыми, с числом страниц.

    Одной страницы-образца мало: в блок могли склеиться две части одной акции
    («скидка до 40%» на 66 страницах и «скидка 10% → 30%» на 17 из них).
    """
    pages_by_key: dict[str, int] = {}
    shown: dict[str, str] = {}
    for url in sorted(by_url):
        keys = {}
        for line in by_url[url]:
            keys.setdefault(_DIGITS_RE.sub("#", line), line)
        for key, line in keys.items():
            shown.setdefault(key, line)
            pages_by_key[key] = pages_by_key.get(key, 0) + 1
    # Строки со словами — первыми: голые «26 400» и «₽» без них ничего не говорят.
    # sort стабилен: при равной частоте — порядок на странице.
    ordered = sorted(shown, key=lambda key: (not _LETTER_RE.search(key), -pages_by_key[key]))
    total = len(by_url)
    return [
        shown[key] if pages_by_key[key] == total else f"{shown[key]}   [на {pages_by_key[key]} стр. из {total}]"
        for key in ordered[:_GROUP_LINES_LIMIT]
    ]


def find_sitewide_changes(diffs: list[PageDiff], min_pages: int) -> SitewideSplit:
    """Ищет сквозные блоки среди изменённых страниц обхода.

    min_pages <= 1 — склейка выключена (вернётся пустой результат).
    """
    changed = [d for d in diffs if d.change_type is ChangeType.CHANGED]
    if min_pages <= 1 or len(changed) < min_pages:
        return SitewideSplit()

    runs_by_url: dict[str, list[list[str]]] = {}
    pages_by_shingle: dict[Shingle, set[str]] = {}
    for diff in changed:
        runs = _change_runs(diff.old_text or "", diff.new_text or "")
        runs_by_url[diff.url] = runs
        for run in runs:
            for _start, shingle in _shingles(run):
                pages_by_shingle.setdefault(shingle, set()).add(diff.url)

    sitewide = {
        shingle: frozenset(urls)
        for shingle, urls in pages_by_shingle.items()
        if len(urls) >= min_pages and _meaningful(shingle)
    }
    if not sitewide:
        return SitewideSplit()

    # Склеиваем куски в блоки: (1) соседние куски одного изменённого места на
    # странице — это один баннер или одна карточка цены; (2) куски с почти одинаковым
    # набором страниц — тот же блок, на паре страниц разошедшийся с соседним текстом.
    parent = {shingle: shingle for shingle in sitewide}

    def find(shingle: Shingle) -> Shingle:
        while parent[shingle] != shingle:
            parent[shingle] = parent[parent[shingle]]
            shingle = parent[shingle]
        return shingle

    def union(a: Shingle, b: Shingle) -> None:
        parent[find(a)] = find(b)

    for runs in runs_by_url.values():
        for run in runs:
            previous: Shingle | None = None
            for _start, shingle in _shingles(run):
                if shingle not in sitewide:
                    previous = None
                    continue
                if previous is not None:
                    union(previous, shingle)
                previous = shingle

    representative_of_set: dict[frozenset[str], Shingle] = {}
    for shingle, page_set in sitewide.items():
        if page_set in representative_of_set:
            union(shingle, representative_of_set[page_set])
        else:
            representative_of_set[page_set] = shingle
    page_sets = sorted(representative_of_set, key=lambda s: (-len(s), sorted(s)))
    for i, big in enumerate(page_sets):
        for small in page_sets[i + 1 :]:
            if _jaccard(big, small) >= _MERGE_JACCARD:
                union(representative_of_set[small], representative_of_set[big])

    roots: dict[Shingle, int] = {}
    cluster_of_shingle = {shingle: roots.setdefault(find(shingle), len(roots)) for shingle in sitewide}

    split = SitewideSplit()
    # cluster -> url -> строки страницы, попавшие в этот блок (по порядку)
    cluster_lines: dict[int, dict[str, list[str]]] = {}
    for url, runs in runs_by_url.items():
        own_lines: list[str] = []
        touched = False
        for run in runs:
            covered: list[int | None] = [None] * len(run)
            for start, shingle in _shingles(run):
                cluster = cluster_of_shingle.get(shingle)
                if cluster is None:
                    continue
                for position in range(start, start + len(shingle)):
                    covered[position] = cluster
            for line, cluster in zip(run, covered, strict=True):
                if cluster is None:
                    own_lines.append(line)
                else:
                    touched = True
                    cluster_lines.setdefault(cluster, {}).setdefault(url, []).append(line)
        if touched:
            own_lines = _drop_moved_lines(own_lines)
            # Остались одни цифры («+7 280», «-10 490 +990») — это цены той же акции,
            # у каждого курса свои; или обрывок текста («Узнайте свой у») от блока,
            # порезанного на этой странице иначе. Отдельной находкой это не нужно.
            if len(_LETTER_RE.findall("".join(own_lines))) < _MIN_OWN_LETTERS:
                own_lines = []
            split.own_changes[url] = "\n".join(own_lines) if own_lines else None

    for cluster_index in sorted(cluster_lines, key=lambda index: -len(cluster_lines[index])):
        by_url = cluster_lines[cluster_index]
        split.groups.append(SitewideGroup(urls=sorted(by_url), lines=_group_lines(by_url)))
    return split
