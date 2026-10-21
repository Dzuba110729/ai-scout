"""ИИ-анализ изменений через Claude Code CLI в headless-режиме (`claude -p`).

Важно: используется личная подписка Claude Code, а не платный Anthropic API
(см. CLAUDE.md) — поэтому вызовы идут через subprocess к CLI, а не через SDK.
При частоте раз в неделю на одного конкурента нагрузка минимальна.
"""

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass

from app.config import settings
from app.crawler.diff import ChangeType, PageDiff
from app.crawler.sitewide import SitewideGroup

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class ClaudeCliError(Exception):
    """CLI недоступен или вернул код ошибки после ретрая."""


class ClaudeLimitError(ClaudeCliError):
    """Лимит подписки исчерпан — повторять бессмысленно до паузы."""


# Как CLI сообщает об исчерпанном лимите: «Claude AI usage limit reached|…»,
# «You've hit your limit · resets 3pm», «5-hour limit reached».
_LIMIT_RE = re.compile(r"usage limit|hit your limit|limit reached|rate limit|limit · resets", re.IGNORECASE)
# time.monotonic(), до которого CLI не вызываем (общий на весь процесс).
_limit_pause_until = 0.0


def _limit_pause_left() -> float:
    return max(0.0, _limit_pause_until - time.monotonic())


def limit_pause_minutes_left() -> int:
    """Сколько минут ещё не вызываем CLI после «limit reached» (0 — паузы нет)."""
    return math.ceil(_limit_pause_left() / 60)


def _start_limit_pause() -> None:
    global _limit_pause_until
    _limit_pause_until = time.monotonic() + settings.claude_cli_limit_pause_minutes * 60


@dataclass
class AiAnalysisResult:
    category: str | None
    usp: str | None
    cta: str | None
    summary: str | None
    raw_response: dict
    importance: str | None = None  # high / medium / low, см. normalize_importance
    importance_reason: str | None = None


IMPORTANCE_LEVELS = ("high", "medium", "low")

_IMPORTANCE_ALIASES = {
    "high": "high",
    "высокая": "high",
    "важно": "high",
    "medium": "medium",
    "средняя": "medium",
    "средне": "medium",
    "low": "low",
    "низкая": "low",
    "мелочь": "low",
}

# Что считать важным — отдельной константой, чтобы критерии было легко найти и
# поправить под себя, не копаясь в тексте промпта.
IMPORTANCE_CRITERIA = (
    "Оценка важности для отдела маркетинга (importance):\n"
    "- high: цены, скидки, акции, новый продукт/курс/тариф/направление, новый оффер или лендинг\n"
    "  под рекламу, смена УТП или гарантий, удаление продукта или целого направления;\n"
    "- medium: заметная переработка текста/структуры продающей страницы, новые кейсы,\n"
    "  новые преподаватели, новая статья по ключевой для школы теме;\n"
    "- low: мелкие правки текста, даты, опечатки, служебные и юридические страницы, блог\n"
    "  на второстепенную тему, технические изменения без смысла для клиента;\n"
    "  новые отзывы, рост числа отзывов или рейтинга, смена отзывов в карусели;\n"
    "  всё про колледж/СПО/техникум — у нашей школы таких программ нет.\n"
)


def normalize_importance(value: object) -> str | None:
    """Ответ модели -> high/medium/low. Всё непонятное — None (считается «средне»)."""
    if not isinstance(value, str):
        return None
    return _IMPORTANCE_ALIASES.get(value.strip().lower())


def importance_rank(importance: str | None) -> int:
    """Ключ сортировки «важное сначала». Без оценки — как «средне»."""
    return {"high": 0, "medium": 1, "low": 2}.get(importance or "medium", 1)


# Поля ответа, общие для разбора страницы и сквозного блока. Описание — коротко:
# SEO-специалист просил «покороче, чем сейчас» (обратная связь 2026-10-19).
_ANSWER_FIELDS = (
    '  "category": "тип: лендинг/оффер/цены/курс/статья/новость/чек-лист/тест/другое",\n'
    '  "usp": "что конкретно появилось или поменялось, до 10 слов (например: «добавлен раздел '
    'Советы по сдаче», «скидка 30% при оплате полностью»), иначе null",\n'
    '  "cta": "текст призыва к действию, если есть, иначе null",\n'
    '  "summary": "одно короткое предложение, до 20 слов: суть изменения на русском",\n'
    '  "importance": "high | medium | low",\n'
    '  "importance_reason": "одно короткое предложение: почему такая важность"\n'
)


def build_prompt(page_diff: PageDiff) -> str:
    if page_diff.change_type is ChangeType.NEW:
        content = page_diff.new_text or ""
        context_line = "Это новая страница конкурента."
    elif page_diff.change_type is ChangeType.REMOVED:
        content = page_diff.old_text or ""
        context_line = "Эта страница конкурента была удалена."
    elif page_diff.own_diff_text is not None:
        content = page_diff.own_diff_text
        context_line = (
            "На странице конкурента произошли изменения. Общие для многих страниц сайта (баннеры, "
            "акции, меню) разобраны отдельно — ниже только то, что поменялось именно на этой "
            "странице: «+» добавлено, «-» убрано."
        )
    else:
        content = page_diff.diff_text or ""
        context_line = "На странице конкурента произошли изменения (unified diff, было/стало)."

    content = content[:8000]  # ограничиваем объём промпта

    return (
        "Ты аналитик, который отслеживает сайт конкурента для отдела маркетинга.\n"
        f"{context_line}\n"
        f"URL страницы: {page_diff.url}\n\n"
        "Содержимое:\n"
        "```\n"
        f"{content}\n"
        "```\n\n"
        f"{IMPORTANCE_CRITERIA}\n"
        "Верни СТРОГО один JSON-объект (без пояснений вне JSON) со следующими полями:\n"
        "{\n"
        f"{_ANSWER_FIELDS}"
        "}\n"
    )


