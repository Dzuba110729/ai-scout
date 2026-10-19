"""Кому адресована находка: SEO-специалисту (инфоконтент) или продакту (коммерция).

Обратная связь SEO-специалиста (2026-10-19): статьи, новости, чек-листы, вопросы —
его работа; курсы, скидки, баннеры, тарифы — работа продакт-менеджера. Пока отчёт
один, каждая находка помечается, чтобы каждый читал своё.

Правило простое, без ИИ: сначала раздел в адресе (/blog/, /articles/, /polza/ …),
потом категория из ИИ-разбора (статья/новость/чек-лист/тест). Всё остальное —
коммерция.

То же правило по адресу (is_info_url) решает, какие страницы — статьи, у которых
нужна структура (H1, мета-теги, оглавление — app/crawler/structure.py), у конкурентов
и у нашего сайта. Отдельного поля у конкурента нет: разделы статей у всех сайтов
названы одинаково (/articles, /blog, /wiki, /polza…), а новый раздел — одно слово здесь.
"""

import re
from urllib.parse import urlparse

INFO = "info"
COMMERCE = "commerce"

CONTENT_KIND_LABELS = {
    INFO: "Инфо (SEO)",
    COMMERCE: "Коммерция (продукт)",
}

_INFO_PATH_RE = re.compile(
    r"/(?:articles?|blog|news|novosti[\w-]*|media|journal|zhurnal|wiki|polza|poleznoe|videouroki|questions?|"
    r"voprosy|glossary|slovar\w*|terminy|encyclopedia|enciklopediya|spravochnik|stat[iy]a?|stati|"
    r"sovety|chek-?listy?|checklists?|baza-znanij|knowledge|faq|testy?|quiz)(?:/|$)",
    re.IGNORECASE,
)
_INFO_CATEGORY_RE = re.compile(r"стать|новост|чек-?лист|тест|совет|блог|вопрос", re.IGNORECASE)


def is_info_url(url: str) -> bool:
    """Страница из раздела статей/новостей/вопросов — инфоконтент для SEO."""
    return bool(_INFO_PATH_RE.search(urlparse(url).path))


def content_kind(url: str, category: str | None) -> str:
    if is_info_url(url):
        return INFO
    if category and _INFO_CATEGORY_RE.search(category):
        return INFO
    return COMMERCE
