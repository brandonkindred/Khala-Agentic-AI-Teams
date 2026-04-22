"""
Rule-based security agent for the API gateway.

Scans request method, path, query string, headers, and body for patterns
indicating malicious, destructive, or security-compromising content.
Returns (passed, findings) where findings are human-readable messages.

Each rule declares which request parts it applies to via a scope set.
Path-traversal patterns are scoped to URL-layer inputs (path, query string,
headers) because request bodies in this system are primarily free-form
content destined for storage or LLM prompts, not filesystem path handling —
so scanning bodies for ``../`` produces false positives without blocking any
real attack vector.
"""

from __future__ import annotations

import re

# Type alias for scan result
ScanResult = tuple[bool, list[str]]

# Scope identifiers for per-rule request-part targeting.
_SCOPE_PATH = "path"
_SCOPE_QUERY = "query_string"
_SCOPE_HEADERS = "headers"
_SCOPE_BODY = "body"

_URL_SCOPES: frozenset[str] = frozenset({_SCOPE_PATH, _SCOPE_QUERY, _SCOPE_HEADERS})
_ALL_SCOPES: frozenset[str] = _URL_SCOPES | {_SCOPE_BODY}

# Fixed order used when joining per-scope strings into a rule's haystack.
_SCOPE_ORDER: tuple[str, ...] = (_SCOPE_PATH, _SCOPE_QUERY, _SCOPE_HEADERS, _SCOPE_BODY)


def _normalize_parts(
    path: str,
    query_string: bytes,
    headers: list[tuple[bytes, bytes]],
    body_bytes: bytes,
) -> dict[str, str]:
    """Return a per-scope dict of lowercased, UTF-8-decoded request parts."""
    header_blob = " ".join(
        k.decode("utf-8", errors="replace") + " " + v.decode("utf-8", errors="replace") for k, v in headers or []
    )
    return {
        _SCOPE_PATH: (path or "").lower(),
        _SCOPE_QUERY: query_string.decode("utf-8", errors="replace").lower() if query_string else "",
        _SCOPE_HEADERS: header_blob.lower(),
        _SCOPE_BODY: body_bytes.decode("utf-8", errors="replace").lower() if body_bytes else "",
    }


# Rules: (pattern, message, scopes). Scopes declare which request parts each
# rule applies to; unlisted parts are skipped for that rule.
_RULES: list[tuple[re.Pattern[str], str, frozenset[str]]] = []


def _add_rule(
    pattern: str,
    message: str,
    *,
    scopes: frozenset[str] = _ALL_SCOPES,
    flags: int = re.IGNORECASE,
) -> None:
    _RULES.append((re.compile(pattern, flags), message, scopes))


# Destructive / dangerous commands
_add_rule(r"\brm\s+-rf\b", "Destructive shell command pattern detected (e.g. rm -rf).")
_add_rule(r"\brm\s+-r\s+-f\b", "Destructive shell command pattern detected (e.g. rm -r -f).")
_add_rule(r"\bdel\s+/f\s+/s", "Destructive Windows command pattern detected (del /f /s).")
_add_rule(r"\bformat\s+[a-z]:", "Destructive format command pattern detected.")
_add_rule(r"\bdrop\s+table\b", "Destructive SQL pattern detected (DROP TABLE).")
_add_rule(r"\btruncate\s+table\b", "Destructive SQL pattern detected (TRUNCATE TABLE).")
_add_rule(r";\s*rm\s+", "Shell command chaining with rm detected.")
_add_rule(r"&&\s*rm\s+", "Shell command chaining with rm detected.")
_add_rule(r"\|\s*rm\s+", "Shell command piping to rm detected.")
_add_rule(r"\$\(rm\s+", "Command substitution with rm detected.")
_add_rule(r"`rm\s+", "Backtick command with rm detected.")
_add_rule(r"&\s*&\s*del\s+", "Shell/command chaining with del detected.")

# Path traversal — URL-layer only; bodies are free-form content.
_add_rule(r"\.\./", "Path traversal sequence (e.g. '..') detected.", scopes=_URL_SCOPES)
_add_rule(r"\.\.\\", "Path traversal sequence (e.g. '..\\') detected.", scopes=_URL_SCOPES)
_add_rule(r"%2e%2e%2f", "Path traversal sequence (encoded) detected.", scopes=_URL_SCOPES)
_add_rule(r"%2e%2e/", "Path traversal sequence (encoded) detected.", scopes=_URL_SCOPES)
_add_rule(r"\.\.%2f", "Path traversal sequence (encoded) detected.", scopes=_URL_SCOPES)

# Prompt / instruction override
_add_rule(r"ignore\s+(all\s+)?previous\s+instructions", "Prompt or instruction override phrase detected.")
_add_rule(r"disregard\s+(all\s+)?(previous|above|prior)", "Prompt or instruction override phrase detected.")
_add_rule(r"jailbreak", "Content may attempt to bypass safety or security controls.")
_add_rule(r"override\s+(system|security|safety)", "System override phrase detected.")

# Script injection
_add_rule(r"<script\b", "Script injection pattern detected (<script).")
_add_rule(r"javascript\s*:", "Script injection pattern (javascript:) detected.")

# Conservative SQL injection
_add_rule(r"'\s*or\s*'\s*1\s*=\s*'1", "SQL injection-like pattern detected.")
_add_rule(r'"\s*or\s*"\s*1\s*=\s*"1', "SQL injection-like pattern detected.")


def scan(
    method: str,
    path: str,
    query_string: bytes,
    headers: list[tuple[bytes, bytes]],
    body_bytes: bytes,
) -> ScanResult:
    """
    Scan request for malicious, destructive, or security-compromising content.

    Args:
        method: HTTP method (e.g. GET, POST). Unused for pattern matching —
            kept in the signature for API stability and future rule options.
        path: Request path (e.g. /api/blogging/full-pipeline).
        query_string: Raw query string bytes.
        headers: ASGI headers list of (name, value) bytes.
        body_bytes: Raw request body bytes.

    Returns:
        (passed, findings). passed is True if no issues; otherwise False with
        a non-empty list of human-readable finding messages.
    """
    del method  # method is not scanned; no rule meaningfully matches GET/POST.
    parts = _normalize_parts(path, query_string, headers, body_bytes)
    for pattern, message, scopes in _RULES:
        haystack = "\n".join(parts[name] for name in _SCOPE_ORDER if name in scopes)
        if pattern.search(haystack):
            return (False, [message])
    return (True, [])
