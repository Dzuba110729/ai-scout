"""Тесты разбора номера страницы в ленте изменений.

Главное, что проверяем: пустое значение в адресе (?page=) не должно ломать страницу —
именно так браузер отправляет параметры при сбросе фильтров через <select>.
"""

import pytest

from app.routers.ui import _parse_page


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 1),
        ("", 1),  # сброс фильтра: ?page=
        ("1", 1),
        ("3", 3),
        ("0", 1),
        ("-5", 1),
        ("abc", 1),
        ("2.5", 1),
        ("  7  ", 7),
    ],
)
def test_parse_page(raw, expected):
    assert _parse_page(raw) == expected
