"""Tests for task parsing: flat and hierarchical formats."""


from software_engineering_team.shared.models import TaskType
from software_engineering_team.shared.task_parsing import (
    flatten_hierarchy_to_assignment,
    parse_assignment_from_data,
    parse_hierarchy_from_data,
)


def test_parse_flat_assignment() -> None:
    """parse_assignment_from_data handles flat tasks list."""
    data = {
        "tasks": [
            {
                "id": "backend-models",
                "title": "Data models",
                "type": "backend",
                "assignee": "backend",
                "description": "Create models",
                "user_story": "As a dev",
                "requirements": "",
                "acceptance_criteria": [],
                "dependencies": [],
            },
            {
                "id": "frontend-app-shell",
                "title": "App shell",
                "type": "frontend",
                "assignee": "frontend",
                "description": "Create app shell",
                "user_story": "As a dev",
                "requirements": "",
                "acceptance_criteria": [],
                "dependencies": [],
            },
        ],
        "execution_order": ["backend-models", "frontend-app-shell"],
        "rationale": "Simple plan",
    }
    assignment = parse_assignment_from_data(data)
    assert len(assignment.tasks) == 2
    assert assignment.execution_order == ["backend-models", "frontend-app-shell"]
    assert assignment.tasks[0].type == TaskType.BACKEND
    assert assignment.tasks[1].type == TaskType.FRONTEND


def test_parse_hierarchical_assignment() -> None:
    """parse_assignment_from_data handles initiative/epic/story hierarchy."""
    data = {
        "initiatives": [
            {
                "id": "init-1",
                "title": "Build App",
                "description": "Main initiative",
                "epics": [
                    {
                        "id": "epic-users",
                        "title": "User Management",
                        "description": "User CRUD",
                        "user_stories_summary": ["User can register"],
                        "acceptance_criteria": ["Users can be created"],
                        "stories": [
                            {
                                "id": "backend-user-api",
                                "title": "User API",
                                "description": "REST API for users",
                                "user_story": "As a dev, I want user endpoints",
                                "assignee": "backend",
                                "requirements": "FastAPI",
                                "acceptance_criteria": ["POST /users works"],
                                "dependencies": [],
                            },
                            {
                                "id": "frontend-user-form",
                                "title": "User Form",
                                "description": "Registration form",
                                "user_story": "As a user, I want to register",
                                "assignee": "frontend",
                                "requirements": "Angular",
                                "acceptance_criteria": ["Form submits"],
                                "dependencies": ["backend-user-api"],
                            },
                        ],
                    },
                ],
            },
        ],
        "execution_order": ["backend-user-api", "frontend-user-form"],
        "rationale": "Users first",
    }
    assignment = parse_assignment_from_data(data)
    assert len(assignment.tasks) == 2
    assert assignment.tasks[0].id == "backend-user-api"
    assert assignment.tasks[0].type == TaskType.BACKEND
    assert assignment.tasks[0].metadata["epic_id"] == "epic-users"
    assert assignment.tasks[0].metadata["initiative_id"] == "init-1"
    assert assignment.tasks[1].id == "frontend-user-form"
    assert assignment.tasks[1].type == TaskType.FRONTEND


def test_hierarchy_story_metadata_flows_to_task() -> None:
    """Story metadata (e.g. framework_target) is merged into Task.metadata."""
    data = {
        "initiatives": [
            {
                "id": "init-1",
                "title": "App",
                "epics": [
                    {
                        "id": "epic-1",
                        "title": "UI",
                        "stories": [
                            {
                                "id": "frontend-dashboard",
                                "title": "Dashboard",
                                "assignee": "frontend",
                                "description": "React dashboard",
                                "metadata": {"framework_target": "react"},
                            },
                        ],
                    },
                ],
            },
        ],
        "execution_order": ["frontend-dashboard"],
        "rationale": "",
    }
    assignment = parse_assignment_from_data(data)
    assert len(assignment.tasks) == 1
    t = assignment.tasks[0]
    assert t.metadata.get("framework_target") == "react"
    assert t.metadata.get("epic_id") == "epic-1"
    assert t.metadata.get("initiative_id") == "init-1"


def test_flat_task_metadata_preserved() -> None:
    """Flat format tasks with metadata preserve it."""
    data = {
        "tasks": [
            {
                "id": "frontend-login",
                "title": "Login",
                "assignee": "frontend",
                "description": "Login page",
                "metadata": {"framework_target": "vue"},
            },
        ],
        "execution_order": ["frontend-login"],
        "rationale": "",
    }
    assignment = parse_assignment_from_data(data)
    assert len(assignment.tasks) == 1
    assert assignment.tasks[0].metadata.get("framework_target") == "vue"


