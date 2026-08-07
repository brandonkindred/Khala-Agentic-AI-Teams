# PR Dispatch Change Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When admission produced a non-empty change surface, dispatch it as the primary reviewer `code=` / `pre_numbered=True` attempt (replacing whole-file `files=`), while keeping hunk `code` for partial-fetch leftovers.

**Architecture:** Add optional `change_surface` to `_run_reviewer`. If non-empty, append a surface attempt with `_hunk_review_focus` and skip `head_files`. Keep existing `if code:` hunk attempt. Wire `mode.change_surface` from `_run_pr_review_body`. Extend `TestRunReviewerUnit` with primary-kwargs assertions.

**Tech Stack:** Python 3.10+, existing `ChangeSurface`, pytest recording provider in `test_coding_team_pr_review_unit_helpers.py`.

## Global Constraints

- Non-empty surface **replaces** whole-file `files=head_files` attempt (do not run both).
- Surface attempt: `code=change_surface.code`, `pre_numbered=True`, `task_requirements=_hunk_review_focus(pr.body or "")`.
- Empty / missing surface: keep today’s `head_files` / `code` behavior.
- Non-empty surface **and** non-empty hunk `code`: two pre_numbered attempts; merge as today.
- Do not change focus-note helpers, builder, or admission surface construction.
- Do not change partition / anchoring maps (`valid_by_path` / `changed_by_path`).
- Offline tests only; never reference GitHub issue numbers in code/comments/commits/docs.
- Update `_run_reviewer` docstring Preconditions/Postconditions for surface-primary behavior.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/api/pr_review.py` | `_run_reviewer` attempt rules + caller pass-through |
| `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py` | Primary kwargs + surface/hunk tests |

---

### Task 1: Surface-primary `_run_reviewer` dispatch

**Files:**
- Modify: `backend/agents/software_engineering_team/api/pr_review.py`
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py`

**Interfaces:**
- Consumes: `ChangeSurface` (already imported), `_hunk_review_focus`, `_whole_file_focus`
- Produces: `_run_reviewer(..., change_surface: Optional[ChangeSurface] = None)`; caller passes `mode.change_surface`

- [ ] **Step 1: Write failing tests**

In `test_coding_team_pr_review_unit_helpers.py`, ensure `ChangeSurface` is importable (from `software_engineering_team.code_review_agent.change_surface`). Extend `_run_reviewer_kwargs` default with `change_surface=None`.

Add to `TestRunReviewerUnit`:

```python
def test_nonempty_surface_primary_skips_whole_file(self, monkeypatch) -> None:
    self._patch_collaborators(monkeypatch)
    output = _FakeOutput(["issue"], "summary", "notes")
    provider = _RecordingProvider([output])
    surface = ChangeSurface(blocks={"mod.py": "1: def f():\n2:     return 1"})
    kwargs = _run_reviewer_kwargs(
        head_files={"mod.py": "def f():\n    return 1\n"},
        code="",
        change_surface=surface,
    )

    result = pr_review._run_reviewer(provider, **kwargs)

    assert result is output
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["pre_numbered"] is True
    assert call["code"] == surface.code
    assert "files" not in call
    assert call["task_requirements"] == pr_review._hunk_review_focus("PR body")
    assert "pre_existing" in call["task_requirements"]


def test_empty_surface_keeps_whole_file(self, monkeypatch) -> None:
    self._patch_collaborators(monkeypatch)
    output = _FakeOutput(["issue"], "summary", "notes")
    provider = _RecordingProvider([output])
    kwargs = _run_reviewer_kwargs(
        head_files={"a.py": "content"},
        code="",
        change_surface=ChangeSurface(blocks={}),
    )

    result = pr_review._run_reviewer(provider, **kwargs)

    assert result is output
    assert len(provider.calls) == 1
    assert provider.calls[0]["pre_numbered"] is False
    assert provider.calls[0]["files"] == {"a.py": "content"}


def test_surface_plus_hunk_code_two_prenumbred_no_files(self, monkeypatch) -> None:
    self._patch_collaborators(monkeypatch)
    surface_out = _FakeOutput(["s"], "s", "")
    hunk_out = _FakeOutput(["h"], "", "h")
    provider = _RecordingProvider([surface_out, hunk_out])
    surface = ChangeSurface(blocks={"a.py": "1: a"})
    kwargs = _run_reviewer_kwargs(
        head_files={"a.py": "a\n"},
        code="### b.py ###\n1: y = 2",
        change_surface=surface,
    )

    result = pr_review._run_reviewer(provider, **kwargs)

    assert len(provider.calls) == 2
    assert provider.calls[0]["pre_numbered"] is True
    assert provider.calls[0]["code"] == surface.code
    assert "files" not in provider.calls[0]
    assert provider.calls[1]["pre_numbered"] is True
    assert provider.calls[1]["code"] == "### b.py ###\n1: y = 2"
    assert isinstance(result, pr_review._MergedReviewerOutput)
    assert result.issues == ["s", "h"]
```

