"""
Dummy LLM client for tests and environments without an LLM.

Returns heuristic stub responses matching SE team prompts so existing tests keep passing.
Also implements the ``strands.models.model.Model`` ABC so it can be passed directly to
``strands.Agent(model=DummyLLMClient())`` in tests without requiring a live Ollama server.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterable
from typing import Any, Dict, Optional

from strands.models.model import Model
from strands.types.content import Message as StrandsMessage
from strands.types.content import SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from ..interface import LLMClient

_STRIP_VERBS = {
    "implement",
    "create",
    "build",
    "add",
    "setup",
    "set",
    "up",
    "configure",
    "make",
    "define",
    "develop",
    "write",
    "design",
    "establish",
    "generate",
    "fetches",
    "displays",
    "handles",
    "manages",
    "processes",
    "returns",
    "provides",
    "supports",
    "includes",
    "enables",
    "renders",
}
_STRIP_FILLERS = {
    "the",
    "that",
    "with",
    "using",
    "which",
    "for",
    "and",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "by",
    "from",
    "into",
    "as",
    "via",
    "its",
    "all",
    "application",
    "system",
    "project",
    "based",
    "proper",
    "production",
    "quality",
    "complete",
    "full",
    "new",
    "existing",
    "angular",
    "react",
    "vue",
    "spring",
    "fastapi",
    "flask",
    "django",
}
_STRIP_SUFFIXES = {
    "component",
    "service",
    "module",
    "endpoint",
    "endpoints",
    "middleware",
    "guard",
    "pipe",
    "directive",
    "interceptor",
    "controller",
    "repository",
}


def _extract_name_from_hint(hint: str, separator: str = "-", max_length: int = 25) -> str:
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", hint)
    words = re.sub(r"[^a-z0-9\s]+", " ", expanded.lower()).split()
    filtered = [
        w
        for w in words
        if w not in _STRIP_VERBS and w not in _STRIP_FILLERS and w not in _STRIP_SUFFIXES
    ]
    name_words = filtered[:3] if filtered else words[:2]
    result = separator.join(name_words)
    if len(result) > max_length:
        result = result[:max_length].rstrip(separator)
    return result or f"item{separator}1"


class DummyLLMClient(LLMClient, Model):
    """No-op implementation for tests and environments without an LLM.

    Also implements the Strands ``Model`` ABC so tests can pass this directly
    to ``strands.Agent(model=DummyLLMClient())``.
    """

    _call_counter: int = 0

    def __init__(self) -> None:
        self._request_count = 0
        self._model_config: dict[str, Any] = {}

    # -----------------------------------------------------------------------
    # strands.models.model.Model ABC implementation
    # -----------------------------------------------------------------------

    def update_config(self, **model_config: Any) -> None:
        self._model_config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._model_config)

    def structured_output(
        self,
        output_model: type,
        prompt: list,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError("DummyLLMClient.structured_output is not implemented for tests")

    async def stream(
        self,
        messages: list[StrandsMessage],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Yield a minimal stream that the Strands Agent event loop can process.

        When ``tool_specs`` contains a StructuredOutputTool (added by Strands
        when ``structured_output_model=...`` is used), yields a tool-use event
        invoking that tool with data from the ``complete_json`` pattern matcher.
        Otherwise yields a plain text response.
        """
        # Extract user text from the last user message
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                for block in msg.get("content", []):
                    if isinstance(block, dict) and "text" in block:
                        user_text = block["text"]
                        break
                    elif isinstance(block, str):
                        user_text = block
                        break
                break

        # Route through the existing complete_json pattern matcher for rich responses
        response_data = self.complete_json(user_text, system_prompt=system_prompt)
        response_text = json.dumps(response_data) if isinstance(response_data, dict) else str(response_data)

        # Check if Strands is requesting structured output via a tool
        structured_tool_name = None
        if tool_specs:
            for spec in tool_specs:
                desc = (spec.get("description") or "").lower()
                if "structuredoutputtool" in desc or "structured_output" in desc:
                    structured_tool_name = spec.get("name", "structured_output")
                    break

        yield {"messageStart": {"role": "assistant"}}

        if structured_tool_name:
            # Yield a tool-use block so Strands' structured output flow works
            tool_use_id = f"dummy_tool_{structured_tool_name}"
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {
                        "toolUse": {"toolUseId": tool_use_id, "name": structured_tool_name},
                    },
                },
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": response_text}},
                },
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
        else:
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": response_text}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}

        yield {
            "messageStop": {"stopReason": "tool_use" if structured_tool_name else "end_turn"},
            "metadata": {
                "usage": {"inputTokens": len(user_text) // 4, "outputTokens": len(response_text) // 4, "totalTokens": (len(user_text) + len(response_text)) // 4},
                "metrics": {"latencyMs": 1},
            },
        }

    @property
    def request_count(self) -> int:
        """Total number of LLM requests (for compatibility with blog tests)."""
        return getattr(self, "_request_count", 0)

    @staticmethod
    def _extract_task_hint(prompt: str) -> str:
        for line in prompt.split("\n"):
            stripped = line.strip()
            if stripped.startswith("**Task:**"):
                return stripped.replace("**Task:**", "").strip()[:80]
        return hashlib.md5(prompt[:500].encode()).hexdigest()[:12]

    def get_max_context_tokens(self) -> int:
        return 16384

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        think: bool = False,
    ) -> str:
        self._request_count += 1
        return "Dummy text completion (no LLM)."

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # Pattern-match against the user prompt only. Callers (including
        # Strands-migrated agents that hand their persona to the Strands
        # ``Agent`` as a system prompt) must include the anchor tokens the
        # branches below look for in the user prompt they build. Scanning
        # ``system_prompt`` here was tried and reverted in commit <<CI fix>>
        # because loose single-word branches (``"pipeline"``, ``"security"``)
        # cross-contaminated other teams' prompts that happened to mention
        # those words in their persona text.
        lowered = prompt.lower()
        DummyLLMClient._call_counter += 1
        self._request_count += 1
        counter = DummyLLMClient._call_counter
        task_hint = self._extract_task_hint(prompt)

        if "architecture_document" in lowered and "components" in lowered and "overview" in lowered:
            return {
                "overview": "API backend + WebApp frontend (Dummy architecture).",
                "architecture_document": "# System Architecture (Dummy)\n\nPlaceholder architecture.",
                "components": [
                    {"name": "API", "type": "backend"},
                    {"name": "WebApp", "type": "frontend"},
                ],
                "diagrams": {
                    "client_server_architecture": "graph LR\n  Browser-->API\n  API-->DB",
                    "frontend_code_structure": "graph TD\n  App-->Components\n  App-->Services",
                },
                "decisions": [
                    {
                        "decision": "Use REST API",
                        "context": "Standard web stack",
                        "consequences": "Simple integration",
                    }
                ],
            }
        elif "codebase audit" in lowered and "files_inventory" in lowered:
            return {
                "files_inventory": [
                    {
                        "path": "initial_spec.md",
                        "language": "markdown",
                        "purpose": "Project specification",
                        "key_exports": [],
                    }
                ],
                "frameworks": {
                    "backend": "unknown",
                    "frontend": "unknown",
                    "database": "unknown",
                    "testing": "unknown",
                    "cicd": "unknown",
                    "other": [],
                },
                "existing_functionality": ["Project specification document exists"],
                "partial_implementations": [],
                "gaps": [
                    "No application code exists yet",
                    "No backend framework set up",
                    "No frontend framework set up",
                    "No CI/CD pipeline",
                    "No database configuration",
                    "No tests",
                ],
                "code_conventions": {
                    "naming": "unknown",
                    "structure": "flat",
                    "config_approach": "unknown",
                },
                "summary": "The repository contains only the project specification (initial_spec.md). No application code, infrastructure, or tests exist yet. The entire application needs to be built from scratch according to the spec.",
            }
        elif "deep analysis" in lowered and "total_deliverable_count" in lowered:
            return {
                "data_entities": [
                    {
                        "name": "User",
                        "attributes": ["id", "email", "password_hash", "created_at"],
                        "relationships": [],
                        "validation_rules": ["email must be valid", "password required"],
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/auth/signup",
                        "description": "Create new user account",
                        "auth_required": False,
                    },
                    {
                        "method": "POST",
                        "path": "/auth/login",
                        "description": "Authenticate user and return JWT",
                        "auth_required": False,
                    },
                    {
                        "method": "POST",
                        "path": "/auth/refresh",
                        "description": "Refresh access token",
                        "auth_required": True,
                    },
                    {
                        "method": "GET",
                        "path": "/api/users/me",
                        "description": "Get current user profile",
                        "auth_required": True,
                    },
                ],
                "ui_screens": [
                    {
                        "name": "Login Page",
                        "description": "User login form",
                        "components": ["LoginForm", "ErrorDisplay"],
                        "states": ["idle", "loading", "error", "success"],
                    },
                    {
                        "name": "Registration Page",
                        "description": "User registration form",
                        "components": ["RegistrationForm", "ErrorDisplay"],
                        "states": ["idle", "loading", "error", "success"],
                    },
                    {
                        "name": "Dashboard",
                        "description": "Main authenticated view",
                        "components": ["Navbar", "UserProfile"],
                        "states": ["loading", "loaded"],
                    },
                ],
                "user_flows": [
                    {
                        "name": "User Registration",
                        "steps": [
                            "Navigate to signup",
                            "Fill form",
                            "Submit",
                            "Receive confirmation",
                            "Redirect to login",
                        ],
                    },
                    {
                        "name": "User Login",
                        "steps": [
                            "Navigate to login",
                            "Enter credentials",
                            "Submit",
                            "Receive JWT",
                            "Redirect to dashboard",
                        ],
                    },
                ],
                "non_functional": [
                    {"category": "security", "requirement": "Passwords must be hashed with bcrypt"},
                    {"category": "security", "requirement": "JWT tokens must expire"},
                    {"category": "performance", "requirement": "API response time under 500ms"},
                ],
                "infrastructure": [
                    {"category": "deployment", "requirement": "Docker containerization"},
                    {"category": "cicd", "requirement": "Automated CI/CD pipeline"},
                ],
                "integrations": [],
                "total_deliverable_count": 18,
                "summary": "The spec requires a full-stack authentication application with user registration, login, token refresh, and protected routes. The backend needs FastAPI with JWT auth, the frontend needs Angular with login/registration/dashboard screens, and DevOps needs Docker and CI/CD.",
            }
        elif "qa agent has reviewed code" in lowered and "fix tasks" in lowered:
            return {"tasks": [], "rationale": "QA approved; no fix tasks needed (dummy)."}
        elif "run security review now" in lowered and "90%" in lowered:
            return {"run_security": False, "rationale": "Code coverage not yet at 90% (dummy)."}
        elif "reviewing the progress" in lowered and "spec_compliance_pct" in lowered:
            return {
                "tasks": [],
                "spec_compliance_pct": 50,
                "gaps_identified": [],
                "rationale": "Progress review complete. Current tasks cover the planned scope (dummy).",
            }
        elif "clarification questions from specialist" in lowered:
            return {
                "title": "Refined Task Title",
                "description": "Refined task description with additional details from spec. The implementation should follow Angular best practices using standalone components and reactive forms. All public methods must have JSDoc documentation. Error states must be handled with user-friendly messages.",
                "user_story": "As a user, I want refined functionality so that the feature works as specified in the requirements.",
                "requirements": "Detailed requirements addressing clarification questions. Use Angular Material for UI components. Implement loading spinners during async operations. Handle HTTP errors with retry logic.",
                "acceptance_criteria": [
                    "Criterion 1: Component renders without errors",
                    "Criterion 2: User interactions trigger correct API calls",
                    "Criterion 3: Error states display meaningful messages",
                ],
            }
        elif (
            ("execution_order" in lowered or "task_assignments" in lowered)
            and "tasks" in lowered
        ) or (
            # Strands-migrated Tech Lead: user prompt has product context
            # while execution_order / initiative → epic → story keywords
            # live in the system prompt.
            system_prompt
            and "execution_order" in system_prompt.lower()
            and "initiative" in system_prompt.lower()
            and "**product title:**" in lowered
        ):
            return {
                "tasks": [
                    {
                        "id": "git-setup",
                        "title": "Initialize Git Development Branch",
                        "type": "git_setup",
                        "description": "Ensure the development branch exists.",
                        "user_story": "As a developer, I want a dedicated development branch.",
                        "assignee": "devops",
                        "requirements": "Create development branch from main if missing.",
                        "acceptance_criteria": ["Development branch exists and is checked out"],
                        "dependencies": [],
                    },
                    {
                        "id": "devops-dockerfile",
                        "title": "Multi-Stage Dockerfile",
                        "type": "devops",
                        "description": "Create a multi-stage Dockerfile.",
                        "user_story": "As a developer, I want a multi-stage Dockerfile.",
                        "assignee": "devops",
                        "requirements": "Multi-stage Dockerfile.",
                        "acceptance_criteria": ["Dockerfile builds successfully"],
                        "dependencies": ["git-setup"],
                    },
                ],
                "execution_order": ["git-setup", "devops-dockerfile"],
                "rationale": "Granular plan (dummy).",
                "summary": "2 tasks (dummy).",
                "requirement_task_mapping": [],
                "clarification_questions": [],
            }
        elif "bugs_found" in lowered and (
            "integration_test" in lowered or "readme_content" in lowered or "test_plan" in lowered
        ):
            # Kept ABOVE code-review catch-all and security/accessibility
            # branches because QA prompts now include a shared
            # REVIEW_PRIORITY_FRAMEWORK that mentions "security
            # vulnerabilities", and QA user prompts also contain
            # "code to review" which would match the code-review catch-all.
            # ``bugs_found`` is the anchor token — it's unique to the QA
            # output contract.
            return {
                "bugs_found": [],
                "integration_tests": "# Dummy integration test",
                "unit_tests": "# Dummy unit tests",
                "test_plan": "Dummy test plan",
                "summary": "Dummy QA assessment",
                "live_test_notes": "Dummy notes",
                "readme_content": "# Dummy README",
                "suggested_commit_message": "test: add integration tests",
                "approved": True,
            }
        elif "senior code reviewer" in lowered and ("approved" in lowered or "issues" in lowered):
            return {
                "approved": True,
                "issues": [],
                "summary": "Code review passed (dummy).",
                "spec_compliance_notes": "Code aligns with task requirements.",
                "suggested_commit_message": "",
            }
        elif ("code to review" in lowered or "review this code" in lowered or "chunk" in lowered) and (
            "approved" not in lowered or len(lowered) > 200
        ):
            # Catch-all for code review / chunk review prompts routed through Strands
            return {
                "approved": True,
                "issues": [],
                "summary": "Code review passed (dummy).",
                "spec_compliance_notes": "",
                "suggested_commit_message": "",
            }
        elif "security" in lowered and "vulnerabilities" in lowered:
            return {"vulnerabilities": [], "summary": "No security issues found (dummy)"}
        elif "accessibility" in lowered and "wcag" in lowered and "issues" in lowered:
            return {"issues": [], "summary": "No WCAG 2.2 accessibility issues found (dummy)"}
        elif "senior backend software engineer" in lowered:
            slug = (
                _extract_name_from_hint(task_hint, separator="_", max_length=25)
                or f"module_{counter}"
            )
            slug.title().replace("_", "")
            return {
                "code": f'"""Backend module: {task_hint}"""\nfrom fastapi import APIRouter\nrouter = APIRouter()\n',
                "language": "python",
                "summary": f"Backend implementation for: {task_hint}",
                "files": {
                    f"app/routers/{slug}.py": f'"""Backend module: {task_hint}"""\nfrom fastapi import APIRouter\nrouter = APIRouter()\n',
                    f"tests/test_{slug}.py": f'"""Tests for {task_hint}."""\ndef test_{slug}():\n    assert True\n',
                },
                "tests": f'"""Tests for {task_hint}."""\ndef test_{slug}():\n    assert True\n',
                "suggested_commit_message": f"feat(api): implement {slug.replace('_', ' ')}",
            }
        elif "senior frontend software engineer" in lowered:
            slug = (
                _extract_name_from_hint(task_hint, separator="-", max_length=25)
                or f"component-{counter}"
            )
            class_name = "".join(w.capitalize() for w in slug.split("-")) + "Component"
            selector = f"app-{slug}"
            return {
                "code": f"import {{ Component }} from '@angular/core';\n@Component({{ selector: '{selector}', template: '<div>{task_hint}</div>' }})\nexport class {class_name} {{}}\n",
                "summary": f"Frontend component for: {task_hint}",
                "files": {
                    f"src/app/components/{slug}/{slug}.component.ts": f"import {{ Component }} from '@angular/core';\n@Component({{ selector: '{selector}', template: '<div>{task_hint}</div>' }})\nexport class {class_name} {{}}\n",
                    f"src/app/components/{slug}/{slug}.component.spec.ts": f"import {{ {class_name} }} from './{slug}.component';\ndescribe('{class_name}', () => {{ it('should create', () => {{}}); }});\n",
                },
                "components": [class_name],
                "suggested_commit_message": f"feat(ui): add {slug} component",
            }
        elif "devops" in lowered or "pipeline" in lowered:
            return {
                "pipeline_yaml": f"# CI Pipeline (task #{counter})\nname: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n",
                "iac_content": f"# Infrastructure (task #{counter})\n",
                "dockerfile": f"# Dockerfile (task #{counter})\nFROM python:3.11-slim\nWORKDIR /app\n",
                "docker_compose": f"# Docker Compose (task #{counter})\nversion: '3.8'\nservices:\n  backend:\n    build: .\n",
                "summary": f"DevOps configuration generated for: {task_hint[:60]}",
                "suggested_commit_message": f"ci: add devops configuration (task #{counter})",
            }
        elif (
            "technical writer" in lowered
            and "readme_content" in lowered
            and "readme_changed" in lowered
        ):
            return {
                "readme_content": f"# Project\n\nAuto-generated documentation (task #{counter}).\n",
                "readme_changed": True,
                "summary": f"Updated README (task #{counter})",
                "suggested_commit_message": f"docs(readme): update (task #{counter})",
            }
        elif "contributors.md" in lowered and "contributors_content" in lowered:
            return {
                "contributors_content": "# Contributors\n| Agent | Role |\n|-------|------|\n",
                "contributors_changed": True,
                "summary": "Updated contributors list (dummy)",
            }
        elif "documentation update needed" in lowered and "should_update_docs" in lowered:
            return {
                "should_update_docs": True,
                "rationale": "Task completed with code changes (dummy).",
            }
        elif (
            "design by contract" in lowered
            and "comments_added" in lowered
            and "already_compliant" in lowered
        ):
            return {
                "files": {},
                "comments_added": 0,
                "comments_updated": 0,
                "already_compliant": True,
                "summary": "All code fully complies with Design by Contract.",
                "suggested_commit_message": "docs(dbc): verify Design by Contract compliance",
            }
        elif "acceptance_criteria" in lowered and "specification" in lowered:
            return {
                "title": "Software Project",
                "description": "Project specification (parsed from initial_spec.md).",
                "acceptance_criteria": ["See specification document"],
                "constraints": [],
                "priority": "medium",
            }
        elif (
            "integration expert" in lowered
            and "backend code" in lowered
            and "frontend code" in lowered
        ):
            return {
                "issues": [],
                "passed": True,
                "summary": "Backend and frontend API contract aligned (dummy).",
                "fix_task_suggestions": [],
            }
        elif "acceptance criteria verifier" in lowered and "per_criterion" in lowered:
            return {
                "per_criterion": [
                    {
                        "criterion": "Criterion 1",
                        "satisfied": True,
                        "evidence": "Code implements the requirement.",
                    }
                ],
                "all_satisfied": True,
                "summary": "All acceptance criteria satisfied (dummy).",
            }
        # Nutrition & Meal Planning: intake profile prompt
        elif "client profile" in lowered and (
            "dietary_needs" in lowered
            or "household" in lowered
            or "produce a single complete client profile" in lowered
        ):
            return {
                "household": {
                    "number_of_people": 2,
                    "description": "couple",
                    "ages_if_relevant": [],
                },
                "dietary_needs": ["vegetarian"],
                "allergies_and_intolerances": [],
                "lifestyle": {
                    "max_cooking_time_minutes": 30,
                    "lunch_context": "remote",
                    "equipment_constraints": [],
                    "other_constraints": "",
                },
                "preferences": {
                    "cuisines_liked": [],
                    "cuisines_disliked": [],
                    "ingredients_disliked": [],
                    "preferences_free_text": "",
                },
                "goals": {"goal_type": "maintain", "notes": ""},
            }
        # Nutrition: nutrition plan / meal planning prompts (minimal valid structure)
        elif "nutrition" in lowered and "plan" in lowered:
            return {
                "daily_calories": 2000,
                "macros": {"protein": 100, "carbs": 200, "fat": 67},
                "meals_per_day": 3,
                "notes": "Dummy nutrition plan.",
            }
        elif "meal" in lowered and ("suggestions" in lowered or "recommendations" in lowered):
            return {"suggestions": [{"meal": "Dummy meal", "reason": "Dummy reason"}]}
        elif (
            system_prompt
            and "senior software engineer" in system_prompt.lower()
            and "files_to_create_or_edit" in system_prompt.lower()
        ):
            th = self._extract_task_hint(prompt)
            return {
                "summary": f"Implemented (dummy): {th}",
                "files_to_create_or_edit": [
                    {"path": "dummy_impl.txt", "content": f"# dummy implementation for {th}\n"}
                ],
                "commands_run": [],
                "ready_for_review": True,
            }
        # Blogging: plan-critic report (token lives in the user prompt tail)
        elif "plancriticreport" in lowered or "return a single plancriticreport" in lowered:
            return {
                "status": "PASS",
                "approved": True,
                "violations": [],
                "notes": "Dummy plan critic: rubber-stamp PASS for tests.",
                "rubric_version": "v1",
            }
        # Blogging: structured content plan JSON (planning agent; token in user prompt)
        elif "content_plan_json_v1" in lowered:
            return {
                "overarching_topic": "Dummy blog topic",
                "narrative_flow": "Open with context, develop the core idea, close with actions.",
                "sections": [
                    {
                        "title": "Introduction",
                        "coverage_description": "Hook and problem framing.",
                        "order": 0,
                        "research_support_note": "Supported by research digest.",
                        "gap_flag": False,
                    },
                    {
                        "title": "Core ideas",
                        "coverage_description": "Main substance from sources.",
                        "order": 1,
                        "research_support_note": None,
                        "gap_flag": False,
                    },
                    {
                        "title": "Conclusion",
                        "coverage_description": "Recap and one next step.",
                        "order": 2,
                        "research_support_note": None,
                        "gap_flag": False,
                    },
                    {
                        "title": "Further reading",
                        "coverage_description": "Optional pointers (keeps section count in band for standard_article).",
                        "order": 3,
                        "research_support_note": None,
                        "gap_flag": False,
                    },
                ],
                "title_candidates": [
                    {
                        "title": "Dummy Title: Why This Topic Matters",
                        "probability_of_success": 0.72,
                    },
                    {"title": "A Practical Take on the Topic", "probability_of_success": 0.58},
                ],
                "requirements_analysis": {
                    "plan_acceptable": True,
                    "scope_feasible": True,
                    "research_gaps": [],
                    "fits_profile": True,
                    "gaps": [],
                    "risks": [],
                    "suggested_format_change": None,
                },
                "plan_version": 1,
            }
        return {"output": "Dummy response", "status": "ok"}

    def chat_json_round(
        self,
        messages: list,
        *,
        temperature: float = 0.2,
        tools: Optional[list] = None,
        think: bool = False,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Support tool-loop tests and Strands structured-output flows.

        Behavior, in order of precedence:

        1. **Strands structured output**: when ``tools`` contains a tool whose
           description marks it as a ``StructuredOutputTool`` (the sentinel
           Strands' ``Agent`` adds when ``structured_output_model=...`` is
           used), return a single tool call invoking that tool with the dict
           produced by the normal ``complete_json`` pattern matcher. This
           lets Strands-migrated agents run end-to-end against the dummy
           client without changes.

        2. **Legacy tool loop** (first round, ``tools`` provided): emit a
           no-op ``git_status`` tool call. Test suites for
           ``complete_json_with_tool_loop`` register a ``git_status`` handler
           and expect this handoff.

        3. **Follow-up rounds or no tools**: fall through to
           ``complete_json`` using the flattened user + system prompts.
        """
        self._request_count += 1
        system_prompt = None
        user_prompt = ""
        for m in messages:
            if m.get("role") == "system":
                system_prompt = m.get("content")
            elif m.get("role") == "user":
                user_prompt = m.get("content") or ""

        has_tool_result = any(m.get("role") == "tool" for m in messages)

        if tools and not has_tool_result:
            structured_tool = None
            for t in tools:
                fn = (t or {}).get("function") or {}
                desc = (fn.get("description") or "").lower()
                if "structuredoutputtool" in desc:
                    structured_tool = fn
                    break

            if structured_tool is not None:
                # Produce stub data via the pattern matcher and invoke the
                # structured output tool with it. Strands will validate the
                # arguments against the Pydantic schema attached to the tool.
                data = self.complete_json(
                    user_prompt,
                    temperature=temperature,
                    system_prompt=system_prompt,
                    tools=None,
                    think=think,
                    **kwargs,
                )
                return {
                    "__tool_calls__": [
                        {
                            "id": f"dummy_{structured_tool.get('name', 'structured')}",
                            "type": "function",
                            "function": {
                                "name": structured_tool.get("name", "structured_output"),
                                "arguments": data,
                            },
                        }
                    ]
                }

            # Legacy path — tests that drive ``complete_json_with_tool_loop``
            # rely on this first-round git_status handoff.
            return {
                "__tool_calls__": [
                    {
                        "id": "dummy_git_status",
                        "type": "function",
                        "function": {"name": "git_status", "arguments": {}},
                    }
                ]
            }

        return self.complete_json(
            user_prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            tools=None,
            think=think,
            **kwargs,
        )
