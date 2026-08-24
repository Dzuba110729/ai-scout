"""Детект антибот-блокировок (Cloudflare и подобные) на странице обхода.

Задача — не пытаться обойти защиту автоматически (капчи решаются вручную,
см. CLAUDE.md), а надёжно распознать блокировку и остановить обход конкурента,
переведя его в статус "нужна ручная сессия" вместо бесконечных ретраев.
"""

import re

BLOCKED_STATUS_CODES = {403, 429, 503}

_CHALLENGE_MARKERS = (
    "checking your browser",
    "just a moment",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "cf_chl_opt",
    "please enable cookies",
    "verify you are human",
    "unusual traffic",
    "доступ ограничен",
    "подтвердите, что вы не робот",
)

# Challenge/интерстишл-страницы почти всегда маленькие (пара KB HTML-обёртки
# вокруг JS-редиректа). Обычная страница с контентом может случайно содержать
# слово из маркеров (например конфиг капчи для формы) — не считаем это блокировкой,
# если страница выглядит как настоящий контент, а не заглушка.
_MAX_CHALLENGE_PAGE_LENGTH = 20_000


def is_blocked(status_code: int, html: str) -> bool:
    """True, если ответ похож на антибот-блокировку/challenge-страницу."""
    if status_code in BLOCKED_STATUS_CODES:
        return True

    if len(html) > _MAX_CHALLENGE_PAGE_LENGTH:
        return False

    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def blocked_reason(status_code: int, html: str) -> str | None:
    """Возвращает человекочитаемую причину блокировки или None, если её нет."""
    if status_code in BLOCKED_STATUS_CODES:
        return f"HTTP {status_code}"

    if len(html) > _MAX_CHALLENGE_PAGE_LENGTH:
        return None

    lowered = html.lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in lowered:
            return f'challenge-маркер: "{marker}"'

    return None


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def page_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    return match.group(1).strip() if match else None
