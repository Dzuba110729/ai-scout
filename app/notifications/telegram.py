"""Прямая интеграция с Telegram Bot API (без MCP/плагинов, см. CLAUDE.md)."""

import logging

import httpx

from app.ai.compare import ComparisonResult
from app.config import settings
from app.crawler.diff import ChangeType, PageDiff

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

_CHANGE_TYPE_LABELS = {
    ChangeType.NEW: "🆕 Новая страница",
    ChangeType.CHANGED: "✏️ Изменение",
    ChangeType.REMOVED: "🗑 Страница удалена",
}


def format_change_message(competitor_name: str, page_diff: PageDiff, ai_summary: str | None = None) -> str:
    label = _CHANGE_TYPE_LABELS[page_diff.change_type]
    lines = [f"{label} — {competitor_name}", page_diff.url]
    if ai_summary:
        lines.append("")
        lines.append(ai_summary)
    return "\n".join(lines)


def format_blocked_message(competitor_name: str, url: str, reason: str) -> str:
    return (
        f"⛔ Обход конкурента «{competitor_name}» не удался\n"
        f"{url}\n"
        f"Причина: {reason}\n"
        "Проверьте APIFY_API_TOKEN и лог прогона в Apify Console."
    )


def format_started_message(competitor_name: str) -> str:
    return f"▶️ Обход конкурента «{competitor_name}» начат"


def format_finished_message(competitor_name: str, changes_count: int, report_url: str | None) -> str:
    lines = [f"✅ Обход конкурента «{competitor_name}» завершён", f"Изменений найдено: {changes_count}"]
    if report_url:
        lines.append(f"Отчёт: {report_url}")
    return "\n".join(lines)


def format_comparison_summary(
    comparisons: list[tuple[str, ComparisonResult]], limit: int = 5
) -> str:
    """Блок «есть ли такое у нас» для итогового сообщения об обходе.

    Отдельным сообщением на каждую находку не шлём: при десятке изменений это
    превратилось бы в спам. Длинный список обрезаем — полный лежит в отчёте.
    """
    lines = ["🔍 Сравнили с нашим сайтом:"]

    for url, comparison in comparisons[:limit]:
        lines.append("")
        lines.append(url)
        head = comparison.label
        if comparison.our_url:
            head = f"{head} — {comparison.our_url}"
        lines.append(head)
        if comparison.differences:
            lines.append(f"Отличия: {comparison.differences}")
        if comparison.missing:
            lines.append(f"Чего нам не хватает: {comparison.missing}")

    remaining = len(comparisons) - limit
    if remaining > 0:
        lines.append("")
        lines.append(f"Ещё сравнений: {remaining} — смотрите в отчёте.")

    return "\n".join(lines)


def format_error_message(competitor_name: str, reason: str) -> str:
    return f"⛔ Обход конкурента «{competitor_name}» завершился ошибкой\nПричина: {reason}"


class TelegramNotifier:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        self.bot_token = bot_token if bot_token is not None else settings.telegram_bot_token
        self.chat_id = chat_id if chat_id is not None else settings.telegram_chat_id

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, text: str) -> None:
        if not self.is_configured:
            logger.warning("Telegram не настроен (нет токена/chat_id) — уведомление не отправлено: %s", text)
            return

        url = _TELEGRAM_API.format(token=self.bot_token)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
            )
            response.raise_for_status()
