import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from cookies_to_storage_state import convert_cookie  # noqa: E402


def test_convert_cookie_maps_no_restriction_to_none():
    cookie = {
        "domain": ".foxford.ru",
        "expirationDate": 1999999999.5,
        "httpOnly": True,
        "name": "qrator_jsr",
        "path": "/",
        "sameSite": "no_restriction",
        "secure": True,
        "session": False,
        "value": "abc123",
    }

    result = convert_cookie(cookie)

    assert result["name"] == "qrator_jsr"
    assert result["value"] == "abc123"
    assert result["domain"] == ".foxford.ru"
    assert result["sameSite"] == "None"
    assert result["httpOnly"] is True
    assert result["secure"] is True
    assert result["expires"] == 1999999999.5


def test_convert_cookie_session_cookie_has_no_expiry():
    cookie = {
        "domain": "foxford.ru",
        "httpOnly": False,
        "name": "session_id",
        "path": "/",
        "sameSite": "lax",
        "secure": False,
        "session": True,
        "value": "xyz",
    }

    result = convert_cookie(cookie)

    assert result["expires"] == -1
    assert result["sameSite"] == "Lax"


def test_convert_cookie_defaults_unknown_samesite_to_lax():
    cookie = {
        "domain": "foxford.ru",
        "name": "misc",
        "path": "/",
        "value": "1",
        "session": True,
    }

    result = convert_cookie(cookie)
    assert result["sameSite"] == "Lax"
