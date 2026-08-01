"""
Response continuation module for handling truncated LLM responses.

This module provides functionality to continue truncated LLM responses
by building multi-turn conversations and prompting the LLM to continue
from where it left off.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

ENV_LLM_ENABLE_THINKING = "LLM_ENABLE_THINKING"
ENV_LLM_OLLAMA_API_KEY = "LLM_OLLAMA_API_KEY"
ENV_LLM_MAX_OUTPUT_TOKENS = "LLM_MAX_OUTPUT_TOKENS"
DEFAULT_MAX_OUTPUT_TOKENS = 32768


def _ollama_auth_headers() -> Dict[str, str]:
    """Return Authorization Bearer header for Ollama Cloud when API key is set."""
    key = os.environ.get("OLLAMA_API_KEY") or os.environ.get(ENV_LLM_OLLAMA_API_KEY)
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


MAX_CONTINUATION_CYCLES = 10
CONTINUATION_CONTEXT_CHARS = 150
CONTINUATION_LOG_DIR = "continuation_logs"


@dataclass
class ContinuationResult:
    """Result of a continuation attempt.

    Attributes:
        success: Whether continuation produced a complete response
            (``final_done_reason != "length"``).
        content: The concatenated content from all continuation cycles,
            deduplicated at cycle boundaries by `ResponseContinuator._merge_responses`.
        cycles_used: Number of continuation cycles attempted, including a
            final failed cycle when `success` is False due to a request error.
        partial_responses: List of all partial responses collected, one per
            cycle that completed successfully. A cycle that raised an error
            is NOT appended here, so `len(partial_responses)` can be less
            than `cycles_used` in the error case.
        final_done_reason: The `done_reason` from the last successful
            response, or `"error"` if the last cycle raised, or `"length"`
            if `max_cycles` was exhausted while still truncated.

    Invariants:
        * `cycles_used == len(partial_responses)` holds whenever construction
          came from a normal (non-error) return path; it does NOT hold on the
          error path, where `cycles_used` counts the failed attempt but
          `partial_responses` does not include it.
    """

    success: bool
    content: str
    cycles_used: int
    partial_responses: List[str] = field(default_factory=list)
    final_done_reason: str = ""


class LLMContinuationExhaustedError(Exception):
    """Raised when continuation attempts are exhausted without success.

    Note: this exception is defined for callers that prefer an
    exception-based API, but nothing in this module currently raises it —
    `ResponseContinuator.attempt_continuation` reports exhaustion by
    returning `ContinuationResult(success=False, final_done_reason="length")`
    instead. A caller wanting exception-based signaling must raise this
    itself from the returned `ContinuationResult`.

    Attributes:
        partial_content: The accumulated partial content.
        cycles_attempted: Number of cycles attempted.
        partial_responses: All partial responses collected.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        cycles_attempted: int = 0,
        partial_responses: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.cycles_attempted = cycles_attempted
        self.partial_responses = partial_responses or []


