"""ИИ-анализ изменений через Claude Code CLI в headless-режиме (`claude -p`).

Важно: используется личная подписка Claude Code, а не платный Anthropic API
(см. CLAUDE.md) — поэтому вызовы идут через subprocess к CLI, а не через SDK.
При частоте раз в неделю на одного конкурента нагрузка минимальна.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from app.config import settings
from app.crawler.diff import ChangeType, PageDiff

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class ClaudeCliError(Exception):
    """CLI недоступен или вернул код ошибки после ретрая."""


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
    "- medium: заметная переработка текста/структуры продающей страницы, новые отзывы/кейсы,\n"
    "  новые преподаватели, новая статья по ключевой для школы теме;\n"
    "- low: мелкие правки текста, даты, опечатки, служебные и юридические страницы, блог\n"
    "  на второстепенную тему, технические изменения без смысла для клиента.\n"
)


def normalize_importance(value: object) -> str | None:
    """Ответ модели -> high/medium/low. Всё непонятное — None (считается «средне»)."""
    if not isinstance(value, str):
        return None
    return _IMPORTANCE_ALIASES.get(value.strip().lower())


def importance_rank(importance: str | None) -> int:
    """Ключ сортировки «важное сначала». Без оценки — как «средне»."""
    return {"high": 0, "medium": 1, "low": 2}.get(importance or "medium", 1)


def build_prompt(page_diff: PageDiff) -> str:
    if page_diff.change_type is ChangeType.NEW:
        content = page_diff.new_text or ""
        context_line = "Это новая страница конкурента."
    elif page_diff.change_type is ChangeType.REMOVED:
        content = page_diff.old_text or ""
        context_line = "Эта страница конкурента была удалена."
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
        '{\n'
        '  "category": "тип страницы: лендинг/оффер/цены/статья/другое",\n'
        '  "usp": "ключевое УТП или выгода, если есть, иначе null",\n'
        '  "cta": "текст призыва к действию, если есть, иначе null",\n'
        '  "summary": "1-2 предложения о сути изменения на русском",\n'
        '  "importance": "high | medium | low",\n'
        '  "importance_reason": "одно короткое предложение: почему такая важность"\n'
        '}\n'
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


async def _run_claude_cli_once(prompt: str, extra_args: tuple[str, ...] = (), cwd: str | None = None) -> str:
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
            process.communicate(), timeout=settings.claude_cli_timeout_seconds
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ClaudeCliError("Claude CLI не ответил за отведённое время")

    if process.returncode != 0:
        raise ClaudeCliError(f"Claude CLI завершился с кодом {process.returncode}: {stderr.decode(errors='ignore')}")

    return stdout.decode(errors="ignore")


async def run_claude_cli(
    prompt: str, *, retries: int = 1, extra_args: tuple[str, ...] = (), cwd: str | None = None
) -> str:
    """Запускает `claude -p` headless, с одним ретраем при временной недоступности."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _run_claude_cli_once(prompt, extra_args, cwd)
        except ClaudeCliError as exc:
            last_error = exc
            logger.warning("Claude CLI попытка %s не удалась: %s", attempt + 1, exc)
            if attempt < retries:
                await asyncio.sleep(2**attempt)
    assert last_error is not None
    raise last_error


async def analyze_page_change(page_diff: PageDiff) -> AiAnalysisResult:
    prompt = build_prompt(page_diff)
    raw_text = await run_claude_cli(prompt)
    return parse_ai_response(raw_text)
