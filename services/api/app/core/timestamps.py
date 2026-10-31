"""Request timestamps: one parser, so a malformed time is a 422 that names the field."""

from datetime import datetime

from fastapi import HTTPException


def parse_timestamp(value: str | None, field: str) -> datetime | None:
    """An ISO 8601 time from a request field, or None when it is empty. Bad text is the caller's
    mistake, so it is a 422 that names the field, never a 500."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(
            422, f"{field} must be an ISO 8601 time, for example 2026-10-31T09:00:00Z"
        ) from error
