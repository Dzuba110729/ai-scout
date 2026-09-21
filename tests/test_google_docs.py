from datetime import UTC, datetime

from app.ai.analyze import AiAnalysisResult
from app.ai.compare import ComparisonResult
from app.crawler.diff import ChangeType, PageDiff
from app.integrations.google_docs import (
    _COLUMN_WIDTH_WEIGHTS,
    _HEADER,
    _PAGE_SIDE_MARGIN_PT,
    GoogleDocsClient,
    build_column_width_requests,
    build_insert_text_requests,
    build_row,
    compute_column_widths_pt,
    landscape_content_width_pt,
)


def test_build_row_new_page_with_analysis():
    page_diff = PageDiff(url="https://x.ru/promo", change_type=ChangeType.NEW, new_text="текст")
    analysis = AiAnalysisResult(
        category="лендинг", usp="Скидка 30%", cta="Записаться", summary="Новая акция", raw_response={}
    )
    detected_at = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)

    row = build_row(page_diff, analysis, detected_at)

    assert row[0] == "2026-08-10\n12:30"
    assert row[1] == "Новая страница"
    assert row[2] == "https://x.ru/promo"
    assert row[3] == "лендинг"
    assert row[4] == "Скидка 30%"
    assert row[5] == "Записаться"
    assert row[6] == "Новая акция"


def test_build_row_without_analysis_uses_empty_strings():
    page_diff = PageDiff(url="https://x.ru/removed", change_type=ChangeType.REMOVED, old_text="текст")

    row = build_row(page_diff, None, datetime.now(UTC))

    assert row[1] == "Страница удалена"
    assert row[3] == row[4] == row[5] == row[6] == ""


def test_build_row_for_moved_page_shows_target_and_what_is_there_now():
    page_diff = PageDiff(url="https://x.ru/old", change_type=ChangeType.REMOVED, old_text="текст")

    row = build_row(
        page_diff,
        None,
        datetime.now(UTC),
        redirect_to="https://x.ru/new",
        redirect_summary="Страница курса по физике со скидкой 30%",
    )

    assert row[1] == "Страница переехала"
    assert row[2] == "https://x.ru/old"
    assert "Теперь там: Страница курса по физике" in row[6]
    assert row[7] == "https://x.ru/new"


def test_build_row_for_deleted_page_leaves_redirect_column_empty():
    page_diff = PageDiff(url="https://x.ru/old", change_type=ChangeType.REMOVED, old_text="текст")

    row = build_row(page_diff, None, datetime.now(UTC))

    assert row[1] == "Страница удалена"
    assert row[7] == ""


def test_build_row_adds_comparison_with_our_site():
    page_diff = PageDiff(url="https://x.ru/promo", change_type=ChangeType.NEW, new_text="текст")
    comparison = ComparisonResult(
        verdict="similar",
        our_url="https://og1.ru/promo",
        differences="У конкурента есть цена, у нас нет.",
        missing="Добавить отзывы учеников.",
        raw_response={},
    )

    row = build_row(page_diff, None, datetime.now(UTC), comparison=comparison)

    assert "У нас похожее есть" in row[8]
    assert "https://og1.ru/promo" in row[8]
    assert "Отличия: У конкурента есть цена" in row[9]
    assert "Чего нам не хватает: Добавить отзывы учеников." in row[9]


def test_build_row_leaves_comparison_columns_empty_when_not_compared():
    page_diff = PageDiff(url="https://x.ru/promo", change_type=ChangeType.NEW, new_text="текст")

    row = build_row(page_diff, None, datetime.now(UTC))

    assert row[8] == ""
    assert row[9] == ""


def test_client_not_configured_without_any_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr("app.integrations.google_docs.settings.google_service_account_json_path", "")
    monkeypatch.setattr(
        "app.integrations.google_docs.settings.google_oauth_token_path", str(tmp_path / "missing.json")
    )
    client = GoogleDocsClient()
    assert client.is_configured is False


