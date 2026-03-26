# Shared Utilities

The `shared/` directory contains common utilities, models, and infrastructure used across all Software Engineering Team agents.

## Overview

```
shared/
├── llm.py                    # LLM client abstraction
├── models.py                 # Shared Pydantic models
├── job_store.py              # Job state management
├── repo_writer.py            # File writing utilities
├── repo_utils.py             # Repository utilities
├── git_utils.py              # Git operations
├── command_runner.py         # Shell command execution
├── task_parsing.py           # Task parsing utilities
├── task_validation.py        # Task validation
├── task_utils.py             # Task utilities
├── task_plan.py              # Task planning models
├── logging_config.py         # Logging configuration
├── coding_standards.py       # Code style standards
├── frontend_framework.py     # Frontend framework detection
├── clarification_store.py    # Open questions storage
├── llm_response_utils.py     # LLM response parsing
├── development_plan_writer.py  # Plan file generation
├── context_sizing.py         # Context window management
├── execution_tracker.py      # Execution progress tracking
├── prompt_utils.py           # Prompt building utilities
├── sla_best_practices.py     # SLA/quality best practices
├── planning_cache.py         # Planning result caching
├── error_parsing.py          # Error message parsing
└── test_spec_expectations.py # Test specification helpers
```

## LLM Client (`llm.py`)

The LLM client provides a unified interface for text and JSON generation with support for Ollama and dummy providers.

### Configuration

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `LLM_PROVIDER` | `ollama` or `dummy` | `dummy` |
| `LLM_MODEL` | Model name | `qwen3.5:397b-cloud` |
| `LLM_BASE_URL` | Ollama API URL | `http://127.0.0.1:11434` |
| `LLM_TIMEOUT` | Request timeout (seconds) | 600 |
| `LLM_MAX_RETRIES` | Retry attempts | 4 |
| `LLM_MAX_CONCURRENCY` | Concurrent calls | 2 |
| `LLM_MAX_TOKENS` | Max output tokens | min(context, 32768) |
| `LLM_CONTEXT_SIZE` | Context window | Model-specific |
| `LLM_MODEL_<AGENT>` | Per-agent model override | — |

### Agent-Specific Models

```python
AGENT_DEFAULT_MODELS = {
    "backend": "qwen3.5:397b-cloud",
    "frontend": "qwen3.5:397b-cloud",
    "tech_lead": "qwen3.5:397b-cloud",
    "architecture": "qwen3.5:397b-cloud",
    "qa": "qwen3.5:397b-cloud",
    "security": "qwen3.5:397b-cloud",
    # ... all agents use qwen3.5:397b-cloud
}
```

### Usage

```python
from shared.llm import LLMClient

llm = LLMClient()

# Text completion
response = llm.complete(
    prompt="Explain dependency injection",
    temperature=0.7,
    system_prompt="You are a senior engineer."
)

# JSON completion with validation
result = llm.complete_json(
    prompt="List the top 3 Python web frameworks",
    temperature=0.2,
    expected_keys=["frameworks"],  # Optional validation
)
```

### Agent-Specific Client

```python
from shared.llm import get_llm_for_agent

# Uses LLM_MODEL_BACKEND or agent default
llm = get_llm_for_agent("backend")
```

## Models (`models.py`)

Core Pydantic models used across agents:

### Task Models

```python
class TaskType(str, Enum):
    ARCHITECTURE = "architecture"
    BACKEND = "backend"
    FRONTEND = "frontend"
    DEVOPS = "devops"
    SECURITY = "security"
    QA = "qa"
    DOCUMENTATION = "documentation"

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    COMPLETED = "completed"
    FAILED = "failed"

class Task(BaseModel):
    id: str
    title: str
    description: str
    type: TaskType
    status: TaskStatus
    dependencies: List[str]
    assigned_to: Optional[str]
```

### Architecture Models

```python
class ArchitectureComponent(BaseModel):
    name: str
    type: str  # backend, frontend, database, etc.
    description: str
    technology: Optional[str]
    dependencies: List[str]

class SystemArchitecture(BaseModel):
    overview: str
    components: List[ArchitectureComponent]
    architecture_document: str
    diagrams: Dict[str, str]
    decisions: List[Dict[str, Any]]
```

### Planning Hierarchy

```python
class TaskPlan(BaseModel):
    id: str
    title: str
    description: str
    acceptance_criteria: List[str]
    dependencies: List[str]

class StoryPlan(BaseModel):
    id: str
    title: str
    tasks: List[TaskPlan]

class Epic(BaseModel):
    id: str
    title: str
    stories: List[StoryPlan]

class Initiative(BaseModel):
    id: str
    title: str
    epics: List[Epic]

class PlanningHierarchy(BaseModel):
    initiatives: List[Initiative]
```

