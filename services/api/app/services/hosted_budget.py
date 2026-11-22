"""Refuse unmetered hosted inference; preserve the denied cost decision without prompt text."""

import json
import os
import time

from app.models.audit import MemoryAudit


def record_refusal(db, model: str, input_chars: int, max_tokens: int) -> dict:
    quote = {"status": "unknown", "upper_bound_microusd": None}
    try:
        supplied = json.loads(os.environ.get("MEMORYGATE_HOSTED_COST_QUOTE", "{}"))
        names = ("input_token_ceiling", "input_per_million_microusd", "output_per_million_microusd")
        if (
            supplied["model"] != model
            or not time.time() < supplied["valid_until"]
            or not str(supplied["source"]).startswith("https://")
            or any(type(supplied[n]) is not int or not 0 < supplied[n] <= 10**12 for n in names)
        ):
            raise ValueError("Quote is not current for this model")
        total = (
            supplied["input_token_ceiling"] * supplied["input_per_million_microusd"]
            + max_tokens * supplied["output_per_million_microusd"]
            + 999999
        ) // 1000000
        quote = {name: supplied[name] for name in ("model", "valid_until", "source", *names)}
        quote.update(status="owner_supplied_estimate", upper_bound_microusd=total)
    except (KeyError, TypeError, ValueError):
        quote["reason"] = (
            "No current model-specific prices and input ceiling supplied; cost is unknown"
        )
    decision = {
        "provider": "openai",
        "model": model,
        "input_chars": input_chars,
        "max_output_tokens": max_tokens,
        "dispatched": False,
        "quote": quote,
        "reason": "HOSTED_BUDGET_UNAVAILABLE",
        "next_action": "Select local Ollama; hosted inference needs a durable shared-budget adapter",
    }
    db.add(MemoryAudit(action="hosted_generation_refused", payload_json=json.dumps(decision)))
    db.commit()
    return decision
