# PR Admission Change Surface Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** During PR admission, build a head-backed change surface via the shared builder and attach it to `ReviewModeDecision` without changing reviewer dispatch.

**Architecture:** Add `_build_change_surface_for_reviewable(files, head_files)` that selects reviewable files present in `head_files`, maps their patches, and calls `build_change_surface_from_patches`. Wire it into `_decide_review_mode` after head fetch. Extend `ReviewModeDecision` with required `change_surface: ChangeSurface` (empty when nothing assemblable). Leave `code` / `_run_reviewer` unchanged.

**Tech Stack:** Python 3.10+, existing `change_surface` builder, pytest, existing `PullRequestFile` / `_decide_review_mode` fixtures in `test_coding_team_review_pr.py`.

## Global Constraints

- Always-present `change_surface: ChangeSurface` on `ReviewModeDecision` (never `None`; empty when nothing assemblable).
- Eligibility: `_is_whole_file_reviewable(f)` **and** `f.filename in head_files`.
- Call `build_change_surface_from_patches(patches, new_contents=head_files)` — do not reimplement expand/merge/pre-number.
- Do not change `_run_reviewer` / primary `code=` selection (dispatch leaf).
- Do not change focus notes or SE paths.
- Offline tests only (mock / inject head content; no live GitHub).
- Never reference GitHub issue numbers in code, comments, commit messages, or docs.
- DbC: `Preconditions:` / `Postconditions:` on the new helper.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/api/pr_review.py` | Helper + `ReviewModeDecision` field + wire into `_decide_review_mode` |
| `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py` | Focused helper unit tests (preferred home for pure helper) |
| `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py` | Assert `change_surface` on existing `_decide_review_mode` cases |

---

### Task 1: Helper + `ReviewModeDecision` field + admission wire

**Files:**
- Modify: `backend/agents/software_engineering_team/api/pr_review.py`
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py`
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py`

**Interfaces:**
- Consumes: `build_change_surface_from_patches`, `_is_whole_file_reviewable`, `ChangeSurface`
- Produces: `_build_change_surface_for_reviewable(files, head_files) -> ChangeSurface`; `ReviewModeDecision.change_surface`

- [ ] **Step 1: Write failing helper tests**

Append to `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py` (add imports as needed):

```python
from software_engineering_team.github_source import PullRequestFile


class TestBuildChangeSurfaceForReviewable:
    def test_head_backed_usable_patch_builds_surface(self) -> None:
        content = "def f():\n    return 1\n"
        patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        files = [PullRequestFile("mod.py", "modified", patch, 1, 1, None)]
        head = {"mod.py": content}
        surface = pr_review._build_change_surface_for_reviewable(files, head)
        assert not surface.is_empty
        assert "mod.py" in surface.blocks
        assert "### mod.py ###" in surface.code

    def test_missing_head_omits_path_empty_surface(self) -> None:
        patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        files = [PullRequestFile("mod.py", "modified", patch, 1, 1, None)]
        surface = pr_review._build_change_surface_for_reviewable(files, {})
        assert surface.is_empty

    def test_removed_and_no_patch_excluded(self) -> None:
        content = "def f():\n    return 1\n"
        patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        files = [
            PullRequestFile("gone.py", "removed", patch, 0, 1, None),
            PullRequestFile("bin.png", "added", "", 0, 0, None),
            PullRequestFile("ok.py", "modified", patch, 1, 1, None),
        ]
        surface = pr_review._build_change_surface_for_reviewable(
            files, {"gone.py": content, "ok.py": content}
        )
        assert list(surface.blocks.keys()) == ["ok.py"]
```

- [ ] **Step 2: Run helper tests — expect FAIL (helper missing)**

From worktree `backend/`:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestBuildChangeSurfaceForReviewable \
  -q --tb=short
```

Expected: FAIL — `AttributeError` / import error for `_build_change_surface_for_reviewable`.

- [ ] **Step 3: Implement helper + NamedTuple field + wire admission**

In `backend/agents/software_engineering_team/api/pr_review.py`:

1. Import:

```python
from software_engineering_team.code_review_agent.change_surface import (
    ChangeSurface,
    build_change_surface_from_patches,
)
```

(Place with other team imports; avoid circular imports — `change_surface` must not import `pr_review`.)

2. Extend `ReviewModeDecision`:

```python
class ReviewModeDecision(NamedTuple):
    """Whole-file vs. hunk review-mode decision, plus every input ``_run_reviewer`` needs."""

    valid_by_path: Dict[str, List[int]]
    changed_by_path: Dict[str, List[int]]
    head_files: Dict[str, str]
    change_surface: ChangeSurface
    code: str
    files_reviewed: int
    repo_reader: Any
```

3. Add helper (near `_build_review_code` / `_is_whole_file_reviewable`):

