"""Auto-promotes clusters of confirmed observations into patterns.

Run after every observation create/confirm. Observations are considered
"qualifying" once they're confirmed or have been deduped/re-confirmed at
least twice. Qualifying observations are grouped by (signal_type, normalized
hypothesis) as a proxy for "similar hypothesis" - there's no LLM here to do
real semantic clustering, so exact-normalized-text grouping is the
rule-based stand-in. A group of 3+ becomes (or reinforces) a candidate
pattern; candidates promote to active at confirmation_count >= 5.
"""
import json
from datetime import UTC, datetime

from app.models.observation import Observation
from app.models.pattern import Pattern
from sqlalchemy import select

MIN_CLUSTER_SIZE = 3
PROMOTE_AT_CONFIRMATIONS = 5
DEPRECATE_AT_CONTRADICTIONS = 3

# A pattern that could reach 100% confidence could never be doubted, which
# means it could never be deprecated - the whole pipeline's honesty depends
# on deprecation staying reachable. Nothing sets confidence above this.
MAX_PATTERN_CONFIDENCE = 0.95


def clamp_confidence(value: float) -> float:
    return min(value, MAX_PATTERN_CONFIDENCE)


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _pattern_name_for(signal_type: str, hypothesis_norm: str) -> str:
    label = hypothesis_norm[:60] if hypothesis_norm else "unlabeled"
    return f"{signal_type}: {label}"


def maybe_promote(pattern: Pattern) -> None:
    if pattern.status == "candidate" and pattern.confirmation_count >= PROMOTE_AT_CONFIRMATIONS:
        pattern.status = "active"
        pattern.promoted_at = datetime.now(UTC)
    if pattern.status == "active" and pattern.contradiction_count >= DEPRECATE_AT_CONTRADICTIONS:
        pattern.status = "deprecated"


def promote_from_observations(db, agent_id: str, signal_type: str | None = None) -> list[Pattern]:
    stmt = select(Observation).where(Observation.agent_id == agent_id)
    if signal_type:
        stmt = stmt.where(Observation.signal_type == signal_type)
    rows = db.execute(stmt).scalars().all()

    qualifying = [r for r in rows if r.status == "confirmed" or r.confirmation_count >= 2]
    if not qualifying:
        return []

    clusters: dict[tuple[str, str], list[Observation]] = {}
    for row in qualifying:
        if not row.hypothesis:
            continue
        key = (row.signal_type, _normalize(row.hypothesis))
        clusters.setdefault(key, []).append(row)

    touched: list[Pattern] = []
    for (sig_type, hypothesis_norm), members in clusters.items():
        if len(members) < MIN_CLUSTER_SIZE:
            continue

        pattern_name = _pattern_name_for(sig_type, hypothesis_norm)
        observation_ids = [m.id for m in members]

        existing = db.execute(
            select(Pattern).where(Pattern.agent_id == agent_id, Pattern.pattern_name == pattern_name)
        ).scalars().first()

        if existing:
            new_size = len(members)
            # this function reruns on every observation create for the
            # signal_type, reclustering everything each time - only count it
            # as a fresh confirmation when the cluster actually grew, or an
            # unrelated observation of the same signal_type would silently
            # re-confirm this pattern every time it's created.
            if new_size > existing.instance_count:
                existing.confirmation_count += 1
            existing.instance_count = new_size
            existing.observation_ids_json = json.dumps(observation_ids)
            existing.last_confirmed_at = datetime.now(UTC)
            maybe_promote(existing)
            touched.append(existing)
        else:
            applies_to = []
            seen = set()
            for m in members:
                for eid in json.loads(m.entity_ids_json):
                    if eid not in seen:
                        seen.add(eid)
                        applies_to.append(eid)

            pattern = Pattern(
                agent_id=agent_id,
                pattern_name=pattern_name,
                description=f"Auto-promoted from {len(members)} observations ({sig_type}).",
                observation_ids_json=json.dumps(observation_ids),
                instance_count=len(members),
                confirmation_count=1,
                contradiction_count=0,
                confidence=0.5,
                interpretation=members[0].hypothesis,
                applies_to_entity_ids_json=json.dumps(applies_to),
                context_conditions_json=json.dumps({}),
                status="candidate",
                last_confirmed_at=datetime.now(UTC),
            )
            db.add(pattern)
            touched.append(pattern)

    db.commit()
    return touched
