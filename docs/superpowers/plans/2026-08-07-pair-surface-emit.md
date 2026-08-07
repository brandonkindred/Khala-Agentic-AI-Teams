# Pair Surface Emit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `build_change_surface_from_pairs` by composing `unified_diffs_from_pairs` with `build_change_surface_from_patches` so SE old/new maps emit the same change-surface format as the PR patch path.

**Architecture:** Thin composition in `change_surface.py`: derive per-path unified diffs, then call the existing patch assembler with the same `new_contents` map for expansion. Blank/identical diffs pass through and are omitted by the patch path.

**Tech Stack:** Python 3.10+, pytest, existing `unified_diffs_from_pairs` / `build_change_surface_from_patches` / `ChangeSurface`.

## Global Constraints

- Public API: keep `build_change_surface_from_pairs(new_contents, old_contents=None) -> ChangeSurface`.
- Implementation: `patches = unified_diffs_from_pairs(...); return build_change_surface_from_patches(patches, new_contents=new_contents)`.
- Do not reimplement expand / merge / pre-number / `### path ###` emission.
- Do not filter blank diffs before calling the patch path.
- Keep early `_empty_surface()` when `new_contents` is empty.
- No network, LLM, or GitHub client in helpers or tests.
- Never reference GitHub issue numbers in code, comments, commit messages, or docs.
- DbC: update `Preconditions:` / `Postconditions:` on the public function to match real behavior (no stub / `NotImplementedError` wording).

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/change_surface.py` | Replace pairs stub with thin compose + updated DbC |
| `backend/agents/software_engineering_team/tests/test_build_change_surface_from_pairs.py` | Parent-complete pairs emit tests (new) |
| `backend/agents/software_engineering_team/tests/test_change_surface_api.py` | Remove pairs `NotImplementedError` stub tests |

---

### Task 1: Implement `build_change_surface_from_pairs`

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_build_change_surface_from_pairs.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/change_surface.py`
- Modify: `backend/agents/software_engineering_team/tests/test_change_surface_api.py`

**Interfaces:**
- Consumes: `unified_diffs_from_pairs(new_contents, old_contents=None) -> dict[str, str]`, `build_change_surface_from_patches(patches, *, new_contents=None) -> ChangeSurface`, `_empty_surface() -> ChangeSurface`
- Produces: working `build_change_surface_from_pairs(new_contents: Mapping[str, str], old_contents: Optional[Mapping[str, str]] = None) -> ChangeSurface`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_build_change_surface_from_pairs.py`:

```python
"""Tests for ``build_change_surface_from_pairs``."""

from __future__ import annotations

from software_engineering_team.code_review_agent.change_surface import (
    build_change_surface_from_pairs,
    build_change_surface_from_patches,
    unified_diffs_from_pairs,
)

_OLD = "def outer():\n    return 0\n"
_NEW = "def outer():\n    return 1\n"


def test_empty_new_contents() -> None:
    assert build_change_surface_from_pairs({}).is_empty
    assert build_change_surface_from_pairs({}, old_contents={"a.py": "x"}).is_empty


def test_identical_old_new_yields_empty_surface() -> None:
    text = "def f():\n    return 1\n"
    surface = build_change_surface_from_pairs(
        {"mod.py": text},
        old_contents={"mod.py": text},
    )
    assert surface.is_empty


def test_new_file_when_old_contents_none() -> None:
    surface = build_change_surface_from_pairs({"a.py": _NEW}, old_contents=None)
    assert not surface.is_empty
    assert "### a.py ###" in surface.code
    assert "a.py" in surface.blocks


def test_new_file_when_key_missing_from_old_map() -> None:
    surface = build_change_surface_from_pairs(
        {"b.py": _NEW},
        old_contents={"other.py": "x\n"},
    )
    assert not surface.is_empty
    assert "### b.py ###" in surface.code


