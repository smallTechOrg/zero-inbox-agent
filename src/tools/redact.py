"""Egress chokepoint: strip obvious secrets from any string BEFORE it leaves the machine.

Every subject/snippet/body fragment that is sent to an LLM passes through :func:`redact`
first (see ``graph.nodes.redact_items``). Redaction happens here, never inside the
provider, so there is exactly one place to audit.

Replacements are of the form ``[REDACTED:<kind>]`` with kinds:
``api_key``, ``card``, ``otp``, ``password``.
"""

from __future__ import annotations

import re

SNIPPET_MAX_CHARS = 200

_API_KEY_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
]

# 13-19 digits, optionally split by single spaces or hyphens.
_CARD_RE = re.compile(r"(?<![\d\-])(?:\d[ -]?){12,18}\d(?![\d\-])")

# 4-8 digit code appearing in a security/verification context.
_OTP_RE = re.compile(
    r"(?i)\b(one[\s\-]?time\s+(?:code|password|passcode)|otp|passcode|pin|"
    r"(?:\w+\s+)?code)\b([^\d\n]{0,32}?)(\d{4,8})(?!\d)"
)

_PASSWORD_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|secret|api[\s_\-]?key|token)\b"
    r"(\s*(?:is|:|=)\s*)(?!\[REDACTED)(\S+)"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[REDACTED:card]"
        return match.group(0)

    return _CARD_RE.sub(_sub, text)


def redact(text: str | None) -> str:
    """Return ``text`` with OTPs, API keys, card numbers and passwords removed."""
    if not text:
        return "" if text is None else text

    out = text
    for pattern in _API_KEY_PATTERNS:
        out = pattern.sub("[REDACTED:api_key]", out)
    out = _redact_cards(out)
    out = _OTP_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:otp]", out)
    out = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:password]", out)
    return out


def redact_snippet(text: str | None, *, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """Redact and hard-truncate to the ``<=200`` char snippet budget."""
    return redact(text)[:max_chars]


def redact_item(item: dict) -> dict:
    """Redact the content-bearing fields of an item dict **in place**."""
    item["subject"] = redact(item.get("subject") or "")
    item["snippet_redacted"] = redact_snippet(
        item.get("snippet_redacted") or item.get("snippet") or ""
    )
    # A raw body must never survive past the chokepoint.
    item.pop("snippet", None)
    item.pop("body", None)
    item.pop("body_text", None)
    item.pop("body_html", None)
    return item


def redact_items(items: list[dict]) -> list[dict]:
    """Redact every item in the list, in place. Returns the same list."""
    for item in items:
        redact_item(item)
    return items
