# Requirement Citation Review Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden SE code-review prompts so Spec Compliance findings must cite verbatim requirement text and must not invent named entities absent from task context.

**Architecture:** Export one shared guardrail string from `shared/prompts/`, append it only under Spec Compliance–flavored criteria in review profiles, mirror it (plus optional `requirement_citation:`) in the LLM-fallback template, and derive `CODE_REVIEW_PROMPT` from `build_review_system_prompt` so profiles are the single source of truth.

**Tech Stack:** Python 3.10+, pytest, existing `code_review_agent` profile composer.

## Global Constraints

- Do not mention GitHub issue numbers in code, comments, or commit messages; `Closes #1275` only in PR body.
- Prompt-only for `requirement_citation:` — no parser/grounding changes.
- Do not modify `ACCEPTANCE` or `DEVOPS_MAINTAINABILITY` criteria.
- `profiles` must not import `prompts` (avoid circular import after collapse).
- DbC on new public surfaces; TDD for prompt assertions.
- Coverage floor 90% for touched code.

## File map

| Path | Responsibility |
|---|---|
| `shared/prompts/requirement_citation.py` | `REQUIREMENT_CITATION_GUARDRAIL` constant |
| `shared/prompts/__init__.py` | Export the constant |
| `shared/prompts/templates.py` | Use constant in `build_code_review_prompt` |
| `code_review_agent/profiles.py` | Append constant to spec-flavored criteria; flip docs |
| `code_review_agent/prompts.py` | Derive `CODE_REVIEW_PROMPT` from builder |
| `tests/test_review_profiles.py` | Presence / absence / equivalence |
| `tests/test_prompt_templates.py` | Fallback prompt contains guardrail + field |

---

### Task 1: Failing tests for profiles + template

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_review_profiles.py`
- Create: `backend/agents/software_engineering_team/tests/test_prompt_templates.py`

**Interfaces:**
- Consumes: `REQUIREMENT_CITATION_GUARDRAIL` (to be added), `build_review_system_prompt`, `ReviewProfile`, `CODE_REVIEW_PROMPT`, `build_code_review_prompt`
- Produces: failing assertions that drive Task 2

- [ ] **Step 1: Write failing profile tests**

Add to `test_review_profiles.py`:

```python
from software_engineering_team.shared.prompts import REQUIREMENT_CITATION_GUARDRAIL


def test_requirement_citation_guardrail_in_spec_flavored_sections_only() -> None:
    code_review = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
    assert REQUIREMENT_CITATION_GUARDRAIL in code_review
    i_spec = code_review.index("**Spec Compliance**")
    i_guard = code_review.index(REQUIREMENT_CITATION_GUARDRAIL)
    i_naming = code_review.index("**Naming Conventions**")
    assert i_spec < i_guard < i_naming
    i_quality = code_review.index("**Code Quality**")
    assert i_guard < i_quality

    spec_conf = build_review_system_prompt(ReviewProfile.SPEC_CONFORMANCE)
    assert REQUIREMENT_CITATION_GUARDRAIL in spec_conf
    assert spec_conf.count(REQUIREMENT_CITATION_GUARDRAIL) >= 2

    senior = build_review_system_prompt(ReviewProfile.SENIOR_ARCHITECTURE)
    assert REQUIREMENT_CITATION_GUARDRAIL in senior
    i_cov = senior.index("**Spec Coverage**")
    i_g = senior.index(REQUIREMENT_CITATION_GUARDRAIL)
    i_risk = senior.index("**Maintainability & Risk**")
    assert i_cov < i_g < i_risk


def test_requirement_citation_guardrail_absent_from_non_spec_profiles() -> None:
    assert REQUIREMENT_CITATION_GUARDRAIL not in build_review_system_prompt(
        ReviewProfile.DEVOPS_MAINTAINABILITY
    )
    assert REQUIREMENT_CITATION_GUARDRAIL not in build_review_system_prompt(
        ReviewProfile.ACCEPTANCE
    )
```

Keep existing `test_code_review_profile_is_byte_identical_to_legacy_prompt`.

- [ ] **Step 2: Write failing template tests**

```python
# tests/test_prompt_templates.py
from software_engineering_team.shared.prompts import (
    REQUIREMENT_CITATION_GUARDRAIL,
    build_code_review_prompt,
)


