"""LLM client wrapper for Personal Assistant team with robust JSON extraction."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "software_engineering_team"))

logger = logging.getLogger(__name__)

MAX_CONTINUATION_CYCLES = 10


class LLMError(Exception):
    """Raised when LLM operations fail."""


class LLMTruncatedError(LLMError):
    """Raised when LLM response was truncated due to token limit.

    This exception signals that the response is incomplete and the caller should
    decompose the task into smaller pieces rather than attempting partial recovery.

    Attributes:
        partial_content: The truncated content returned by the LLM.
        done_reason: The done_reason from the Ollama API response.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        done_reason: str = "length",
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.done_reason = done_reason


class JSONExtractionFailure(LLMError):
    """
    Raised when JSON extraction fails after all recovery attempts.
    
    This is a LOUD failure that provides detailed information about what happened
    and how the user might resolve the issue.
    """

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
        suggestions = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(self.recovery_suggestions))
        last_response = self.raw_responses[-1][:800] if self.raw_responses else "No responses captured"
        
        return (
            f"\n{'='*80}\n"
            f"CRITICAL: JSON EXTRACTION FAILED\n"
            f"{'='*80}\n\n"
            f"Error: {self.args[0]}\n\n"
            f"Recovery Attempts Made:\n"
            f"  - Total LLM calls: {self.attempts_made}\n"
            f"  - Continuation requests (asking LLM to finish): {self.continuation_attempts}\n"
            f"  - Task decompositions (breaking into smaller tasks): {self.decomposition_attempts}\n\n"
            f"HOW TO RESOLVE THIS ISSUE:\n{suggestions}\n\n"
            f"Original prompt (first 500 chars):\n"
            f"  {self.original_prompt[:500]}{'...' if len(self.original_prompt) > 500 else ''}\n\n"
            f"Last raw response (first 800 chars):\n"
            f"  {last_response}\n"
            f"{'='*80}\n"
        )