def test_parse_hierarchy_from_data() -> None:
    """parse_hierarchy_from_data produces a PlanningHierarchy."""
    data = {
        "initiatives": [
            {
                "id": "init-1",
                "title": "Build App",
                "description": "Main",
                "epics": [
                    {
                        "id": "epic-1",
                        "title": "Feature",
                        "stories": [
                            {
                                "id": "story-1",
                                "title": "Backend work",
                                "assignee": "backend",
                            },
                        ],
                    },
                ],
            },
        ],
        "execution_order": ["story-1"],
        "rationale": "Simple",
    }
    hierarchy = parse_hierarchy_from_data(data)
    assert len(hierarchy.initiatives) == 1
    assert len(hierarchy.initiatives[0].epics) == 1
    assert len(hierarchy.initiatives[0].epics[0].stories) == 1
    assert hierarchy.execution_order == ["story-1"]


def test_flatten_hierarchy_deduplicates() -> None:
    """flatten_hierarchy_to_assignment skips duplicate story IDs."""
    hierarchy = parse_hierarchy_from_data({
        "initiatives": [
            {
                "id": "init-1",
                "title": "App",
                "epics": [
                    {
                        "id": "epic-1",
                        "title": "Feature",
                        "stories": [
                            {"id": "s1", "title": "Story", "assignee": "backend"},
                            {"id": "s1", "title": "Duplicate", "assignee": "backend"},
                        ],
                    },
                ],
            },
        ],
        "execution_order": ["s1"],
    })
    assignment = flatten_hierarchy_to_assignment(hierarchy)
    assert len(assignment.tasks) == 1


def test_assignee_trusted_from_llm() -> None:
    """LLM-provided assignee is trusted directly, no normalization."""
    data = {
        "tasks": [
            {
                "id": "frontend-app-init",
                "title": "Initialize frontend app",
                "type": "backend",
                "assignee": "backend",
                "description": "Create Angular app",
                "user_story": "As a dev",
                "requirements": "",
                "acceptance_criteria": [],
                "dependencies": [],
            },
        ],
        "execution_order": ["frontend-app-init"],
        "rationale": "",
    }
    assignment = parse_assignment_from_data(data)
    assert len(assignment.tasks) == 1
    t = assignment.tasks[0]
    assert t.type == TaskType.BACKEND
    assert t.assignee == "backend"


def test_execution_order_preserves_llm_order() -> None:
    """execution_order from LLM is preserved as-is."""
    data = {
        "tasks": [
            {"id": "a", "assignee": "backend", "description": "A"},
            {"id": "b", "assignee": "frontend", "description": "B"},
            {"id": "c", "assignee": "backend", "description": "C"},
        ],
        "execution_order": ["a", "b", "c"],
    }
    assignment = parse_assignment_from_data(data)
    assert assignment.execution_order == ["a", "b", "c"]


def test_initiative_epic_story_tasks_four_levels() -> None:
    """When stories contain a 'tasks' array, each task becomes an assignable unit; execution_order uses task IDs."""
    data = {
        "initiatives": [
            {
                "id": "init-1",
                "title": "Build App",
                "description": "Main initiative",
                "epics": [
                    {
                        "id": "epic-auth",
                        "title": "Authentication",
                        "description": "User auth feature",
                        "user_stories_summary": ["User can log in"],
                        "acceptance_criteria": ["Login works"],
                        "stories": [
                            {
                                "id": "story-login",
                                "title": "Login flow",
                                "description": "End-to-end login: UI and API.",
                                "acceptance_criteria": ["User can log in with email/password"],
                                "example": "User enters email and password; POST /auth/login returns token.",
                                "tasks": [
                                    {
                                        "id": "backend-auth-login-api",
                                        "title": "Login API",
                                        "description": "POST /auth/login with email/password; return JWT.",
                                        "user_story": "As a client, I want a login endpoint",
                                        "assignee": "backend",
                                        "requirements": "FastAPI, JWT",
                                        "acceptance_criteria": ["POST returns 200 with token", "Invalid credentials return 401"],
                                        "dependencies": [],
                                        "example": '{"email":"u@e.com","password":"p"} -> {"token":"..."}',
                                    },
                                    {
                                        "id": "frontend-login-form",
                                        "title": "Login form",
                                        "description": "Form that calls login API and stores token.",
                                        "user_story": "As a user, I want to log in",
                                        "assignee": "frontend",
                                        "requirements": "Angular form",
                                        "acceptance_criteria": ["Form submits to API", "Token stored"],
                                        "dependencies": ["backend-auth-login-api"],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        ],
        "execution_order": ["backend-auth-login-api", "frontend-login-form"],
        "rationale": "Backend first, then frontend.",
    }
    assignment = parse_assignment_from_data(data)
    assert len(assignment.tasks) == 2
    assert assignment.tasks[0].id == "backend-auth-login-api"
    assert assignment.tasks[0].assignee == "backend"
    assert assignment.tasks[0].metadata.get("story_id") == "story-login"
    assert assignment.tasks[1].id == "frontend-login-form"
    assert assignment.tasks[1].assignee == "frontend"
    assert assignment.tasks[1].dependencies == ["backend-auth-login-api"]
    assert assignment.execution_order == ["backend-auth-login-api", "frontend-login-form"]
