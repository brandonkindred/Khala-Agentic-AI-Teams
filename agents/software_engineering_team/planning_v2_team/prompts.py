"""
Prompts for planning-v2 phases and tool agent orchestration.

No reuse from planning_team. Tool agents have their own embedded prompts.
These prompts are used by the orchestrator and phase implementations.
"""

# ---------------------------------------------------------------------------
# Phase-level prompts (used by orchestrator phases)
# ---------------------------------------------------------------------------

SPEC_REVIEW_PROMPT = """You are a Product Requirement Analysis expert. Review the following product specification.

Perform a thorough analysis to identify:
1. Issues - Problems or inconsistencies in the specification
2. Product gaps - Missing features, requirements, or considerations
3. Open questions - Items that need clarification from the product owner. For each question, provide 2-3 reasonable answer options based on industry standards and best practices. Mark one option as the recommended default (most conservative or most reasonable choice).
4. Plan summary - A brief outline of how this product could be built

Respond with a JSON object only, no markdown, with these exact keys:
- "issues": list of strings (problems or inconsistencies identified in the spec)
- "product_gaps": list of strings (missing features or requirements)
- "open_questions": list of objects, each with:
  - "id": unique identifier (e.g., "q1", "q2", "q3")
  - "question_text": the question requiring clarification
  - "context": why this question matters for the project (1-2 sentences)
  - "options": list of 2-3 objects, each with:
    - "id": option identifier (e.g., "opt1", "opt2")
    - "label": the answer option text
    - "is_default": boolean (true for exactly one option - the recommended default)
- "plan_summary": string (brief outline of the implementation approach)
- "summary": string (one paragraph overview of the analysis)

Specification:
---
{spec_content}
---
"""

SPEC_UPDATE_PROMPT = """You are a Product Specification Writer. Your task is to update the product specification to incorporate the answers to open questions.

For each answered question, integrate the answer naturally into the specification, adding more detail and clarity where the original spec was unclear or incomplete.

IMPORTANT:
- Preserve all existing content that is still valid
- Add new sections or details based on the answers
- Make the spec more specific and actionable
- Write in clear, professional language

Current Specification:
---
{spec_content}
---

Answered Questions:
---
{answered_questions}
---

Respond with the FULL updated specification as plain text (markdown format). Include all original content plus the new details from the answered questions.
"""

PLANNING_PROMPT = """You are a Product Planning expert. Using the spec and any prior review, produce a comprehensive product plan.

Create a structured plan covering all aspects of the product. Respond with a JSON object only, with these exact keys:

- "goals_vision": string (the product's goals and vision statement)
- "constraints_limitations": string (technical and business constraints)
- "key_features": list of strings (main features to be implemented)
- "milestones": list of strings (project milestones with deliverables)
- "architecture": string (high-level architecture overview)
- "maintainability": string (code quality, testing, and maintenance considerations)
- "security": string (security requirements and considerations)
- "file_system": string (proposed file/folder structure)
- "styling": string (UI/UX styling guidelines and design system)
- "dependencies": list of strings (external libraries and dependencies)
- "microservices": string (microservices breakdown if applicable, or "N/A" for monolithic)
- "others": string (additional notes, edge cases, or considerations)
- "summary": string (overall planning summary)

Spec excerpt:
---
{spec_content}
---

Prior review summary (if any): {review_summary}
"""

REVIEW_PROMPT = """You are reviewing planning assets for cohesion and alignment with the spec.

Respond with a JSON object only:
- "passed": boolean (true if assets are cohesive and align with spec)
- "issues": list of strings (any issues found)
- "summary": string

Spec excerpt:
---
{spec_content}
---

Artifacts to review:
---
{artifacts}
---
"""

PROBLEM_SOLVING_PROMPT = """You are a problem-solving expert. Given review issues, suggest fixes.

Respond with a JSON object only:
- "fixes_applied": list of strings (description of each fix)
- "resolved": boolean
- "summary": string

Review issues: {issues}
"""

# ---------------------------------------------------------------------------
# Orchestration prompts (for coordinating tool agents)
# ---------------------------------------------------------------------------

TOOL_AGENT_COORDINATION_PROMPT = """You are coordinating multiple planning tool agents.

Current phase: {phase}
Active tool agents: {active_agents}

Spec:
---
{spec_content}
---

Prior results:
{prior_results}

Determine what each tool agent should focus on for this phase.

Respond with JSON:
{{
  "agent_instructions": [
    {{"agent": "system_design", "focus": "what to focus on"}},
    {{"agent": "architecture", "focus": "what to focus on"}}
  ],
  "summary": "coordination summary"
}}
"""

DELIVERABLES_CONSOLIDATION_PROMPT = """You are consolidating planning deliverables from multiple tool agents.

Tool agent outputs:
---
{tool_agent_outputs}
---

Create a unified summary and identify any conflicts or gaps.

Respond with JSON:
{{
  "consolidated_summary": "unified summary of all outputs",
  "conflicts": ["any conflicts between agent outputs"],
  "gaps": ["any remaining gaps"],
  "next_steps": ["recommended next steps"]
}}
"""
