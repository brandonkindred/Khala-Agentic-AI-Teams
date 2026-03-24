"""
Ollama-backed LLM client for the central LLM service.

Uses /v1/chat/completions (OpenAI-compatible), /api/show for context size,
retries/backoff, and concurrency limit. Supports Ollama Cloud auth.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict, Optional

import httpx

from .. import config as llm_config
from ..interface import (
    LLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
)

logger = logging.getLogger(__name__)

# Default cap for max_tokens
DEFAULT_MAX_OUTPUT_TOKENS = 32768

# Continuation on truncation (same behavior as software_engineering_team)
MAX_CONTINUATION_CYCLES = 10
CONTINUATION_CONTEXT_CHARS = 150

# Max response/body length to log (avoid huge logs)
_MAX_LOG_BODY = 2000

# Expected keys for "try every code block" fallback
_EXPECTED_KEYS = frozenset(
    {
        "files",
        "summary",
        "code",
        "overview",
        "issues",
        "approved",
        "components",
        "architecture_document",
        "diagrams",
        "decisions",
        "tasks",
        "execution_order",
        "bugs_found",
        "integration_tests",
        "unit_tests",
        "readme_content",
        "feedback_items",
    }
)
_JSON_NOISE_RE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\uFFFD]")


def _parse_retry_config() -> tuple[int, float, float]:
    """Parse retry env vars. Returns (max_retries, initial_backoff_seconds, backoff_max_seconds).

    Backoff is exponential: wait initial * 2^attempt after each failure (first retry ~initial seconds).
    """
    raw_retries = (
        os.environ.get(llm_config.ENV_LLM_MAX_RETRIES)
        or os.environ.get(llm_config.ENV_LLM_MAX_RETRIES_SW)
        or "6"
    )
    raw_initial = (
        os.environ.get(llm_config.ENV_LLM_BACKOFF_BASE)
        or os.environ.get(llm_config.ENV_LLM_BACKOFF_BASE_SW)
        or "2"
    )
    raw_max = (
        os.environ.get(llm_config.ENV_LLM_BACKOFF_MAX)
        or os.environ.get(llm_config.ENV_LLM_BACKOFF_MAX_SW)
        or "120"
    )
    try:
        max_retries = max(0, int(raw_retries))
    except ValueError:
        max_retries = 6
    try:
        initial_backoff = float(raw_initial)
    except ValueError:
        initial_backoff = 2.0
    try:
        backoff_max = float(raw_max)
    except ValueError:
        backoff_max = 120.0
    return max_retries, initial_backoff, backoff_max


def _exponential_retry_delay(
    failed_attempt_index: int, initial_seconds: float, cap_seconds: float
) -> float:
    """Seconds to wait before the next HTTP attempt. failed_attempt_index is 0 after the first failure (waits ~initial_seconds)."""
    base = initial_seconds * (2**failed_attempt_index)
    jitter = random.uniform(0, min(2.0, max(0.25, base * 0.1)))
    return min(base + jitter, cap_seconds)


_ollama_semaphore: Optional[threading.BoundedSemaphore] = None
_semaphore_lock = threading.Lock()


def _get_ollama_semaphore() -> threading.BoundedSemaphore:
    """Lazily create the global Ollama concurrency semaphore."""
    global _ollama_semaphore
    with _semaphore_lock:
        if _ollama_semaphore is None:
            raw = (
                os.environ.get(llm_config.ENV_LLM_MAX_CONCURRENCY)
                or os.environ.get(llm_config.ENV_LLM_MAX_CONCURRENCY_SW)
                or "4"
            )
            try:
                limit = max(1, int(raw))
            except ValueError:
                limit = 4
            _ollama_semaphore = threading.BoundedSemaphore(limit)
        return _ollama_semaphore


class OllamaLLMClient(LLMClient):
    """LLM client that talks to Ollama (or OpenAI-compatible) /v1/chat/completions."""

    def __init__(
        self,
        model: str = "llama3.1",
        *,
        base_url: str = "https://ollama.com",
        timeout: float = 600.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._model_num_ctx: Optional[int] = None

    def _ollama_auth_headers(self) -> dict[str, str]:
        """Return Authorization Bearer header for Ollama Cloud. Uses OLLAMA_API_KEY (or LLM_* overrides)."""
        key = (
            (os.environ.get("OLLAMA_API_KEY") or "")
            or (os.environ.get(llm_config.ENV_LLM_OLLAMA_API_KEY) or "")
            or (os.environ.get(llm_config.ENV_LLM_OLLAMA_API_KEY_SW) or "")
        ).strip()
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}"}

    def _fetch_model_num_ctx(self) -> int:
        """Fetch model's num_ctx from config known table, env, or Ollama /api/show. Cached. Fallback 16384."""
        if self._model_num_ctx is not None:
            return self._model_num_ctx
        ctx = llm_config.resolve_context_size_for_model(self.model)
        if ctx is not None:
            self._model_num_ctx = ctx
            logger.info(
                "LLM model %s: using known/context size %s", self.model, self._model_num_ctx
            )
            return self._model_num_ctx
        try:
            url = f"{self.base_url}/api/show"
            headers = self._ollama_auth_headers()
            with httpx.Client(timeout=min(30, self.timeout)) as client:
                resp = client.post(url, json={"model": self.model}, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "Ollama /api/show returned %s for model %s; using 16384",
                    resp.status_code,
                    self.model,
                )
                self._model_num_ctx = 16384
                return self._model_num_ctx
            data = resp.json()
            params_str = data.get("parameters") or ""
            match = re.search(r"num_ctx\s+(\d+)", params_str, re.IGNORECASE)
            if match:
                self._model_num_ctx = max(2048, int(match.group(1)))
                logger.info("Ollama model %s num_ctx=%s", self.model, self._model_num_ctx)
                return self._model_num_ctx
            for path in ("model_info", "details"):
                obj = data.get(path)
                if isinstance(obj, dict):
                    ctx_val = obj.get("num_ctx") or obj.get("context_length")
                    if ctx_val is not None:
                        self._model_num_ctx = max(2048, int(ctx_val))
                        return self._model_num_ctx
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            logger.warning(
                "Could not fetch Ollama model info for %s: %s; using 16384", self.model, e
            )
        self._model_num_ctx = 16384
        return self._model_num_ctx

    def get_max_context_tokens(self) -> int:
        """Return model's num_ctx (cached)."""
        return self._fetch_model_num_ctx()

    def _log_llm_server_error(
        self,
        status_code: int,
        response_text: Optional[str],
        response_headers: Optional[Any],
        attempt: int,
        reason: str = "",
    ) -> None:
        """Log full server error details (status, body, useful headers) at ERROR level."""
        body = (response_text or "")[:_MAX_LOG_BODY]
        if len(response_text or "") > _MAX_LOG_BODY:
            body += "... [truncated]"
        extra_headers = ""
        if response_headers is not None:
            useful = ["content-type", "retry-after", "x-request-id"]
            parts = []
            for name in useful:
                try:
                    v = response_headers.get(name)
                    if v is not None:
                        parts.append(f"{name}={v!r}")
                except (TypeError, AttributeError):
                    pass
            if parts:
                extra_headers = " headers=" + ", ".join(parts)
        reason_str = f" reason={reason}" if reason else ""
        logger.error(
            "LLM server error response: status=%s model=%s base_url=%s attempt=%s%s.%s Response body: %s",
            status_code,
            self.model,
            self.base_url,
            attempt,
            reason_str,
            extra_headers,
            body,
        )

    def _repair_json(self, s: str) -> str:
        """Attempt tolerant JSON repair for common LLM output issues."""
        s = re.sub(r",\s*([}\]])", r"\1", s)
        return s

    def _escape_unescaped_quotes(self, s: str) -> str:
        """Escape unescaped double quotes inside JSON string values.

        Uses a state machine that tracks whether we are inside a string.
        Quotes that appear mid-string and are not followed by a structural
        JSON character (:, ,, }, ]) are treated as unescaped inner quotes
        and get a backslash prepended.
        """
        result: list[str] = []
        in_string = False
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == "\\" and in_string:
                # Consume escape sequence as-is
                result.append(c)
                i += 1
                if i < n:
                    result.append(s[i])
                    i += 1
                continue
            if c == '"':
                if not in_string:
                    in_string = True
                    result.append(c)
                else:
                    # Determine if this closes the string by checking the
                    # next non-whitespace character.
                    j = i + 1
                    while j < n and s[j] in " \t\n\r":
                        j += 1
                    if j >= n or s[j] in ":,}]":
                        in_string = False
                        result.append(c)
                    else:
                        # Mid-string quote: escape it
                        result.append("\\")
                        result.append(c)
            else:
                result.append(c)
            i += 1
        return "".join(result)

    def _is_truncated_json(self, s: str) -> bool:
        """Return True if ``s`` appears to be truncated (unclosed brackets or string)."""
        t = s.strip()
        if t.count("{") != t.count("}"):
            return True
        if t.count("[") != t.count("]"):
            return True
        # Check for unclosed string (odd number of unescaped quotes)
        in_str = False
        i = 0
        while i < len(t):
            c = t[i]
            if c == "\\" and in_str:
                i += 2
                continue
            if c == '"':
                in_str = not in_str
            i += 1
        return in_str

    def _parse_with_json_repair(self, s: str) -> Optional[Dict[str, Any]]:
        """Parse broken LLM JSON (e.g. unescaped quotes inside strings).

        Skips repair entirely for truncated JSON (unclosed brackets/strings)
        so that truncated responses surface as parse errors rather than being
        silently completed with invented content.

        For syntactically broken but complete JSON (e.g. unescaped inner
        quotes), first tries the built-in ``_escape_unescaped_quotes``
        heuristic, then falls back to the optional *json-repair* library.
        """
        if not (s or "").strip():
            return None
        # Do not repair truncated JSON — return None so the caller raises.
        if self._is_truncated_json(s.strip()):
            return None
        # Built-in heuristic: escape unescaped inner quotes
        try:
            repaired = self._escape_unescaped_quotes(s.strip())
            out = json.loads(repaired)
            if isinstance(out, dict) and out:
                return out
        except Exception:
            pass
        # Optional external library
        try:
            from json_repair import loads as json_repair_loads

            out = json_repair_loads(s.strip())
            return out if isinstance(out, dict) and out else None
        except ImportError:
            return None
        except Exception:
            return None

    def _strip_json_noise(self, s: str) -> str:
        """Drop transport artifacts (BOM/replacement chars/control bytes) from JSON-ish text."""
        if not s:
            return s
        s = s.replace("\ufeff", "")
        return _JSON_NOISE_RE.sub("", s)

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """Extract a single JSON object from model output. Raises LLMJsonParseError on failure."""
        text = self._strip_json_noise(text)
        if "---DRAFT---" in text:
            parts = text.split("---DRAFT---", 1)
            if len(parts) == 2 and parts[1].strip():
                return {"content": parts[1].strip()}
        json_block_match = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
        if json_block_match:
            text = json_block_match.group(1).strip()
        else:
            fenced_match = re.search(
                r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.DOTALL | re.IGNORECASE
            )
            if fenced_match:
                block_content = fenced_match.group(1).strip()
                if block_content.lstrip().startswith(("{", "[")):
                    text = block_content
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        repaired = self._repair_json(text)
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            pass
        obj_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if obj_match:
            raw = obj_match.group(0)
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                try:
                    return json.loads(self._repair_json(raw))
                except (json.JSONDecodeError, ValueError):
                    pass
        stripped = text.strip()
        for pattern in (
            r"^(?:Here(?:'s| is) (?:the )?JSON:?)\s*",
            r"^(?:The (?:response|output|result) is:?)\s*",
            r"^(?:JSON:?)\s*",
            r"^\s*```(?:json)?\s*",
            r"\s*```\s*$",
        ):
            stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE).strip()
        if stripped != text.strip():
            obj_match2 = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
            if obj_match2:
                try:
                    return json.loads(obj_match2.group(0))
                except (json.JSONDecodeError, ValueError):
                    pass
        for block_match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
            block = block_match.group(1).strip()
            if not block:
                continue
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict) and _EXPECTED_KEYS & set(parsed.keys()):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                try:
                    parsed = json.loads(self._repair_json(block))
                    if isinstance(parsed, dict) and _EXPECTED_KEYS & set(parsed.keys()):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    jr = self._parse_with_json_repair(block)
                    if jr and _EXPECTED_KEYS & set(jr.keys()):
                        logger.info(
                            "LLM JSON parsed via json-repair (fenced block, keys=%s)",
                            list(jr.keys())[:8],
                        )
                        return jr
                    continue
        # Last resort: json-repair on full text or outermost {...} (fixes unescaped " in string values)
        candidates: list[str] = []
        t = text.strip()
        if t:
            candidates.append(t)
        lo, hi = t.find("{"), t.rfind("}")
        if lo >= 0 and hi > lo:
            brace_slice = t[lo : hi + 1]
            if brace_slice not in candidates:
                candidates.append(brace_slice)
        for cand in candidates:
            jr_dict = self._parse_with_json_repair(cand)
            if jr_dict is not None:
                logger.info(
                    "LLM JSON parsed via json-repair fallback (%d top-level keys)", len(jr_dict)
                )
                return jr_dict
        logger.error(
            "LLM JSON parse failed. model=%s base_url=%s. Raw content (truncated): %s",
            self.model,
            self.base_url,
            text[:_MAX_LOG_BODY] + ("... [truncated]" if len(text) > _MAX_LOG_BODY else ""),
        )
        raise LLMJsonParseError(
            "Could not parse structured JSON from LLM response. Model returned invalid or non-JSON output. "
            f"Response preview: {text[:500]!r}...",
            error_kind="json_parse",
            response_preview=text[:500],
        )

    def _should_enable_thinking(self) -> bool:
        """Enable thinking mode for all models by default; disable via LLM_ENABLE_THINKING=false."""
        env_val = (
            os.environ.get(llm_config.ENV_LLM_ENABLE_THINKING)
            or os.environ.get(llm_config.ENV_LLM_ENABLE_THINKING_SW)
            or ""
        ).lower()
        return env_val != "false"

    def _parse_response_content(self, data: dict) -> str:
        """Extract content or tool_calls from OpenAI-compatible response.

        Returns content string for normal replies, or a JSON-serialized
        ``{"__tool_calls__": [...]}`` dict string when the model invokes tools.
        Raises LLMTruncatedError if finish_reason=length.
        """
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise LLMPermanentError(
                "Unexpected response format from LLM: missing or invalid 'choices'"
            )
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMPermanentError("Unexpected response format from LLM: invalid choice object")
        finish_reason = first.get("finish_reason", "")
        msg = first.get("message")
        if not msg or not isinstance(msg, dict):
            raise LLMPermanentError(
                "Unexpected response format from LLM: missing or invalid 'message'"
            )
        # Tool calls take priority — model is invoking a function rather than replying with text.
        tool_calls = msg.get("tool_calls")
        if tool_calls or finish_reason == "tool_calls":
            if not tool_calls:
                raise LLMPermanentError(
                    "LLM returned finish_reason=tool_calls but no tool_calls in message"
                )
            logger.info("LLM returned %d tool call(s)", len(tool_calls))
            return json.dumps({"__tool_calls__": tool_calls})
        if finish_reason == "length":
            partial_content = msg.get("content", "")
            partial_content = str(partial_content) if partial_content else ""
            if not partial_content.strip():
                logger.warning(
                    "LLM returned empty response (finish_reason=length). Treating as transient error for retry."
                )
                raise LLMTemporaryError(
                    "Empty response (finish_reason=length); treating as transient for retry",
                )
            logger.warning(
                "LLM response truncated (finish_reason=length). Partial content: %d chars",
                len(partial_content),
            )
            raise LLMTruncatedError(
                "Response truncated due to token limit (finish_reason=length)",
                partial_content=partial_content,
                finish_reason=finish_reason,
            )
        content = msg.get("content")
        if content is None:
            raise LLMPermanentError("Unexpected response format from LLM: missing 'content'")
        content_str = str(content)
        if not content_str.strip():
            logger.warning(
                "LLM returned empty response (200). Treating as transient error for retry."
            )
            raise LLMTemporaryError(
                "Empty response from LLM; treating as transient for retry",
            )
        return content_str

    def _ollama_post(
        self,
        payload: dict,
        max_retries: int,
        initial_backoff: float,
        backoff_max: float,
        sem: threading.BoundedSemaphore,
    ) -> str:
        """POST to /v1/chat/completions with SSE streaming; return raw content. Raises LLM* on non-200 or malformed."""
        url = f"{self.base_url}/v1/chat/completions"
        last_error: Optional[Exception] = None
        headers = self._ollama_auth_headers()
        stream_payload = {**payload, "stream": True}
        for attempt in range(max_retries + 1):
            try:
                with sem:
                    logger.info(
                        "Waiting for LLM response (timeout=%ss, attempt %d/%d)...",
                        int(self.timeout),
                        attempt + 1,
                        max_retries + 1,
                    )
                    t0 = time.monotonic()
                    with httpx.Client(timeout=self.timeout) as client:
                        with client.stream(
                            "POST", url, json=stream_payload, headers=headers
                        ) as response:
                            status = response.status_code
                            if status != 200:
                                response.read()
                            if status == 200:
                                content_parts: list[str] = []
                                finish_reason: Optional[str] = None
                                tool_call_buffers: dict[int, dict] = {}
                                for line in response.iter_lines():
                                    if not line or not line.startswith("data:"):
                                        continue
                                    chunk_data = line[5:].lstrip()
                                    if chunk_data.strip() == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(chunk_data)
                                    except json.JSONDecodeError as e:
                                        raise LLMTemporaryError(
                                            f"Malformed SSE chunk (JSON decode failed): {chunk_data[:200]!r}"
                                        ) from e
                                    choices = chunk.get("choices") or []
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        piece = delta.get("content")
                                        if piece:
                                            content_parts.append(piece)
                                        for tc in delta.get("tool_calls") or []:
                                            idx = tc.get("index", 0)
                                            if idx not in tool_call_buffers:
                                                tool_call_buffers[idx] = {
                                                    "id": "",
                                                    "type": "function",
                                                    "function": {"name": "", "arguments": ""},
                                                }
                                            buf = tool_call_buffers[idx]
                                            if tc.get("id"):
                                                buf["id"] = tc["id"]
                                            if tc.get("type"):
                                                buf["type"] = tc["type"]
                                            fn = tc.get("function") or {}
                                            if fn.get("name"):
                                                buf["function"]["name"] = fn["name"]
                                            if fn.get("arguments"):
                                                buf["function"]["arguments"] += fn["arguments"]
                                        fr = choices[0].get("finish_reason")
                                        if fr:
                                            finish_reason = fr
                                elapsed = time.monotonic() - t0
                                logger.info("LLM streaming response complete in %.1fs", elapsed)
                                tool_calls = None
                                if tool_call_buffers:
                                    tool_calls = []
                                    for idx in sorted(tool_call_buffers.keys()):
                                        buf = tool_call_buffers[idx]
                                        try:
                                            buf["function"]["arguments"] = json.loads(
                                                buf["function"]["arguments"]
                                            )
                                        except (json.JSONDecodeError, ValueError):
                                            pass
                                        tool_calls.append(buf)
                                synthetic = {
                                    "choices": [
                                        {
                                            "message": {
                                                "content": "".join(content_parts),
                                                "tool_calls": tool_calls,
                                            },
                                            "finish_reason": finish_reason or "stop",
                                        }
                                    ]
                                }
                                return self._parse_response_content(synthetic)
                            if status == 429:
                                last_error = LLMRateLimitError(
                                    f"LLM rate limited (429) after {attempt + 1} attempt(s)",
                                    status_code=429,
                                )
                                if attempt < max_retries:
                                    wait = _exponential_retry_delay(
                                        attempt, initial_backoff, backoff_max
                                    )
                                    logger.warning(
                                        "LLM 429 (attempt %d/%d). Retrying in %.1fs",
                                        attempt + 1,
                                        max_retries + 1,
                                        wait,
                                    )
                                    time.sleep(wait)
                                    continue
                                self._log_llm_server_error(
                                    429,
                                    response.text,
                                    response.headers,
                                    attempt + 1,
                                    reason="rate limited",
                                )
                                raise last_error
                            if 500 <= status < 600:
                                hint = ""
                                if (
                                    "ollama.com" in self.base_url
                                    and "qwen3.5" in self.model.lower()
                                ):
                                    hint = " If using Ollama Cloud with qwen3.5, try LLM_ENABLE_THINKING=false."
                                last_error = LLMTemporaryError(
                                    f"LLM server error {status} after {attempt + 1} attempt(s): {response.text[:200]}.{hint}",
                                    status_code=status,
                                )
                                if attempt < max_retries:
                                    wait = _exponential_retry_delay(
                                        attempt, initial_backoff, backoff_max
                                    )
                                    time.sleep(wait)
                                    continue
                                self._log_llm_server_error(
                                    status,
                                    response.text,
                                    response.headers,
                                    attempt + 1,
                                    reason="server error",
                                )
                                raise last_error
                            if 400 <= status < 500:
                                err_text = response.text[:500]
                                self._log_llm_server_error(
                                    status,
                                    response.text,
                                    response.headers,
                                    attempt + 1,
                                    reason="client error",
                                )
                                if status == 404 and (
                                    "not found" in err_text.lower() or "model" in err_text.lower()
                                ):
                                    raise LLMPermanentError(
                                        f"LLM model not found (404). API at {self.base_url} does not have model '{self.model}'. Original: {err_text[:200]}",
                                        status_code=status,
                                    )
                                if status == 401:
                                    auth_hint = (
                                        " Set OLLAMA_API_KEY (or LLM_OLLAMA_API_KEY / SW_LLM_OLLAMA_API_KEY) for Ollama Cloud."
                                        if not headers
                                        else " Check that the key is valid and not expired."
                                    )
                                    raise LLMPermanentError(
                                        f"LLM unauthorized (401): {err_text[:200]}.{auth_hint}",
                                        status_code=status,
                                    )
                                raise LLMPermanentError(
                                    f"LLM client error {status}: {err_text}", status_code=status
                                )
                            self._log_llm_server_error(
                                status,
                                response.text,
                                response.headers,
                                attempt + 1,
                                reason="unexpected status",
                            )
                            raise LLMPermanentError(
                                f"Unexpected LLM response status {status}: {response.text[:200]}",
                                status_code=status,
                            )
            except (LLMPermanentError, LLMRateLimitError, LLMTruncatedError):
                raise
            except LLMTemporaryError as e:
                last_error = e
                if attempt < max_retries:
                    wait = _exponential_retry_delay(attempt, initial_backoff, backoff_max)
                    logger.warning(
                        "LLM temporary error (attempt %d/%d): %s. Retrying in %.1fs",
                        attempt + 1,
                        max_retries + 1,
                        e,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                raise last_error
            except httpx.HTTPStatusError as e:
                resp = e.response
                status = resp.status_code if resp else None
                if resp is not None:
                    self._log_llm_server_error(
                        resp.status_code,
                        resp.text,
                        resp.headers,
                        attempt + 1,
                        reason="HTTPStatusError",
                    )
                if status == 429:
                    last_error = LLMRateLimitError(str(e), status_code=429, cause=e)
                    if attempt < max_retries:
                        wait = _exponential_retry_delay(attempt, initial_backoff, backoff_max)
                        time.sleep(wait)
                        continue
                    raise last_error
                if status and 500 <= status < 600:
                    last_error = LLMTemporaryError(str(e), status_code=status, cause=e)
                    if attempt < max_retries:
                        wait = _exponential_retry_delay(attempt, initial_backoff, backoff_max)
                        time.sleep(wait)
                        continue
                    raise last_error
                if status and 400 <= status < 500:
                    raise LLMPermanentError(str(e), status_code=status or 0, cause=e)
                raise LLMPermanentError(str(e), status_code=status or 0, cause=e)
            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.WriteError,
                httpx.ReadError,
                httpx.ProxyError,
            ) as e:
                hint = ""
                if "name resolution" in str(e).lower() or "temporary failure" in str(e).lower():
                    hint = (
                        f" Cannot reach LLM at {self.base_url}. "
                        "If running in Docker, set LLM_BASE_URL to a reachable endpoint "
                        "(e.g. http://host.docker.internal:11434 for local Ollama, or ensure the container has DNS/outbound access)."
                    )
                elif isinstance(e, httpx.RemoteProtocolError):
                    hint = " (server closed connection; retrying with exponential backoff)"
                last_error = LLMTemporaryError(
                    f"LLM connection/transport error ({type(e).__name__}): {e}.{hint}",
                    cause=e,
                )
                if attempt < max_retries:
                    wait = _exponential_retry_delay(attempt, initial_backoff, backoff_max)
                    logger.warning(
                        "LLM transport error (attempt %d/%d): %s. Retrying in %.1fs",
                        attempt + 1,
                        max_retries + 1,
                        type(e).__name__,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                timeout_hint = ""
                if isinstance(e, httpx.ReadTimeout):
                    timeout_hint = (
                        f" Per-request timeout is {int(self.timeout)}s; increase LLM_TIMEOUT or SW_LLM_TIMEOUT "
                        "(e.g. 900–1200) for slow cloud models or very long prompts."
                    )
                logger.error(
                    "LLM connection/timeout failed after all retries. model=%s base_url=%s attempt=%s error=%s%s%s",
                    self.model,
                    self.base_url,
                    attempt + 1,
                    type(e).__name__,
                    hint,
                    timeout_hint,
                )
                raise last_error
        if last_error:
            raise last_error
        raise LLMTemporaryError("LLM request failed after all retries")

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run the model with JSON mode and return a decoded dict."""
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        sem = _get_ollama_semaphore()
        logger.info("LLM request: provider=ollama model=%s base_url=%s", self.model, self.base_url)
        system_message = system_prompt or (
            "You are a strict JSON generator. Respond with a single valid JSON object only, "
            "no explanatory text, no Markdown, no code fences. "
            "If you use a code block, put only the JSON object inside it with no surrounding text."
        )
        max_tokens = kwargs.pop("max_tokens", None)
        if max_tokens is None:
            env_max = os.environ.get(llm_config.ENV_LLM_MAX_TOKENS) or os.environ.get(
                llm_config.ENV_LLM_MAX_TOKENS_SW
            )
            if env_max:
                try:
                    max_tokens = min(int(env_max), DEFAULT_MAX_OUTPUT_TOKENS)
                except ValueError:
                    max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
            else:
                max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
        max_tokens = min(max_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        tools = kwargs.pop("tools", None)
        payload: dict = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
            "think": self._should_enable_thinking(),
        }
        if tools:
            payload["tools"] = tools
        else:
            payload["response_format"] = {"type": "json_object"}
        try:
            content = self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)
            # Defensive: retry on empty content (e.g. thinking model or API quirk)
            for empty_attempt in range(2):
                if (content or "").strip():
                    break
                logger.warning(
                    "Empty JSON response (attempt %d/2). Retrying in %.1fs...",
                    empty_attempt + 1,
                    backoff_base + random.uniform(0, 1),
                )
                time.sleep(backoff_base + random.uniform(0, 1))
                content = self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)
            if not (content or "").strip():
                raise LLMTemporaryError(
                    "Empty response from LLM after retries; try again or set LLM_ENABLE_THINKING=false for qwen3.5."
                )
            return self._extract_json(content)
        except LLMTruncatedError as e:
            return self._complete_json_with_continuation(
                initial_partial=e.partial_content,
                prompt=prompt,
                system_message=system_message,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=max_retries,
                backoff_base=backoff_base,
                backoff_max=backoff_max,
                sem=sem,
            )
        except LLMJsonParseError:
            # If content starts with '{' but is unparseable, the server likely cut off the
            # response before the JSON was complete (finish_reason="stop" despite truncation).
            # Attempt continuation to recover the rest of the JSON.
            stripped = (content or "").strip()
            if stripped.startswith(("{", "[")):
                logger.warning(
                    "JSON parse failed on content starting with '%s'; treating as implicit truncation and attempting continuation.",
                    stripped[0],
                )
                return self._complete_json_with_continuation(
                    initial_partial=content,
                    prompt=prompt,
                    system_message=system_message,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=max_retries,
                    backoff_base=backoff_base,
                    backoff_max=backoff_max,
                    sem=sem,
                )
            raise

    def _merge_continuation(self, accumulated: str, next_chunk: str, min_overlap: int = 10) -> str:
        """Append next_chunk to accumulated, stripping overlap at boundary."""
        if not next_chunk:
            return accumulated
        if not accumulated:
            return next_chunk
        max_check = min(len(accumulated), len(next_chunk), 500)
        for overlap_len in range(max_check, min_overlap - 1, -1):
            if accumulated[-overlap_len:] == next_chunk[:overlap_len]:
                return accumulated + next_chunk[overlap_len:]
        return accumulated + next_chunk

    def _continuation_user_message(self, partial_content: str) -> str:
        """Prompt for the model to continue from where it left off."""
        last_chars = partial_content[-CONTINUATION_CONTEXT_CHARS:] if partial_content else ""
        last_escaped = last_chars.replace("\n", "\\n")
        return (
            f"Please continue exactly from where you left off. "
            f"Your previous response ended with: '{last_escaped}'. "
            f"Continue the response seamlessly without repeating what you already wrote."
        )

    def _complete_json_with_continuation(
        self,
        initial_partial: str,
        prompt: str,
        system_message: str,
        temperature: float,
        max_tokens: int,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        sem: threading.BoundedSemaphore,
    ) -> Dict[str, Any]:
        """On truncation: continue via multi-turn conversation, then parse JSON (same as SE team)."""
        accumulated = initial_partial
        for cycle in range(MAX_CONTINUATION_CYCLES):
            logger.info(
                "Continuation cycle %d/%d (accumulated %d chars)",
                cycle + 1,
                MAX_CONTINUATION_CYCLES,
                len(accumulated),
            )
            messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": accumulated},
                {"role": "user", "content": self._continuation_user_message(accumulated)},
            ]
            payload = {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": messages,
                "think": self._should_enable_thinking(),
            }

            try:
                next_content = self._ollama_post(
                    payload, max_retries, backoff_base, backoff_max, sem
                )
                accumulated = self._merge_continuation(accumulated, next_content)
                return self._extract_json(accumulated)
            except LLMTruncatedError as e2:
                accumulated = self._merge_continuation(accumulated, e2.partial_content)
        logger.warning(
            "Continuation exhausted after %d cycles (%d chars). Re-raising truncation.",
            MAX_CONTINUATION_CYCLES,
            len(accumulated),
        )
        raise LLMTruncatedError(
            f"Response still truncated after {MAX_CONTINUATION_CYCLES} continuation cycles",
            partial_content=accumulated,
            finish_reason="length",
        )

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
    ) -> str:
        """Return raw text from the model (no JSON mode). Pass tools for function/tool calling."""
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        sem = _get_ollama_semaphore()
        logger.info(
            "LLM request (text): provider=ollama model=%s base_url=%s", self.model, self.base_url
        )
        env_max = os.environ.get(llm_config.ENV_LLM_MAX_TOKENS) or os.environ.get(
            llm_config.ENV_LLM_MAX_TOKENS_SW
        )
        if max_tokens is None:
            if env_max:
                try:
                    max_tokens = min(int(env_max), DEFAULT_MAX_OUTPUT_TOKENS)
                except ValueError:
                    max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
            else:
                max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
        max_tokens = min(max_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        payload: dict = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "think": self._should_enable_thinking(),
        }
        if system_prompt:
            payload["messages"] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        if tools:
            payload["tools"] = tools
        try:
            return self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)
        except LLMTruncatedError as e:
            return self._complete_text_with_continuation(
                initial_partial=e.partial_content,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=max_retries,
                backoff_base=backoff_base,
                backoff_max=backoff_max,
                sem=sem,
            )

    def _complete_text_with_continuation(
        self,
        initial_partial: str,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        sem: threading.BoundedSemaphore,
    ) -> str:
        """On truncation: continue via multi-turn conversation, return merged text."""
        accumulated = initial_partial
        system_message = system_prompt or ""
        for cycle in range(MAX_CONTINUATION_CYCLES):
            logger.info(
                "Continuation cycle %d/%d (text, accumulated %d chars)",
                cycle + 1,
                MAX_CONTINUATION_CYCLES,
                len(accumulated),
            )
            messages: list = []
            if system_message:
                messages.append({"role": "system", "content": system_message})
            messages.append({"role": "user", "content": prompt})
            messages.append({"role": "assistant", "content": accumulated})
            messages.append(
                {"role": "user", "content": self._continuation_user_message(accumulated)}
            )
            payload = {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
                "think": self._should_enable_thinking(),
            }
            try:
                next_content = self._ollama_post(
                    payload, max_retries, backoff_base, backoff_max, sem
                )
                accumulated = self._merge_continuation(accumulated, next_content)
                return accumulated
            except LLMTruncatedError as e2:
                accumulated = self._merge_continuation(accumulated, e2.partial_content)
        logger.warning(
            "Continuation exhausted after %d cycles (text, %d chars). Re-raising truncation.",
            MAX_CONTINUATION_CYCLES,
            len(accumulated),
        )
        raise LLMTruncatedError(
            f"Response still truncated after {MAX_CONTINUATION_CYCLES} continuation cycles",
            partial_content=accumulated,
            finish_reason="length",
        )
