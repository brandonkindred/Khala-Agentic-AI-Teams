# `_write_draft_to_path` Preconditions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce Design-by-Contract preconditions on `_write_draft_to_path` so non-string `draft` and non-`str`/`Path` `path` raise clear `TypeError`s before any filesystem I/O.

**Architecture:** Add explicit `isinstance` guards at the top of the private helper in `blog_writer_agent/agent.py`, document Preconditions/Postconditions in the docstring, and cover rejection paths with unit tests next to the existing happy-path test. No call-site changes.

**Tech Stack:** Python 3.10+, pytest, pathlib

**Spec:** `docs/superpowers/specs/2026-07-24-write-draft-to-path-preconditions-design.md`

## Global Constraints

- Validation style: explicit `isinstance` + `TypeError` (not `assert`)
- Empty-string `draft` remains allowed
- No call-site refactors
- Do not mention GitHub issue numbers in code, comments, docs, or commit messages
- Work in the existing worktree at `.worktrees/fix-2376-write-draft-precondition` on branch `fix-2376-write-draft-precondition`

## File map

| File | Role |
|---|---|
| `backend/agents/blogging/blog_writer_agent/agent.py` | Modify `_write_draft_to_path` (~line 156): guards + docstring |
| `backend/agents/blogging/tests/test_writer_and_v2_helpers.py` | Add rejection tests after `test_write_draft_to_path_creates_parents` |

---

### Task 1: Failing rejection tests

**Files:**
- Modify: `backend/agents/blogging/tests/test_writer_and_v2_helpers.py` (after `test_write_draft_to_path_creates_parents`, ~line 57)
- Test: same file

**Interfaces:**
- Consumes: `_write_draft_to_path(draft: str, path: Union[str, Path]) -> None` from `agents.blogging.blog_writer_agent.agent`
- Produces: four parametrized/focused tests that expect `TypeError` with the exact message prefixes from the spec

- [ ] **Step 1: Add rejection tests after the existing happy-path test**

Insert immediately after `test_write_draft_to_path_creates_parents`:

```python
@pytest.mark.parametrize("draft", [None, 123])
def test_write_draft_to_path_rejects_non_string_draft(tmp_path: Path, draft: object) -> None:
    from agents.blogging.blog_writer_agent.agent import _write_draft_to_path

    target = tmp_path / "draft.md"
    with pytest.raises(TypeError, match="draft must be a string"):
        _write_draft_to_path(draft, target)  # type: ignore[arg-type]
    assert not target.exists()


@pytest.mark.parametrize("path", [None, 123])
def test_write_draft_to_path_rejects_invalid_path(tmp_path: Path, path: object) -> None:
    from agents.blogging.blog_writer_agent.agent import _write_draft_to_path

    with pytest.raises(TypeError, match="path must be a str or Path"):
        _write_draft_to_path("# draft\n", path)  # type: ignore[arg-type]
```

Keep `test_write_draft_to_path_creates_parents` unchanged.

- [ ] **Step 2: Run the new tests and confirm they fail for the right reason**

Run from `backend/`:

```bash
cd backend && python -m pytest agents/blogging/tests/test_writer_and_v2_helpers.py::test_write_draft_to_path_rejects_non_string_draft agents/blogging/tests/test_writer_and_v2_helpers.py::test_write_draft_to_path_rejects_invalid_path -v
```

Expected: FAIL — either no match on the `TypeError` message (opaque `write_text`/`Path` error) or wrong exception type. Do **not** proceed if the tests unexpectedly PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/agents/blogging/tests/test_writer_and_v2_helpers.py
git commit -m "$(cat <<'EOF'
Add failing tests for _write_draft_to_path type preconditions.

EOF
)"
```

---

### Task 2: Implement guards and docstring

**Files:**
- Modify: `backend/agents/blogging/blog_writer_agent/agent.py` (`_write_draft_to_path`, currently ~lines 156–161)
- Test: `backend/agents/blogging/tests/test_writer_and_v2_helpers.py`

**Interfaces:**
- Consumes: rejection expectations from Task 1
- Produces: `_write_draft_to_path(draft: str, path: Union[str, Path]) -> None` with documented Preconditions/Postconditions and `TypeError` on violation

- [ ] **Step 1: Replace `_write_draft_to_path` with the guarded implementation**

Replace the existing function body and docstring with:

```python
def _write_draft_to_path(draft: str, path: Union[str, Path]) -> None:
    """Write draft content to path; create parent dirs if needed. Log the saved path.

    Preconditions:
        - ``draft`` must be a string (may be empty).
        - ``path`` must be a ``str`` or ``pathlib.Path``.
    Postconditions:
        - Parent directories of ``path`` exist.
        - The resolved path contains ``draft`` as UTF-8 text.
        - A success log records the resolved path.
    """
    if not isinstance(draft, str):
        raise TypeError(f"draft must be a string, got {type(draft).__name__}")
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path must be a str or Path, got {type(path).__name__}")
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(draft, encoding="utf-8")
    logger.info("Draft written to %s", p)
```

`Path` is already imported at module top (`from pathlib import Path`). Do not change call sites.

- [ ] **Step 2: Run rejection tests — expect PASS**

```bash
cd backend && python -m pytest agents/blogging/tests/test_writer_and_v2_helpers.py::test_write_draft_to_path_rejects_non_string_draft agents/blogging/tests/test_writer_and_v2_helpers.py::test_write_draft_to_path_rejects_invalid_path -v
```

Expected: PASS (4 parametrized cases).

- [ ] **Step 3: Run the happy-path test — expect PASS**

```bash
cd backend && python -m pytest agents/blogging/tests/test_writer_and_v2_helpers.py::test_write_draft_to_path_creates_parents -v
```

Expected: PASS.

- [ ] **Step 4: Run the full helper test module**

```bash
cd backend && python -m pytest agents/blogging/tests/test_writer_and_v2_helpers.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/blogging/blog_writer_agent/agent.py
git commit -m "$(cat <<'EOF'
Enforce draft and path type preconditions in _write_draft_to_path.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `draft` must be `str`; else `TypeError` with `draft must be a string` | Task 1 + 2 |
| `path` must be `str` or `Path`; else `TypeError` with `path must be a str or Path` | Task 1 + 2 |
| No I/O on rejection (`assert not target.exists()`) | Task 1 |
| Preconditions/Postconditions docstring | Task 2 |
| Empty draft allowed / call sites unchanged / no broader audit | Global constraints + out of scope |
