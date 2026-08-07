# PR review whole-file fallback-only partition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After change-surface admission, put only surface-uncovered paths into whole-file / hunk reviewer inputs so whole-file review is a degradation path and no reviewable file is dropped.

**Architecture:** Keep universal `_fetch_head_files` for expansion. Partition in `_decide_review_mode` so decision `head_files` excludes `change_surface.blocks` paths and hunk `code` covers only the residual. Flip `_run_reviewer` surface vs whole-file from `elif` to independent `if`s so mixed PRs run two attempts and merge.

**Tech Stack:** Python 3.10, pytest, existing `ChangeSurface` / `pr_review.py` helpers.

## Global Constraints

- Do not mention GitHub issue numbers in code, comments, commits, or docs (PR body only, with `Closes #N`).
- Design by Contract: update Preconditions/Postconditions on touched helpers.
- Tests must cover ≥90% of new/changed lines.
- Out of scope: change-surface builder, focus-note unification, SE path, broad suite rewrite of older whole-file-preference assertions beyond what this leaf breaks.
- Spec: `docs/superpowers/specs/2026-08-07-pr-review-whole-file-fallback-only-design.md`

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/api/pr_review.py` | `_decide_review_mode` partition; `_run_reviewer` independent attempts; docstring contracts |
| `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py` | `_run_reviewer` unit tests (surface + filtered whole-file) |
| `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py` | `_decide_review_mode` unit tests (partition + `files_reviewed`) |

---

### Task 1: Dispatch — surface and whole-file are independent attempts

**Files:**
- Modify: `backend/agents/software_engineering_team/api/pr_review.py` (`_run_reviewer`, `_MergedReviewerOutput` docstring)
- Test: `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py` (`TestRunReviewerUnit`)

**Interfaces:**
- Consumes: `ChangeSurface`, existing `_run_reviewer(..., change_surface=..., head_files=..., code=...)`
- Produces: When both non-empty surface and truthy `head_files` are passed, two reviewer calls (surface then whole-file) merged via `_MergedReviewerOutput`. Admission (Task 2) guarantees path-disjoint `head_files`; this task does not filter.

- [ ] **Step 1: Write the failing tests**

In `TestRunReviewerUnit`, **replace** `test_nonempty_surface_primary_skips_whole_file` (it encodes the old exclusive gate and passes overlapping `head_files`) with these two tests:

```python
def test_nonempty_surface_alone_skips_whole_file(self, monkeypatch) -> None:
    self._patch_collaborators(monkeypatch)
    output = _FakeOutput(["issue"], "summary", "notes")
    provider = _RecordingProvider([output])
    surface = ChangeSurface(blocks={"mod.py": "1: def f():\n2:     return 1"})
    kwargs = _run_reviewer_kwargs(
        head_files=None,  # admission cleared surface-covered paths
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


def test_surface_plus_filtered_head_files_two_attempts(self, monkeypatch) -> None:
    self._patch_collaborators(monkeypatch)
    surface_out = _FakeOutput(["s"], "s", "")
    whole_out = _FakeOutput(["w"], "", "w")
    provider = _RecordingProvider([surface_out, whole_out])
    surface = ChangeSurface(blocks={"a.py": "1: a"})
    fallback_head = {"b.py": "whole b\n"}  # path-disjoint from surface
    kwargs = _run_reviewer_kwargs(
        head_files=fallback_head,
        code="",
        change_surface=surface,
    )

    result = pr_review._run_reviewer(provider, **kwargs)

    assert len(provider.calls) == 2
    assert provider.calls[0]["pre_numbered"] is True
    assert provider.calls[0]["code"] == surface.code
    assert "files" not in provider.calls[0]
    assert provider.calls[1]["pre_numbered"] is False
    assert provider.calls[1]["files"] == fallback_head
    assert set(provider.calls[1]["files"]).isdisjoint(surface.blocks)
    assert isinstance(result, pr_review._MergedReviewerOutput)
    assert result.issues == ["s", "w"]
```

Also update `test_surface_plus_hunk_code_two_prenumbred_no_files` so `head_files` is `None` or `{}` (admission would have cleared `a.py` once it is in the surface). Keep expecting two pre_numbered attempts and no `files=` call:

```python
kwargs = _run_reviewer_kwargs(
    head_files=None,
    code="### b.py ###\n1: y = 2",
    change_surface=surface,
)
```

Leave `test_empty_surface_keeps_whole_file` unchanged.

- [ ] **Step 2: Run tests to verify they fail**

Run (from `backend/` with the project venv):

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit::test_surface_plus_filtered_head_files_two_attempts \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit::test_nonempty_surface_alone_skips_whole_file \
  -v
```

Expected: `test_surface_plus_filtered_head_files_two_attempts` FAIL — only one call (surface) because of `elif head_files`. `test_nonempty_surface_alone_skips_whole_file` may already PASS.

- [ ] **Step 3: Flip `elif head_files` to `if head_files` and update contracts**

In `_run_reviewer`, change the attempt assembly so surface and whole-file are independent:

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
    if head_files:
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
```

Update `_run_reviewer` docstring:

- Replace “replaces a whole-file `head_files` attempt” with: a non-empty surface drives the primary pre-numbered attempt for covered paths; a truthy `head_files` **also** drives whole-file review when present (admission must pass only surface-uncovered paths).
- Preconditions: at least one of non-empty surface / truthy `head_files` / truthy `code`; mixed combinations allowed when path-disjoint.
- Postconditions: one/two/three attempts merge as today via `_MergedReviewerOutput`.

Update `_MergedReviewerOutput` class docstring to note sources may be surface + whole-file + hunks (still path-disjoint when admission partitions).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit \
  -v
```

Expected: PASS for the whole class.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/api/pr_review.py \
  backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py
git commit -m "$(cat <<'EOF'
Allow surface and whole-file reviewer attempts to run together.

EOF
)"
```

---

### Task 2: Admission — partition `head_files` / hunks after surface build

**Files:**
- Modify: `backend/agents/software_engineering_team/api/pr_review.py` (`_decide_review_mode`)
- Test: `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py` (`TestDecideReviewModeUnit`)

**Interfaces:**
- Consumes: `_fetch_head_files`, `_build_change_surface_for_reviewable`, `_build_review_code`, `ChangeSurface.blocks`
- Produces: `ReviewModeDecision` where `head_files` ⊆ fetched and `set(head_files) ∩ set(change_surface.blocks) = ∅`; hunk `code` only for `reviewable - surface_paths - set(head_files)`; `files_reviewed` counts unique covered paths once.

- [ ] **Step 1: Write the failing admission tests**

Add to `TestDecideReviewModeUnit` (use monkeypatch on `_build_change_surface_for_reviewable` so partition is deterministic):

```python
def test_surface_covered_paths_excluded_from_decision_head_files(
    self, monkeypatch: pytest.MonkeyPatch
) -> None:
    from software_engineering_team.api import pr_review
    from software_engineering_team.code_review_agent.change_surface import ChangeSurface

    files = [
        PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None),
        PullRequestFile("b.py", "modified", "@@ -1 +1 @@\n+y", 1, 0, None),
    ]

    def _contents(o, r, path, ref):
        return f"WHOLE {path}\n"

    monkeypatch.setattr(
        pr_review,
        "_build_change_surface_for_reviewable",
        lambda files_arg, head: ChangeSurface(blocks={"a.py": "1: x"}),
    )

    result = pr_review._decide_review_mode(
        _file_contents_client(_contents), "job1", "o", "r", 7, _mode_pr(), files
    )
    assert result is not None
    assert "a.py" in result.change_surface.blocks
    assert set(result.head_files) == {"b.py"}  # surface path excluded
    assert result.code == ""
    assert result.files_reviewed == 2
    assert set(result.head_files).isdisjoint(result.change_surface.blocks)


