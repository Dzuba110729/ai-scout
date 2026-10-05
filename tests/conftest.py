import pytest


@pytest.fixture(autouse=True)
def _no_real_run_overview(monkeypatch):
    """Абзац «Главное» зовёт настоящий `claude -p` — в тестах обхода его не должно быть."""

    async def no_overview(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.pipeline.write_run_overview", no_overview)
