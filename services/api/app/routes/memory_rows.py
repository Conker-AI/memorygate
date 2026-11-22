"""Row and tag helpers shared by the memory route modules."""

import json
from datetime import UTC, datetime, timedelta

PHASE_REVIEW_WINDOW = timedelta(days=14)


def _default_review_by(memory_type: str, review_by: datetime | None) -> datetime | None:
    if review_by is not None:
        return review_by
    if memory_type == "phase":
        return datetime.now(UTC) + PHASE_REVIEW_WINDOW
    return None


def _row_to_dict(row, score=None):
    tags = json.loads(row.tags_json)
    if isinstance(tags, dict):
        tags = tags.get("tags", [])
    result = {
        "id": row.id,
        "revision": row.revision,
        "agent_id": row.agent_id,
        "text": row.text,
        "summary": row.summary,
        "memory_type": row.memory_type,
        "source_type": row.source_type,
        "confidence": row.confidence,
        "do_not_generalize": row.do_not_generalize,
        "review_by": row.review_by.isoformat() if row.review_by else None,
        "tags": tags,
        "status": row.status,
        "valid_from": row.valid_from.isoformat() if row.valid_from else None,
        "valid_until": row.valid_until.isoformat() if row.valid_until else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if score is not None:
        result["similarity"] = score
    return result


def _merge_tags(old_tags: list[str], new_tags: list[str]) -> list[str]:
    merged = []
    seen = set()
    for tag in old_tags + new_tags:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    return merged