_SITEWIDE_EXAMPLES = 5
_SITEWIDE_NEW_PAGES_LIMIT = 30


def build_sitewide_prompt(group: SitewideGroup, new_page_urls: list[str]) -> str:
    """Промпт для сквозного изменения: один блок поменялся сразу на многих страницах."""
    examples = "\n".join(group.urls[:_SITEWIDE_EXAMPLES])
    new_pages = "\n".join(new_page_urls[:_SITEWIDE_NEW_PAGES_LIMIT]) or "нет"
    return (
        "Ты аналитик, который отслеживает сайт конкурента для отдела маркетинга.\n"
        f"Одно и то же изменение появилось сразу на {len(group.urls)} страницах конкурента — это "
        "сквозной блок (баннер, акция, блок курсов, меню). Опиши его ОДИН раз, как одно событие.\n"
        f"Примеры страниц:\n{examples}\n\n"
        "Строки блока («+» добавлено, «-» убрано; в скобках — на скольких страницах, если не на всех):\n"
        "```\n"
        f"{group.diff_text[:8000]}\n"
        "```\n\n"
        "Новые страницы конкурента, найденные в этом же обходе. Если блок рекламирует одну из них "
        f"(например, новый курс), укажи её в related_url:\n{new_pages}\n\n"
        f"{IMPORTANCE_CRITERIA}\n"
        "Верни СТРОГО один JSON-объект (без пояснений вне JSON) со следующими полями:\n"
        "{\n"
        f"{_ANSWER_FIELDS.rstrip()},\n"
        '  "related_url": "адрес новой страницы, на которую ведёт блок, из списка выше, иначе null"\n'
        "}\n"
    )


def extract_json_object(raw_text: str) -> dict:
    """Достаёт JSON-объект из ответа CLI: вокруг него бывает текст-обёртка."""
    match = _JSON_BLOCK_RE.search(raw_text)
    if not match:
        raise ValueError(f"В ответе ИИ не найден JSON: {raw_text[:200]!r}")

    return json.loads(match.group(0))


def parse_ai_response(raw_text: str) -> AiAnalysisResult:
    data = extract_json_object(raw_text)

    return AiAnalysisResult(
        category=data.get("category"),
        usp=data.get("usp"),
        cta=data.get("cta"),
        summary=data.get("summary"),
        raw_response=data,
        importance=normalize_importance(data.get("importance")),
        importance_reason=data.get("importance_reason"),
    )


async def _run_claude_cli_once(
    prompt: str, extra_args: tuple[str, ...] = (), cwd: str | None = None, timeout: float | None = None
) -> str:
    if _limit_pause_left():
        raise ClaudeLimitError(f"лимит подписки Claude, пауза ещё {int(_limit_pause_left() // 60) + 1} мин")
    process = await asyncio.create_subprocess_exec(
        settings.claude_cli_path,
        "-p",
        prompt,
        "--output-format",
        "text",
        *extra_args,
        # Без этого `claude -p` дочитывает stdin родителя и приклеивает его к промпту.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout or settings.claude_cli_timeout_seconds
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ClaudeCliError("Claude CLI не ответил за отведённое время")

    if process.returncode != 0:
        # Причину CLI нередко пишет в stdout, а stderr пустой — берём оба.
        details = " ".join(
            part.decode(errors="ignore").strip() for part in (stderr, stdout) if part.strip()
        )[-500:]
        if _LIMIT_RE.search(details):
            _start_limit_pause()
            raise ClaudeLimitError(f"лимит подписки Claude: {details}")
        raise ClaudeCliError(f"Claude CLI завершился с кодом {process.returncode}: {details or 'без пояснений'}")

    return stdout.decode(errors="ignore")


async def run_claude_cli(
    prompt: str,
    *,
    retries: int = 1,
    extra_args: tuple[str, ...] = (),
    cwd: str | None = None,
    timeout: float | None = None,
) -> str:
    """Запускает `claude -p` headless, с одним ретраем при временной недоступности."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _run_claude_cli_once(prompt, extra_args, cwd, timeout)
        except ClaudeLimitError:
            raise
        except ClaudeCliError as exc:
            last_error = exc
            logger.warning("Claude CLI попытка %s не удалась: %s", attempt + 1, exc)
            if attempt < retries:
                await asyncio.sleep(5 * 2**attempt)
    assert last_error is not None
    raise last_error


async def analyze_sitewide_group(group: SitewideGroup, new_page_urls: list[str]) -> AiAnalysisResult:
    raw_text = await run_claude_cli(build_sitewide_prompt(group, new_page_urls))
    return parse_ai_response(raw_text)


async def analyze_page_change(page_diff: PageDiff) -> AiAnalysisResult:
    prompt = build_prompt(page_diff)
    raw_text = await run_claude_cli(prompt)
    return parse_ai_response(raw_text)
