import pytest


@pytest.fixture(autouse=True)
def _no_real_run_overview(monkeypatch):
    """Абзац «Главное» зовёт настоящий `claude -p` — в тестах обхода его не должно быть."""

    async def no_overview(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.pipeline.write_run_overview", no_overview)


@pytest.fixture(autouse=True)
def _no_real_intent_cards(monkeypatch):
    """Карточки интента зовут настоящий `claude -p` — в тестах по умолчанию ИИ «молчит»."""

    async def no_cards(articles):
        return [None] * len(articles)

    monkeypatch.setattr("app.seo.make_intent_cards", no_cards)
