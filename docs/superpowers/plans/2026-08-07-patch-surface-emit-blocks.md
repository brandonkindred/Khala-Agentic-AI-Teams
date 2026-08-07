# Patch Surface Emit Blocks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `build_change_surface_from_patches` so PR/unified patches plus new-file contents become a pre-numbered `ChangeSurface` via touched-line extraction, enclosing-construct expansion, range merge, and `### path ###` formatting.

**Architecture:** Private helpers `_merge_line_ranges`, `_pre_number_ranges`, and `_assemble_path_block` do per-path work; the public builder iterates `patches` in insertion order, omits paths without usable content or added lines, and returns `ChangeSurface(blocks=OrderedDict(...))`.

**Tech Stack:** Python 3.10+, pytest, existing `change_surface` helpers (`extract_touched_lines`, `expand_touched_ranges`, `format_change_surface_code`).

## Global Constraints

- Body source = expanded slices from `new_contents` (never `render_patch_hunks` as the emitted body).
- Missing / blank `new_contents` for a path → omit that path.
- Empty touched set (no `+` lines) → omit that path.
- Merge overlapping/adjacent ranges (`next.start_line <= prev.end_line + 1`), then join remaining gaps with a bare `...` line.
- Do not implement `build_change_surface_from_pairs`.
- No network, LLM, or GitHub client in helpers or tests.
- Never reference GitHub issue numbers in code, comments, commit messages, or docs.
- DbC: every new public function (and new private helpers that are non-trivial) documents `Preconditions:` / `Postconditions:`.
- Update the stub docstring so postconditions match real behavior (no `NotImplementedError`).

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/change_surface.py` | Private merge/pre-number/assemble helpers; implement builder |
| `backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py` | Focused assembly tests (new) |
| `backend/agents/software_engineering_team/tests/test_change_surface_api.py` | Replace non-empty `NotImplementedError` stub test |

---

### Task 1: Merge + pre-number helpers

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/change_surface.py`
- Create: `backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py`

**Interfaces:**
- Consumes: `LineRange`
- Produces:
  - `_merge_line_ranges(ranges: Sequence[LineRange]) -> tuple[LineRange, ...]`
  - `_pre_number_ranges(content: str, ranges: Sequence[LineRange]) -> str`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py`:

```python
"""Assembly tests for ``build_change_surface_from_patches``."""

from __future__ import annotations

from collections import OrderedDict

from software_engineering_team.code_review_agent.change_surface import (
    LineRange,
    _merge_line_ranges,
    _pre_number_ranges,
)


def test_merge_line_ranges_overlaps_and_adjacent() -> None:
    ranges = (
        LineRange(5, 7),
        LineRange(1, 2),
        LineRange(3, 4),  # adjacent to 1-2 → merge to 1-4
        LineRange(6, 9),  # overlaps 5-7 → 5-9
    )
    assert _merge_line_ranges(ranges) == (LineRange(1, 4), LineRange(5, 9))


def test_merge_line_ranges_empty() -> None:
    assert _merge_line_ranges(()) == ()
    assert _merge_line_ranges([]) == ()


def test_pre_number_ranges_single_span() -> None:
    content = "a\nb\nc\n"
    body = _pre_number_ranges(content, (LineRange(2, 3),))
    assert body == "2: b\n3: c"


def test_pre_number_ranges_inserts_gap_marker() -> None:
    content = "a\nb\nc\nd\ne\n"
    body = _pre_number_ranges(content, (LineRange(1, 1), LineRange(4, 5)))
    assert body == "1: a\n...\n4: d\n5: e"
```

- [ ] **Step 2: Run tests to verify they fail**

From `backend/`:

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_from_patches.py -v
```

Expected: FAIL with cannot import `_merge_line_ranges` / `_pre_number_ranges`.

(If the main-repo venv is needed: `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest ...` with cwd `backend/`.)

- [ ] **Step 3: Implement helpers**

In `change_surface.py`, add (near other private helpers; before `build_change_surface_from_patches` is fine):

```python
def _merge_line_ranges(ranges: Sequence[LineRange]) -> tuple[LineRange, ...]:
    """Merge overlapping or adjacent inclusive line ranges.

    Preconditions:
        - ``ranges`` is a sequence of valid ``LineRange`` values (may be empty).

    Postconditions:
        - Returns sorted unique merged ranges where each next range starts at
          ``prev.end_line + 2`` or later (overlap or ``start <= end + 1`` merges).
        - Empty input → ``()``.
        - Never raises for valid ``LineRange`` inputs.
    """
    if not ranges:
        return ()
    ordered = sorted(ranges, key=lambda r: (r.start_line, r.end_line))
    merged: list[LineRange] = [ordered[0]]
    for r in ordered[1:]:
        cur = merged[-1]
        if r.start_line <= cur.end_line + 1:
            merged[-1] = LineRange(
                start_line=cur.start_line,
                end_line=max(cur.end_line, r.end_line),
            )
        else:
            merged.append(r)
    return tuple(merged)


def _pre_number_ranges(content: str, ranges: Sequence[LineRange]) -> str:
    """Render merged-or-raw ranges as pre-numbered body text with gap markers.

    Preconditions:
        - ``content`` is the full new-file text (may be empty).
        - ``ranges`` is a sequence of inclusive 1-based ``LineRange`` values
          (caller should merge first when desired).

    Postconditions:
        - Emits ``f\"{n}: {line}\"`` for each line in each range, clamped to the
          file's last line when ``end_line`` exceeds length.
        - Between successive ranges, inserts a bare ``...`` line.
        - Empty ``ranges`` or empty file with no emitable lines → ``\"\"``.
        - Never raises.
    """
    lines = content.splitlines()
    if not ranges or not lines:
        # Empty ranges → ""; empty file cannot emit numbered lines.
        if not ranges:
            return ""
        return ""
    total = len(lines)
    chunks: list[str] = []
    for idx, r in enumerate(ranges):
        if idx > 0:
            chunks.append("...")
        start = min(max(1, r.start_line), total)
        end = min(max(start, r.end_line), total)
        for n in range(start, end + 1):
            chunks.append(f"{n}: {lines[n - 1]}")
    return "\n".join(chunks)
```

