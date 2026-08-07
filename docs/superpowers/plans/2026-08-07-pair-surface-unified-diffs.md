# Pair Surface Unified Diffs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `unified_diffs_from_pairs` so SE-style old/new content maps become per-path unified-diff text (empty string when identical) without emitting a change surface.

**Architecture:** Thin `difflib.unified_diff` wrapper in `change_surface.py`: iterate `new_contents`, resolve old as `""` when the map is `None` or the key is missing, compare equality for the no-op case, otherwise emit a full unified diff with `a/<path>` / `b/<path>` headers.

**Tech Stack:** Python 3.10+, stdlib `difflib`, pytest, existing `extract_touched_lines`.

## Global Constraints

- Public API: `unified_diffs_from_pairs(new_contents, old_contents=None) -> dict[str, str]` in `change_surface.py`, exported in `__all__`.
- Identical old/new → keep key with value `""`.
- Missing key / `old_contents is None` → old = `""` (new-file semantics).
- Diff shape: full `difflib.unified_diff` with `fromfile=f"a/{path}"`, `tofile=f"b/{path}"`, `splitlines(keepends=True)`.
- Do not call `build_change_surface_from_patches` or implement `build_change_surface_from_pairs`.
- No network, LLM, or GitHub client in helpers or tests.
- Never reference GitHub issue numbers in code, comments, commit messages, or docs.
- DbC: `Preconditions:` / `Postconditions:` on the public function.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/change_surface.py` | Add + export `unified_diffs_from_pairs` |
| `backend/agents/software_engineering_team/tests/test_unified_diffs_from_pairs.py` | Focused unit tests (new) |

---

### Task 1: `unified_diffs_from_pairs`

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_unified_diffs_from_pairs.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/change_surface.py`

**Interfaces:**
- Consumes: `difflib.unified_diff`, `extract_touched_lines` (tests only)
- Produces: `unified_diffs_from_pairs(new_contents: Mapping[str, str], old_contents: Optional[Mapping[str, str]] = None) -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_unified_diffs_from_pairs.py`:

```python
"""Tests for ``unified_diffs_from_pairs``."""

from __future__ import annotations

from collections import OrderedDict

from software_engineering_team.code_review_agent.change_surface import (
    extract_touched_lines,
    unified_diffs_from_pairs,
)


def test_empty_new_contents() -> None:
    assert unified_diffs_from_pairs({}) == {}
    assert unified_diffs_from_pairs({}, old_contents={"a.py": "x"}) == {}


def test_identical_old_new_yields_empty_string() -> None:
    text = "def f():\n    return 1\n"
    out = unified_diffs_from_pairs({"mod.py": text}, old_contents={"mod.py": text})
    assert list(out.keys()) == ["mod.py"]
    assert out["mod.py"] == ""


def test_new_file_when_old_contents_none() -> None:
    new = "hello\n"
    out = unified_diffs_from_pairs({"a.txt": new}, old_contents=None)
    patch = out["a.txt"]
    assert patch.startswith("--- a/a.txt\n+++ b/a.txt\n")
    assert "@@" in patch
    assert "+hello" in patch
    assert extract_touched_lines(patch)


def test_new_file_when_key_missing_from_old_map() -> None:
    new = "only\n"
    out = unified_diffs_from_pairs(
        {"b.txt": new},
        old_contents={"other.txt": "x\n"},
    )
    patch = out["b.txt"]
    assert "--- a/b.txt\n+++ b/b.txt\n" in patch
    assert extract_touched_lines(patch)


def test_modified_file_diff() -> None:
    old = "a\nb\n"
    new = "a\nc\n"
    out = unified_diffs_from_pairs(
        OrderedDict([("m.txt", new)]),
        old_contents={"m.txt": old},
    )
    patch = out["m.txt"]
    assert patch.startswith("--- a/m.txt\n+++ b/m.txt\n")
    assert "-b" in patch
    assert "+c" in patch
    assert extract_touched_lines(patch) == frozenset({2})


def test_preserves_new_contents_key_order() -> None:
    out = unified_diffs_from_pairs(
        OrderedDict([("z.py", "z\n"), ("a.py", "a\n")]),
        old_contents=None,
    )
    assert list(out.keys()) == ["z.py", "a.py"]
```

- [ ] **Step 2: Run tests to verify they fail**

From `backend/`:

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_unified_diffs_from_pairs.py -v
```

(If needed: `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest ...`)

Expected: FAIL with cannot import `unified_diffs_from_pairs`.

- [ ] **Step 3: Implement `unified_diffs_from_pairs`**

In `change_surface.py`:

1. Add `import difflib` near the other imports.
2. Add `"unified_diffs_from_pairs"` to `__all__` (keep alphabetical: after `render_patch_hunks` or sorted with the list).
3. Add the function (near `extract_touched_lines` / before the pairs stub is fine):

```python
def unified_diffs_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Build per-path unified diffs from SE-style old/new content maps.

    Preconditions:
        - ``new_contents`` maps path → new-file text (may be empty).
        - ``old_contents``, when omitted/`None`, means empty old for every
          path. When provided, missing keys are treated as empty old for
          that path.

    Postconditions:
        - ``new_contents == {}`` → ``{}``.
        - Result contains exactly the keys of ``new_contents`` (insertion
          order preserved).
        - For each path: if resolved old text equals new text → ``\"\"``;
          otherwise a non-empty ``difflib.unified_diff`` string with
          ``fromfile=f\"a/{path}\"``, ``tofile=f\"b/{path}\"``, using
          ``splitlines(keepends=True)``.
        - Paths present only in ``old_contents`` are ignored.
        - Never raises for well-typed string mappings.
    """
    if not new_contents:
        return {}
    old_map = old_contents  # None means empty old for every path
    out: dict[str, str] = {}
    for path, new_text in new_contents.items():
        if old_map is None:
            old_text = ""
        else:
            old_text = old_map[path] if path in old_map else ""
        if old_text == new_text:
            out[path] = ""
            continue
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        out[path] = "".join(diff)
    return out
```

4. Optionally extend the module docstring one sentence noting pairs→diff helper exists; do **not** claim pairs surface assembly is done.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_unified_diffs_from_pairs.py \
  agents/software_engineering_team/tests/test_change_surface_api.py \
  -v
```

Expected: all PASS. Confirm `build_change_surface_from_pairs` still raises `NotImplementedError` for non-empty new contents.

If `test_modified_file_diff` fails on `extract_touched_lines` line numbers, print the patch and align the expected frozenset to the actual added new-file line (do not change `extract_touched_lines`).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/change_surface.py \
  backend/agents/software_engineering_team/tests/test_unified_diffs_from_pairs.py
git commit -m "$(cat <<'EOF'
Add unified_diffs_from_pairs for SE old/new content maps.

EOF
)"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| Public `unified_diffs_from_pairs` + `__all__` | Task 1 |
| Identical → `""` | Task 1 |
| New file (`None` / missing key) | Task 1 |
| Full `difflib` headers | Task 1 |
| Consumable by `extract_touched_lines` | Task 1 |
| No surface emission / pairs stub unchanged | Explicit non-touch |

## Placeholder scan

No TBD/TODO steps; code and commands are concrete.
