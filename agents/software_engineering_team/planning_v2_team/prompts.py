"""Prompts for planning-v2 phases. No reuse from planning_team."""

SPEC_REVIEW_PROMPT = """You are a System Design and Architecture expert. Review the following product specification.

Identify:
1. Critical gaps (what is missing or unclear)
2. Open questions (what needs to be clarified)
3. High-level system design notes
4. High-level architecture notes

Respond with a JSON object only, no markdown, with these exact keys:
- "gaps": list of strings (critical gaps)
- "open_questions": list of strings
- "system_design_notes": string (brief)
- "architecture_notes": string (brief)
- "summary": string (one paragraph)

Specification:
---
{spec_content}
---
"""

PLANNING_PROMPT = """You are a product planning expert. Using the spec and any prior review, produce a high-level plan.

Respond with a JSON object only:
- "milestones": list of strings (high-level milestones)
- "user_stories": list of strings (short user story titles)
- "high_level_plan": string (narrative plan)
- "summary": string

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
"""

PROBLEM_SOLVING_PROMPT = """You are a problem-solving expert. Given review issues, suggest fixes.

Respond with a JSON object only:
- "fixes_applied": list of strings (description of each fix)
- "resolved": boolean
- "summary": string

Review issues: {issues}
"""
