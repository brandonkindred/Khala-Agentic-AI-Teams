"""
Shared coding standards enforced across all software engineering team agents.

These rules MUST be followed for all code produced by Backend, Frontend, DevOps,
Security, and QA agents.
"""

from typing import Optional

_coding_standards_cache: Optional[str] = None


def get_coding_standards_cached() -> str:
    """
    Return CODING_STANDARDS text. Cached per process to avoid redundant resolution.
    Use this when building prompts that need the full standards; agents can pass
    the result instead of re-importing.
    """
    global _coding_standards_cache
    if _coding_standards_cache is None:
        _coding_standards_cache = CODING_STANDARDS
    return _coding_standards_cache


CODING_STANDARDS = """
**MANDATORY CODING STANDARDS (all agents must enforce):**

1. **Design by Contract** – All code must use Design by Contract:
   - Preconditions: conditions that must hold before a method/function is called
   - Postconditions: conditions guaranteed to hold after successful execution
   - Invariants: conditions that hold before and after each public operation
   - Document contracts in comments; use assertions or validation where appropriate

2. **SOLID Principles** – Code must conform to:
   - **S**ingle Responsibility: each class/function has one reason to change
   - **O**pen/Closed: open for extension, closed for modification
   - **L**iskov Substitution: subtypes must be substitutable for base types
   - **I**nterface Segregation: many specific interfaces over one general
   - **D**ependency Inversion: depend on abstractions, not concretions

3. **Documentation** – Every class, interface, method, and function must have a comment block that explains:
   - **How** it is used (usage examples or call pattern)
   - **Why** it exists (purpose, role in the system)
   - **Constraints** enforced (preconditions, postconditions, invariants, edge cases)
   Use docstrings (Python) or JSDoc/Javadoc (Java/TypeScript) as appropriate.

4. **Test Coverage** – Minimum 85% code coverage:
   - Unit tests for all public methods and critical paths
   - Integration tests for API boundaries and component interactions
   - Tests must be runnable and included in CI

5. **README.md** – Must be maintained and include:
   - How to build the application
   - How to run the application (dev and prod)
   - How to run tests
   - How to deploy
   - Any environment variables or configuration required

6. **Git Branching** – Use proper branching strategy:
   - All development happens on a `development` branch (or `develop`)
   - Create feature/task branches off `development`, NOT off `main`
   - When work is complete, create a Pull Request to merge `development` into `main`
   - If `development` branch does not exist, the Tech Lead creates it before any commits

7. **Commit Messages** – All commit messages MUST follow Conventional Commits (semantic-versioning compliant):
   - Format: `type(scope): description`
   - Types: feat (feature), fix (bug), docs, style, refactor, perf, test, build, ci, chore
   - Scope: optional module/component (e.g. api, auth, frontend)
   - Description: imperative, lowercase, no period at end
   - Examples: `feat(auth): add JWT refresh endpoint`, `fix(api): handle null user in login`
   - Breaking changes: append `!` after type or add `BREAKING CHANGE:` in footer

8. **Naming Conventions** – All names (files, folders, classes, functions, variables) must follow professional standards:

   **General rules (ALL languages):**
   - Names describe WHAT the thing IS or DOES – never derived from a task description
   - Names must be 1-3 words maximum (e.g. `user-list`, `auth_service`, `TaskController`)
   - BANNED words in file/folder/class names: `implement`, `create`, `build`, `setup`, `configure`, `add`, `make`, `define`, `develop`, `write`, `design`, `establish`, `the`, `that`, `with`, `using`, `which`, `for`, `and`, `a`, `an`
   - To derive a name: extract the core NOUN from the requirement, discard all verbs and filler words
   - Example: task "Implement the UserForm component using reactive forms" → name is `user-form`, NOT `implement-the-userform-component-using`

   **Python:**
   - Modules/files: `snake_case` (e.g. `user_service.py`, `task_router.py`, `auth_middleware.py`)
   - Functions/variables: `snake_case` (e.g. `get_user_by_id`, `is_authenticated`)
   - Classes: `PascalCase` (e.g. `UserService`, `TaskRepository`, `AuthMiddleware`)
   - Constants: `UPPER_SNAKE_CASE` (e.g. `MAX_RETRIES`, `DEFAULT_PAGE_SIZE`)
   - GOOD: `user_service.py`, `task_router.py`, `auth.py`, `models.py`
   - BAD: `implement_user_registration_with_email.py`, `create_the_authentication_service.py`

   **TypeScript / Frontend (React, Angular, Vue):**
   - Files/folders: `kebab-case` (e.g. `user-list/`, `task-detail.component.ts`, `auth.service.ts`)
   - Angular component selectors: `kebab-case` with `app-` prefix (e.g. `app-user-list`, `app-nav-bar`)
   - React components: `PascalCase` files (e.g. `UserList.tsx`, `TaskDetail.tsx`)
   - Classes/components: `PascalCase` (e.g. `UserListComponent`, `AuthService`, `TaskDetailComponent`)
   - Variables/methods: `camelCase` (e.g. `getUserById`, `isLoading`, `taskList`)
   - GOOD: `user-list/`, `task-form/`, `app-shell/`, `nav-bar/`, `auth.service.ts`
   - BAD: `implement-the-userlistcomponent-that/`, `create-the-application-shell/`

   **Java:**
   - Classes: `PascalCase` (e.g. `UserController`, `TaskService`, `AuthFilter`)
   - Methods/variables: `camelCase` (e.g. `getUserById`, `isAuthenticated`)
   - Packages: all lowercase (e.g. `com.app.controller`, `com.app.service`)
   - Constants: `UPPER_SNAKE_CASE` (e.g. `MAX_RETRIES`)
   - GOOD: `UserController.java`, `TaskService.java`, `AuthFilter.java`
   - BAD: `ImplementUserRegistrationEndpoint.java`, `CreateTheAuthenticationService.java`
"""