def test_build_code_review_prompt_includes_requirement_citation_guardrail() -> None:
    prompt = build_code_review_prompt(project_kind="backend")
    assert REQUIREMENT_CITATION_GUARDRAIL in prompt
    assert "requirement_citation:" in prompt
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && .venv/bin/python -m pytest agents/software_engineering_team/tests/test_review_profiles.py agents/software_engineering_team/tests/test_prompt_templates.py -q --tb=line`

Expected: FAIL (import or assertion — guardrail not defined / not in prompts).

- [ ] **Step 4: Commit tests**

```bash
git add backend/agents/software_engineering_team/tests/test_review_profiles.py \
  backend/agents/software_engineering_team/tests/test_prompt_templates.py
git commit -m "test: require verbatim citation guardrail in SE review prompts"
```

---

### Task 2: Guardrail constant + wire profiles, prompts, templates

**Files:**
- Create: `backend/agents/software_engineering_team/shared/prompts/requirement_citation.py`
- Modify: `backend/agents/software_engineering_team/shared/prompts/__init__.py`
- Modify: `backend/agents/software_engineering_team/shared/prompts/templates.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/profiles.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/prompts.py`
- Modify: `backend/agents/software_engineering_team/tests/test_review_profiles.py` (doc comments only if needed)

**Interfaces:**
- Produces: `REQUIREMENT_CITATION_GUARDRAIL: str`
- Consumes: that constant in profiles + templates; `build_review_system_prompt` in prompts

- [ ] **Step 1: Add shared constant**

```python
# shared/prompts/requirement_citation.py
"""Shared Spec Compliance citation guardrail for code-review prompts."""

REQUIREMENT_CITATION_GUARDRAIL = (
    "You must be able to quote, verbatim, the requirement text a Spec Compliance "
    "finding is based on. Do not invent named entities (vendor names, provider "
    "names, feature names, integrations) that do not appear verbatim in the "
    "provided Requirements/Acceptance Criteria/Specification/Architecture "
    "context. If you cannot locate such a sentence, do not emit the issue."
)
```

Export from `__init__.py`.

- [ ] **Step 2: Append to profile criteria**

In `profiles.py`, import the constant. Append a bullet containing it under:
- `_CODE_REVIEW_CRITERIA` Spec Compliance item (before Naming)
- `_SPEC_CONFORMANCE_CRITERIA` Spec Compliance and Acceptance Criteria items
- `_SENIOR_ARCHITECTURE_CRITERIA` Spec Coverage item

Flip module docstring: profiles are SoT; `CODE_REVIEW_PROMPT` is a derived alias of `build_review_system_prompt(CODE_REVIEW)`.

- [ ] **Step 3: Collapse `prompts.py`**

Replace the large concatenation with:

```python
from code_review_agent.profiles import ReviewProfile, build_review_system_prompt

CODE_REVIEW_PROMPT = build_review_system_prompt(ReviewProfile.CODE_REVIEW)
```

Do not import `prompts` from `profiles`.

- [ ] **Step 4: Update `build_code_review_prompt`**

Insert the guardrail after the verify-requirements sentence; add
`requirement_citation: optional verbatim quote from Requirements/Acceptance Criteria/Specification/Architecture`
to the issue template block.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && .venv/bin/python -m pytest agents/software_engineering_team/tests/test_review_profiles.py agents/software_engineering_team/tests/test_prompt_templates.py -q --tb=short`

Expected: PASS.

- [ ] **Step 6: Commit implementation**

```bash
git add backend/agents/software_engineering_team/shared/prompts/ \
  backend/agents/software_engineering_team/code_review_agent/profiles.py \
  backend/agents/software_engineering_team/code_review_agent/prompts.py
git commit -m "feat: require verbatim citation for Spec Compliance review findings"
```

---

### Task 3: Plan doc + verification

- [ ] **Step 1: Ensure this plan is committed** (with design already on branch)

- [ ] **Step 2: Re-run targeted tests once more; report results**
