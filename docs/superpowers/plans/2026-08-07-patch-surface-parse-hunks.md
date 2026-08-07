# Patch Surface Parse Hunks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add thin `change_surface` wrappers that extract added-only touched new-file lines and reuse annotated hunk rendering from existing GitHub PR patch helpers.

**Architecture:** Two public functions in `code_review_agent/change_surface.py` wrap `parse_valid_lines(..., added_only=True)` and `render_annotated_hunks` from `github_source.pr_review_mapping`. No reimplementation of hunk parsing; `build_change_surface_from_patches` stays a stub until the emit leaf.

**Tech Stack:** Python 3.10+, pytest, existing `software_engineering_team` packages (`code_review_agent`, `github_source`).

## Global Constraints

- Touched lines = added (`+`) only — never context or removed.
- Annotated hunk text must equal `render_annotated_hunks(patch)` for every input.
- Do not reimplement unified-diff parsing in `change_surface.py`.
- Do not change `COMMENT_ON_ADDED_LINES_ONLY` or the default of `parse_valid_lines` for PR comment mapping.
- Do not implement `build_change_surface_from_patches` assembly or `### path ###` emission.
- No network, LLM, or GitHub client in these helpers or their tests.
- Never reference GitHub issue numbers in code, comments, commit messages, or docs (describe changes on their own terms).
- DbC: every new public function documents `Preconditions:` / `Postconditions:`.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/change_surface.py` | Add `extract_touched_lines` / `render_patch_hunks`; export in `__all__`; brief module-doc note |
| `backend/agents/software_engineering_team/tests/test_change_surface_patch_parse.py` | Focused single-file patch parse tests (new) |
| `backend/agents/software_engineering_team/github_source/pr_review_mapping.py` | **Read-only** — consume `parse_valid_lines` / `render_annotated_hunks` |

---

### Task 1: `extract_touched_lines`

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_change_surface_patch_parse.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/change_surface.py`

**Interfaces:**
- Consumes: `software_engineering_team.github_source.pr_review_mapping.parse_valid_lines(patch: str, *, added_only: bool = ...) -> set[int]`
- Produces: `extract_touched_lines(patch: str) -> frozenset[int]` (public, in `__all__`)

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_change_surface_patch_parse.py` (Task 1 imports only — add `render_patch_hunks` / `render_annotated_hunks` in Task 2):

```python
"""Single-file patch parse helpers on ``change_surface``."""

from __future__ import annotations

from software_engineering_team.code_review_agent.change_surface import (
    extract_touched_lines,
)

# Realistic single-file hunk: context, removed, added.
_SINGLE_FILE_PATCH = (
    "@@ -1,3 +1,3 @@\n"
    " keep\n"
    "-deleted\n"
    "+added\n"
    " trail\n"
)


def test_extract_touched_lines_added_only_excludes_context_and_removed() -> None:
    # New-file coords: line1 keep (ctx), line2 added (+), line3 trail (ctx).
    # Removed '-' does not advance new-file line numbers.
    assert extract_touched_lines(_SINGLE_FILE_PATCH) == frozenset({2})


def test_extract_touched_lines_empty_patch() -> None:
    assert extract_touched_lines("") == frozenset()
    assert extract_touched_lines("   \n") == frozenset()
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_patch_parse.py -v
```

Expected: FAIL with `ImportError` / cannot import `extract_touched_lines`.

- [ ] **Step 3: Implement `extract_touched_lines`**

In `change_surface.py`:

1. Add import (near other imports; prefer the leaf module to avoid pulling the whole `github_source` package side effects if any — `pr_review_mapping` is dependency-light):

```python
from software_engineering_team.github_source.pr_review_mapping import (
    parse_valid_lines,
    render_annotated_hunks,
)
```

(`render_annotated_hunks` may be unused until Task 2; either import both now or add the second import in Task 2. Prefer importing only `parse_valid_lines` in this step.)

2. Add `"extract_touched_lines"` to `__all__` (keep list sorted alphabetically with existing names where practical; place next to other public helpers).

3. Add the function after `DEFAULT_EXPANSION_CONTEXT_LINES` / near other pure helpers (before or after `expand_touched_ranges` is fine; prefer just above `build_change_surface_from_patches` so parse helpers sit with the patch path):

```python
def extract_touched_lines(patch: str) -> frozenset[int]:
    """Return added-only new-file line numbers from one file's unified patch.

    Preconditions:
        - ``patch`` is one file's unified-diff text (GitHub ``files[].patch``
          style), or empty / blank for binary / oversized / unchanged files.

    Postconditions:
        - Returns a frozenset of 1-based new-file line numbers that appear as
          added (``+``) lines in the patch.
        - Context (`` ``), removed (``-``), and ``\\ No newline at end of file``
          markers are never included.
        - Empty or blank ``patch`` → empty frozenset.
        - Never raises.
    """
    return frozenset(parse_valid_lines(patch or "", added_only=True))
