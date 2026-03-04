"""
LLM client abstraction for software engineering team agents.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from abc import ABC, abstractmethod
import json
import re
from typing import Any, Callable, Dict, Optional, Union

import httpx

# Environment variables for LLM configuration
ENV_LLM_PROVIDER = "SW_LLM_PROVIDER"  # "dummy" or "ollama"
ENV_LLM_MODEL = "SW_LLM_MODEL"  # model name for ollama
ENV_LLM_BASE_URL = "SW_LLM_BASE_URL"  # ollama base URL
ENV_LLM_OLLAMA_API_KEY = "SW_LLM_OLLAMA_API_KEY"  # Ollama Cloud API key (or use OLLAMA_API_KEY)
ENV_LLM_TIMEOUT = "SW_LLM_TIMEOUT"  # timeout in seconds
ENV_LLM_MAX_RETRIES = "SW_LLM_MAX_RETRIES"  # max retries for temporary errors (default 4)
ENV_LLM_BACKOFF_BASE = "SW_LLM_BACKOFF_BASE"  # base seconds for exponential backoff (default 2)
ENV_LLM_BACKOFF_MAX = "SW_LLM_BACKOFF_MAX_SECONDS"  # max backoff seconds (default 60)
ENV_LLM_MAX_CONCURRENCY = "SW_LLM_MAX_CONCURRENCY"  # max concurrent complete_json calls (default 2)
ENV_LLM_MAX_TOKENS = "SW_LLM_MAX_TOKENS"  # max tokens to generate; if unset, min(context_size, 32768)
ENV_LLM_CONTEXT_SIZE = "SW_LLM_CONTEXT_SIZE"  # context window in tokens; if unset, uses known model or Ollama /api/show
ENV_LLM_ENABLE_THINKING = "SW_LLM_ENABLE_THINKING"  # "true"/"false"; enables thinking mode for qwen3.5 models (default: true)

# Default cap for max_tokens (max output length) in API requests. Many APIs limit output to 32K even when context is 256K.
DEFAULT_MAX_OUTPUT_TOKENS = 32768

# Model max context (tokens). Effective context = max - largest agent reservation.
# 256K = 262144 (qwen models). Largest reservations: coding/review 20K (qwen3.5).
KNOWN_MODEL_CONTEXT: dict[str, int] = {
    "qwen3.5:397b": 262144,
    "qwen3.5:397b-cloud": 242144,  # 256K - 20K
    "qwen3.5:cloud": 242144,  # keep for overrides; 256K - 20K
    "qwen3-coder:480b-cloud": 242144,
    "qwen3-coder:480b": 242144,
}

# Recommended default model per agent (all :cloud versions). Used when SW_LLM_MODEL_<agent_key> and SW_LLM_MODEL are unset.
AGENT_DEFAULT_MODELS: dict[str, str] = {
    "backend": "qwen3.5:397b-cloud",
    "frontend": "qwen3.5:397b-cloud",
    "code_review": "qwen3.5:397b-cloud",
    "repair": "qwen3.5:397b-cloud",
    "devops": "qwen3.5:397b-cloud",
    "dbc_comments": "qwen3.5:397b-cloud",
    "tech_lead": "qwen3.5:397b-cloud",
    "architecture": "qwen3.5:397b-cloud",
    "spec_intake": "qwen3.5:397b-cloud",
    "spec_clarification": "qwen3.5:397b-cloud",
    "product_analysis": "qwen3.5:397b-cloud",
    "project_planning": "qwen3.5:397b-cloud",
    "integration": "qwen3.5:397b-cloud",
    "api_contract": "qwen3.5:397b-cloud",
    "data_architecture": "qwen3.5:397b-cloud",
    "ui_ux": "qwen3.5:397b-cloud",
    "frontend_architecture": "qwen3.5:397b-cloud",
    "infrastructure": "qwen3.5:397b-cloud",
    "devops_planning": "qwen3.5:397b-cloud",
    "qa_test_strategy": "qwen3.5:397b-cloud",
    "security_planning": "qwen3.5:397b-cloud",
    "observability": "qwen3.5:397b-cloud",
    "acceptance_verifier": "qwen3.5:397b-cloud",
    "documentation": "qwen3.5:397b-cloud",
    "qa": "qwen3.5:397b-cloud",
    "security": "qwen3.5:397b-cloud",
    "accessibility": "qwen3.5:397b-cloud",
}

# Cache for OllamaLLMClient instances keyed by (model, base_url, timeout)
_client_cache: dict[tuple[str, str, float], "OllamaLLMClient"] = {}
_client_cache_lock = threading.Lock()

logger = logging.getLogger(__name__)

# Message used when Ollama 429 indicates weekly usage limit exceeded (for logging and job state)
OLLAMA_WEEKLY_LIMIT_MESSAGE = "Ollama LLM usage limit exceeded for week"


# ---------------------------------------------------------------------------
# Domain-specific exceptions for LLM errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base exception for LLM-related errors."""

    def __init__(self, message: str, *, status_code: int | None = None, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.cause = cause


class LLMRateLimitError(LLMError):
    """Raised when the LLM returns 429 Too Many Requests and retries are exhausted."""


class LLMTemporaryError(LLMError):
    """Raised when the LLM returns 5xx or network errors and retries are exhausted."""


class LLMUnreachableAfterRetriesError(LLMError):
    """Raised when the frontend (or other agent) exhausted retries and could not reach the LLM. Orchestrator should pause job."""


class LLMPermanentError(LLMError):
    """Raised for 4xx errors (except 429) or malformed responses. Do not retry."""


class LLMJsonParseError(LLMPermanentError):
    """Raised when LLM returned a 200 response but the content is not valid JSON.

    Consumers can catch this specifically to trigger decomposition / retry
    strategies without catching unrelated permanent errors (e.g. HTTP 400).
    """

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "json_parse",
        response_preview: str = "",
    ):
        super().__init__(message)
        self.error_kind = error_kind
        self.response_preview = response_preview


