"""Исчерпанный лимит подписки Claude: пауза вместо сотен заведомо пустых вызовов."""

import asyncio

import pytest

from app.ai import analyze


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 1):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


@pytest.fixture(autouse=True)
def _reset_pause(monkeypatch):
    monkeypatch.setattr(analyze, "_limit_pause_until", 0.0)


def _patch_cli(monkeypatch, stdout: bytes) -> list[int]:
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(1)
        return _FakeProcess(stdout)

    monkeypatch.setattr(analyze.asyncio, "create_subprocess_exec", fake_exec)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(analyze.asyncio, "sleep", lambda _s: real_sleep(0))
    return calls


def test_limit_message_pauses_further_calls(monkeypatch):
    calls = _patch_cli(monkeypatch, b"You've hit your limit \xc2\xb7 resets 3pm")

    with pytest.raises(analyze.ClaudeLimitError):
        asyncio.run(analyze.run_claude_cli("промпт"))
    with pytest.raises(analyze.ClaudeLimitError):
        asyncio.run(analyze.run_claude_cli("промпт"))

    assert len(calls) == 1  # без ретрая и без второго вызова — пауза


def test_other_error_keeps_stdout_details_and_retries(monkeypatch):
    calls = _patch_cli(monkeypatch, b"Invalid model name")

    with pytest.raises(analyze.ClaudeCliError, match="Invalid model name") as error:
        asyncio.run(analyze.run_claude_cli("промпт"))

    assert not isinstance(error.value, analyze.ClaudeLimitError)
    assert len(calls) == 2
