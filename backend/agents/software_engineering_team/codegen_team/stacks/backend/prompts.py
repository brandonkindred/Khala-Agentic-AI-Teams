"""
Prompts for the backend-code-v2 team.

Written from scratch — no reuse of ``backend_agent`` prompts.
"""

from software_engineering_team.shared.coding_standards import CODING_STANDARDS
from software_engineering_team.shared.prompts import (
    DELIVER_COMMIT_MSG_TEMPLATE as DELIVER_COMMIT_MSG_TEMPLATE,
)
from software_engineering_team.shared.prompts import (
    DOCUMENTATION_PROBLEM_SOLVE_PROMPT as DOCUMENTATION_PROBLEM_SOLVE_PROMPT,
)
from software_engineering_team.shared.prompts import (
    FILES_OUTPUT_TEMPLATE_INSTRUCTIONS as FILES_OUTPUT_TEMPLATE_INSTRUCTIONS,
)
from software_engineering_team.shared.prompts import (
    build_batch_fix_prompt,
    build_code_review_prompt,
    build_documentation_self_review_prompt,
    build_execution_prompt,
    build_planning_prompt,
    build_problem_solving_prompt,
    build_problem_solving_single_issue_prompt,
    build_qa_review_prompt,
)
from software_engineering_team.shared.security_service import (
    CODE_BACKEND_FOCUS,
    SecurityProfile,
    build_review_prompt,
)

PYTHON_CONVENTIONS = """
**Python conventions:**
- Use type hints on all function signatures.
- Follow PEP 8 naming (snake_case functions/variables, PascalCase classes).
- requirements.txt with pinned versions;
- Pydantic v2 BaseModel for all request/response schemas.
"""

JAVA_CONVENTIONS = """
**Java conventions:**
- Follow standard Maven/Gradle project layout: src/main/java, src/test/java.
- Spring Boot: @RestController, @Service, @Repository layering.
- Use records or DTOs for request/response; Jackson for serialization.
- JUnit 5 + Mockito for testing.
- PascalCase classes, camelCase methods/fields.
- 
"""

# ---------------------------------------------------------------------------
# Planning phase
# ---------------------------------------------------------------------------

_PLANNING_TOOL_AGENT_DOMAINS = """\
- data_engineering — schema design, data models, data integrity, query optimisation (NO migrations unless explicitly requested)
- api_openapi — API endpoint design, OpenAPI contract, route implementation
- auth — authentication, authorisation, RBAC, permissions, secure defaults
- documentation — README, API docs, runbooks
- testing_qa — test plan, test files, coverage improvements
- security — security hardening, vulnerability fixes
- general — anything else (default code generation)"""

_PLANNING_RULES = """\
Rules:
- Emit 2-10 microtasks. Prefer smaller, focused microtasks over large monolithic ones.
- Include at least one testing_qa microtask unless the task is pure docs/config.
- Dependency order matters: list prerequisites in depends_on (pipe-separated IDs).
- Do NOT create migration microtasks (Alembic, Flyway, etc.) for greenfield projects. Migrations are only needed when modifying an existing database schema. If the project is new, create models/schemas directly without migration infrastructure.
- Do not use JSON. Use only the template above. No explanatory text before or after."""

PLANNING_PROMPT = build_planning_prompt(
    team_kind="backend",
    tool_agent_domains=_PLANNING_TOOL_AGENT_DOMAINS,
    language_input_line="Target language (python or java)",
    language_output="python",
    planning_rules=_PLANNING_RULES,
)

# Planning fix microtasks for unresolved review issues (escalation from problem-solving).
PLANNING_FIXES_FOR_ISSUES_PROMPT = """You are an expert Planning Agent. The problem-solving phase could not fix these issues automatically. Create microtasks that implement the fixes.

**Your job:** For each unresolved issue (or a small related group), emit one microtask that describes the exact fix. Each microtask should be implementable by a single code change or small set of changes.

**Unresolved issues:**
{issues_text}

**Current codebase (excerpt):**
{existing_code}

**Language:** {language}

**Output format (template – use exactly these section headers):**

## MICROTASKS ##
---
id: mt-fix-<short-kebab>
title: short title describing the fix
description: what to change and why (2-4 sentences). Reference the issue (e.g. "Fix the build error in X").
tool_agent: general
depends_on:
---
## END MICROTASKS ##
## LANGUAGE ##
{language}
## END LANGUAGE ##
## SUMMARY ##
1-2 sentence overview of the fix plan
## END SUMMARY ##

- Emit one microtask per issue (or group closely related issues into one microtask).
- Use tool_agent: general for all fix microtasks.
- Do not use JSON. Use only the template above. No explanatory text before or after.
"""

# ---------------------------------------------------------------------------
# Execution phase
# ---------------------------------------------------------------------------

_EXECUTION_PATH_RULES = """\
- Use paths relative to the project root (e.g. `src/main.py`, `src/services/user_service.py`)
- Do NOT include `backend/` prefix in paths — you are already in the backend project
- Example: use `src/main.py`, NOT `backend/src/main.py`"""

EXECUTION_PROMPT = build_execution_prompt(
    engineer_intro="You are an expert Senior Backend Software Engineer implementing production-quality code.",
    coding_standards=CODING_STANDARDS,
    has_language_conventions=True,
    file_noun="code files",
    path_rules=_EXECUTION_PATH_RULES,
)

# ---------------------------------------------------------------------------
# Review phase
# ---------------------------------------------------------------------------

REVIEW_PROMPT = build_code_review_prompt(project_kind="backend")