Simplify empty handling in `_pre_number_ranges` to a single early return:

```python
    lines = content.splitlines()
    if not ranges or not lines:
        return ""
```

Do **not** export these helpers in `__all__` (private). Tests may import the underscored names (same pattern as other SE internal-helper tests if present; this plan intentionally tests them directly).

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_from_patches.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/change_surface.py \
  backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py
git commit -m "$(cat <<'EOF'
Add line-range merge and pre-number helpers for patch surfaces.

EOF
)"
```

---

### Task 2: Assemble path blocks + implement builder

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/change_surface.py`
- Modify: `backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py`
- Modify: `backend/agents/software_engineering_team/tests/test_change_surface_api.py`

**Interfaces:**
- Consumes: `extract_touched_lines`, `expand_touched_ranges`, `_merge_line_ranges`, `_pre_number_ranges`, `format_change_surface_code`, `_mapping_has_nonblank_value`, `_empty_surface`
- Produces:
  - `_assemble_path_block(path: str, patch: str, content: str) -> Optional[str]`
  - `build_change_surface_from_patches(...)` fully implemented (no `NotImplementedError`)

- [ ] **Step 1: Write the failing / updated tests**

Append to `test_change_surface_from_patches.py` (update imports):

```python
from software_engineering_team.code_review_agent.change_surface import (
    LineRange,
    _merge_line_ranges,
    _pre_number_ranges,
    build_change_surface_from_patches,
)

_PY_CONTENT = "def outer():\n    return 1\n\nx = 1\n"
# Touch the body line of ``outer`` (new-file line 2).
_PY_PATCH = "@@ -1,2 +1,2 @@\n def outer():\n-    return 0\n+    return 1\n"


def test_build_from_patches_single_file_expands_construct() -> None:
    surface = build_change_surface_from_patches(
        {"mod.py": _PY_PATCH},
        new_contents={"mod.py": _PY_CONTENT},
    )
    assert not surface.is_empty
    assert list(surface.blocks.keys()) == ["mod.py"]
    # AST expansion of line 2 → enclosing ``outer`` (lines 1-2).
    assert surface.blocks["mod.py"] == "1: def outer():\n2:     return 1"
    assert surface.code == "### mod.py ###\n1: def outer():\n2:     return 1"


def test_build_from_patches_multi_file() -> None:
    ts_content = "function f() {\n  return 1;\n}\n"
    ts_patch = "@@ -1,3 +1,3 @@\n function f() {\n-  return 0;\n+  return 1;\n }\n"
    surface = build_change_surface_from_patches(
        OrderedDict(
            [
                ("mod.py", _PY_PATCH),
                ("f.ts", ts_patch),
            ]
        ),
        new_contents={"mod.py": _PY_CONTENT, "f.ts": ts_content},
    )
    assert list(surface.blocks.keys()) == ["mod.py", "f.ts"]
    assert "### mod.py ###" in surface.code
    assert "### f.ts ###" in surface.code


def test_build_from_patches_omits_without_new_contents() -> None:
    surface = build_change_surface_from_patches(
        {"mod.py": _PY_PATCH},
        new_contents=None,
    )
    assert surface.is_empty


def test_build_from_patches_omits_path_missing_content_keeps_other() -> None:
    surface = build_change_surface_from_patches(
        OrderedDict([("skip.py", _PY_PATCH), ("mod.py", _PY_PATCH)]),
        new_contents={"mod.py": _PY_CONTENT},
    )
    assert list(surface.blocks.keys()) == ["mod.py"]


def test_build_from_patches_omits_when_no_added_lines() -> None:
    # Context-only hunk: no '+' lines → empty touched set → omit.
    patch = "@@ -1,2 +1,2 @@\n def outer():\n     return 1\n"
    surface = build_change_surface_from_patches(
        {"mod.py": patch},
        new_contents={"mod.py": _PY_CONTENT},
    )
    assert surface.is_empty
```

In `test_change_surface_api.py`, **replace** `test_build_from_patches_nonempty_raises_not_implemented` with:

