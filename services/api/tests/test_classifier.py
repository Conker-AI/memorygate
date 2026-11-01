"""Direct tests for the memory classifier service."""

import pytest
from app.services.classifier import (
    CURRENT_MEMORY_TYPES,
    LEGACY_TYPE_MAP,
    classify_memory,
    normalize_memory_type,
)


@pytest.mark.parametrize(
    "text, source_type, expected_type, expected_confidence",
    [
        ("I always prefer to work late", "user", "fact", "high"),
        ("I am a night owl, I'm a developer", "user", "fact", "medium"),
        ("I always love dark humor, deadpan", "user", "fact", "medium"),
        ("I am feeling tired today", "user", "phase", "low"),
        ("I'm feeling sad and overwhelmed this week", "user", "phase", "high"),
        ("I am a person and I am sad today", "user", "phase", "high"),
        ("I keep doing it again, can't stop", "user", "watch", "low"),
        ("the meeting is at 3", "user", "context", "medium"),
        ("the meeting is at 3", "soulgate_inferred", "watch", "low"),
        ("I keep doing it again", "soulgate_inferred", "watch", "medium"),
        # Equal scores are broken by TYPE_PRIORITY (watch, then fact, then phase).
        ("I prefer to work late, lately exhausted", "user", "fact", "medium"),
        ("I am a developer, again", "user", "watch", "low"),
    ],
)
def test_classification(text, source_type, expected_type, expected_confidence):
    result = classify_memory(text, source_type)
    assert result["memory_type"] == expected_type
    assert result["confidence"] == expected_confidence


def test_summary_is_the_first_280_characters():
    long_text = "x" * 400
    result = classify_memory(long_text)
    assert result["memory_type"] == "context"
    assert len(result["summary"]) == 280

    short_text = "hello world"
    result = classify_memory(short_text)
    assert result["summary"] == short_text


def test_matching_ignores_case():
    lower_result = classify_memory("I always prefer to work late", "user")
    upper_result = classify_memory("I ALWAYS PREFER TO WORK LATE", "user")
    assert lower_result["memory_type"] == upper_result["memory_type"]
    assert lower_result["confidence"] == upper_result["confidence"]


def test_legacy_types_map_to_current_ones():
    # Test all the mappings from the brief
    assert normalize_memory_type("stable_preference") == "fact"
    assert normalize_memory_type("identity_trait") == "fact"
    assert normalize_memory_type("humor_style") == "fact"
    assert normalize_memory_type("temporary_phase") == "phase"
    assert normalize_memory_type("support_context") == "phase"
    assert normalize_memory_type("task_context") == "context"
    assert normalize_memory_type("harmful_pattern") == "watch"

    # Test that current types pass through unchanged
    assert normalize_memory_type("fact") == "fact"
    assert normalize_memory_type("skill") == "skill"

    # Test unknown values pass through unchanged
    assert normalize_memory_type("weird") == "weird"

    # Test None returns None
    assert normalize_memory_type(None) is None


def test_legacy_map_targets_are_current_types():
    for _legacy_type, current_type in LEGACY_TYPE_MAP.items():
        assert current_type in CURRENT_MEMORY_TYPES