```

4. Update the module docstring’s short summary so it mentions patch parse helpers exist (one sentence; no issue numbers). Example addition after the `expand_touched_ranges` sentence:

```text
``extract_touched_lines`` / ``render_patch_hunks`` wrap GitHub unified-patch
helpers for added-only touched lines and annotated hunk text.
```

(If `render_patch_hunks` is not implemented yet, mention only `extract_touched_lines` in this step and extend the sentence in Task 2.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_patch_parse.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/change_surface.py \
  backend/agents/software_engineering_team/tests/test_change_surface_patch_parse.py
git commit -m "$(cat <<'EOF'
Add extract_touched_lines wrapper for added-only patch lines.

EOF
)"
```

---

### Task 2: `render_patch_hunks`

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/change_surface.py`
- Modify: `backend/agents/software_engineering_team/tests/test_change_surface_patch_parse.py`

**Interfaces:**
- Consumes: `software_engineering_team.github_source.pr_review_mapping.render_annotated_hunks(patch: str) -> str`
- Produces: `render_patch_hunks(patch: str) -> str` (public, in `__all__`)
- Relies on Task 1’s `_SINGLE_FILE_PATCH` fixture in the test module

- [ ] **Step 1: Write the failing tests**

Append to `test_change_surface_patch_parse.py`:

```python
from software_engineering_team.code_review_agent.change_surface import (
    extract_touched_lines,
    render_patch_hunks,
)
from software_engineering_team.github_source.pr_review_mapping import (
    render_annotated_hunks,
)


def test_render_patch_hunks_matches_annotated_helper() -> None:
    assert render_patch_hunks(_SINGLE_FILE_PATCH) == render_annotated_hunks(
        _SINGLE_FILE_PATCH
    )


def test_render_patch_hunks_empty_patch() -> None:
    assert render_patch_hunks("") == ""
    assert render_patch_hunks("   \n") == render_annotated_hunks("   \n")


def test_touched_set_diverges_from_annotated_context_lines() -> None:
    """Lock the added-only vs annotated(+context) split on one patch."""
    touched = extract_touched_lines(_SINGLE_FILE_PATCH)
    annotated = render_patch_hunks(_SINGLE_FILE_PATCH)
    assert touched == frozenset({2})
    # Annotated includes context lines 1 and 3 as ``N: ...`` prefixes.
    assert "1:" in annotated
    assert "2:" in annotated
    assert "3:" in annotated
```

Merge imports at the top of the file (single import block for `change_surface`, one for `render_annotated_hunks`).

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_patch_parse.py -v
```

Expected: FAIL with cannot import / missing `render_patch_hunks`.

- [ ] **Step 3: Implement `render_patch_hunks`**

In `change_surface.py`:

1. Ensure import includes `render_annotated_hunks`:

```python
from software_engineering_team.github_source.pr_review_mapping import (
    parse_valid_lines,
    render_annotated_hunks,
)
```

2. Add `"render_patch_hunks"` to `__all__`.

3. Add:

```python
def render_patch_hunks(patch: str) -> str:
    """Render annotated hunk text for one file's unified / PR patch.

    Preconditions:
        - ``patch`` is one file's unified-diff text (GitHub ``files[].patch``
          style), or empty / blank for binary / oversized / unchanged files.

    Postconditions:
        - Return value is identical to ``render_annotated_hunks(patch)`` for
          every input (string equality).
        - Empty or blank ``patch`` → ``\"\"``.
        - Never raises.
    """
    return render_annotated_hunks(patch or "")
```

Note: `render_annotated_hunks` already treats falsy patches as empty; passing `patch or ""` keeps the wrapper’s blank-string handling explicit and aligned with `extract_touched_lines`.

4. Extend the module docstring sentence to name both helpers.

- [ ] **Step 4: Run full focused suite**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_patch_parse.py \
  agents/software_engineering_team/tests/test_change_surface_api.py \
  agents/software_engineering_team/tests/test_coding_team_pr_review_mapping.py \
  -v
```

Expected: all PASS. Confirm `test_change_surface_api.py` still sees `build_change_surface_from_patches` raising `NotImplementedError` for non-empty patches (stub unchanged). Confirm PR-mapping defaults for `parse_valid_lines` are unchanged (context still included when `added_only` is omitted).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/change_surface.py \
  backend/agents/software_engineering_team/tests/test_change_surface_patch_parse.py
git commit -m "$(cat <<'EOF'
Add render_patch_hunks wrapper reusing annotated hunk rendering.

EOF
)"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| Touched = added only via `parse_valid_lines(..., added_only=True)` | Task 1 |
| Annotated hunks reuse `render_annotated_hunks` unchanged | Task 2 |
| Two wrappers in `change_surface.py` + `__all__` | Tasks 1–2 |
| DbC on both public functions | Tasks 1–2 |
| Empty/binary → empty frozenset / `""` | Tasks 1–2 |
| Single-file tests with `+` / ` ` / `-` divergence locked | Tasks 1–2 |
| No `build_change_surface_from_patches` implementation | Explicit non-touch |
| No multi-file helper / no `ParsedPatchHunks` | Explicit non-touch |
| No change to PR comment `parse_valid_lines` default | Task 2 regression run |

## Placeholder scan

No TBD/TODO steps; all code and commands are concrete.
