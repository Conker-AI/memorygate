"""Rule-based (no-LLM) signal filter applied before a memory write.

Two independent scores decide whether a write proceeds:

- novelty: how similar the text is to what's already stored for this agent
  (via vector search). Handled by the caller using `qdrant_store.find_near_duplicate`;
  this module just defines the bucket thresholds.
- value: would the agent act differently knowing this? A heuristic keyword
  score in [0, 1], compared against the agent's configured `value_threshold`.
"""

import re
import unicodedata

ACKNOWLEDGMENTS = {
    "ok", "okay", "yes", "no", "thanks", "thank you", "sure", "got it",
    "cool", "fine", "alright", "k", "yep", "nope", "kk",
    "да", "нет", "ок", "окей", "ага", "угу", "спасибо", "понятно", "хорошо",
}

PREFERENCE_WORDS = ["prefer", "always", "never", "i like", "i hate", "i want", "i decided", "i chose", "favorite", "i love"]
PREFERENCE_WORDS += ["prefers", "preferred", "preferring", "preference", "preferences", "favourite"]
BEHAVIORAL_WORDS = [
    "every time", "whenever", "tends to", "usually", "habit", "pattern of",
    "keep doing", "can't stop", "cant stop", "again", "relapse", "spiraling",
]
RELATIONSHIP_WORDS = ["my friend", "my partner", "my boss", "my mom", "my dad", "my sister", "my brother", "works at", "lives in", "my wife", "my husband"]
GOAL_WORDS = ["goal", "need to", "deadline", "must", "have to", "constraint", "plan to", "trying to"]

NEGATION_WORDS = ["not", "no longer", "stopped", "don't", "never"]

HIGH_VALUE_WEIGHT = 0.3


RUSSIAN_SIGNALS = (
    r"\b(?:предпоч(?:ита\w*|тени\w*|ел|ла|ли)|люблю|нравится|нравятся|хочу|решил[аи]?|выбрал[аи]?|любим\w*)\b|\bмне\s+удобнее\b",
    r"\b(?:обычно|привыч\w*|снова|каждый|каждую|всегда|никогда)\b|\bкаждый\s+раз\b",
    r"\b(?:мой|моя|мои)\s+(?:друг\w*|подруг\w*|партнер\w*|мам\w*|пап\w*|сестр\w*|брат\w*|муж\w*|жен\w*)\b|\b(?:работаю|живу)\b",
    r"\b(?:цель|цели|нужно|надо|должен|должна|срок|дедлайн|планирую|стараюсь)\b",
)


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def _contains(text: str, phrases: list[str]) -> bool:
    # Word boundaries avoid signals such as 'again' inside 'against'.
    return any(re.search(r"(?<!\w)" + re.escape(p) + r"s?(?!\w)", text) for p in phrases)


def score_value(text: str, existing_similar_text: str | None = None) -> float:
    lower = _normalized(text).strip()
    if lower.rstrip(".!? ") in ACKNOWLEDGMENTS:
        return 0.0
    groups = (PREFERENCE_WORDS, BEHAVIORAL_WORDS, RELATIONSHIP_WORDS, GOAL_WORDS)
    score = sum(HIGH_VALUE_WEIGHT for phrases, russian in zip(groups, RUSSIAN_SIGNALS, strict=True)
                if _contains(lower, phrases) or re.search(russian, lower))
    negations = [*NEGATION_WORDS, "не", "больше не", "перестал", "перестала"]
    if existing_similar_text and _contains(lower, negations) and not _contains(_normalized(existing_similar_text), negations):
        score += HIGH_VALUE_WEIGHT
    return min(score, 1.0)


NOVELTY_DUPLICATE = "duplicate"
NOVELTY_LOW = "low"
NOVELTY_NEW = "new"


def novelty_bucket(best_score: float | None, novelty_threshold: float) -> str:
    if best_score is None:
        return NOVELTY_NEW
    if best_score >= novelty_threshold:
        return NOVELTY_DUPLICATE
    if best_score >= 0.75:
        return NOVELTY_LOW
    return NOVELTY_NEW
