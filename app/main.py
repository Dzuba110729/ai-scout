import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import competitor_ops, crawl_manager
from app.config import settings
from app.db import SessionLocal
from app.routers import changes, competitors, own_site, schedule, ui
from app.scheduler import start_scheduler
from app.telegram_bot import create_bot, create_dispatcher

logging.basicConfig(level=settings.log_level)
# httpx на уровне INFO пишет каждый запрос с полным адресом, а в адресе запросов к
# Telegram Bot API — секретный токен бота. В лог он попадать не должен.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

BOT_RESTART_DELAY_SECONDS = 30


async def _run_bot_forever(dispatcher, bot) -> None:
    """Держит бота запущенным: после перезагрузки Мака сеть появляется не сразу,
    и start_polling падает на первом же запросе к Telegram — тогда пробуем снова."""
    while True:
        try:
            await dispatcher.start_polling(bot, handle_signals=False)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Telegram-бот остановился с ошибкой, перезапуск через %s с", BOT_RESTART_DELAY_SECONDS
            )
        await asyncio.sleep(BOT_RESTART_DELAY_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Обходы, оборванные прошлым запуском сервера, иначе навсегда блокируют новые.
    db = SessionLocal()
    try:
        crawl_manager.release_stale_crawls(db)
        competitor_ops.ensure_own_site(db)
    finally:
        db.close()

    start_scheduler()

    bot = None
    bot_task = None
    if settings.telegram_bot_token:
        bot = create_bot()
        dispatcher = create_dispatcher()
        bot_task = asyncio.create_task(_run_bot_forever(dispatcher, bot))
        logger.info("Telegram-бот запущен (long polling)")

    yield

    if bot_task:
        bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot_task
    if bot:
        await bot.session.close()


app = FastAPI(title="AI-Скаут", lifespan=lifespan)

app.include_router(competitors.router)
app.include_router(own_site.router)
app.include_router(schedule.router)
app.include_router(changes.router)
app.include_router(ui.router)
