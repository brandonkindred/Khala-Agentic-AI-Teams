"""
Prompts for the frontend-code-v2 team.

Written from scratch — no reuse of frontend_team or feature_agent prompts.
"""

from software_engineering_team.shared.coding_standards import CODING_STANDARDS
from software_engineering_team.shared.prompts import (
    DELIVER_COMMIT_MSG_TEMPLATE as DELIVER_COMMIT_MSG_TEMPLATE,
)
from software_engineering_team.shared.prompts import (
    DOCUMENTATION_PROBLEM_SOLVE_PROMPT as DOCUMENTATION_PROBLEM_SOLVE_PROMPT,
)
from software_engineering_team.shared.prompts import (
    build_batch_fix_prompt,
    build_documentation_self_review_prompt,
    build_execution_prompt,
    build_planning_prompt,
    build_problem_solving_prompt,
    build_problem_solving_single_issue_prompt,
    build_qa_review_prompt,
)
from software_engineering_team.shared.security_service import (
    CODE_FRONTEND_FOCUS,
    SecurityProfile,
    build_review_prompt,
)

TYPESCRIPT_CONVENTIONS = """
**TypeScript conventions:**
- Use strict TypeScript settings (strict: true).
- Prefer interfaces over type aliases for object shapes.
- Use JSDoc/TSDoc comments for all public exports.
- camelCase for variables/functions, PascalCase for classes/interfaces/components.
- Use explicit return types on exported functions.
- Avoid `any` type; use `unknown` or proper generics.
- Import types with `import type` when importing only types.
"""

# ---------------------------------------------------------------------------
# Planning phase
# ---------------------------------------------------------------------------

_PLANNING_TOOL_AGENT_DOMAINS = """\
- state_management — state shape, stores, data flow (e.g. NgRx, Redux, signals)
- auth — login UI, auth guards, token handling, permissions in UI
- api_openapi — API client code, service layer, request/response types
- documentation — README, component docs, Storybook
- testing_qa — unit tests, e2e tests, test utilities
- security — XSS prevention, CSP, secure forms
- ui_design — layout, components, visual structure
- branding_theme — themes, design tokens, brand compliance
- ux_usability — flows, interactions, usability improvements
- accessibility — a11y checks, WCAG, screen reader support
- performance — bundle size, code splitting, lazy loading, caching
- architecture — folder structure, routing, state management patterns, API client patterns
- linter — lint rules, format fixes
- general — anything else (default code generation)"""

_PLANNING_RULES = """\
Rules:
- Emit 2-10 microtasks. Prefer smaller, focused microtasks.
- Include at least one testing_qa microtask unless the task is pure docs/config.
- Dependency order matters: list prerequisites in depends_on (pipe-separated IDs).
- For LANGUAGE use one of: angular, react, vue, typescript, javascript. Use the stack specified in the input or detected from the project.
- Do not use JSON. Use only the template above. No explanatory text before or after."""

PLANNING_PROMPT = build_planning_prompt(
    team_kind="frontend",
    tool_agent_domains=_PLANNING_TOOL_AGENT_DOMAINS,
    language_input_line="Target stack (e.g. angular, react, typescript, javascript)",
    language_output="{detected_language}",
    planning_rules=_PLANNING_RULES,
)

PLANNING_FIXES_FOR_ISSUES_PROMPT = """You are an expert Planning Agent for a frontend team. Create microtasks that implement fixes for the following unresolved review issues.

**Unresolved issues:**
{issues_text}

**Current codebase (excerpt):**
{existing_code}

**Stack:** {language}

**Output format (same as main planning):**
## MICROTASKS ##
---
id: mt-fix-<short-kebab>
title: short title
description: what to change and why
tool_agent: general
depends_on:
---
## END MICROTASKS ##
## LANGUAGE ##
{language}
## END LANGUAGE ##
## SUMMARY ##
1-2 sentence fix plan
## END SUMMARY ##

- One microtask per issue or small related group. Use tool_agent: general.
- Do not use JSON. Use only the template above. No explanatory text before or after.
"""

# ---------------------------------------------------------------------------
# Execution phase
# ---------------------------------------------------------------------------

