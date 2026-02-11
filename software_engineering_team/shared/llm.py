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
        # Architecture prompt is more specific (architecture_document); check before Tech Lead
        if "architecture_document" in lowered and "components" in lowered and "overview" in lowered:
            return {
                "overview": "API backend + WebApp frontend (Dummy architecture).",
                "architecture_document": "# System Architecture (Dummy)\n\nPlaceholder architecture.",
                "components": [{"name": "API", "type": "backend"}, {"name": "WebApp", "type": "frontend"}],
            }
        # Tech Lead prompt asks for tasks + execution_order – return granular plan with descriptive IDs
        if ("execution_order" in lowered or "task_assignments" in lowered) and "tasks" in lowered:
            return {
                "tasks": [
                    {"id": "git-setup", "label": "Git setup", "type": "git_setup", "description": "Ensure development branch exists", "assignee": "devops", "requirements": "Create development branch from main if missing", "acceptance_criteria": ["Development branch exists"], "dependencies": []},
                    {"id": "devops-dockerfile", "label": "DevOps Dockerfile", "type": "devops", "description": "Create Dockerfile for the application", "assignee": "devops", "requirements": "Multi-stage Dockerfile, Python/Node base, build and run config", "acceptance_criteria": ["Dockerfile builds", "Image runs"], "dependencies": ["git-setup"]},
                    {"id": "devops-ci-pipeline", "label": "DevOps CI pipeline", "type": "devops", "description": "Create CI/CD pipeline (GitHub Actions or similar)", "assignee": "devops", "requirements": "Pipeline runs on push, lint, test, build steps", "acceptance_criteria": ["CI runs on push", "Tests executed"], "dependencies": ["devops-dockerfile"]},
                    {"id": "backend-data-models", "label": "Backend data models", "type": "backend", "description": "Define Pydantic/SQLAlchemy models for domain entities", "assignee": "backend", "requirements": "Models match spec, validation, relationships", "acceptance_criteria": ["Models defined", "Validation works"], "dependencies": ["devops-ci-pipeline"]},
                    {"id": "backend-crud-api", "label": "Backend CRUD API", "type": "backend", "description": "Implement REST CRUD endpoints for main entities", "assignee": "backend", "requirements": "GET list, POST create, GET by id, PUT update, DELETE", "acceptance_criteria": ["Endpoints respond correctly", "404 for missing"], "dependencies": ["backend-data-models"]},
                    {"id": "backend-validation", "label": "Backend validation", "type": "backend", "description": "Add input validation and error handling to API", "assignee": "backend", "requirements": "Pydantic validation, 422 on invalid input", "acceptance_criteria": ["Invalid input rejected", "Clear error messages"], "dependencies": ["backend-crud-api"]},
                    {"id": "frontend-app-shell", "label": "Frontend app shell", "type": "frontend", "description": "Create Angular app shell, routing, layout", "assignee": "frontend", "requirements": "App component, routing module, main layout", "acceptance_criteria": ["App loads", "Routing works"], "dependencies": ["devops-ci-pipeline"]},
                    {"id": "frontend-list-component", "label": "Frontend list component", "type": "frontend", "description": "Implement list view component for main entities", "assignee": "frontend", "requirements": "Fetch from API, display list, loading state", "acceptance_criteria": ["List displays data", "Loading indicator"], "dependencies": ["frontend-app-shell", "backend-crud-api"]},
                    {"id": "frontend-form-component", "label": "Frontend form component", "type": "frontend", "description": "Implement add/edit form with validation", "assignee": "frontend", "requirements": "Reactive forms, validation, submit to API", "acceptance_criteria": ["Form validates", "Submit creates/updates"], "dependencies": ["frontend-list-component"]},
                    {"id": "frontend-detail-component", "label": "Frontend detail component", "type": "frontend", "description": "Implement detail view and delete confirmation", "assignee": "frontend", "requirements": "Detail view, delete with confirmation", "acceptance_criteria": ["Detail shows", "Delete works"], "dependencies": ["frontend-list-component"]},
                    {"id": "security-backend-review", "label": "Security backend review", "type": "security", "description": "Review backend code for vulnerabilities", "assignee": "security", "requirements": "Check injection, auth, input sanitization", "acceptance_criteria": ["No critical vulns"], "dependencies": ["backend-validation"]},
                    {"id": "security-frontend-review", "label": "Security frontend review", "type": "security", "description": "Review frontend code for XSS and other issues", "assignee": "security", "requirements": "Check XSS, CSRF, sensitive data exposure", "acceptance_criteria": ["No critical vulns"], "dependencies": ["frontend-form-component", "frontend-detail-component"]},
                    {"id": "qa-integration-tests", "label": "QA integration tests", "type": "qa", "description": "Add integration tests for API and critical flows", "assignee": "qa", "requirements": "Test CRUD, error cases, coverage", "acceptance_criteria": ["Tests pass", "Coverage adequate"], "dependencies": ["security-backend-review", "security-frontend-review"]},
                    {"id": "qa-readme", "label": "QA README", "type": "qa", "description": "Document build, run, test, deploy instructions", "assignee": "qa", "requirements": "README.md with all sections", "acceptance_criteria": ["README complete", "Instructions work"], "dependencies": ["qa-integration-tests"]},
                ],
                "execution_order": ["git-setup", "devops-dockerfile", "devops-ci-pipeline", "backend-data-models", "backend-crud-api", "backend-validation", "frontend-app-shell", "frontend-list-component", "frontend-form-component", "frontend-detail-component", "security-backend-review", "security-frontend-review", "qa-integration-tests", "qa-readme"],
                "rationale": "Granular plan: git setup, devops (Docker + CI), backend (models -> CRUD -> validation), frontend (shell -> list -> form/detail), security review, QA tests and README.",
                "summary": "15 tasks covering full spec: git, devops (2), backend (3), frontend (4), security (2), qa (2). Every spec requirement mapped to tasks.",
            }
        if "security" in lowered and "vulnerabilities" in lowered:
            return {
                "vulnerabilities": [],
                "summary": "No security issues found (dummy)",
            }
        if "devops" in lowered or "pipeline" in lowered:
            return {
                "pipeline_yaml": "# Dummy pipeline\nname: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo build",
                "iac_content": "# Dummy IaC",
                "dockerfile": "FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"-m\", \"http.server\"]",
                "suggested_commit_message": "ci: add pipeline configuration",
            }
        if "backend" in lowered and "language" in lowered and ("code" in lowered or "files" in lowered):
            return {
                "code": "# Dummy backend API\nfrom fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get(\"/\")\ndef root():\n    return {\"status\": \"ok\"}\n",
                "language": "python",
                "summary": "Dummy backend implementation",
                "files": {"main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get(\"/\")\ndef root():\n    return {\"status\": \"ok\"}\n"},
                "tests": "# Dummy tests\ndef test_root():\n    assert True\n",
                "suggested_commit_message": "feat(api): add backend API",
            }
        if "frontend" in lowered and "angular" in lowered and "component" in lowered:
            return {
                "code": "import { Component } from '@angular/core';\n\n@Component({ selector: 'app-root', template: '<p>Dummy</p>' })\nexport class AppComponent {}\n",
                "summary": "Dummy frontend implementation",
                "files": {"app.component.ts": "import { Component } from '@angular/core';\n\n@Component({ selector: 'app-root', template: '<p>Dummy</p>' })\nexport class AppComponent {}\n"},
                "components": ["AppComponent"],
                "suggested_commit_message": "feat(ui): add frontend component",
            }
        if "integration_test" in lowered or "readme_content" in lowered or ("bugs_found" in lowered and "test_plan" in lowered):
            return {
                "bugs_found": [],
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