def test_surface_covers_all_fetched_clears_head_files(
    self, monkeypatch: pytest.MonkeyPatch
) -> None:
    from software_engineering_team.api import pr_review
    from software_engineering_team.code_review_agent.change_surface import ChangeSurface

    files = [
        PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None),
        PullRequestFile("b.py", "modified", "@@ -1 +1 @@\n+y", 1, 0, None),
    ]
    monkeypatch.setattr(
        pr_review,
        "_build_change_surface_for_reviewable",
        lambda files_arg, head: ChangeSurface(
            blocks={"a.py": "1: x", "b.py": "1: y"}
        ),
    )

    result = pr_review._decide_review_mode(
        _file_contents_client(lambda o, r, path, ref: f"WHOLE {path}\n"),
        "job1",
        "o",
        "r",
        7,
        _mode_pr(),
        files,
    )
    assert result is not None
    assert result.head_files == {}
    assert result.code == ""
    assert result.files_reviewed == 2
    assert set(result.change_surface.blocks) == {"a.py", "b.py"}


def test_surface_plus_fetch_miss_uses_hunks_for_missing_only(
    self, monkeypatch: pytest.MonkeyPatch
) -> None:
    from software_engineering_team.api import pr_review
    from software_engineering_team.code_review_agent.change_surface import ChangeSurface

    files = [
        PullRequestFile(
            "a.py", "modified", "@@ -1,2 +1,3 @@\n ctx\n+added\n more", 1, 0, None
        ),
        PullRequestFile(
            "b.py", "modified", "@@ -1,1 +1,2 @@\n x\n+y", 1, 0, None
        ),
    ]
    monkeypatch.setattr(
        pr_review,
        "_build_change_surface_for_reviewable",
        lambda files_arg, head: ChangeSurface(blocks={"a.py": "1: added"}),
    )

    result = pr_review._decide_review_mode(
        _file_contents_client(
            lambda o, r, path, ref: "whole a\n" if path == "a.py" else None
        ),
        "job1",
        "o",
        "r",
        7,
        _mode_pr(),
        files,
    )
    assert result is not None
    assert result.head_files == {}  # a.py covered by surface; b.py never fetched
    assert "b.py" in result.code
    assert "a.py" not in result.code
    assert result.files_reviewed == 2
