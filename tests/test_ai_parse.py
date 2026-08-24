import pytest

from app.ai.analyze import build_prompt, parse_ai_response
from app.crawler.diff import ChangeType, PageDiff


def test_parse_ai_response_extracts_fields():
    raw = """Вот результат анализа:
    {
      "category": "лендинг",
      "usp": "Скидка 30% на курс",
      "cta": "Записаться на бесплатный урок",
      "summary": "Конкурент запустил акцию со скидкой."
    }
    """
    result = parse_ai_response(raw)

    assert result.category == "лендинг"
    assert result.usp == "Скидка 30% на курс"
    assert result.cta == "Записаться на бесплатный урок"
    assert "акцию" in result.summary
    assert result.raw_response["category"] == "лендинг"


def test_parse_ai_response_handles_null_fields():
    raw = '{"category": "статья", "usp": null, "cta": null, "summary": "Обновили текст статьи."}'
    result = parse_ai_response(raw)

    assert result.category == "статья"
    assert result.usp is None
    assert result.cta is None


def test_parse_ai_response_raises_without_json():
    with pytest.raises(ValueError):
        parse_ai_response("Извините, не могу выполнить запрос.")


def test_build_prompt_for_new_page_mentions_new():
    page_diff = PageDiff(url="https://x.ru/promo", change_type=ChangeType.NEW, new_text="Новый оффер")
    prompt = build_prompt(page_diff)

    assert "новая страница" in prompt.lower()
    assert "https://x.ru/promo" in prompt
    assert "Новый оффер" in prompt


def test_build_prompt_for_changed_page_uses_diff_text():
    page_diff = PageDiff(
        url="https://x.ru/pricing", change_type=ChangeType.CHANGED, old_text="1000 руб", new_text="1200 руб"
    )
    prompt = build_prompt(page_diff)

    assert "1000" in prompt
    assert "1200" in prompt
