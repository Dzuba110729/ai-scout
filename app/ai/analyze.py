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
        "Верни СТРОГО один JSON-объект (без пояснений вне JSON) со следующими полями:\n"
        '{\n'
        '  "category": "тип страницы: лендинг/оффер/цены/статья/другое",\n'
        '  "usp": "ключевое УТП или выгода, если есть, иначе null",\n'
        '  "cta": "текст призыва к действию, если есть, иначе null",\n'
        '  "summary": "1-2 предложения о сути изменения на русском"\n'
        '}\n'
    )


def parse_ai_response(raw_text: str) -> AiAnalysisResult:
    match = _JSON_BLOCK_RE.search(raw_text)
    if not match:
        raise ValueError(f"В ответе ИИ не найден JSON: {raw_text[:200]!r}")

    data = json.loads(match.group(0))

    return AiAnalysisResult(
        category=data.get("category"),
        usp=data.get("usp"),
        cta=data.get("cta"),
        summary=data.get("summary"),
        raw_response=data,
    )


async def _run_claude_cli_once(prompt: str) -> str:
    process = await asyncio.create_subprocess_exec(
        settings.claude_cli_path,
        "-p",
        prompt,
        "--output-format",
        "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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


async def run_claude_cli(prompt: str, *, retries: int = 1) -> str:
    """Запускает `claude -p` headless, с одним ретраем при временной недоступности."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _run_claude_cli_once(prompt)
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
