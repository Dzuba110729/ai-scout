"""Telegram-бот: постоянное меню, разделы с кнопками и ИИ-помощник для обычного текста.

Работает поверх той же логики, что и веб-интерфейс (app/crawl_manager.py,
app/competitor_ops.py, app/scheduler.py) — оба входа равноправны, ни один не знает
про другой. Тексты экранов собирает app/bot_views.py, свободный текст разбирает
app/ai/assistant.py (claude -p), а этот модуль только раскладывает их по сообщениям
и кнопкам и выполняет выбранные действия.

Поднимается long polling'ом (без вебхука — не нужен публичный HTTPS для внутреннего
инструмента, см. CLAUDE.md) как фоновая asyncio-задача в app/main.py::lifespan,
на том же event loop, что и APScheduler и все обходы. Именно поэтому вызовы
crawl_manager.start/stop отсюда безопасны без дополнительных мер (см. память
про RuntimeError: no running event loop при вызове из чужого потока).

Порядок обработчиков важен: кнопки постоянного меню и команды срабатывают в любом
состоянии (и сбрасывают его), затем шаги добавления конкурента / смены адреса,
и только всё остальное уходит ИИ-помощнику.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from app import bot_views, competitor_ops, crawl_manager, digest
from app.ai import assistant
from app.ai.assistant import AssistantAction, ClaudeCliError
from app.config import settings
from app.db import SessionLocal
from app.models import Competitor, PageChange
from app.own_site import get_own_site
from app.scheduler import set_interval

logger = logging.getLogger(__name__)

router = Router(name="ai_scout_menu")

BTN_SUMMARY = "📊 Сводка"
BTN_COMPETITORS = "🏢 Конкуренты"
BTN_CHANGES = "🆕 Находки"
BTN_REPORTS = "📄 Отчёты"
BTN_CRAWL_ALL = "🔄 Обойти всех"
BTN_OWN_SITE = "🌐 Наш сайт"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_SUMMARY), KeyboardButton(text=BTN_COMPETITORS)],
        [KeyboardButton(text=BTN_CHANGES), KeyboardButton(text=BTN_REPORTS)],
        [KeyboardButton(text=BTN_CRAWL_ALL), KeyboardButton(text=BTN_OWN_SITE)],
        [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Кнопка меню или вопрос обычным языком",
)

BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="summary", description="Сводка"),
    BotCommand(command="competitors", description="Конкуренты"),
    BotCommand(command="changes", description="Последние находки"),
    BotCommand(command="digest", description="Дайджест за неделю"),
    BotCommand(command="reports", description="Отчёты об обходах"),
    BotCommand(command="settings", description="Расписание и подключения"),
    BotCommand(command="help", description="Что умеет бот"),
]

# Кнопки интервала в настройках: (подпись, дни, часы).
SCHEDULE_PRESETS = [
    ("Каждый день", 1, 0),
    ("Раз в 3 дня", 3, 0),
    ("Раз в неделю", 7, 0),
    ("Раз в 2 недели", 14, 0),
]

MAX_SCHEDULE_DAYS = 90

HELP_TEXT = """❓ Что умеет бот

Кнопки внизу чата — всегда под рукой:
— 📊 Сводка: что происходит сейчас, сколько находок, когда следующий обход
— 🏢 Конкуренты: список, карточка, обход, пауза, добавление и удаление
— 🆕 Находки: лента новых, изменённых и удалённых страниц с фильтрами; нажмите номер — откроется подробный разбор
— 📄 Отчёты: ссылки на последние отчёты в Google Docs и папки со всеми отчётами
— 🔄 Обойти всех: запустить обход всех конкурентов прямо сейчас
— 🌐 Наш сайт: адрес и обход нашего сайта (с ним сравниваются находки)
— ⚙️ Настройки: как часто обходить, что подключено

После каждого обхода бот сам пришлёт итог, 🔥 важные находки и кнопку «📄 Открыть отчёт».
Раз в неделю — дайджест: главное за неделю, чего нет у нас, что требует внимания.
Его можно запросить и сейчас: /digest или кнопка в «📊 Сводке».

🔥 — находки, которые ИИ счёл важными: цены, акции, новые продукты и офферы.

Можно писать обычным языком, например:
— «что нового у фоксфорда за неделю?»
— «обойди фоксфорд и пришли отчёт»
— «добавь конкурента skysmart.ru»
— «поставь умскул на паузу»
— «обходи всех раз в 3 дня»
— «какие цены поменялись?»
— «что важного за неделю?»

