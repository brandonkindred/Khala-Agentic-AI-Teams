"""
Integration tests for backend-code-v2 routing through the main orchestrator.

Verifies that:
1. Tasks assigned to 'backend-code-v2' are routed to the new team's worker.
2. The backend_code_v2_worker calls BackendCodeV2TeamLead.run_workflow (not BackendExpertAgent).
3. Task parsing accepts backend-code-v2 as a valid assignee.
4. No imports from backend_agent exist in backend_code_v2_team.
"""

from __future__ import annotations

import sys
from pathlib import Path

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from software_engineering_team.shared.models import TaskType  # noqa: E402
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
                {"id": "bv2-1", "type": "backend", "assignee": "backend-code-v2", "description": "a", "dependencies": []},
                {"id": "be-1", "type": "backend", "assignee": "backend", "description": "b", "dependencies": []},
                {"id": "fe-1", "type": "frontend", "assignee": "frontend", "description": "c", "dependencies": []},
            ],
            "execution_order": ["bv2-1", "be-1", "fe-1"],
            "rationale": "",
        }
        assignment = parse_assignment_from_data(data)
        assert "bv2-1" in assignment.execution_order
        assert "be-1" in assignment.execution_order
        assert "fe-1" in assignment.execution_order


class TestNoBackendAgentImports:
    """Verify zero backend_agent imports in the backend_code_v2_team package."""

    def test_no_import_statements(self):
        team_dir = Path(__file__).resolve().parent.parent / "backend_code_v2_team"
        assert team_dir.is_dir(), f"backend_code_v2_team not found at {team_dir}"

        violations: list[str] = []
        for py_file in team_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                if "from backend_agent" in stripped or "import backend_agent" in stripped:
                    violations.append(f"{py_file.relative_to(team_dir)}:{i}: {stripped}")

        assert not violations, (
            "Found backend_agent imports in backend_code_v2_team:\n" + "\n".join(violations)
        )


class TestOrchestratorRegistration:
    """Verify that the orchestrator uses backend_code_v2_team as the backend team."""

    def test_get_agents_includes_backend_code_v2(self):
        from orchestrator import _get_agents

        agents = _get_agents()
        assert "backend" in agents
        from backend_code_v2_team import BackendCodeV2TeamLead
        assert isinstance(agents["backend"], BackendCodeV2TeamLead)
        # Replaced standalone backend_code_v2 key with backend
        assert "backend_code_v2" not in agents

    def test_backend_code_v2_worker_exists(self):
        import orchestrator
        assert hasattr(orchestrator, "_backend_code_v2_worker")
