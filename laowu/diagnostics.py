"""Classify CLI failures without returning any provider text or environment values.

Only fixed enums and bounded numeric metadata leave this module. Pattern matches
are hints, not proof of a provider/account fault. Unknown errors stay unknown.
"""
from __future__ import annotations

import re
import errno
import sqlite3


_VARIANTS = {
    "Unauthorized": "authentication", "UsageLimitExceeded": "usage_limit",
    "ContextWindowExceeded": "context_limit", "HttpConnectionFailed": "network",
    "ResponseStreamConnectionFailed": "stream", "ResponseStreamDisconnected": "stream",
    "ResponseTooManyFailedAttempts": "stream", "SandboxError": "permission",
    "InternalServerError": "server", "BadRequest": "bad_request", "Other": "unknown",
}
_VARIANT_NAMES = {name.replace("_", "").lower(): name for name in _VARIANTS}
_PATTERNS = tuple((category, re.compile(pattern, re.I)) for category, pattern in (
    ("database_busy", r"database (?:table )?is locked|database is busy"),
    ("database_read_only", r"attempt to write a readonly database"),
    ("filesystem_read_only", r"read.only file system|\bos error 30\b"),
    ("authentication", r"\bunauthorized\b|\b401\b|invalid (?:api key|authentication)|"
                       r"(?:access|refresh|auth(?:entication)?) token.{0,40}(?:expired|invalid)|"
                       r"not (?:logged|signed) in|authentication (?:failed|required)"),
    ("usage_limit", r"usage limit|insufficient_quota|quota exceeded"),
    ("rate_limit", r"rate.?limit|too many requests|\b429\b"),
    ("context_limit", r"context (?:window|length).{0,40}(?:exceed|limit)|context_length_exceeded"),
    ("permission", r"permission denied|operation not permitted|\bos error (?:1|13)\b|sandbox.{0,40}(?:failed|error)"),
    ("server", r"\b(?:500|502|503|504)\b|internal server error|service unavailable"),
    ("stream", r"stream (?:disconnected|closed|ended)|response.{0,20}stream.{0,40}(?:fail|disconnect)|reconnecting\.\.\."),
    ("network", r"connection (?:refused|reset|closed)|failed to connect|error sending request|"
                r"network is unreachable|dns error|name resolution|timed out|\btimeout\b|tls.{0,20}(?:error|fail)"),
    ("configuration", r"error loading (?:config|configuration)|unknown (?:field|variant)|"
                      r"failed to (?:parse|load) config|unexpected argument|unrecognized (?:option|argument)"),
))


def classify_error(value: object) -> dict:
    """Read only known error fields; never echo messages, codes, URLs or IDs."""
    nodes = [value] if isinstance(value, dict) else []
    if nodes and isinstance(value.get("error"), dict):
        nodes.append(value["error"])
    variant = None
    status = None
    for node in list(nodes):
        info = node.get("codexErrorInfo", node.get("codex_error_info"))
        if isinstance(info, str):
            variant = _VARIANT_NAMES.get(info.replace("_", "").lower(), variant)
        elif isinstance(info, dict):
            for key, detail in info.items():
                name = _VARIANT_NAMES.get(key.replace("_", "").lower())
                if name:
                    variant = name
                    if isinstance(detail, dict):
                        nodes.append(detail)
                    break
    for node in nodes:
        for key in ("httpStatusCode", "http_status_code"):
            number = node.get(key)
            if type(number) is int and 100 <= number <= 599:
                status = number
    category = _VARIANTS.get(variant, "unknown")
    if status in (401, 403):
        category = "authentication"
    elif status == 429:
        category = "rate_limit"
    elif status is not None and status >= 500:
        category = "server"
    basis = "structured" if category != "unknown" else "unclassified"
    if category == "unknown":
        texts = [value] if isinstance(value, str) else []
        for node in nodes:
            texts.extend(node[key] for key in ("message", "error") if isinstance(node.get(key), str))
        for text in texts:
            for name, pattern in _PATTERNS:
                if pattern.search(text[:16384]):
                    category, basis = name, "message_pattern"
                    break
            if category != "unknown":
                break
    result = {"category": category, "basis": basis}
    if status is not None:
        result["http_status"] = status
    if variant is not None:
        result["variant"] = variant
    return result


def error_code(diagnostic: dict) -> str:
    category = diagnostic["category"]
    return "client_error" if category == "unknown" else "client_" + category


def classify_exception(exc: Exception) -> dict:
    """Prefer fixed OS/SQLite codes; exception messages only feed the classifier."""
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', None)
        if type(code) is int and 0 < code < 65536:
            # Stable SQLite primary result codes; Python 3.10 does not expose
            # sqlite3.SQLITE_* constants or attach codes to its own exceptions.
            category = {5: 'database_busy', 6: 'database_busy', 8: 'database_read_only',
                        13: 'storage_full', 14: 'database_unavailable',
                        11: 'database_corrupt'}.get(code & 255, 'database_error')
            return {'category': category, 'basis': 'structured', 'sqlite_code': code}
    if isinstance(exc, OSError):
        category = {errno.EROFS: 'filesystem_read_only', errno.EACCES: 'permission',
                    errno.EPERM: 'permission', errno.ENOSPC: 'storage_full',
                    errno.EMFILE: 'resource_limit', errno.ENFILE: 'resource_limit'}.get(exc.errno)
        if category:
            return {'category': category, 'basis': 'structured', 'os_errno': exc.errno}
    return classify_error(str(exc))


class ErrorStream:
    """Drain stderr with bounded RAM, retaining only a classification and count."""
    def __init__(self):
        self.byte_count = 0
        self.diagnostic = None
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        self.byte_count += len(chunk)
        for offset in range(0, len(chunk), 8192):
            data = self._tail + chunk[offset:offset + 8192]
            diagnostic = classify_error(data.decode("utf-8", errors="replace"))
            if diagnostic["category"] != "unknown":
                self.diagnostic = diagnostic
            self._tail = data[-256:]
