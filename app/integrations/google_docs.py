"""Отчёты об обходах конкурентов в Google Docs/Drive.

Структура в Google Drive: папка проекта -> папка конкурента -> документ на
каждый прогон (с таблицей находок внутри), назван датой обхода. Доступ "любой
по ссылке" настраивается один раз на папке проекта и наследуется всеми
вложенными папками и документами — не нужно шарить каждый файл отдельно.

Два способа авторизации (см. app/config.py):
- личный Google-аккаунт через OAuth (см. scripts/google_oauth_login.py) —
  файлы создаются прямо в "Моём диске" пользователя;
- сервисный аккаунт — если задан GOOGLE_SERVICE_ACCOUNT_JSON_PATH, имеет
  приоритет над OAuth, файлы принадлежат робот-аккаунту.
"""

import logging
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.ai.analyze import AiAnalysisResult
from app.config import settings
from app.crawler.diff import ChangeType, PageDiff

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_PROJECT_FOLDER_NAME = "AI-Скаут"

_HEADER = ["Дата", "Тип изменения", "URL", "Категория", "УТП", "CTA", "Описание (ИИ)"]

_CHANGE_TYPE_LABELS = {
    ChangeType.NEW: "Новая страница",
    ChangeType.CHANGED: "Изменение",
    ChangeType.REMOVED: "Страница удалена",
}


class GoogleDocsError(Exception):
    """Сбой обращения к Google Docs/Drive API."""


def build_row(page_diff: PageDiff, analysis: AiAnalysisResult | None, detected_at: datetime) -> list[str]:
    return [
        detected_at.strftime("%Y-%m-%d %H:%M"),
        _CHANGE_TYPE_LABELS[page_diff.change_type],
        page_diff.url,
        (analysis.category if analysis else "") or "",
        (analysis.usp if analysis else "") or "",
        (analysis.cta if analysis else "") or "",
        (analysis.summary if analysis else "") or "",
    ]


def build_insert_text_requests(cell_starts: list[int], values: list[str]) -> list[dict]:
    """Запросы вставки текста в ячейки таблицы, от последней ячейки к первой.

    Индексы ячеек считаются относительно документа ДО вставки текста. Вставка
    в более позднюю (большую по индексу) ячейку не сдвигает индексы более
    ранних ячеек — поэтому идти нужно строго от конца к началу, иначе
    сохранённые индексы "уедут" после первой же вставки.
    """
    pairs = sorted(zip(cell_starts, values), key=lambda pair: pair[0], reverse=True)
    return [
        {"insertText": {"location": {"index": index}, "text": text}}
        for index, text in pairs
        if text
    ]


