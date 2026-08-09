# ADR-010 — Single-pass spec/acceptance-criteria compliance for code review

- **Status**: Accepted — implemented with a deviation from the original Decision; see the
  Amendment section at the bottom.
- **Date**: 2026-08-05
- **Owner**: Software Engineering Team / Code Review
- **Related**:
  - Reduces redundant per-chunk spec/architecture context in the code-review map phase; this ADR was
    the design-decision deliverable that gated that work's implementation sub-issues. Implemented by
    #5065, which gates per-chunk spec/acceptance-criteria inclusion behind
    `CODE_REVIEW_SPEC_COMPLIANCE_PASS` in `chunk_reviewer.py`, `coordinator.py`, and related modules,
    with the deviation from the original Decision documented in the Amendment section below.
  - `backend/agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py`
    — the existing once-per-submission tail-pass pattern this ADR's new pass is structurally modeled
    on.
  - `backend/agents/software_engineering_team/code_review_agent/synthesis.py` — the existing
    reduce-phase narrative-synthesis pattern this ADR's new pass feeds into unchanged.

## Context

The code-review coordinator's map-reduce pipeline
(`backend/agents/software_engineering_team/code_review_agent/coordinator.py`) reviews a submission
in bounded chunks (`chunking.py::build_review_chunks`), then merges the per-chunk results. Two
pieces of shared context — the project specification excerpt and the acceptance-criteria list — are
computed **once per submission**, but are then embedded verbatim into **every chunk's** prompt:

- `run_coordinator` computes `spec_content`/`arch_overview` once, via
  `compact_text`/`compute_code_review_spec_excerpt_chars`, and stores them, unchanged, in
  `base_input`.
- That `base_input` is handed to every chunk via `ChunkReviewInput`.
- `chunk_reviewer.py::_run_chunk_review` renders `input_data.acceptance_criteria`
  and `input_data.spec_excerpt` into the prompt's `context_parts` for **each** chunk.

On a submission with N chunks, the same spec/acceptance-criteria text transmits N times — on a
15-chunk submission with a ~14K-character spec excerpt, that is ~14K × 15 of pure duplication,
proportional to chunk count rather than to submission size.

The codebase already has a working precedent for eliminating exactly this kind of duplication for a
*different* kind of whole-submission check:
`merged_architecture_side_effect_pass.py::find_architecture_and_side_effect_issues` runs
architecture-consistency and side-effect-impact checks **once per submission, never once per
chunk** (its own module docstring says so explicitly), gated by `env_flag_enabled` env vars
(`shared/env/__init__.py:32`), additive-only and fail-safe (`try/except` → `([], [])` on any
failure), invoked from `coordinator.py::_run_tail_passes` after the map phase, running
concurrently with the false-positive filter via `parallel_map` when possible.

Separately, `synthesis.py::synthesize_review_findings` already performs a single, cheap,
**findings-only** (no source code) reduce-phase LLM pass that consolidates N per-chunk
`spec_compliance_notes` strings into one narrative — but that pass only rewrites text the per-chunk
reviewers already produced; it does not eliminate the per-chunk spec/AC duplication that produced
those notes in the first place. `_merge_narrative` already skips this
synthesis LLM call entirely when there is exactly one chunk and no additive tail-pass findings —
an existing precedent for "run once, not per chunk" wherever possible.

**Explicitly out of scope** (per the parent work item this ADR resolves): removing per-chunk
`architecture_overview`/sibling-surface context (it stays exactly as-is), and any change to the
false-positive filter or the existing merged architecture/side-effect pass.

## Decision

Add a new, additive, fail-safe tail pass — structurally modeled on
`find_architecture_and_side_effect_issues`, but functionally scoped to spec/acceptance-criteria
compliance only — gated by a new environment flag:

**`CODE_REVIEW_SPEC_COMPLIANCE_PASS`**, read via `env_bool(name, default=False)`
(`shared/env_config/config.py:56`) — **default off**.

This is a deliberate departure from the sibling architecture/side-effect flags, which use
`env_flag_enabled` (default **on**, opt-out). Those flags are purely additive: disabling them only
removes extra findings, never changes what the map phase already does. This flag is not purely
additive — when on, it *removes* the acceptance-criteria/spec-excerpt blocks from every chunk's
prompt, a real change to existing per-chunk behavior. Defaulting off means every existing submission
keeps its current per-chunk spec/AC review unchanged unless an operator explicitly opts in, matching
the parent work item's requirement that per-chunk spec inclusion be "gated behind a new flag,
defaulting to current behavior."

