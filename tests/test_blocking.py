from app.crawler.blocking import blocked_reason, is_blocked


def test_normal_page_is_not_blocked():
    html = "<html><body><h1>Курсы</h1><p>Обычная страница сайта</p></body></html>"
    assert is_blocked(200, html) is False
    assert blocked_reason(200, html) is None


def test_403_status_is_blocked():
    assert is_blocked(403, "<html>forbidden</html>") is True


def test_429_status_is_blocked():
    assert is_blocked(429, "<html>too many requests</html>") is True


def test_cloudflare_challenge_page_is_blocked():
    html = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    assert is_blocked(200, html) is True
    assert "challenge-маркер" in blocked_reason(200, html)


def test_verify_human_marker_is_blocked():
    html = "<html><body>Please verify you are human before continuing</body></html>"
    assert is_blocked(200, html) is True


def test_blocked_reason_reports_status_code_first():
    html = "<html>Just a moment...</html>"
    assert blocked_reason(503, html) == "HTTP 503"


def test_large_real_page_mentioning_captcha_config_is_not_blocked():
    # Реальная страница с виджетом капчи для формы — не блокировка, см. падение
    # на foxford.ru 2026-08-10: страница вернула 200 и полный контент (1.7 МБ),
    # но содержала "captcha" в JSON-конфиге виджета для формы обратной связи.
    filler = "<p>Контент страницы. </p>" * 2000
    html = f'<html><body>{filler}<script>{{"captcha":{{"provider":"yandex_smart_captcha"}}}}</script></body></html>'
    assert len(html) > 20_000
    assert is_blocked(200, html) is False
    assert blocked_reason(200, html) is None
