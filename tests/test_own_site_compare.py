"""Подбор похожих страниц нашего сайта и разбор ответа ИИ о сравнении.

Всё на фикстурах: ни сети, ни вызовов claude -p здесь нет.
"""

import pytest

from app.ai.compare import (
    VERDICT_NONE,
    VERDICT_SIMILAR,
    ComparisonResult,
    OwnPage,
    OwnSite,
    build_comparison_prompt,
    competitor_text_for_comparison,
    find_similar_pages,
    parse_comparison_response,
    stem,
    tokenize,
)
from app.crawler.diff import ChangeType, PageDiff

MATH_PAGE = OwnPage(
    url="https://og1.ru/math-ege",
    title="Подготовка к ЕГЭ по математике",
    text=(
        "Курс подготовки к ЕГЭ по математике. Занятия с репетитором в малых группах, "
        "разбор задач профильного уровня, пробные экзамены каждый месяц."
    ),
)

ENGLISH_PAGE = OwnPage(
    url="https://og1.ru/english",
    title="Английский язык для школьников",
    text="Курсы английского языка: разговорная практика, грамматика, подготовка к олимпиадам.",
)

PAYMENT_PAGE = OwnPage(
    url="https://og1.ru/oplata",
    title="Оплата и рассрочка",
    text="Оплатить обучение можно картой или в рассрочку на 12 месяцев без переплаты.",
)

OWN_PAGES = [MATH_PAGE, ENGLISH_PAGE, PAYMENT_PAGE]


def test_stem_cuts_russian_endings():
    assert stem("курсы") == stem("курс") == stem("курсов") == "курс"
    assert stem("подготовка") == stem("подготовки") == stem("подготовке")


def test_stem_keeps_short_words_intact():
    # Иначе «егэ» и «цена» превратились бы в огрызки и перестали совпадать сами с собой.
    assert stem("егэ") == "егэ"
    assert stem("цена") == "цена"


def test_tokenize_drops_stopwords_and_short_words():
    tokens = tokenize("Курс по математике и для всех")
    assert "курс" in tokens
    assert "для" not in tokens
    assert "и" not in tokens


def test_most_similar_page_is_found_despite_different_word_forms():
    # У конкурента «курсы подготовки», у нас «курс подготовка» — без отсечения
    # окончаний эти страницы не совпали бы.
    similar = find_similar_pages(
        "Курсы подготовки к ЕГЭ по математике",
        "Готовим к ЕГЭ по математике: разборы задач профильного уровня, пробные экзамены.",
        OWN_PAGES,
    )

    assert similar
    assert similar[0].page.url == MATH_PAGE.url


def test_similar_pages_are_limited_and_sorted_by_score():
    similar = find_similar_pages("Английский язык", "Курсы английского языка", OWN_PAGES, limit=2)

    assert len(similar) <= 2
    assert similar[0].page.url == ENGLISH_PAGE.url
    assert similar == sorted(similar, key=lambda item: -item.score)


def test_completely_unrelated_page_gets_no_candidates():
    similar = find_similar_pages(
        "Ремонт двигателей", "Сервис грузовых двигателей, токарные работы", OWN_PAGES
    )

    assert similar == []


def test_no_candidates_when_our_site_is_empty():
    assert find_similar_pages("Курсы ЕГЭ", "Подготовка к ЕГЭ", []) == []


def test_prompt_contains_competitor_page_and_all_candidates():
    own_site = OwnSite(base_url="https://og1.ru", pages=OWN_PAGES)
    candidates = find_similar_pages("Подготовка к ЕГЭ", "Курсы подготовки к ЕГЭ", OWN_PAGES)

    prompt = build_comparison_prompt("https://rival.ru/ege", "Курсы ЕГЭ от 2900 ₽", own_site, candidates)

    assert "https://rival.ru/ege" in prompt
    assert "Курсы ЕГЭ от 2900 ₽" in prompt
    assert "https://og1.ru" in prompt
    for candidate in candidates:
        assert candidate.page.url in prompt


def test_parse_comparison_response_reads_all_fields():
    raw = """Вот ответ:
    {
      "verdict": "similar",
      "our_url": "https://og1.ru/math-ege",
      "differences": "У конкурента указана цена от 2900 ₽, у нас цены нет.",
      "missing": "Добавить блок с отзывами учеников."
    }
    """

    result = parse_comparison_response(raw)

    assert result.verdict == VERDICT_SIMILAR
    assert result.our_url == "https://og1.ru/math-ege"
    assert "2900" in result.differences
    assert result.missing == "Добавить блок с отзывами учеников."
    assert result.label == "У нас похожее есть"


def test_parse_comparison_response_treats_nulls_as_empty():
    result = parse_comparison_response('{"verdict": "none", "our_url": null, "differences": null, "missing": null}')

    assert result.verdict == VERDICT_NONE
    assert result.our_url is None
    assert result.differences is None
    assert result.label == "У нас такого нет"


def test_unknown_verdict_falls_back_to_presence_of_our_page():
    with_page = parse_comparison_response('{"verdict": "есть похожее", "our_url": "https://og1.ru/x"}')
    without_page = parse_comparison_response('{"verdict": "непонятно"}')

    assert with_page.verdict == VERDICT_SIMILAR
    assert without_page.verdict == VERDICT_NONE


def test_parse_comparison_response_without_json_raises():
    with pytest.raises(ValueError):
        parse_comparison_response("Не могу ответить")


@pytest.mark.parametrize(
    ("change_type", "expected"),
    [
        (ChangeType.NEW, "новый текст"),
        (ChangeType.CHANGED, "новый текст"),
        (ChangeType.REMOVED, None),
    ],
)
def test_text_for_comparison_uses_current_page_and_skips_removed(change_type, expected):
    page_diff = PageDiff(
        url="https://rival.ru/p",
        change_type=change_type,
        old_text="старый текст",
        new_text="новый текст" if change_type is not ChangeType.REMOVED else None,
    )

    assert competitor_text_for_comparison(page_diff) == expected


def test_comparison_label_falls_back_for_unknown_verdict():
    result = ComparisonResult(
        verdict="что-то своё", our_url=None, differences=None, missing=None, raw_response={}
    )

    assert result.label == "У нас такого нет"
