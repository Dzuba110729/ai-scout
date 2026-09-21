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
from app.ai.compare import ComparisonResult
from app.config import settings
from app.crawler.diff import ChangeType, PageDiff

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_PROJECT_FOLDER_NAME = "AI-Скаут"

_HEADER = [
    "Дата",
    "Тип изменения",
    "URL",
    "Категория",
    "УТП (чем конкурент выделяется)",
    "Призыв к действию",
    "Описание (ИИ)",
    "Куда переехала",
    "Есть ли такое у нас",
    "Чем отличается и чего нам не хватает",
]

_CHANGE_TYPE_LABELS = {
    ChangeType.NEW: "Новая страница",
    ChangeType.CHANGED: "Изменение",
    ChangeType.REMOVED: "Страница удалена",
}

_MOVED_LABEL = "Страница переехала"

# Доля ширины таблицы на каждую колонку (сумма = 1.0), в порядке _HEADER.
# Без этого Google Docs делит таблицу на равные колонки, и текстовые поля
# (УТП, описание) становятся нечитаемо узкими рядом с короткими (Дата, Категория) —
# ровно то, что видно на скриншоте нечитаемого отчёта.
_COLUMN_WIDTH_WEIGHTS = [
    0.09,  # Дата
    0.09,  # Тип изменения
    0.13,  # URL
    0.07,  # Категория
    0.13,  # УТП (чем конкурент выделяется)
    0.09,  # Призыв к действию
    0.15,  # Описание (ИИ)
    0.08,  # Куда переехала
    0.08,  # Есть ли такое у нас
    0.09,  # Чем отличается и чего нам не хватает
]

_PAGE_SIDE_MARGIN_PT = 14.2  # 0,5 см вместо стандартных 2,54 — таблице нужна вся ширина листа
_TABLE_FONT_SIZE_PT = 9  # 10 колонок на альбомном листе — стандартные 11pt не помещаются


class GoogleDocsError(Exception):
    """Сбой обращения к Google Docs/Drive API."""


def build_row(
    page_diff: PageDiff,
    analysis: AiAnalysisResult | None,
    detected_at: datetime,
    *,
    redirect_to: str | None = None,
    redirect_summary: str | None = None,
    comparison: ComparisonResult | None = None,
) -> list[str]:
    """Строка таблицы отчёта.

    redirect_* заполняются, если страница не удалена, а переехала; comparison —
    ответ на вопрос «есть ли такое у нас на сайте» (пусто, если не сравнивали).
    """
    summary_parts = [(analysis.summary if analysis else "") or ""]
    if redirect_summary:
        summary_parts.append(f"Теперь там: {redirect_summary}")

    return [
        detected_at.strftime("%Y-%m-%d\n%H:%M"),
        _MOVED_LABEL if redirect_to else _CHANGE_TYPE_LABELS[page_diff.change_type],
        page_diff.url,
        (analysis.category if analysis else "") or "",
        (analysis.usp if analysis else "") or "",
        (analysis.cta if analysis else "") or "",
        "\n".join(part for part in summary_parts if part),
        redirect_to or "",
        _comparison_verdict_cell(comparison),
        _comparison_details_cell(comparison),
    ]


def _comparison_verdict_cell(comparison: ComparisonResult | None) -> str:
    if comparison is None:
        return ""
    if comparison.our_url:
        return f"{comparison.label}\n{comparison.our_url}"
    return comparison.label


def _comparison_details_cell(comparison: ComparisonResult | None) -> str:
    if comparison is None:
        return ""
    parts = []
    if comparison.differences:
        parts.append(f"Отличия: {comparison.differences}")
    if comparison.missing:
        parts.append(f"Чего нам не хватает: {comparison.missing}")
    return "\n".join(parts)


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


def compute_column_widths_pt(available_width_pt: float) -> list[float]:
    """Ширина каждой колонки в пунктах — доля от доступной ширины по _COLUMN_WIDTH_WEIGHTS."""
    return [round(available_width_pt * weight, 1) for weight in _COLUMN_WIDTH_WEIGHTS]


def build_column_width_requests(table_start_index: int, widths_pt: list[float]) -> list[dict]:
    """Запросы Docs API, задающие фиксированную ширину каждой колонки таблицы."""
    return [
        {
            "updateTableColumnProperties": {
                "tableStartLocation": {"index": table_start_index},
                "columnIndices": [column_index],
                "tableColumnProperties": {
                    "widthType": "FIXED_WIDTH",
                    "width": {"magnitude": width, "unit": "PT"},
                },
                "fields": "widthType,width",
            }
        }
        for column_index, width in enumerate(widths_pt)
    ]


