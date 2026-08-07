# Collapse Multi-Hunk Dedupe AC Regression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lock in assembly-level behavior that a two-hunk patch touching one enclosing construct emits that construct span once in the change surface.

**Architecture:** Test-only leaf. Production collapse already happens in `_assemble_path_block` via `expand_touched_ranges` (dedupe) + `_merge_line_ranges`. Add one regression test on `build_change_surface_from_patches`.

**Tech Stack:** Python 3.10+, pytest, existing change-surface assembly API.

## Global Constraints

- No production code changes in `change_surface.py` (or elsewhere) unless the new test fails — if it fails, stop and escalate; do not invent a new collapse helper without a design update.
- Test lives in `test_change_surface_from_patches.py`.
- Input must be a real two-`@@` unified-diff patch (not expand-only touched-line sets).
- Assert a single construct span: function `def` appears once; no `...` gap marker; prefer full body equality.
- No network, LLM, or GitHub client.
- Never reference GitHub issue numbers in code, comments, commit messages, or docs.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py` | Add multi-hunk same-function collapse regression test |

---

### Task 1: Multi-hunk same-construct assembly regression

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py`

**Interfaces:**
- Consumes: `build_change_surface_from_patches(patches, *, new_contents=None) -> ChangeSurface`
- Produces: new test `test_build_from_patches_two_hunks_same_function_emits_one_span`

- [ ] **Step 1: Add the regression test**

Append to `backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py`:

```python
def test_build_from_patches_two_hunks_same_function_emits_one_span() -> None:
    """Two hunks in one function must not duplicate the expanded construct."""
    content = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    # Hunk 1 touches new-file line 2; hunk 2 touches new-file line 4.
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " def f():\n"
        "-    a = 0\n"
        "+    a = 1\n"
        "     b = 2\n"
        "@@ -3,2 +3,2 @@\n"
        "     b = 2\n"
        "-    return a\n"
        "+    return a + b\n"
    )
    surface = build_change_surface_from_patches(
        {"f.py": patch},
        new_contents={"f.py": content},
    )
    assert not surface.is_empty
    body = surface.blocks["f.py"]
    assert body.count("def f():") == 1
    assert "..." not in body
    assert body == (
        "1: def f():\n"
        "2:     a = 1\n"
        "3:     b = 2\n"
        "4:     return a + b"
    )
    assert surface.code == f"### f.py ###\n{body}"
```

- [ ] **Step 2: Run the test (expect PASS — behavior already on main)**

From the worktree `backend/` directory:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_from_patches.py::test_build_from_patches_two_hunks_same_function_emits_one_span \
  -q --tb=short
```

Expected: PASS (1 passed).

If FAIL: **stop and report BLOCKED** with the failure output — do not change production code under this plan without escalating.

Also run the full assembly file:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_change_surface_from_patches.py \
  -q --tb=short
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_change_surface_from_patches.py
git commit -m "$(cat <<'EOF'
Add assembly regression for multi-hunk same-construct collapse.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| No production code change | Task 1 (test only) |
| Two-`@@` patch input | Task 1 Step 1 |
| Single construct span (`def` once, no `...`) | Task 1 Step 1 assertions |
| Prefer full body equality | Task 1 Step 1 |
| Test in `test_change_surface_from_patches.py` | Task 1 |
| No issue numbers in commit | Task 1 Step 3 message |