# ---------------------------------------------------------------------------
# Problem-solving phase
# ---------------------------------------------------------------------------

PROBLEM_SOLVING_PROMPT = build_problem_solving_prompt(
    project_kind="backend",
    coding_standards=CODING_STANDARDS,
    files_line="Files (same as execution): for each updated file:",
)

# Single-issue problem-solving: one issue at a time to keep prompts small.
_SINGLE_ISSUE_FILE_OUTPUT_BLOCK = """\
## FILE path/to/file.ext ##
<full updated file content>
## FILE path/to/next.ext ##
<content if you need to change more than one file>
"""

PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT = build_problem_solving_single_issue_prompt(
    coding_standards=CODING_STANDARDS,
    has_language_conventions=True,
    file_output_block=_SINGLE_ISSUE_FILE_OUTPUT_BLOCK,
)

# ---------------------------------------------------------------------------
# QA tool agent: review (find issues from testing/QA perspective)
# ---------------------------------------------------------------------------

QA_TOOL_AGENT_REVIEW_PROMPT = build_qa_review_prompt(
    second_test_kind="integration tests",
    flakiness_examples="non-determinism, poor isolation",
)

# ---------------------------------------------------------------------------
# Security tool agent: review (find issues from security perspective)
# ---------------------------------------------------------------------------

# Built from the unified Security Review service's ``code`` profile with the
# backend focus list, so the prompt body and severity vocabulary live in one
# place (see ``shared/security_service.py``).
SECURITY_TOOL_AGENT_REVIEW_PROMPT = build_review_prompt(
    SecurityProfile.CODE, focus=CODE_BACKEND_FOCUS
)

# ---------------------------------------------------------------------------
# Tool agents: files + summary (template output, reused by execution and tool agents)
# ---------------------------------------------------------------------------

# FILES_OUTPUT_TEMPLATE_INSTRUCTIONS is byte-identical between backend and
# frontend; imported from shared above and re-exported here (no local override).

# ---------------------------------------------------------------------------
# Documentation tool agent
# ---------------------------------------------------------------------------

DOCUMENTATION_MICROTASK_PROMPT = """You are an expert Documentation Specialist reviewing code changes for a completed microtask.

**Your task:** Update inline documentation (docstrings, comments) for the code that was just added or modified. Ensure all public functions, classes, and methods have proper docstrings.

**Microtask:** {microtask_title}
**Microtask Description:** {microtask_description}
**Task Context:** {task_description}

**Code to document:**
{code}

**What to do:**
1. Add or improve docstrings for all public functions, classes, and methods
2. Add inline comments for complex or non-obvious logic
3. Ensure docstrings include parameter descriptions, return values, and raised exceptions
4. Keep existing functionality unchanged — only add/improve documentation

**Output format (template – use exactly these markers):**
For each file that needs documentation updates:
## FILE path/to/file.ext ##
<full file content with improved documentation>
## SUMMARY ##
what documentation you added or improved
## END SUMMARY ##

- Only output files that you actually changed. If no documentation updates are needed, output an empty SUMMARY.
- Use "## FILE <path> ##" at the start of each file.
- Do not use JSON. Use only the template above. No explanatory text before or after.
"""

DOCUMENTATION_REVIEW_PROMPT = """You are an expert Documentation Reviewer assessing the completeness and quality of documentation.

**Task:** {task_title}
**Task Description:** {task_description}

**Existing Documentation Files:**
{documentation}

**Code to review:**
{code}

**What to check:**
1. README.md exists and is up-to-date with features, installation, and usage instructions
2. All public functions, classes, and methods have docstrings
3. Docstrings include parameter descriptions, return values, and exceptions
4. API endpoints are documented (if applicable)
5. Complex logic has inline comments
6. CONTRIBUTORS.md exists (if multiple contributors)
7. Any existing documentation in /docs folder is current

**Output format (template – use exactly these section headers):**

## PASSED ##
true|false
## END PASSED ##
## ISSUES ##
---
source: documentation
severity: critical|high|medium|low|info
description: what documentation is missing or incorrect
file_path: which file needs documentation
recommendation: how to fix it
---
## END ISSUES ##
## SUMMARY ##
brief documentation assessment
## END SUMMARY ##

- Use "---" to separate each issue block. Use source: documentation for every issue.
- Omit ## ISSUES ## / ## END ISSUES ## if there are no issues.
- Do not use JSON. Use only the template above. No explanatory text before or after.
"""

# DOCUMENTATION_PROBLEM_SOLVE_PROMPT is byte-identical between backend and
# frontend; imported from shared above and re-exported here (no local override).

# ---------------------------------------------------------------------------
# Batch fix prompt: all issues from a review phase at once
# ---------------------------------------------------------------------------

BATCH_FIX_PROMPT = build_batch_fix_prompt(
    role_title="Senior Backend Software Engineer",
    coding_standards=CODING_STANDARDS,
)

# ---------------------------------------------------------------------------
# Documentation self-review prompt: iterative refinement
# ---------------------------------------------------------------------------

DOCUMENTATION_SELF_REVIEW_PROMPT = build_documentation_self_review_prompt(
    project_kind_suffix="",
    completeness_clause="",
    accuracy_target="code",
)

# ---------------------------------------------------------------------------
# Deliver phase (no LLM prompt needed — this is procedural git work)
# ---------------------------------------------------------------------------

# DELIVER_COMMIT_MSG_TEMPLATE is byte-identical between backend and frontend;
# imported from shared above and re-exported here (no local override).