def landscape_content_width_pt(page_size: dict) -> float:
    """Ширина области под таблицу: большая сторона листа минус боковые поля из _set_page_layout.

    Берём большую сторону, а не текущую ширину, потому что документ создаётся портретным
    и разворачивается в альбомный уже после.
    """
    landscape_width = max(page_size["width"]["magnitude"], page_size["height"]["magnitude"])
    return landscape_width - 2 * _PAGE_SIDE_MARGIN_PT


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

    def create_run_document(
        self,
        competitor_folder_id: str,
        run_at: datetime,
        rows: list[list[str]],
        notes: list[str] | None = None,
    ) -> str:
        """Создаёт документ прогона с таблицей находок в папке конкурента. Возвращает url.

        notes — пояснения к прогону простым языком (например, что сайт больше, чем
        мы успели посмотреть); идут отдельным абзацем перед таблицей.
        """
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

            page_size = doc["documentStyle"]["pageSize"]
            self._set_page_layout(doc_id, page_size)
            if notes:
                self._insert_notes(doc_id, notes)
            self._insert_table(doc_id, [_HEADER, *rows], landscape_content_width_pt(page_size))

            return f"https://docs.google.com/document/d/{doc_id}/edit"
        except HttpError as exc:
            raise GoogleDocsError(f"Не удалось создать документ прогона: {exc}") from exc

    def _set_page_layout(self, doc_id: str, page_size: dict) -> None:
        """Альбомная ориентация и узкие боковые поля: таблица находок шире портретной страницы.

        Разворот — обмен ширины/высоты местами (в тех же единицах, что вернул Docs при создании).
        """
        width = page_size["width"]
        height = page_size["height"]
        if width["magnitude"] < height["magnitude"]:
            width, height = height, width

        side_margin = {"magnitude": _PAGE_SIDE_MARGIN_PT, "unit": "PT"}
        self._docs().documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "updateDocumentStyle": {
                            "documentStyle": {
                                "pageSize": {"width": width, "height": height},
                                "marginLeft": side_margin,
                                "marginRight": side_margin,
                            },
                            "fields": "pageSize,marginLeft,marginRight",
                        }
                    }
                ]
            },
        ).execute()

    def _insert_notes(self, doc_id: str, notes: list[str]) -> None:
        text = "\n".join(notes) + "\n\n"
        self._docs().documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]},
        ).execute()

    def _body_end_index(self, doc_id: str) -> int:
        """Индекс, куда можно вставлять новый блок: конец тела минус завершающий перевод строки."""
        doc = self._docs().documents().get(documentId=doc_id).execute()
        return doc["body"]["content"][-1]["endIndex"] - 1

    def _insert_table(self, doc_id: str, all_rows: list[list[str]], available_width_pt: float) -> None:
        n_rows = len(all_rows)
        n_cols = len(all_rows[0])

        # Вставляем в конец документа, а не в индекс 1: перед таблицей уже могут
        # стоять пояснения к прогону, и жёсткая единица затолкала бы таблицу над ними.
        index = self._body_end_index(doc_id)
        self._docs().documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertTable": {"rows": n_rows, "columns": n_cols, "location": {"index": index}}}]},
        ).execute()

        doc = self._docs().documents().get(documentId=doc_id).execute()
        table_element = next(element for element in doc["body"]["content"] if "table" in element)
        table = table_element["table"]

        cell_starts = [
            cell["content"][0]["startIndex"]
            for table_row in table["tableRows"]
            for cell in table_row["tableCells"]
        ]
        flat_values = [value for row in all_rows for value in row]

        # Ширина колонок идёт первой: она ссылается на начало таблицы, которое
        # вставка текста в ячейки не сдвигает — порядок безопасен.
        widths_pt = compute_column_widths_pt(available_width_pt)
        requests = build_column_width_requests(table_element["startIndex"], widths_pt)
        requests.extend(build_insert_text_requests(cell_starts, flat_values))
        self._docs().documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()

        self._set_table_font_size(doc_id)

    def _set_table_font_size(self, doc_id: str) -> None:
        # Границы таблицы берём заново: после вставки текста её конец сдвинулся.
        doc = self._docs().documents().get(documentId=doc_id).execute()
        table_element = next(element for element in doc["body"]["content"] if "table" in element)
        self._docs().documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": table_element["startIndex"],
                                "endIndex": table_element["endIndex"],
                            },
                            "textStyle": {"fontSize": {"magnitude": _TABLE_FONT_SIZE_PT, "unit": "PT"}},
                            "fields": "fontSize",
                        }
                    }
                ]
            },
        ).execute()
