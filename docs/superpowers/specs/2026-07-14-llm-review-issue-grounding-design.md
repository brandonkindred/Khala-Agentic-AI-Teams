# SE LLM Review Issue Grounding

**Status:** Approved 2026-07-14  
**Date:** 2026-07-14  
**Type:** Bug fix / hallucination guard for LLM-fallback code review  
**Issue:** GitHub #1274 (PR body only; do not cite in code)

## Problem

`shared/llm_review.py::run_llm_review` builds review issues from parsed free-text LLM output with no check that a claimed file, requirement, or named entity exists in the task’s requirements / acceptance criteria / spec, or in the submitted files.

That enabled a production loop: the senior/dev agent kept “fixing” `index.html` for a fabricated insurance provider (a new fake name each cycle) on a task unrelated to insurance. Existing `_validate_findings` in `code_review_agent` only bounds-checks file/line anchors — a real file path with fabricated *content* claims passes that check.

## Goals

1. Drop LLM-fallback findings whose description/recommendation contain checkable proper-noun-like claims absent from the task grounding corpus.
2. Blank `file_path` values that are not in the submitted `files` dict (degrade to submission-wide; never silently delete the issue for a bad path alone).
3. Keep phrase-free findings (most legitimate issues).
4. Provide a kill switch on `BaseMicrotaskReviewConfig` plumbed into `run_llm_review`.

## Non-goals

- Wiring or changing the in-process `code_review_agent` path / `_validate_findings` (companion work).
- Embedding or second-LLM re-judging of findings.
- Using file *contents* as grounding (only `files.keys()` plus task/architecture text).
- Scanning the `source` field for phrases.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Narrow phrase grounding (Title Case multi-word + quoted) | Fixes the observed bug with low false-positive rate |
| Text scanned | `description` + `recommendation` | Fabrication may appear only in the fix suggestion |
| Kill switch plumbing | `enable_grounding` on `run_llm_review`; flag on `BaseMicrotaskReviewConfig`; BE/FE wrappers forward | Config is the product kill switch; helper stays injectable for tests |
| Bad file path | Blank path, keep issue | Same fail-safe posture as `_validate_findings` |
| All issues dropped | Return `[]` (review passes) | Breaks the infinite fix loop |
| Grounding internal errors | Log and keep the issue (fail-open) | Never abort a review because the guard threw |
| File contents in corpus | No | Avoid false keeps when an unrelated token appears in code |

## Architecture

```
run_llm_review(...)
  → chunk / prompt / parse / issue_factory  (unchanged)
  → if enable_grounding:
       drop_ungrounded_issues(...)   # shared/issue_grounding.py
  → return issues
```

### New module: `shared/issue_grounding.py`

**`extract_checkable_phrases(text) -> list[str]`**

- Multi-word Title Case runs (e.g. `Insurance Provider`).
- Quoted substrings (single or double quotes).
- Skip single capitalized tokens (avoids normal code identifiers).

**`ground_issue_file_path(file_path, files) -> str`**

- Preconditions: `files` is the submitted path→content map.
- Postconditions: non-blank `file_path` not matching a key (exact or basename/suffix alias against keys) becomes `""`; otherwise return the path (normalized to the real key when an alias resolves). Never raises.

**`drop_ungrounded_issues(issues, *, files, requirements, acceptance_criteria, spec_content, architecture_context="", on_dropped=None) -> list`**

- Grounding corpus (case-insensitive substring match): `requirements`, joined `acceptance_criteria`, `spec_content`, `architecture_context`, and `files.keys()`.
- Per issue: blank bad `file_path`; extract phrases from description + recommendation; drop only if at least one phrase is absent from the corpus; call `on_dropped` with the full issue payload when dropping.
- Phrase-free issues always keep.
- Duck-types issues via `getattr`; prefer `model_copy` / `dataclasses.replace` when updating `file_path`.

### Config & wiring

- `BaseMicrotaskReviewConfig.enable_llm_review_grounding: bool = True` (BE/FE `MicrotaskReviewConfig` inherit).
- `run_llm_review(..., enable_grounding: bool = True)` — when True, run `drop_ungrounded_issues` immediately before return; log every drop at WARNING with full issue payload.
- BE/FE `_run_llm_review` accept `enable_grounding: bool = True` and forward it.
- Default remains True at every layer so grounding is on even when a caller omits config.
- Thread the flag so the config kill switch actually reaches the LLM fallback: `MicrotaskReviewConfig` → gated execution / `run_code_review_phase` (and equivalent FE path) → `_run_llm_review(..., enable_grounding=config.enable_llm_review_grounding)`. Prefer a small optional kwarg on the review-phase entrypoints (and on the shared `llm_review_fn` call) over a large signature rewrite; a closure around `_run_llm_review` at the gate is acceptable if it avoids widening `v2_review`’s public surface.

## Data flow & failure modes

```
files + LLM chunks → parse → issue_factory → [optional grounding] → return issues
                                              │
                                              ├─ blank unknown file_path (keep)
                                              └─ drop if Title Case / quoted phrase
                                                 in description|recommendation
                                                 ∉ corpus
```

| Case | Outcome |
|---|---|
| Phrase grounded in corpus | Keep (path may still be blanked) |
| Phrase ungrounded | Drop + log |
| No checkable phrases | Keep |
| Bad `file_path` only | Blank path, keep |
| `enable_grounding=False` | Identical to today’s behavior |
| Exception inside grounding | Log, keep issue |

## Testing

1. **`tests/test_issue_grounding.py`** — grounded keep; ungrounded content drop; bad `file_path` blanked not dropped; phrase-free keep; recommendation-only fabrication dropped.
2. **`tests/test_shared_llm_review.py`** — meal-planning task + mocked “insurance provider X” finding dropped before return; with `enable_grounding=False` the finding is kept.
3. Re-run existing `test_shared_llm_review.py` / `test_v2_review_shared.py` — legitimate findings still pass.

## Files

| Path | Change |
|---|---|
| `shared/issue_grounding.py` | New |
| `shared/llm_review.py` | Wire grounding before return |
| `shared/v2_models.py` | `enable_llm_review_grounding` on base config |
| `backend_code_v2_team/phases/review.py` | Forward `enable_grounding` on `_run_llm_review` / phase entrypoints as needed |
| `frontend_code_v2_team/phases/review.py` | Same |
| Gated execution / code-review gate (BE+FE) | Pass `config.enable_llm_review_grounding` into the LLM review path |
| `tests/test_issue_grounding.py` | New |
| `tests/test_shared_llm_review.py` | Regression + kill-switch |

## Implementation notes

- Follow DbC on every new public function (`Preconditions` / `Postconditions` in docstrings).
- TDD: write failing tests first for `issue_grounding` and the `run_llm_review` regression, then implement.
- Do not mention GitHub issue numbers in code, comments, or commit messages; use `Closes #1274` only in the PR body.