```

Also update existing tests that will break under the new semantics:

1. `test_decide_review_mode_attaches_head_backed_change_surface` — after real surface build for `mod.py`, assert `result.head_files == {}` (surface covered) and keep `result.code == ""`. Remove or rewrite the comment “whole-file fetch still primary for dispatch this leaf”.

2. `test_all_files_fetch_whole_skips_hunk_rendering` — with real builder, if both paths land in the surface, expect `head_files == {}` and `files_reviewed == 2`; if the builder omits them (empty surface), keep today’s `head_files == {a,b}`. Prefer making this deterministic by monkeypatching the builder to return empty surface (preserves “all fetch → no hunks” intent) **or** to return both paths (asserts cleared `head_files`). Recommended: monkeypatch empty surface so this test stays about fetch/hunk gating, and rely on the new tests above for partition.

3. `test_partial_fetch_falls_back_to_hunks_for_the_missing_subset` — if real surface includes `a.py`, expect `head_files == {}` not `{"a.py"}`; still `b.py` in `code`, `files_reviewed == 2`. Safest: monkeypatch builder to put `a.py` in surface (matches post-condition) or empty surface (then `head_files == {"a.py"}` still). Recommended for this existing test: monkeypatch empty surface to keep its original “partial fetch → hunk missing” focus; coverage for surface+miss is the new test above.

- [ ] **Step 2: Run tests to verify new ones fail**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestDecideReviewModeUnit::test_surface_covered_paths_excluded_from_decision_head_files \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestDecideReviewModeUnit::test_surface_covers_all_fetched_clears_head_files \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestDecideReviewModeUnit::test_surface_plus_fetch_miss_uses_hunks_for_missing_only \
  -v
```

Expected: FAIL — `head_files` still contains surface-covered paths (and/or wrong `files_reviewed`).

- [ ] **Step 3: Implement partition in `_decide_review_mode`**

Replace the post-fetch branch that currently sets `code` / `files_reviewed` from fetch-missing alone, and the return that passes raw `head_files`, with:

