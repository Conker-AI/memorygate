"""MemoryGate's model calls through Pi's model roles, so the owner picks the model in one place.

Configured by MEMORYGATE_PI_URL and MEMORYGATE_PI_SERVICE_KEY. Every call asks Pi's
`memory-sorting` role (`POST /inference`); Pi decides which model answers. Any failure - 503,
timeout, refusal, a malformed answer - returns None, the same "no model answer" every caller
already handles, so nothing is ever made up. The key goes only in the request header: it is
never logged, stored or returned. An answer also carries the model and provider Pi says wrote it.
"""

import logging
import uuid
from datetime import UTC, datetime

import httpx
from app.core.config import PI_SERVICE_KEY, PI_URL

ROLE = "memory-sorting"
CHOSEN_IN = "Chosen in Conker: Settings, Models, Memory sorting"

# Connecting must be quick; a hosted model may need a while to write its answer.
TIMEOUT = httpx.Timeout(90.0, connect=5.0)

log = logging.getLogger("memorygate")

# The model and provider of Pi's last answer since MemoryGate started; None until Pi names one.
_last_answer: dict | None = None


class PiText(str):
    """Pi's answer text, a plain string to every caller, plus the model and provider that wrote it.

    `model` and `provider` are None when Pi's answer did not name them in a usable shape.
    """

    def __new__(cls, text: str, model: str | None = None, provider: str | None = None):
        answer = super().__new__(cls, text)
        answer.model = model
        answer.provider = provider
        return answer


def pi_configured() -> bool:
    """Both settings present and the URL is plain HTTP(S); anything else keeps the direct path."""
    return bool(PI_SERVICE_KEY) and PI_URL.startswith(("http://", "https://"))


def model_choice() -> dict:
    """Who chooses MemoryGate's model, for the settings and runtime routes."""
    if pi_configured():
        return {"managed_by": "pi", "editable": False, "role": ROLE, "chosen_in": CHOSEN_IN}
    return {"managed_by": "memorygate", "editable": True}


def last_answer() -> dict | None:
    """`{"model", "provider", "at"}` of Pi's last named answer since start, or None."""
    return dict(_last_answer) if _last_answer else None


def generate(system: str, prompt: str, max_tokens: int, *, json_output: bool) -> PiText | None:
    """One answer from Pi's memory-sorting role, or None when Pi has no usable answer."""
    body = {
        "role": ROLE,
        "system": system,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "json": json_output,
        "request_id": f"memorygate.{uuid.uuid4().hex}",
    }
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                f"{PI_URL}/inference",
                json=body,
                headers={"X-Pi-Service-Key": PI_SERVICE_KEY},
            )
    except httpx.TimeoutException:
        log.warning("Pi inference timed out; continuing without a model answer.")
        return None
    except httpx.HTTPError as exc:
        log.warning(
            "Pi inference unreachable (%s); continuing without a model answer.", type(exc).__name__
        )
        return None
    if response.status_code != 200:
        log.warning(
            "Pi inference answered HTTP %s (%s); continuing without a model answer.",
            response.status_code,
            _refusal_code(response),
        )
        return None
    try:
        answer = response.json()
        text = answer.get("text")
    except (ValueError, AttributeError):
        answer, text = {}, None
    if not isinstance(text, str):
        log.warning("Pi inference returned no text; continuing without a model answer.")
        return None
    if not text.strip():
        return None
    model, provider = _name(answer.get("model")), _name(answer.get("provider"))
    if model:
        global _last_answer
        _last_answer = {"model": model, "provider": provider, "at": datetime.now(UTC).isoformat()}
    return PiText(text.strip(), model, provider)


def _name(value) -> str | None:
    """A model or provider name fit to store and show: short printable text, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= 200 and value.isprintable() else None


def _refusal_code(response: httpx.Response) -> str:
    """Pi's refusal code (`no_model`, `unavailable`), never its message or our request."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return "no detail"
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and code.isidentifier() else "no detail"
