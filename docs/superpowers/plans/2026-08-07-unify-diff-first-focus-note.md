# Unify diff-first PR review focus note Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `_whole_file_focus` / `_hunk_review_focus` with one `_diff_first_focus` so every PR reviewer attempt shares a change-scoped note (eight criteria + `pre_existing` tagging).

**Architecture:** Single helper appends one note body to the PR body (or returns the note alone when blank). `_run_reviewer` uses that helper for surface, whole-file, and hunk attempts alike. Mode-specific framing is dropped; input shape already distinguishes modes.

**Tech Stack:** Python 3.10, pytest, existing `pr_review.py` focus constants.

## Global Constraints

- Do not mention GitHub issue numbers in code, comments, commits, or docs (PR body only, with `Closes #N`).
- Design by Contract: update Preconditions/Postconditions on `_diff_first_focus` and any `_run_reviewer` docstring lines that name the old helpers.
- Tests must cover ≥90% of new/changed lines.
- Out of scope: `profiles.py` checklist rewrite, admission flip, scoped tools, broad surface-first preference suite rewrite beyond breakage from this rename.
- Spec: `docs/superpowers/specs/2026-08-07-unify-diff-first-focus-note-design.md`

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/api/pr_review.py` | `_diff_first_focus`; remove old helpers; `_run_reviewer` call sites; comment updates on shared constants |
| `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py` | `TestDiffFirstFocusUnit`; `_run_reviewer` `task_requirements` expectations |
| `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py` | Drop mode-divergent wording asserts; keep prefix / `pre_existing` / eight-criteria checks |

---

### Task 1: `_diff_first_focus` helper + unit / dispatch wiring

**Files:**
- Modify: `backend/agents/software_engineering_team/api/pr_review.py` (focus helpers ~241–315; `_run_reviewer` attempt `task_requirements` and docstring references)
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py` (`TestWholeFileFocusUnit` / `TestHunkReviewFocusUnit` → `TestDiffFirstFocusUnit`; `TestRunReviewerUnit` focus assertions)
- Test: same unit helpers file

**Interfaces:**
- Consumes: `REVIEW_FOCUS_NOTE_PREFIX`, `_PRE_EXISTING_TAG_INSTRUCTIONS`, `body: str`
- Produces: `_diff_first_focus(body: str) -> str` — blank/whitespace `body` → note alone starting with `REVIEW_FOCUS_NOTE_PREFIX`; else `f"{body}\n\n{note}"`. Note lists the eight criteria and includes both `pre_existing: true` and `pre_existing: false`. Deletes `_whole_file_focus` and `_hunk_review_focus`.

- [ ] **Step 1: Write the failing unit tests for `_diff_first_focus`**

In `test_coding_team_pr_review_unit_helpers.py`, replace the entire `# _whole_file_focus` and `# _hunk_review_focus` sections (both classes) with:

```python
# ---------------------------------------------------------------------------
# _diff_first_focus
# ---------------------------------------------------------------------------

_EIGHT_CRITERIA_MARKERS = (
    "Logical / syntactic correctness",
    "Contract changes on touched functions/classes",
    "Side effects on callers",
    "Architectural standards",
    "Language / library / framework best practices",
    "New issues introduced by the change",
    "implement/fix the ticket/spec",
    "Project style preferences",
)


class TestDiffFirstFocusUnit:
    def test_blank_body_returns_note_alone(self) -> None:
        result = pr_review._diff_first_focus("")
        assert result.startswith(pr_review.REVIEW_FOCUS_NOTE_PREFIX)
        assert "\n\n" not in result[: len(pr_review.REVIEW_FOCUS_NOTE_PREFIX) + 1]

    def test_whitespace_only_body_returns_note_alone(self) -> None:
        result = pr_review._diff_first_focus("   \n")
        assert result.startswith(pr_review.REVIEW_FOCUS_NOTE_PREFIX)
        assert "   " not in result

    def test_non_blank_body_is_prefixed_to_note(self) -> None:
        result = pr_review._diff_first_focus("Fixes the flaky retry loop.")
        assert result.startswith("Fixes the flaky retry loop.\n\n")
        assert pr_review.REVIEW_FOCUS_NOTE_PREFIX in result

    def test_note_instructs_pre_existing_field(self) -> None:
        result = pr_review._diff_first_focus("body")
        assert "pre_existing: false" in result
        assert "pre_existing: true" in result

    def test_note_lists_eight_criteria(self) -> None:
        result = pr_review._diff_first_focus("body")
        for marker in _EIGHT_CRITERIA_MARKERS:
            assert marker in result, f"missing criterion marker: {marker!r}"

    def test_note_is_diff_first(self) -> None:
        result = pr_review._diff_first_focus("")
        lower = result.lower()
        assert "diff-first" in lower or "what this pull request changes" in lower
        assert "enclosing" in lower
```

