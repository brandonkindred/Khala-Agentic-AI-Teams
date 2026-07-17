"""Byte-identity guard for the Discovery/Requirements prompt split (§5).

The Planning LLM runtime (``LLMClient.complete_text``) supports only a single
prompt string. ``AGENT_ANATOMY.md`` §5 therefore has the System/User split live
in code as ``SYSTEM_PROMPT`` + ``build_user_prompt(...)``, re-joined by
``build_prompt``. These tests pin ``build_prompt(x)`` to be *byte-identical* to
the pre-split literal ``PROMPT.format(input_text=x)`` — proving the extraction
changed structure only, never the bytes sent to the model.

``_DISCOVERY_GOLDEN`` / ``_REQUIREMENTS_GOLDEN`` are verbatim copies of the
pre-refactor ``DISCOVERY_PROMPT`` / ``REQUIREMENTS_PROMPT`` module constants.
"""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

import pytest  # noqa: E402

from planning_team.agents.discovery import prompts as discovery_prompts  # noqa: E402
from planning_team.agents.requirements import prompts as requirements_prompts  # noqa: E402

# --- Golden literals: exact pre-split prompt constants ------------------------

_DISCOVERY_GOLDEN = """You are an expert product owner doing discovery for a software engagement.

Given the following client brief and/or spec, extract and structure:

1. **Problem summary**: 2-4 sentences on the core problem.
2. **Opportunity statement**: Why now, what success looks like.
3. **Target users**: List of user segments or personas (short labels).
4. **Success criteria**: 3-7 measurable or observable criteria.
5. **Technology constraints**: Technologies the brief/spec explicitly requires or mandates
   (languages, frameworks, databases, platforms, cloud/hosting). Include ONLY what is
   explicitly stated — leave this empty if the input does not name a required technology.
   Do NOT guess or infer a default stack here.

Keep each section concise. If information is missing, infer reasonable defaults and note them under "Assumptions". (This does not apply to "Technology constraints", which must stay empty unless a technology is explicitly required.)

Input:
---
{input_text}
---

Respond with JSON only (no markdown fences):
{{
  "problem_summary": "...",
  "opportunity_statement": "...",
  "target_users": ["...", "..."],
  "success_criteria": ["...", "..."],
  "tech_constraints": ["..."],
  "assumptions": ["..."]
}}
"""

_REQUIREMENTS_GOLDEN = """You are an expert product owner capturing requirements for a software engagement.

From the problem summary and opportunity below, generate 3-6 short clarification questions that a client PO would need to answer so that dev/UI/UX teams can align. Include:
- RTO/RPO or disaster recovery (if relevant)
- Deployment target (cloud/on-prem/hybrid)
- Compliance or security constraints (if any)
- Tech stack preferences (if any)

SLA defaults (for your reference): General apps often use RPO ≤ 15 min, RTO 1-2 hours; stricter for critical systems.

Input:
---
{input_text}
---

Respond with JSON only (no markdown):
{{
  "questions": [
    {{
      "id": "req_short_id",
      "question_text": "...",
      "context": "...",
      "category": "business|infrastructure|security|compliance|tech",
      "priority": "high|medium|low",
      "options": [
        {{ "id": "opt_1", "label": "...", "is_default": false }}
      ]
    }}
  ]
}}
"""

# Representative payloads: empty, plain, multiline, brace-containing (a value with
# ``{``/``}`` must survive because build_user_prompt interpolates rather than .format()s
# the payload), and the exact "Problem: .../Brief/Spec section:" shape requirements builds.
_SAMPLES = [
    "",
    "Build a dashboard.",
    "line one\nline two\nline three",
    "a value with {curly} and }unbalanced{ braces",
    "Problem: Need reports\nBrief/Spec section:\n# Spec\n\nBody",
]


@pytest.mark.parametrize("input_text", _SAMPLES)
def test_discovery_build_prompt_is_byte_identical(input_text):
    assert discovery_prompts.build_prompt(input_text) == _DISCOVERY_GOLDEN.format(
        input_text=input_text
    )


@pytest.mark.parametrize("input_text", _SAMPLES)
def test_requirements_build_prompt_is_byte_identical(input_text):
    assert requirements_prompts.build_prompt(input_text) == _REQUIREMENTS_GOLDEN.format(
        input_text=input_text
    )


@pytest.mark.parametrize("mod", [discovery_prompts, requirements_prompts])
def test_build_prompt_is_system_join_user(mod):
    """build_prompt must be exactly SYSTEM_PROMPT + "\\n\\n" + build_user_prompt(x)."""
    for s in _SAMPLES:
        assert mod.build_prompt(s) == f"{mod.SYSTEM_PROMPT}\n\n{mod.build_user_prompt(s)}"


@pytest.mark.parametrize("mod", [discovery_prompts, requirements_prompts])
def test_system_prompt_has_no_trailing_newline(mod):
    # The paragraph break before the User turn comes from build_prompt's "\n\n" join,
    # so SYSTEM_PROMPT itself must not end with a newline.
    assert mod.SYSTEM_PROMPT
    assert not mod.SYSTEM_PROMPT.endswith("\n")


@pytest.mark.parametrize("mod", [discovery_prompts, requirements_prompts])
def test_user_prompt_carries_payload_and_json_anchor(mod):
    user = mod.build_user_prompt("SENTINEL_PAYLOAD")
    assert "SENTINEL_PAYLOAD" in user  # the input_text anchor stays in the User turn
    assert user.startswith("Input:")
    assert user.rstrip().endswith("}")  # JSON output shape stays in the User turn
