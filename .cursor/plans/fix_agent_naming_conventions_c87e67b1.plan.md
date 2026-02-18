---
name: Fix Agent Naming Conventions
overview: Strengthen naming convention guidance in agent prompts, tighten file path validation to catch more bad names, and fix the mock LLM to stop slugifying task descriptions into file/folder names.
todos:
  - id: shared-naming-standards
    content: Add naming convention rules to CODING_STANDARDS in shared/coding_standards.py
    status: completed
  - id: frontend-prompt
    content: Strengthen naming rules in frontend_agent/prompts.py with examples and derivation algorithm
    status: completed
  - id: backend-prompt
    content: Strengthen naming rules in backend_agent/prompts.py with examples and derivation algorithm
    status: completed
  - id: repo-writer-validation
    content: Tighten path validation in shared/repo_writer.py (lower thresholds, add verb/filler checks)
    status: completed
  - id: agent-validation
    content: Mirror validation improvements in frontend_agent/agent.py and backend_agent/agent.py
    status: completed
  - id: mock-llm-names
    content: Fix DummyLLMClient in shared/llm.py to extract proper names from task hints
    status: completed
isProject: false
---

# Fix Agent Naming Conventions

## Problem

The terminal output shows the agents are creating folders/files named after task descriptions rather than using proper component or module names:

```
implement-the-userformcomponent-using-an/
create-the-angular-application-shell-tha/
implement-the-userlistcomponent-that-fet/
```

These should be something like `user-form/`, `app-shell/`, `user-list/`.

The problem exists at three layers:

1. **Prompt guidance** -- the agents have some naming rules but they are not emphatic enough and the LLM ignores them
2. **Validation** -- the path validators catch 6+ hyphenated words and 40+ char segments, but bad names like `implement-the-userformcomponent-using-an` (5 segments, exactly 40 chars) slip through
3. **Mock LLM** -- `DummyLLMClient` directly slugifies the task description into file paths, guaranteeing bad names

## Changes

### 1. Add naming standards to shared coding standards

**File:** `[shared/coding_standards.py](software_engineering_team/shared/coding_standards.py)`

Add a new section to `CODING_STANDARDS` covering naming conventions for all languages:

- **Python**: `snake_case` for modules/functions/variables, `PascalCase` for classes, 1-3 words max for module names
- **TypeScript/Angular**: `kebab-case` for files/folders/selectors, `PascalCase` for classes/components, 1-3 words max
- **Java**: `PascalCase` for classes, `camelCase` for methods/variables, lowercase packages
- **General rule**: Names must describe WHAT the thing IS or DOES, never derived from a task description. Include a "banned words in names" list: `implement`, `create`, `build`, `the`, `that`, `with`, `using`, `which`, `for`, `and`

### 2. Strengthen frontend prompt naming rules

**File:** `[frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py)`

Rewrite the "CRITICAL RULES" section (lines 24-29) with:

- More good/bad examples
- Explicit instruction: "Extract the NOUN (what the component is) from the task description -- ignore all verbs and filler words"
- A step-by-step name derivation algorithm: task says "Implement the UserFormComponent using Angular reactive forms" -> extract noun "user-form" -> use that as the name
- Emphasize that component folder names must NEVER contain verbs like "implement", "create", "build"

### 3. Strengthen backend prompt naming rules

**File:** `[backend_agent/prompts.py](software_engineering_team/backend_agent/prompts.py)`

Add a similar explicit naming section under the existing rule #4 (lines 48-52):

- More good/bad examples for Python (`user_service.py` not `implement_user_registration_with_email.py`)
- Same "extract the noun" algorithm
- Banned words list

### 4. Tighten path validation in `repo_writer.py`

**File:** `[shared/repo_writer.py](software_engineering_team/shared/repo_writer.py)`

- Lower `MAX_SEGMENT_LENGTH` from 40 to 30
- Change `_SENTENCE_NAME_RE` from requiring 6+ hyphenated words to 4+ (matching the pattern `^[a-z]+-[a-z]+-[a-z]+-[a-z]+`)
- Add a new `_VERB_PREFIX_RE` pattern that rejects segments starting with common verbs: `^(implement|create|build|setup|configure|add|make|define|develop|write|design|establish)-`
- Add a new `_FILLER_WORD_RE` pattern that rejects segments containing filler words as standalone segments: `-the-`, `-that-`, `-with-`, `-using-`, `-which-`, `-for-`

### 5. Tighten validation in agent files

**Files:** `[frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)` (line 19), `[backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)` (line 18)

Mirror the same validation improvements from `repo_writer.py`:

- Lower `MAX_PATH_SEGMENT_LENGTH` from 40 to 30
- Change `BAD_NAME_PATTERN` from 6+ to 4+ hyphenated words
- Add verb-prefix and filler-word checks inside `_validate_file_paths`

### 6. Fix mock LLM to generate proper names

**File:** `[shared/llm.py](software_engineering_team/shared/llm.py)` (lines 315-344)

Instead of slugifying the entire `task_hint`, add a helper `_extract_name_from_hint(hint: str) -> str` that:

1. Strips common leading verbs ("implement", "create", "build", "add", "set up", "define")
2. Strips filler words ("the", "that", "with", "using", "which", "for", "and", "a", "an")
3. Extracts the core noun phrase (e.g., "user form", "task list", "app shell")
4. Converts to the appropriate case (snake_case for backend, kebab-case for frontend)
5. Truncates to 25 chars max

For example:

- "Implement the UserFormComponent using Angular reactive forms" -> `user-form`
- "Create user registration endpoint with email validation" -> `user_registration`
- "Build the task list component with pagination" -> `task-list`