## Job Store (`job_store.py`)

In-memory job state management for API endpoints:

```python
from shared.job_store import (
    create_job,
    update_job,
    get_job,
    is_waiting_for_answers,
    submit_answers,
)

# Create a job
job_id = create_job()

# Update progress
update_job(job_id, progress=50, current_phase="execution")

# Check for pending questions
if is_waiting_for_answers(job_id):
    # User submits answers
    submit_answers(job_id, {"q1": "answer1"})

# Get job state
job = get_job(job_id)
```

## Repository Writer (`repo_writer.py`)

Safe file writing with conflict detection:

```python
from shared.repo_writer import write_agent_output, NO_FILES_TO_WRITE_MSG

files = {
    "src/main.py": "print('hello')",
    "README.md": "# Project\n",
}

result = write_agent_output(
    files=files,
    repo_path=Path("/path/to/repo"),
    agent_name="backend",
    dry_run=False,
)
```

## Git Utilities (`git_utils.py`)

Git operations wrapper:

```python
from shared.git_utils import (
    init_repo,
    create_branch,
    commit_changes,
    merge_branch,
    get_current_branch,
)

# Initialize repository
init_repo(repo_path)

# Create and switch to branch
create_branch(repo_path, "feature/new-api")

# Commit changes
commit_changes(repo_path, "Add new API endpoint")

# Merge to main
merge_branch(repo_path, "feature/new-api", "main")
```

## Command Runner (`command_runner.py`)

Safe shell command execution:

```python
from shared.command_runner import run_command

result = run_command(
    ["npm", "install"],
    cwd="/path/to/project",
    timeout=300,
    capture_output=True,
)

if result.returncode == 0:
    print(result.stdout)
else:
    print(result.stderr)
```

## Context Sizing (`context_sizing.py`)

Manages LLM context window limits:

```python
from shared.context_sizing import (
    estimate_tokens,
    truncate_to_fit,
    get_available_context,
)

# Estimate token count
tokens = estimate_tokens(long_text)

# Truncate to fit context
truncated = truncate_to_fit(content, max_tokens=50000)

# Get available context for agent
available = get_available_context("backend")
```

## Clarification Store (`clarification_store.py`)

Manages open questions for user input:

```python
from shared.clarification_store import (
    store_questions,
    get_pending_questions,
    submit_answers,
    clear_questions,
)

# Store questions
store_questions(job_id, [
    {"id": "q1", "question": "Which database?", "options": ["PostgreSQL", "MySQL"]},
])

# Get pending
pending = get_pending_questions(job_id)

# Submit answers
submit_answers(job_id, {"q1": "PostgreSQL"})
```

## Logging Configuration (`logging_config.py`)

Standardized logging setup:

```python
from shared.logging_config import setup_logging

# Setup with default format
setup_logging(level="INFO")

# Or with agent-specific logger
import logging
logger = logging.getLogger(__name__)
logger.info("Agent started")
```

## Frontend Framework (`frontend_framework.py`)

Detects frontend framework from code:

```python
from shared.frontend_framework import detect_framework

framework = detect_framework(repo_path)
# Returns: "angular", "react", "vue", or "unknown"
```

## Coding Standards (`coding_standards.py`)

Code style and formatting standards:

```python
from shared.coding_standards import (
    PYTHON_STYLE_GUIDE,
    TYPESCRIPT_STYLE_GUIDE,
    get_linting_config,
)

# Get linting configuration for language
config = get_linting_config("python")
```

## Error Parsing (`error_parsing.py`)

Parses error messages from build/test output:

```python
from shared.error_parsing import parse_errors

errors = parse_errors(build_output)
# Returns list of structured error objects with file, line, message
```

## SLA Best Practices (`sla_best_practices.py`)

Quality and SLA guidelines:

```python
from shared.sla_best_practices import (
    CODE_REVIEW_SLA,
    TEST_COVERAGE_TARGET,
    DOCUMENTATION_REQUIREMENTS,
)
```

## Usage Notes

1. **Import from `shared`**: All utilities are importable from the `shared` package
2. **Environment variables**: Configure LLM and other settings via environment variables
3. **Pydantic models**: Use provided models for type safety and validation
4. **Logging**: Use the shared logging configuration for consistent output
5. **Git operations**: Always use `git_utils` for repository operations to ensure consistency