Also update the module docstring line that says "the whole-file focus note" to "the diff-first focus note".

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py::TestDiffFirstFocusUnit \
  -v
```

Expected: FAIL with `AttributeError: module ... has no attribute '_diff_first_focus'` (or import/collection error naming the missing symbol).

- [ ] **Step 3: Implement `_diff_first_focus` and remove the old helpers**

In `api/pr_review.py`, replace the block from the `REVIEW_FOCUS_NOTE_PREFIX` comment through `_hunk_review_focus` with:

```python
# Prefix of the scope-tagging focus note, exposed so callers/tests can detect
# the note (e.g. in task_requirements) without duplicating its full wording.
REVIEW_FOCUS_NOTE_PREFIX = "Review focus:"

# Shared "tag pre-existing findings" instruction body, appended after the
# diff-first framing and eight-criteria list. Kept as one constant so the
# tagging contract cannot drift from call-site edits.
_PRE_EXISTING_TAG_INSTRUCTIONS = (
    "For EVERY issue you report, add a boolean field named `pre_existing` to the issue "
    "object:\n"
    "- Set `pre_existing: false` for a defect in the code this pull request ADDS or MODIFIES — "
    "these are the findings that matter for reviewing the PR.\n"
    "- Set `pre_existing: true` for a genuine bug you notice in PRE-EXISTING, UNCHANGED code "
    "that this pull request did not touch (an unrelated defect visible in the surrounding "
    "code). Still report such bugs — do not stay silent about them — but tag them so they are "
    "recorded separately instead of blamed on this change.\n"
    "Do not invent pre-existing issues to pad the review; only tag a finding `pre_existing: "
    "true` when it is a real defect in code outside this PR's change."
)

_DIFF_FIRST_FOCUS_NOTE = (
    f"{REVIEW_FOCUS_NOTE_PREFIX} evaluate what this pull request changes (and enclosing "
    "constructs when shown). Treat surrounding or unchanged code as context, not the primary "
    "target — this is a diff-first review.\n"
    "Judge the change against these eight criteria:\n"
    "1. Logical / syntactic correctness of the change\n"
    "2. Contract changes on touched functions/classes (DbC, signatures, invariants)\n"
    "3. Side effects on callers of those encapsulating constructs\n"
    "4. Architectural standards\n"
    "5. Language / library / framework best practices\n"
    "6. New issues introduced by the change\n"
    "7. Does the change actually implement/fix the ticket/spec?\n"
    "8. Project style preferences\n"
    f"{_PRE_EXISTING_TAG_INSTRUCTIONS}"
)


def _diff_first_focus(body: str) -> str:
    """Append the shared diff-first focus note to ``body``.

    Every PR reviewer attempt (change surface, whole-file fallback, or hunk
    ``code``) gets the same note so findings stay change-scoped, the eight
    review criteria are explicit, and ``pre_existing`` tagging stays consistent.

    Preconditions:
        - ``body`` is a string (the PR body or "").

    Postconditions:
        - Returns ``body`` with the focus note appended (or the note alone when
          ``body`` is blank/whitespace). The note starts with
          ``REVIEW_FOCUS_NOTE_PREFIX``, lists the eight criteria, and includes
          ``_PRE_EXISTING_TAG_INSTRUCTIONS``.
    """
    note = _DIFF_FIRST_FOCUS_NOTE
    return f"{body}\n\n{note}" if body.strip() else note
