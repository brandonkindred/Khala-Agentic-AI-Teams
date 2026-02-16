from __future__ import annotations

from abc import ABC, abstractmethod
import json
import re
from threading import Lock
from typing import Any, Dict

import httpx


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
    """

    def __init__(
        self,
        model: str = "llama3.1",
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 1800.0,
    ) -> None:
        """
        :param model: Name of the Ollama model to use (e.g. 'llama3.1').
        :param base_url: Base URL of the Ollama server.
        :param timeout: Request timeout in seconds.

        Preconditions:
            - model is a non-empty string.
            - timeout > 0.
            - base_url is a non-empty string.
        """
        super().__init__()

        assert model, "model name is required"
        assert timeout > 0, "timeout must be positive"
        assert base_url, "base_url is required"
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """
        Extract and parse a JSON object from the model's text response.

        Ollama models may wrap JSON in prose or code fences. For draft/revise
        responses, the model may use ---DRAFT--- so the markdown is not inside
        JSON (avoiding escaping issues). This helper tries multiple strategies.

        Preconditions:
            - text contains at least one parseable JSON object or ---DRAFT--- (or raises ValueError).
        Postconditions:
            - Returns a dict; or raises ValueError if no JSON could be parsed.
        """
        # Delimiter format: draft/revise agents ask for {"draft": 0}\n---DRAFT---\n<markdown>.
        # This avoids JSON escaping of long markdown. Try this first when present.
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

        # Fallback: take the first {...} block (greedy; may include trailing content in DOTALL)
        obj_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if obj_match:
            candidate = obj_match.group(0)
            try:
                return json.loads(candidate)
            except Exception:
                pass

        # Draft fallback: model may have returned invalid JSON with markdown inside.
        # Only use when the response looks like a draft (contains "draft") so we don't
        # mis-treat other agents' output (e.g. review outline starting with #).
        if "draft" in text.lower():
            markdown_start = re.search(r"(?:^|\n)\s*#\s*.+", text)
            if markdown_start:
                draft_content = text[markdown_start.start() :].strip()
                if len(draft_content) >= 20:
                    return {"draft": draft_content}

        # As a last resort, raise a clear error so callers can handle it.
        raise ValueError(f"Could not parse JSON from Ollama response: {text!r}")

    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Call the Ollama chat completions API and return a parsed JSON dict.

        Preconditions: prompt non-empty; 0.0 <= temperature <= 2.0.
        Postconditions: Returns dict; never None. Raises on API or parse failure.
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

        url = f"{self.base_url}/v1/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()

        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Ollama response format: {data!r}") from exc

        return self._extract_json(content)