def _load_oauth_user_credentials(token_path: str) -> UserCredentials:
    """Читает токен личного аккаунта, полученный через scripts/google_oauth_login.py,
    и обновляет access_token по refresh_token, если он истёк — без участия человека."""
    credentials = UserCredentials.from_authorized_user_file(token_path, _SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        Path(token_path).write_text(credentials.to_json())
    return credentials


class GoogleDocsClient:
    def __init__(self) -> None:
        self._docs_service = None
        self._drive_service = None
        self._project_folder_id: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(settings.google_service_account_json_path) or Path(
            settings.google_oauth_token_path
        ).exists()

    def _credentials(self):
        if settings.google_service_account_json_path:
            return ServiceAccountCredentials.from_service_account_file(
                settings.google_service_account_json_path, scopes=_SCOPES
            )
        return _load_oauth_user_credentials(settings.google_oauth_token_path)

    def _docs(self):
        if self._docs_service is None:
            self._docs_service = build("docs", "v1", credentials=self._credentials())
        return self._docs_service

    def _drive(self):
        if self._drive_service is None:
            self._drive_service = build("drive", "v3", credentials=self._credentials())
        return self._drive_service

    def _find_folder(self, name: str, parent_id: str) -> str | None:
        query = (
            f"name = '{name}' and mimeType = '{_FOLDER_MIME_TYPE}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        response = self._drive().files().list(q=query, fields="files(id)").execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, name: str, parent_id: str) -> str:
        metadata = {"name": name, "mimeType": _FOLDER_MIME_TYPE, "parents": [parent_id]}
        folder = self._drive().files().create(body=metadata, fields="id").execute()
        return folder["id"]

    def _share_anyone_reader(self, file_id: str) -> None:
        """Не критично для основной функции — если политика аккаунта запрещает
        публичные ссылки (частое ограничение в Workspace-организациях), просто
        логируем и продолжаем: владелец аккаунта всё равно видит файл в Drive."""
        try:
            self._drive().permissions().create(
                fileId=file_id, body={"role": "reader", "type": "anyone"}
            ).execute()
        except HttpError:
            logger.warning("Не удалось выдать доступ 'любой по ссылке' для %s", file_id)

    def _get_or_create_project_folder(self) -> str:
        if self._project_folder_id:
            return self._project_folder_id

        query = f"name = '{_PROJECT_FOLDER_NAME}' and mimeType = '{_FOLDER_MIME_TYPE}' and trashed = false"
        response = self._drive().files().list(q=query, fields="files(id)").execute()
        files = response.get("files", [])
        if files:
            self._project_folder_id = files[0]["id"]
        else:
            metadata = {"name": _PROJECT_FOLDER_NAME, "mimeType": _FOLDER_MIME_TYPE}
            folder = self._drive().files().create(body=metadata, fields="id").execute()
            self._project_folder_id = folder["id"]
            self._share_anyone_reader(self._project_folder_id)
        return self._project_folder_id

    def get_or_create_competitor_folder(self, competitor_name: str) -> tuple[str, str]:
        """Папка конкурента внутри папки проекта. Возвращает (folder_id, url)."""
        if not self.is_configured:
            raise GoogleDocsError("Google не настроен: заполните OAuth или сервисный аккаунт в .env")

        try:
            project_folder_id = self._get_or_create_project_folder()
            folder_id = self._find_folder(competitor_name, project_folder_id)
            if not folder_id:
                folder_id = self._create_folder(competitor_name, project_folder_id)
            return folder_id, f"https://drive.google.com/drive/folders/{folder_id}"
        except HttpError as exc:
            raise GoogleDocsError(f"Не удалось создать папку конкурента: {exc}") from exc

    def create_run_document(self, competitor_folder_id: str, run_at: datetime, rows: list[list[str]]) -> str:
        """Создаёт документ прогона с таблицей находок в папке конкурента. Возвращает url."""
        if not self.is_configured:
            raise GoogleDocsError("Google не настроен: заполните OAuth или сервисный аккаунт в .env")

        title = run_at.strftime("Обход %d.%m.%Y %H:%M")
        try:
            doc = self._docs().documents().create(body={"title": title}).execute()
            doc_id = doc["documentId"]

            file = self._drive().files().get(fileId=doc_id, fields="parents").execute()
            previous_parents = ",".join(file.get("parents", []))
            self._drive().files().update(
                fileId=doc_id,
                addParents=competitor_folder_id,
                removeParents=previous_parents,
                fields="id, parents",
            ).execute()

            self._set_landscape_orientation(doc_id, doc["documentStyle"]["pageSize"])
            self._insert_table(doc_id, [_HEADER, *rows])

            return f"https://docs.google.com/document/d/{doc_id}/edit"
        except HttpError as exc:
            raise GoogleDocsError(f"Не удалось создать документ прогона: {exc}") from exc

    def _set_landscape_orientation(self, doc_id: str, page_size: dict) -> None:
        """Таблица находок обычно шире портретной страницы — переворачиваем в альбомную,
        меняя местами ширину/высоту (в тех же единицах, что вернул Docs при создании)."""
        width = page_size["width"]
        height = page_size["height"]
        if width["magnitude"] >= height["magnitude"]:
            return  # уже альбомная

        self._docs().documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "updateDocumentStyle": {
                            "documentStyle": {"pageSize": {"width": height, "height": width}},
                            "fields": "pageSize",
                        }
                    }
                ]
            },
        ).execute()

    def _insert_table(self, doc_id: str, all_rows: list[list[str]]) -> None:
        n_rows = len(all_rows)
        n_cols = len(all_rows[0])

        self._docs().documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertTable": {"rows": n_rows, "columns": n_cols, "location": {"index": 1}}}]},
        ).execute()

        doc = self._docs().documents().get(documentId=doc_id).execute()
        table = next(element["table"] for element in doc["body"]["content"] if "table" in element)

        cell_starts = [
            cell["content"][0]["startIndex"]
            for table_row in table["tableRows"]
            for cell in table_row["tableCells"]
        ]
        flat_values = [value for row in all_rows for value in row]

        requests = build_insert_text_requests(cell_starts, flat_values)
        if requests:
            self._docs().documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