```

Delete `_whole_file_focus` and `_hunk_review_focus` entirely (no thin wrappers).

- [ ] **Step 4: Wire `_run_reviewer` to the unified helper**

In `_run_reviewer`, change every `task_requirements=_hunk_review_focus(...)` and `task_requirements=_whole_file_focus(...)` to:

```python
task_requirements=_diff_first_focus(pr.body or ""),
```

Update the `_run_reviewer` docstring wherever it names `_hunk_review_focus` / `_whole_file_focus` so it names `_diff_first_focus` instead (same blank-body / append semantics; no mode-specific note).

Also fix any other comments in this file that still say the two modes have different focus notes (e.g. near issue partitioning that mentions `_hunk_review_focus`).

- [ ] **Step 5: Update `TestRunReviewerUnit` expectations**

In the same unit helpers file, replace every `pr_review._whole_file_focus(...)` / `pr_review._hunk_review_focus(...)` equality with `pr_review._diff_first_focus(...)`.

Remove asserts that require mode-specific copy, e.g.:

```python
assert "diff hunks" in call["task_requirements"]
```

Replace those with shared-content guards, e.g.:

```python
assert "pre_existing" in call["task_requirements"]
assert "Architectural standards" in call["task_requirements"]
```

Update comments that describe “hunk-mode focus note” / “whole-file focus note” to “diff-first focus note”.

Rename `test_none_body_coerces_to_hunk_focus_note_alone` → `test_none_body_coerces_to_diff_first_focus_note_alone` and assert:

```python
assert provider.calls[0]["task_requirements"] == pr_review._diff_first_focus("")
assert "pre_existing" in provider.calls[0]["task_requirements"]
assert "Architectural standards" in provider.calls[0]["task_requirements"]
```

- [ ] **Step 6: Run unit helper tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py \
  -v
```

Expected: PASS (all tests in the file).

- [ ] **Step 7: Commit**

```bash
git add -f \
  backend/agents/software_engineering_team/api/pr_review.py \
  backend/agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py
git commit -m "$(cat <<'EOF'
Unify PR review focus note into a single diff-first helper.

EOF
)"
```

---

### Task 2: Update `/review-pr` suite assertions for the shared note

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py` (focus-note tests around the whole-file / hunk / partial-fetch cases that assert `"diff hunks"` / `"complete file contents"`)
- Test: same file

**Interfaces:**
- Consumes: `REVIEW_FOCUS_NOTE_PREFIX`, `_diff_first_focus` wording (eight criteria + `pre_existing`)
- Produces: Suite still proves every reviewer call gets the shared note; no longer asserts mode-divergent copy.

- [ ] **Step 1: Update the failing mode-divergent asserts**

Locate the three sites that assert mode-specific wording (`"diff hunks"` / `"complete file contents"` / comments about `_hunk_review_focus` vs `_whole_file_focus`). For each captured `task_requirements`:

Keep:

```python
from software_engineering_team.api.pr_review import REVIEW_FOCUS_NOTE_PREFIX

assert REVIEW_FOCUS_NOTE_PREFIX in captured["task_requirements"]
assert "pre_existing" in captured["task_requirements"]
```

Replace mode-divergence checks with shared eight-criteria markers, for example:

```python
assert "Architectural standards" in captured["task_requirements"]
assert "diff-first" in captured["task_requirements"].lower()
```

For the partial-fetch test that checks both whole and hunk calls, assert the **same** shared markers on both `whole_call["task_requirements"]` and `hunk_call["task_requirements"]` (they must be equal to each other modulo any body prefix already covered elsewhere — at minimum both contain the prefix, `pre_existing`, and one eight-criteria marker).

Rewrite comments so they no longer claim the two modes must differ in focus-note text.

Do **not** rewrite admission / surface-first preference tests beyond what breaks from this focus-note change (owned by a sibling leaf).

- [ ] **Step 2: Run the focused review-pr tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py \
  -k "focus_note or partial_head_fetch_reviews_fetched" \
  -v
```

Expected: PASS for the selected tests.

- [ ] **Step 3: Run both touched suites together**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_pr_review_unit_helpers.py \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py \
  -k "focus or DiffFirst or RunReviewer or partial_head_fetch_reviews_fetched" \
  -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add \
  backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py
git commit -m "$(cat <<'EOF'
Align PR review suite focus-note asserts with the unified helper.

EOF
)"
```

---

## Spec coverage check

| Spec requirement | Task |
|---|---|
| Single `_diff_first_focus`; delete dual helpers | Task 1 |
| Eight criteria listed in note | Task 1 (`_DIFF_FIRST_FOCUS_NOTE` + unit test) |
| `_PRE_EXISTING_TAG_INSTRUCTIONS` reused | Task 1 |
| `_run_reviewer` call sites unified | Task 1 |
| Unit helper tests updated; divergence test removed | Task 1 |
| `test_coding_team_review_pr.py` mode-divergence asserts updated | Task 2 |
| Out of scope (profiles / admission / scoped tools) untouched | Both |
