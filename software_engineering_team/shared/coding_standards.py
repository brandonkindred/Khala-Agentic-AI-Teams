"""
Shared coding standards enforced across all software engineering team agents.

These rules MUST be followed for all code produced by Backend, Frontend, DevOps,
Security, and QA agents.
"""

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
