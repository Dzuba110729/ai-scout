from datetime import UTC, datetime

from app.ai.analyze import AiAnalysisResult
from app.crawler.diff import ChangeType, PageDiff
from app.integrations.google_docs import (
    GoogleDocsClient,
    build_insert_text_requests,
    build_row,
)


def test_build_row_new_page_with_analysis():
    page_diff = PageDiff(url="https://x.ru/promo", change_type=ChangeType.NEW, new_text="текст")
    analysis = AiAnalysisResult(
        category="лендинг", usp="Скидка 30%", cta="Записаться", summary="Новая акция", raw_response={}
    )
    detected_at = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)

    row = build_row(page_diff, analysis, detected_at)

    assert row[0] == "2026-08-10 12:30"
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


def test_set_landscape_orientation_swaps_portrait_dimensions():
    calls: list[dict] = []
    client = GoogleDocsClient()
    client._docs_service = _FakeDocsService(calls)

    portrait = {
        "width": {"magnitude": 612, "unit": "PT"},
        "height": {"magnitude": 792, "unit": "PT"},
    }
    client._set_landscape_orientation("doc-1", portrait)

    assert len(calls) == 1
    new_page_size = calls[0]["body"]["requests"][0]["updateDocumentStyle"]["documentStyle"]["pageSize"]
    assert new_page_size["width"]["magnitude"] == 792
    assert new_page_size["height"]["magnitude"] == 612


def test_set_landscape_orientation_leaves_already_landscape_untouched():
    calls: list[dict] = []
    client = GoogleDocsClient()
    client._docs_service = _FakeDocsService(calls)

    landscape = {
        "width": {"magnitude": 792, "unit": "PT"},
        "height": {"magnitude": 612, "unit": "PT"},
    }
    client._set_landscape_orientation("doc-1", landscape)

    assert calls == []