Existing tests that omit `change_surface` must keep passing (default `None` ⇒ empty-surface path).

- [ ] **Step 2: Run new tests — expect FAIL**

From worktree `backend/`:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit::test_nonempty_surface_primary_skips_whole_file \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit::test_empty_surface_keeps_whole_file \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit::test_surface_plus_hunk_code_two_prenumbred_no_files \
  -q --tb=short
```

Expected: FAIL (unexpected kwargs / whole-file still runs / `change_surface` unexpected).

- [ ] **Step 3: Implement dispatch + caller wire**

In `pr_review.py`, update `_run_reviewer` signature:

```python
def _run_reviewer(
    provider: Any,
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    job_id: str,
    pr: Any,
    files: List[Any],
    code: str,
    head_files: Optional[Dict[str, str]] = None,
    repo_reader: Any = None,
    change_surface: Optional[ChangeSurface] = None,
) -> Optional[Any]:
```

Replace attempt construction with:

```python
attempts: List[Dict[str, Any]] = []
surface = change_surface
if surface is not None and not surface.is_empty:
    attempts.append(
        dict(
            code=surface.code,
            pre_numbered=True,
            task_requirements=_hunk_review_focus(pr.body or ""),
        )
    )
elif head_files:
    attempts.append(
        dict(
            files=head_files,
            pre_numbered=False,
            task_requirements=_whole_file_focus(pr.body or ""),
        )
    )
if code:
    attempts.append(
        dict(
            code=code,
            pre_numbered=True,
            task_requirements=_hunk_review_focus(pr.body or ""),
        )
    )
assert attempts, (
    "caller must supply a non-empty change_surface, head_files, and/or non-empty code"
)
```

Update the docstring to describe surface-primary behavior (non-empty surface replaces whole-file; hunk `code` still merges when present). Keep failure/merge postconditions.

In `_run_pr_review_body`, pass:

```python
output = _run_reviewer(
    provider,
    client,
    owner,
    repo,
    pr_number,
    job_id,
    pr,
    files,
    mode.code,
    head_files=mode.head_files or None,
    change_surface=mode.change_surface,
    repo_reader=mode.repo_reader,
)
```

- [ ] **Step 4: Run focused suites — expect PASS**

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit \
  -q --tb=short
```

Expected: all `TestRunReviewerUnit` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/api/pr_review.py \
  backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py
git commit -m "$(cat <<'EOF'
Prefer change surface as primary PR reviewer code input.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Surface replaces whole-file when non-empty | Task 1 Step 3 |
| `_hunk_review_focus` for surface | Task 1 Step 3 |
| Surface + hunk `code` both run | Task 1 Steps 1 + 3 |
| Empty surface keeps whole-file | Task 1 Steps 1 + 3 |
| Caller passes `mode.change_surface` | Task 1 Step 3 |
| Primary kwargs tests | Task 1 Step 1 |
| Anchoring unchanged | no partition edits |
| No issue numbers | Global + commit |
