# Design: Normalize CodeReviewIssue severity in blocking checks

Date: 2026-08-06

Parent: GitHub issue for normalizing `_reconcile_approval` severity membership
(sub-issue of the code-review gate correctness work).

## Goal

Make critical/high blocking checks case-insensitive and whitespace-tolerant,
consistent with `_cap_issues` ranking, so values like `High` / `HIGH` /
` critical ` are treated as blocking. Keep the `_reconcile_approval`
postcondition: `approved is False` implies at least one critical/high finding
when that path applies.

## Context

`_cap_issues` already ranks with `(severity or "").strip().lower()` against
`_CAP_SEVERITY_RANK`. `_reconcile_approval` uses a case-sensitive membership
test:

```python
critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
```

`CodeReviewIssue.severity` is a free `str` (not the `CodeReviewIssueSeverity`
Literal). Chunk parsing usually lowercases via `chunking._issues_from_chunk_output`,
but other construction paths and mixed-case LLM leftovers can still reach the
gate. The same case-sensitive membership appears in
`ChunkReviewLLMResponse._require_approval_consistent_with_issues`, which is
documented as mirroring the coordinator gate.

`security_service.is_blocking` already folds case, but lives behind a
security-oriented API (`explicit_blocking`) and does not own the code-review
severity vocabulary.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Shared `_normalized_severity` helper (Approach 1), not `is_blocking` reuse |
| Helper home | `code_review_agent/models.py` next to `CodeReviewIssueSeverity` |
| Call sites | `_cap_issues`, `_reconcile_approval`, `ChunkReviewLLMResponse` validator |
| Blocking set | Normalized value in `{"critical", "high"}` only |
| Non-blocking | Mixed-case / padded `medium` / `low` / `info` / unknown still do not block |
| Taxonomies / UI | Unchanged |

## Scope

### In scope

- Add `_normalized_severity(severity: Optional[str]) -> str` returning
  `(severity or "").strip().lower()`, with DbC docstring
  (preconditions/postconditions).
- Refactor `_cap_issues` rank lookup to use the helper.
- Change `_reconcile_approval` blocking filter to use normalized membership.
- Change `ChunkReviewLLMResponse._require_approval_consistent_with_issues` to
  use the same normalized membership for the severity half of its actionable
  critical/high predicate (description / no-op suggestion rules unchanged).
- Unit tests for mixed-case and whitespace blocking / non-blocking behavior in
  reconcile and the LLM schema validator.

### Out of scope

- Changing the severity vocabulary or display strings.
- Migrating other teams onto this helper.
- Replacing phase/security gates with this helper (`is_blocking` stays as-is).
- Mutating stored severity strings on issues (normalize at compare time only).

## Architecture

```
models._normalized_severity(severity)
        │
        ├─ coordinator._cap_issues          (rank key)
        ├─ coordinator._reconcile_approval  (blocking membership)
        └─ ChunkReviewLLMResponse validator (blocking membership)
```

Coordinator continues to import models (existing dependency). Models do not
import coordinator.

## Error handling / contracts

- Helper never raises; empty/`None` → `""`.
- `_reconcile_approval` postcondition preserved: reject only when a normalized
  critical/high finding is present; reject-with-only-nits still flips to approve.
- Validator still requires actionable (non-blank description, non-no-op
  suggestion) critical/high findings for `approved=False`.

## Testing

- `test_code_review_coordinator.py`:
  - `_reconcile_approval(True, [issue("High"|"HIGH"|" critical ")])` →
    `approved is False`.
  - Reject with only mixed-case `Medium` → still auto-approves.
  - Existing lowercase cap/reconcile tests remain green.
- `test_chunk_review_llm_schema.py` (or equivalent):
  - `approved=False` + actionable `HIGH` validates.
  - `approved=True` + actionable `High` fails validation.

## Risks

- False positives if unknown severities somehow normalize to `critical`/`high`
  — they cannot; only exact folded tokens match.
- Broader import of a private helper from tests is fine; keep the name
  underscored as module-private shared within the package.
