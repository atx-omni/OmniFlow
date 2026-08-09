from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from .exceptions import SecurityPolicyError


SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password)", re.IGNORECASE)
SECRET_VALUE_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"(OMNI_API_KEY=)[^\s]+|"
    r"([?&](?:api[_-]?key|token|secret|password)=)[^&\s]+",
    re.IGNORECASE,
)
RAW_KEYS = {"raw", "raw_issue", "raw_payload", "raw_response", "payload"}
STANDARD_PUBLIC_REDACT_KEYS = {
    "email",
    "owner_email",
    "content_url",
    "document_url",
    "url",
    "web_url",
    "folder_path",
    "folder_name",
}
STRICT_PUBLIC_REDACT_KEYS = STANDARD_PUBLIC_REDACT_KEYS | {
    "owner",
    "document_owner",
    "owner_name",
    "name",
    "content_name",
    "document_name",
    "query_name",
    "folder",
    "labels",
}


def contains_secret_key(key: Any) -> bool:
    return isinstance(key, str) and bool(SECRET_KEY_RE.search(key))


def find_secret_keys(payload: Any, prefix: str = "") -> list[str]:
    matches: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if contains_secret_key(key):
                matches.append(path)
            matches.extend(find_secret_keys(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            matches.extend(find_secret_keys(value, f"{prefix}[{index}]"))
    return matches


def reject_secret_keys(payload: Any, *, source: str) -> None:
    keys = find_secret_keys(payload)
    if keys:
        joined = ", ".join(sorted(keys))
        raise SecurityPolicyError(
            f"Secret-like keys are not allowed in {source}: {joined}. "
            "Use environment variables or a secret manager instead."
        )


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            redacted[key] = "[REDACTED]" if contains_secret_key(key) else redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE_RE.sub(lambda match: _redact_match(match), value)
    return value


def public_safe(value: Any, *, redaction_level: str = "standard") -> Any:
    if redaction_level not in {"standard", "strict"}:
        raise SecurityPolicyError("security.redaction_level must be 'standard' or 'strict'")
    return _public_safe(value, strict=redaction_level == "strict")


def _public_safe(value: Any, *, strict: bool) -> Any:
    if isinstance(value, Mapping):
        safe = {}
        redact_keys = STRICT_PUBLIC_REDACT_KEYS if strict else STANDARD_PUBLIC_REDACT_KEYS
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in RAW_KEYS:
                continue
            if contains_secret_key(key) or normalized in redact_keys or normalized.endswith("_url") or normalized.endswith("_email"):
                safe[key] = "[REDACTED]"
            else:
                safe[key] = _public_safe(item, strict=strict)
        return safe
    if isinstance(value, list):
        return [_public_safe(item, strict=strict) for item in value]
    return redact(value)


def _redact_match(match: re.Match[str]) -> str:
    for index in range(1, len(match.groups()) + 1):
        prefix = match.group(index)
        if prefix:
            return f"{prefix}[REDACTED]"
    return "[REDACTED]"


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True
