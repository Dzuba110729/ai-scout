import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.routers import changes, competitors, schedule, ui
from app.scheduler import start_scheduler

logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield


app = FastAPI(title="AI-Скаут", lifespan=lifespan)

app.include_router(competitors.router)
app.include_router(schedule.router)
app.include_router(changes.router)
app.include_router(ui.router)