class LLMTruncatedError(LLMError):
    """Raised when LLM response was truncated due to token limit (finish_reason=length).

    This exception signals that the response is incomplete and the caller should
    decompose the task into smaller pieces rather than attempting partial recovery.

    Attributes:
        partial_content: The truncated content returned by the LLM.
        finish_reason: The finish_reason from the API response.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        finish_reason: str = "length",
    ):
        super().__init__(message)
        self.partial_content = partial_content
        self.finish_reason = finish_reason


def get_llm_config_summary() -> str:
    """
    Return a short summary of the effective LLM provider and model from env vars.
    Used for prominent logging at startup and on each request.
    """
    provider = (os.environ.get(ENV_LLM_PROVIDER) or "ollama").lower().strip()
    if provider == "ollama":
        model = os.environ.get(ENV_LLM_MODEL) or "qwen3.5:397b-cloud"
        base_url = os.environ.get(ENV_LLM_BASE_URL) or "http://127.0.0.1:11434"
        return f"provider={provider}, model={model}, base_url={base_url}"
    return f"provider={provider}"


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

    def get_max_context_tokens(self) -> int:
        """
        Return the model's maximum context size in tokens.
        Used to compute chunk sizes for analysis. Default 16384.
        Override in implementations that can query the model (e.g. Ollama).
        """
        return 16384


# Words to strip when extracting a component/module name from a task description.
# These are verbs and filler words that describe the task, not the thing being built.
_STRIP_VERBS = {
    "implement", "create", "build", "add", "setup", "set", "up", "configure",
    "make", "define", "develop", "write", "design", "establish", "generate",
    "fetches", "displays", "handles", "manages", "processes", "returns",
    "provides", "supports", "includes", "enables", "renders",
}
_STRIP_FILLERS = {
    "the", "that", "with", "using", "which", "for", "and", "a", "an", "to",
    "of", "in", "on", "by", "from", "into", "as", "via", "its", "all",
    "application", "system", "project", "based", "proper", "production",
    "quality", "complete", "full", "new", "existing",
    "angular", "react", "vue", "spring", "fastapi", "flask", "django",
}
_STRIP_SUFFIXES = {
    "component", "service", "module", "endpoint", "endpoints", "middleware",
    "guard", "pipe", "directive", "interceptor", "controller", "repository",
}


def _extract_name_from_hint(hint: str, separator: str = "-", max_length: int = 25) -> str:
    """
    Extract a short, meaningful name from a task description hint.

    Strips leading verbs, filler words, and generic suffixes to produce
    a 1-3 word noun phrase suitable for file/folder names.

    Args:
        hint: Task description text (e.g. "Implement the UserFormComponent using reactive forms")
        separator: Word separator for the output ("-" for kebab-case, "_" for snake_case)
        max_length: Maximum length of the resulting name

    Returns:
        A short name like "user-form" or "user_form"

    Examples:
        >>> _extract_name_from_hint("Implement the UserFormComponent using Angular reactive forms")
        'user-form'
        >>> _extract_name_from_hint("Create user registration endpoint with email validation", "_")
        'user_registration'
        >>> _extract_name_from_hint("Build the task list component with pagination")
        'task-list'
    """
    # Split PascalCase/camelCase words (e.g. "UserFormComponent" -> "User Form Component")
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", hint)
    # Normalize to lowercase words
    words = re.sub(r"[^a-z0-9\s]+", " ", expanded.lower()).split()
    # Strip verbs, fillers, and generic suffixes
    filtered = [
        w for w in words
        if w not in _STRIP_VERBS and w not in _STRIP_FILLERS and w not in _STRIP_SUFFIXES
    ]
    # Take first 3 meaningful words
    name_words = filtered[:3] if filtered else words[:2]
    result = separator.join(name_words)
    # Truncate to max_length
    if len(result) > max_length:
        result = result[:max_length].rstrip(separator)
    return result or f"item{separator}1"


class DummyLLMClient(LLMClient):
    """No-op implementation for tests and environments without an LLM."""

    # Counter to generate unique file content per invocation, avoiding "no changes to commit"
    _call_counter: int = 0

    @staticmethod
    def _extract_task_hint(prompt: str) -> str:
        """Extract a task identifier from the prompt to generate unique per-task output."""
        import hashlib
        # Look for **Task:** lines in the prompt
        for line in prompt.split("\n"):
            stripped = line.strip()
            if stripped.startswith("**Task:**"):
                hint = stripped.replace("**Task:**", "").strip()[:80]
                return hint
        # Fallback: hash a portion of the prompt for uniqueness
        return hashlib.md5(prompt[:500].encode()).hexdigest()[:12]

    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        lowered = prompt.lower()
        DummyLLMClient._call_counter += 1
        counter = DummyLLMClient._call_counter
        task_hint = self._extract_task_hint(prompt)

        # Architecture prompt is more specific (architecture_document); check before Tech Lead
        if "architecture_document" in lowered and "components" in lowered and "overview" in lowered:
            return {
                "overview": "API backend + WebApp frontend (Dummy architecture).",
                "architecture_document": "# System Architecture (Dummy)\n\nPlaceholder architecture.",
                "components": [{"name": "API", "type": "backend"}, {"name": "WebApp", "type": "frontend"}],
                "diagrams": {
                    "client_server_architecture": "graph LR\n  Browser-->API\n  API-->DB",
                    "frontend_code_structure": "graph TD\n  App-->Components\n  App-->Services",
                },
                "decisions": [{"decision": "Use REST API", "context": "Standard web stack", "consequences": "Simple integration"}],
            }
        # Tech Lead codebase analysis prompt (Step 1 of multi-step planning)
        elif "codebase audit" in lowered and "files_inventory" in lowered:
            return {
                "files_inventory": [
                    {"path": "initial_spec.md", "language": "markdown", "purpose": "Project specification", "key_exports": []},
                ],
                "frameworks": {"backend": "unknown", "frontend": "unknown", "database": "unknown", "testing": "unknown", "cicd": "unknown", "other": []},
                "existing_functionality": ["Project specification document exists"],
                "partial_implementations": [],
                "gaps": ["No application code exists yet", "No backend framework set up", "No frontend framework set up", "No CI/CD pipeline", "No database configuration", "No tests"],
                "code_conventions": {"naming": "unknown", "structure": "flat", "config_approach": "unknown"},
                "summary": "The repository contains only the project specification (initial_spec.md). No application code, infrastructure, or tests exist yet. The entire application needs to be built from scratch according to the spec.",
            }
        # Tech Lead spec analysis prompt (Step 2 of multi-step planning)
        elif "deep analysis" in lowered and "total_deliverable_count" in lowered:
            return {
                "data_entities": [{"name": "User", "attributes": ["id", "email", "password_hash", "created_at"], "relationships": [], "validation_rules": ["email must be valid", "password required"]}],
                "api_endpoints": [
                    {"method": "POST", "path": "/auth/signup", "description": "Create new user account", "auth_required": False},
                    {"method": "POST", "path": "/auth/login", "description": "Authenticate user and return JWT", "auth_required": False},
                    {"method": "POST", "path": "/auth/refresh", "description": "Refresh access token", "auth_required": True},
                    {"method": "GET", "path": "/api/users/me", "description": "Get current user profile", "auth_required": True},
                ],
                "ui_screens": [
                    {"name": "Login Page", "description": "User login form", "components": ["LoginForm", "ErrorDisplay"], "states": ["idle", "loading", "error", "success"]},
                    {"name": "Registration Page", "description": "User registration form", "components": ["RegistrationForm", "ErrorDisplay"], "states": ["idle", "loading", "error", "success"]},
                    {"name": "Dashboard", "description": "Main authenticated view", "components": ["Navbar", "UserProfile"], "states": ["loading", "loaded"]},
                ],
                "user_flows": [
                    {"name": "User Registration", "steps": ["Navigate to signup", "Fill form", "Submit", "Receive confirmation", "Redirect to login"]},
                    {"name": "User Login", "steps": ["Navigate to login", "Enter credentials", "Submit", "Receive JWT", "Redirect to dashboard"]},
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
        # Tech Lead evaluate QA prompt (create fix tasks from QA feedback)
        elif "qa agent has reviewed code" in lowered and "fix tasks" in lowered:
            return {"tasks": [], "rationale": "QA approved; no fix tasks needed (dummy)."}
        # Tech Lead should run security prompt
        elif "run security review now" in lowered and "90%" in lowered:
            return {"run_security": False, "rationale": "Code coverage not yet at 90% (dummy)."}
        # Tech Lead review progress prompt
        elif "reviewing the progress" in lowered and "spec_compliance_pct" in lowered:
            return {
                "tasks": [],
                "spec_compliance_pct": 50,
                "gaps_identified": [],
                "rationale": "Progress review complete. Current tasks cover the planned scope (dummy).",
            }
        # Tech Lead refine task prompt (clarification)
        elif "clarification questions from specialist" in lowered:
            return {
                "title": "Refined Task Title",
                "description": "Refined task description with additional details from spec. The implementation should follow Angular best practices using standalone components and reactive forms. All public methods must have JSDoc documentation. Error states must be handled with user-friendly messages.",
                "user_story": "As a user, I want refined functionality so that the feature works as specified in the requirements.",
                "requirements": "Detailed requirements addressing clarification questions. Use Angular Material for UI components. Implement loading spinners during async operations. Handle HTTP errors with retry logic.",
                "acceptance_criteria": ["Criterion 1: Component renders without errors", "Criterion 2: User interactions trigger correct API calls", "Criterion 3: Error states display meaningful messages"],
            }
        # Tech Lead prompt asks for tasks + execution_order – return granular plan with descriptive IDs
        elif ("execution_order" in lowered or "task_assignments" in lowered) and "tasks" in lowered:
            return {
                "tasks": [
                    {
                        "id": "git-setup",
                        "title": "Initialize Git Development Branch",
                        "type": "git_setup",
                        "description": "Ensure the development branch exists and is properly configured for the feature branch workflow. If the development branch does not yet exist, create it from the main branch. Verify that the branch is checked out and the git history is clean with no uncommitted changes. This is the foundational step that enables all subsequent feature branches to be created from a stable base.",
                        "user_story": "As a developer, I want a dedicated development branch so that all feature branches are created from a stable integration point and merged back systematically without disrupting the main branch.",
                        "assignee": "devops",
                        "requirements": "Create development branch from main if missing. Checkout the development branch. Verify git status is clean.",
                        "acceptance_criteria": ["Development branch exists and is checked out", "Git status shows clean working directory", "Branch is created from main or master"],
                        "dependencies": [],
                    },
                    {
                        "id": "devops-dockerfile",
                        "title": "Multi-Stage Application Dockerfile with Dev and Prod Targets",
                        "type": "devops",
                        "description": "Create a multi-stage Dockerfile that supports both development and production builds for the full-stack application. The first stage should use python:3.11-slim as the base image and install all Python backend dependencies from requirements.txt. The second stage should use node:18-alpine to build the Angular frontend with ng build --configuration production. The final production stage should combine the backend ASGI server (uvicorn) with the compiled frontend static assets served from a /static directory. Include a .dockerignore file to exclude node_modules, __pycache__, .git, and test directories from the build context. The dev target should mount source code as a volume and enable hot-reload for both backend and frontend.",
                        "user_story": "As a developer, I want a multi-stage Dockerfile so that I can build optimized production images while also having a fast development workflow with hot-reload capabilities.",
                        "assignee": "devops",
                        "requirements": "Multi-stage Dockerfile with python:3.11-slim backend stage, node:18-alpine frontend build stage, and slim production stage. Include .dockerignore. Dev target with volume mounts. Production target under 200MB.",
                        "acceptance_criteria": ["Dockerfile builds successfully with docker build", "Production image size is under 200MB", "Multi-stage build separates build dependencies from runtime", "Dev target supports hot-reload via volume mounts", ".dockerignore excludes node_modules, __pycache__, .git"],
                        "dependencies": ["git-setup"],
                    },
                    {
                        "id": "devops-docker-compose",
                        "title": "Docker Compose for Local Development Environment",
                        "type": "devops",
                        "description": "Create a docker-compose.yml file that orchestrates the local development environment with three services: the FastAPI backend (port 8000), the Angular frontend dev server (port 4200), and a PostgreSQL 15 database (port 5432). The backend service should use the dev target from the Dockerfile and mount the backend source directory for hot-reload. The frontend service should run ng serve with --poll 2000 for file watching inside Docker. The database service should use a named volume for data persistence and initialize with a healthcheck. Include environment variables for database connection string, JWT secret, and CORS allowed origins. Add a .env.example file documenting all required environment variables.",
                        "user_story": "As a developer, I want a docker-compose setup so that I can spin up the entire application stack locally with a single command and have changes reflected immediately during development.",
                        "assignee": "devops",
                        "requirements": "docker-compose.yml with backend (port 8000), frontend (port 4200), and PostgreSQL 15 (port 5432) services. Named volumes for DB persistence. Environment variables via .env file. Health checks on database.",
                        "acceptance_criteria": ["docker-compose up starts all three services", "Backend is accessible at http://localhost:8000", "Frontend dev server is accessible at http://localhost:4200", "Database persists data across restarts via named volume", ".env.example documents all required environment variables"],
                        "dependencies": ["devops-dockerfile"],
                    },
                    {
                        "id": "devops-ci-pipeline",
                        "title": "GitHub Actions CI Pipeline with Lint, Test, and Build Stages",
                        "type": "devops",
                        "description": "Create a GitHub Actions CI/CD pipeline configuration at .github/workflows/ci.yml that triggers on push to the development branch and on pull requests targeting development. The pipeline should have three sequential jobs: (1) lint - run flake8 and mypy on backend Python code and ng lint on frontend TypeScript code, (2) test - run pytest with coverage reporting for backend and ng test --no-watch --code-coverage for frontend, (3) build - build the Docker production image and verify it starts without errors. Each job should use appropriate caching (pip cache, npm cache) to speed up builds. The pipeline should fail fast if any stage fails and report test coverage as a PR comment.",
                        "user_story": "As a developer, I want an automated CI pipeline so that every push and pull request is validated for code quality, test coverage, and build integrity before merging.",
                        "assignee": "devops",
                        "requirements": "GitHub Actions workflow at .github/workflows/ci.yml. Trigger on push to development and PRs. Three jobs: lint (flake8, mypy, ng lint), test (pytest --cov, ng test --code-coverage), build (docker build). Pip and npm caching. Fail-fast on errors.",
                        "acceptance_criteria": ["CI triggers on push to development branch", "CI triggers on pull requests to development", "Lint stage runs flake8, mypy, and ng lint", "Test stage runs pytest and ng test with coverage", "Build stage builds Docker image successfully", "Pipeline uses caching for pip and npm dependencies"],
                        "dependencies": ["devops-docker-compose"],
                    },
                    {
                        "id": "backend-data-models",
                        "title": "Domain Entity Data Models with Pydantic Schemas and SQLAlchemy ORM",
                        "type": "backend",
                        "description": "Define all domain entity data models using both Pydantic (for API request/response validation) and SQLAlchemy (for database ORM). Create a User model with fields: id (UUID, primary key, auto-generated), email (unique, validated format), password_hash (string, never exposed in API responses), display_name (string, 2-50 chars), is_active (boolean, default True), created_at (datetime, auto-set), and updated_at (datetime, auto-updated). Create corresponding Pydantic schemas: UserCreate (email + password + display_name), UserUpdate (optional fields), UserResponse (excludes password_hash), and UserInDB (includes password_hash for internal use). Set up the SQLAlchemy engine configuration to read DATABASE_URL from environment variables with a fallback to SQLite for development. Database models should be defined directly using SQLAlchemy declarative base. All models must enforce Design by Contract with precondition validation on field lengths and formats.",
                        "user_story": "As a backend developer, I want well-defined data models with Pydantic validation and SQLAlchemy ORM mappings so that the API can validate input data, serialize responses correctly, and persist entities to the database with referential integrity.",
                        "assignee": "backend",
                        "requirements": "User model with id (UUID), email (unique), password_hash, display_name, is_active, created_at, updated_at. Pydantic schemas: UserCreate, UserUpdate, UserResponse, UserInDB. SQLAlchemy Base with declarative models, engine, SessionLocal. DATABASE_URL from env. Design by Contract on all validators.",
                        "acceptance_criteria": ["User SQLAlchemy model maps to 'users' table with all specified fields", "UserCreate schema validates email format and password length (min 8 chars)", "UserResponse schema excludes password_hash field", "Database engine reads DATABASE_URL from environment with SQLite fallback", "All models have docstrings explaining purpose, constraints, and usage"],
                        "dependencies": ["devops-ci-pipeline"],
                    },
                    {
                        "id": "backend-crud-api",
                        "title": "RESTful CRUD API Endpoints for Domain Entities with FastAPI Router",
                        "type": "backend",
                        "description": "Implement REST CRUD API endpoints for the User entity using FastAPI's APIRouter. Create a dedicated router module at backend/routers/users.py with the following endpoints: GET /api/users (list all users with pagination via query params page=1&per_page=20, returns paginated response with total count), POST /api/users (create new user, hash password with bcrypt, return 201 with UserResponse), GET /api/users/{user_id} (get single user by UUID, return 404 if not found), PUT /api/users/{user_id} (partial update of user fields, return 404 if not found, validate input), DELETE /api/users/{user_id} (soft delete by setting is_active=False, return 204 on success, 404 if not found). Each endpoint must use dependency injection for the database session. Create a main.py FastAPI application that mounts the users router and includes CORS middleware allowing the Angular frontend origin. Include proper HTTP status codes, response models, and OpenAPI documentation tags.",
                        "user_story": "As an API consumer, I want RESTful CRUD endpoints for managing users so that the frontend application can create, list, view, update, and delete user accounts through a well-documented API.",
                        "assignee": "backend",
                        "requirements": "FastAPI APIRouter at /api/users. Endpoints: GET list (paginated), POST create (201), GET by id (404 handling), PUT update (partial), DELETE soft-delete (204). Bcrypt password hashing. Dependency-injected DB sessions. CORS middleware. OpenAPI tags.",
                        "acceptance_criteria": ["GET /api/users returns 200 with paginated list including total count", "GET /api/users?page=2&per_page=10 returns correct page", "POST /api/users with valid body returns 201 with UserResponse (no password_hash)", "POST /api/users with duplicate email returns 409 Conflict", "GET /api/users/{id} returns 404 for non-existent UUID", "PUT /api/users/{id} updates only provided fields and returns 200", "DELETE /api/users/{id} sets is_active=False and returns 204"],
                        "dependencies": ["backend-data-models"],
                    },
                    {
                        "id": "backend-validation",
                        "title": "Input Validation Middleware and Structured Error Responses",
                        "type": "backend",
                        "description": "Add comprehensive input validation and structured error handling to all API endpoints. Create a custom exception handler middleware that catches ValidationError (Pydantic), HTTPException (FastAPI), and generic exceptions, returning a consistent JSON error response format: {\"error\": {\"code\": \"VALIDATION_ERROR\", \"message\": \"Human-readable message\", \"details\": [{\"field\": \"email\", \"message\": \"Invalid email format\"}]}}. Add request ID tracking via a middleware that generates a UUID for each request and includes it in the response headers (X-Request-ID) and error responses. Implement rate limiting on the POST /api/users endpoint (max 10 requests per minute per IP) to prevent abuse. Add input sanitization to strip leading/trailing whitespace from string fields and normalize email addresses to lowercase. All validation must follow Design by Contract: document preconditions in docstrings and enforce them with explicit checks before processing.",
                        "user_story": "As an API consumer, I want clear, structured validation errors with request tracking so that I can quickly identify and fix invalid input, and as a system operator, I want rate limiting so that the API is protected from abuse.",
                        "assignee": "backend",
                        "requirements": "Custom exception handler middleware returning {error: {code, message, details}}. Request ID middleware (X-Request-ID header). Rate limiting on POST endpoints (10/min/IP). Input sanitization (trim whitespace, lowercase emails). Design by Contract on all validators.",
                        "acceptance_criteria": ["Invalid input returns 422 with structured error format including field-level details", "All error responses include X-Request-ID header", "POST /api/users is rate-limited to 10 requests per minute per IP", "Email addresses are normalized to lowercase before storage", "String fields are trimmed of leading/trailing whitespace", "Generic exceptions return 500 with error code INTERNAL_ERROR and request ID"],
                        "dependencies": ["backend-crud-api"],
                    },
                    {
                        "id": "frontend-app-shell",
                        "title": "Angular Application Shell with Routing, Layout, and Navigation Components",
                        "type": "frontend",
                        "description": "Create the Angular application shell that serves as the foundation for all frontend features. Generate the Angular project using Angular CLI with standalone components enabled. Configure the Angular Router with lazy-loaded routes for the main feature areas: '/' (redirect to /dashboard), '/login', '/register', '/dashboard', '/users', and '/users/:id'. Create a MainLayoutComponent that renders a responsive header with the application logo and title, a sidebar navigation menu with links to Dashboard and Users sections, and a footer with copyright information. The header should include a user avatar dropdown (placeholder for future auth integration) and a mobile hamburger menu that toggles the sidebar. Use Angular Material (MatToolbar, MatSidenav, MatList, MatIcon) for consistent Material Design styling. Create an AppShellModule that declares the layout components and exports them for use by feature modules. Include a loading bar component (MatProgressBar) at the top of the page that activates during route transitions.",
                        "user_story": "As a user, I want a well-structured application with clear navigation so that I can easily move between the dashboard, user list, and detail views without confusion, and the interface feels responsive on both desktop and mobile devices.",
                        "assignee": "frontend",
                        "requirements": "Angular CLI project with standalone components. Lazy-loaded routes: /, /login, /register, /dashboard, /users, /users/:id. MainLayoutComponent with MatToolbar header, MatSidenav sidebar, footer. Mobile-responsive hamburger menu. Angular Material theming. Loading bar on route transitions. AppShellModule.",
                        "acceptance_criteria": ["Angular app loads without errors at http://localhost:4200", "Router navigates between /dashboard, /users, /users/:id views", "MainLayoutComponent renders header with logo, sidebar with nav links, and footer", "Sidebar collapses to hamburger menu on mobile viewport (< 768px)", "Route transitions show MatProgressBar loading indicator", "Lazy loading is configured for feature modules (verified in network tab)"],
                        "dependencies": ["devops-ci-pipeline"],
                    },
                    {
                        "id": "frontend-list-component",
                        "title": "User List View Component with API Integration, Pagination, and State Management",
                        "type": "frontend",
                        "description": "Implement the UserListComponent that fetches user data from the backend REST API (GET /api/users) and displays it in a Material Design data table. The component must handle three distinct UI states: (1) Loading state - display a MatSpinner centered on the page while the API request is in flight, (2) Empty state - show an illustration with 'No users found' message and a 'Create First User' call-to-action button when the API returns an empty list, (3) Data state - render a MatTable with columns for display_name, email, is_active (as a colored badge), and created_at (formatted as relative time, e.g. '2 hours ago'). Implement pagination using MatPaginator with page sizes [10, 20, 50] and default page size of 20. Each row should be clickable, navigating to /users/:id. Add a search bar at the top that filters by email or display_name (debounced 300ms, calls API with query param). Create a UserService (injectable) that encapsulates all HTTP calls to /api/users and returns Observables. Handle HTTP errors by displaying a MatSnackBar with the error message and a 'Retry' action.",
                        "user_story": "As a user, I want to see a paginated list of all users with search functionality so that I can quickly find and select a user to view their details, even when there are hundreds of records.",
                        "assignee": "frontend",
                        "requirements": "UserListComponent with MatTable, MatPaginator, MatSpinner, empty state. UserService (injectable) for GET /api/users with pagination and search query params. Debounced search (300ms). Three UI states: loading, empty, data. Row click navigates to /users/:id. Error handling with MatSnackBar.",
                        "acceptance_criteria": ["UserListComponent displays data from GET /api/users in a MatTable", "Loading state shows MatSpinner while API request is pending", "Empty state shows 'No users found' message with 'Create First User' button", "MatPaginator supports page sizes [10, 20, 50] with default 20", "Clicking a table row navigates to /users/:id", "Search bar filters by email/display_name with 300ms debounce", "HTTP errors display MatSnackBar with error message and Retry action"],
                        "dependencies": ["frontend-app-shell", "backend-crud-api"],
                    },
                    {
                        "id": "frontend-form-component",
                        "title": "User Create/Edit Form with Reactive Validation and API Submission",
                        "type": "frontend",
                        "description": "Implement the UserFormComponent using Angular Reactive Forms that serves both create and edit modes. In create mode (route: /users/new), display a form with fields: display_name (required, 2-50 chars), email (required, valid email format), and password (required, min 8 chars, must contain uppercase, lowercase, and number). In edit mode (route: /users/:id/edit), pre-populate the form by fetching the user from GET /api/users/:id, and make the password field optional (only update if provided). Each field should show inline validation errors as the user types (not on pristine fields). The submit button should be disabled when the form is invalid. On submit, call POST /api/users (create) or PUT /api/users/:id (update) via the UserService. Show a MatProgressBar during submission. On success, display a MatSnackBar confirmation message and navigate back to the user list. On API error (e.g. 409 duplicate email), display the server error message inline under the relevant field. Include a 'Cancel' button that navigates back without saving.",
                        "user_story": "As a user, I want a validated form to create and edit user accounts so that I receive immediate feedback on input errors before submitting, and the form clearly communicates success or failure after submission.",
                        "assignee": "frontend",
                        "requirements": "UserFormComponent with Angular Reactive Forms. Create mode (/users/new) and edit mode (/users/:id/edit). Fields: display_name (required, 2-50 chars), email (required, email format), password (required for create, optional for edit, min 8 chars with complexity). Inline validation errors. Disabled submit when invalid. MatProgressBar during submission. Success/error MatSnackBar. Cancel navigation. Server error mapping to fields.",
                        "acceptance_criteria": ["Create form at /users/new validates all required fields before enabling submit", "Edit form at /users/:id/edit pre-populates with existing user data", "Inline validation errors appear after field is touched (not on pristine)", "Password field enforces min 8 chars with uppercase, lowercase, and number", "Submit button is disabled when form is invalid", "Successful create shows 'User created' snackbar and navigates to user list", "API error 409 shows 'Email already exists' inline under email field", "Cancel button navigates back without saving"],
                        "dependencies": ["frontend-list-component"],
                    },
                    {
                        "id": "frontend-detail-component",
                        "title": "User Detail View with Profile Display and Delete Confirmation Dialog",
                        "type": "frontend",
                        "description": "Implement the UserDetailComponent that displays comprehensive user information on a dedicated page (route: /users/:id). Fetch the user from GET /api/users/:id and display all fields in a Material Design card layout: display_name as the card title, email with a mailto link, account status (is_active) as a colored chip (green for active, red for inactive), created_at and updated_at formatted as locale-specific dates. Include an 'Edit' button that navigates to /users/:id/edit and a 'Delete' button styled in warn color. When the Delete button is clicked, open a MatDialog confirmation with the message 'Are you sure you want to delete {display_name}? This action cannot be undone.' with 'Cancel' and 'Delete' action buttons. On confirmation, call DELETE /api/users/:id via the UserService. On success, show a MatSnackBar 'User deleted' and navigate back to the user list. On error, show the error message in a MatSnackBar. Handle the case where the user ID does not exist (404) by showing a 'User not found' message with a 'Back to list' link.",
                        "user_story": "As a user, I want to view complete user profile details and have the ability to edit or delete the account with a confirmation step so that I can manage individual users confidently without accidental deletions.",
                        "assignee": "frontend",
                        "requirements": "UserDetailComponent at /users/:id. Material card layout with all user fields. Edit button -> /users/:id/edit. Delete button opens MatDialog confirmation. DELETE /api/users/:id on confirm. Success snackbar + navigate to list. 404 handling with 'User not found' message.",
                        "acceptance_criteria": ["Detail view displays all user fields in a Material card", "Email is rendered as a clickable mailto link", "Active status shows as green chip, inactive as red chip", "Edit button navigates to /users/:id/edit", "Delete button opens MatDialog with confirmation message", "Confirming delete calls DELETE /api/users/:id and navigates to list", "404 response shows 'User not found' with 'Back to list' link", "Created/updated dates are formatted in locale-specific format"],
                        "dependencies": ["frontend-list-component"],
                    },
                ],
                "execution_order": [
                    "git-setup", "devops-dockerfile", "devops-docker-compose", "devops-ci-pipeline",
                    "backend-data-models", "backend-crud-api", "backend-validation",
                    "frontend-app-shell", "frontend-list-component", "frontend-form-component", "frontend-detail-component",
                ],
                "rationale": "Granular plan following dependency order: git setup, then devops (Dockerfile -> Compose -> CI), then backend (models -> CRUD -> validation), then frontend (shell -> list -> form/detail). Security and QA are invoked automatically by the orchestrator after coding tasks. Each task is scoped to a single focused deliverable with detailed descriptions enabling autonomous implementation.",
                "summary": "11 tasks covering full spec: git (1), devops (3), backend (3), frontend (4). Every spec requirement is mapped to specific tasks with detailed descriptions, user stories, and testable acceptance criteria. The plan follows strict dependency ordering so each task can build on completed work.",
                "requirement_task_mapping": [
                    {"spec_item": "User data model with email, password, profile fields", "task_ids": ["backend-data-models"]},
                    {"spec_item": "REST CRUD API for user management", "task_ids": ["backend-crud-api"]},
                    {"spec_item": "Input validation and error handling", "task_ids": ["backend-validation"]},
                    {"spec_item": "Docker containerization", "task_ids": ["devops-dockerfile", "devops-docker-compose"]},
                    {"spec_item": "CI/CD pipeline", "task_ids": ["devops-ci-pipeline"]},
                    {"spec_item": "Angular application with routing", "task_ids": ["frontend-app-shell"]},
                    {"spec_item": "User list view with pagination", "task_ids": ["frontend-list-component"]},
                    {"spec_item": "User create/edit form with validation", "task_ids": ["frontend-form-component"]},
                    {"spec_item": "User detail view with delete", "task_ids": ["frontend-detail-component"]},
                    {"spec_item": "See specification document", "task_ids": ["backend-data-models", "backend-crud-api", "backend-validation", "frontend-app-shell", "frontend-list-component", "frontend-form-component", "frontend-detail-component"]},
                ],
                "clarification_questions": [],
            }
        # Code review agent
        elif "senior code reviewer" in lowered and ("approved" in lowered or "issues" in lowered):
            return {
                "approved": True,
                "issues": [],
                "summary": "Code review passed (dummy). Code meets basic standards and follows project conventions.",
                "spec_compliance_notes": "Code aligns with task requirements and acceptance criteria.",
                "suggested_commit_message": "",
            }
        elif "security" in lowered and "vulnerabilities" in lowered:
            return {
                "vulnerabilities": [],
                "summary": "No security issues found (dummy)",
            }
        elif "accessibility" in lowered and "wcag" in lowered and "issues" in lowered:
            return {
                "issues": [],
                "summary": "No WCAG 2.2 accessibility issues found (dummy)",
            }
        # Backend agent – generate unique files per task based on task hint and counter
        # NOTE: Uses the agent's unique role identifier from BACKEND_PROMPT to avoid
        # matching DevOps/Frontend prompts that share CODING_STANDARDS keywords.
        elif "senior backend software engineer" in lowered:
            # Derive a short module name from the task hint (e.g. "user_registration")
            slug = _extract_name_from_hint(task_hint, separator="_", max_length=25) or f"module_{counter}"
            class_prefix = slug.title().replace("_", "")
            return {
                "code": f'"""\nBackend module: {task_hint}\nGenerated as task #{counter}\n"""\nfrom fastapi import APIRouter, Depends, HTTPException\nfrom pydantic import BaseModel\n\nrouter = APIRouter(prefix="/api", tags=["{slug}"])\n\n\nclass {class_prefix}Request(BaseModel):\n    """Request model for {task_hint}."""\n    name: str\n\n\nclass {class_prefix}Response(BaseModel):\n    """Response model for {task_hint}."""\n    id: int\n    name: str\n\n\n@router.get("/{slug}")\ndef list_items():\n    """List all items for {task_hint}."""\n    return []\n\n\n@router.post("/{slug}", status_code=201)\ndef create_item(data: {class_prefix}Request):\n    """Create item for {task_hint}."""\n    return {class_prefix}Response(id=1, name=data.name)\n',
                "language": "python",
                "summary": f"Backend implementation for: {task_hint}",
                "files": {
                    f"app/routers/{slug}.py": f'"""\nBackend module: {task_hint}\n"""\nfrom fastapi import APIRouter, HTTPException\nfrom pydantic import BaseModel\n\nrouter = APIRouter(prefix="/api", tags=["{slug}"])\n\n\nclass ItemRequest(BaseModel):\n    name: str\n\n\n@router.get("/{slug}")\ndef list_items():\n    return []\n\n\n@router.post("/{slug}", status_code=201)\ndef create_item(data: ItemRequest):\n    return {{"id": 1, "name": data.name}}\n',
                    f"tests/test_{slug}.py": f'"""Tests for {task_hint}."""\nimport pytest\n\n\ndef test_{slug}_list():\n    """Test list endpoint returns empty list."""\n    assert [] == []\n\n\ndef test_{slug}_create():\n    """Test create returns correct structure."""\n    result = {{"id": 1, "name": "test"}}\n    assert result["id"] == 1\n',
                },
                "tests": f'"""Tests for {task_hint}."""\nimport pytest\n\n\ndef test_{slug}():\n    assert True\n',
                "suggested_commit_message": f"feat(api): implement {slug.replace('_', ' ')}",
            }
        # Frontend agent – generate unique Angular files per task based on task hint and counter
        # NOTE: Uses the agent's unique role identifier from FRONTEND_PROMPT to avoid
        # matching DevOps prompts that share CODING_STANDARDS keywords.
        elif "senior frontend software engineer" in lowered:
            # Derive a short component name from the task hint (e.g. "user-form")
            slug = _extract_name_from_hint(task_hint, separator="-", max_length=25) or f"component-{counter}"
            class_name = "".join(w.capitalize() for w in slug.split("-")) + "Component"
            selector = f"app-{slug}"
            return {
                "code": f"import {{ Component, OnInit }} from '@angular/core';\nimport {{ CommonModule }} from '@angular/common';\n\n/**\n * {class_name}\n * Implements: {task_hint}\n * Generated as task #{counter}\n */\n@Component({{\n  selector: '{selector}',\n  standalone: true,\n  imports: [CommonModule],\n  template: `\n    <div class=\"{slug}-container\">\n      <h2>{task_hint}</h2>\n      <p>Component implementation placeholder</p>\n    </div>\n  `,\n  styles: [`\n    .{slug}-container {{\n      padding: 16px;\n      max-width: 1200px;\n      margin: 0 auto;\n    }}\n  `]\n}})\nexport class {class_name} implements OnInit {{\n  /** Initialize the component and load data. */\n  ngOnInit(): void {{\n    console.log('{class_name} initialized');\n  }}\n}}\n",
                "summary": f"Frontend component for: {task_hint}",
                "files": {
                    f"src/app/components/{slug}/{slug}.component.ts": f"import {{ Component, OnInit }} from '@angular/core';\nimport {{ CommonModule }} from '@angular/common';\n\n@Component({{\n  selector: '{selector}',\n  standalone: true,\n  imports: [CommonModule],\n  template: `<div class=\"{slug}\"><h2>{task_hint}</h2></div>`,\n}})\nexport class {class_name} implements OnInit {{\n  ngOnInit(): void {{\n    console.log('{class_name} initialized');\n  }}\n}}\n",
                    f"src/app/components/{slug}/{slug}.component.spec.ts": f"import {{ ComponentFixture, TestBed }} from '@angular/core/testing';\nimport {{ {class_name} }} from './{slug}.component';\n\ndescribe('{class_name}', () => {{\n  let component: {class_name};\n  let fixture: ComponentFixture<{class_name}>;\n\n  beforeEach(async () => {{\n    await TestBed.configureTestingModule({{\n      imports: [{class_name}],\n    }}).compileComponents();\n    fixture = TestBed.createComponent({class_name});\n    component = fixture.componentInstance;\n    fixture.detectChanges();\n  }});\n\n  it('should create', () => {{\n    expect(component).toBeTruthy();\n  }});\n}});\n",
                },
                "components": [class_name],
                "suggested_commit_message": f"feat(ui): add {slug} component",
            }
        # DevOps agent – generate unique content per task using counter
        elif "devops" in lowered or "pipeline" in lowered:
            return {
                "pipeline_yaml": f"# CI Pipeline (task #{counter})\nname: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: echo 'Build step {counter}'",
                "iac_content": f"# Infrastructure as Code (task #{counter})\nresource \"docker_image\" \"app\" {{\n  name = \"app:latest\"\n}}",
                "dockerfile": f"# Dockerfile (task #{counter})\nFROM python:3.11-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\nCOPY . .\nEXPOSE 8000\nCMD [\"uvicorn\", \"main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]",
                "docker_compose": f"# Docker Compose (task #{counter})\nversion: '3.8'\nservices:\n  backend:\n    build: .\n    ports:\n      - '8000:8000'\n  db:\n    image: postgres:15\n    ports:\n      - '5432:5432'",
                "summary": f"DevOps configuration generated for: {task_hint[:60]}",
                "suggested_commit_message": f"ci: add devops configuration (task #{counter})",
            }
        # Documentation agent - README prompt
        elif "technical writer" in lowered and "readme_content" in lowered and "readme_changed" in lowered:
            return {
                "readme_content": f"# Project\n\nAuto-generated documentation (task #{counter}).\n\n## Prerequisites\n\n- Python 3.11+\n- Node 18+\n- Docker\n\n## Installation\n\n```bash\npip install -r requirements.txt\n```\n\n## Running\n\n```bash\nuvicorn main:app --reload\n```\n\n## Testing\n\n```bash\npytest\n```\n\n## Deployment\n\nSee docker-compose.yml\n",
                "readme_changed": True,
                "summary": f"Updated README with project setup and usage instructions (task #{counter})",
                "suggested_commit_message": f"docs(readme): update project documentation (task #{counter})",
            }
        # Documentation agent - contributors prompt
        elif "contributors.md" in lowered and "contributors_content" in lowered and "contributors_changed" in lowered:
            return {
                "contributors_content": f"# Contributors\n\n| Agent | Role | Contributions |\n|-------|------|---------------|\n| Backend Agent | Backend Engineer | API endpoints, data models |\n| Frontend Agent | Frontend Engineer | Angular components, UI |\n| DevOps Agent | Infrastructure | Docker, CI/CD |\n| Documentation Agent | Technical Writer | README, docs |\n",
                "contributors_changed": True,
                "summary": f"Updated contributors list (task #{counter})",
            }
        # Tech Lead trigger documentation prompt
        elif "documentation update needed" in lowered and "should_update_docs" in lowered:
            return {
                "should_update_docs": True,
                "rationale": "Task completed with code changes that affect project setup or usage (dummy).",
            }
        # DbC Comments agent
        elif "design by contract" in lowered and "comments_added" in lowered and "already_compliant" in lowered:
            return {
                "files": {},
                "comments_added": 0,
                "comments_updated": 0,
                "already_compliant": True,
                "summary": "All code fully complies with Design by Contract principles. Excellent documentation!",
                "suggested_commit_message": "docs(dbc): verify Design by Contract compliance",
            }
        elif "integration_test" in lowered or "readme_content" in lowered or ("bugs_found" in lowered and "test_plan" in lowered):
            return {
                "bugs_found": [],
                "integration_tests": "# Dummy integration test",
                "unit_tests": "# Dummy unit tests for 85% coverage",
                "test_plan": "Dummy test plan",
                "summary": "Dummy QA assessment",
                "live_test_notes": "Dummy live test notes",
                "readme_content": "# Dummy README - build, run, test, deploy sections",
                "suggested_commit_message": "test: add integration tests",
                "approved": True,
            }
        # Spec parsing prompt
        elif "acceptance_criteria" in lowered and "specification" in lowered:
            return {
                "title": "Software Project",
                "description": "Project specification (parsed from initial_spec.md).",
                "acceptance_criteria": ["See specification document"],
                "constraints": [],
                "priority": "medium",
            }
        # Integration agent – validate backend-frontend API contract
        elif "integration expert" in lowered and "backend code" in lowered and "frontend code" in lowered:
            return {
                "issues": [],
                "passed": True,
                "summary": "Backend and frontend API contract aligned (dummy).",
                "fix_task_suggestions": [],
            }
        # Acceptance verifier agent – per-criterion verification
        elif "acceptance criteria verifier" in lowered and "per_criterion" in lowered:
            return {
                "per_criterion": [
                    {"criterion": "Criterion 1", "satisfied": True, "evidence": "Code implements the requirement."},
                    {"criterion": "Criterion 2", "satisfied": True, "evidence": "Code implements the requirement."},
                ],
                "all_satisfied": True,
                "summary": "All acceptance criteria satisfied (dummy).",
            }
        return {"output": "Dummy response", "status": "ok"}


