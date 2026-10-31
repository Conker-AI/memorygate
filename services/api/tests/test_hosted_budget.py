"""The hosted-inference refusal: an estimate is recorded only from a current, exact quote, never prompt text."""

import json
import time

import pytest
from app.core.db import Base
from app.models.audit import MemoryAudit
from app.services.hosted_budget import record_refusal
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

MODEL = "hosted-test"
DECISION_KEYS = {"provider", "model", "input_chars", "max_output_tokens", "dispatched", "quote", "reason", "next_action"}
UNKNOWN = "No current model-specific prices and input ceiling supplied; cost is unknown"


def _quote(**changes):
    quote = {"model": MODEL, "valid_until": time.time() + 60, "source": "https://provider.example/prices",
             "input_token_ceiling": 1000, "input_per_million_microusd": 3, "output_per_million_microusd": 7}
    quote.update(changes)
    return quote


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'budget.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.delenv("MEMORYGATE_HOSTED_COST_QUOTE", raising=False)
    with sessionmaker(engine)() as session:
        yield session
    engine.dispose()


def _refuse(db, monkeypatch, quote, max_tokens=160):
    if quote is not None:
        monkeypatch.setenv("MEMORYGATE_HOSTED_COST_QUOTE", quote if isinstance(quote, str) else json.dumps(quote))
    decision = record_refusal(db, MODEL, 4321, max_tokens)
    rows = list(db.scalars(select(MemoryAudit)))
    assert [row.action for row in rows] == ["hosted_generation_refused"]
    assert json.loads(rows[0].payload_json) == decision
    # Only counts and the cost decision are kept: the record has no field that could carry prompt text.
    assert set(decision) == DECISION_KEYS and decision["input_chars"] == 4321
    assert decision["dispatched"] is False and decision["reason"] == "HOSTED_BUDGET_UNAVAILABLE"
    return decision


def test_a_current_quote_gives_an_upper_bound_rounded_up_and_still_refuses(db, monkeypatch):
    decision = _refuse(db, monkeypatch, _quote(status="free", upper_bound_microusd=0, note="ignored"))
    quote = decision["quote"]
    # (1000 * 3 + 160 * 7) / 1e6 is a fraction of a micro-dollar: the bound rounds up, never down to zero.
    assert quote["upper_bound_microusd"] == 1
    assert quote["status"] == "owner_supplied_estimate"
    assert set(quote) == {"model", "valid_until", "source", "input_token_ceiling", "input_per_million_microusd",
                          "output_per_million_microusd", "status", "upper_bound_microusd"}


@pytest.mark.parametrize(("ceiling", "in_price", "out_price", "max_tokens"), [
    (1000, 1_000_000, 2_000_000, 160),
    (8192, 2_500_000, 10_000_000, 1),
    (1, 10**12, 10**12, 4096),
])
def test_the_bound_is_the_ceiling_formula(db, monkeypatch, ceiling, in_price, out_price, max_tokens):
    supplied = _quote(input_token_ceiling=ceiling, input_per_million_microusd=in_price,
                      output_per_million_microusd=out_price)
    decision = _refuse(db, monkeypatch, supplied, max_tokens)
    expected = (ceiling * in_price + max_tokens * out_price + 999999) // 1000000
    assert decision["quote"]["upper_bound_microusd"] == expected


EXPIRED = object()
DROP_CEILING = object()


@pytest.mark.parametrize("quote", [
    None,
    "{}",
    "{not json",
    "[]",
    "null",
    EXPIRED,
    {"valid_until": "tomorrow"},
    {"model": "another-model"},
    {"source": "http://provider.example/prices"},
    {"source": None},
    {"input_per_million_microusd": 1.5},
    {"output_per_million_microusd": True},
    {"input_token_ceiling": 0},
    {"input_per_million_microusd": -1},
    {"output_per_million_microusd": 10**12 + 1},
    DROP_CEILING,
], ids=["no-variable", "empty", "bad-json", "list", "null", "expired", "date-text", "other-model", "http-source",
        "no-source", "float-price", "bool-price", "zero-ceiling", "negative-price", "price-too-high", "missing-ceiling"])
def test_anything_but_a_current_exact_quote_leaves_the_cost_unknown(db, monkeypatch, quote):
    # Quotes are built here, not at collection, so each case fails for its own reason and not for age.
    if quote is EXPIRED:
        quote = _quote(valid_until=time.time() - 1)
    elif quote is DROP_CEILING:
        quote = {key: value for key, value in _quote().items() if key != "input_token_ceiling"}
    elif isinstance(quote, dict):
        quote = _quote(**quote)
    decision = _refuse(db, monkeypatch, quote)
    assert decision["quote"] == {"status": "unknown", "upper_bound_microusd": None, "reason": UNKNOWN}
