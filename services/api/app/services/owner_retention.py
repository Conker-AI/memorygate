"""Narrow retention directives in attributable owner conversation, never listeners.

Recognizing a request changes admission only. The complete original statement
remains untrusted evidence; it grants no permissions and is not an inferred fact.
"""

import re
import unicodedata

_PLEASE = r"(?:please(?:[ \t]*,[ \t]*|[ \t]+))?"
_POZHALUYSTA = r"(?:пожалуйста(?:[ \t]*,[ \t]*|[ \t]+))?"
_REQUEST = re.compile(
    r"\A[ ]{0,3}(?:"
    + _PLEASE
    + r"remember(?:[ \t]+that[ \t]+|[ \t]+this[ \t]*:[ \t]*|[ \t]*:[ \t]*)"
    + r"|"
    + _POZHALUYSTA
    + r"запомни(?:те)?(?:[ \t]*,[ \t]*что[ \t]+|[ \t]+что[ \t]+"
    + r"|[ \t]+это[ \t]*:[ \t]*|[ \t]*:[ \t]*)"
    + r")(?P<body>[\s\S]*)\Z"
)
_DECLINE = re.compile(
    r"\A[ ]{0,3}(?:"
    + _PLEASE
    + r"(?:do[ \t]+not|don't|don\u2019t|never)[ \t]+remember\b"
    + r"|"
    + _POZHALUYSTA
    + r"не[ \t]+(?:запоминай(?:те)?|запомни(?:те)?)\b"
    + r")"
)


def retention_intent(content: str) -> str | None:
    """Return a direct request/decline, leaving other wording to existing scoring.

    Leading quotes, block quotes, fenced/indented code and indirect questions do
    not match. A bare starter or punctuation-only placeholder is not evidence.
    Normalization affects recognition only, never the retained original text.
    """
    normalized = unicodedata.normalize("NFKC", content).casefold()
    if _DECLINE.match(normalized):
        return "declined"
    match = _REQUEST.fullmatch(normalized)
    if (
        match
        and not normalized.rstrip().endswith("?")
        and any(
            unicodedata.category(character)[0] in {"L", "N", "S"} for character in match["body"]
        )
    ):
        return "requested"
    return None