### When `CODE_REVIEW_SPEC_COMPLIANCE_PASS` is on

1. **Per-chunk prompt shrinks.** `chunk_reviewer.py::_run_chunk_review` skips the
   `acceptance_criteria` and `spec_excerpt` blocks. This is driven
   by a new boolean threaded through `ChunkReviewInput` (e.g.
   `spec_compliance_single_pass: bool = False`), computed once in `run_coordinator` from the flag
   and copied into every chunk's `base_input` — never re-read from the environment per chunk.
   `architecture_overview` and every other context block are untouched, per the
   out-of-scope boundary above. The per-chunk LLM response schema
   (`ChunkReviewLLMResponse.spec_compliance_notes`) is not changed — a chunk given no spec/AC
   context naturally has nothing concrete to report and is expected to return an empty note, which
   is exactly this pipeline's existing contract for "no gaps found" (`synthesis.py`'s docstring:
   "an empty string means the reviewers recorded no spec/acceptance-criteria gaps").

2. **A new tail pass runs once per submission.** New module (e.g. `spec_compliance_pass.py`,
   alongside `merged_architecture_side_effect_pass.py`), invoked as an additional entry in
   `_run_tail_passes`'s `calls` list so it runs concurrently with the
   false-positive filter and the merged architecture/side-effect pass when the run's concurrency
   budget allows. It receives the already-computed, already-compacted `spec_content` and
   `input_data.acceptance_criteria` (no re-compaction) plus the full changed-code content, inlined
   and budgeted the same way `find_architecture_and_side_effect_issues` budgets its changed-file
   inlining. Restricted to the `CODE_REVIEW` profile only, matching every sibling tail pass. Any
   setup/LLM/validation failure is fail-safe: logged, and the pass contributes nothing (an empty
   note), never blocking or changing the rest of the review.

3. **Output shape: one narrative note, not new blocking issues.** The new pass's result is a single
   string with the same shape and contract as one chunk's `spec_compliance_notes` field — concrete
   spec/acceptance-criteria gaps only, empty when there are none. It does not mint new
   `CodeReviewIssue` entries and does not re-decide the verdict, matching
   `synthesize_review_findings`'s existing invariant that narrative synthesis "is best-effort and
   never authoritative."

4. **Feeds into existing, unchanged reduce-phase machinery.** The single note is passed to
   `_merge_narrative`/`synthesize_review_findings` as a length-1 `chunk_spec_notes` list, replacing
   the N per-chunk notes `outcome.spec_notes` would otherwise have contributed (which are now empty,
   per point 1). `synthesize_review_findings` and `build_findings_digest` require **no code
   change** — they already accept `chunk_spec_notes: List[str]` of any length. The one change
   required in `coordinator.py` is `_merge_narrative`'s single-chunk fast path:
   today it returns `outcome.summaries[0]`/`outcome.spec_notes[0]`
   directly, skipping the synthesis LLM call, whenever there is exactly one chunk and
   `has_additive_pass_findings` is `False`. That condition must be generalized to also require the
   new pass's note is empty — otherwise a single-chunk submission with a real single-pass finding
   would have that finding silently dropped by the fast path, since `outcome.spec_notes[0]` would be
   empty (the chunk was given no spec/AC context to comment on) while the real note sits unused in
   the tail-pass result.

5. **When the flag is off (default): zero behavior change.** `_run_tail_passes` does not schedule
   the new pass, `chunk_reviewer.py` renders the full per-chunk spec/AC context exactly as it does
   today, and `_merge_narrative`'s fast path is unaffected. This is the concrete guarantee behind
   "defaulting to current behavior."

### Rejected alternatives

- **Reuse `synthesize_review_findings` itself as the compliance-detection pass** (rather than adding
  a new tail pass that *feeds* it). Rejected because `synthesize_review_findings` is deliberately
  findings-only — its own module docstring states "Source code is never included" — and spec
  compliance cannot be judged without seeing the actual code changes. Repurposing it to also inline
  code would break that invariant for every other caller of the same function (the general
  summary-narrative synthesis, which must stay cheap and code-free). Keeping them as two separate
  passes — a new code-aware compliance pass feeding a single note into the existing code-free
  narrative synthesizer — preserves both existing invariants while still achieving "one dedicated
  compliance pass" that the parent work item asked for.
- **Default the new flag on (`env_flag_enabled`-style), matching the architecture/side-effect
  passes.** Rejected because those passes are purely additive and safe to default on; this flag
  actively changes what today's chunk prompts contain, which is a strictly larger blast radius that
  should be opt-in until validated in practice (token/cost delta measurement and dual-mode test
  coverage are explicitly separate, later work items).
