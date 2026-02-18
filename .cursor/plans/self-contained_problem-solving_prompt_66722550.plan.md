---
name: Self-contained problem-solving prompt
overview: Make the problem-solving mode section self-contained by inlining all issue details (QA, security, code review, and for frontend accessibility) into that section and updating instructions to reference "this section" instead of "above".
todos: []
isProject: false
---

# Self-contained problem-solving prompt

## Problem

The problem-solving header built in [software_engineering_team/shared/prompt_utils.py](software_engineering_team/shared/prompt_utils.py) says "Identify the likely root cause from the **error details above**" and only lists issue **counts** (e.g. "code review issues: 1"). The actual issue text (descriptions, locations, recommendations) is appended later in the prompt under **Task**, **Requirements**, and then **QA issues to fix** / **Code review issues to resolve**. So:

- "Above" is wrong — the details appear **below** in the document.
- The section is not self-contained — the model must scan the rest of the prompt to find what to fix.

## Approach

1. **Inline issue details into the problem-solving block**
  When `has_issues` is true, the block sent to the LLM should be one contiguous "problem-solving section": header + instructions + **full issue text** (QA, security, code review; plus accessibility on frontend), then the separator, then Task/Requirements/etc. No reference to "above" or "elsewhere".
2. **Avoid duplication**
  Issue text should appear only inside this block, not again later in the prompt. So when building `context_parts`, only add the issue subsections (e.g. "**QA issues to fix:**" + qa_text) when building the problem-solving block; do not add them again in the existing `if input_data.qa_issues` / `if input_data.code_review_issues` branches.
3. **Update instruction wording**
  Change the default instructions in `prompt_utils.py` from "from the error details above" to something like "from the issue details in this section" so the header is accurate and self-contained. Optionally align frontend custom instructions similarly.

---

## Implementation

### 1. [software_engineering_team/shared/prompt_utils.py](software_engineering_team/shared/prompt_utils.py)

- In `build_problem_solving_header`, change the default instruction line from:
  - `"1. Identify the likely root cause from the error details above.\n"`  
  to:
  - `"1. Identify the likely root cause using the issue details in this section.\n"`
- No API change: still accept `issue_summaries` (counts) and optional `instructions`; callers will add the full issue text after the header.

### 2. Backend agent: single problem-solving block with inlined issues

In [software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py), in the prompt-building block (around 1500–1565):

- When `has_issues`:
  - Build the header with `build_problem_solving_header` as today.
  - Build formatted issue strings **here** (same format as today: qa_text, sec_text, cr_text).
  - Append to `context_parts` a **single** block: `header` + newline + (if qa_issues) `"**QA issues to fix (implement these):**"` + qa_text + (if security_issues) same for security + (if code_review_issues) same for code review.
  - Do **not** extend `context_parts` with the existing `if input_data.qa_issues` / `if input_data.security_issues` / `if input_data.code_review_issues` blocks when `has_issues` is true (so issue content appears only in the problem-solving section).
- When `has_issues` is false: keep current behavior (no issue blocks at all).

Resulting order in the prompt: `BACKEND_PROMPT` + `---` + (if has_issues: problem-solving block with header + all issue text) + `**Task:**` + `**Requirements:**` + … (spec, architecture, existing code, API spec; no duplicate issue sections).

### 3. Frontend agent: same pattern

In [software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py), mirror the backend:

- When `has_issues`:
  - Build header (with `_ANGULAR_PROBLEM_SOLVING_INSTRUCTIONS`).
  - Build qa_text, sec_text, a11y_text, cr_text in the same place.
  - Append one problem-solving block: header + QA + Security + Accessibility + Code review issue text (each only if present).
  - Do not add the same issue content again in the later `if input_data.qa_issues` / security / accessibility / code_review_issues branches when `has_issues` is true.
- Optional: in `_ANGULAR_PROBLEM_SOLVING_INSTRUCTIONS`, replace "Use the provided compiler/test errors" with wording that refers to "the issue details in this section" so the frontend block is also self-describing.

### 4. Tests

- **[software_engineering_team/tests/test_prompt_utils.py](software_engineering_team/tests/test_prompt_utils.py)**  
  - Update the assertion that checks for "Identify the likely root cause" to expect "using the issue details in this section" (or the new phrase) instead of "from the error details above".
- **Backend [software_engineering_team/tests/test_backend_agent.py**](software_engineering_team/tests/test_backend_agent.py)  
  - In tests that provide `code_review_issues` (or qa/security) and assert PROBLEM-SOLVING MODE is present, add an assertion that the **prompt** contains the actual issue content (e.g. description or suggestion from the provided issues) **inside** the same prompt (so we know it’s inlined). Optionally assert that "error details above" is not in the prompt.
- **Frontend [software_engineering_team/tests/test_frontend_agent.py**](software_engineering_team/tests/test_frontend_agent.py)  
  - Same idea: when problem-solving mode is on, assert that the prompt contains the inlined issue text (e.g. the code review suggestion or description) and does not rely on "above".

---

## Summary


| File                           | Change                                                                                                                                                |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `shared/prompt_utils.py`       | Default instruction: "error details above" → "issue details in this section".                                                                         |
| `backend_agent/agent.py`       | When `has_issues`, build one block (header + QA + security + code review text) and append it; skip adding issue blocks in the later branches.         |
| `frontend_agent/agent.py`      | Same: one problem-solving block (header + QA + security + a11y + code review text); no duplicate issue blocks. Optionally tweak Angular instructions. |
| `tests/test_prompt_utils.py`   | Assert new instruction phrase.                                                                                                                        |
| `tests/test_backend_agent.py`  | Assert issue content appears in prompt and "above" is gone when in problem-solving mode.                                                              |
| `tests/test_frontend_agent.py` | Same for frontend.                                                                                                                                    |


No change to `BackendInput`/`FrontendInput` or to the structure of issue dicts; only prompt assembly and wording change.