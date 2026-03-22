"""
Rule-based security agent for the API gateway.

Scans request method, path, query string, headers, and body for patterns
indicating malicious, destructive, or security-compromising content.
Returns (passed, findings) where findings are human-readable messages.
"""

from __future__ import annotations

import re

# Type alias for scan result
ScanResult = tuple[bool, list[str]]


def _normalize_inputs(
    method: str,
    path: str,
    query_string: bytes,
    headers: list[tuple[bytes, bytes]],
    body_bytes: bytes,
) -> str:
    """Build a single string from all request parts for scanning. UTF-8 decode with replace."""
    parts = [method or "", path or ""]
    if query_string:
        parts.append(query_string.decode("utf-8", errors="replace"))
    for k, v in headers or []:
        parts.append(k.decode("utf-8", errors="replace") + " " + v.decode("utf-8", errors="replace"))
    if body_bytes:
        parts.append(body_bytes.decode("utf-8", errors="replace"))
    return "\n".join(parts).lower()


# Rules: (pattern, message). Pattern is compiled regex or we use re.escape for literal.
# Messages are human-readable findings for the 403 response.
_RULES: list[tuple[re.Pattern[str], str]] = []


def _add_rule(pattern: str, message: str, flags: int = re.IGNORECASE) -> None:
    _RULES.append((re.compile(pattern, flags), message))


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

# Path traversal
_add_rule(r"\.\./", "Path traversal sequence (e.g. '..') detected.")
_add_rule(r"\.\.\\", "Path traversal sequence (e.g. '..\\') detected.")
_add_rule(r"%2e%2e%2f", "Path traversal sequence (encoded) detected.")
_add_rule(r"%2e%2e/", "Path traversal sequence (encoded) detected.")
_add_rule(r"\.\.%2f", "Path traversal sequence (encoded) detected.")

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
        method: HTTP method (e.g. GET, POST).
        path: Request path (e.g. /api/blogging/full-pipeline).
        query_string: Raw query string bytes.
        headers: ASGI headers list of (name, value) bytes.
        body_bytes: Raw request body bytes.

    Returns:
        (passed, findings). passed is True if no issues; otherwise False with
        a non-empty list of human-readable finding messages.
    """
    text = _normalize_inputs(method, path, query_string, headers, body_bytes)
    findings: list[str] = []
    for pattern, message in _RULES:
        if pattern.search(text):
            findings.append(message)
            # Return on first match (plan: "return (False, findings) on first rule match")
            return (False, findings)
    return (True, [])
