from app.ai.compare import ComparisonResult
from app.crawler.diff import ChangeType, PageDiff
from app.notifications.telegram import (
    TelegramNotifier,
    format_blocked_message,
    format_change_message,
    format_comparison_summary,
    format_error_message,
    format_finished_message,
    format_started_message,
)


def _comparison(verdict="similar", our_url="https://og1.ru/ege"):
    return ComparisonResult(
        verdict=verdict,
        our_url=our_url,
        differences="У конкурента указана цена от 2900 ₽, у нас цены нет.",
        missing="Добавить блок с отзывами учеников.",
        raw_response={},
    )


def test_comparison_summary_explains_what_we_have_and_what_is_missing():
    message = format_comparison_summary([("https://rival.ru/ege", _comparison())])

    assert "https://rival.ru/ege" in message
    assert "У нас похожее есть" in message
    assert "https://og1.ru/ege" in message
    assert "Отличия: У конкурента указана цена" in message
    assert "Чего нам не хватает: Добавить блок с отзывами" in message


def test_comparison_summary_trims_long_list():
    # Десяток находок не должен превращать одно сообщение в простыню.
    items = [(f"https://rival.ru/p{i}", _comparison()) for i in range(8)]

    message = format_comparison_summary(items, limit=3)

    assert "https://rival.ru/p3" not in message
    assert "Ещё сравнений: 5" in message


def test_comparison_summary_without_our_page():
    message = format_comparison_summary([("https://rival.ru/x", _comparison("none", None))])

    assert "У нас такого нет" in message


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