```python
    fetched = _fetch_head_files(client, owner, repo, files, pr.head_sha)
    change_surface = _build_change_surface_for_reviewable(files, fetched)
    surface_paths = set(change_surface.blocks)

    # Whole-file reviewer inputs: fetched paths the surface did not cover.
    head_files = {
        path: text for path, text in fetched.items() if path not in surface_paths
    }

    uncovered = reviewable - surface_paths - set(head_files)
    code = ""
    hunk_reviewed = 0
    if uncovered:
        fallback_files = [f for f in files if f.filename in uncovered]
        code, hunk_reviewed = _build_review_code(fallback_files)

    files_reviewed = len(surface_paths | set(head_files)) + hunk_reviewed

    if not surface_paths and not head_files and not code:
        # Total failure with blank hunks (deletion-only, etc.)
        _complete_review_noop(
            client,
            job_id,
            owner,
            repo,
            pr_number,
            pr,
            comment="Code review: no reviewable file content.",
            status_text="No reviewable file content",
        )
        return None

    if uncovered:
        logger.info(
            "PR review #%s: surface=%d whole-file-fallback=%d hunk-fallback=%d "
            "(reviewable=%d)",
            pr_number,
            len(surface_paths),
            len(head_files),
            len(uncovered),
            len(reviewable),
        )
    elif head_files and not surface_paths:
        # Surface empty; whole-file covers everyone fetched — no extra log required
        # beyond existing patterns; optional info log OK.
        pass
    elif surface_paths and not head_files and not uncovered:
        logger.info(
            "PR review #%s: reviewing %d file(s) via change surface only",
            pr_number,
            len(surface_paths),
        )

    repo_reader = GitHubRepoReader(client, owner, repo, pr.head_sha)
    return ReviewModeDecision(
        valid_by_path=valid_by_path,
        changed_by_path=changed_by_path,
        head_files=head_files,
        change_surface=change_surface,
        code=code,
        files_reviewed=files_reviewed,
        repo_reader=repo_reader,
    )
```

Notes for the implementer:

- Remove the old three-way `if head_files and not missing / elif head_files / else` block; the partition above replaces it.
- Keep the early empty-`files` / empty-`reviewable` noops unchanged.
- Update `_decide_review_mode` docstring: whole-file vs hunk is still per-file, but **surface is preferred**; decision `head_files` means whole-file fallback only; HTTP fetch still runs for all reviewable files for expansion.
- `files_reviewed`: if `_build_review_code` returns a count that can diverge from `len(uncovered)` (filters internally), prefer `len(surface_paths) + len(head_files) + hunk_reviewed` and do not also add `len(uncovered)`.
- Logging: keep informative; exact log strings may match the snippet or be tightened — do not assert on log text in tests.

- [ ] **Step 4: Run admission + reviewer tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestDecideReviewModeUnit \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestRunReviewerUnit \
  -v
```

Expected: PASS. If any integration tests in `test_coding_team_review_pr.py` assert `head_files` == all fetched when a surface is present, update them to expect the partitioned map (grep for `head_files` / `files_reviewed` failures).

- [ ] **Step 5: Broader related suite + commit**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py \
  -q --tb=line
```

Expected: PASS (fix any incidental assertion drift from partition semantics).

```bash
git add \
  backend/agents/software_engineering_team/api/pr_review.py \
  backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py
git commit -m "$(cat <<'EOF'
Restrict whole-file review inputs to surface-uncovered paths.

EOF
)"
```

---

### Task 3: Whole-branch verification

**Files:** none new

- [ ] **Step 1: Lint touched Python**

```bash
cd backend && ruff check agents/software_engineering_team/api/pr_review.py \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py
cd backend && ruff format --check agents/software_engineering_team/api/pr_review.py \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py
```

Expected: clean / already formatted (run `ruff format` on those paths if needed).

- [ ] **Step 2: Final test pass**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py \
  -q --tb=short
```

Expected: PASS.

- [ ] **Step 3: Commit plan/spec if not yet on the feature branch**

If implementing on a feature branch that does not yet include the design/plan docs, force-add (path is gitignored under `docs/superpowers/`):

```bash
git add -f \
  docs/superpowers/specs/2026-08-07-pr-review-whole-file-fallback-only-design.md \
  docs/superpowers/plans/2026-08-07-pr-review-whole-file-fallback-only.md
git commit -m "$(cat <<'EOF'
Add design and plan for whole-file fallback-only partition.

EOF
)"
```

Only if those files are missing from the branch history.

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Keep HTTP fetch for expansion | Task 2 (`fetched = _fetch_head_files`) |
| Decision `head_files` excludes surface paths | Task 2 |
| Hunks only for residual uncovered | Task 2 |
| `files_reviewed` unique count | Task 2 |
| Independent surface + whole-file attempts | Task 1 |
| Path-disjoint / no silent drop | Tasks 1–2 tests |
| Docstring / contract updates | Tasks 1–2 |
| Out of scope (builder, focus note, SE) | Not in plan |

## Placeholder scan

No TBD/TODO steps; concrete tests and implementation snippets included.

## Type consistency

- `ChangeSurface.blocks` / `.is_empty` / `.code` used consistently.
- `ReviewModeDecision.head_files: Dict[str, str]` retained; semantics = fallback-only after Task 2.
- `_run_reviewer` still accepts `Optional[Dict[str, str]]` for `head_files`.
