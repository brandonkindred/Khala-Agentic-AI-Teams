"""Minimal LLM client for SOC2 audit agents (Ollama or dummy for tests)."""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

ENV_PROVIDER = "SOC2_LLM_PROVIDER"  # "dummy" | "ollama"
ENV_MODEL = "SOC2_LLM_MODEL"
ENV_BASE_URL = "SOC2_LLM_BASE_URL"
ENV_TIMEOUT = "SOC2_LLM_TIMEOUT"


class LLMClient(ABC):
    """Abstract LLM client for JSON completion."""

    @abstractmethod
    def complete_json(self, prompt: str, temperature: float = 0.1) -> Dict[str, Any]:
        """Return a single JSON object parsed from the model response."""
        pass


class OllamaLLMClient(LLMClient):
    """Ollama-backed LLM client. Expects Ollama running with the configured model."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.model = model or os.environ.get(ENV_MODEL) or "llama3.1"
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = float(os.environ.get(ENV_TIMEOUT, timeout))

    def complete_json(self, prompt: str, temperature: float = 0.1) -> Dict[str, Any]:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "")
        return _parse_json_from_response(text)


class DummyLLMClient(LLMClient):
    """Returns minimal valid JSON for testing without an LLM."""

    def complete_json(self, prompt: str, temperature: float = 0.1) -> Dict[str, Any]:
        # Return empty findings so pipeline produces "next steps" document
        if "findings" in prompt.lower() or "vulnerability" in prompt.lower() or "compliance" in prompt.lower():
            if "security" in prompt.lower():
                return {"summary": "No issues found in dummy run.", "findings": [], "compliant": True}
            if "availability" in prompt.lower():
                return {"summary": "No availability gaps in dummy run.", "findings": [], "compliant": True}
            if "processing integrity" in prompt.lower():
                return {"summary": "No processing integrity issues in dummy run.", "findings": [], "compliant": True}
            if "confidentiality" in prompt.lower():
                return {"summary": "No confidentiality gaps in dummy run.", "findings": [], "compliant": True}
            if "privacy" in prompt.lower():
                return {"summary": "No privacy issues in dummy run.", "findings": [], "compliant": True}
        if "next steps" in prompt.lower() or "next_steps" in prompt.lower():
            return {
                "title": "Next Steps for SOC2 Certification",
                "introduction": "The codebase audit found no material SOC2 gaps.",
                "steps": [
                    {"title": "Engage a CPA firm", "description": "Select an auditor for a Type I/II examination."},
                    {"title": "Document controls", "description": "Map controls to TSC and collect evidence."},
                ],
                "recommended_timeline": "3–6 months for readiness, then examination.",
                "raw_markdown": "# Next Steps\n\nEngage a CPA firm and document controls.",
            }
        return {"summary": "Dummy response.", "findings": [], "raw_markdown": "# Dummy report\n\nNo issues."}


def _parse_json_from_response(text: str) -> Dict[str, Any]:
    """Extract a single JSON object from model output (may be wrapped in markdown or prose)."""
    text = text.strip()
    # Try to find ```json ... ``` block
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    # Find first { ... }
    start = text.find("{")
    if start == -1:
        return {"error": "No JSON object in response", "raw": text[:500]}
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return {"error": "Unbalanced braces", "raw": text[:500]}
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as e:
        return {"error": str(e), "raw": text[start:end][:500]}


def get_llm_client() -> LLMClient:
    """Return LLM client based on SOC2_LLM_PROVIDER (default ollama)."""
    provider = (os.environ.get(ENV_PROVIDER) or "ollama").lower().strip()
    if provider == "dummy":
        return DummyLLMClient()
    return OllamaLLMClient()
