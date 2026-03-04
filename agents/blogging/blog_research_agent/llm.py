from __future__ import annotations

import logging
import os
import random
import time
from abc import ABC, abstractmethod
import json
import re
from threading import Lock
from typing import Any, Dict, Optional

import httpx

from shared.errors import (
    LLMError,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMUnreachableError,
)

logger = logging.getLogger(__name__)

# Environment variables for LLM configuration
ENV_BLOG_LLM_MODEL = "BLOG_LLM_MODEL"
ENV_BLOG_LLM_BASE_URL = "BLOG_LLM_BASE_URL"
ENV_BLOG_LLM_TIMEOUT = "BLOG_LLM_TIMEOUT"
ENV_BLOG_LLM_MAX_RETRIES = "BLOG_LLM_MAX_RETRIES"
ENV_LLM_ENABLE_THINKING = "SW_LLM_ENABLE_THINKING"  # shared with SW team; "true"/"false"

# Default configuration
DEFAULT_MODEL = "qwen3.5:397b-cloud"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 1800.0
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0


def get_blog_llm_config() -> Dict[str, Any]:
    """Get LLM configuration from environment variables with defaults."""
    return {
        "model": os.environ.get(ENV_BLOG_LLM_MODEL, DEFAULT_MODEL),
        "base_url": os.environ.get(ENV_BLOG_LLM_BASE_URL, DEFAULT_BASE_URL),
        "timeout": float(os.environ.get(ENV_BLOG_LLM_TIMEOUT, DEFAULT_TIMEOUT)),
        "max_retries": int(os.environ.get(ENV_BLOG_LLM_MAX_RETRIES, DEFAULT_MAX_RETRIES)),
    }


