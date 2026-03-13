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

# Expected keys for "try every code block" fallback
_EXPECTED_KEYS = frozenset({
    "files", "summary", "code", "overview", "issues", "approved", "components",
    "architecture_document", "diagrams", "decisions",
    "tasks", "execution_order",
    "bugs_found", "integration_tests", "unit_tests", "readme_content",
})


def _parse_retry_config() -> tuple[int, float, float]:
    """Parse retry-related environment variables. Returns (max_retries, backoff_base, backoff_max)."""
    raw_retries = os.environ.get(llm_config.ENV_LLM_MAX_RETRIES) or os.environ.get(llm_config.ENV_LLM_MAX_RETRIES_SW) or "4"
    raw_base = os.environ.get(llm_config.ENV_LLM_BACKOFF_BASE) or os.environ.get(llm_config.ENV_LLM_BACKOFF_BASE_SW) or "2"
    raw_max = os.environ.get(llm_config.ENV_LLM_BACKOFF_MAX) or os.environ.get(llm_config.ENV_LLM_BACKOFF_MAX_SW) or "60"
    try:
        max_retries = int(raw_retries)
    except ValueError:
        max_retries = 4
    try:
        backoff_base = float(raw_base)
    except ValueError:
        backoff_base = 2.0
    try:
        backoff_max = float(raw_max)
    except ValueError:
        backoff_max = 60.0
    return max_retries, backoff_base, backoff_max


_ollama_semaphore: Optional[threading.BoundedSemaphore] = None
_semaphore_lock = threading.Lock()


