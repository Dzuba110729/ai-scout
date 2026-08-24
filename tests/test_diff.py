from app.crawler.diff import ChangeType, content_hash, diff_crawl, extract_text


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