/start — вернуть меню, если оно пропало."""


class AddCompetitor(StatesGroup):
    name = State()
    base_url = State()


class EditOwnSite(StatesGroup):
    url = State()


class AllowlistMiddleware(BaseMiddleware):
    """Тихо игнорирует апдейты от чатов вне settings.telegram_allowed_chat_ids()."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = data.get("event_chat")
        if chat is not None and str(chat.id) not in settings.telegram_allowed_chat_ids():
            logger.warning("Telegram-бот: отклонён чат %s (нет в списке разрешённых)", chat.id)
            return None
        return await handler(event, data)


# ---------------------------------------------------------------- вывод на экран


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup | None:
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


async def _show(
    target: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    new_message: bool = False,
) -> None:
    """Сообщение — отвечаем новым; нажатие кнопки — правим то же сообщение на месте,
    чтобы разделы не плодили простыню в чате."""
    if isinstance(target, CallbackQuery):
        if not new_message and target.message is not None:
            try:
                await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
                logger.debug("Не удалось отредактировать сообщение, шлём новое: %s", exc)
        if target.message is not None:
            await target.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        return
    await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ---------------------------------------------------------------- разделы


def _summary_screen(db) -> tuple[str, InlineKeyboardMarkup | None]:
    return bot_views.summary_text(db), _kb(
        [
            [_btn("🔥 Важные находки", "chg:all:important:1"), _btn("🆕 Все находки", "chg:all:all:1")],
            [_btn("🗞 Дайджест за неделю", "sec:digest"), _btn("📄 Отчёты", "sec:reports")],
            [_btn("🔄 Обновить", "sec:summary")],
        ]
    )


def _competitors_screen(db) -> tuple[str, InlineKeyboardMarkup | None]:
    competitors = bot_views.list_competitors(db)
    rows = [[_btn(f"{c.name} — {bot_views.status_line(c)}", f"comp:{c.id}")] for c in competitors]
    rows.append([_btn("➕ Добавить конкурента", "menu:add")])
    if competitors:
        rows.append([_btn("🔄 Обойти всех", "menu:crawl_all")])
    text = "🏢 Конкуренты" if competitors else "🏢 Конкурентов пока нет. Добавьте первого:"
    return text, _kb(rows)


def _competitor_screen(db, competitor: Competitor) -> tuple[str, InlineKeyboardMarkup | None]:
    cid = competitor.id
    rows = [
        [_btn("⏹ Остановить обход", f"comp:{cid}:stop")]
        if competitor.is_crawling
        else [_btn("▶️ Обойти сейчас", f"comp:{cid}:crawl")],
        [_btn("▶️ Снять с паузы", f"comp:{cid}:resume")]
        if competitor.is_paused
        else [_btn("⏸ Пауза", f"comp:{cid}:pause")],
        [_btn("🆕 Находки", f"chg:c{cid}:all:1")],
        [
            *([_url_btn("📄 Последний отчёт", competitor.last_report_url)] if competitor.last_report_url else []),
            *(
                [_url_btn("📁 Все отчёты", competitor.google_drive_folder_url)]
                if competitor.google_drive_folder_url
                else []
            ),
        ],
        [_url_btn("🌐 Открыть сайт", competitor.base_url), _btn("🔄 Обновить", f"comp:{cid}")],
        [_btn("🗑 Удалить", f"comp:{cid}:delete"), _btn("← К списку", "sec:competitors")],
    ]
    return bot_views.competitor_card_text(db, competitor), _kb(rows)


