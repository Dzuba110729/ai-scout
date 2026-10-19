"""Блок «Коротко» в начале отчёта и абзац «Главное» от ИИ."""

import asyncio
from datetime import UTC, datetime

from app.ai import run_overview
from app.ai.analyze import AiAnalysisResult, ClaudeCliError
from app.ai.compare import ComparisonResult
from app.ai.run_overview import OverviewItem, build_overview_prompt
from app.crawler.diff import ChangeType, PageDiff
from app.integrations.google_docs import ReportSummary, build_intro_paragraphs, build_intro_requests
from app.notifications.telegram import format_overview
from app.pipeline import RunFinding, build_overview_items, build_report_summary

RUN_AT = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _analysis(importance=None, summary="Суть", reason=None):
    return AiAnalysisResult(
        category=None, usp=None, cta=None, summary=summary, raw_response={},
        importance=importance, importance_reason=reason,
    )


def _finding(url, kind=ChangeType.NEW, analysis=None, comparison=None, moved_to=None):
    return RunFinding(PageDiff(url=url, change_type=kind, new_text="t"), moved_to, analysis, comparison)


def _none():
    return ComparisonResult("none", None, None, "нет такого курса", {})


def test_summary_counts_and_lists():
    findings = [
        _finding("https://x.ru/a", analysis=_analysis("high", reason="Скидка 30%"), comparison=_none()),
        _finding("https://x.ru/b", ChangeType.CHANGED, analysis=_analysis("low", summary="Мелочь")),
        _finding("https://x.ru/c", ChangeType.REMOVED, moved_to="https://x.ru/c2"),
        _finding("https://x.ru/d", ChangeType.REMOVED),
    ]

    summary = build_report_summary(
        "Скайсмарт", RUN_AT, findings, baseline_pages=None, baseline_comparisons=[], overview="Абзац"
    )

    assert summary.heading == "Коротко: Скайсмарт, обход 05.10.2026"
    assert summary.counts == (
        "Найдено: новых страниц — 1, удалено — 1, переехало — 1. Из них важных 🔥 — 1."
        " Для SEO (инфо) — 0, для продукта (коммерция) — 3."
        " Мелких правок в отчёт не вошло: 1 (они есть в ленте бота)."
    )
    assert summary.important == [("Скидка 30%", "https://x.ru/a")]
    assert summary.missing == [("Суть", "https://x.ru/a")]
    assert summary.overview == "Абзац"


def test_summary_lists_are_limited_to_five():
    findings = [_finding(f"https://x.ru/{i}", analysis=_analysis("high", reason=f"п{i}")) for i in range(8)]

    summary = build_report_summary("X", RUN_AT, findings, baseline_pages=None, baseline_comparisons=[], overview=None)

    assert len(summary.important) == 5
    assert summary.important_total == 8
    paragraphs = build_intro_paragraphs(summary, [])
    assert ("NORMAL_TEXT", "…и ещё 3 — в таблице ниже.") in paragraphs


def test_summary_without_changes():
    summary = build_report_summary("X", RUN_AT, [], baseline_pages=None, baseline_comparisons=[], overview=None)

    assert summary.counts == "Изменений с прошлого обхода нет."
    assert summary.important == summary.missing == []


def test_intro_puts_summary_before_details():
    summary = ReportSummary(
        heading="Коротко: X", counts="Найдено: …", overview="Главное тут",
        important=[("Скидка", "https://x.ru/a")],
    )

    paragraphs = build_intro_paragraphs(summary, ["Показан не весь сайт"])

    assert paragraphs == [
        ("HEADING_1", "Коротко: X"),
        ("NORMAL_TEXT", "Главное: Главное тут"),
        ("NORMAL_TEXT", "Найдено: …"),
        ("HEADING_2", "🔥 Важное"),
        ("NORMAL_TEXT", "• Скидка — https://x.ru/a"),
        ("HEADING_1", "Подробности"),
        ("NORMAL_TEXT", "Показан не весь сайт"),
    ]


def test_intro_without_summary_is_just_notes():
    assert build_intro_paragraphs(None, ["a", "b"]) == [("NORMAL_TEXT", "a"), ("NORMAL_TEXT", "b")]


def test_intro_requests_count_emoji_as_two_utf16_units():
    requests = build_intro_requests([("HEADING_2", "🔥 Важное"), ("NORMAL_TEXT", "ок")])

    assert requests[0]["insertText"]["text"] == "🔥 Важное\nок\n\n"
    first = requests[1]["updateParagraphStyle"]["range"]
    second = requests[2]["updateParagraphStyle"]["range"]
    assert first == {"startIndex": 1, "endIndex": 1 + 9 + 1}  # 🔥 = 2 единицы, « Важное» = 7, \n
    assert second == {"startIndex": 11, "endIndex": 14}


def test_overview_items_put_important_first_and_skip_minor():
    findings = [
        _finding("https://x.ru/low", analysis=_analysis("low")),
        _finding("https://x.ru/mid", analysis=_analysis("medium")),
        _finding("https://x.ru/high", analysis=_analysis("high")),
    ]

    items = build_overview_items(findings, [])

    assert [item.url for item in items] == ["https://x.ru/high", "https://x.ru/mid"]


def test_only_minor_changes():
    findings = [_finding("https://x.ru/a", ChangeType.CHANGED, analysis=_analysis("low"))]

    summary = build_report_summary("X", RUN_AT, findings, baseline_pages=None, baseline_comparisons=[], overview=None)

    assert summary.counts == "Значимых изменений нет. Мелких правок в отчёт не вошло: 1 (они есть в ленте бота)."


def test_college_pages_excluded_for_competitors_only():
    from app.crawler.crawl import competitor_include_paths, path_included

    rival = competitor_include_paths(None, is_own=False)
    foxford = competitor_include_paths("/catalog, /college", is_own=False)
    own = competitor_include_paths(None, is_own=True)

    assert not path_included("https://foxford.ru/college/9-class/dizajn", foxford)
    assert path_included("https://foxford.ru/catalog/ege", foxford)
    assert not path_included("https://onlineschool-1.ru/onlinecollege-1", rival)
    assert not path_included("https://x.ru/news/kak-postupit-v-kolledzh", rival)
    assert not path_included("https://x.ru/spo/programmy", rival)
    assert path_included("https://x.ru/sport-i-ucheba", rival)
    assert path_included("https://x.ru/", rival)
    assert path_included("https://og1.ru/polza/kak-okonchit-kolledzh-eksternom", own)


def test_overview_prompt_marks_important_and_missing():
    items = [OverviewItem("https://x.ru/a", "Новая страница", _analysis("high", summary="Курс ОГЭ"), _none())]

    prompt = build_overview_prompt("Скайсмарт", items, baseline=False)

    assert "🔥 Новая страница | https://x.ru/a | Курс ОГЭ | У нас такого нет" in prompt
    assert build_overview_prompt("Скайсмарт", [], baseline=False) is None


def test_overview_survives_cli_failure(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise ClaudeCliError("нет ответа")

    monkeypatch.setattr(run_overview, "run_claude_cli", fail)
    items = [OverviewItem("https://x.ru/a", "Новая страница", _analysis())]

    assert asyncio.run(run_overview.write_run_overview("X", items, baseline=False)) is None


def test_telegram_overview_line():
    assert format_overview("Конкурент снизил цены.") == "📝 Главное: Конкурент снизил цены."