def test_client_configured_via_oauth_token_file(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    monkeypatch.setattr("app.integrations.google_docs.settings.google_service_account_json_path", "")
    monkeypatch.setattr("app.integrations.google_docs.settings.google_oauth_token_path", str(token_path))

    client = GoogleDocsClient()

    assert client.is_configured is True


def test_client_configured_via_service_account_even_without_oauth(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.integrations.google_docs.settings.google_service_account_json_path", "/tmp/sa.json"
    )
    monkeypatch.setattr(
        "app.integrations.google_docs.settings.google_oauth_token_path", str(tmp_path / "missing.json")
    )

    client = GoogleDocsClient()

    assert client.is_configured is True


def test_build_insert_text_requests_processes_highest_index_first():
    cell_starts = [2, 10, 25]
    values = ["a", "b", "c"]

    requests = build_insert_text_requests(cell_starts, values)

    indices = [r["insertText"]["location"]["index"] for r in requests]
    assert indices == [25, 10, 2]
    assert [r["insertText"]["text"] for r in requests] == ["c", "b", "a"]


def test_build_insert_text_requests_skips_empty_values():
    requests = build_insert_text_requests([1, 5], ["", "непустое"])

    assert len(requests) == 1
    assert requests[0]["insertText"]["text"] == "непустое"


class _FakeExecutable:
    def execute(self):
        return {}


class _FakeDocumentsResource:
    def __init__(self, calls: list[dict]):
        self._calls = calls

    def batchUpdate(self, documentId, body):  # noqa: N802 — имя метода задано googleapiclient
        self._calls.append({"documentId": documentId, "body": body})
        return _FakeExecutable()


class _FakeDocsService:
    def __init__(self, calls: list[dict]):
        self._calls = calls

    def documents(self):
        return _FakeDocumentsResource(self._calls)


def test_set_page_layout_swaps_portrait_dimensions_and_narrows_side_margins():
    calls: list[dict] = []
    client = GoogleDocsClient()
    client._docs_service = _FakeDocsService(calls)

    portrait = {
        "width": {"magnitude": 612, "unit": "PT"},
        "height": {"magnitude": 792, "unit": "PT"},
    }
    client._set_page_layout("doc-1", portrait)

    assert len(calls) == 1
    update = calls[0]["body"]["requests"][0]["updateDocumentStyle"]
    assert update["documentStyle"]["pageSize"]["width"]["magnitude"] == 792
    assert update["documentStyle"]["pageSize"]["height"]["magnitude"] == 612
    assert update["documentStyle"]["marginLeft"]["magnitude"] == _PAGE_SIDE_MARGIN_PT
    assert update["documentStyle"]["marginRight"]["magnitude"] == _PAGE_SIDE_MARGIN_PT
    assert update["fields"] == "pageSize,marginLeft,marginRight"


def test_column_width_weights_match_header_and_sum_to_one():
    assert len(_COLUMN_WIDTH_WEIGHTS) == len(_HEADER)
    assert abs(sum(_COLUMN_WIDTH_WEIGHTS) - 1.0) < 1e-9


def test_compute_column_widths_text_columns_wider_than_short_ones():
    widths = compute_column_widths_pt(648)

    assert len(widths) == len(_HEADER)
    assert abs(sum(widths) - 648) < 1  # с учётом округления
    date_col, category_col, summary_col = widths[0], widths[3], widths[6]
    assert summary_col > date_col
    assert summary_col > category_col


def test_landscape_content_width_uses_longer_side_minus_narrow_margins():
    portrait = {
        "width": {"magnitude": 612, "unit": "PT"},
        "height": {"magnitude": 792, "unit": "PT"},
    }

    assert landscape_content_width_pt(portrait) == 792 - 2 * _PAGE_SIDE_MARGIN_PT


def test_build_column_width_requests_one_fixed_width_request_per_column():
    requests = build_column_width_requests(table_start_index=5, widths_pt=[100.0, 250.5])

    assert len(requests) == 2
    first = requests[0]["updateTableColumnProperties"]
    assert first["tableStartLocation"] == {"index": 5}
    assert first["columnIndices"] == [0]
    assert first["tableColumnProperties"]["widthType"] == "FIXED_WIDTH"
    assert first["tableColumnProperties"]["width"] == {"magnitude": 100.0, "unit": "PT"}
    assert requests[1]["updateTableColumnProperties"]["columnIndices"] == [1]
    assert requests[1]["updateTableColumnProperties"]["tableColumnProperties"]["width"]["magnitude"] == 250.5


def test_set_page_layout_keeps_already_landscape_dimensions():
    calls: list[dict] = []
    client = GoogleDocsClient()
    client._docs_service = _FakeDocsService(calls)

    landscape = {
        "width": {"magnitude": 792, "unit": "PT"},
        "height": {"magnitude": 612, "unit": "PT"},
    }
    client._set_page_layout("doc-1", landscape)

    assert len(calls) == 1
    new_page_size = calls[0]["body"]["requests"][0]["updateDocumentStyle"]["documentStyle"]["pageSize"]
    assert new_page_size["width"]["magnitude"] == 792
    assert new_page_size["height"]["magnitude"] == 612
