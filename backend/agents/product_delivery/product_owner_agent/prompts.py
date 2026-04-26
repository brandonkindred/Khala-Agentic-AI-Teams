"""Prompt templates for the ProductOwnerAgent.

The agent asks the model for *scoring inputs* (cost-of-delay components,
RICE components) — never for the score itself. Scores are computed by
the deterministic functions in :mod:`product_delivery.scoring` so a
re-groom over the same backlog produces consistent, auditable numbers.
"""

from __future__ import annotations

SYSTEM_PROMPT = (
    "You are an experienced Product Owner. You estimate the value, urgency, "
    "and effort of stories so that the deterministic WSJF / RICE formulas "
    "below can rank them. You never invent the final score — you only "
    "produce the inputs the formulas consume."
)


WSJF_INSTRUCTIONS = """
For each story, return WSJF input components on a 1..10 scale (10 = highest):

- user_business_value: customer or business value if shipped today
- time_criticality: how quickly this loses value if delayed (regulation,
  market window, dependency)
- risk_reduction_or_opportunity_enablement: how much it de-risks future
  work or unlocks new capability
- job_size: relative effort (1 = trivial, 10 = multi-sprint epic). Use
  the story's estimate_points if present; otherwise infer from scope.

Also include a one-sentence rationale.
"""


RICE_INSTRUCTIONS = """
For each story, return RICE input components:

- reach: people impacted per quarter (raw count, e.g. 5000)
- impact: 0.25 (minimal) | 0.5 (low) | 1 (medium) | 2 (high) | 3 (massive)
- confidence: 0..1 (0.5 = medium confidence). Lower this when the value
  estimate relies on speculation.
- effort: person-months (use estimate_points / 4 if estimate present,
  else infer)

Also include a one-sentence rationale.
"""


def build_user_prompt(method: str, stories_payload: str) -> str:
    instructions = WSJF_INSTRUCTIONS if method == "wsjf" else RICE_INSTRUCTIONS
    return (
        f"Score the following backlog stories using {method.upper()}.\n"
        f"{instructions}\n"
        "Respond as a JSON object with the shape:\n"
        '  {"items": [{"id": "<story_id>", "inputs": {...}, "rationale": "..."}]}\n\n'
        "Only include stories present in the input. Do not invent ids.\n\n"
        f"Stories:\n{stories_payload}"
    )