class ResponseContinuator:
    """Handles continuation of truncated LLM responses via multi-turn conversation.

    When an LLM response is truncated (done_reason='length'), this class builds
    a conversation history including the partial response and prompts the LLM
    to continue from where it left off.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 300.0,
        max_cycles: int = MAX_CONTINUATION_CYCLES,
        num_predict: Optional[int] = None,
    ):
        """Initialize the continuator.

        Args:
            base_url: Ollama API base URL.
            model: Model name to use.
            timeout: Request timeout in seconds.
            max_cycles: Maximum number of continuation cycles.
            num_predict: Max tokens to generate per continuation turn. If None, uses
                LLM_MAX_OUTPUT_TOKENS env or DEFAULT_MAX_OUTPUT_TOKENS (32768) to match main LLM client.

        Preconditions:
            * `base_url` and `model` are expected to be non-empty; neither is
              validated here, so an empty value is only surfaced later as an
              HTTP failure from `_send_chat_request`, not at construction time.
            * `max_cycles` is expected to be `>= 1`; a value of 0 is accepted
              without error but makes `attempt_continuation`'s loop a no-op,
              immediately returning `success=False` with `cycles_used=0`.
        Postconditions:
            * `self.num_predict` resolves in this order: the explicit
              `num_predict` argument if not None, else the `LLM_MAX_OUTPUT_TOKENS`
              env var parsed as `int` if set, else `DEFAULT_MAX_OUTPUT_TOKENS`.
            * If `LLM_MAX_OUTPUT_TOKENS` is set but not parseable as `int`, this
              raises `ValueError` — that env var is not defensively parsed
              here (unlike the numeric-env-var convention described in the
              project docs for other settings).
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_cycles = max_cycles
        env_max = os.environ.get(ENV_LLM_MAX_OUTPUT_TOKENS)
        self.num_predict = (
            num_predict
            if num_predict is not None
            else (int(env_max) if env_max else DEFAULT_MAX_OUTPUT_TOKENS)
        )

    def attempt_continuation(
        self,
        original_prompt: str,
        partial_content: str,
        *,
        system_prompt: Optional[str] = None,
        json_mode: bool = False,
        task_id: Optional[str] = None,
        project_root: Optional[Path] = None,
    ) -> ContinuationResult:
        """Attempt to continue a truncated response.

        Builds a multi-turn conversation with the partial response and
        prompts the LLM to continue. Repeats until the response is complete
        or max_cycles is reached.

        Args:
            original_prompt: The original user prompt.
            partial_content: The truncated response content.
            system_prompt: Optional system prompt for the conversation.
            json_mode: Whether to request JSON format.
            task_id: Optional task identifier for logging (logs to continuation_logs/).
            project_root: Optional root directory for log files.

        Returns:
            ContinuationResult with success status and accumulated content.

        Preconditions:
            * `partial_content` is the truncated content collected so far
              (may be empty); it is treated as cycle 0 and is always the
              first entry of the returned `partial_responses`.

        Postconditions (the truncation-handling contract):
            * Runs at most `self.max_cycles` request cycles. Each cycle
              sends the accumulated conversation and inspects the response's
              `done_reason`.
            * Success path: as soon as a cycle's `done_reason != "length"`,
              returns immediately with `success=True`, `cycles_used` equal to
              that cycle's 1-based count, `final_done_reason` set to that
              cycle's `done_reason`, and `content` equal to all responses
              merged so far (via `_merge_responses`, which strips duplicate
              boundary text).
            * Still-truncated path: if every cycle's `done_reason == "length"`
              through `max_cycles` cycles, returns `success=False`,
              `cycles_used == self.max_cycles`, `final_done_reason="length"`,
              and `content` equal to the full merge of all cycles attempted.
            * Error path: if `_send_chat_request` raises for any reason, this
              method does NOT retry or re-raise — it returns immediately with
              `success=False`, `final_done_reason="error"`, `cycles_used`
              equal to the failed cycle's 1-based count, and `content`/
              `partial_responses` reflecting only the cycles that completed
              *before* the failure (the failed cycle's response is never
              collected, since none was received).
            * `task_id`/`project_root`, when provided, cause each cycle's raw
              response to be appended to a log file as a side effect. A file
              WRITE failure there is swallowed (see
              `_log_continuation_response`) and never affects the returned
              `ContinuationResult`. However, a directory-creation failure in
              `_log_continuation_response` (e.g. permission denied on the
              log directory) is NOT wrapped here either, and propagates
              uncaught out of this method — logging is best-effort for
              writes only, not for directory setup.
        """
        partial_responses = [partial_content]
        accumulated_content = partial_content

        if task_id:
            self._log_continuation_response(
                task_id=task_id,
                cycle=0,
                response_content=partial_content,
                done_reason="length (initial truncated response)",
                project_root=project_root,
            )

        for cycle in range(self.max_cycles):
            logger.info(
                "Continuation cycle %d/%d: accumulated %d chars",
                cycle + 1,
                self.max_cycles,
                len(accumulated_content),
            )

            messages = self._build_continuation_messages(
                original_prompt=original_prompt,
                partial_responses=partial_responses,
                system_prompt=system_prompt,
            )

            try:
                response_content, done_reason = self._send_chat_request(
                    messages=messages,
                    json_mode=json_mode,
                )
            except Exception as e:
                logger.warning(
                    "Continuation cycle %d failed: %s",
                    cycle + 1,
                    str(e),
                )
                return ContinuationResult(
                    success=False,
                    content=accumulated_content,
                    cycles_used=cycle + 1,
                    partial_responses=partial_responses,
                    final_done_reason="error",
                )

            if task_id:
                self._log_continuation_response(
                    task_id=task_id,
                    cycle=cycle + 1,
                    response_content=response_content,
                    done_reason=done_reason,
                    project_root=project_root,
                )

            partial_responses.append(response_content)
            accumulated_content = self._merge_responses(partial_responses)

            if done_reason != "length":
                logger.info(
                    "Continuation complete after %d cycles (%d chars, done_reason=%s)",
                    cycle + 1,
                    len(accumulated_content),
                    done_reason,
                )
                return ContinuationResult(
                    success=True,
                    content=accumulated_content,
                    cycles_used=cycle + 1,
                    partial_responses=partial_responses,
                    final_done_reason=done_reason,
                )

            logger.debug(
                "Cycle %d still truncated (done_reason=length), continuing...",
                cycle + 1,
            )

        logger.warning(
            "Continuation exhausted after %d cycles (%d chars accumulated)",
            self.max_cycles,
            len(accumulated_content),
        )
        return ContinuationResult(
            success=False,
            content=accumulated_content,
            cycles_used=self.max_cycles,
            partial_responses=partial_responses,
            final_done_reason="length",
        )

    def _build_continuation_messages(
        self,
        original_prompt: str,
        partial_responses: List[str],
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Build conversation messages for continuation.

        Creates a message history with:
        1. System message (if provided)
        2. Original user prompt
        3. For each partial response: assistant message + continuation prompt

        Args:
            original_prompt: The original user prompt.
            partial_responses: List of partial responses so far.
            system_prompt: Optional system prompt.

        Returns:
            List of message dicts for the chat API.

        Preconditions:
            * `partial_responses` is non-empty (callers always seed it with
              at least the initial truncated content).
        Postconditions:
            * Returns a flat list alternating `{"role": "assistant", ...}`
              (one per entry in `partial_responses`) with a following
              `{"role": "user", ...}` continuation prompt, prefixed by an
              optional system message and the original user message.
            * Every entry in `partial_responses`, including the last, is
              followed by a continuation prompt — the `if`/`else` branching
              on whether an entry is the last one currently produces the
              same `_create_continuation_prompt(partial)` call in both
              branches, so the last entry is not treated differently today.
        """
        messages: List[Dict[str, str]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        messages.append({"role": "user", "content": original_prompt})

        for i, partial in enumerate(partial_responses):
            messages.append({"role": "assistant", "content": partial})

            if i < len(partial_responses) - 1:
                messages.append(
                    {
                        "role": "user",
                        "content": self._create_continuation_prompt(partial),
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": self._create_continuation_prompt(partial),
                    }
                )

        return messages

    def _create_continuation_prompt(self, partial_content: str) -> str:
        """Create a prompt asking the LLM to continue.

        Args:
            partial_content: The truncated content to continue from.

        Returns:
            Continuation prompt string.

        Preconditions:
            * None; `partial_content` may be empty.
        Postconditions:
            * Pure function (no side effects, no I/O).
            * Embeds only the last `CONTINUATION_CONTEXT_CHARS` (150)
              characters of `partial_content` (or all of it if shorter), with
              newlines replaced by the literal two-character sequence `\\n`
              so the quoted excerpt renders on one line in the prompt.
            * If `partial_content` is empty, embeds an empty quoted excerpt.
        """
        last_chars = partial_content[-CONTINUATION_CONTEXT_CHARS:] if partial_content else ""
        last_chars_escaped = last_chars.replace("\n", "\\n")

        return (
            f"Please continue exactly from where you left off. "
            f"Your previous response ended with: '{last_chars_escaped}'. "
            f"Continue the response seamlessly without repeating what you already wrote."
        )

    def _send_chat_request(
        self,
        messages: List[Dict[str, str]],
        json_mode: bool = False,
    ) -> tuple[str, str]:
        """Send a chat request to the Ollama API.

        Uses the native /api/chat endpoint for multi-turn conversation support.

        Args:
            messages: List of conversation messages.
            json_mode: Whether to request JSON format.

        Returns:
            Tuple of (response_content, done_reason).

        Raises:
            Exception: If the request fails.

        Preconditions:
            * `messages` is non-empty (built by `_build_continuation_messages`).
        Postconditions:
            * On an HTTP 200 response, returns `(content, done_reason)` where
              `content` defaults to `""` and `done_reason` defaults to
              `"stop"` if either key is absent from the response body.
            * On any non-200 HTTP response, raises a plain `Exception`
              (not a specific subclass) carrying the status code and body
              text — callers distinguish this from other failures only by
              message content, not exception type.
            * Any transport-level failure (timeout, connection error, JSON
              decode error) propagates as whatever `httpx`/stdlib exception
              it naturally raises; this method adds no additional handling.
            * `self.timeout` bounds the whole request; `self.num_predict`
              is sent as `options.num_predict` on every call.
        """
        url = f"{self.base_url}/api/chat"

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": self.num_predict},
            "think": False,
        }

        if json_mode:
            payload["format"] = "json"

        logger.debug("Thinking mode enabled for continuation with model %s", self.model)

        logger.debug(
            "Sending chat request: %d messages, json_mode=%s",
            len(messages),
            json_mode,
        )

        headers = _ollama_auth_headers()
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload, headers=headers)

            if response.status_code != 200:
                raise Exception(
                    f"Chat request failed with status {response.status_code}: {response.text}"
                )

            data = response.json()

        message = data.get("message", {})
        content = message.get("content", "")
        done_reason = data.get("done_reason", "stop")

        return content, done_reason

    def _merge_responses(self, partial_responses: List[str]) -> str:
        """Merge multiple partial responses into one.

        Handles overlapping text at boundaries by detecting and removing
        duplicate content where responses might have re-stated context.

        Args:
            partial_responses: List of partial responses to merge.

        Returns:
            Merged content string.

        Preconditions:
            * None; `partial_responses` may be empty or contain empty strings.
        Postconditions:
            * Returns `""` if `partial_responses` is empty, or the single
              element unchanged if there is exactly one.
            * Otherwise, concatenates left-to-right, and before appending
              each subsequent response, strips a detected overlap (see
              `_find_overlap`) from the START of that response only — overlap
              is checked independently at each boundary against the merged
              result so far, not globally across the whole merged string.
            * Pure function (no side effects, no I/O).
        """
        if not partial_responses:
            return ""

        if len(partial_responses) == 1:
            return partial_responses[0]

        result = partial_responses[0]

        for i in range(1, len(partial_responses)):
            continuation = partial_responses[i]

            overlap = self._find_overlap(result, continuation)
            if overlap > 0:
                continuation = continuation[overlap:]

            result += continuation

        return result

    def _find_overlap(self, text1: str, text2: str, min_overlap: int = 10) -> int:
        """Find overlapping text between end of text1 and start of text2.

        Args:
            text1: First text (check end).
            text2: Second text (check start).
            min_overlap: Minimum overlap length to consider.

        Returns:
            Length of overlap found, or 0 if no significant overlap.

        Preconditions:
            * None; either argument may be empty (returns 0 immediately).
        Postconditions:
            * Searches candidate overlap lengths from
              `min(len(text1), len(text2), 500)` down to `min_overlap`,
              inclusive, and returns the length of the FIRST (i.e. longest)
              length for which `text1`'s trailing substring exactly equals
              `text2`'s leading substring of that same length.
            * Returns 0 if no candidate length in that range matches,
              including when `max_check < min_overlap` (checked range is
              empty) — an overlap shorter than `min_overlap` is never
              reported even if one exists.
            * Pure function (no side effects, no I/O).
        """
        if not text1 or not text2:
            return 0

        max_check = min(len(text1), len(text2), 500)

        for overlap_len in range(max_check, min_overlap - 1, -1):
            if text1[-overlap_len:] == text2[:overlap_len]:
                logger.debug("Found overlap of %d chars", overlap_len)
                return overlap_len

        return 0

    def _log_continuation_response(
        self,
        task_id: str,
        cycle: int,
        response_content: str,
        done_reason: str,
        project_root: Optional[Path] = None,
    ) -> None:
        """Log a continuation response to a task-specific file.

        Each continuation cycle's response is appended to a file for verification
        and debugging purposes.

        Args:
            task_id: Identifier for the current task (used in filename).
            cycle: Current cycle number (1-based).
            response_content: The response content from this cycle.
            done_reason: The done_reason from the API response.
            project_root: Root directory for the log folder. If None, uses cwd.

        Preconditions:
            * `task_id` may contain arbitrary characters; only
              alphanumerics, `-`, and `_` survive into the filename, all
              others are replaced with `_`.
        Postconditions:
            * Best-effort only, but only for the FILE WRITE step: on success,
              appends a formatted entry to
              `{project_root or cwd}/continuation_logs/{safe_task_id}_continuation.txt`,
              creating the directory if needed.
            * `log_dir.mkdir(...)` runs OUTSIDE the try/except below — if
              directory creation fails (e.g. permission denied), that
              exception propagates uncaught to the caller
              (`attempt_continuation`), unlike the write step.
            * Only the subsequent `open(...)`/`write(...)` is wrapped: any
              exception there is caught, logged as a warning, and swallowed
              — this method returns normally in that case, and its caller
              cannot detect a swallowed write failure.
        """
        if project_root is None:
            project_root = Path.cwd()
        else:
            project_root = Path(project_root)

        log_dir = project_root / CONTINUATION_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)

        safe_task_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        log_file = log_dir / f"{safe_task_id}_continuation.txt"

        timestamp = datetime.now().isoformat()

        entry = (
            f"\n{'=' * 80}\n"
            f"CONTINUATION CYCLE {cycle}/{self.max_cycles}\n"
            f"Timestamp: {timestamp}\n"
            f"Done Reason: {done_reason}\n"
            f"Content Length: {len(response_content)} chars\n"
            f"{'-' * 80}\n"
            f"{response_content}\n"
            f"{'=' * 80}\n"
        )

        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry)
            logger.debug(
                "Logged continuation cycle %d to %s",
                cycle,
                log_file,
            )
        except Exception as e:
            logger.warning(
                "Failed to log continuation response: %s",
                str(e),
            )


def attempt_response_continuation(
    base_url: str,
    model: str,
    original_prompt: str,
    partial_content: str,
    *,
    system_prompt: Optional[str] = None,
    json_mode: bool = False,
    timeout: float = 300.0,
    max_cycles: int = MAX_CONTINUATION_CYCLES,
    task_id: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> ContinuationResult:
    """Convenience function for attempting response continuation.

    Args:
        base_url: Ollama API base URL.
        model: Model name to use.
        original_prompt: The original user prompt.
        partial_content: The truncated response content.
        system_prompt: Optional system prompt.
        json_mode: Whether to request JSON format.
        timeout: Request timeout in seconds.
        max_cycles: Maximum continuation cycles.
        task_id: Optional task identifier for logging (logs to continuation_logs/).
        project_root: Optional root directory for log files.

    Returns:
        ContinuationResult with success status and accumulated content.

    Preconditions:
        * Same as `ResponseContinuator.__init__` and
          `ResponseContinuator.attempt_continuation` combined, since this is
          a thin passthrough to both.
    Postconditions:
        * Constructs a fresh `ResponseContinuator` (with `num_predict` left
          at its default resolution — this function has no `num_predict`
          parameter of its own) and delegates entirely to its
          `attempt_continuation`; adds no additional behavior of its own.
    """
    continuator = ResponseContinuator(
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_cycles=max_cycles,
    )

    return continuator.attempt_continuation(
        original_prompt=original_prompt,
        partial_content=partial_content,
        system_prompt=system_prompt,
        json_mode=json_mode,
        task_id=task_id,
        project_root=project_root,
    )
