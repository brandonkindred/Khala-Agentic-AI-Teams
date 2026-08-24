"""
Integration tests for codegen-team routing through the main orchestrator.

Verifies that:
1. Tasks assigned to 'backend-code-v2'/'frontend-code-v2' are routed to the
   codegen team's worker for the matching stack.
2. Task parsing accepts backend-code-v2/frontend-code-v2 as valid assignees.
3. No imports from backend_agent, frontend_team, or feature_agent exist in
   codegen_team.
4. CodegenTeamLead constructs for both stacks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from shared.dev_models.models import TaskType  # noqa: E402
from software_engineering_team.shared.task_parsing import (  # noqa: E402
    _assignee_to_task_type,
    parse_assignment_from_data,
)


class TestTaskParsingBackendCodeV2:
    """Verify that backend-code-v2 assignee is accepted and routed correctly."""

    def test_assignee_to_task_type_maps_backend_code_v2(self):
        assert _assignee_to_task_type("backend-code-v2") == TaskType.BACKEND

    def test_assignee_to_task_type_maps_backend(self):
        assert _assignee_to_task_type("backend") == TaskType.BACKEND

    def test_parse_assignment_with_backend_code_v2(self):
        data = {
            "tasks": [
                {
                    "id": "bv2-auth-api",
                    "title": "Auth API",
                    "type": "backend",
                    "assignee": "backend-code-v2",
                    "description": "Implement auth endpoints",
                    "user_story": "As a dev",
                    "requirements": "",
                    "acceptance_criteria": ["login works"],
                    "dependencies": [],
                },
                {
                    "id": "frontend-shell",
                    "title": "App Shell",
                    "type": "frontend",
                    "assignee": "frontend",
                    "description": "Angular shell",
                    "user_story": "As a user",
                    "requirements": "",
                    "acceptance_criteria": [],
                    "dependencies": [],
                },
            ],
            "execution_order": ["bv2-auth-api", "frontend-shell"],
            "rationale": "test",
        }
        assignment = parse_assignment_from_data(data)
        bv2_tasks = [t for t in assignment.tasks if t.assignee == "backend-code-v2"]
        assert len(bv2_tasks) == 1
        assert bv2_tasks[0].id == "bv2-auth-api"

    def test_execution_order_preserves_backend_code_v2(self):
        data = {
            "tasks": [
                {
                    "id": "bv2-1",
                    "type": "backend",
                    "assignee": "backend-code-v2",
                    "description": "a",
                    "dependencies": [],
                },
                {
                    "id": "be-1",
                    "type": "backend",
                    "assignee": "backend",
                    "description": "b",
                    "dependencies": [],
                },
                {
                    "id": "fe-1",
                    "type": "frontend",
                    "assignee": "frontend",
                    "description": "c",
                    "dependencies": [],
                },
            ],
            "execution_order": ["bv2-1", "be-1", "fe-1"],
            "rationale": "",
        }
        assignment = parse_assignment_from_data(data)
        assert "bv2-1" in assignment.execution_order
        assert "be-1" in assignment.execution_order
        assert "fe-1" in assignment.execution_order


class TestTaskParsingFrontendCodeV2:
    def test_assignee_to_task_type_maps_frontend_code_v2(self):
        assert _assignee_to_task_type("frontend-code-v2") == TaskType.FRONTEND

    def test_assignee_to_task_type_maps_frontend(self):
        assert _assignee_to_task_type("frontend") == TaskType.FRONTEND

    def test_parse_assignment_with_frontend_code_v2(self):
        data = {
            "tasks": [
                {
                    "id": "fv2-login",
                    "title": "Login UI",
                    "type": "frontend",
                    "assignee": "frontend-code-v2",
                    "description": "Implement login component",
                    "user_story": "As a user",
                    "requirements": "",
                    "acceptance_criteria": ["form submits"],
                    "dependencies": [],
                },
                {
                    "id": "be-api",
                    "title": "Auth API",
                    "type": "backend",
                    "assignee": "backend-code-v2",
                    "description": "Auth endpoints",
                    "user_story": "As a dev",
                    "requirements": "",
                    "acceptance_criteria": [],
                    "dependencies": [],
                },
            ],
            "execution_order": ["be-api", "fv2-login"],
            "rationale": "test",
        }
        assignment = parse_assignment_from_data(data)
        fv2_tasks = [t for t in assignment.tasks if t.assignee == "frontend-code-v2"]
        assert len(fv2_tasks) == 1
        assert fv2_tasks[0].id == "fv2-login"


class TestNoLegacyTeamImports:
    """Verify zero backend_agent/frontend_team/feature_agent imports in codegen_team."""

    def test_no_import_statements(self):
        team_dir = Path(__file__).resolve().parent.parent / "codegen_team"
        assert team_dir.is_dir(), f"codegen_team not found at {team_dir}"

        banned = ("backend_agent", "frontend_team", "feature_agent")
        violations: list[str] = []
        for py_file in team_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                is_import = stripped.startswith("from ") or stripped.startswith("import ")
                if is_import and any(name in stripped for name in banned):
                    violations.append(f"{py_file.relative_to(team_dir)}:{i}: {stripped}")

        assert not violations, "Found banned legacy-team imports in codegen_team:\n" + "\n".join(
            violations
        )


class TestOrchestratorRegistration:
    """Verify that CodegenTeamLead constructs for both stacks."""

    @pytest.mark.parametrize("stack", ["backend", "frontend"])
    def test_codegen_team_lead_constructs_for_stack(self, stack):
        from llm_service.clients.dummy import DummyLLMClient
        from software_engineering_team.codegen_team import CodegenTeamLead

        agent = CodegenTeamLead(DummyLLMClient(), stack=stack)
        assert isinstance(agent, CodegenTeamLead)
        assert agent.stack == stack