```python
def _build_change_surface_for_reviewable(
    files: List[Any],
    head_files: Dict[str, str],
) -> ChangeSurface:
    """Build a change surface for head-backed reviewable patched files.

    Preconditions:
        - ``files`` is the PR changed-file list (may be empty). Each entry
          exposes ``.filename``, ``.status``, and ``.patch``.
        - ``head_files`` maps path → non-blank head text for successfully
          fetched files.

    Postconditions:
        - Considers only files that pass ``_is_whole_file_reviewable`` and
          whose ``filename`` is present in ``head_files``.
        - Returns ``build_change_surface_from_patches`` for those patches with
          ``new_contents=head_files`` (empty ``ChangeSurface`` when no
          candidates or the builder omits all paths).
        - Never raises for well-typed inputs.
    """
    patches = {
        f.filename: f.patch
        for f in files
        if _is_whole_file_reviewable(f) and f.filename in head_files
    }
    if not patches:
        return ChangeSurface(blocks={})
    return build_change_surface_from_patches(patches, new_contents=head_files)
```

4. In `_decide_review_mode`, before the successful `return ReviewModeDecision(...)`, compute:

```python
change_surface = _build_change_surface_for_reviewable(files, head_files)
```

and pass `change_surface=change_surface` into the NamedTuple constructor. Do not alter `code` / `files_reviewed` logic.

5. Update `_decide_review_mode` docstring postconditions to mention `change_surface` (head-backed builder result; empty when none).

- [ ] **Step 4: Run helper tests — expect PASS**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestBuildChangeSurfaceForReviewable \
  -q --tb=short
```

Expected: PASS.

- [ ] **Step 5: Extend `_decide_review_mode` unit assertions**

In `TestDecideReviewModeUnit` in `test_coding_team_review_pr.py`:

- In `test_all_files_fetch_whole_skips_hunk_rendering`: after existing asserts, add:

```python
assert not result.change_surface.is_empty
assert set(result.change_surface.blocks) <= {"a.py", "b.py"}
```

(Use content that actually assembles if `"WHOLE {path}\n"` yields empty blocks — if surface is empty with that fixture content, switch the client to return `def f():\n    return 1\n` and patches that add a body line, **or** assert only that `change_surface` is a `ChangeSurface` instance and that paths without head stay empty in the partial-fetch test.)

Prefer concrete non-empty surface: update that test’s `_contents` and patches to the same `def f()` / return-change pair used in the helper test for at least one file, **or** keep WHOLE content and assert:

```python
from software_engineering_team.code_review_agent.change_surface import ChangeSurface
assert isinstance(result.change_surface, ChangeSurface)
```

and add a dedicated method:

```python
def test_decide_review_mode_attaches_head_backed_change_surface(self) -> None:
    from software_engineering_team.api import pr_review

    content = "def f():\n    return 1\n"
    patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
    files = [PullRequestFile("mod.py", "modified", patch, 1, 1, None)]

    result = pr_review._decide_review_mode(
        _file_contents_client(lambda o, r, path, ref: content),
        "job1",
        "o",
        "r",
        7,
        _mode_pr(),
        files,
    )
    assert result is not None
    assert not result.change_surface.is_empty
    assert "mod.py" in result.change_surface.blocks
    assert result.code == ""  # whole-file fetch still primary for dispatch this leaf
```

- In `test_partial_fetch_falls_back_to_hunks_for_the_missing_subset`: assert `"b.py" not in result.change_surface.blocks` (no head for `b.py`). If `a.py` content `"whole a\n"` does not assemble, that is OK — only require `b.py` absent.

- In `test_total_fetch_failure_renders_every_files_hunks`: assert `result.change_surface.is_empty`.

- [ ] **Step 6: Run focused suites**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestBuildChangeSurfaceForReviewable \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestDecideReviewModeUnit \
  -q --tb=short
```

Expected: PASS.

If other tests construct `ReviewModeDecision` positionally and fail, update them to keyword args including `change_surface=ChangeSurface(blocks={})` or the real surface.

- [ ] **Step 7: Commit**

```bash
git add \
  backend/agents/software_engineering_team/api/pr_review.py \
  backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py \
  backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py
git commit -m "$(cat <<'EOF'
Build head-backed change surface during PR review admission.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `change_surface` on `ReviewModeDecision` | Task 1 Step 3 |
| Always-present empty surface | Task 1 Step 3 helper |
| Head-backed eligibility only | Task 1 Steps 1 + 3 |
| Shared builder, no reimplementation | Task 1 Step 3 |
| No `_run_reviewer` / `code=` change | Task 1 (wire only) |
| Offline unit tests | Task 1 Steps 1 + 5 |
| No issue numbers | Global + commit message |
