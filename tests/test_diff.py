import pytest

from app.crawler.diff import (
    ChangeType,
    content_hash,
    diff_crawl,
    extract_text,
    normalize_for_compare,
    strip_related_blocks,
    strip_review_blocks,
)


def test_extract_text_strips_scripts_and_styles():
    html = """
    <html><body>
        <script>trackUser();</script>
        <style>.a { color: red; }</style>
        <h1>Курсы по математике</h1>
        <p>Запишись сейчас</p>
    </body></html>
    """
    text = extract_text(html)
    assert "trackUser" not in text
    assert "color: red" not in text
    assert "Курсы по математике" in text
    assert "Запишись сейчас" in text


def test_content_hash_stable_for_identical_text():
    assert content_hash("привет") == content_hash("привет")
    assert content_hash("привет") != content_hash("пока")


def test_diff_crawl_detects_new_page():
    previous = {"https://x.ru/a": "старый текст"}
    current = {"https://x.ru/a": "старый текст", "https://x.ru/b": "новая страница"}

    changes = diff_crawl(previous, current)

    assert len(changes) == 1
    assert changes[0].url == "https://x.ru/b"
    assert changes[0].change_type is ChangeType.NEW


def test_diff_crawl_detects_removed_page():
    previous = {"https://x.ru/a": "текст", "https://x.ru/b": "текст b"}
    current = {"https://x.ru/a": "текст"}

    changes = diff_crawl(previous, current)

    assert len(changes) == 1
    assert changes[0].url == "https://x.ru/b"
    assert changes[0].change_type is ChangeType.REMOVED
    assert changes[0].old_text == "текст b"


def test_diff_crawl_detects_changed_page_and_produces_diff():
    previous = {"https://x.ru/a": "Цена: 1000 руб"}
    current = {"https://x.ru/a": "Цена: 1200 руб"}

    changes = diff_crawl(previous, current)

    assert len(changes) == 1
    change = changes[0]
    assert change.change_type is ChangeType.CHANGED
    assert "1000" in change.diff_text
    assert "1200" in change.diff_text


def test_diff_crawl_ignores_unchanged_pages():
    previous = {"https://x.ru/a": "тот же текст"}
    current = {"https://x.ru/a": "тот же текст"}

    assert diff_crawl(previous, current) == []


def test_diff_crawl_handles_multiple_changes_together():
    previous = {
        "https://x.ru/keep": "не меняется",
        "https://x.ru/change": "было",
        "https://x.ru/gone": "удалят",
    }
    current = {
        "https://x.ru/keep": "не меняется",
        "https://x.ru/change": "стало",
        "https://x.ru/new": "новая",
    }

    changes = diff_crawl(previous, current)
    by_type = {c.change_type for c in changes}

    assert by_type == {ChangeType.NEW, ChangeType.CHANGED, ChangeType.REMOVED}
    assert len(changes) == 3


def test_cookie_banner_and_view_counters_are_not_changes():
    old = "Глагол bear\nПохожие статьи\n2736K\nИмена\n5313"
    new = (
        "Глагол bear\nПользуясь нашим сайтом, вы соглашаетесь с тем, что мы используем\n"
        "cookies\nOK\nПохожие статьи\n2736.3K\nИмена\n5331"
    )

    assert diff_crawl({"https://x.ru/a": old}, {"https://x.ru/a": new}) == []


def test_ticking_counters_are_not_changes():
    old = "Курс\nУже идёт 17 дней, но можно присоединиться"
    new = "Курс\nУже идёт 20 дней, но можно присоединиться"

    assert diff_crawl({"https://x.ru/a": old}, {"https://x.ru/a": new}) == []


def test_price_split_over_lines_is_still_a_change():
    # Цена часто разбита на строки «1 390» и «₽» — её прятать нельзя.
    old = "Курс\n1 390\n₽"
    new = "Курс\n1 590\n₽"

    [change] = diff_crawl({"https://x.ru/a": old}, {"https://x.ru/a": new})

    assert change.change_type is ChangeType.CHANGED
    assert "1 590" in change.diff_text


def _blog_page(related: list[tuple[str, str]]) -> str:
    cards = []
    for title, views in related:
        cards += ["Родителям", title, "12 мин", views, "23 сентября 2026"]
    return "\n".join(
        ["Статья про дроби", "Текст статьи", "Похожие статьи", "Previous", "Next", *cards, "Наши", "приложения"]
    )


