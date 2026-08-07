# Design: Code-review additive-pass context-overflow recovery

Date: 2026-08-06

## Goal

When a code-review additive LLM call exceeds the model context window, recover by
chunking and/or shrinking the prompt and retrying so findings can still be
produced. A context-overflow alone must never silently return empty findings.

## Context

`MergedArchitectureSideEffectPass` (and the standalone architecture /
side-effect passes) already budget the *initial* prompt via
`compute_code_review_merged_pass_budgets` / map-chunk caps. Mid-turn tool use
and underestimate of tokens can still overflow the window. Today any exception
— including provider `400` errors like
`input length (N tokens) exceeds the model's maximum context length (M tokens)`
— is caught by a fail-safe that logs and returns no additional findings.

The map-phase chunk reviewer already classifies content failures and
bisects/retries. Additive whole-submission passes do not.

## Decisions

| Topic | Choice |
|---|---|
| Primary strategy | Proactive file-group chunking **and** reactive shrink/bisect retry |
| Per-call visibility | Full changed-path manifest every call; inline only that group’s file bodies; tools reach omitted bodies and the rest of the repo |
| Tool results | Leave unbounded in v1 (no `read_file` / search truncation) |
| Scope | All code-review additive paths that currently fail-empty on overflow: merged architecture+side-effect, standalone architecture consistency, standalone side-effect impact (Temporal activities stay thin wrappers) |
| Implementation style | Shared recovery helper module (Approach 1), not a full pass-runner refactor |
| Non-overflow failures | Unchanged fail-safe (empty for that call/pass; never break the review) |
| Cost invariant | Replace “exactly one LLM call” with “one call when the submission fits; otherwise N group calls + bounded reactive retries” |

## Scope

### In scope

- New helper module under `backend/agents/software_engineering_team/code_review_agent/`
  (e.g. `context_overflow_recovery.py`) providing:
  - `is_context_overflow(exc)` — walk `__cause__` / `__context__`; match provider
    400 / phrases such as `exceeds the model's maximum context length`,
    `input length`, `context length`, `context window`
  - `pack_changed_file_groups(...)` — ordered groups whose inlined bodies fit
    the call’s inline budget
  - `run_chunked_pass(...)` — proactive groups → per-call invoke → merge
    findings; on overflow, reactive bisect/shrink and retry
- Wire the helper into:
  - `merged_architecture_side_effect_pass.py`
  - `architecture_consistency_pass.py`
  - `side_effect_impact_pass.py`
- Prompt builders: optional “inline only these paths” filter while always
  rendering the full path manifest (subject to existing manifest budget)
- Update pass module docstrings / invariants that claim a single LLM call
- Unit and integration tests for classifier, packing, recovery sequencing, and
  per-pass wiring

### Out of scope

- Bounding or truncating tool results (`read_file`, `search_repository`, etc.)
- Map-phase chunk-review recovery (already has bisect/retry)
- Compaction-first recovery (`compact_text`) as the primary overflow strategy
- Full shared submission-pass runner refactor (Approach 2) — follow-on work that
  extracts budgeting, chunking, Agent construction, and recovery into one runner
  so each pass only supplies prompt/tools/parsers

## Architecture

```
find_*_issues (pass entry, never raises)
  └─ _run_pass / equivalent
       ├─ budgets (existing helpers)
       ├─ pack_changed_file_groups(files, max_inline_code_chars)
       └─ run_chunked_pass(groups, invoke_fn)
            for each group:
              build prompt (full manifest + group bodies + arch section)
              try invoke_fn(prompt) → parse/validate findings
              on context overflow:
                multi-file → bisect groups, enqueue
                single-file → shrink inline budget, retry (bounded)
                exhausted → log, skip group (keep other groups)
              on other error → fail-empty that group
            merge + dedupe findings
```

Temporal activities continue to call the public `find_*` entry points; recovery
lives in-process inside those functions.

## Algorithms

### Proactive packing

1. Compute budgets as today (`compute_code_review_merged_pass_budgets` for the
   merged pass; map-chunk / existing caps for standalone passes).
2. Pack `index.files` into ordered groups where each group’s inlined bodies fit
   `max_inline_code_chars`, reusing the same per-file block fitting /
   truncation-note pattern as `_fit_changed_file_block`.
3. If everything fits in one group → one call (current shape).

### Per-call prompt shape

- Architecture section when enabled (still budgeted / truncatable as today)
- **Full** changed-path manifest for the submission (truncated only when the
  manifest budget requires it, with `list_changed_files` recovery note)
- **Full content** only for files in this group; omitted bodies reachable via
  tools
- Same tools as today (unbounded)

### Reactive recovery

```
on context-overflow for a group:
  if group has >1 file → bisect into two groups, enqueue both
  else if single file still too large inline → shrink inline budget
       (more omission → tools) and retry within a small ceiling
  else → log + skip that group (fail-empty for that group only)
```

Cap reactive depth (bisect depth analogous to map-phase, plus a small
shrink-retry ceiling) so cost stays bounded.

### Overflow detection

Walk the exception chain the same way map-phase classifiers do. Only
context-overflow matches trigger shrink/bisect. Everything else keeps the
existing fail-safe.

### Finding merge

Concatenate findings from successful groups. Deduplicate by a stable identity
such as `(file_path, line, category, description)` (or an existing issue
identity helper if one already exists) so overlapping recovery work does not
double-count.

## Error handling

| Failure | Behavior |
|---|---|
| Context overflow | Bisect / shrink / retry (bounded); do not treat as “no findings” until recovery for that group is exhausted |
| Recovery exhausted for a group | Log warning; continue with other groups’ findings (partial success) |
| Non-overflow LLM / parse / setup error | Unchanged fail-safe: empty for that call/group; never break the review |
| Outer pass entry | Still never raises to the coordinator |

## Testing

- **Unit:** overflow classifier (direct message and wrapped
  `EventLoopException`); file-group packing; bisect/shrink retry sequencing
  with a fake callable that fails then succeeds.
- **Integration (Dummy / scripted client):** oversized multi-file submission →
  ≥2 calls → findings merged; overflow-then-recover path returns findings
  instead of `[]`; non-overflow failure still returns empty without raising.
- Extend existing merged / architecture / side-effect tests rather than only
  testing the helper in isolation.

## Docs / configuration

- Update pass module docstrings and any README notes that claim “exactly one
  LLM call” for these additive checks.
- No new environment variables required for v1. An optional bisect-depth
  override may be added later if operators need it; default to a map-phase-style
  depth constant in code.

## Follow-on (Approach 2)

After this helper lands and proves out, extract a shared submission-pass runner
that owns budgeting, chunking, Agent construction, and recovery. Each pass then
only supplies prompt, tools, and parse/validate logic. Tracked separately so
this change stays a focused recovery fix.