class LLMClient(ABC):
    """
    Minimal abstraction around an LLM client.

    The concrete implementation should adapt your Strands runtime's
    LLM interface to this method.
    """

    def __init__(self) -> None:
        self._request_count = 0
        self._request_count_lock = Lock()

    @property
    def request_count(self) -> int:
        """Total number of LLM requests made through this client instance."""
        return self._request_count

    def _increment_request_count(self) -> None:
        with self._request_count_lock:
            self._request_count += 1

    @abstractmethod
    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Run the model with the given prompt and return a JSON-decoded dict.

        Implementations are responsible for:
        - adding any system messages
        - choosing the underlying model
        - parsing the model output as JSON and returning it

        Preconditions:
            - prompt is a non-empty string.
            - 0.0 <= temperature <= 2.0 (implementation-defined range).
        Postconditions:
            - Returns a (possibly empty) dict; never None.
        """


class DummyLLMClient(LLMClient):
    """
    A no-op implementation useful for tests and environments without an LLM.

    It returns very rough, heuristic outputs instead of calling a real model.
    This is NOT meant for production use but is handy to keep the agent runnable.
    """

    def __init__(self) -> None:
        super().__init__()

    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Preconditions: prompt non-empty; 0.0 <= temperature <= 2.0.
        Postconditions: Returns dict; never None. Heuristic outputs for testing only.
        """
        self._increment_request_count()

        # This is intentionally simplistic and only for demonstration/testing.
        lowered = prompt.lower()
        if "core_topics" in lowered and "angle" in lowered and "constraints" in lowered:
            return {
                "core_topics": ["general topic inferred from brief"],
                "angle": "overview",
                "constraints": [],
            }
        if '"queries"' in lowered and "query_text" in lowered:
            return {
                "queries": [
                    {"query_text": "example overview query", "intent": "overview"},
                    {"query_text": "example how-to query", "intent": "how-to"},
                ]
            }
        if "relevance_score" in lowered and "type" in lowered:
            return {
                "relevance_score": 0.5,
                "authority_score": 0.5,
                "accuracy_score": 0.5,
                "type": "guides",
                "tags": ["placeholder"],
            }
        if '"summary"' in lowered and '"key_points"' in lowered:
            return {
                "summary": "Placeholder summary for this document.",
                "key_points": ["Point 1", "Point 2"],
                "is_promotional": False,
            }
        # Blog review prompt (title choices + outline)
        if "title_choices" in lowered and "probability_of_success" in lowered:
            return {
                "title_choices": [
                    {"title": "Why LLM Observability Is Non-Negotiable for Enterprise AI", "probability_of_success": 0.85},
                    {"title": "From Experiment to Production: What CTOs Get Wrong About LLM Monitoring", "probability_of_success": 0.78},
                    {"title": "The Real Cost of Skipping Observability in Your AI Stack", "probability_of_success": 0.72},
                    {"title": "How to Implement LLM Observability Without Slowing Down Shipping", "probability_of_success": 0.68},
                    {"title": "LLM Observability Best Practices: What the Data Actually Shows", "probability_of_success": 0.65},
                ],
                "outline": "# Blog Outline (Dummy)\n\n## 1. Introduction\n- Hook from research; key stat or question.\n- State what the reader will learn.\n\n## 2. Main Section A\n- Key point from source 1.\n- Supporting detail.\n\n## 3. Main Section B\n- Key point from source 2.\n- Example or quote.\n\n## 4. Conclusion\n- Recap and CTA.",
            }
        # Blog draft revise prompt (draft + feedback -> revised draft)
        if "revising" in lowered and "copy editor feedback" in lowered and '"draft"' in lowered:
            return {
                "draft": "# Revised Draft (Dummy)\n\nThis is a placeholder revised draft. Use a real LLM to apply copy editor feedback.\n\n## Introduction\n\nRevised based on feedback.\n\n## Wrap up\n\nRecap and one practical next step.",
            }
        # Blog draft prompt (research + outline -> draft)
        if '"draft"' in lowered and ("style guide" in lowered or "research document" in lowered or "outline" in lowered):
            return {
                "draft": "# Example Draft (Dummy)\n\nThis is a placeholder draft. Use a real LLM to generate the full post.\n\n## Introduction\n\nHook and stakes would go here.\n\n## Main content\n\nSections from the outline, using the research document.\n\n## Wrap up\n\nRecap and one practical next step.",
            }
        # Copy editor prompt (draft + style guide -> feedback)
        if "feedback_items" in lowered and ("summary" in lowered or "copy editor" in lowered):
            return {
                "summary": "Dummy copy edit: The draft was not evaluated. Use a real LLM for professional feedback.",
                "feedback_items": [
                    {
                        "category": "style",
                        "severity": "consider",
                        "location": "opening",
                        "issue": "Example feedback item for testing.",
                        "suggestion": "Replace with real LLM output.",
                    },
                ],
            }
        # Publication agent: rejection follow-up (feedback -> ready_to_revise, questions)
        if "ready_to_revise" in lowered and "feedback_collected" in lowered:
            return {
                "ready_to_revise": True,
                "questions": [],
                "feedback_summary": "Dummy: author feedback collected for revision.",
            }
        # Publication agent: convert human feedback to editor feedback items
        if "author's collected feedback" in lowered and "feedback_items" in lowered:
            return {
                "feedback_items": [
                    {
                        "category": "voice",
                        "severity": "must_fix",
                        "location": None,
                        "issue": "Dummy: apply author feedback.",
                        "suggestion": "Address the author's requested changes.",
                    },
                ],
            }
        # Compliance agent (brand/style enforcer)
        if '"status"' in lowered and "violations" in lowered and "required_fixes" in lowered and "brand spec" in lowered:
            return {
                "status": "PASS",
                "violations": [],
                "required_fixes": [],
                "notes": "Dummy: compliance check passed.",
            }
        # Similar topics prompt
        if "similar_topics" in lowered and "similarity_score" in lowered:
            return {
                "similar_topics": [
                    {"topic": "Related topic A", "similarity_score": 0.85},
                    {"topic": "Related topic B", "similarity_score": 0.78},
                    {"topic": "Related topic C", "similarity_score": 0.72},
                ],
            }
        # Final synthesis prompt
        return {
            "analysis": "High-level synthesis is not available in DummyLLMClient.",
            "outline": ["Intro", "Body", "Conclusion"],
        }