def _get_ollama_semaphore() -> threading.BoundedSemaphore:
    """Lazily create the global Ollama concurrency semaphore."""
    global _ollama_semaphore
    with _semaphore_lock:
        if _ollama_semaphore is None:
            raw = os.environ.get(llm_config.ENV_LLM_MAX_CONCURRENCY) or os.environ.get(llm_config.ENV_LLM_MAX_CONCURRENCY_SW) or "4"
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
        timeout: float = 120.0,
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
            logger.info("LLM model %s: using known/context size %s", self.model, self._model_num_ctx)
            return self._model_num_ctx
        try:
            url = f"{self.base_url}/api/show"
            headers = self._ollama_auth_headers()
            with httpx.Client(timeout=min(30, self.timeout)) as client:
                resp = client.post(url, json={"model": self.model}, headers=headers)
            if resp.status_code != 200:
                logger.warning("Ollama /api/show returned %s for model %s; using 16384", resp.status_code, self.model)
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
            logger.warning("Could not fetch Ollama model info for %s: %s; using 16384", self.model, e)
        self._model_num_ctx = 16384
        return self._model_num_ctx

    def get_max_context_tokens(self) -> int:
        """Return model's num_ctx (cached)."""
        return self._fetch_model_num_ctx()

    def _repair_json(self, s: str) -> str:
        """Attempt tolerant JSON repair for common LLM output issues."""
        s = re.sub(r",\s*([}\]])", r"\1", s)
        return s

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """Extract a single JSON object from model output. Raises LLMJsonParseError on failure."""
        if "---DRAFT---" in text:
            parts = text.split("---DRAFT---", 1)
            if len(parts) == 2 and parts[1].strip():
                return {"content": parts[1].strip()}
        json_block_match = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
        if json_block_match:
            text = json_block_match.group(1).strip()
        else:
            fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.DOTALL | re.IGNORECASE)
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
                    continue
        raise LLMJsonParseError(
            "Could not parse structured JSON from LLM response. Model returned invalid or non-JSON output. "
            f"Response preview: {text[:500]!r}...",
            error_kind="json_parse",
            response_preview=text[:500],
        )

    def _should_enable_thinking(self) -> bool:
        """Enable thinking mode for qwen3.5 models by default; overridable via env."""
        env_val = (os.environ.get(llm_config.ENV_LLM_ENABLE_THINKING) or os.environ.get(llm_config.ENV_LLM_ENABLE_THINKING_SW) or "").lower()
        if env_val == "false":
            return False
        if env_val == "true":
            return "qwen3.5" in self.model.lower()
        return "qwen3.5" in self.model.lower()

    def _parse_response_content(self, data: dict) -> str:
        """Extract content from OpenAI-compatible response. Raises LLMTruncatedError if finish_reason=length."""
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise LLMPermanentError("Unexpected response format from LLM: missing or invalid 'choices'")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMPermanentError("Unexpected response format from LLM: invalid choice object")
        finish_reason = first.get("finish_reason", "")
        if finish_reason == "length":
            msg = first.get("message", {})
            partial_content = msg.get("content", "") if isinstance(msg, dict) else ""
            logger.warning("LLM response truncated (finish_reason=length). Partial content: %d chars", len(partial_content))
            raise LLMTruncatedError(
                "Response truncated due to token limit (finish_reason=length)",
                partial_content=str(partial_content) if partial_content else "",
                finish_reason=finish_reason,
            )
        msg = first.get("message")
        if not msg or not isinstance(msg, dict):
            raise LLMPermanentError("Unexpected response format from LLM: missing or invalid 'message'")
        content = msg.get("content")
        if content is None:
            raise LLMPermanentError("Unexpected response format from LLM: missing 'content'")
        return str(content)

    def _ollama_post(self, payload: dict, max_retries: int, backoff_base: float, backoff_max: float, sem: threading.BoundedSemaphore) -> str:
        """POST to /v1/chat/completions; return raw content. Raises LLM* on non-200 or malformed."""
        url = f"{self.base_url}/v1/chat/completions"
        last_error: Optional[Exception] = None
        headers = self._ollama_auth_headers()
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
                        response = client.post(url, json=payload, headers=headers)
                    elapsed = time.monotonic() - t0
                    status = response.status_code
                    if status == 200:
                        logger.info("LLM response received in %.1fs", elapsed)
                        try:
                            data = response.json()
                        except json.JSONDecodeError as e:
                            raise LLMPermanentError(f"Malformed LLM response (invalid JSON): {e}") from e
                        return self._parse_response_content(data)
                    if status == 429:
                        last_error = LLMRateLimitError(
                            f"LLM rate limited (429) after {attempt + 1} attempt(s)",
                            status_code=429,
                        )
                        if attempt < max_retries:
                            wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                            logger.warning("LLM 429 (attempt %d/%d). Retrying in %.1fs", attempt + 1, max_retries + 1, wait)
                            time.sleep(wait)
                            continue
                        raise last_error
                    if 500 <= status < 600:
                        last_error = LLMTemporaryError(
                            f"LLM server error {status} after {attempt + 1} attempt(s): {response.text[:200]}",
                            status_code=status,
                        )
                        if attempt < max_retries:
                            wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                            time.sleep(wait)
                            continue
                        raise last_error
                    if 400 <= status < 500:
                        err_text = response.text[:500]
                        if status == 404 and ("not found" in err_text.lower() or "model" in err_text.lower()):
                            raise LLMPermanentError(
                                f"LLM model not found (404). API at {self.base_url} does not have model '{self.model}'. Original: {err_text[:200]}",
                                status_code=status,
                            )
                        if status == 401:
                            auth_hint = (
                                " Set OLLAMA_API_KEY (or LLM_OLLAMA_API_KEY / SW_LLM_OLLAMA_API_KEY) for Ollama Cloud."
                                if not headers else " Check that the key is valid and not expired."
                            )
                            raise LLMPermanentError(
                                f"LLM unauthorized (401): {err_text[:200]}.{auth_hint}",
                                status_code=status,
                            )
                        raise LLMPermanentError(f"LLM client error {status}: {err_text}", status_code=status)
                    raise LLMPermanentError(f"Unexpected LLM response status {status}: {response.text[:200]}", status_code=status)
            except (LLMPermanentError, LLMRateLimitError, LLMTemporaryError, LLMTruncatedError):
                raise
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response else None
                if status == 429:
                    last_error = LLMRateLimitError(str(e), status_code=429, cause=e)
                    if attempt < max_retries:
                        wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                        time.sleep(wait)
                        continue
                    raise last_error
                if status and 500 <= status < 600:
                    last_error = LLMTemporaryError(str(e), status_code=status, cause=e)
                    if attempt < max_retries:
                        wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                        time.sleep(wait)
                        continue
                    raise last_error
                if status and 400 <= status < 500:
                    raise LLMPermanentError(str(e), status_code=status or 0, cause=e)
                raise LLMPermanentError(str(e), status_code=status or 0, cause=e)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout) as e:
                hint = ""
                if "name resolution" in str(e).lower() or "temporary failure" in str(e).lower():
                    hint = (
                        f" Cannot reach LLM at {self.base_url}. "
                        "If running in Docker, set LLM_BASE_URL to a reachable endpoint "
                        "(e.g. http://host.docker.internal:11434 for local Ollama, or ensure the container has DNS/outbound access)."
                    )
                last_error = LLMTemporaryError(
                    f"LLM connection/timeout error: {e}.{hint}",
                    cause=e,
                )
                if attempt < max_retries:
                    wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                    logger.warning("LLM connection error (attempt %d/%d): %s. Retrying in %.1fs", attempt + 1, max_retries + 1, type(e).__name__, wait)
                    time.sleep(wait)
                    continue
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
            env_max = os.environ.get(llm_config.ENV_LLM_MAX_TOKENS) or os.environ.get(llm_config.ENV_LLM_MAX_TOKENS_SW)
            if env_max:
                try:
                    max_tokens = min(int(env_max), DEFAULT_MAX_OUTPUT_TOKENS)
                except ValueError:
                    max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
            else:
                max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
        max_tokens = min(max_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
        }
        if self._should_enable_thinking():
            payload["think"] = True
        try:
            content = self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)
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
            }
            if self._should_enable_thinking():
                payload["think"] = True
            try:
                next_content = self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)
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
    ) -> str:
        """Return raw text from the model (no JSON mode)."""
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        sem = _get_ollama_semaphore()
        logger.info("LLM request (text): provider=ollama model=%s base_url=%s", self.model, self.base_url)
        env_max = os.environ.get(llm_config.ENV_LLM_MAX_TOKENS) or os.environ.get(llm_config.ENV_LLM_MAX_TOKENS_SW)
        if max_tokens is None:
            if env_max:
                try:
                    max_tokens = min(int(env_max), DEFAULT_MAX_OUTPUT_TOKENS)
                except ValueError:
                    max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
            else:
                max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
        max_tokens = min(max_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            payload["messages"] = [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
        if self._should_enable_thinking():
            payload["think"] = True
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
            messages.append({"role": "user", "content": self._continuation_user_message(accumulated)})
            payload = {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if self._should_enable_thinking():
                payload["think"] = True
            try:
                next_content = self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)
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