- **Mint new blocking `CodeReviewIssue` findings from the single pass** (like the architecture/
  side-effect pass does) instead of a narrative note. Rejected because per-chunk spec-compliance
  output today is informational narrative only — it has never gated the approval verdict — and
  changing that contract is a separate, larger decision than "where does the compliance text come
  from." Keeping the output shape identical (one note) makes this purely a plumbing change from the
  downstream narrative-synthesis machinery's point of view.

## Risks and tradeoffs

- **Chunk-local awareness loss.** A per-chunk reviewer today can pin a spec violation to the exact
  chunk/file/line it was actively reviewing, because it holds both the spec and that chunk's code
  simultaneously. A single post-merge pass instead sees the whole diff at once and must
  self-attribute file/line references across every changed file, so attribution precision may
  degrade on very large, many-file submissions. Large submissions may also require the same kind of
  content budgeting/truncation the architecture pass already accepts
  (`compute_code_review_merged_pass_budgets`), which the per-chunk model — each chunk individually
  bounded, but never truncating
  the spec itself — does not need.
- **Token-cost efficiency.** Duplication drops from O(chunks × spec/AC size) to O(1) for the
  spec/AC text; the new pass still inlines the full changed-code content once (comparable to what
  the merged architecture/side-effect pass already pays), so net savings scale with chunk count and
  previously-duplicated spec/AC size — consistent with the ~14K-characters-×-15-chunks duplication
  this ADR's context section describes. Savings are close to zero on small (1-2 chunk) submissions,
  where `_merge_narrative`'s existing fast path already avoids extra LLM calls.
- **Fail-safe posture changes from N independent chances to one.** Today, if one chunk's reviewer
  fails to produce a usable spec note, the other N-1 chunks' notes are unaffected — partial
  degradation. A single pass is fail-safe (failure → empty note, never blocks) but is a single point
  of failure for spec-compliance findings specifically: one bad LLM call or a truncated budget can
  silently zero out spec-compliance narrative for the entire submission where today's per-chunk
  model would have caught at least some violations independently. This is an accepted tradeoff of
  centralizing the check, not an oversight, and should be watched via the token/cost and quality
  measurement called for separately.
- **Two materially different code paths behind one flag increase testing burden.** Per-chunk-inclusion
  mode and single-pass mode diverge in prompt content, tail-pass scheduling, and the
  `_merge_narrative` fast-path condition — both modes need dedicated test coverage, not just a
  flag-flip smoke test.

## Contract boundary

A future implementation must satisfy exactly this surface:

- New env var `CODE_REVIEW_SPEC_COMPLIANCE_PASS`, read via `env_bool(..., default=False)` — default
  off, matching `CODE_REVIEW_BLOCK_ON_UNREVIEWED`'s "restore/opt-in" flag style rather than the
  sibling tail passes' default-on `env_flag_enabled` style.
- `ChunkReviewInput` (`models.py`) gains one new field controlling whether
  `chunk_reviewer.py::_run_chunk_review` renders the `acceptance_criteria`/`spec_excerpt` prompt
  blocks; `architecture_overview` rendering is unaffected.
- A new tail-pass module, function-shaped like `find_architecture_and_side_effect_issues`
  (`llm`, `input_data`, optional `repo_reader`/`index` in; a single narrative string out, empty on
  any failure or when the flag is off), restricted to `ReviewProfile.CODE_REVIEW`, added as one more
  entry in `_run_tail_passes`'s `calls` list.
- `_TailPassResult` gains a field carrying the new pass's note (e.g.
  `spec_compliance_note: str = ""`), threaded through `_run_tail_passes`'s return.
- `_merge_narrative`'s single-chunk fast-path condition
  (`len(outcome.summaries) == 1 and not has_additive_pass_findings`) must additionally require the
  new pass's note to be empty before skipping the synthesis LLM call; when the new pass has a
  non-empty note, `chunk_spec_notes` passed to `synthesize_review_findings` is
  `[tail_pass_result.spec_compliance_note]` instead of `outcome.spec_notes`.
- `synthesize_review_findings`, `build_findings_digest`, and `REVIEW_SYNTHESIS_PROMPT`
  (`synthesis.py`, `prompts.py`) — **unchanged**. They already accept `chunk_spec_notes` of any
  length; only the caller's data source changes.
