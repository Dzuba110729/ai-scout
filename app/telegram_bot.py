"""Интерактивное меню в Telegram: список конкурентов, добавление, запуск обхода.

Работает поверх той же логики, что и веб-интерфейс (app/crawl_manager.py,
app/competitor_ops.py) — оба входа равноправны, ни один не знает про другой.
Поднимается long polling'ом (без вебхука — не нужен публичный HTTPS для внутреннего
инструмента, см. CLAUDE.md) как фоновая asyncio-задача в app/main.py::lifespan,
на том же event loop, что и APScheduler и все обходы. Именно поэтому вызовы
crawl_manager.start/stop отсюда безопасны без дополнительных мер (см. память
про RuntimeError: no running event loop при вызове из чужого потока).
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app import competitor_ops, crawl_manager
from app.config import settings
from app.db import SessionLocal
from app.models import Competitor, SessionStatus
from app.own_site import get_own_site

logger = logging.getLogger(__name__)

router = Router(name="ai_scout_menu")

_STATUS_LABELS = {
    SessionStatus.ACTIVE: "активна",
    SessionStatus.NEEDS_SESSION: "нужна ручная сессия",
    SessionStatus.PAUSED: "на паузе",
}


class AddCompetitor(StatesGroup):
    name = State()
    base_url = State()


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


def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Конкуренты", callback_data="menu:list")],
            [InlineKeyboardButton(text="➕ Добавить конкурента", callback_data="menu:add")],
            [InlineKeyboardButton(text="🔄 Обойти всех", callback_data="menu:crawl_all")],
            [InlineKeyboardButton(text="🌐 Наш сайт", callback_data="menu:own_site")],
        ]
    )


async def _show_main_menu(message: Message) -> None:
    await message.answer("Что делаем?", reply_markup=_main_menu_kb())


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_main_menu(message)


@router.callback_query(lambda c: c.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Что делаем?", reply_markup=_main_menu_kb())
    await callback.answer()


def _competitor_status_line(competitor: Competitor) -> str:
    parts = [_STATUS_LABELS[competitor.status]]
    if competitor.is_paused:
        parts.append("пауза")
    if competitor.is_crawling:
        parts.append("⏳ обход идёт")
    return ", ".join(parts)


@router.callback_query(lambda c: c.data == "menu:list")
async def cb_list_competitors(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        competitors = (
            db.query(Competitor)
            .filter(Competitor.is_own.is_(False))
            .order_by(Competitor.created_at.desc())
            .all()
        )
        if not competitors:
            buttons = [[InlineKeyboardButton(text="← Назад", callback_data="menu:main")]]
            await callback.message.edit_text("Конкурентов пока нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            await callback.answer()
            return

        buttons = [
            [
                InlineKeyboardButton(
                    text=f"{c.name} — {_competitor_status_line(c)}", callback_data=f"comp:{c.id}"
                )
            ]
            for c in competitors
        ]
        buttons.append([InlineKeyboardButton(text="← Назад", callback_data="menu:main")])
        await callback.message.edit_text("Конкуренты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    finally:
        db.close()
    await callback.answer()


def _competitor_detail_kb(competitor: Competitor) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if competitor.is_crawling:
        rows.append([InlineKeyboardButton(text="⏹ Остановить обход", callback_data=f"comp:{competitor.id}:stop")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Обойти сейчас", callback_data=f"comp:{competitor.id}:crawl")])

    if competitor.is_paused:
        rows.append([InlineKeyboardButton(text="▶️ Снять с паузы", callback_data=f"comp:{competitor.id}:resume")])
    else:
        rows.append([InlineKeyboardButton(text="⏸ Пауза", callback_data=f"comp:{competitor.id}:pause")])

    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"comp:{competitor.id}:delete")])
    rows.append([InlineKeyboardButton(text="← К списку", callback_data="menu:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _competitor_detail_text(competitor: Competitor) -> str:
    last_crawl = (
        competitor.last_crawl_finished_at.strftime("%d.%m.%Y %H:%M")
        if competitor.last_crawl_finished_at
        else "ещё не было"
    )
    return (
        f"{competitor.name}\n"
        f"{competitor.base_url}\n\n"
        f"Статус: {_competitor_status_line(competitor)}\n"
        f"Последний обход: {last_crawl}"
    )


async def _render_competitor(callback: CallbackQuery, db, competitor_id: int) -> Competitor | None:
    competitor = db.get(Competitor, competitor_id)
    if competitor is None:
        await callback.answer("Конкурент не найден — возможно, уже удалён", show_alert=True)
        return None
    await callback.message.edit_text(_competitor_detail_text(competitor), reply_markup=_competitor_detail_kb(competitor))
    return competitor


@router.callback_query(lambda c: c.data and c.data.startswith("comp:") and c.data.count(":") == 1)
async def cb_competitor_detail(callback: CallbackQuery) -> None:
    competitor_id = int(callback.data.split(":")[1])
    db = SessionLocal()
    try:
        await _render_competitor(callback, db, competitor_id)
    finally:
        db.close()
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("comp:") and c.data.count(":") == 2)
async def cb_competitor_action(callback: CallbackQuery) -> None:
    _, raw_id, action = callback.data.split(":")
    competitor_id = int(raw_id)

    db = SessionLocal()
    try:
        competitor = db.get(Competitor, competitor_id)
        if competitor is None:
            await callback.answer("Конкурент не найден — возможно, уже удалён", show_alert=True)
            return

        if action == "crawl":
            ok = crawl_manager.start(db, competitor)
            await callback.answer("Обход запущен" if ok else "Обход уже выполняется", show_alert=not ok)
        elif action == "stop":
            ok = crawl_manager.stop(competitor_id)
            await callback.answer("Обход остановлен" if ok else "Обход сейчас не выполняется", show_alert=not ok)
        elif action == "pause":
            competitor_ops.pause_competitor(db, competitor)
            await callback.answer("Поставлено на паузу")
        elif action == "resume":
            competitor_ops.resume_competitor(db, competitor)
            await callback.answer("Снято с паузы")
        elif action == "delete":
            buttons = [
                [
                    InlineKeyboardButton(text="Да, удалить", callback_data=f"comp:{competitor_id}:delete_yes"),
                    InlineKeyboardButton(text="Отмена", callback_data=f"comp:{competitor_id}"),
                ]
            ]
            await callback.message.edit_text(
                f"Удалить «{competitor.name}» и всю историю по нему?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            await callback.answer()
            return
        elif action == "delete_yes":
            name = competitor.name
            competitor_ops.delete_competitor(db, competitor)
            await callback.message.edit_text(
                f"Конкурент «{name}» удалён.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="← К списку", callback_data="menu:list")]]
                ),
            )
            await callback.answer()
            return
        else:
            await callback.answer()
            return

        db.expire(competitor)
        await _render_competitor(callback, db, competitor_id)
    finally:
        db.close()


@router.callback_query(lambda c: c.data == "menu:crawl_all")
async def cb_crawl_all(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        result = crawl_manager.start_all(db)
    finally:
        db.close()

    text = (
        "Запущен обход всех конкурентов:\n"
        f"запущено — {result.started}\n"
        f"уже шли — {result.skipped_running}\n"
        f"на паузе — {result.skipped_paused}"
    )
    buttons = [[InlineKeyboardButton(text="← Назад", callback_data="menu:main")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


def _own_site_kb(own: Competitor) -> InlineKeyboardMarkup:
    action = ("⏹ Остановить обход", "stop") if own.is_crawling else ("▶️ Обойти сейчас", "crawl")
    buttons = [
        [InlineKeyboardButton(text=action[0], callback_data=f"own:{action[1]}")],
        [InlineKeyboardButton(text="← Назад", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_own_site(callback: CallbackQuery, db) -> None:
    own = get_own_site(db)
    if own is None:
        buttons = [[InlineKeyboardButton(text="← Назад", callback_data="menu:main")]]
        await callback.message.edit_text(
            "Свой сайт ещё не указан — задайте его в веб-интерфейсе, в настройках.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    last_crawl = (
        own.last_crawl_finished_at.strftime("%d.%m.%Y %H:%M") if own.last_crawl_finished_at else "ещё не было"
    )
    text = f"{own.name}\n{own.base_url}\n\nСтатус: {_competitor_status_line(own)}\nПоследний обход: {last_crawl}"
    await callback.message.edit_text(text, reply_markup=_own_site_kb(own))


@router.callback_query(lambda c: c.data == "menu:own_site")
async def cb_own_site(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        await _render_own_site(callback, db)
    finally:
        db.close()
    await callback.answer()


@router.callback_query(lambda c: c.data in ("own:crawl", "own:stop"))
async def cb_own_site_action(callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        own = get_own_site(db)
        if own is None:
            await callback.answer("Свой сайт не указан", show_alert=True)
            return

        if callback.data == "own:crawl":
            ok = crawl_manager.start(db, own)
            await callback.answer("Обход запущен" if ok else "Обход уже выполняется", show_alert=not ok)
        else:
            ok = crawl_manager.stop(own.id)
            await callback.answer("Обход остановлен" if ok else "Обход сейчас не выполняется", show_alert=not ok)

        db.expire(own)
        await _render_own_site(callback, db)
    finally:
        db.close()


@router.callback_query(lambda c: c.data == "menu:add")
async def cb_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCompetitor.name)
    buttons = [[InlineKeyboardButton(text="Отмена", callback_data="menu:main")]]
    await callback.message.edit_text(
        "Как назвать конкурента?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.message(AddCompetitor.name)
async def add_name_received(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введите ещё раз, или /start для отмены.")
        return
    await state.update_data(name=name)
    await state.set_state(AddCompetitor.base_url)
    await message.answer("Теперь адрес сайта (начиная с http:// или https://):")


@router.message(AddCompetitor.base_url)
async def add_url_received(message: Message, state: FSMContext) -> None:
    base_url = (message.text or "").strip()
    if not base_url.startswith(("http://", "https://")):
        await message.answer("Адрес должен начинаться с http:// или https://. Введите ещё раз, или /start для отмены.")
        return

    data = await state.get_data()
    name = data["name"]
    await state.clear()

    db = SessionLocal()
    try:
        competitor_ops.create_competitor(db, name, base_url)
    finally:
        db.close()

    await message.answer(f"Конкурент «{name}» добавлен.")
    await _show_main_menu(message)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AllowlistMiddleware())
    dp.callback_query.outer_middleware(AllowlistMiddleware())
    dp.include_router(router)
    return dp


def create_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)
