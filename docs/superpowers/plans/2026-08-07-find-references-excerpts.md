# find_references Enclosing-Construct Excerpts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach enclosing function/class excerpts to each `find_references` hit when boundary helpers resolve.

**Architecture:** After hit collection (unchanged), format each hit as `path:line` plus, for `.py`/`.pyi` with a resolved `enclosing_construct`, the same slice as `read_function` via `_format_construct_slice`. Unresolved hits stay `path:line` only. Truncation / no-reader banners still append after the hit body.

**Tech Stack:** Python 3.10+, pytest, existing `enclosing_construct` / `strip_numbered_prefixes` / `_format_construct_slice`

## Global Constraints

- Work only in worktree `.worktrees/5446-find-references-excerpts` on branch `feature/5446-find-references-excerpts`
- Design-by-Contract docstrings on new/updated helpers and `find_references`
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Do not implement size cap or line-window fallback (sibling)
- Do not change hit discovery / match caps / truncation semantics
- Do not modify `_build_tools` or prompts

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | `_format_reference_hit` + wire into `find_references` |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Excerpt tests + update existing hit parsers |

---

### Task 1: Attach construct excerpts to find_references hits

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Modify: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py`

**Interfaces:**
- Consumes: `CodebaseIndex._read`, `strip_numbered_prefixes`, `enclosing_construct`, `_format_construct_slice`
- Produces:
  - `_format_reference_hit(index: CodebaseIndex, path: str, lineno: int) -> str`
  - Updated `find_references` body formatting via `"\n\n".join(...)` of hit blocks

- [ ] **Step 1: Add test helpers + failing excerpt tests; update brittle existing asserts**

Near `_NO_REPO`, add:

```python
import re

_HIT_LOC_RE = re.compile(r"^.+:\d+$")


def _hit_body(result: str) -> str:
    """Strip trailing no-reader / truncation banners from a find_references result."""
    for marker in ("\n\n(Scan truncated", f"\n\n{_NO_REPO}"):
        if marker in result:
            return result.split(marker, 1)[0]
    return result


def _hit_locs(result: str) -> list[str]:
    """Return path:line locator lines from the hit body (ignore excerpt bodies)."""
    return [ln for ln in _hit_body(result).splitlines() if _HIT_LOC_RE.match(ln)]
```

Update existing tests that parse hits:

- `test_find_references_respects_max_matches` → `assert _hit_locs(result) == [...]`
- `test_find_references_merges_submission_then_repo_under_cap` → use `_hit_locs`; keep `"Scan truncated" in result`
- `test_find_references_skips_submission_paths_in_repo_half` → use `_hit_locs`
- `test_find_references_truncated_banner_when_match_cap_skips_repo` → use `_hit_locs`
- `test_find_references_no_reader_unchanged` for `"foo"`: assert `"a.py:1" in _hit_locs(result)`, `"function foo" in result` or construct body, and `_NO_REPO in result` (exact equality will break once excerpts attach)
- `test_find_references_no_reader_note_on_hits` / `returns_capped_path_line_hits`: still check locs + `_NO_REPO`; capped test may include excerpts for `foo` hits — assert locs contain the three paths and no line text after `path:line` on locator lines
- `test_find_references_includes_repo_reader_hits`: assert `"other/caller.py:1" in _hit_locs(result)`

Add:

```python
def test_find_references_attaches_enclosing_construct_excerpt() -> None:
    """A hit inside a Python function includes the construct slice."""
    src = (
        "def outer():\n"
        "    return 1\n"
        "\n"
        "def caller():\n"
        "    return outer()\n"
    )
    idx = CodebaseIndex(files={"mod.py": src})
    result = idx.find_references("outer")
    assert "mod.py:5" in _hit_locs(result)
    assert "function caller" in result
    assert "return outer()" in result
    assert "def outer():" in result  # definition hit may also appear
    assert _NO_REPO in result