- `docs/ENV_VARS.md` gets a new `### CODE_REVIEW_SPEC_COMPLIANCE_PASS` entry alongside the
  existing `CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS` / `CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS` /
  `CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION` entries, following their established structure (default
  state, what it changes, fail-safe contract, how to enable/disable).

## Consequences

- **The design question is closed, not deferred.** A concrete flag name, default value, structural
  home (new tail pass feeding the existing narrative synthesizer, unchanged), and the exact
  `_merge_narrative` edge case that must not regress are all specified here.
- **No existing submission changes behavior until the flag is explicitly enabled.** Default-off
  means every currently-running review keeps its current per-chunk spec/AC prompt content and
  current tail-pass set unchanged.
- **`synthesize_review_findings` and its digest/prompt stay untouched**, minimizing the blast radius
  of the eventual implementation to `chunk_reviewer.py`'s prompt assembly, one new tail-pass module,
  and `_merge_narrative`'s fast-path condition.
- **This ADR does not itself implement anything.** *(Superseded by the Amendment below: the gating
  sub-issue has since landed. What it actually did was add the `env_bool` flag read, thread the new
  `ChunkReviewInput` boolean, wire the already-shipped, findings-only `synthesize_spec_compliance`
  call into `run_coordinator` immediately before `_merge_narrative` — not a new tail-pass module, and
  no `_TailPassResult`/`_run_tail_passes` change, per the Amendment — adjust `_merge_narrative`'s
  fast-path condition, add dual-mode test coverage (≥90% on changed code, per this repository's
  testing floor), and add the `docs/ENV_VARS.md` entry described above. Token/cost delta measurement
  on a representative multi-chunk fixture remains a separate, later work item.)*

## Amendment (implementation, gating sub-issue)

The single-pass function implementation sub-issue shipped `synthesize_spec_compliance`
(`backend/agents/software_engineering_team/code_review_agent/synthesis.py`) as a
**findings-only** pass — paired with `synthesize_review_findings`, taking the final
deduped issue list plus the full spec/acceptance-criteria text, with **no source code**
included — rather than the code-aware tail-pass module this ADR's Decision section
originally specified. The gating sub-issue then wired that already-shipped function in
as "the new single pass," instead of writing a second, separate module. Concretely, this
means the Decision/Contract-boundary text above is superseded on the following points:

- **No new tail-pass module** (e.g. `spec_compliance_pass.py`) was written. There is no
  code-aware pass that inlines changed-file content, and nothing runs inside
  `_run_tail_passes` for this purpose.
- **`_TailPassResult` gained no new field.** `synthesize_spec_compliance` requires the
  *final* deduped issue list (post-dedupe) per its own precondition, which is only
  available after `_run_tail_passes` and `_dedupe_issues`/`_cap_issues`/
  `_reconcile_approval` complete — well outside the concurrent tail-pass phase — so
  there is nothing to thread through that machinery.
- **The call site is in `run_coordinator`, immediately before `_merge_narrative`,** gated
  on `spec_compliance_single_pass and input_data.profile == ReviewProfile.CODE_REVIEW`.
  Its result is passed into a new `_merge_narrative(..., single_pass_spec_notes=...)`
  parameter: `None` (flag/profile off, or the pass failed) preserves today's behavior
  exactly (including the single-chunk fast path); a non-`None` value (possibly `""`)
  unconditionally bypasses the fast path and feeds `[single_pass_spec_notes]` into
  `synthesize_review_findings` in place of `outcome.spec_notes` — satisfying the same
  "must not silently drop a real single-chunk finding" concern the Contract boundary's
  point 4 raised, without needing the `_TailPassResult` field it proposed.
- The `ChunkReviewInput` boolean (`spec_compliance_single_pass`), the `env_bool`-gated,
  default-off flag read (computed once in `run_coordinator`), the `chunk_reviewer.py`
  gating of the `acceptance_criteria`/`spec_excerpt` blocks (leaving
  `architecture_overview` untouched), and the `CODE_REVIEW` profile restriction all
  landed exactly as this ADR specified.

Rationale for taking the smaller path: `synthesize_spec_compliance` was already
implemented, reviewed, and merged by the time the gating sub-issue started, and its own
issue's acceptance criteria described it in the same findings-only terms ("runs once
over the merged issue list plus the full spec/acceptance criteria") the gating
sub-issue's acceptance criteria then referred back to ("routes spec compliance through
the new single pass"). Writing a second, code-aware pass would have duplicated
functionality without a clear justification for the extra complexity relative to this
sub-issue's own Fibonacci complexity score.
