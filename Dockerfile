FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Claude Code CLI нужен внутри контейнера для headless ИИ-анализа (`claude -p`).
# Требует авторизованной подписки — см. README про монтирование ~/.claude в volume.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir -e .

EXPOSE 8888

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8888"]
