import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.routers import changes, competitors, own_site, schedule, ui
from app.scheduler import start_scheduler
from app.telegram_bot import create_bot, create_dispatcher

logging.basicConfig(level=settings.log_level)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()

    bot = None
    bot_task = None
    if settings.telegram_bot_token:
        bot = create_bot()
        dispatcher = create_dispatcher()
        bot_task = asyncio.create_task(dispatcher.start_polling(bot))
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
