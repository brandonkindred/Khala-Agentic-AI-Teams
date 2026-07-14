"""Shared Spec Compliance citation guardrail for code-review prompts.

Used by in-process review profiles and the LLM-fallback ``build_code_review_prompt``
so Spec Compliance findings must quote real requirement text and must not invent
named entities absent from the task context.
"""

REQUIREMENT_CITATION_GUARDRAIL = (
    "You must be able to quote, verbatim, the requirement text a Spec Compliance "
    "finding is based on. Do not invent named entities (vendor names, provider "
    "names, feature names, integrations) that do not appear verbatim in the "
    "provided Requirements/Acceptance Criteria/Specification/Architecture "
    "context. If you cannot locate such a sentence, do not emit the issue."
)
