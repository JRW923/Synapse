"""Bounded, non-content report values shared by benchmark result writers."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_MAX_ITEMS = 128
_MAX_DEPTH = 8
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEYS = {
    "api_key", "apikey", "access_token", "auth_token", "authorization",
    "password", "secret", "token",
}
_SECRET_KEY_SUFFIXES = (
    "_api_key", "_access_token", "_auth_token", "_password", "_secret", "_token",
)
_SAFE_STRING_KEYS = {
    "action",
    "backend",
    "category",
    "functional_grader",
    "git_commit",
    "grader",
    "grader_label",
    "grader_version",
    "harness",
    "isolation",
    "license",
    "model",
    "model_id",
    "name",
    "official_runner",
    "path",
    "platform",
    "provider",
    "python_version",
    "run_id",
    "source",
    "status",
    "token_count_source",
    "token_count_sources",
    "trajectory",
    "type",
    "version",
    "synapse_version",
    "actual_model_ids",
    "actual_run_ids",
}


def text_fingerprint(value: Any) -> dict[str, int | str]:
    """Return metadata for text without retaining its contents."""
    text = value if isinstance(value, str) else str(value)
    raw = text.encode("utf-8", errors="replace")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def redact_text(value: Any) -> str:
    """Return a stable human-readable digest instead of arbitrary text."""
    digest = text_fingerprint(value)
    return f"[redacted text: {digest['bytes']} bytes; sha256={digest['sha256']}]"


def is_sensitive_key(key: Any) -> bool:
    """Return whether a mapping key conventionally contains credentials."""
    normalized = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip(),
    ).lower().replace("-", "_").replace(" ", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)


def redact_secrets(value: Any) -> Any:
    """Recursively replace credential-bearing fields while preserving structure."""
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if is_sensitive_key(key) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_secrets(item) for item in value]
    return value


def _strip_url_credentials(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return redact_text(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return redact_text(value)
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def sanitize_value(
    value: Any,
    *,
    key: str | None = None,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Keep numeric report facts while removing free-form text and bounding size."""
    if key is not None and is_sensitive_key(key):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if key in _SAFE_STRING_KEYS or (
            key is not None
            and (
                key == "sha256"
                or key.endswith("_sha256")
                or key.endswith("_fingerprint")
            )
            and _HASH_RE.fullmatch(value)
        ):
            return _strip_url_credentials(value)
        return redact_text(value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace"))

    seen = _seen if _seen is not None else set()
    marker = id(value)
    if marker in seen:
        return "[redacted cyclic value]"
    if _depth >= _MAX_DEPTH:
        return "[redacted nested value]"

    if isinstance(value, Mapping):
        seen.add(marker)
        result: dict[str, Any] = {}
        items = list(value.items())
        for raw_key, item in items[:_MAX_ITEMS]:
            item_key = str(raw_key)
            result[item_key] = sanitize_value(
                item, key=item_key, _depth=_depth + 1, _seen=seen,
            )
        if len(items) > _MAX_ITEMS:
            result["_redacted_items"] = len(items) - _MAX_ITEMS
        seen.remove(marker)
        return result

    if isinstance(value, (list, tuple, set)):
        seen.add(marker)
        items = list(value)
        result = [
            sanitize_value(item, key=key, _depth=_depth + 1, _seen=seen)
            for item in items[:_MAX_ITEMS]
        ]
        if len(items) > _MAX_ITEMS:
            result.append({"_redacted_items": len(items) - _MAX_ITEMS})
        seen.remove(marker)
        return result

    return redact_text(repr(value))


__all__ = [
    "is_sensitive_key", "redact_secrets", "redact_text", "sanitize_value",
    "text_fingerprint",
]