_EXECUTION_PATH_RULES = """\
- Use paths relative to the project root (e.g. `src/app/component.ts`, `src/styles.scss`)
- Do NOT include `frontend/` prefix in paths — you are already in the frontend project
- Example: use `src/app/app.component.ts`, NOT `frontend/src/app/app.component.ts`"""

EXECUTION_PROMPT = build_execution_prompt(
    engineer_intro="You are an expert Senior Frontend Engineer implementing production-quality UI code.",
    coding_standards=CODING_STANDARDS,
    has_language_conventions=False,
    file_noun="component/service files",
    path_rules=_EXECUTION_PATH_RULES,
)

# ---------------------------------------------------------------------------
# Problem-solving phase
# ---------------------------------------------------------------------------

PROBLEM_SOLVING_PROMPT = build_problem_solving_prompt(
    project_kind="frontend",
    coding_standards=CODING_STANDARDS,
    files_line="Files: for each updated file:",
    has_language_conventions=False,
)

_SINGLE_ISSUE_FILE_OUTPUT_BLOCK = """\
## FILE path/to/file.ext ##
<full updated file content>
"""

PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT = build_problem_solving_single_issue_prompt(
    coding_standards=CODING_STANDARDS,
    has_language_conventions=False,
    file_output_block=_SINGLE_ISSUE_FILE_OUTPUT_BLOCK,
)

# ---------------------------------------------------------------------------
# QA tool agent: review (find issues from testing/QA perspective)
# ---------------------------------------------------------------------------

QA_TOOL_AGENT_REVIEW_PROMPT = build_qa_review_prompt(
    second_test_kind="e2e tests",
    flakiness_examples="hard-coded waits, non-determinism",
)

# ---------------------------------------------------------------------------
# Security tool agent: review (find issues from security perspective)
# ---------------------------------------------------------------------------

# Built from the unified Security Review service's ``code`` profile with the
# frontend focus list, so the prompt body and severity vocabulary live in one
# place (see ``shared/security_service.py``).
SECURITY_TOOL_AGENT_REVIEW_PROMPT = build_review_prompt(
    SecurityProfile.CODE, focus=CODE_FRONTEND_FOCUS
)

# ---------------------------------------------------------------------------
# Documentation tool agent
# ---------------------------------------------------------------------------

DOCUMENTATION_MICROTASK_PROMPT = """You are an expert Documentation Specialist reviewing code changes for a completed microtask.

**Your task:** Update inline documentation (JSDoc/TSDoc comments) for the code that was just added or modified. Ensure all public functions, components, and interfaces have proper documentation.

**Microtask:** {microtask_title}
**Microtask Description:** {microtask_description}
**Task Context:** {task_description}

**Code to document:**
{code}

**What to do:**
1. Add or improve JSDoc/TSDoc comments for all public functions, components, and interfaces
2. Document component props with @param tags
3. Add inline comments for complex or non-obvious logic
4. Include @example tags where helpful
5. Keep existing functionality unchanged — only add/improve documentation

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

DOCUMENTATION_REVIEW_PROMPT = """You are an expert Documentation Reviewer assessing the completeness and quality of frontend documentation.

**Task:** {task_title}
**Task Description:** {task_description}

**Existing Documentation Files:**
{documentation}

**Code to review:**
{code}

**What to check:**
1. README.md exists and is up-to-date with features, installation, and usage instructions
2. All public components, functions, and interfaces have JSDoc/TSDoc comments
3. Component props are documented with type information
4. Usage examples are provided for complex components
5. Storybook stories exist for UI components (if applicable)
6. Complex logic has inline comments
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
    role_title="Senior Frontend Software Engineer",
    coding_standards=CODING_STANDARDS,
)

# ---------------------------------------------------------------------------
# Documentation self-review prompt: iterative refinement
# ---------------------------------------------------------------------------

DOCUMENTATION_SELF_REVIEW_PROMPT = build_documentation_self_review_prompt(
    project_kind_suffix=" frontend",
    completeness_clause=" (props, usage, examples)",
    accuracy_target="component/function",
)

# ---------------------------------------------------------------------------
# Deliver phase (procedural git work; no LLM prompt)
# ---------------------------------------------------------------------------

# DELIVER_COMMIT_MSG_TEMPLATE is byte-identical between backend and frontend;
# imported from shared above and re-exported here (no local override).
