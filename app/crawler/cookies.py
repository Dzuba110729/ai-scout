"""Выгрузка кук из обычного Chrome (расширение Cookie-Editor) -> сессия бота.

Для сайтов, чья защита пускает только настоящий браузер человека (Qrator у
foxford.ru): владелец выгружает куки из своего Chrome, бот кладёт их в
storage_state конкурента (storage_states/competitor_<id>.json) и обходит сайт с ними.
Выгрузку принимают и скрипт scripts/cookies_to_storage_state.py, и Telegram-бот.
"""

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

_SAME_SITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
}


class CookieExportError(ValueError):
    """Присланное — не выгрузка кук Cookie-Editor."""


def convert_cookie(cookie: dict) -> dict:
    same_site_raw = str(cookie.get("sameSite", "unspecified")).lower()
    expires = cookie.get("expirationDate")

    return {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie["domain"],
        "path": cookie.get("path", "/"),
        "expires": float(expires) if expires is not None and not cookie.get("session") else -1,
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", False)),
        "sameSite": _SAME_SITE_MAP.get(same_site_raw, "Lax"),
    }


def _rtf_to_text(raw: bytes) -> str:
    """Выгрузку часто сохраняют через TextEdit — получается RTF. На Маке его
    понимает системный textutil (прод — Мак, см. CLAUDE.md)."""
    try:
        done = subprocess.run(
            ["textutil", "-convert", "txt", "-stdin", "-stdout"],
            input=raw,
            capture_output=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CookieExportError("файл в формате RTF — сохраните выгрузку как простой текст (.json)") from exc
    return done.stdout.decode("utf-8", errors="replace")


def parse_cookie_export(raw: bytes) -> list[dict]:
    """Список кук из выгрузки Cookie-Editor (Export -> JSON): JSON-файл, RTF из
    TextEdit или просто вставленный текст."""
    text = _rtf_to_text(raw) if raw.lstrip().startswith(b"{\\rtf") else raw.decode("utf-8-sig", errors="replace")
    # TextEdit и мессенджеры любят заменять кавычки на «умные».
    text = text.replace("“", '"').replace("”", '"').strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CookieExportError("это не JSON — в Cookie-Editor нужен Export → JSON") from exc
    if not isinstance(data, list) or not data:
        raise CookieExportError("ожидался список кук — в Cookie-Editor нужен Export → JSON")
    for cookie in data:
        if not isinstance(cookie, dict) or not {"name", "value", "domain"} <= cookie.keys():
            raise CookieExportError("в списке не куки — в Cookie-Editor нужен Export → JSON")
    return data


def cookie_domains(cookies: list[dict]) -> set[str]:
    return {str(cookie["domain"]).lstrip(".").lower() for cookie in cookies}


def site_matches_cookies(base_url: str, cookies: list[dict]) -> bool:
    """Куки выгружены с этого сайта: домен куки совпадает с сайтом или его родителем
    (.foxford.ru подходит и для foxford.ru, и для www.foxford.ru)."""
    host = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    return any(host == domain or host.endswith("." + domain) or domain.endswith("." + host)
               for domain in cookie_domains(cookies))


def save_storage_state(cookies: list[dict], path: Path) -> int:
    """Пишет сессию для браузера бота. Файл — это вход на сайт, поэтому только владельцу (600)."""
    storage_state = {"cookies": [convert_cookie(c) for c in cookies], "origins": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return len(storage_state["cookies"])
