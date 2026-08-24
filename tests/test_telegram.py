from app.crawler.diff import ChangeType, PageDiff
from app.notifications.telegram import (
    TelegramNotifier,
    format_blocked_message,
    format_change_message,
    format_error_message,
    format_finished_message,
    format_started_message,
)


def test_format_change_message_for_new_page():
    page_diff = PageDiff(url="https://x.ru/promo", change_type=ChangeType.NEW, new_text="текст")
    message = format_change_message("Фоксфорд", page_diff, ai_summary="Запустили новую акцию.")

    assert "Новая страница" in message
    assert "Фоксфорд" in message
    assert "https://x.ru/promo" in message
    assert "новую акцию" in message


def test_format_blocked_message_mentions_reason():
    message = format_blocked_message("Фоксфорд", "https://x.ru", "HTTP 403")
    assert "не удался" in message
    assert "HTTP 403" in message
    assert "Apify" in message


def test_notifier_not_configured_without_token():
    notifier = TelegramNotifier(bot_token="", chat_id="")
    assert notifier.is_configured is False


def test_format_started_message_mentions_competitor():
    message = format_started_message("Фоксфорд")
    assert "Фоксфорд" in message
    assert "начат" in message


def test_format_finished_message_includes_count_and_report_link():
    message = format_finished_message("Фоксфорд", 3, "https://docs.google.com/document/d/abc")
    assert "Фоксфорд" in message
    assert "3" in message
    assert "https://docs.google.com/document/d/abc" in message


def test_format_finished_message_without_report_link():
    message = format_finished_message("Фоксфорд", 0, None)
    assert "Отчёт" not in message


def test_format_error_message_mentions_reason():
    message = format_error_message("Фоксфорд", "таймаут")
    assert "Фоксфорд" in message
    assert "таймаут" in message
