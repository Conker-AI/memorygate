"""Scored multi-signal memory classifier.

Four memory types: `fact` (durable facts/preferences/identity/humor-style),
`phase` (temporary emotional/circumstantial states - carries a review_by),
`context` (default/fallback, everyday task info), `watch` (behavioral
patterns worth monitoring). This used to be seven finer-grained types
(stable_preference/identity_trait/humor_style -> fact, temporary_phase/
support_context -> phase, task_context -> context, harmful_pattern ->
watch) - the underlying keyword-signal detectors are unchanged from that
version, just aggregated into fewer output buckets.

Each candidate type is defined by one or more independent keyword-list
"signals". A signal fires (contributes weight 1.0) if any phrase in its
list appears in the text - some are gated by an exclusion list instead
(e.g. facts-via-identity-phrase are disqualified by temporary-language
phrasing). The type with the most fired signals wins; ties are broken by
priority order. Confidence scales with how many signals fired: 1 -> low,
2 -> medium, 3+ -> high.
"""

import re
from functools import cache

# priority order used to break ties between types with equal signal counts
TYPE_PRIORITY = ["watch", "fact", "phase"]

# maps the old 7-type taxonomy onto the new 4 types, so callers (ToolGate)
# that haven't been updated yet and still pass an explicit legacy
# memory_type override keep working instead of getting an unrecognized value.
LEGACY_TYPE_MAP = {
    "stable_preference": "fact",
    "identity_trait": "fact",
    "humor_style": "fact",
    "temporary_phase": "phase",
    "support_context": "phase",
    "task_context": "context",
    "harmful_pattern": "watch",
}


CURRENT_MEMORY_TYPES = {"fact", "phase", "context", "watch", "skill"}


def normalize_memory_type(memory_type: str | None) -> str | None:
    if memory_type is None:
        return None
    return LEGACY_TYPE_MAP.get(memory_type, memory_type)


PREFERENCE_WORDS = ["always", "never", "prefer", "hate when"]
DOMAIN_WORDS = ["build", "code", "design", "food", "sleep", "train", "work"]

IDENTITY_PHRASES = ["i am", "i'm a", "my personality"]
TEMPORARY_LANGUAGE = ["feeling", "today", "right now"]

HUMOR_TERMS = ["dark humor", "sarcasm", "deadpan", "joke style", "humor style"]
PATTERN_LANGUAGE = ["always", "usually", "tend to", "kind of humor", "style of humor"]

TEMPORARY_TIMEFRAMES = ["right now", "lately", "these days", "this week"]
EMOTIONAL_LANGUAGE = ["exhausted", "hate everyone", "disappear", "nothing matters", "sad", "overwhelmed", "anxious", "stressed"]

HARMFUL_BEHAVIOR_INDICATORS = ["again", "keep doing", "can't stop", "relapse", "binge", "self-destructive", "spiraling", "hurting myself"]

SUPPORT_EMOTIONAL_WORDS = ["sad", "upset", "anxious", "stressed", "overwhelmed", "hurt", "struggling", "hard time"]
SUPPORT_TIMEFRAMES = ["today", "right now", "this week"]


@cache
def _phrase(phrase: str) -> re.Pattern:
    # A whole word or phrase: no letter or digit right before or after it ("again" is not in
    # "against", "sad" is not in "Sadie"). Spaces inside a phrase match any run of whitespace.
    body = r"\s+".join(re.escape(part) for part in phrase.split())
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])")


def _count_hits(lower: str, phrases: list[str]) -> int:
    return sum(1 for p in phrases if _phrase(p).search(lower))


def _score_preference(lower: str) -> int:
    signals = 0
    if _count_hits(lower, PREFERENCE_WORDS) > 0:
        signals += 1
    if _count_hits(lower, DOMAIN_WORDS) > 0:
        signals += 1
    # both must be present for this to count as a preference at all
    return signals if signals == 2 else 0


def _score_identity(lower: str) -> int:
    if _count_hits(lower, TEMPORARY_LANGUAGE):
        return 0
    return _count_hits(lower, IDENTITY_PHRASES)


def _score_humor(lower: str) -> int:
    signals = 0
    if _count_hits(lower, HUMOR_TERMS) > 0:
        signals += 1
    if _count_hits(lower, PATTERN_LANGUAGE) > 0:
        signals += 1
    return signals


def _score_fact(lower: str) -> int:
    return _score_preference(lower) + _score_identity(lower) + _score_humor(lower)


def _score_phase(lower: str) -> int:
    # Two independent signals: a feeling word, and a word that says it is for now. The two old
    # lists of each overlap ("sad", "right now" sit in both), so they are joined, and one word
    # fires one signal, not two.
    signals = 0
    if _count_hits(lower, EMOTIONAL_LANGUAGE + SUPPORT_EMOTIONAL_WORDS) > 0:
        signals += 1
    if _count_hits(lower, TEMPORARY_TIMEFRAMES + SUPPORT_TIMEFRAMES) > 0:
        signals += 1
    return signals


def _score_watch(lower: str, source_type: str) -> int:
    signals = 0
    if _count_hits(lower, HARMFUL_BEHAVIOR_INDICATORS) > 0:
        signals += 1
    if source_type == "soulgate_inferred":
        signals += 1
    return signals


def _confidence_for(signal_count: int) -> str:
    if signal_count >= 3:
        return "high"
    if signal_count == 2:
        return "medium"
    return "low"


def classify_memory(text: str, source_type: str = "user") -> dict:
    lower = text.lower().replace(chr(0x2019), "'")  # a curly apostrophe (typed on a phone) counts as a straight one

    scores = {
        "watch": _score_watch(lower, source_type),
        "fact": _score_fact(lower),
        "phase": _score_phase(lower),
    }

    best_type = None
    best_score = 0
    for memory_type in TYPE_PRIORITY:
        score = scores[memory_type]
        if score > best_score:
            best_score = score
            best_type = memory_type

    if best_type is None:
        return {
            "memory_type": "context",
            "confidence": "medium",
            "summary": text[:280],
        }

    return {
        "memory_type": best_type,
        "confidence": _confidence_for(best_score),
        "summary": text[:280],
    }