def _parse_retry_config() -> tuple[int, float, float]:
    """Parse retry-related environment variables. Returns (max_retries, backoff_base, backoff_max)."""
    try:
        max_retries = int(os.environ.get(ENV_LLM_MAX_RETRIES) or "4")
    except ValueError:
        max_retries = 4
    try:
        backoff_base = float(os.environ.get(ENV_LLM_BACKOFF_BASE) or "2")
    except ValueError:
        backoff_base = 2.0
    try:
        backoff_max = float(os.environ.get(ENV_LLM_BACKOFF_MAX) or "60")
    except ValueError:
        backoff_max = 60.0
    return max_retries, backoff_base, backoff_max


def call_llm_with_retries(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    backoff_max: float = 60.0,
) -> Any:
    """
    Call fn() up to max_attempts times with exponential backoff on connection/temporary errors.
    Used by the frontend team so that no fallback runs when LLM is unreachable.
    On permanent/rate-limit errors, re-raises immediately. After exhausting retries, raises
    LLMUnreachableAfterRetriesError so the caller can return a structured result (e.g. llm_unreachable=True).
    """
    last_error: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except (LLMPermanentError, LLMRateLimitError, LLMUnreachableAfterRetriesError):
            raise
        except (LLMTemporaryError, httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout, LLMError) as e:
            last_error = e
            if attempt < max_attempts - 1:
                wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s. Next step -> Retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    e,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "LLM call exhausted. Recovery summary: attempted %d calls with exponential backoff, "
                    "all failed. Final error: %s",
                    max_attempts, e,
                )
                raise LLMUnreachableAfterRetriesError(
                    f"LLM unreachable after {max_attempts} attempts: {e}",
                    cause=e,
                ) from e
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1:
                wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s. Next step -> Retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    e,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "LLM call exhausted. Recovery summary: attempted %d calls with exponential backoff, "
                    "all failed. Final error: %s",
                    max_attempts, e,
                )
                raise LLMUnreachableAfterRetriesError(
                    f"LLM unreachable after {max_attempts} attempts: {e}",
                    cause=e,
                ) from e
    if last_error:
        raise LLMUnreachableAfterRetriesError(
            f"LLM unreachable after {max_attempts} attempts: {last_error}",
            cause=last_error,
        ) from last_error
    raise LLMUnreachableAfterRetriesError(f"LLM unreachable after {max_attempts} attempts")


