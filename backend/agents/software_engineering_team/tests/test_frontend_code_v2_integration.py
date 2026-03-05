"""
Integration tests for frontend-code-v2 routing and task parsing.

Verifies that:
1. Tasks assigned to 'frontend-code-v2' are accepted by task parsing.
2. No imports from frontend_team or feature_agent exist in frontend_code_v2_team.
3. Orchestrator registers frontend_code_v2 team.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from software_engineering_team.shared.models import Task, TaskStatus, TaskType
from software_engineering_team.shared.task_parsing import parse_assignment_from_data, _assignee_to_task_type


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


class TestNoFrontendTeamImports:
    """Verify zero frontend_team/feature_agent imports in frontend_code_v2_team."""

    def test_no_import_statements(self):
        team_dir = Path(__file__).resolve().parent.parent / "frontend_code_v2_team"
        assert team_dir.is_dir(), f"frontend_code_v2_team not found at {team_dir}"

        violations: list[str] = []
        for py_file in team_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                # Only flag actual import statements, not docstrings that mention the names
                if (stripped.startswith("from ") and ("frontend_team" in stripped or "feature_agent" in stripped)):
                    violations.append(f"{py_file.relative_to(team_dir)}:{i}: {stripped}")
                if (stripped.startswith("import ") and ("frontend_team" in stripped or "feature_agent" in stripped)):
                    violations.append(f"{py_file.relative_to(team_dir)}:{i}: {stripped}")

        assert not violations, (
            f"Found frontend_team/feature_agent imports in frontend_code_v2_team:\n" + "\n".join(violations)
        )


class TestOrchestratorRegistration:
    def test_get_agents_includes_frontend_code_v2(self):
        from orchestrator import _get_agents
        from frontend_code_v2_team import FrontendCodeV2TeamLead

        agents = _get_agents()
        assert "frontend_code_v2" in agents
        assert isinstance(agents["frontend_code_v2"], FrontendCodeV2TeamLead)

    def test_frontend_code_v2_worker_exists(self):
        import orchestrator
        assert hasattr(orchestrator, "_frontend_code_v2_worker")
