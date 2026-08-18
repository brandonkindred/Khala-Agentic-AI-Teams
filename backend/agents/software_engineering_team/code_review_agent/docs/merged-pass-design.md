# Merged architecture / side-effect pass (in-process coordinator)

**Date:** 2026-07-28  
**Scope:** In-process coordinator only. Temporal workflow wiring and snapshot comparison of findings are tracked separately.

**Status update:** Temporal workflow wiring for this same merge order has since landed — `combine_findings_activity` (the durable, no-LLM counterpart of the combine step below) plus a sequential merged-pass → combine → re-verify pipeline, gated behind `_REORDERED_TAIL_PASSES_PATCH` for replay compatibility with in-flight pre-migration histories. This design's merge-order goal and decisions below are unchanged and remain the source of truth for the in-process path; see `code_review_agent/temporal/workflows.py`'s module docstring (and that gate's own docstring) as the authoritative source for the durable-mode pipeline and its old-history replay contract — not duplicated here.

## Goal

Replace the two separate LLM calls to `architecture_consistency_pass` and `side_effect_impact_pass` in `coordinator._run_tail_passes` with a single merged-pass call. Split the merged response back into the two existing finding lists so downstream dedupe/gate/synthesis behavior stays unchanged, at one fewer LLM call per submission when both halves would previously have run.

Also remove the requirement that an architecture document must be present before architecture review can run: architecture findings may come from documented standards *or* from established codebase architecture / project patterns verified via repository tools.

Depends on the already-merged prompt/schema design (`MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`, `MergedArchitectureSideEffectResponse`).

## Decisions already locked

| Decision | Choice |
|---|---|
| When only one half is “useful” | Run the merged prompt whenever *either* env-enabled half is eligible (do not fall back to a single standalone call for simplicity). |
| Architecture without a formal doc | Drop the architecture-document early-return; broaden the shared architecture instruction body so Part 1 can evaluate consistency with existing codebase architecture when no doc is present. |
| Module shape | New `merged_architecture_side_effect_pass.py`; keep standalone pass modules for Temporal and existing unit tests until Temporal is wired separately. |

## Architecture without a required document

### Runtime gate (standalone architecture pass)

Remove the early return that requires `architecture_document` / rendered overview / components / decisions.

Keep early returns for:

- `CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS` disabled
- `input_data.profile != ReviewProfile.CODE_REVIEW`
- no readable submission files
- fail-safe empty list on exception

### Prompt body (`_ARCHITECTURE_CONSISTENCY_BODY`)

Shared by standalone `ARCHITECTURE_CONSISTENCY_PROMPT` and Part 1 of `MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`. Broaden so `category: "architecture"` findings may come from either:

1. **Documented standards** — when an architecture document or structured architecture context is provided, quote/paraphrase it (existing behavior).
2. **Established codebase architecture** — when no formal document is present (or in addition), use repository tools to judge whether the change alters existing module/service boundaries, layering, and project patterns in a way inconsistent with how this repository is already structured. Still tool-verified; still no invented rules from naming alone.

`category: "refactor"` (cross-codebase redundancy) remains unchanged: must confirm a real duplicate elsewhere via tools.

### User prompt

- When architecture context is present: inline it in full (as today).
- When absent: state explicitly that no formal architecture document was provided and instruct the model to rely on repository structure/patterns instead of inventing a phantom document.

## Merged module

**File:** `backend/agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py`

**Entry point:**

```text
find_architecture_and_side_effect_issues(
    llm, input_data, repo_reader=None, index=None
) -> tuple[list[CodeReviewIssue], list[CodeReviewIssue]]
```

Returns `(architecture_findings, side_effect_findings)` in the same `CodeReviewIssue` shapes each standalone pass returns today.

### Eligibility (one LLM call)

Run when all of:

- `input_data.profile == ReviewProfile.CODE_REVIEW`
- the shared `CodebaseIndex` has readable files
- at least one of `CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS` / `CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS` is enabled

Do **not** require an architecture document. Do **not** skip solely because `pre_numbered` is true (merged runs when either half’s env flag allows; Part 2 may still return `[]` if the model cannot safely reason from partial hunks — the standalone side-effect pass keeps its existing `pre_numbered` early-return for Temporal until that path is migrated).

When both env flags are off, or profile/files disqualify the run: return `([], [])` with no LLM call.

### Implementation sketch

- Tools: the side-effect tool set (shared false-positive tools + `search_repository`) so Part 2 is not underpowered.
- System prompt: `MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`.
- User prompt: changed files under the map-chunk char budget; architecture doc/context when present, else the explicit “no formal document” note.
- Parse into `MergedArchitectureSideEffectResponse` (or equivalent JSON + schema validation).
- **Disabled-half filtering:** after parsing, return an empty list for any half whose
  env flag is disabled (or, for the side-effect half, when `pre_numbered` forces that
  half off). The merged prompt still contains both Part 1 and Part 2 instructions, so
  the model may emit findings for a disabled half; those must be discarded before
  findings reach dedupe/gate/synthesis so the existing feature-toggle contract is
  preserved.
- Coerce/validate each array by reusing the existing per-pass validators (`architecture_consistency_pass` / `side_effect_impact_pass`) — do not fork validation logic.
- Fail-safe: never raise to the coordinator; on failure log a warning and return `([], [])`.

## Coordinator (`_run_tail_passes`)

- Schedule optional `filter` + one `merged` call (was `filter` + `architecture` + `side_effect`).
- Unpack the merged tuple into `architecture_findings` / `side_effect_findings`.
- Merge order unchanged: verified chunk issues → architecture findings → side-effect findings.
- `has_additive_findings` remains True iff either list is non-empty.
- Concurrent fan-out: at most two slots when the filter runs (was three). Sequential path when DummyLLMClient / parallelism ≤ 1 / fewer than two calls — same predicates as today.
- Update module docstring / comments that still describe three independent additive passes for architecture and side-effect separately.

Standalone `find_architecture_and_redundancy_issues` and `find_side_effect_impact_issues` remain importable and used by Temporal activities until Temporal is migrated.

## Testing

### New: `test_merged_architecture_side_effect_pass.py`

- Both env flags off → `([], [])`, no LLM call
- Non-`CODE_REVIEW` profile → skip
- No readable files → skip
- Happy path: scripted two-key JSON → correctly categorized `CodeReviewIssue` lists
- Malformed / raising LLM → `([], [])`
- No architecture document still runs; user prompt includes the “no formal document” note
- Spot-check reused line validation / no-op suggestion filtering

### Update: architecture pass tests

- Remove/replace assertions that “no architecture → `[]` and no LLM call”
- Add a case that runs without a document

### Update: `test_code_review_coordinator.py`

- Monkeypatch `find_architecture_and_side_effect_issues` instead of the two separate functions
- Concurrent arrival expectations: `filter` + `merged` (not three names)
- Keep “sequential when parallelism = 1” and “concurrent ≡ sequential output” coverage

### Update: `test_merged_review_prompt.py` (as needed)

- Reflect broadened `_ARCHITECTURE_CONSISTENCY_BODY` (composition assertions that assumed the old document-only wording)

## Out of scope

- Temporal workflow/activities
- Snapshot comparison of findings
- Deleting standalone pass modules
- Changing false-positive verification or map-phase chunk review
- Changing side-effect instruction content (except shared delivery via the merged prompt)

## Acceptance criteria mapping

| Criterion | How this design meets it |
|---|---|
| `_run_tail_passes` invokes the merged pass | Single `merged` scheduled call |
| Response split into architecture vs side-effect shapes | Tuple return + existing validators |
| One fewer LLM call per submission (in-process) | Two tail additive LLM calls → one when both flags on |
| Coordinator tests updated | Monkeypatches and concurrency assertions retargeted |
| Architecture without a required doc | Gate removed + prompt body broadened |

## Risks / follow-ups

- Broadening architecture instructions can change finding volume/quality even before consolidation; later snapshot comparison should cover with-doc and without-doc submissions.
- Temporal parity has since landed: `combine_findings_activity` plus the reordered sequential pipeline (merged pass → combine → re-verify) now mirror this design's in-process merge order — see `temporal/workflows.py`'s module docstring (particularly the `_REORDERED_TAIL_PASSES_PATCH` gate) for the durable-mode equivalent and its old-history replay-compatibility contract.

## Status update: mutation-vs-replaced-code contract sub-check (2026-08-18)

Part 2 of the merged prompt (the side-effect / blast-radius half) gained a
mutation-analysis sub-check, gated by `CODE_REVIEW_MUTATION_ANALYSIS`
(default on). This section records how it plugs into the merge design above;
it does not restate the full behavioral contract, which lives in
`docs/ENV_VARS.md`'s `### CODE_REVIEW_MUTATION_ANALYSIS` entry.

**What it is.** When a changed file has a before-image on
`CodeReviewInput.replaced_content` (populated end to end from the PR diff's
removed-hunk side — see `api/pr_review.py::_build_replaced_content`), the
side-effect half may compare the file's current content against that shown
before-image for data/variable-mutation differences, assess whether the
difference changed the enclosing function/class's observable contract, and —
only when it did — use `find_references`/`search_repository` and
`read_file`/`read_function`/`read_lines` to inspect real callers before
deciding whether the new code or its callers are the defect (Design-by-
Contract framing). This is the *only* case in which the pass's otherwise-
absolute "never assume a prior version" guard relaxes, and only for the one
file whose before-image is actually shown — every other file keeps the
absolute guard regardless of this toggle.

**Where it plugs into this design.**

- `_build_prompt` (`merged_architecture_side_effect_pass.py`) takes a
  `replaced_content` parameter and, per changed path shown in a given call,
  renders a "Replaced (pre-change) content" block immediately after that
  path's current-content block when an entry is present — the same
  per-path-gated rendering the standalone `side_effect_impact_pass.py` uses.
- `_run_pass` never passes `input_data.replaced_content` straight through:
  it resolves `effective_replaced_content(input_data, mutation_on)`
  (`side_effect_consolidation.py`) first, so a disabled toggle hides the
  before-image from the model entirely rather than passing it through with
  an "ignore this" instruction.
- The sub-check's instruction text lives in `prompts.py`
  (`_build_side_effect_impact_body`'s `mutation_on` branch), shared verbatim
  between the standalone pass's system prompt and Part 2 of this merged
  pass's system prompt — one prompt-text source, not a fork.
- `MUTATION_ANALYSIS_ENV` is defined in `side_effect_consolidation.py`
  (alongside `SIDE_EFFECT_CONSOLIDATION_ENV`) rather than in either pass
  module, so `mapping.py`'s cache/fingerprint layer can read the toggle name
  without importing a tail-pass module.

**Cache-fingerprint interaction.** This sub-check is output-affecting, so it
participates in `mapping._submission_fingerprint` via a `__mutation_analysis__`
payload entry (alongside `__side_effect_consolidation__` and
`__spec_compliance_single_pass__`); `replaced_content` itself is already part
of that fingerprint by virtue of being a real `CodeReviewInput` field. A
verdict computed with a before-image, or under one toggle state, is never
served from a cache entry computed without one or under the other state.

**Testing.** `test_merged_architecture_side_effect_pass.py` covers: the
before-image reaching (or being hidden from) the merged prompt per the
toggle, the sub-check text appearing (or not) in the system prompt, and an
end-to-end pair proving the behavioral claim directly —
`test_fires_mutation_finding_when_before_image_present` /
`test_no_speculative_finding_without_before_image` script the *same*
conditional LLM reply (a mutation-contract finding, returned only when the
prompt actually shows a before-image) against inputs that do and don't carry
`replaced_content`, so the pair proves the finding path itself goes silent
without a before-image rather than merely asserting prompt text presence.
The standalone `side_effect_impact_pass.py` half has the identical pair.