def test_modified_file_golden_parity_with_patch_path() -> None:
    new_contents = {"mod.py": _NEW}
    old_contents = {"mod.py": _OLD}
    patches = unified_diffs_from_pairs(new_contents, old_contents)
    via_pairs = build_change_surface_from_pairs(new_contents, old_contents)
    via_patches = build_change_surface_from_patches(
        patches,
        new_contents=new_contents,
    )
    assert not via_pairs.is_empty
    assert via_pairs == via_patches
    assert via_pairs.code == via_patches.code
```

In `backend/agents/software_engineering_team/tests/test_change_surface_api.py`, **delete** these two functions entirely (coverage moves to the new file):

- `test_build_from_pairs_nonempty_raises_not_implemented`
- `test_build_from_pairs_nonempty_with_old_raises_not_implemented`

Leave `test_build_from_pairs_empty_new_contents` and `test_build_from_pairs_empty_new_with_old_still_empty` in place.

- [ ] **Step 2: Run tests to verify they fail**

From `backend/`:

```bash
../.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_build_change_surface_from_pairs.py \
  -q --tb=short
```

If the main-repo venv lives at `backend/.venv` relative to the repo root (not the worktree), use that absolute path instead:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_build_change_surface_from_pairs.py \
  -q --tb=short
```

Expected: FAIL — `NotImplementedError` from `build_change_surface_from_pairs` on non-empty cases (or import/collection error if the new file is incomplete). Empty-map cases may already pass.

- [ ] **Step 3: Implement thin composition + update DbC**

In `backend/agents/software_engineering_team/code_review_agent/change_surface.py`, replace `build_change_surface_from_pairs` with:

```python
def build_change_surface_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> ChangeSurface:
    """Build a change surface from SE-style old/new content maps.

    Preconditions:
        - ``new_contents`` maps path → new-file content. May be empty.
        - ``old_contents``, when omitted/`None`, means empty old for every
          path. When provided, missing keys are treated as empty old for
          that path (same as ``unified_diffs_from_pairs``).

    Postconditions:
        - ``new_contents == {}`` → empty ``ChangeSurface`` regardless of
          ``old_contents``.
        - Otherwise equivalent to
          ``build_change_surface_from_patches(
              unified_diffs_from_pairs(new_contents, old_contents),
              new_contents=new_contents,
          )``: identical old/new → blank patch → path omitted; all-identical
          / no assemblable bodies → empty surface; new and modified files
          with assemblable bodies match the patch-path surface for those
          diffs.
        - Never raises for well-typed string mappings.
    """
    if not new_contents:
        return _empty_surface()
    patches = unified_diffs_from_pairs(new_contents, old_contents)
    return build_change_surface_from_patches(patches, new_contents=new_contents)
```

Do not change `unified_diffs_from_pairs` or `build_change_surface_from_patches` bodies.

- [ ] **Step 4: Run tests to verify they pass**

From `backend/` in the worktree:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_build_change_surface_from_pairs.py \
  agents/software_engineering_team/tests/test_change_surface_api.py \
  agents/software_engineering_team/tests/test_change_surface_from_patches.py \
  agents/software_engineering_team/tests/test_unified_diffs_from_pairs.py \
  -q --tb=short
```

Expected: PASS (all collected tests green).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/change_surface.py \
  backend/agents/software_engineering_team/tests/test_build_change_surface_from_pairs.py \
  backend/agents/software_engineering_team/tests/test_change_surface_api.py
git commit -m "$(cat <<'EOF'
Wire build_change_surface_from_pairs through the shared patch path.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Thin compose diffs → patch assembler | Task 1 Step 3 |
| Pass blank diffs through (no pre-filter) | Task 1 Step 3 |
| Empty `new_contents` → empty surface | Task 1 Steps 1 + 3 |
| Modified golden parity | Task 1 Step 1 |
| New-file surface | Task 1 Step 1 |
| Identical → empty surface | Task 1 Step 1 |
| Update DbC; remove NIE stub wording | Task 1 Step 3 |
| Remove NIE stub tests from API suite | Task 1 Step 1 |
| No issue numbers in code/commits | Global Constraints + Step 5 message |