def test_find_references_unresolved_construct_is_path_line_only() -> None:
    """Module-level / non-Python hits stay path:line without a construct slice."""
    idx = CodebaseIndex(
        files={
            "mod.py": "NEEDLE = 1\n\ndef f():\n    return NEEDLE\n",
            "note.txt": "NEEDLE appears here\n",
        }
    )
    result = idx.find_references("NEEDLE")
    locs = _hit_locs(result)
    assert "mod.py:1" in locs
    assert "note.txt:1" in locs
    # module-level assignment has no enclosing construct
    body = _hit_body(result)
    # The mod.py:1 block should not include a "function"/"class" header before the next hit
    first_block = body.split("\n\n")[0]
    assert first_block.strip() == "mod.py:1"
    assert "note.txt:1" in locs
```

Refine the unresolved test if `NEEDLE` on line 4 inside `f` also matches — that's fine; assert the module-level block is path:line only:

```python
def test_find_references_unresolved_construct_is_path_line_only() -> None:
    idx = CodebaseIndex(files={"mod.py": "NEEDLE = 1\n"})
    result = idx.find_references("NEEDLE")
    assert _hit_locs(result) == ["mod.py:1"]
    assert _hit_body(result).strip() == "mod.py:1"
    assert "function" not in result
    assert "class" not in result
    assert _NO_REPO in result
```

- [ ] **Step 2: Run tests RED**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5446-find-references-excerpts/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -k find_references -v
```

Expected: new excerpt tests fail (no construct text); some updated exact-equality tests fail until implementation.

- [ ] **Step 3: Implement `_format_reference_hit` and wire `find_references`**

Add after `_format_construct_slice` (or immediately above `find_references` usage site as a module-level function taking `index`):

```python
def _format_reference_hit(index: CodebaseIndex, path: str, lineno: int) -> str:
    """Format one find_references hit as path:line plus optional construct excerpt.

    Preconditions:
        - ``lineno`` >= 1.

    Postconditions:
        - Always starts with ``{path}:{lineno}``.
        - When readable ``.py``/``.pyi`` content has an enclosing construct at
          ``lineno``, appends a blank-line-free construct slice from
          ``_format_construct_slice`` (same shape as ``read_function``).
        - Otherwise returns only ``{path}:{lineno}`` (no fallback window).
        - Never raises.
    """
    loc = f"{path}:{lineno}"
    content, _error = index._read(path)
    if content is None:
        return loc
    display = index.resolve_path(path) or path
    if display == index.EXISTING_CODEBASE_PATH:
        display = path
    _, ext = os.path.splitext(display)
    if ext.lower() not in (".py", ".pyi"):
        return loc
    try:
        stripped, physical, mapper = strip_numbered_prefixes(content, lineno)
        construct = enclosing_construct(
            stripped, physical, annotated_hunks=mapper is not None
        )
    except Exception:  # noqa: BLE001 - excerpt failure must not abort find_references
        return loc
    if construct is None:
        return loc
    excerpt = _format_construct_slice(
        display, construct, stripped.splitlines(), mapper=mapper
    )
    return f"{loc}\n{excerpt}"
```

In `find_references`, replace hit formatting:

Where no-reader body builds from hits:

```python
            body = (
                "\n\n".join(
                    _format_reference_hit(self, path, lineno) for path, lineno, _ in hits
                )
                if hits
                else f"No references for {symbol!r}."
            )
```

Where reader path builds `result`:

```python
        result = "\n\n".join(
            _format_reference_hit(self, path, lineno) for path, lineno, _ in hits
        )
```

Update `find_references` docstring postconditions to mention excerpts.

Note: `_format_construct_slice` is defined *after* `find_references` in the file today. Either move `_format_reference_hit` below `_format_construct_slice`, or keep it as a nested/later function — place `_format_reference_hit` immediately after `_format_construct_slice` and call it from `find_references` (forward reference is fine at runtime when the method runs).

- [ ] **Step 4: Run tests GREEN**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Attach enclosing-construct excerpts to find_references hits.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Excerpt when boundaries resolve | Task 1 |
| path:line only when unresolved | Task 1 |
| Reuse construct slice format | Task 1 |
| No size cap / fallback | Global constraints |
| Truncation / no-reader unchanged | Task 1 (banners after body) |
| Unit test excerpt contains construct | Task 1 Step 1 |