def test_reshuffled_related_articles_are_not_changes():
    old = _blog_page([("Дроби в 5 классе", "304"), ("Счёт с дошкольником", "2839")])
    new = _blog_page([("ВПР в 7 классе", "14419"), ("Дроби в 5 классе", "305"), ("Сюрреализм", "14024")])

    assert diff_crawl({"/a": old}, {"/a": new}) == []


def test_real_change_next_to_related_block_is_shown_without_the_block():
    old = _blog_page([("Дроби в 5 классе", "304")])
    new = _blog_page([("ВПР в 7 классе", "14419")]).replace("Текст статьи", "Новый текст статьи")

    [change] = diff_crawl({"/a": old}, {"/a": new})

    assert "Новый текст статьи" in change.diff_text
    assert "ВПР в 7 классе" not in change.diff_text
    assert "Наши" in strip_related_blocks(new)


def test_heading_without_dated_cards_is_kept():
    text = "Новые статьи\nСкоро здесь появятся материалы\nКупить курс"

    assert strip_related_blocks(text) == text


def test_countdown_timer_and_reading_time_are_not_changes():
    old = "Уже идёт 5 дней, но можно присоединиться\n04:52\nдней\n12 мин\nЦена 2 990 ₽"
    new = "Уже идёт 21 день, но можно присоединиться\n04:56\nдня\n13 мин\nЦена 2 990 ₽"

    assert diff_crawl({"/a": old}, {"/a": new}) == []


def _review_card(initial: str, author: str, date: str, text: str, teacher: str) -> str:
    return f"{initial}\n{author}\n{date}\n5\n{text}\nОтзыв о преподавателе\n{teacher}\nна курс\nМатематика 5 класс"


def _course_page(cards: list[str]) -> str:
    return "\n".join(
        [
            "Математика для 5 класса",
            "Цена 990 ₽",
            "Отзывы о преподавателях, ведущих курсы",
            "5.0",
            "26 684",
            "оценки",
            *cards,
            "Показать ещё отзывы",
            "Как проходит обучение",
        ]
    )


def test_review_carousel_is_noise():
    old = _course_page(
        [
            _review_card("С", "Софа", "18 октября 2026", "Отличный учитель", "Наталье"),
            _review_card("Д", "Даниил", "10 октября 2026", "Всё понятно", "Полине"),
        ]
    )
    new = _course_page(
        [
            _review_card("П", "Павел", "19 октября 2026", "очень добрая,\nвсегда выслушает,\nпомогает", "Елене"),
            _review_card("М", "Мария", "17 октября 2026", "Спасибо", "Олегу"),
            _review_card("А", "Аноним", "16 октября 2026", "Супер", "Анне"),
        ]
    )

    assert normalize_for_compare(old) == normalize_for_compare(new)
    assert "Как проходит обучение" in normalize_for_compare(new)
    assert "Отзыв о преподавателе" not in normalize_for_compare(new)
    assert diff_crawl({"u": old}, {"u": new}) == []


def test_change_next_to_review_carousel_is_still_seen():
    cards = [
        _review_card("С", "Софа", "18 октября 2026", "Отличный учитель", "Наталье"),
        _review_card("Д", "Даниил", "10 октября 2026", "Всё понятно", "Полине"),
    ]
    old = _course_page(cards)
    new = old.replace("Цена 990 ₽", "Цена 1290 ₽")

    assert [d.change_type for d in diff_crawl({"u": old}, {"u": new})] == [ChangeType.CHANGED]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("5,0\n·\n1476\nотзывов", "5,0\n·\n1503\nотзыва"),
        ("Курс\n1 476 отзывов", "Курс\n1 503 отзыва"),
        ("Курс\nОтзывы (1476)", "Курс\nОтзывы (1503)"),
    ],
)
def test_review_counters_are_noise(old, new):
    assert normalize_for_compare(old) == normalize_for_compare(new)


def test_single_date_after_reviews_heading_is_kept():
    text = "Отзывы родителей\nСтарт курса\n1 ноября 2026\nЗаписаться"

    assert strip_review_blocks(text) == text
