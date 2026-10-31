"""Bounded literal retrieval over the authorized corpus, ranked before loading.

The database may scan the namespace to match literal substrings. Python never
loads the whole corpus or limits by recency before looking for relevant rows.
This is lexical evidence, not a replacement for unavailable semantic search.
"""

import re
from dataclasses import dataclass

from app.models.conversation_receipt import ConversationReceipt
from app.models.memory import Memory
from app.services.scoring import CONFIDENCE_BONUSES, MEMORY_TYPE_BONUSES
from sqlalchemy import case, false, or_, select, true

MAX_QUERY_CHARS = 4096
MAX_TERMS = 12
MAX_TERM_CHARS = 80
MAX_PHRASE_CHARS = 256
MAX_CANDIDATES = 120
_WORDS = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_STOP_WORDS = frozenset(
    ["an", "and", "are", "as", "at", "be", "by", "can", "could", "did", "do", "does",
     "for", "from", "has", "have", "how", "in", "is", "it", "me", "my", "of", "on", "or",
     "our", "please", "should", "that", "the", "their", "these", "this", "to", "us", "was",
     "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "would",
     "you", "your"]
)


def escape_like(text: str) -> str:
    # Escape LIKE syntax as literal text; parameters still carry every value.
    return text.replace("/", "//").replace("%", "/%").replace("_", "/_")


def _pattern(text: str) -> str:
    return "%" + escape_like(text) + "%"


def _matches(columns, text):
    return or_(*(column.ilike(_pattern(text), escape="/") for column in columns))


@dataclass(frozen=True)
class LexicalQuery:
    terms: tuple[str, ...]
    phrase: str
    query_truncated: bool
    terms_limited: bool

    def matches(self, *columns):
        values = (*self.terms, *((self.phrase,) if self.phrase else ()))
        return or_(*(_matches(columns, value) for value in values)) if values else false()

    def receipt(self, candidate_count: int) -> dict:
        return {
            "candidate_limit": MAX_CANDIDATES,
            "candidate_count": candidate_count,
            "candidate_limit_reached": candidate_count == MAX_CANDIDATES,
            "query_char_limit": MAX_QUERY_CHARS,
            "query_truncated": self.query_truncated,
            "term_limit": MAX_TERMS,
            "terms_used": len(self.terms),
            "terms_limited": self.terms_limited,
            "literal_phrase_used": bool(self.phrase),
        }


def prepare_query(query: str) -> LexicalQuery:
    bounded = query[:MAX_QUERY_CHARS].strip()
    terms = []
    seen = set()
    limited = False
    for match in _WORDS.finditer(bounded):
        term = match.group().lower()
        if len(term) > MAX_TERM_CHARS:
            limited = True
            continue
        if len(term) < 2 or term in seen or term in _STOP_WORDS:
            continue
        seen.add(term)
        if len(terms) == MAX_TERMS:
            limited = True
            break
        terms.append(term)
    truncated = len(query) > MAX_QUERY_CHARS
    phrase = bounded.lower() if not truncated and len(bounded) <= MAX_PHRASE_CHARS else ""
    return LexicalQuery(tuple(terms), phrase, truncated, limited)


def memory_candidates(db, query: LexicalQuery, agent_id: str, within=None):
    """Matching active memories, best first. `within` (a select of memory ids) narrows them."""
    if not query.terms and not query.phrase:
        return []
    columns = (Memory.text, Memory.summary)
    matches = [case((_matches(columns, term), 0.3), else_=0.0) for term in query.terms]
    # Preserve existing type/confidence weights and reward an exact phrase.
    # Compute the same final score in SQL before LIMIT, so older relevant rows
    # cannot be crowded out by newer rows that merely share a generic term.
    score = (
        sum(matches, 0.0)
        + (case((_matches(columns, query.phrase), 0.3), else_=0.0) if query.phrase else 0.0)
        + case(MEMORY_TYPE_BONUSES, value=Memory.memory_type, else_=0.0)
        + case(CONFIDENCE_BONUSES, value=Memory.confidence, else_=0.0)
    ).label("lexical_score")
    deleted = select(ConversationReceipt.id).where(
        ConversationReceipt.agent_id == agent_id,
        ConversationReceipt.memory_id == Memory.id,
        ConversationReceipt.state == "deleted",
    ).exists()
    statement = (
        select(Memory, score)
        .where(
            Memory.agent_id == agent_id,
            Memory.status == "active",
            ~deleted,
            query.matches(*columns),
            true() if within is None else Memory.id.in_(within),
        )
        .order_by(score.desc(), Memory.id.asc())
        .limit(MAX_CANDIDATES)
    )
    return db.execute(statement).all()
