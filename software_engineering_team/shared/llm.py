"""
LLM client abstraction for software engineering team agents.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import re
from typing import Any, Dict

import httpx


class LLMClient(ABC):
    """
    Minimal abstraction around an LLM client.
    """

    @abstractmethod
    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Run the model with the given prompt and return a JSON-decoded dict.
        """

    def complete_text(self, prompt: str, *, temperature: float = 0.0) -> str:
        """
        Run the model and return raw text. Override for implementations that support it.
        Falls back to complete_json if a simple text response is wrapped in JSON.
        """
        result = self.complete_json(prompt, temperature=temperature)
        if isinstance(result, dict) and len(result) == 1 and "text" in result:
            return str(result["text"])
        return json.dumps(result)


class DummyLLMClient(LLMClient):
    """No-op implementation for tests and environments without an LLM."""

    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        lowered = prompt.lower()
        if "architecture" in lowered and "components" in lowered:
            return {
                "architecture_document": "# System Architecture (Dummy)\n\nPlaceholder architecture.",
                "components": [{"name": "API", "type": "backend"}, {"name": "WebApp", "type": "frontend"}],
            }
        if "task_assignments" in lowered or "tasks" in lowered:
            return {
                "tasks": [
                    {"id": "t1", "type": "backend", "description": "Implement API", "assignee": "backend"},
                    {"id": "t2", "type": "frontend", "description": "Implement UI", "assignee": "frontend"},
                ],
            }
        if "security" in lowered and "vulnerabilities" in lowered:
            return {
                "vulnerabilities": [],
                "fixed_code": "// No changes needed (dummy)",
                "suggested_commit_message": "fix(security): apply security review",
            }
        if "devops" in lowered or "pipeline" in lowered:
            return {
                "pipeline_yaml": "# Dummy pipeline",
                "iac_content": "# Dummy IaC",
                "suggested_commit_message": "ci: add pipeline configuration",
            }
        if "integration_test" in lowered or "readme_content" in lowered or ("bugs_found" in lowered and "test_plan" in lowered):
            return {
                "bugs_found": [],
                "fixed_code": "",
                "integration_tests": "# Dummy integration test",
                "unit_tests": "# Dummy unit tests for 85% coverage",
                "test_plan": "Dummy test plan",
                "summary": "Dummy QA assessment",
                "live_test_notes": "Dummy live test notes",
                "readme_content": "# Dummy README - build, run, test, deploy sections",
                "suggested_commit_message": "test: add integration tests",
            }
        # Spec parsing prompt
        if "acceptance_criteria" in lowered and "specification" in lowered:
            return {
                "title": "Software Project",
                "description": "Project specification (parsed from initial_spec.md).",
                "acceptance_criteria": ["See specification document"],
                "constraints": [],
                "priority": "medium",
            }
        return {"output": "Dummy response", "status": "ok"}


class OllamaLLMClient(LLMClient):
    """LLM client that talks to a local Ollama instance."""

    def __init__(
        self,
        model: str = "llama3.1",
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 1800.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _extract_json(self, text: str) -> Dict[str, Any]:
        if "---DRAFT---" in text:
            parts = text.split("---DRAFT---", 1)
            if len(parts) == 2 and parts[1].strip():
                return {"content": parts[1].strip()}
        fenced_match = re.search(r"```(?:json)?(.*)```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced_match:
            text = fenced_match.group(1).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        obj_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if obj_match:
            try:
                return json.loads(obj_match.group(0))
            except Exception:
                pass
        raise ValueError(f"Could not parse JSON from response: {text!r}")

    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        system_message = (
            "You are a strict JSON generator. Respond with a single valid JSON object only, "
            "no explanatory text, no Markdown, no code fences."
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
        content = data["choices"][0]["message"]["content"]
        return self._extract_json(content)