COMMIT_MESSAGE_STANDARDS = """
**Commit messages (Conventional Commits – semantic-versioning compliant):**
- Format: type(scope): description
- Types: feat, fix, docs, style, refactor, perf, test, build, ci, chore
- Scope: optional (e.g. api, auth, frontend)
- Description: imperative mood, lowercase, no period
- Example: feat(auth): add JWT refresh endpoint
"""

GIT_BRANCHING_RULES = """
**Git branching strategy:**
- Branch off `development` (or `develop`) for all work; never branch off `main`
- Create Pull Request to merge `development` → `main` when all development is complete
- Tech Lead must ensure `development` branch exists before team commits; create it if missing
- All commits use Conventional Commits format (semantic-versioning compliant)
"""

# ---------------------------------------------------------------------------
# Subset of coding standards relevant to agents that REVIEW code but do not
# write it (code review agent, security agent, QA agent). Omits git branching,
# commit messages, and README rules that only apply to code authors.
# ---------------------------------------------------------------------------
REVIEW_STANDARDS = """
**CODING STANDARDS TO ENFORCE DURING REVIEW:**

1. **Design by Contract** -- Code should use Design by Contract:
   - Preconditions: conditions that must hold before a method/function is called
   - Postconditions: conditions guaranteed to hold after successful execution
   - Invariants: conditions that hold before and after each public operation
   - Contracts should be documented in comments; assertions or validation used where appropriate

2. **SOLID Principles** -- Code should conform to:
   - **S**ingle Responsibility: each class/function has one reason to change
   - **O**pen/Closed: open for extension, closed for modification
   - **L**iskov Substitution: subtypes must be substitutable for base types
   - **I**nterface Segregation: many specific interfaces over one general
   - **D**ependency Inversion: depend on abstractions, not concretions

3. **Documentation** -- Classes, methods, and functions should have docstrings/comments explaining:
   - **How** it is used (usage examples or call pattern)
   - **Why** it exists (purpose, role in the system)
   - **Constraints** enforced (preconditions, postconditions, invariants, edge cases)

4. **Test Coverage** -- Minimum 85% code coverage:
   - Unit tests for all public methods and critical paths
   - Integration tests for API boundaries and component interactions

5. **Naming Conventions** -- Names must follow professional standards:
   - Names describe WHAT the thing IS or DOES -- never derived from task descriptions
   - Names must be 1-3 words maximum
   - Python: snake_case for modules/functions, PascalCase for classes
   - TypeScript: kebab-case for files/folders, PascalCase for components/classes, camelCase for variables
   - Java: PascalCase for classes, camelCase for methods/variables
"""