def _get_llm_concurrency_limit() -> int:
    """Return max concurrent complete_json calls from env (default 4)."""
    try:
        return max(1, int(os.environ.get(ENV_LLM_MAX_CONCURRENCY) or "4"))
    except ValueError:
        return 4


# Module-level semaphore for Ollama LLM concurrency (shared across client instances)
_ollama_semaphore: threading.BoundedSemaphore | None = None


def _get_ollama_semaphore() -> threading.BoundedSemaphore:
    """Lazily create the global Ollama concurrency semaphore."""
    global _ollama_semaphore
    if _ollama_semaphore is None:
        limit = _get_llm_concurrency_limit()
        _ollama_semaphore = threading.BoundedSemaphore(limit)
    return _ollama_semaphore


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
        self._model_num_ctx: int | None = None  # cached from /api/show

    def _ollama_auth_headers(self) -> dict[str, str]:
        """Return Authorization Bearer header for Ollama Cloud when API key is set."""
        key = os.environ.get(ENV_LLM_OLLAMA_API_KEY) or os.environ.get("OLLAMA_API_KEY")
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}"}

    def _fetch_model_num_ctx(self) -> int:
        """Fetch model's num_ctx from env, known model table, or Ollama /api/show. Cached per client. Fallback 16384 on failure."""
        if self._model_num_ctx is not None:
            return self._model_num_ctx
        env_ctx = os.environ.get(ENV_LLM_CONTEXT_SIZE)
        if env_ctx:
            try:
                self._model_num_ctx = max(2048, int(env_ctx))
                logger.info("LLM context size from %s: %s", ENV_LLM_CONTEXT_SIZE, self._model_num_ctx)
                return self._model_num_ctx
            except ValueError:
                pass
        if self.model in KNOWN_MODEL_CONTEXT:
            self._model_num_ctx = KNOWN_MODEL_CONTEXT[self.model]
            logger.info("LLM model %s: using known context size %s", self.model, self._model_num_ctx)
            return self._model_num_ctx
        try:
            url = f"{self.base_url}/api/show"
            headers = self._ollama_auth_headers()
            with httpx.Client(timeout=min(30, self.timeout)) as client:
                resp = client.post(url, json={"model": self.model}, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "Ollama /api/show returned %s for model %s; using max_tokens=16384",
                    resp.status_code,
                    self.model,
                )
                self._model_num_ctx = 16384
                return self._model_num_ctx
            data = resp.json()
            # parameters is a string like "temperature 0.7\nnum_ctx 8192\n..."
            params_str = data.get("parameters") or ""
            match = re.search(r"num_ctx\s+(\d+)", params_str, re.IGNORECASE)
            if match:
                ctx = int(match.group(1))
                self._model_num_ctx = max(2048, ctx)  # ensure minimum 2048
                logger.info("Ollama model %s num_ctx=%s; using as max_tokens", self.model, self._model_num_ctx)
                return self._model_num_ctx
            # Try model_info.parameter_size or details for fallback
            for path in ("model_info", "details"):
                obj = data.get(path)
                if isinstance(obj, dict):
                    ctx = obj.get("num_ctx") or obj.get("context_length")
                    if ctx is not None:
                        self._model_num_ctx = max(2048, int(ctx))
                        logger.info("Ollama model %s context=%s; using as max_tokens", self.model, self._model_num_ctx)
                        return self._model_num_ctx
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            logger.warning(
                "Could not fetch Ollama model info for %s: %s; using max_tokens=16384",
                self.model,
                e,
            )
        self._model_num_ctx = 16384
        return self._model_num_ctx

    def get_max_context_tokens(self) -> int:
        """Return model's num_ctx from Ollama /api/show (cached)."""
        return self._fetch_model_num_ctx()

    def _repair_json(self, s: str) -> str:
        """Attempt tolerant JSON repair for common LLM output issues."""
        # Remove trailing commas before ] or }
        s = re.sub(r",\s*([}\]])", r"\1", s)
        return s

    def _extract_json(self, text: str) -> Dict[str, Any]:
        # #region agent log
        _log_path = "/home/deepthought/Dev/strands-agents/.cursor/debug-c656da.log"
        try:
            _log = {"sessionId": "c656da", "timestamp": int(time.time() * 1000), "location": "llm.py:_extract_json", "message": "extract_json_input", "data": {"text_len": len(text), "text_tail_100": (text[-100:] if len(text) > 100 else text)}, "hypothesisId": "D"}
            os.makedirs(os.path.dirname(_log_path), exist_ok=True)
            with open(_log_path, "a") as _f:
                _f.write(json.dumps(_log) + "\n")
        except Exception:
            pass
        # #endregion
        if "---DRAFT---" in text:
            parts = text.split("---DRAFT---", 1)
            if len(parts) == 2 and parts[1].strip():
                return {"content": parts[1].strip()}
        # Prefer a ```json block so we don't use a non-JSON block (e.g. ```bash) as the content
        json_block_match = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
        if json_block_match:
            text = json_block_match.group(1).strip()
        else:
            fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.DOTALL | re.IGNORECASE)
            if fenced_match:
                block_content = fenced_match.group(1).strip()
                # Only use first block if it looks like JSON (starts with { or [)
                if block_content.lstrip().startswith(("{", "[")):
                    text = block_content
                # else: keep full response so object extraction / try-every-block can find JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            # #region agent log
            try:
                _err = {"sessionId": "c656da", "timestamp": int(time.time() * 1000), "location": "llm.py:_extract_json_parse_err", "message": "primary_loads_failed", "data": {"error_type": type(e).__name__, "error_msg": str(e)[:300]}, "hypothesisId": "D"}
                with open(_log_path, "a") as _f2:
                    _f2.write(json.dumps(_err) + "\n")
            except Exception:
                pass
            # #endregion
            logger.debug("Primary JSON parse failed; attempting repair")
        repaired = self._repair_json(text)
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            logger.debug("Repaired JSON parse failed; attempting object extraction fallback")
        obj_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if obj_match:
            raw = obj_match.group(0)
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                try:
                    return json.loads(self._repair_json(raw))
                except (json.JSONDecodeError, ValueError):
                    logger.debug("Object extraction JSON parse failed; trying noise-stripped retry")
        # Retry once after stripping common leading/trailing noise
        stripped = text.strip()
        for pattern in (
            r"^(?:Here(?:'s| is) (?:the )?JSON:?)\s*",
            r"^(?:The (?:response|output|result) is:?)\s*",
            r"^(?:JSON:?)\s*",
            r"^\s*```(?:json)?\s*",
            r"\s*```\s*$",
        ):
            stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE).strip()
        if stripped and stripped != text.strip():
            obj_match2 = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
            if obj_match2:
                try:
                    return json.loads(obj_match2.group(0))
                except (json.JSONDecodeError, ValueError):
                    pass

        # Try every markdown code block: parse each as JSON and use first that yields a useful dict
        _EXPECTED_KEYS = frozenset({
            "files", "summary", "code", "overview", "issues", "approved", "components",
            "architecture_document", "diagrams", "decisions",
            "tasks", "execution_order",  # Task Generator / Tech Lead output
            "bugs_found", "integration_tests", "unit_tests", "readme_content",  # QA agent output
        })
        for block_match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
            block = block_match.group(1).strip()
            if not block:
                continue
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict) and _EXPECTED_KEYS & set(parsed.keys()):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                try:
                    parsed = json.loads(self._repair_json(block))
                    if isinstance(parsed, dict) and _EXPECTED_KEYS & set(parsed.keys()):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    continue

        logger.debug(
            "Raw LLM response that failed all JSON extraction strategies (first 2000 chars):\n%s",
            text[:2000],
        )
        raise LLMJsonParseError(
            "Could not parse structured JSON from LLM response. "
            "Model returned invalid or non-JSON output. "
            f"Response preview: {text[:500]!r}...",
            error_kind="json_parse",
            response_preview=text[:500],
        )

    def _should_enable_thinking(self) -> bool:
        """Check if thinking mode should be enabled for this model.
        
        Thinking mode is enabled for qwen3.5 models by default, but can be
        controlled via the SW_LLM_ENABLE_THINKING environment variable.
        """
        env_val = os.environ.get(ENV_LLM_ENABLE_THINKING, "").lower()
        if env_val == "false":
            return False
        if env_val == "true":
            return "qwen3.5" in self.model.lower()
        # Default: enable for qwen3.5 models
        return "qwen3.5" in self.model.lower()

    def complete_json(self, prompt: str, *, temperature: float = 0.0) -> Dict[str, Any]:
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        sem = _get_ollama_semaphore()

        logger.info("LLM request: provider=ollama model=%s base_url=%s", self.model, self.base_url)
        # If the model uses a code block, put only the JSON object inside it with no surrounding text.
        system_message = (
            "You are a strict JSON generator. Respond with a single valid JSON object only, "
            "no explanatory text, no Markdown, no code fences. "
            "If you use a code block, put only the JSON object inside it with no surrounding text."
        )
        env_max = os.environ.get(ENV_LLM_MAX_TOKENS)
        if env_max:
            max_tokens = int(env_max)
        else:
            # Use context size but cap at DEFAULT_MAX_OUTPUT_TOKENS; many APIs limit output to 32K.
            max_tokens = min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS)
        # Cap at DEFAULT_MAX_OUTPUT_TOKENS so we never exceed typical API output limits (e.g. 32768)
        max_tokens = min(max_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},  # OpenAI-compatible JSON mode
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
        }
        # Enable thinking mode for qwen3.5 models (improves reasoning quality)
        if self._should_enable_thinking():
            payload["think"] = True
            logger.debug("Thinking mode enabled for model %s", self.model)
        content = self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)
        return self._extract_json(content)

    def complete_text(self, prompt: str, *, temperature: float = 0.0) -> str:
        """Return raw text from the model (no JSON mode). Use for template-based output."""
        max_retries, backoff_base, backoff_max = _parse_retry_config()
        sem = _get_ollama_semaphore()
        logger.info("LLM request (text): provider=ollama model=%s base_url=%s", self.model, self.base_url)
        env_max = os.environ.get(ENV_LLM_MAX_TOKENS)
        max_tokens = min(
            int(env_max) if env_max else min(self._fetch_model_num_ctx(), DEFAULT_MAX_OUTPUT_TOKENS),
            DEFAULT_MAX_OUTPUT_TOKENS,
        )
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }
        # Enable thinking mode for qwen3.5 models (improves reasoning quality)
        if self._should_enable_thinking():
            payload["think"] = True
            logger.debug("Thinking mode enabled for model %s", self.model)
        return self._ollama_post(payload, max_retries, backoff_base, backoff_max, sem)

    def complete_json_with_continuation(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_continuation_cycles: int = 5,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Complete JSON request with automatic continuation on truncation.

        This method attempts to get a complete JSON response. If the response
        is truncated (finish_reason=length), it uses multi-turn conversation
        to continue the response up to max_continuation_cycles times.

        Args:
            prompt: The prompt to send.
            temperature: Sampling temperature.
            max_continuation_cycles: Max continuation attempts before raising.
            task_id: Optional task identifier for logging continuation responses.

        Returns:
            Parsed JSON dict.

        Raises:
            LLMTruncatedError: If truncated and continuation fails.
            LLMJsonParseError: If JSON parsing fails.
        """
        from software_engineering_team.shared.continuation import ResponseContinuator, ContinuationResult

        try:
            return self.complete_json(prompt, temperature=temperature)
        except LLMTruncatedError as e:
            logger.info(
                "Response truncated (%d chars). Attempting continuation (max %d cycles)",
                len(e.partial_content),
                max_continuation_cycles,
            )

            system_message = (
                "You are a strict JSON generator. Respond with a single valid JSON object only, "
                "no explanatory text, no Markdown, no code fences. "
                "If you use a code block, put only the JSON object inside it with no surrounding text."
            )

            continuator = ResponseContinuator(
                base_url=self.base_url,
                model=self.model,
                timeout=self.timeout,
                max_cycles=max_continuation_cycles,
            )

            result: ContinuationResult = continuator.attempt_continuation(
                original_prompt=prompt,
                partial_content=e.partial_content,
                system_prompt=system_message,
                json_mode=True,
                task_id=task_id or "sw_eng_json",
            )

            if result.success:
                logger.info(
                    "Continuation succeeded after %d cycles (%d chars total)",
                    result.cycles_used,
                    len(result.content),
                )
                return self._extract_json(result.content)

            logger.warning(
                "Continuation exhausted after %d cycles (%d chars). Re-raising truncation error.",
                result.cycles_used,
                len(result.content),
            )
            raise LLMTruncatedError(
                f"Response still truncated after {result.cycles_used} continuation cycles",
                partial_content=result.content,
                finish_reason="length",
            )

    def complete_text_with_continuation(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_continuation_cycles: int = 5,
        task_id: Optional[str] = None,
    ) -> str:
        """Complete text request with automatic continuation on truncation.

        Args:
            prompt: The prompt to send.
            temperature: Sampling temperature.
            max_continuation_cycles: Max continuation attempts before raising.
            task_id: Optional task identifier for logging continuation responses.

        Returns:
            Complete text response.

        Raises:
            LLMTruncatedError: If truncated and continuation fails.
        """
        from software_engineering_team.shared.continuation import ResponseContinuator, ContinuationResult

        try:
            return self.complete_text(prompt, temperature=temperature)
        except LLMTruncatedError as e:
            logger.info(
                "Text response truncated (%d chars). Attempting continuation (max %d cycles)",
                len(e.partial_content),
                max_continuation_cycles,
            )

            continuator = ResponseContinuator(
                base_url=self.base_url,
                model=self.model,
                timeout=self.timeout,
                max_cycles=max_continuation_cycles,
            )

            result: ContinuationResult = continuator.attempt_continuation(
                original_prompt=prompt,
                partial_content=e.partial_content,
                system_prompt=None,
                json_mode=False,
                task_id=task_id or "sw_eng_text",
            )

            if result.success:
                logger.info(
                    "Text continuation succeeded after %d cycles (%d chars total)",
                    result.cycles_used,
                    len(result.content),
                )
                return result.content

            logger.warning(
                "Text continuation exhausted after %d cycles (%d chars). Re-raising.",
                result.cycles_used,
                len(result.content),
            )
            raise LLMTruncatedError(
                f"Response still truncated after {result.cycles_used} continuation cycles",
                partial_content=result.content,
                finish_reason="length",
            )

    def _ollama_post(
        self,
        payload: dict,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        sem: Any,
    ) -> str:
        """POST to chat completions; return raw content string. Raises on non-200 or malformed response."""
        url = f"{self.base_url}/v1/chat/completions"
        # #region agent log
        _log_path = "/home/deepthought/Dev/strands-agents/.cursor/debug-c656da.log"
        try:
            _msgs = payload.get("messages") or []
            _total = sum(len(m.get("content") or "") for m in _msgs if isinstance(m, dict))
            _log = {"sessionId": "c656da", "timestamp": int(time.time() * 1000), "location": "llm.py:_ollama_post", "message": "ollama_request", "data": {"url": url, "model": payload.get("model"), "messages_count": len(_msgs), "content_chars": _total}, "hypothesisId": "A"}
            os.makedirs(os.path.dirname(_log_path), exist_ok=True)
            with open(_log_path, "a") as _f:
                _f.write(json.dumps(_log) + "\n")
        except Exception:
            pass
        # #endregion
        last_error: Exception | None = None
        headers = self._ollama_auth_headers()
        for attempt in range(max_retries + 1):
            try:
                with sem:
                    with httpx.Client(timeout=self.timeout) as client:
                        response = client.post(url, json=payload, headers=headers)
                        status = response.status_code
                        # #region agent log
                        if status >= 500:
                            try:
                                _body = (response.text or "")[:500]
                                _log2 = {"sessionId": "c656da", "timestamp": int(time.time() * 1000), "location": "llm.py:_ollama_post_5xx", "message": "ollama_5xx_response", "data": {"status": status, "response_body": _body}, "hypothesisId": "C"}
                                os.makedirs(os.path.dirname(_log_path), exist_ok=True)
                                with open(_log_path, "a") as _f2:
                                    _f2.write(json.dumps(_log2) + "\n")
                            except Exception:
                                pass
                        # #endregion
                        if status == 200:
                            try:
                                data = response.json()
                            except json.JSONDecodeError as e:
                                raise LLMPermanentError(f"Malformed LLM response (invalid JSON): {e}") from e
                            return self._parse_response_content(data)
                        if status == 429:
                            last_error = LLMRateLimitError(
                                f"LLM rate limited (429) after {attempt + 1} attempt(s)",
                                status_code=429,
                            )
                            wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                            if attempt < max_retries:
                                logger.warning(
                                    "LLM 429 rate limited (attempt %d/%d). Next step -> Retrying in %.1fs",
                                    attempt + 1, max_retries + 1, wait,
                                )
                                time.sleep(wait)
                                continue
                            raise last_error
                        if 500 <= status < 600:
                            last_error = LLMTemporaryError(
                                f"LLM server error {status} after {attempt + 1} attempt(s): {response.text[:200]}",
                                status_code=status,
                            )
                            if attempt < max_retries:
                                wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                                logger.warning(
                                    "LLM %d server error (attempt %d/%d). Next step -> Retrying in %.1fs",
                                    status, attempt + 1, max_retries + 1, wait,
                                )
                                time.sleep(wait)
                                continue
                            raise last_error
                        if 400 <= status < 500:
                            err_text = response.text[:500]
                            if status == 404 and ("not found" in err_text.lower() or "model" in err_text.lower()):
                                raise LLMPermanentError(
                                    f"LLM model not found (404). The API at {self.base_url} does not have model "
                                    f"'{self.model}'. Set SW_LLM_MODEL to a model your API supports. Original: {err_text[:200]}",
                                    status_code=status,
                                )
                            raise LLMPermanentError(f"LLM client error {status}: {err_text}", status_code=status)
                        raise LLMPermanentError(
                            f"Unexpected LLM response status {status}: {response.text[:200]}",
                            status_code=status,
                        )
            except (LLMPermanentError, LLMRateLimitError, LLMTemporaryError):
                raise
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response else None
                if status == 429:
                    last_error = LLMRateLimitError(str(e), status_code=429, cause=e)
                    if attempt < max_retries:
                        wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                        time.sleep(wait)
                        continue
                    raise last_error
                if status and 500 <= status < 600:
                    last_error = LLMTemporaryError(str(e), status_code=status, cause=e)
                    if attempt < max_retries:
                        wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                        time.sleep(wait)
                        continue
                    raise last_error
                if status and 400 <= status < 500:
                    raise LLMPermanentError(str(e), status_code=status, cause=e)
                raise LLMPermanentError(str(e), status_code=status, cause=e)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout) as e:
                last_error = LLMTemporaryError(f"LLM connection/timeout error: {e}", cause=e)
                if attempt < max_retries:
                    wait = min(backoff_base ** attempt + random.uniform(0, 1), backoff_max)
                    logger.warning(
                        "LLM connection error (attempt %d/%d): %s. Next step -> Retrying in %.1fs",
                        attempt + 1, max_retries + 1, type(e).__name__, wait,
                    )
                    time.sleep(wait)
                    continue
                raise last_error
        if last_error:
            logger.error(
                "LLM request failed. Recovery summary: 1) Attempted %d requests with exponential backoff, "
                "2) All attempts exhausted. Final error: %s",
                max_retries + 1, last_error,
            )
            raise last_error
        raise LLMTemporaryError("LLM request failed after all retries")

    def _parse_response_content(self, data: dict) -> str:
        """Extract content from Ollama/OpenAI-compatible response.

        Raises:
            LLMTruncatedError: If finish_reason indicates token limit truncation.
            LLMPermanentError: If response format is malformed.
        """
        try:
            choices = data.get("choices")
            if not choices or not isinstance(choices, list):
                raise LLMPermanentError("Unexpected response format from LLM: missing or invalid 'choices'")
            first = choices[0]
            if not isinstance(first, dict):
                raise LLMPermanentError("Unexpected response format from LLM: invalid choice object")

            finish_reason = first.get("finish_reason", "")
            if finish_reason == "length":
                msg = first.get("message", {})
                partial_content = msg.get("content", "") if isinstance(msg, dict) else ""
                logger.warning(
                    "LLM response truncated (finish_reason=length). Partial content: %d chars",
                    len(partial_content),
                )
                raise LLMTruncatedError(
                    "Response truncated due to token limit (finish_reason=length)",
                    partial_content=str(partial_content) if partial_content else "",
                    finish_reason=finish_reason,
                )

            msg = first.get("message")
            if not msg or not isinstance(msg, dict):
                raise LLMPermanentError("Unexpected response format from LLM: missing or invalid 'message'")
            content = msg.get("content")
            if content is None:
                raise LLMPermanentError("Unexpected response format from LLM: missing 'content'")
            return str(content)
        except (LLMPermanentError, LLMTruncatedError):
            raise
        except (KeyError, IndexError, TypeError) as e:
            raise LLMPermanentError(f"Unexpected response format from LLM: {e}") from e


def get_llm_for_agent(agent_key: str) -> Union["DummyLLMClient", "OllamaLLMClient"]:
    """
    Create or return cached LLM client for the given agent.

    Model resolution order:
    1. SW_LLM_MODEL_<agent_key> (e.g. SW_LLM_MODEL_backend)
    2. SW_LLM_MODEL (global fallback)
    3. AGENT_DEFAULT_MODELS[agent_key] (recommended default)
    4. qwen3.5:397b-cloud (hardcoded fallback)

    When SW_LLM_PROVIDER=dummy, returns DummyLLMClient regardless of model config.
    OllamaLLMClient instances are cached by (model, base_url, timeout).
    """
    provider = (os.environ.get(ENV_LLM_PROVIDER) or "ollama").lower().strip()
    if provider == "dummy":
        return DummyLLMClient()

    per_agent = os.environ.get(f"SW_LLM_MODEL_{agent_key}")
    global_model = os.environ.get(ENV_LLM_MODEL)
    default_model = AGENT_DEFAULT_MODELS.get(agent_key)
    model = (per_agent or global_model or default_model or "qwen3.5:397b-cloud").strip()

    base_url = os.environ.get(ENV_LLM_BASE_URL) or "http://127.0.0.1:11434"
    try:
        timeout = float(os.environ.get(ENV_LLM_TIMEOUT) or "1800")
    except ValueError:
        timeout = 1800.0

    cache_key = (model, base_url, timeout)
    with _client_cache_lock:
        if cache_key not in _client_cache:
            _client_cache[cache_key] = OllamaLLMClient(model=model, base_url=base_url, timeout=timeout)
        return _client_cache[cache_key]


def _clear_client_cache_for_testing() -> None:
    """Clear the per-agent client cache. For use in tests only."""
    with _client_cache_lock:
        _client_cache.clear()


def get_llm_client() -> Union["DummyLLMClient", "OllamaLLMClient"]:
    """
    Create LLM client from environment configuration.

    Environment variables:
    - SW_LLM_PROVIDER: "ollama" (default) or "dummy"
    - SW_LLM_MODEL: model name for ollama (default: qwen3.5:397b-cloud)
    - SW_LLM_BASE_URL: ollama base URL (default: http://127.0.0.1:11434)
    - SW_LLM_TIMEOUT: timeout in seconds (default: 1800)
    """
    provider = (os.environ.get(ENV_LLM_PROVIDER) or "ollama").lower().strip()
    if provider == "ollama":
        model = os.environ.get(ENV_LLM_MODEL) or "qwen3.5:397b-cloud"
        base_url = os.environ.get(ENV_LLM_BASE_URL) or "http://127.0.0.1:11434"
        try:
            timeout = float(os.environ.get(ENV_LLM_TIMEOUT) or "1800")
        except ValueError:
            timeout = 1800.0
        client = OllamaLLMClient(model=model, base_url=base_url, timeout=timeout)
        logger.info("LLM config: %s", get_llm_config_summary())
        return client
    logger.info("LLM config: %s", get_llm_config_summary())
    return DummyLLMClient()
