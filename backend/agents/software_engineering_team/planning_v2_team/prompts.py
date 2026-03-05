"""
Prompts for planning-v2 phases and tool agent orchestration.

No reuse from planning_team. Tool agents have their own embedded prompts.
These prompts are used by the orchestrator and phase implementations.

Note: This team expects a pre-validated specification - no spec review prompts
are included. Use the Product Requirements Analysis agent for spec validation.
"""

# ---------------------------------------------------------------------------
# Phase-level prompts (used by orchestrator phases)
# ---------------------------------------------------------------------------

PLANNING_PROMPT = """You are a Product Planning expert. Using the spec and any prior review, produce a comprehensive product plan.

Respond using this EXACT format. Use the section markers exactly as shown.

## GOALS_VISION ##
The product's goals and vision statement (one short paragraph).
## END GOALS_VISION ##

## CONSTRAINTS_LIMITATIONS ##
Technical and business constraints.
## END CONSTRAINTS_LIMITATIONS ##

## KEY_FEATURES ##
- Feature 1
- Feature 2
- Feature 3
## END KEY_FEATURES ##

## MILESTONES ##
- Milestone 1 with deliverables
- Milestone 2 with deliverables
## END MILESTONES ##

## ARCHITECTURE ##
High-level architecture overview.
## END ARCHITECTURE ##

## MAINTAINABILITY ##
Code quality, testing, and maintenance considerations.
## END MAINTAINABILITY ##

## SECURITY ##
Security requirements and considerations.
## END SECURITY ##

## FILE_SYSTEM ##
Proposed file/folder structure.
## END FILE_SYSTEM ##

## STYLING ##
UI/UX styling guidelines and design system.
## END STYLING ##

## DEPENDENCIES ##
- Library 1
- Library 2
## END DEPENDENCIES ##

## MICROSERVICES ##
Microservices breakdown if applicable, or "N/A" for monolithic.
## END MICROSERVICES ##

## OTHERS ##
Additional notes, edge cases, or considerations.
## END OTHERS ##

## SUMMARY ##
Overall planning summary (one short paragraph).
## END SUMMARY ##

Spec excerpt:
---
{spec_content}
---

Prior review summary (if any): {review_summary}
"""

REVIEW_PROMPT = """You are reviewing planning assets for cohesion and alignment with the spec.

Respond using this EXACT format:

## PASSED ##
true or false
## END PASSED ##

## ISSUES ##
- Issue 1 (if any)
- Issue 2 (if any)
## END ISSUES ##

## SUMMARY ##
Brief summary of the review.
## END SUMMARY ##

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

Respond using this EXACT format:

## FIXES_APPLIED ##
- Description of fix 1
- Description of fix 2
## END FIXES_APPLIED ##

## RESOLVED ##
true or false
## END RESOLVED ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##

Review issues: {issues}
"""

PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT = """You are a planning expert fixing a specific issue in the planning artifacts.

ISSUE TO FIX:
---
{issue}
---

SPECIFICATION EXCERPT:
---
{spec_excerpt}
---

CURRENT PLANNING ARTIFACTS:
---
{current_artifacts}
---

Analyze this specific issue and provide a fix. If the issue requires updating a planning artifact file, provide the complete updated file content using the format below.

Respond using this EXACT format:

## ROOT_CAUSE ##
Explanation of why this issue exists.
## END ROOT_CAUSE ##

## FIX_DESCRIPTION ##
Description of the fix being applied.
## END FIX_DESCRIPTION ##

## RESOLVED ##
true or false
## END RESOLVED ##

## FILE_UPDATES ##
### plan/planning_team/filename.md ###
Complete updated file content here (if needed). Use a new ### path ### block for each file. Paths must be under plan/planning_team/.
### END FILE ###
## END FILE_UPDATES ##

If no file updates are needed, leave ## FILE_UPDATES ## empty and explain in FIX_DESCRIPTION.
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