```python
def test_build_from_patches_nonempty_assembles_when_content_provided() -> None:
    content = "def outer():\n    return 1\n"
    patch = "@@ -1,2 +1,2 @@\n def outer():\n-    return 0\n+    return 1\n"
    surface = build_change_surface_from_patches(
        {"a.py": patch},
        new_contents={"a.py": content},
    )
    assert not surface.is_empty
    assert "### a.py ###" in surface.code
```

Keep `test_build_from_patches_empty_mapping` and `test_build_from_patches_all_blank_patches` unchanged.

- [ ] **Step 2: Run tests to verify new expectations fail**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_from_patches.py \
  agents/software_engineering_team/tests/test_change_surface_api.py::test_build_from_patches_nonempty_assembles_when_content_provided \
  -v
```

Expected: FAIL with `NotImplementedError` from the stub builder (merge/pre-number tests from Task 1 still pass).

- [ ] **Step 3: Implement `_assemble_path_block` and the builder**

```python
from collections import OrderedDict  # add to imports at top of change_surface.py
```

```python
def _assemble_path_block(path: str, patch: str, content: str) -> Optional[str]:
    """Build one path's pre-numbered body, or ``None`` to omit the path.

    Preconditions:
        - ``path`` is the review path key (may be empty string).
        - ``patch`` is one file's unified-diff text.
        - ``content`` is the full new-file text for expansion (caller must not
          pass blank content; blank is treated as omit).

    Postconditions:
        - Blank ``content`` → ``None``.
        - Empty ``extract_touched_lines(patch)`` → ``None``.
        - Otherwise expands, merges, and pre-numbers; empty body → ``None``.
        - Never raises.
    """
    if not (content or "").strip():
        return None
    touched = extract_touched_lines(patch)
    if not touched:
        return None
    ranges = expand_touched_ranges(content, touched, path=path)
    merged = _merge_line_ranges(ranges)
    body = _pre_number_ranges(content, merged)
    if not body.strip():
        return None
    return body


def build_change_surface_from_patches(
    patches: Mapping[str, str],
    *,
    new_contents: Optional[Mapping[str, str]] = None,
) -> ChangeSurface:
    """Build a change surface from per-path unified / PR patch text.

    Preconditions:
        - ``patches`` maps path → one file's unified-diff text (GitHub
          ``files[].patch`` style). May be empty.
        - ``new_contents``, when provided, maps path → full new-file content
          used for enclosing-construct expansion. Omitted/`None` means no
          content for any path (all non-blank patches are omitted).

    Postconditions:
        - ``patches == {}`` or every patch value is blank → empty
          ``ChangeSurface`` (``code == ""``, ``blocks == {}``).
        - For each path with a non-blank patch, in iteration order: omit when
          ``new_contents`` is missing/blank for that path, when there are no
          added touched lines, or when the assembled body is empty; otherwise
          include a pre-numbered expanded body.
        - ``ChangeSurface.code`` equals ``format_change_surface_code(blocks)``.
        - Never raises for well-typed string mappings.
    """
    if not _mapping_has_nonblank_value(patches):
        return _empty_surface()
    contents = new_contents or {}
    blocks: OrderedDict[str, str] = OrderedDict()
    for path, patch in patches.items():
        if not (patch or "").strip():
            continue
        body = _assemble_path_block(path, patch, contents.get(path, ""))
        if body is not None:
            blocks[path] = body
    if not blocks:
        return _empty_surface()
    return ChangeSurface(blocks=blocks)
```

Also update the module docstring’s “Surface assembly is owned by follow-on work” sentence to state that the patch path is implemented (pairs path remains follow-on). **Do not** put GitHub issue numbers in the docstring.

- [ ] **Step 4: Run focused suites**

```bash
python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_from_patches.py \
  agents/software_engineering_team/tests/test_change_surface_api.py \
  agents/software_engineering_team/tests/test_change_surface_patch_parse.py \
  agents/software_engineering_team/tests/test_expand_touched_ranges_ast.py \
  agents/software_engineering_team/tests/test_expand_touched_ranges_fallback.py \
  -v
```

Expected: all PASS.

If `test_build_from_patches_single_file_expands_construct` fails because expansion bounds differ, adjust the expected body to match `expand_touched_ranges(_PY_CONTENT, extract_touched_lines(_PY_PATCH), path="mod.py")` then `_merge_line_ranges` / `_pre_number_ranges` — do **not** change expansion behavior.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/change_surface.py \
  backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py \
  backend/agents/software_engineering_team/tests/test_change_surface_api.py
git commit -m "$(cat <<'EOF'
Implement patch change-surface assembly with expansion.

EOF
)"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| Expanded slices from `new_contents` | Task 2 |
| Omit missing/blank content | Task 2 |
| Omit empty touched set | Task 2 |
| Merge adjacent/overlap + `...` gaps | Task 1 + Task 2 |
| Private per-path assembler + public builder | Task 2 |
| Multi-file test | Task 2 |
| Replace `NotImplementedError` stub test | Task 2 |
| No pairs path / no annotated body | Explicit non-touch |

## Placeholder scan

No TBD/TODO steps; commands and code are concrete.