class OllamaLLMClient(LLMClient):
    """
    LLM client implementation that talks to a local Ollama instance.

    It assumes an OpenAI-compatible chat completions API exposed by Ollama
    (as provided by `ollama serve`) and uses httpx under the hood.
    
    Features:
    - Retry with exponential backoff for transient errors (5xx, network)
    - Explicit error classification (rate limit, temporary, permanent)
    - Environment variable configuration support
    """

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        """
        :param model: Name of the Ollama model to use. Defaults to BLOG_LLM_MODEL env var or 'qwen3.5:397b-cloud'.
        :param base_url: Base URL of the Ollama server. Defaults to BLOG_LLM_BASE_URL env var.
        :param timeout: Request timeout in seconds. Defaults to BLOG_LLM_TIMEOUT env var.
        :param max_retries: Max retry attempts for transient errors. Defaults to BLOG_LLM_MAX_RETRIES env var.
        """
        super().__init__()
        
        config = get_blog_llm_config()
        self.model = model or config["model"]
        self.base_url = (base_url or config["base_url"]).rstrip("/")
        self.timeout = timeout if timeout is not None else config["timeout"]
        self.max_retries = max_retries if max_retries is not None else config["max_retries"]

        assert self.model, "model name is required"
        assert self.timeout > 0, "timeout must be positive"
        assert self.base_url, "base_url is required"
        
        logger.info(
            "OllamaLLMClient initialized: model=%s, base_url=%s, timeout=%s, max_retries=%s",
            self.model, self.base_url, self.timeout, self.max_retries
        )


def _extract_balanced_json(text: str) -> Optional[str]:
    """Extract a single top-level {...} by matching braces. Returns None if invalid."""
    if not text.startswith("{"):
        return None
    depth = 0
    in_string = False
    escape = False
    quote = None
    for i, c in enumerate(text):
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if not in_string:
            if c in ("'", '"'):
                in_string = True
                quote = c
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[: i + 1]
        else:
            if c == quote:
                in_string = False
    return None