class LLMClient:
    """
    LLM client for making completions with robust JSON extraction.
    
    Features:
    - Immediate decomposition for truncated or unparseable JSON
    - Recursive task decomposition up to 20 levels deep
    - Loud, informative failures with recovery suggestions
    """

    MAX_DECOMPOSITION_ATTEMPTS = 20

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 180.0,
        max_retries: int = 3,
    ) -> None:
        """
        Initialize the LLM client.
        
        Args:
            base_url: Ollama base URL. Defaults to SW_LLM_BASE_URL or localhost.
            model: Model name. Defaults to SW_LLM_MODEL.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts for network errors.
        """
        self.base_url = base_url or os.getenv("SW_LLM_BASE_URL", "http://127.0.0.1:11434")
        self.model = model or os.getenv("SW_LLM_MODEL", "llama3.2")
        self.timeout = timeout
        self.max_retries = max_retries
        
        self._provider = os.getenv("SW_LLM_PROVIDER", "ollama")
        if self._provider == "dummy":
            logger.warning("Using dummy LLM provider")

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """
        Generate a text completion.
        
        Args:
            prompt: The prompt to complete
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            system_prompt: Optional system prompt
            
        Returns:
            Generated text
        """
        if self._provider == "dummy":
            return self._dummy_complete(prompt)
        
        return self._ollama_complete(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        system_prompt: Optional[str] = None,
        expected_keys: Optional[List[str]] = None,
        decomposition_hints: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate a JSON completion with robust extraction and recovery.
        
        This method will:
        1. Attempt direct JSON extraction
        2. If JSON is truncated, request continuations (up to 3 times)
        3. If still failing, decompose into smaller tasks (up to 10 times)
        4. If all attempts fail, raise a LOUD error with recovery suggestions
        
        Args:
            prompt: The prompt (should request JSON output)
            temperature: Sampling temperature
            system_prompt: Optional system prompt
            expected_keys: Keys expected in JSON (helps with decomposition)
            decomposition_hints: Hints for breaking down the task
            
        Returns:
            Parsed JSON dict
            
        Raises:
            JSONExtractionFailure: If extraction fails after all recovery attempts
        """
        if self._provider == "dummy":
            return self._dummy_complete_json(prompt)
        
        return self._robust_json_extraction(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            expected_keys=expected_keys,
            decomposition_hints=decomposition_hints,
        )

    def _robust_json_extraction(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        system_prompt: Optional[str] = None,
        expected_keys: Optional[List[str]] = None,
        decomposition_hints: Optional[List[str]] = None,
        _depth: int = 0,
    ) -> Dict[str, Any]:
        """
        Robust JSON extraction with immediate decomposition on errors.

        When truncation or JSON parse errors are detected, immediately decomposes
        the task into smaller pieces rather than retrying.

        Args:
            prompt: The prompt to send
            temperature: Sampling temperature
            system_prompt: Optional system prompt
            expected_keys: Keys expected in JSON (helps with decomposition)
            decomposition_hints: Hints for breaking down the task
            _depth: Current decomposition depth (internal)

        Returns:
            Parsed JSON dict.

        Raises:
            RuntimeError: If max decomposition depth reached.
            JSONExtractionFailure: If extraction fails after all decomposition.
        """
        raw_responses: List[str] = []
        should_decompose = False
        error_type = "unknown"

        try:
            response = self._ollama_complete(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                json_mode=True,
            )
            raw_responses.append(response)

            parsed = self._try_parse_json(response)
            if parsed is not None:
                return parsed

            logger.warning(
                "JSON parse failed. Next step -> Decomposing task (depth %d/%d)",
                _depth + 1,
                self.MAX_DECOMPOSITION_ATTEMPTS,
            )
            should_decompose = True
            error_type = "JSONParseError"

        except LLMTruncatedError as e:
            logger.warning(
                "LLMTruncatedError (%d chars partial). Next step -> Attempting continuation",
                len(e.partial_content),
            )
            raw_responses.append(e.partial_content)

            continuation_result = self._attempt_continuation(
                prompt,
                e.partial_content,
                system_prompt=system_prompt,
            )

            if continuation_result is not None:
                parsed = self._try_parse_json(continuation_result)
                if parsed is not None:
                    logger.info("Continuation succeeded, JSON parsed successfully")
                    return parsed
                logger.warning(
                    "Continuation produced content but JSON parse failed. "
                    "Next step -> Decomposing task (depth %d/%d)",
                    _depth + 1,
                    self.MAX_DECOMPOSITION_ATTEMPTS,
                )
                raw_responses.append(continuation_result)
            else:
                logger.warning(
                    "Continuation exhausted. Next step -> Decomposing task (depth %d/%d)",
                    _depth + 1,
                    self.MAX_DECOMPOSITION_ATTEMPTS,
                )

            should_decompose = True
            error_type = "LLMTruncatedError"

        if should_decompose:
            if _depth >= self.MAX_DECOMPOSITION_ATTEMPTS:
                raise RuntimeError(
                    f"Maximum decomposition depth ({self.MAX_DECOMPOSITION_ATTEMPTS}) "
                    f"reached without successful response. Error type: {error_type}"
                )

            subtasks = self._decompose_task(prompt, expected_keys, decomposition_hints)

            if subtasks:
                combined_result: Dict[str, Any] = {}
                all_succeeded = True

                for subtask in subtasks:
                    try:
                        subtask_result = self._robust_json_extraction(
                            subtask["prompt"],
                            temperature=temperature,
                            expected_keys=None,
                            decomposition_hints=None,
                            _depth=_depth + 1,
                        )

                        if subtask_result:
                            if subtask["key"]:
                                combined_result[subtask["key"]] = subtask_result.get(
                                    subtask["key"], subtask_result
                                )
                            else:
                                combined_result.update(subtask_result)
                            logger.info(
                                "Subtask '%s' completed (depth %d)",
                                subtask["key"],
                                _depth + 1,
                            )
                    except RuntimeError as e:
                        logger.warning(
                            "Subtask '%s' failed at depth %d: %s",
                            subtask["key"],
                            _depth + 1,
                            str(e)[:100],
                        )
                        all_succeeded = False

                if combined_result:
                    return combined_result

                if not all_succeeded:
                    raise RuntimeError(
                        f"Decomposition failed at depth {_depth}/{self.MAX_DECOMPOSITION_ATTEMPTS}. "
                        "Some subtasks could not be processed."
                    )

        raise JSONExtractionFailure(
            message=f"Failed to extract valid JSON (error: {error_type})",
            original_prompt=prompt,
            attempts_made=1,
            continuation_attempts=0,
            decomposition_attempts=_depth,
            raw_responses=raw_responses,
            recovery_suggestions=self._generate_recovery_suggestions(prompt, raw_responses),
        )

    def _try_parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Attempt to parse JSON from text.
        
        Returns None if parsing fails - does NOT attempt partial extraction.
        """
        if not text:
            return None
        
        text = text.strip()
        
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if json_match:
            text = json_match.group(1).strip()
        
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            elif isinstance(result, list):
                return {"items": result}
            return None
        except json.JSONDecodeError:
            pass
        
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            candidate = text[start:end]
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass
        
        return None

    def _attempt_continuation(
        self,
        original_prompt: str,
        partial_content: str,
        *,
        system_prompt: Optional[str] = None,
        max_cycles: int = MAX_CONTINUATION_CYCLES,
        task_id: Optional[str] = None,
    ) -> Optional[str]:
        """Attempt to continue a truncated response using multi-turn conversation.

        Uses the native /api/chat endpoint to send a continuation prompt.

        Args:
            original_prompt: The original user prompt.
            partial_content: The truncated response content.
            system_prompt: Optional system prompt.
            max_cycles: Maximum continuation cycles.
            task_id: Optional task identifier for logging continuation responses.

        Returns:
            Complete content if successful, None if continuation fails/exhausts.
        """
        try:
            from shared.continuation import ResponseContinuator, ContinuationResult
        except ImportError:
            logger.warning("Continuation module not available, skipping continuation")
            return None

        logger.info(
            "Attempting continuation (%d chars partial, max %d cycles)",
            len(partial_content),
            max_cycles,
        )

        json_system_prompt = system_prompt or (
            "You are a strict JSON generator. Respond with a single valid JSON object only, "
            "no explanatory text, no Markdown, no code fences."
        )

        try:
            continuator = ResponseContinuator(
                base_url=self.base_url,
                model=self.model,
                timeout=self.timeout,
                max_cycles=max_cycles,
            )

            result: ContinuationResult = continuator.attempt_continuation(
                original_prompt=original_prompt,
                partial_content=partial_content,
                system_prompt=json_system_prompt,
                json_mode=True,
                task_id=task_id or "personal_assistant",
            )

            if result.success:
                logger.info(
                    "Continuation succeeded after %d cycles (%d chars total)",
                    result.cycles_used,
                    len(result.content),
                )
                return result.content

            logger.warning(
                "Continuation exhausted after %d cycles (%d chars accumulated)",
                result.cycles_used,
                len(result.content),
            )
            return None

        except Exception as e:
            logger.warning("Continuation failed with error: %s", str(e)[:100])
            return None

    def _decompose_task(
        self,
        original_prompt: str,
        expected_keys: Optional[List[str]],
        hints: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        """Decompose a task into smaller subtasks."""
        subtasks = []
        
        if expected_keys:
            for key in expected_keys:
                subtask_prompt = (
                    f"Extract ONLY the '{key}' field from this request. "
                    f"Return a minimal JSON object containing just '{key}'.\n\n"
                    f"Request:\n{original_prompt}\n\n"
                    f"Return JSON with only '{key}':"
                )
                subtasks.append({"key": key, "prompt": subtask_prompt})
            return subtasks
        
        if hints:
            for hint in hints:
                key = hint.split()[0].lower().replace(":", "").replace(",", "")
                subtask_prompt = (
                    f"Focus on this aspect ONLY: {hint}\n\n"
                    f"Original request:\n{original_prompt}\n\n"
                    f"Return minimal JSON for just this aspect:"
                )
                subtasks.append({"key": key, "prompt": subtask_prompt})
            return subtasks
        
        subtasks = [
            {
                "key": None,
                "prompt": (
                    "Simplify your response. Return the MINIMUM viable JSON. "
                    "Use shorter strings, fewer array items, and omit optional fields.\n\n"
                    f"Request:\n{original_prompt}\n\n"
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
        """Generate actionable suggestions for resolving the failure."""
        suggestions = []
        
        suggestions.append(
            "Simplify the request: Break your request into smaller, more specific parts. "
            "Complex requests with many fields are more likely to exceed output limits."
        )
        
        if len(prompt) > 2000:
            suggestions.append(
                f"Reduce prompt size: Your prompt is {len(prompt)} characters. "
                "Remove unnecessary context or examples to allow more room for the response."
            )
        
        suggestions.append(
            "Use expected_keys parameter: When calling complete_json(), provide "
            "expected_keys=['key1', 'key2', ...] to help with automatic task decomposition."
        )
        
        suggestions.append(
            "Use a model with higher output limits: Configure SW_LLM_MODEL to use a model "
            "that supports longer outputs (e.g., models with higher num_ctx settings)."
        )
        
        suggestions.append(
            "Increase model context: For Ollama, set num_ctx in model options to a higher value "
            "(e.g., 8192 or 16384) to allow longer responses."
        )
        
        suggestions.append(
            "Check the timeout: Increase SW_LLM_TIMEOUT if the model needs more time. "
            "Current timeout may be causing premature termination."
        )
        
        if raw_responses:
            last = raw_responses[-1].lower()
            if "error" in last or "cannot" in last or "unable" in last:
                suggestions.append(
                    "Review LLM response: The model may be refusing the request. "
                    "Check the raw response in the error output for refusal messages."
                )
        
        return suggestions

    def _ollama_complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        json_mode: bool = False,
    ) -> str:
        """Make a completion request to Ollama.

        Raises:
            LLMTruncatedError: If response was truncated due to token limit.
            LLMError: If request fails after retries.
        """
        url = f"{self.base_url}/api/generate"
        
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }
        
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        
        if system_prompt:
            payload["system"] = system_prompt
        
        if json_mode:
            payload["format"] = "json"
        
        last_error = None
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    data = response.json()

                    content = data.get("response", "")
                    done_reason = data.get("done_reason", "")

                    if done_reason == "length":
                        logger.warning(
                            "LLM response truncated (done_reason=length). Partial: %d chars",
                            len(content),
                        )
                        raise LLMTruncatedError(
                            "Response truncated due to token limit (done_reason=length)",
                            partial_content=content,
                            done_reason=done_reason,
                        )

                    return content
            except LLMTruncatedError:
                raise
            except httpx.HTTPError as e:
                last_error = e
                logger.warning(
                    "LLM request failed (attempt %d/%d): %s",
                    attempt + 1, self.max_retries, e
                )
        
        raise LLMError(f"LLM request failed after {self.max_retries} attempts: {last_error}")

    def _dummy_complete(self, prompt: str) -> str:
        """Return a dummy completion for testing."""
        return f"[DUMMY] Response to: {prompt[:100]}..."

    def _dummy_complete_json(self, prompt: str) -> Dict[str, Any]:
        """Return a dummy JSON completion for testing."""
        if "intent" in prompt.lower():
            return {
                "primary_intent": "general",
                "secondary_intents": [],
                "entities": {},
                "confidence": 0.8,
            }
        if "extract" in prompt.lower():
            return {
                "extracted_info": [],
                "reasoning": "Dummy extraction",
            }
        return {"status": "dummy", "message": "This is a test response"}


def get_llm_client(agent_key: Optional[str] = None) -> LLMClient:
    """
    Get an LLM client, optionally configured for a specific agent.
    
    Args:
        agent_key: Optional agent identifier for agent-specific model config.
        
    Returns:
        Configured LLMClient
    """
    model = None
    if agent_key:
        env_key = f"SW_LLM_MODEL_{agent_key}"
        model = os.getenv(env_key)
    
    return LLMClient(model=model)
