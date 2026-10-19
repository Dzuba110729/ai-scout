"""SEO-разбор инфостатей во время обхода: карточки интента (этап 3) и решения
«написать / обновить / ничего» по сравнению с нашими статьями (этап 4).

Обход (app/pipeline.py) зовёт process_info_articles с новыми и изменёнными статьями
конкурента (или нашего сайта) — у каждой уже есть структура из этапа 2. Всё, что
не успели за обход (лимиты seo_*_max_per_run), доберёт стартовый аудит
(scripts/seo_audit.py) тем же кодом.

ИИ-вызовы гоняются пачками и параллельно (до claude_cli_concurrency), а запись в БД —
синхронно после ответа, как и в остальном обходе: одну Session нельзя дёргать из
нескольких операций разом.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.ai.analyze import ClaudeCliError, ClaudeLimitError
from app.ai.intent import ArticleInput, IntentCard, intent_content_hash, make_intent_cards
from app.ai.topic_match import (
    DECISION_COVERED,
    DECISION_NEW,
    DECISION_UPDATE,
    CompetitorArticle,
    MatchResult,
    OwnArticle,
    OwnArticlesIndex,
    TextIndex,
    match_articles,
    new_without_candidates,
)
from app.config import settings
from app.content_kind import is_info_url
from app.crawler.structure import PageStructure
from app.models import ArticleIntent, Page, SeoFinding, TopicDecision
from app.own_site import get_own_site

logger = logging.getLogger(__name__)


@dataclass
class CardUpdate:
    page: Page
    structure: PageStructure
    card: IntentCard
    # Карточки у статьи раньше не было (новая статья или первый разбор).
    is_first: bool
    # Подинтенты, которых не было в прошлой версии статьи (по мнению ИИ).
    added_subintents: list[str] = field(default_factory=list)


FINDING_NEW = "new"
FINDING_CHANGED = "changed"
FINDING_AUDIT = "audit"


@dataclass
class SeoRunResult:
    cards: list[CardUpdate] = field(default_factory=list)
    cards_skipped: int = 0  # не уложились в лимит или ИИ не ответил — доберёт аудит
    limit_hit: bool = False  # упёрлись в лимит подписки Claude
    # Решения по статьям конкурента: сколько тем каждого вида записано за обход.
    decisions: dict[str, int] = field(default_factory=dict)
    merged: int = 0  # статьи, склеенные с уже известной темой (без вызова ИИ)
    not_compared: int = 0  # сравнение не успели (лимит, ИИ не ответил) — доберёт аудит
    findings: list[SeoFinding] = field(default_factory=list)
    calls: int = 0  # сколько раз звали `claude -p` (подписка не безразмерная)

    def merge(self, other: "SeoRunResult") -> None:
        self.cards += other.cards
        self.cards_skipped += other.cards_skipped
        self.limit_hit = self.limit_hit or other.limit_hit
        for decision, count in other.decisions.items():
            self.decisions[decision] = self.decisions.get(decision, 0) + count
        self.merged += other.merged
        self.not_compared += other.not_compared
        self.findings += other.findings
        self.calls += other.calls


def card_from_record(record: ArticleIntent) -> IntentCard:
    return IntentCard(
        topic=record.topic,
        main_intent=record.main_intent,
        subintents=list(record.subintents or []),
        entities=list(record.entities or []),
        relations=[tuple(item) for item in record.relations or []],
        material_type=record.material_type or "другое",
        raw_response=record.raw_response or {},
    )


def _store_card(db: Session, page: Page, card: IntentCard, content_hash: str) -> None:
    record = page.intent
    if record is None:
        record = ArticleIntent(page_id=page.id)
        db.add(record)
        page.intent = record
    record.content_hash = content_hash
    record.topic = card.topic
    record.main_intent = card.main_intent
    record.subintents = card.subintents
    record.entities = card.entities
    record.relations = [list(item) for item in card.relations]
    record.material_type = card.material_type
    record.raw_response = card.raw_response
    record.updated_at = datetime.now(UTC)


def needs_card(page: Page, structure: PageStructure) -> bool:
    """Карточки нет или статья с тех пор поменялась по существу."""
    return page.intent is None or page.intent.content_hash != intent_content_hash(structure)


def _batches(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[start : start + size] for start in range(0, len(items), size)]


async def refresh_intent_cards(
    db: Session, items: list[tuple[Page, PageStructure]], *, limit: int
) -> SeoRunResult:
    """Карточки для статей, у которых их нет или содержимое поменялось. Не больше limit."""
    result = SeoRunResult()
    todo = [(page, structure) for page, structure in items if needs_card(page, structure)]
    if limit < len(todo):
        result.cards_skipped = len(todo) - max(0, limit)
        todo = todo[: max(0, limit)]
    if not todo:
        return result

    semaphore = asyncio.Semaphore(max(1, settings.claude_cli_concurrency))

    async def _one(batch: list[tuple[Page, PageStructure]]) -> list[IntentCard | None] | None:
        articles = [
            ArticleInput(
                url=page.url,
                structure=structure,
                previous_subintents=list(page.intent.subintents or []) if page.intent else None,
            )
            for page, structure in batch
        ]
        async with semaphore:
            if result.limit_hit:
                return None
            try:
                result.calls += 1
                return await make_intent_cards(articles)
            except ClaudeLimitError as exc:
                result.limit_hit = True
                logger.warning("Карточки интента: лимит подписки Claude — %s", exc)
            except (ClaudeCliError, ValueError) as exc:
                logger.warning("Карточки интента для %s статей не получены: %s", len(batch), exc)
            return None

    batches = _batches(todo, settings.seo_intent_batch_size)
    answers = await asyncio.gather(*(_one(batch) for batch in batches))
    for batch, cards in zip(batches, answers, strict=True):
        for (page, structure), card in zip(batch, cards or [None] * len(batch), strict=True):
            if card is None:
                result.cards_skipped += 1
                continue
            is_first = page.intent is None
            _store_card(db, page, card, intent_content_hash(structure))
            result.cards.append(
                CardUpdate(
                    page=page,
                    structure=structure,
                    card=card,
                    is_first=is_first,
                    added_subintents=[] if is_first else card.added_subintents,
                )
            )
    return result


def load_own_articles(db: Session) -> list[OwnArticle]:
    """Наши статьи (инфоразделы og1.ru) — кандидаты для сравнения.

    Структура и карточка — если уже есть; нет структуры — текст последнего снимка.
    Блокирующая и тяжёлая — из обхода только через asyncio.to_thread.
    """
    own = get_own_site(db)
    if own is None:
        return []
    pages = db.query(Page).filter(Page.competitor_id == own.id, Page.is_removed.is_(False)).all()
    articles = []
    for page in pages:
        if not is_info_url(page.url):
            continue
        structure = page.structure
        if structure is not None:
            title = structure.h1 or structure.meta_title or page.title
            headings = [(int(level), str(text)) for level, text in structure.headings or []]
            text = structure.main_text or ""
        elif page.snapshots:
            title, headings, text = page.title, [], page.snapshots[-1].text_content
        else:
            continue
        card = card_from_record(page.intent) if page.intent else None
        articles.append(OwnArticle(url=page.url, title=title, headings=headings, text=text, card=card))
    return articles


def _decision_document(decision: TopicDecision) -> tuple[str, str]:
    return decision.topic, " ".join([decision.main_intent, *(decision.subintents or [])])


def _card_document(card: IntentCard) -> tuple[str, str]:
    return card.topic, " ".join([card.main_intent, *card.subintents])


@dataclass(eq=False)
class _Case:
    update: CardUpdate
    competitor_id: int
    kind: str
    page_change_id: int | None
    article: CompetitorArticle
    decision: TopicDecision | None = None  # уже известная тема (склейка или изменённая статья)
    same_as: int | None = None  # склеена с другой статьёй этого же прогона (номер случая)
    match: MatchResult | None = None
    asked: bool = False  # уложилась в лимит сравнений этого прогона
    recorded: bool = False  # находка записана


def _latest_decision(db: Session, page: Page) -> TopicDecision | None:
    finding = (
        db.query(SeoFinding)
        .filter(SeoFinding.page_id == page.id)
        .order_by(SeoFinding.detected_at.desc(), SeoFinding.id.desc())
        .first()
    )
    return finding.decision if finding else None


def _apply_match(decision: TopicDecision, card: IntentCard, match: MatchResult) -> None:
    decision.decision = match.decision
    decision.coverage = match.coverage
    decision.our_url = match.our_url
    decision.add_subintents = match.add_subintents
    decision.argument = match.argument
    decision.cannibal_urls = match.cannibalization_urls
    decision.raw_response = match.raw_response
    decision.updated_at = datetime.now(UTC)


def _union(first: list[str] | None, second: list[str]) -> list[str]:
    seen = {item.lower() for item in first or []}
    return [*(first or []), *(item for item in second if item.lower() not in seen)]


async def match_cards(
    db: Session,
    cases: list[_Case],
    own_index: OwnArticlesIndex | None,
    *,
    limit: int,
) -> SeoRunResult:
    """Темы и решения для разобранных статей конкурента; пишет TopicDecision и SeoFinding.

    Склейка без ИИ: тема, похожая на уже известную (TF-IDF по теме, интенту и
    подинтентам ≥ seo_topic_merge_threshold), — та же запись; статьи одного прогона
    на одну тему — тоже. ИИ зовём только для новых тем и для изменённых статей,
    в которых появились новые подинтенты.
    """
    result = SeoRunResult()
    if not cases:
        return result
    threshold = settings.seo_topic_merge_threshold

    known = db.query(TopicDecision).all()
    known_index = TextIndex([_decision_document(d) for d in known]) if known else None
    # Какие конкуренты уже есть в каждой теме: две статьи ОДНОГО сайта — всегда две
    # темы (пилот 19.10: «калькулятор степеней» и «степени сравнения прилагательных»
    # у skysmart склеивались по общему слову «степень»).
    sites_by_decision: dict[int, set[int]] = {}
    for decision_id, competitor_id in db.query(SeoFinding.decision_id, SeoFinding.competitor_id):
        sites_by_decision.setdefault(decision_id, set()).add(competitor_id)
    run_index = TextIndex([_card_document(case.update.card) for case in cases])

    to_ask: list[_Case] = []
    for number, case in enumerate(cases):
        card = case.update.card
        if case.decision is not None:  # изменённая статья с известной темой — спросить заново
            to_ask.append(case)
            continue
        if known_index:
            similar = [
                known[number_known]
                for number_known, score in known_index.search(*_card_document(card), limit=5)
                if score >= threshold
                and case.competitor_id not in sites_by_decision.get(known[number_known].id, set())
            ]
            if similar:
                case.decision = similar[0]  # та же тема уже разобрана у другого конкурента — без ИИ
                continue
        earlier = [
            other
            for other, score in run_index.search(*_card_document(card), limit=len(cases))
            if other < number
            and score >= threshold
            and cases[other].same_as is None
            and cases[other].competitor_id != case.competitor_id
        ]
        if earlier:
            case.same_as = earlier[0]  # та же тема, что у статьи выше в этом прогоне
            continue
        to_ask.append(case)

    limit = max(0, limit)
    for case in to_ask[:limit]:
        case.asked = True
    result.not_compared += max(0, len(to_ask) - limit)

    with_candidates: list[tuple[_Case, list[OwnArticle]]] = []
    for case in to_ask[:limit]:
        candidates = own_index.candidates(case.article, settings.seo_compare_candidates) if own_index else []
        if candidates:
            with_candidates.append((case, candidates))
        else:
            case.match = new_without_candidates()

    semaphore = asyncio.Semaphore(max(1, settings.claude_cli_concurrency))
    size = max(1, settings.seo_compare_batch_size)
    batches = [with_candidates[start : start + size] for start in range(0, len(with_candidates), size)]

    async def _one(batch):
        async with semaphore:
            if result.limit_hit:
                return None
            try:
                result.calls += 1
                return await match_articles([(case.article, candidates) for case, candidates in batch])
            except ClaudeLimitError as exc:
                result.limit_hit = True
                logger.warning("Сравнение со своими статьями: лимит подписки Claude — %s", exc)
            except (ClaudeCliError, ValueError) as exc:
                logger.warning("Сравнение %s статей со своими не получено: %s", len(batch), exc)
            return None

    answers = await asyncio.gather(*(_one(batch) for batch in batches))
    for batch, matches in zip(batches, answers, strict=True):
        for (case, _candidates), match in zip(batch, matches or [None] * len(batch), strict=True):
            case.match = match

    now = datetime.now(UTC)
    for case in cases:
        card = case.update.card
        if case.same_as is not None:
            case.decision = cases[case.same_as].decision if cases[case.same_as].recorded else None
            if case.decision is None:
                result.not_compared += 1  # главная статья темы без ответа — доберём вместе
                continue
            result.merged += 1
        elif case in to_ask:
            if not case.asked:
                continue  # сверх лимита — уже посчитана в not_compared
            if case.match is None:
                result.not_compared += 1
                continue
            if case.decision is None:
                case.decision = TopicDecision(
                    topic=card.topic, main_intent=card.main_intent, material_type=card.material_type, decision=DECISION_NEW
                )
                db.add(case.decision)
            _apply_match(case.decision, card, case.match)
            result.decisions[case.match.decision] = result.decisions.get(case.match.decision, 0) + 1
        else:
            result.merged += 1  # склейка с уже известной темой — без ИИ
        case.decision.subintents = _union(case.decision.subintents, card.subintents)
        finding = SeoFinding(
            decision=case.decision,
            page_id=case.update.page.id,
            competitor_id=case.competitor_id,
            page_change_id=case.page_change_id,
            kind=case.kind,
            added_subintents=case.update.added_subintents or None,
            detected_at=now,
        )
        db.add(finding)
        case.recorded = True
        result.findings.append(finding)
    db.flush()
    return result


def build_cases(
    db: Session,
    competitor_id: int,
    updates: list[CardUpdate],
    *,
    kinds: dict[str, str],
    change_ids: dict[str, int],
) -> list[_Case]:
    """Что сравнивать: новые статьи — всегда; изменённые — если в них добавились
    подинтенты (или статья ещё ни разу не сравнивалась)."""
    cases = []
    for update in updates:
        page = update.page
        kind = kinds.get(page.url, FINDING_NEW)
        previous = _latest_decision(db, page) if kind != FINDING_NEW else None
        if previous is not None and not update.added_subintents:
            continue  # правка без новых подинтентов — для SEO не новость
        structure = update.structure
        article = CompetitorArticle(
            url=page.url,
            h1=structure.h1,
            card=update.card,
            headings=list(structure.headings),
            text=structure.main_text,
            added_subintents=update.added_subintents if previous is not None else None,
        )
        cases.append(
            _Case(
                update=update,
                competitor_id=competitor_id,
                kind=kind,
                page_change_id=change_ids.get(page.url),
                article=article,
                decision=previous,
            )
        )
    return cases


async def process_info_articles(
    db: Session,
    items: list[tuple[Page, PageStructure]],
    *,
    is_own: bool,
    competitor_id: int | None = None,
    kinds: dict[str, str] | None = None,
    change_ids: dict[str, int] | None = None,
) -> SeoRunResult:
    """SEO-разбор новых и изменённых статей одного обхода: карточки, а у конкурента —
    ещё и решение «написать / обновить / ничего» по сравнению с нашими статьями."""
    if not settings.seo_enabled or not items:
        return SeoRunResult()
    result = await refresh_intent_cards(db, items, limit=settings.seo_intent_max_per_run)
    if is_own or competitor_id is None or not result.cards:
        return result
    cases = build_cases(db, competitor_id, result.cards, kinds=kinds or {}, change_ids=change_ids or {})
    if cases:
        own_articles = await asyncio.to_thread(load_own_articles, db)
        own_index = OwnArticlesIndex(own_articles) if own_articles else None
        result.merge(await match_cards(db, cases, own_index, limit=settings.seo_compare_max_per_run))
    return result


_DECISION_NOTE_LABELS = (
    (DECISION_NEW, "написать новую статью"),
    (DECISION_UPDATE, "обновить нашу"),
    (DECISION_COVERED, "у нас раскрыто"),
)


def format_seo_note(result: SeoRunResult) -> str | None:
    if not result.cards and not result.cards_skipped and not result.not_compared:
        return None
    note = f"SEO: разобрано инфостатей (интент, подинтенты) — {len(result.cards)}."
    decided = [f"{label} — {result.decisions[key]}" for key, label in _DECISION_NOTE_LABELS if result.decisions.get(key)]
    if decided:
        note += " Решения: " + ", ".join(decided) + "."
    if result.merged:
        note += f" Тех же тем, что уже разобраны (склеены): {result.merged}."
    left = result.cards_skipped + result.not_compared
    if left:
        note += f" Не успели — {left}, доберём стартовым аудитом или в следующий раз."
    if result.limit_hit:
        note += " Упёрлись в лимит подписки Claude."
    return note