def _repair_trailing_commas(text: str) -> str:
    """Remove trailing commas before } or ] to allow parsing."""
    return re.sub(r",\s*([}\]])", r"\1", text)


    def _extract_json(self, text: str) -> Dict[str, Any]:
        """
        Extract and parse a JSON object from the model's text response.

        Ollama models may wrap JSON in prose or code fences. For draft/revise
        responses, the model may use ---DRAFT--- so the markdown is not inside
        JSON (avoiding escaping issues). This helper tries multiple strategies.

        Raises:
            LLMJsonParseError: If no valid JSON could be extracted.
        """
        # Delimiter format: draft/revise agents ask for {"draft": 0}\n---DRAFT---\n<markdown>.
        if "---DRAFT---" in text:
            parts = text.split("---DRAFT---", 1)
            if len(parts) == 2 and parts[1].strip():
                return {"draft": parts[1].strip()}

        # Strip typical markdown code fences if present
        fenced_match = re.search(r"```(?:json)?(.*)```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced_match:
            text = fenced_match.group(1).strip()

        # Try direct JSON parse
        try:
            return json.loads(text)
        except Exception:
            pass

        # Fallback: take the first {...} block (greedy)
        obj_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if obj_match:
            candidate = obj_match.group(0)
            try:
                return json.loads(candidate)
            except Exception:
                pass

        # Fallback: extract single top-level object by balanced braces
        start = text.find("{")
        if start >= 0:
            candidate = _extract_balanced_json(text[start:])
            if candidate:
                for raw in (candidate, _repair_trailing_commas(candidate)):
                    if not raw:
                        continue
                    try:
                        return json.loads(raw)
                    except Exception:
                        pass

        # Draft fallback: model may have returned invalid JSON with markdown inside.
        if "draft" in text.lower():
            markdown_start = re.search(r"(?:^|\n)\s*#\s*.+", text)
            if markdown_start:
                draft_content = text[markdown_start.start() :].strip()
                if len(draft_content) >= 20:
                    return {"draft": draft_content}

        # Raise explicit error - never fail silently
        raise LLMJsonParseError(
            f"Could not parse JSON from LLM response (length={len(text)})",
            response_preview=text[:500] if text else "",
        )

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff with jitter."""
        delay = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_MAX)
        jitter = random.uniform(0, delay * 0.1)
        return delay + jitter

    def _make_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Make HTTP request with retry logic for transient errors.
        
        Raises:
            LLMRateLimitError: On 429 response after retries
            LLMTemporaryError: On 5xx response after retries
            LLMUnreachableError: On network errors after retries
            LLMError: On other 4xx errors (no retry)
        """
        url = f"{self.base_url}/v1/chat/completions"
        last_error: Optional[Exception] = None
        
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, json=payload)
                    
                    # Handle HTTP errors
                    if response.status_code == 429:
                        if attempt < self.max_retries:
                            delay = self._calculate_backoff(attempt)
                            logger.warning(
                                "Rate limited (429), retrying in %.1fs (attempt %d/%d)",
                                delay, attempt + 1, self.max_retries
                            )
                            time.sleep(delay)
                            continue
                        raise LLMRateLimitError(
                            "Rate limit exceeded after retries",
                            status_code=429,
                        )
                    
                    if 500 <= response.status_code < 600:
                        if attempt < self.max_retries:
                            delay = self._calculate_backoff(attempt)
                            logger.warning(
                                "Server error (%d), retrying in %.1fs (attempt %d/%d)",
                                response.status_code, delay, attempt + 1, self.max_retries
                            )
                            time.sleep(delay)
                            continue
                        raise LLMTemporaryError(
                            f"Server error {response.status_code} after retries",
                            status_code=response.status_code,
                        )
                    
                    if 400 <= response.status_code < 500:
                        # Client errors (except 429) are not retryable
                        raise LLMError(
                            f"Client error: {response.status_code} - {response.text[:200]}",
                            status_code=response.status_code,
                        )
                    
                    response.raise_for_status()
                    return response.json()
                    
            except httpx.TimeoutException as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(
                        "Request timeout, retrying in %.1fs (attempt %d/%d)",
                        delay, attempt + 1, self.max_retries
                    )
                    time.sleep(delay)
                    continue
                    
            except httpx.ConnectError as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(
                        "Connection error, retrying in %.1fs (attempt %d/%d): %s",
                        delay, attempt + 1, self.max_retries, str(e)
                    )
                    time.sleep(delay)
                    continue
                    
            except (LLMRateLimitError, LLMTemporaryError, LLMError):
                raise
                
            except Exception as e:
                last_error = e
                logger.error("Unexpected error during LLM request: %s", e)
                raise LLMError(f"Unexpected error: {e}", cause=e) from e
        
        # All retries exhausted
        raise LLMUnreachableError(
            f"LLM unreachable after {self.max_retries} retries: {last_error}",
            cause=last_error,
        )

    def _should_enable_thinking(self) -> bool:
        """Check if thinking mode should be enabled for this model.
        
        Thinking mode is enabled for qwen3.5 models by default, but can be
        controlled via the SW_LLM_ENABLE_THINKING environment variable.
        """
        env_val = os.environ.get(ENV_LLM_ENABLE_THINKING, "").lower()
        if env_val == "false":
            return False
        if env_val == "true":
            return "qwen3.5" in self.model.lower()
        # Default: enable for qwen3.5 models
        return "qwen3.5" in self.model.lower()

    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Call the Ollama chat completions API and return a parsed JSON dict.

        Args:
            prompt: The prompt to send to the LLM.
            temperature: Sampling temperature (0.0-2.0).
            
        Returns:
            Parsed JSON response as a dict.
            
        Raises:
            LLMError: On API errors (rate limit, server error, unreachable)
            LLMJsonParseError: If response cannot be parsed as JSON
        """
        self._increment_request_count()

        system_message = (
            "You are a strict JSON generator used by an automated research agent. "
            "You MUST respond with a single valid JSON object only, with no "
            "explanatory text, no Markdown, and no code fences."
        )

        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
        }

        # Enable thinking mode for qwen3.5 models (improves reasoning quality)
        if self._should_enable_thinking():
            payload["think"] = True
            logger.debug("Thinking mode enabled for model %s", self.model)

        data = self._make_request(payload)
        
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(
                f"Unexpected response format from LLM: missing choices[0].message.content",
                cause=exc,
            ) from exc

        return self._extract_json(content)


