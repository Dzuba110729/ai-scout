"""Google Таблица SEO-находок: один файл на всех конкурентов (этап 5).

Лист на каждую неделю («Неделя 19.10–25.10.2026») и лист «Стартовый аудит». Строка —
тема (TopicDecision) за период: одна тема у нескольких конкурентов — одна строка,
домены и адреса — с новой строки в ячейке. Только инфоконтент: коммерческие находки
остаются в отчёте Google Docs для продакта.

Колонки — как просил SEO-специалист (2026-10-19): Дата, Действие, Конкурент, URL,
Категория, Краткое описание, УТП (подинтент), Наличие у нас, Что добавить.

Лист каждый раз переписывается целиком из базы: склейка тем, пришедших от разных
конкурентов в разные дни недели, видна сразу, а повторная выгрузка ничего не дублирует.

Доступ: тот же токен, что у отчётов Google Docs. В нём полный доступ к Drive, а API
Таблиц его принимает — переавторизация не нужна, но в Google Cloud Console у
OAuth-приложения должен быть включён Google Sheets API (см. README).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.google_docs import GoogleDocsClient
from app.models import AppState, Competitor, Page, SeoFinding, TopicDecision

logger = logging.getLogger(__name__)

SPREADSHEET_STATE_KEY = "seo_spreadsheet_id"
SPREADSHEET_TITLE = "AI-Скаут — инфостатьи конкурентов (SEO)"
AUDIT_SHEET_TITLE = "Стартовый аудит"

HEADER = [
    "Дата",
    "Действие",
    "Конкурент",
    "URL",
    "Категория",
    "Краткое описание",
    "УТП (подинтент)",
    "Наличие у нас",
    "Что добавить",
]
_COLUMN_WIDTHS_PX = [90, 100, 130, 300, 100, 300, 280, 240, 360]

_ACTION_LABELS = {"new": "создание", "changed": "изменение", "audit": "аудит (статья уже есть)"}
_SUBINTENTS_SHOWN = 4


class SeoSheetError(Exception):
    """Сбой обращения к Google Sheets/Drive API."""


@dataclass
class SheetFinding:
    """Одна статья конкурента в теме — всё, что нужно для строки таблицы."""

    decision_id: int
    topic: str
    main_intent: str
    material_type: str | None
    decision: str
    our_url: str | None
    add_subintents: list[str]
    argument: str | None
    cannibal_urls: list[str]
    subintents: list[str]
    competitor: str  # домен
    url: str
    kind: str
    added_subintents: list[str] = field(default_factory=list)
    detected_at: datetime | None = None


def _domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _unique(items) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _availability(item: SheetFinding) -> str:
    if item.decision == "new" or not item.our_url:
        text = "Нет"
    elif item.decision == "update":
        text = f"Частично\n{item.our_url}"
    else:
        text = f"Есть\n{item.our_url}"
    if len(item.cannibal_urls) > 1:
        text += "\n⚠️ Риск каннибализации:\n" + "\n".join(item.cannibal_urls)
    return text


def _what_to_add(item: SheetFinding) -> str:
    argument = f" {item.argument}" if item.argument else ""
    if item.decision == "new":
        return f"Написать новую статью.{argument}"
    if item.decision == "update":
        add = "; ".join(item.add_subintents) or "раскрыть тему подробнее"
        return f"Обновить нашу статью: добавить {add}.{argument}"
    return f"Ничего — у нас раскрыто.{argument}"


def build_sheet_rows(findings: list[SheetFinding], tz: ZoneInfo | None = None) -> list[list[str]]:
    """Строки листа: одна на тему, в порядке первого появления."""
    tz = tz or ZoneInfo(settings.digest_timezone)
    groups: dict[int, list[SheetFinding]] = {}
    for item in sorted(findings, key=lambda f: (f.detected_at or datetime.min.replace(tzinfo=tz), f.url)):
        groups.setdefault(item.decision_id, []).append(item)

    rows = []
    for items in groups.values():
        head = items[0]
        dates = [item.detected_at for item in items if item.detected_at]
        kinds = _unique(item.kind for item in items)
        added = _unique(sub for item in items for sub in item.added_subintents)
        if added:
            usp = "Добавлен подинтент: " + "; ".join(added)
        else:
            shown = head.subintents[:_SUBINTENTS_SHOWN]
            usp = "Подинтенты: " + "; ".join(shown) if shown else ""
            if len(head.subintents) > len(shown):
                usp += f" (и ещё {len(head.subintents) - len(shown)})"
        rows.append(
            [
                min(dates).astimezone(tz).strftime("%d.%m.%Y") if dates else "",
                "\n".join(_ACTION_LABELS.get(kind, kind) for kind in kinds),
                "\n".join(_unique(item.competitor for item in items)),
                "\n".join(_unique(item.url for item in items)),
                head.material_type or "",
                f"{head.topic}. Интент: {head.main_intent}",
                usp,
                _availability(head),
                _what_to_add(head),
            ]
        )
    return rows


def week_bounds(when: datetime, tz: ZoneInfo | None = None) -> tuple[datetime, datetime, str]:
    """Начало и конец недели (пн–вс по Москве) и название листа."""
    tz = tz or ZoneInfo(settings.digest_timezone)
    local = when.astimezone(tz)
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    title = f"Неделя {start:%d.%m}–{end - timedelta(days=1):%d.%m.%Y}"
    return start, end, title


def load_sheet_findings(
    db: Session, *, start: datetime | None = None, end: datetime | None = None, audit: bool = False
) -> list[SheetFinding]:
    query = (
        db.query(SeoFinding, TopicDecision, Page, Competitor)
        .join(TopicDecision, TopicDecision.id == SeoFinding.decision_id)
        .join(Page, Page.id == SeoFinding.page_id)
        .join(Competitor, Competitor.id == SeoFinding.competitor_id)
    )
    if audit:
        query = query.filter(SeoFinding.kind == "audit")
    else:
        query = query.filter(SeoFinding.kind.in_(("new", "changed")))
    if start is not None:
        query = query.filter(SeoFinding.detected_at >= start)
    if end is not None:
        query = query.filter(SeoFinding.detected_at < end)
    return [
        SheetFinding(
            decision_id=decision.id,
            topic=decision.topic,
            main_intent=decision.main_intent,
            material_type=decision.material_type,
            decision=decision.decision,
            our_url=decision.our_url,
            add_subintents=list(decision.add_subintents or []),
            argument=decision.argument,
            cannibal_urls=list(decision.cannibal_urls or []),
            subintents=list(decision.subintents or []),
            competitor=_domain(competitor.base_url),
            url=page.url,
            kind=finding.kind,
            added_subintents=list(finding.added_subintents or []),
            detected_at=finding.detected_at if finding.detected_at.tzinfo else finding.detected_at.replace(
                tzinfo=ZoneInfo("UTC")
            ),
        )
        for finding, decision, page, competitor in query.all()
    ]


def get_state(db: Session, key: str) -> str | None:
    row = db.get(AppState, key)
    return row.value if row else None


def set_state(db: Session, key: str, value: str | None) -> None:
    row = db.get(AppState, key)
    if row is None:
        db.add(AppState(key=key, value=value))
    else:
        row.value = value
    db.flush()


def _quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


class SeoSheetClient(GoogleDocsClient):
    """Таблица в папке проекта «AI-Скаут» на Google Drive — рядом с отчётами."""

    def __init__(self) -> None:
        super().__init__()
        self._sheets_service = None

    def _sheets(self):
        if self._sheets_service is None:
            self._sheets_service = build("sheets", "v4", credentials=self._credentials())
        return self._sheets_service

    def _sheet_ids(self, spreadsheet_id: str) -> dict[str, int]:
        meta = self._sheets().spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
        return {sheet["properties"]["title"]: sheet["properties"]["sheetId"] for sheet in meta.get("sheets", [])}

    def ensure_spreadsheet(self, spreadsheet_id: str | None, first_sheet: str) -> str:
        """Уже созданная таблица (если её не удалили) или новая в папке проекта."""
        if spreadsheet_id:
            try:
                self._sheet_ids(spreadsheet_id)
                return spreadsheet_id
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
                logger.warning("SEO-таблица %s не найдена — создаём новую", spreadsheet_id)
        body = {"properties": {"title": SPREADSHEET_TITLE}, "sheets": [{"properties": {"title": first_sheet}}]}
        created = self._sheets().spreadsheets().create(body=body, fields="spreadsheetId").execute()
        new_id = created["spreadsheetId"]
        folder_id = self._get_or_create_project_folder()
        file = self._drive().files().get(fileId=new_id, fields="parents").execute()
        self._drive().files().update(
            fileId=new_id, addParents=folder_id, removeParents=",".join(file.get("parents", [])), fields="id"
        ).execute()
        return new_id

    def write_sheet(self, spreadsheet_id: str, title: str, rows: list[list[str]], *, index: int | None = 0) -> int:
        """Переписывает лист целиком (создаёт, если его нет). Возвращает sheetId."""
        sheets = self._sheet_ids(spreadsheet_id)
        if title in sheets:
            sheet_id = sheets[title]
        else:
            properties: dict = {"title": title, "gridProperties": {"frozenRowCount": 1}}
            if index is not None:
                properties["index"] = index
            reply = (
                self._sheets()
                .spreadsheets()
                .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{"addSheet": {"properties": properties}}]})
                .execute()
            )
            sheet_id = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
        values = self._sheets().spreadsheets().values()
        values.clear(spreadsheetId=spreadsheet_id, range=_quote(title)).execute()
        values.update(
            spreadsheetId=spreadsheet_id,
            range=f"{_quote(title)}!A1",
            valueInputOption="RAW",
            body={"values": [HEADER, *rows]},
        ).execute()
        self._sheets().spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": build_format_requests(sheet_id)}
        ).execute()
        return sheet_id


def build_format_requests(sheet_id: int) -> list[dict]:
    """Жирная закреплённая шапка, перенос текста, ширина колонок, текст сверху ячейки."""
    requests: list[dict] = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.91, "green": 0.94, "blue": 0.99},
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        },
    ]
    for column, width in enumerate(_COLUMN_WIDTHS_PX):
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": column, "endIndex": column + 1},
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )
    return requests


def _sheet_url(spreadsheet_id: str, sheet_id: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}"


def _export(db: Session, title: str, rows: list[list[str]], *, index: int | None) -> str | None:
    client = SeoSheetClient()
    if not client.is_configured:
        return None
    try:
        spreadsheet_id = client.ensure_spreadsheet(get_state(db, SPREADSHEET_STATE_KEY), title)
        set_state(db, SPREADSHEET_STATE_KEY, spreadsheet_id)
        return _sheet_url(spreadsheet_id, client.write_sheet(spreadsheet_id, title, rows, index=index))
    except HttpError as exc:
        logger.warning("Не удалось выгрузить SEO-таблицу (лист «%s»): %s", title, exc)
        return None


def export_week(db: Session, when: datetime) -> str | None:
    """Лист недели, в которую попал обход. Ссылка на лист или None (Google не настроен,
    API Таблиц выключен, сеть). Блокирующая — из обхода только через asyncio.to_thread."""
    start, end, title = week_bounds(when)
    return _export(db, title, build_sheet_rows(load_sheet_findings(db, start=start, end=end)), index=0)


def export_audit(db: Session) -> str | None:
    """Лист «Стартовый аудит» — все статьи, разобранные аудитом (scripts/seo_audit.py)."""
    return _export(db, AUDIT_SHEET_TITLE, build_sheet_rows(load_sheet_findings(db, audit=True)), index=None)