def _changes_screen(db, scope: str, kind: str, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    competitor = None
    if scope.startswith("c"):
        competitor = db.get(Competitor, int(scope[1:]))
        if competitor is None:
            return "Конкурент не найден — возможно, уже удалён.", None
    if kind not in bot_views.CHANGE_KINDS:
        kind = "all"

    result = bot_views.load_changes(db, competitor_id=competitor.id if competitor else None, kind=kind, page=page)
    text = bot_views.changes_text(db, result, competitor=competitor, kind=kind)
    # Номера перед блоками — чтобы было понятно, какая кнопка какую находку откроет.
    if result.items:
        head, *blocks = text.split("\n\n")
        text = "\n\n".join([head, *(f"{i}. {block}" for i, block in enumerate(blocks, start=1))])

    filters = [
        _btn(("• " if k == kind else "") + label, f"chg:{scope}:{k}:1")
        for k, label in bot_views.CHANGE_KIND_LABELS.items()
    ]
    rows = [
        [_btn(str(i), f"chd:{c.id}:{scope}:{kind}:{result.page}") for i, c in enumerate(result.items, start=1)],
        filters[:2],
        filters[2:],
    ]
    nav = []
    if result.page > 1:
        nav.append(_btn("◀️", f"chg:{scope}:{kind}:{result.page - 1}"))
    if result.total_pages > 1:
        nav.append(_btn(f"{result.page}/{result.total_pages}", f"chg:{scope}:{kind}:{result.page}"))
    if result.page < result.total_pages:
        nav.append(_btn("▶️", f"chg:{scope}:{kind}:{result.page + 1}"))
    rows.append(nav)
    if competitor:
        rows.append([_btn("← К конкуренту", f"comp:{competitor.id}"), _btn("Все конкуренты", f"chg:all:{kind}:1")])
    return text, _kb(rows)


def _change_detail_screen(db, change: PageChange, back: str) -> tuple[str, InlineKeyboardMarkup | None]:
    page = change.page
    lines = [bot_views.change_line(change, summary_limit=1000)]
    analysis = change.ai_analysis
    if analysis:
        if analysis.importance == "high" and analysis.importance_reason:
            lines.append(f"🔥 Почему важно: {analysis.importance_reason}")
        if analysis.category:
            lines.append(f"Тип страницы: {analysis.category}")
        if analysis.usp:
            lines.append(f"УТП: {analysis.usp}")
        if analysis.cta:
            lines.append(f"Призыв к действию: {analysis.cta}")
    comparison = change.own_comparison
    if comparison:
        verdict = {"exact": "✅ У нас такое есть", "similar": "🟡 У нас похожее есть", "none": "❌ У нас такого нет"}
        lines.append("")
        lines.append(verdict.get(comparison.verdict.value, comparison.verdict.value))
        if comparison.our_page_url:
            lines.append(comparison.our_page_url)
        if comparison.differences:
            lines.append(f"Отличия: {comparison.differences}")
        if comparison.missing:
            lines.append(f"Чего нам не хватает: {comparison.missing}")
    if change.diff_text:
        diff = change.diff_text.strip()
        lines.append("")
        lines.append("Что поменялось в тексте (− было, + стало):")
        lines.append(diff[:1500] + ("\n…" if len(diff) > 1500 else ""))

    text = "\n".join(lines)[:4000]
    rows = [
        [_url_btn("🌐 Открыть страницу", page.url)],
        [_btn("← К ленте", back)],
    ]
    return text, _kb(rows)


def _reports_screen(db) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = []
    for c in bot_views.list_competitors(db):
        row = []
        if c.last_report_url:
            row.append(_url_btn(f"📄 {c.name}", c.last_report_url))
        if c.google_drive_folder_url:
            row.append(_url_btn("📁 все", c.google_drive_folder_url))
        rows.append(row)
    return bot_views.reports_text(db), _kb(rows)


def _own_site_screen(db) -> tuple[str, InlineKeyboardMarkup | None]:
    own = get_own_site(db)
    if own is None:
        return (
            "🌐 Наш сайт ещё не указан. С ним бот сравнивает находки у конкурентов: «есть ли у нас такое».",
            _kb([[_btn("✏️ Указать адрес", "own:edit")]]),
        )
    text = (
        f"🌐 {own.name}\n{own.base_url}\n\n"
        f"Статус: {bot_views.status_line(own)}\n"
        f"Последний обход: {bot_views.fmt_dt(own.last_crawl_finished_at, 'ещё не было')}"
    )
    progress = bot_views.crawl_progress(db, own)
    if progress:
        text += f"\nПрогресс: {progress}"
    action = _btn("⏹ Остановить обход", "own:stop") if own.is_crawling else _btn("▶️ Обойти сейчас", "own:crawl")
    return text, _kb(
        [
            [action],
            [_btn("✏️ Сменить адрес", "own:edit"), _url_btn("🌐 Открыть", own.base_url)],
        ]
    )


def _settings_screen(db) -> tuple[str, InlineKeyboardMarkup | None]:
    presets = [_btn(label, f"set:{days}:{hours}") for label, days, hours in SCHEDULE_PRESETS]
    text = bot_views.settings_text(db) + f"\n\nДайджест: {digest.schedule_label()} (меняется в .env: DIGEST_*)"
    return text, _kb([presets[:2], presets[2:], [_btn("🔄 Обновить", "sec:settings")]])


SECTION_SCREENS = {
    "summary": _summary_screen,
    "competitors": _competitors_screen,
    "reports": _reports_screen,
    "own_site": _own_site_screen,
    "settings": _settings_screen,
}


async def _show_section(target: Message | CallbackQuery, section: str, *, new_message: bool = False) -> None:
    if section == "help":
        await _show(target, HELP_TEXT, new_message=new_message)
        return
    if section == "changes":
        await _show_changes(target, "all", "all", 1, new_message=new_message)
        return
    if section == "digest":
        await _show_digest(target)
        return
    db = SessionLocal()
    try:
        text, markup = SECTION_SCREENS.get(section, _summary_screen)(db)
    finally:
        db.close()
    await _show(target, text, markup, new_message=new_message)


async def _show_digest(target: Message | CallbackQuery) -> None:
    """Дайджест собирается с абзацем от ИИ (секунды) — показываем, что думаем,
    и отвечаем новым сообщением: его удобно переслать коллегам."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        return
    placeholder = await message.answer("🗞 Собираю дайджест…")
    db = SessionLocal()
    try:
        result = await digest.make_digest(db)
    finally:
        db.close()
    markup = InlineKeyboardMarkup(inline_keyboard=result.buttons) if result.buttons else None
    await placeholder.edit_text(result.text, reply_markup=markup, disable_web_page_preview=True)


async def _show_competitor(target: Message | CallbackQuery, competitor_id: int, *, new_message: bool = False) -> None:
    db = SessionLocal()
    try:
        competitor = db.get(Competitor, competitor_id)
        if competitor is None or competitor.is_own:
            if isinstance(target, CallbackQuery):
                await target.answer("Конкурент не найден — возможно, уже удалён", show_alert=True)
            else:
                await target.answer("Конкурент не найден — возможно, уже удалён.")
            return
        text, markup = _competitor_screen(db, competitor)
    finally:
        db.close()
    await _show(target, text, markup, new_message=new_message)


async def _show_changes(
    target: Message | CallbackQuery, scope: str, kind: str, page: int, *, new_message: bool = False
) -> None:
    db = SessionLocal()
    try:
        text, markup = _changes_screen(db, scope, kind, page)
    finally:
        db.close()
    await _show(target, text, markup, new_message=new_message)


# ---------------------------------------------------------------- действия


def _crawl_started_text(name: str) -> str:
    return f"▶️ Обход «{name}» запущен. Когда закончится — пришлю итог и ссылку на отчёт."


def _crawl_all_text(result: crawl_manager.CrawlAllResult) -> str:
    return (
        "🔄 Обход всех конкурентов:\n"
        f"запущено — {result.started}\n"
        f"уже шли — {result.skipped_running}\n"
        f"на паузе — {result.skipped_paused}\n\n"
        "По каждому пришлю итог и ссылку на отчёт."
    )


def _find_competitor(db, action: AssistantAction) -> Competitor | None:
    competitor_id = action.int_arg("competitor_id")
    competitor = db.get(Competitor, competitor_id) if competitor_id is not None else None
    return competitor if competitor is not None and not competitor.is_own else None


async def _execute(db, action: AssistantAction) -> tuple[str, InlineKeyboardMarkup | None]:
    """Выполняет действие, которое выбрал ИИ-помощник. Аргументы проверяем сами:
    модели можно ошибиться с id или прислать мусор."""
    kind = action.type

    if kind == "crawl_all":
        return _crawl_all_text(crawl_manager.start_all(db)), None

    if kind == "crawl_own":
        own = get_own_site(db)
        if own is None:
            return "Наш сайт ещё не указан — задайте адрес в разделе «🌐 Наш сайт».", None
        ok = crawl_manager.start(db, own)
        return ("▶️ Обход нашего сайта запущен." if ok else "Обход нашего сайта уже идёт."), None

    if kind == "add_competitor":
        name = str(action.args.get("name") or "").strip()
        try:
            url = competitor_ops.normalize_url(str(action.args.get("url") or ""))
        except ValueError as exc:
            return f"Не добавил: {exc}.", None
        if not name:
            return "Не добавил: не понял, как назвать конкурента.", None
        competitor = competitor_ops.create_competitor(db, name, url)
        return (
            (
                f"➕ Конкурент «{competitor.name}» добавлен ({competitor.base_url}).\n"
                "Первый обход полный — может занять заметное время."
            ),
            _kb([[_btn("▶️ Обойти сейчас", f"comp:{competitor.id}:crawl"), _btn("Карточка", f"comp:{competitor.id}")]]),
        )

    if kind == "set_own_site":
        try:
            own = competitor_ops.save_own_site(db, str(action.args.get("url") or ""))
        except ValueError as exc:
            return f"Не сохранил: {exc}.", None
        return f"🌐 Адрес нашего сайта: {own.base_url}", _kb([[_btn("▶️ Обойти его сейчас", "own:crawl")]])

    if kind == "set_schedule":
        days = action.int_arg("days") or 0
        hours = action.int_arg("hours") or 0
        if days > MAX_SCHEDULE_DAYS:
            return f"Не поменял: интервал больше {MAX_SCHEDULE_DAYS} дней — похоже на ошибку.", None
        try:
            config = set_interval(db, days, hours)
        except ValueError as exc:
            return f"Не поменял: {exc}.", None
        return f"⚙️ Расписание: {bot_views.fmt_interval(config.interval_days, config.interval_hours)}.", None

    competitor = _find_competitor(db, action)
    if competitor is None:
        return "Не нашёл такого конкурента — проверьте список в разделе «🏢 Конкуренты».", None

    if kind == "crawl":
        ok = crawl_manager.start(db, competitor)
        return (_crawl_started_text(competitor.name) if ok else f"Обход «{competitor.name}» уже идёт."), None
    if kind == "stop":
        ok = crawl_manager.stop(competitor.id)
        return (f"⏹ Обход «{competitor.name}» остановлен." if ok else f"Обход «{competitor.name}» сейчас не идёт."), None
    if kind == "pause":
        competitor_ops.pause_competitor(db, competitor)
        return f"⏸ «{competitor.name}» на паузе: плановые обходы не идут.", None
    if kind == "resume":
        competitor_ops.resume_competitor(db, competitor)
        return f"▶️ «{competitor.name}» снова обходится по расписанию.", None
    if kind == "delete_competitor":
        return (
            f"Удалить «{competitor.name}» и всю историю по нему? Это не отменить.",
            _kb([[_btn("Да, удалить", f"comp:{competitor.id}:delete_yes"), _btn("Отмена", f"comp:{competitor.id}")]]),
        )

    return "Это действие я пока не умею.", None


# ---------------------------------------------------------------- команды и кнопки меню


@router.message(Command("start", "menu"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    assistant.history.clear(message.chat.id)
    await message.answer(
        "👋 Это AI-Скаут: слежу за сайтами конкурентов и присылаю, что у них нового.\n\n"
        "Меню — внизу чата. Или просто напишите, что нужно, обычным языком.",
        reply_markup=MAIN_KB,
    )
    await _show_section(message, "summary")


_COMMAND_SECTIONS = {
    "summary": "summary",
    "competitors": "competitors",
    "changes": "changes",
    "digest": "digest",
    "reports": "reports",
    "settings": "settings",
    "help": "help",
}

_BUTTON_SECTIONS = {
    BTN_SUMMARY: "summary",
    BTN_COMPETITORS: "competitors",
    BTN_CHANGES: "changes",
    BTN_REPORTS: "reports",
    BTN_OWN_SITE: "own_site",
    BTN_SETTINGS: "settings",
    BTN_HELP: "help",
}


@router.message(Command(*_COMMAND_SECTIONS))
async def cmd_section(message: Message, state: FSMContext) -> None:
    await state.clear()
    command = (message.text or "").split()[0].lstrip("/").split("@")[0]
    await _show_section(message, _COMMAND_SECTIONS.get(command, "summary"))


@router.message(F.text.in_(_BUTTON_SECTIONS))
async def menu_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_section(message, _BUTTON_SECTIONS[message.text])


@router.message(F.text == BTN_CRAWL_ALL)
async def menu_crawl_all(message: Message, state: FSMContext) -> None:
    await state.clear()
    db = SessionLocal()
    try:
        result = crawl_manager.start_all(db)
    finally:
        db.close()
    await message.answer(_crawl_all_text(result))


# ---------------------------------------------------------------- нажатия inline-кнопок


@router.callback_query(F.data.startswith("sec:"))
async def cb_section(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_section(callback, callback.data.split(":", 1)[1])
    await callback.answer()


# Кнопки из сообщений старой версии бота — чтобы они не молчали.
@router.callback_query(F.data.in_({"menu:main", "menu:list", "menu:own_site"}))
async def cb_legacy_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    section = {"menu:main": "summary", "menu:list": "competitors", "menu:own_site": "own_site"}[callback.data]
    await _show_section(callback, section)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^comp:\d+$"))
async def cb_competitor_detail(callback: CallbackQuery) -> None:
    await _show_competitor(callback, int(callback.data.split(":")[1]))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^comp:\d+:\w+$"))
async def cb_competitor_action(callback: CallbackQuery) -> None:
    _, raw_id, action = callback.data.split(":")
    competitor_id = int(raw_id)

    db = SessionLocal()
    try:
        competitor = db.get(Competitor, competitor_id)
        if competitor is None or competitor.is_own:
            await callback.answer("Конкурент не найден — возможно, уже удалён", show_alert=True)
            return

        if action == "delete":
            text, markup = await _execute(db, AssistantAction("delete_competitor", {"competitor_id": competitor_id}))
            await _show(callback, text, markup)
            await callback.answer()
            return
        if action == "delete_yes":
            name = competitor.name
            competitor_ops.delete_competitor(db, competitor)
            await _show(callback, f"🗑 Конкурент «{name}» удалён.", _kb([[_btn("← К списку", "sec:competitors")]]))
            await callback.answer()
            return
        if action not in {"crawl", "stop", "pause", "resume"}:
            await callback.answer()
            return

        text, _markup = await _execute(db, AssistantAction(action, {"competitor_id": competitor_id}))
        await callback.answer(text[:190], show_alert=action in {"crawl", "stop"})
        db.expire_all()
        screen_text, screen_markup = _competitor_screen(db, db.get(Competitor, competitor_id))
    finally:
        db.close()
    await _show(callback, screen_text, screen_markup)


@router.callback_query(F.data.startswith("chg:"))
async def cb_changes(callback: CallbackQuery) -> None:
    # chg:<all|c{id}>:<kind>:<page>[:n] — «n» у кнопки из уведомления об обходе:
    # его не редактируем, а показываем ленту новым сообщением.
    parts = callback.data.split(":")
    try:
        scope, kind, page = parts[1], parts[2], int(parts[3])
    except (IndexError, ValueError):
        await callback.answer()
        return
    await _show_changes(callback, scope, kind, page, new_message=len(parts) > 4 and parts[4] == "n")
    await callback.answer()


@router.callback_query(F.data.startswith("chd:"))
async def cb_change_detail(callback: CallbackQuery) -> None:
    # chd:<change_id>:<scope>:<kind>:<page> — хвост нужен кнопке «← К ленте».
    parts = callback.data.split(":")
    try:
        change_id = int(parts[1])
        back = f"chg:{parts[2]}:{parts[3]}:{parts[4]}"
    except (IndexError, ValueError):
        await callback.answer()
        return

    db = SessionLocal()
    try:
        change = db.get(PageChange, change_id)
        if change is None:
            await callback.answer("Находка не найдена", show_alert=True)
            return
        text, markup = _change_detail_screen(db, change, back)
    finally:
        db.close()
    await _show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data == "menu:crawl_all")
async def cb_crawl_all(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        result = crawl_manager.start_all(db)
    finally:
        db.close()
    await _show(callback, _crawl_all_text(result), _kb([[_btn("← К конкурентам", "sec:competitors")]]))
    await callback.answer()


@router.callback_query(F.data.in_({"own:crawl", "own:stop"}))
async def cb_own_site_action(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        own = get_own_site(db)
        if own is None:
            await callback.answer("Наш сайт не указан", show_alert=True)
            return
        if callback.data == "own:crawl":
            ok = crawl_manager.start(db, own)
            await callback.answer("Обход запущен" if ok else "Обход уже выполняется", show_alert=not ok)
        else:
            ok = crawl_manager.stop(own.id)
            await callback.answer("Обход остановлен" if ok else "Обход сейчас не выполняется", show_alert=not ok)
        db.expire_all()
        text, markup = _own_site_screen(db)
    finally:
        db.close()
    await _show(callback, text, markup)


@router.callback_query(F.data == "own:edit")
async def cb_own_site_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditOwnSite.url)
    await _show(callback, "Пришлите адрес нашего сайта, например og1.ru", _kb([[_btn("Отмена", "sec:own_site")]]))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^set:\d+:\d+$"))
async def cb_set_schedule(callback: CallbackQuery) -> None:
    _, days, hours = callback.data.split(":")
    db = SessionLocal()
    try:
        text, _markup = await _execute(db, AssistantAction("set_schedule", {"days": days, "hours": hours}))
        screen_text, screen_markup = _settings_screen(db)
    finally:
        db.close()
    await callback.answer(text[:190])
    await _show(callback, screen_text, screen_markup)


@router.callback_query(F.data == "menu:add")
async def cb_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCompetitor.name)
    await _show(callback, "Как назвать конкурента?", _kb([[_btn("Отмена", "sec:competitors")]]))
    await callback.answer()


# ---------------------------------------------------------------- пошаговый ввод


@router.message(AddCompetitor.name)
async def add_name_received(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введите ещё раз, или /start для отмены.")
        return
    await state.update_data(name=name)
    await state.set_state(AddCompetitor.base_url)
    await message.answer("Теперь адрес сайта, например foxford.ru:")


@router.message(AddCompetitor.base_url)
async def add_url_received(message: Message, state: FSMContext) -> None:
    try:
        base_url = competitor_ops.normalize_url(message.text or "")
    except ValueError as exc:
        await message.answer(f"{exc}. Введите ещё раз, или /start для отмены.")
        return

    data = await state.get_data()
    await state.clear()

    db = SessionLocal()
    try:
        text, markup = await _execute(db, AssistantAction("add_competitor", {"name": data["name"], "url": base_url}))
    finally:
        db.close()
    await message.answer(text, reply_markup=markup)


@router.message(EditOwnSite.url)
async def own_site_url_received(message: Message, state: FSMContext) -> None:
    db = SessionLocal()
    try:
        text, markup = await _execute(db, AssistantAction("set_own_site", {"url": message.text or ""}))
    finally:
        db.close()
    if text.startswith("Не сохранил"):
        await message.answer(f"{text} Введите ещё раз, или /start для отмены.")
        return
    await state.clear()
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- свободный текст -> ИИ


@router.message(StateFilter(None), F.text)
async def free_text(message: Message, bot: Bot) -> None:
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    placeholder = await message.answer("🤔 Думаю…")

    db = SessionLocal()
    try:
        try:
            reply = await assistant.ask(db, message.chat.id, message.text)
        except (ClaudeCliError, ValueError) as exc:
            logger.warning("ИИ-помощник не ответил: %s", exc)
            await placeholder.edit_text(
                "Не получилось разобрать сообщение: ИИ сейчас недоступен. "
                "Воспользуйтесь кнопками меню внизу — они работают всегда."
            )
            return

        action = reply.action
        if action is not None and action.type == "show":
            if reply.text:
                await placeholder.edit_text(reply.text, disable_web_page_preview=True)
            else:
                await placeholder.delete()
            section = str(action.args.get("section"))
            competitor_id = action.int_arg("competitor_id")
            if section == "changes":
                kind = str(action.args.get("kind") or "all")
                await _show_changes(message, f"c{competitor_id}" if competitor_id else "all", kind, 1)
            elif section == "competitors" and competitor_id:
                await _show_competitor(message, competitor_id)
            else:
                await _show_section(message, section)
            return

        text, markup = reply.text, None
        if action is not None:
            result_text, markup = await _execute(db, action)
            text = f"{text}\n\n{result_text}" if text else result_text
    finally:
        db.close()

    await placeholder.edit_text(text[:4000], reply_markup=markup, disable_web_page_preview=True)


@router.message(StateFilter(None))
async def other_message(message: Message) -> None:
    await message.answer("Я понимаю только текст. Напишите, что нужно, или выберите раздел в меню.", reply_markup=MAIN_KB)


async def _set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        logger.exception("Не удалось задать команды бота")


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AllowlistMiddleware())
    dp.callback_query.outer_middleware(AllowlistMiddleware())
    dp.include_router(router)
    dp.startup.register(_set_commands)
    return dp


def create_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)
