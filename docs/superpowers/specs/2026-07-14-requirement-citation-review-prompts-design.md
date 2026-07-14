# SE Review Prompts: Verbatim Requirement Citation Guardrail

**Status:** Draft 2026-07-14  
**Date:** 2026-07-14  
**Type:** Prompt hardening / hallucination prevention for code-review gates  
**Issue:** GitHub #1275 (PR body only; do not cite in code)

## Problem

The review prompts used by the gated SE execution loop are broad and open-ended for Spec Compliance–style findings:

- `code_review_agent` default profile (`ReviewProfile.CODE_REVIEW` via `profiles.py`) — multiple checklist categories, none requiring a Spec Compliance claim to quote the actual requirements / acceptance criteria.
- LLM-fallback `shared/prompts/templates.py::build_code_review_prompt` — asks to verify correctness against requirements with no verbatim-citation rule.

Only `ReviewProfile.ACCEPTANCE` already has the right discipline (verbatim criterion prefix; no non-acceptance issues). That gap contributed to a hallucination loop where the reviewer invented an unrelated insurance-coverage requirement.

Companion work post-filters fabricated named entities in LLM-fallback output; this issue hardens the prompts themselves so the model is less likely to emit those findings.

## Goals

1. Add a shared verbatim-citation / no-invented-entities guardrail scoped to Spec Compliance–flavored checklist items.
2. Apply it to `CODE_REVIEW`, `SPEC_CONFORMANCE`, and `SENIOR_ARCHITECTURE` (its Spec Coverage item); leave naming, structure, code quality, docs, testing, refactoring, correctness, and maintainability categories unaffected.
3. Mirror the same guardrail prose in `build_code_review_prompt`, and add an optional prompt-only `requirement_citation:` field on that path’s issue template.
4. Collapse `code_review_agent/prompts.py::CODE_REVIEW_PROMPT` so it is derived from `build_review_system_prompt(ReviewProfile.CODE_REVIEW)` — profiles become the single source of truth.

## Non-goals

- Parsing or validating `requirement_citation` in the LLM-review parser or grounding filter (prompt-only nudge; companion grounding owns runtime filtering).
- Changing `ReviewProfile.ACCEPTANCE` (already stricter) or `DEVOPS_MAINTAINABILITY`.
- Broadening `_SHARED_OUTPUT_SECTION` so the guardrail applies to all finding categories.
- Embedding / second-LLM re-judging of findings.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Shared guardrail constant appended only to Spec Compliance–flavored criteria | Matches risk mitigation in the issue; does not chill non-spec findings |
| Constant location | `shared/prompts/` (exported for both profiles and templates) | Avoids `shared` importing `code_review_agent` |
| `requirement_citation:` field | Prompt-only on LLM-fallback template | Nudge without expanding this issue into parser/grounding work |
| Legacy `CODE_REVIEW_PROMPT` | Derive from `build_review_system_prompt(CODE_REVIEW)` | Single source of truth; keeps the existing equality invariant by construction |
| Senior architecture target | Append under **Spec Coverage** (that profile’s spec-flavored item) | No item literally named Spec Compliance on that profile |
| ACCEPTANCE profile | Unchanged | Already requires verbatim criterion prefixes |

## Architecture

```
REQUIREMENT_CITATION_GUARDRAIL  (shared/prompts/)
        │
        ├─► profiles.py criteria (CODE_REVIEW / SPEC_CONFORMANCE / SENIOR_ARCHITECTURE)
        │         └─► build_review_system_prompt(...)
        │                   └─► prompts.CODE_REVIEW_PROMPT  (derived alias)
        │
        └─► templates.build_code_review_prompt(...)
                  (+ optional requirement_citation: in issue template)
```

### Guardrail text

Exact intended meaning (wording may be lightly adapted for checklist bullet grammar, but must retain these constraints):

> You must be able to quote, verbatim, the requirement text a Spec Compliance finding is based on. Do not invent named entities (vendor names, provider names, feature names, integrations) that do not appear verbatim in the provided Requirements/Acceptance Criteria/Specification/Architecture context. If you cannot locate such a sentence, do not emit the issue.

### Profiles (`code_review_agent/profiles.py`)

- Import and append the guardrail under:
  - `_CODE_REVIEW_CRITERIA` — **Spec Compliance** item only
  - `_SPEC_CONFORMANCE_CRITERIA` — **Spec Compliance** and **Acceptance Criteria** items (both are spec-flavored)
  - `_SENIOR_ARCHITECTURE_CRITERIA` — **Spec Coverage** item only
- Do not append into Naming, File Structure, Code Quality, Documentation, Testing, Integration, Architecture Fit/Consistency, Refactoring, Correctness, Maintainability, or DevOps criteria.
- Update module docs: profiles are the source of truth; `CODE_REVIEW_PROMPT` is a derived alias, not the canonical duplicated string.

### Legacy prompt collapse (`code_review_agent/prompts.py`)

- Replace the large duplicated concatenation with:

  `CODE_REVIEW_PROMPT = build_review_system_prompt(ReviewProfile.CODE_REVIEW)`

- Keep exporting `CODE_REVIEW_PROMPT` for existing importers and size comments.
- Avoid circular imports: `prompts` imports `profiles`; `profiles` must not import `prompts`.

### LLM-fallback template (`shared/prompts/templates.py`)

- Insert the same guardrail near the “verify … correctness against requirements and acceptance criteria” clause.
- Extend the issue template with optional:

  `requirement_citation: optional verbatim quote from Requirements/Acceptance Criteria/Specification/Architecture`

- Do not change parsers to require or store that field in this change.

## Testing

1. **`tests/test_review_profiles.py`**
   - Guardrail present in Spec Compliance / Acceptance Criteria / Spec Coverage sections of the three targeted profiles.
   - Guardrail absent from Naming / Code Quality style sections of `CODE_REVIEW`, and absent from `DEVOPS_MAINTAINABILITY`.
   - Keep `build_review_system_prompt(CODE_REVIEW) == CODE_REVIEW_PROMPT` (true by construction after collapse).
   - Flip comments that call `prompts.py` the “canonical” duplication source.

2. **Template coverage** (extend an existing shared-prompts test file, or add a focused one if none covers `build_code_review_prompt`):
   - Assert the guardrail text and `requirement_citation:` appear in `build_code_review_prompt` output.

3. Rely on companion grounding-filter regression tests as the functional backstop for end-to-end hallucination prevention (prompt-level LLM behavior is not deterministically unit-testable).

## Files

| Path | Change |
|---|---|
| `shared/prompts/` (constant + `__init__.py` export) | New shared guardrail string |
| `code_review_agent/profiles.py` | Append guardrail to spec-flavored criteria; doc flip |
| `code_review_agent/prompts.py` | Derive `CODE_REVIEW_PROMPT` from builder |
| `shared/prompts/templates.py` | Guardrail + optional `requirement_citation:` |
| `tests/test_review_profiles.py` | Presence / absence / equivalence assertions |
| Template tests | Assert fallback prompt contains guardrail + field |

## Implementation notes

- Follow DbC on any new public constant/export if wrapped in a helper; pure string constants need only a short module/doc comment.
- TDD: extend failing profile/template assertions first, then implement.
- Do not mention GitHub issue numbers in code, comments, or commit messages; use `Closes #1275` only in the PR body.
- Coordinate with companion grounding work: that filter remains the runtime backstop even if the model ignores the prompt.
