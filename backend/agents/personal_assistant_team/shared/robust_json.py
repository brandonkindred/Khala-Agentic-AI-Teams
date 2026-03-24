"""Robust JSON extraction with retry, continuation, and task decomposition."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class JSONExtractionError(Exception):
    """Raised when JSON extraction fails after all recovery attempts."""

    def __init__(
        self,
        message: str,
        original_prompt: str,
        attempts_made: int,
        continuation_attempts: int,
        decomposition_attempts: int,
        raw_responses: List[str],
        recovery_suggestions: List[str],
    ):
        super().__init__(message)
        self.original_prompt = original_prompt
        self.attempts_made = attempts_made
        self.continuation_attempts = continuation_attempts
        self.decomposition_attempts = decomposition_attempts
        self.raw_responses = raw_responses
        self.recovery_suggestions = recovery_suggestions

    def __str__(self) -> str:
        suggestions = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(self.recovery_suggestions))
        return (
            f"\n{'=' * 80}\n"
            f"JSON EXTRACTION FAILED\n"
            f"{'=' * 80}\n\n"
            f"Error: {self.args[0]}\n\n"
            f"Recovery Attempts Made:\n"
            f"  - Total attempts: {self.attempts_made}\n"
            f"  - Continuation requests: {self.continuation_attempts}\n"
            f"  - Task decompositions: {self.decomposition_attempts}\n\n"
            f"Suggestions to resolve:\n{suggestions}\n\n"
            f"Original prompt (first 500 chars):\n"
            f"  {self.original_prompt[:500]}{'...' if len(self.original_prompt) > 500 else ''}\n\n"
            f"Last raw response (first 500 chars):\n"
            f"  {self.raw_responses[-1][:500] if self.raw_responses else 'No responses captured'}\n"
            f"{'=' * 80}\n"
        )


@dataclass
class ExtractionAttempt:
    """Record of a single extraction attempt."""

    attempt_number: int
    strategy: str
    prompt_used: str
    raw_response: str
    success: bool
    error: Optional[str] = None
    parsed_result: Optional[Dict[str, Any]] = None


@dataclass
class ExtractionResult:
    """Result of JSON extraction with full audit trail."""

    success: bool
    data: Optional[Dict[str, Any]] = None
    attempts: List[ExtractionAttempt] = field(default_factory=list)
    total_attempts: int = 0
    continuation_attempts: int = 0
    decomposition_attempts: int = 0
    error: Optional[JSONExtractionError] = None


class RobustJSONExtractor:
    """
    Robust JSON extractor with immediate decomposition on errors.

    When JSON parsing fails or response is truncated, immediately decomposes
    the task into smaller subtasks rather than retrying. Supports up to 20
    levels of recursive decomposition.
    """

    MAX_DECOMPOSITION_ATTEMPTS = 20

    def __init__(
        self,
        llm_complete_fn: Callable[[str, bool], str],
        temperature: float = 0.2,
    ):
        """
        Initialize the extractor.

        Args:
            llm_complete_fn: Function that takes (prompt, json_mode) and returns response
            temperature: Temperature for LLM calls
        """
        self.llm_complete = llm_complete_fn
        self.temperature = temperature

    def extract(
        self,
        prompt: str,
        *,
        expected_keys: Optional[List[str]] = None,
        decomposition_hints: Optional[List[str]] = None,
        _depth: int = 0,
    ) -> ExtractionResult:
        """
        Extract JSON from LLM response with immediate decomposition on errors.

        When parsing fails or response is truncated, immediately decomposes
        the task into smaller subtasks rather than retrying.

        Args:
            prompt: The prompt requesting JSON output
            expected_keys: Keys expected in the JSON (for validation)
            decomposition_hints: Hints for how to decompose the task
            _depth: Current decomposition depth (internal)

        Returns:
            ExtractionResult with success status and data or error

        Raises:
            RuntimeError: If max decomposition depth reached.
        """
        result = ExtractionResult()
        raw_responses: List[str] = []

        response = self._make_request(prompt, json_mode=True)
        raw_responses.append(response)
        result.total_attempts += 1

        attempt = ExtractionAttempt(
            attempt_number=1,
            strategy="direct",
            prompt_used=prompt,
            raw_response=response,
            success=False,
        )

        parsed, error = self._try_parse(response)
        if parsed is not None:
            attempt.success = True
            attempt.parsed_result = parsed
            result.attempts.append(attempt)
            result.success = True
            result.data = parsed
            return result

        attempt.error = error
        result.attempts.append(attempt)

        is_truncated = self._detect_incomplete_json(response)
        error_type = "truncated" if is_truncated else "parse_error"

        logger.warning(
            "JSON extraction failed (%s). Next step -> Decomposing task (depth %d/%d)",
            error_type,
            _depth + 1,
            self.MAX_DECOMPOSITION_ATTEMPTS,
        )

        decomposition_result = self._attempt_decomposition(
            prompt, expected_keys, decomposition_hints, raw_responses, result, _depth
        )
        if decomposition_result is not None:
            result.success = True
            result.data = decomposition_result
            return result

        result.error = JSONExtractionError(
            message=f"Failed to extract valid JSON ({error_type}) after decomposition exhausted",
            original_prompt=prompt,
            attempts_made=result.total_attempts,
            continuation_attempts=result.continuation_attempts,
            decomposition_attempts=result.decomposition_attempts,
            raw_responses=raw_responses,
            recovery_suggestions=self._generate_recovery_suggestions(prompt, raw_responses),
        )

        return result

    def _make_request(self, prompt: str, json_mode: bool = True) -> str:
        """Make a request to the LLM."""
        return self.llm_complete(prompt, json_mode)

    def _try_parse(self, text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Try to parse JSON from text."""
        text = text.strip()

        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if json_match:
            text = json_match.group(1).strip()

        try:
            return json.loads(text), None
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end]), None
            except json.JSONDecodeError as e:
                return None, f"JSON decode error: {e}"

        start = text.find("[")
        end = text.rfind("]") + 1
        if start != -1 and end > start:
            try:
                arr = json.loads(text[start:end])
                return {"items": arr}, None
            except json.JSONDecodeError as e:
                return None, f"JSON array decode error: {e}"

        return None, "No valid JSON structure found"

    def _detect_incomplete_json(self, text: str) -> bool:
        """Detect if JSON appears to be truncated."""
        text = text.strip()

        open_braces = text.count("{") - text.count("}")
        open_brackets = text.count("[") - text.count("]")

        if open_braces > 0 or open_brackets > 0:
            return True

        if text.endswith(",") or text.endswith(":"):
            return True

        if re.search(r'"\s*$', text) and not text.rstrip().endswith('"}'):
            if text.count('"') % 2 != 0:
                return True

        return False

    def _attempt_decomposition(
        self,
        original_prompt: str,
        expected_keys: Optional[List[str]],
        decomposition_hints: Optional[List[str]],
        raw_responses: List[str],
        result: ExtractionResult,
        depth: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Attempt to decompose the task into smaller subtasks recursively.

        Raises:
            RuntimeError: If max decomposition depth is reached.
        """
        if depth >= self.MAX_DECOMPOSITION_ATTEMPTS:
            raise RuntimeError(
                f"Maximum decomposition depth ({self.MAX_DECOMPOSITION_ATTEMPTS}) "
                "reached without successful response"
            )

        subtasks = self._decompose_prompt(original_prompt, expected_keys, decomposition_hints)

        if not subtasks:
            logger.warning("Could not decompose prompt into subtasks")
            return None

        logger.info(
            "Decomposing into %d subtasks (depth %d/%d)",
            len(subtasks),
            depth + 1,
            self.MAX_DECOMPOSITION_ATTEMPTS,
        )

        combined_result: Dict[str, Any] = {}

        for subtask_idx, subtask in enumerate(subtasks):
            result.decomposition_attempts += 1
            result.total_attempts += 1

            response = self._make_request(subtask["prompt"], json_mode=True)
            raw_responses.append(response)

            attempt = ExtractionAttempt(
                attempt_number=result.total_attempts,
                strategy=f"decomposition_{depth + 1}_{subtask_idx + 1}_{subtask['key']}",
                prompt_used=subtask["prompt"][:500],
                raw_response=response[:500],
                success=False,
            )

            parsed, error = self._try_parse(response)

            if parsed is None:
                is_truncated = self._detect_incomplete_json(response)
                error_type = "truncated" if is_truncated else "parse_error"

                logger.warning(
                    "Subtask %d (%s) failed (%s). Next step -> Recursive decomposition",
                    subtask_idx + 1,
                    subtask["key"],
                    error_type,
                )

                try:
                    parsed = self._attempt_decomposition(
                        subtask["prompt"],
                        None,
                        None,
                        raw_responses,
                        result,
                        depth + 1,
                    )
                except RuntimeError as e:
                    logger.warning(
                        "Subtask %d (%s) decomposition failed: %s",
                        subtask_idx + 1,
                        subtask["key"],
                        str(e)[:100],
                    )

            if parsed is not None:
                attempt.success = True
                attempt.parsed_result = parsed
                result.attempts.append(attempt)

                if subtask["key"]:
                    combined_result[subtask["key"]] = parsed.get(subtask["key"], parsed)
                else:
                    combined_result.update(parsed)
            else:
                attempt.error = error or "Failed to parse subtask result"
                result.attempts.append(attempt)

        if combined_result:
            return combined_result

        return None

    def _decompose_prompt(
        self,
        original_prompt: str,
        expected_keys: Optional[List[str]],
        hints: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        """Decompose a prompt into smaller subtasks."""
        subtasks = []

        if expected_keys:
            for key in expected_keys:
                subtask_prompt = (
                    f"Focus ONLY on extracting the '{key}' field from this request. "
                    f"Return a small, focused JSON object with just the '{key}' key.\n\n"
                    f"Original request:\n{original_prompt}\n\n"
                    f"Return JSON with only the '{key}' field:"
                )
                subtasks.append({"key": key, "prompt": subtask_prompt})
            return subtasks

        if hints:
            for hint in hints:
                subtask_prompt = (
                    f"Focus ONLY on this aspect: {hint}\n\n"
                    f"Original request:\n{original_prompt}\n\n"
                    f"Return JSON for just this aspect:"
                )
                subtasks.append({"key": hint.split()[0].lower(), "prompt": subtask_prompt})
            return subtasks

        subtasks = [
            {
                "key": "main",
                "prompt": (
                    "Simplify your response. Return the MINIMUM viable JSON that answers "
                    "this request. Use shorter strings, fewer items in arrays, and omit "
                    "optional fields.\n\n"
                    f"Original request:\n{original_prompt}\n\n"
                    "Return minimal JSON:"
                ),
            }
        ]

        return subtasks

    def _generate_recovery_suggestions(
        self,
        prompt: str,
        raw_responses: List[str],
    ) -> List[str]:
        """Generate suggestions for the user on how to resolve the issue."""
        suggestions = []

        suggestions.append(
            "Simplify the request: Break your request into smaller, more specific parts."
        )

        if len(prompt) > 2000:
            suggestions.append(
                f"Reduce prompt size: Your prompt is {len(prompt)} characters. "
                "Try reducing context or being more concise."
            )

        suggestions.append(
            "Use a larger model: The current model may have limited output capacity. "
            "Try a model with higher token limits."
        )

        suggestions.append(
            "Check LLM configuration: Ensure SW_LLM_MODEL and SW_LLM_BASE_URL are "
            "correctly configured for a capable model."
        )

        if raw_responses:
            last_response = raw_responses[-1]
            if "error" in last_response.lower() or "cannot" in last_response.lower():
                suggestions.append(
                    "Review LLM response: The model may be refusing the request. "
                    "Check the raw response for error messages."
                )

        suggestions.append(
            "Increase timeout: Set SW_LLM_TIMEOUT to a higher value (e.g., 300) "
            "if the model needs more time to generate complete responses."
        )

        suggestions.append(
            "Check model output limits: Some models have hard output limits. "
            "Consider using a model with higher num_ctx or max_tokens settings."
        )

        return suggestions


def create_robust_extractor(llm_client: Any) -> RobustJSONExtractor:
    """
    Create a RobustJSONExtractor from an LLM client.

    Args:
        llm_client: An LLMClient instance

    Returns:
        Configured RobustJSONExtractor
    """

    def llm_complete_fn(prompt: str, json_mode: bool) -> str:
        if json_mode:
            return llm_client._ollama_complete(prompt, temperature=0.2, json_mode=True)
        else:
            return llm_client._ollama_complete(prompt, temperature=0.2, json_mode=False)

    return RobustJSONExtractor(llm_complete_fn)
