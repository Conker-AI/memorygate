MEMORY_TYPE_BONUSES = {
    "fact": 0.45,
    "watch": 0.40,
    "skill": 0.35,
    "context": 0.10,
    "phase": -0.05,
}
CONFIDENCE_BONUSES = {"high": 0.20, "medium": 0.10, "low": 0.0}


def memory_rank_bonus(memory_type: str, confidence: str) -> float:
    return MEMORY_TYPE_BONUSES.get(memory_type, 0.0) + CONFIDENCE_BONUSES.get(confidence, 0.0)

def memory_strength(memory_type: str, confidence: str) -> float:
    return memory_rank_bonus(memory_type, confidence)
