"""UX/Usability tool agent for frontend-code-v2: UX design planning and usability review."""

from __future__ import annotations

from typing import Dict, List

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    lenient_json_object,
    relevant_code_for_issue,
)

from ...models import ReviewIssue, ToolAgentPhaseInput, ToolAgentPhaseOutput

MAX_RELEVANT_CODE_CHARS = 8_000

UX_DESIGNER_PLAN_PROMPT = """You are an expert UX Designer Agent. Your job is to define user flows, information architecture, interaction design, microcopy, and edge cases BEFORE pixels get involved. You ensure the app makes sense from a user perspective.

**Your expertise:**
- User journeys (happy path and sad paths)
- Wireframes and flow diagrams (describe in text)
- Interaction rules (empty states, errors, loading, success)
- Microcopy guidelines (tone, clarity, consistency)
- Edge cases and error handling from a UX perspective

**Input:**
- Task description and requirements
- Optional: spec content, architecture, user story

**Your task:**
Produce UX design artifacts that the UI Designer and Feature Implementation agents will use:

1. **User Journeys** – Describe the happy path and key sad paths (errors, empty states, validation failures). Use clear step-by-step flows.
2. **Wireframes / Flow Diagrams** – Describe the layout and flow in text (screens, key elements, navigation between them). No actual pixels; focus on structure and hierarchy.
3. **Interaction Rules** – Define rules for: empty states (what shows when no data), error states (how errors are displayed), loading states (spinners, skeletons), success states (feedback, confirmation).
4. **Microcopy Guidelines** – Tone (friendly, professional, concise), clarity rules, consistency (button labels, error messages, placeholders). Provide examples where helpful.

**Output format:**
Return a single JSON object with:
- "user_journeys": string (full user journey description: happy path + sad paths)
- "wireframes_summary": string (wireframe/flow description in text)
- "interaction_rules": string (empty, error, loading, success state rules)
- "microcopy_guidelines": string (tone, clarity, consistency guidelines)
- "summary": string (2-3 sentence summary of key UX decisions)

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Spec (excerpt):**
{spec_content}
"""

UX_ENGINEER_REVIEW_PROMPT = """You are an expert UX Engineer Agent. Your job is to focus on the feel of the product: performance perception, interaction polish, usability. You catch the stuff users notice immediately but specs rarely mention.

**Your expertise:**
- Interaction polish (focus flow, keyboard shortcuts, friction removal)
- Sensible defaults and progressive disclosure
- Usability review (what feels off, what could be smoother)
- "Delight" without being annoying (motion restraint, feedback timing)

**Input:**
- Code to review (HTML templates, TypeScript components)
- Task description

**Your task:**
Review the code for UX polish and usability. Identify issues that affect the feel of the product:

1. **Focus flow** – Is tab order logical? Are focus indicators visible? Any focus traps?
2. **Keyboard shortcuts** – Are there actions that should have shortcuts? Missing Escape to close?
3. **Friction removal** – Unnecessary clicks? Confusing flows? Could defaults be smarter?
4. **Motion/feedback** – Is feedback timing appropriate? Any jarring or missing transitions? Restraint: delight without being annoying.
5. **Progressive disclosure** – Is information revealed at the right time? Overwhelming or too hidden?

For each issue, produce a code_review-style report with a clear "recommendation" – what the coding agent should implement.

**Output format:**
Return a single JSON object with:
- "issues": list of objects, each with:
  - "severity": string (critical, major, medium, minor)
  - "category": string (focus, keyboard, usability, motion, feedback)
  - "file_path": string (file or component)
  - "description": string (what the UX problem is)
  - "recommendation": string (concrete instruction for the coding agent)
- "summary": string (overall UX polish assessment)
- "approved": boolean (true when no critical/major issues; false when polish pass is needed)

If no issues are found, return empty issues list and approved=true. Be practical – focus on issues that materially affect user experience.

Respond with valid JSON only. No explanatory text outside JSON.

---

**Task:** {task_description}

**Code to review:**
{code}
"""


def _relevant_code_for_issue(issue: ReviewIssue, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files."""
    return relevant_code_for_issue(issue, current_files, MAX_RELEVANT_CODE_CHARS)


class UxUsabilityToolAgent(BaseReviewToolAgent):
    """UX/Usability tool agent: UX design planning and usability review.

    Reports issues for the coding agent to fix. ``plan`` and ``review`` run in
    JSON mode (``_model_json``).
    """

    name = "UX"
    execute_label = "UX/Usability"
    empty_label = "UX issues"
    issue_source = "ux"
    review_prompt = UX_ENGINEER_REVIEW_PROMPT
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "json"
    uses_json_model = True
    review_model_attr = "_model_json"

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate UX design artifacts: user journeys, wireframes, interaction rules, microcopy.

        Preconditions:
            ``inp`` is a :class:`ToolAgentPhaseInput`; ``task_description`` and
            ``spec_context`` are optional strings.

        Postconditions:
            When no LLM is configured (``self._model`` is falsy), returns a
            stub plan and makes no LLM call. Otherwise formats
            ``UX_DESIGNER_PLAN_PROMPT`` with ``inp.task_description`` (falling
            back to ``"N/A"``) for ``{task_description}`` and
            ``inp.spec_context`` (falling back to ``""``) for
            ``{spec_content}``, and returns the parsed recommendations/summary;
            an LLM error or unparsable response falls back to a default
            recommendation.
        """
        if not self._model:
            return ToolAgentPhaseOutput(
                recommendations=[
                    "Consider user flows and interactions.",
                    "Define empty, error, loading, and success states.",
                    "Establish microcopy guidelines for consistency.",
                ],
                summary="UX planning stub (no LLM).",
            )
        prompt = UX_DESIGNER_PLAN_PROMPT.format(
            task_description=inp.task_description or "N/A",
            spec_content=(inp.spec_context or ""),
        )
        try:
            raw = self._run_agent(self._model_json, prompt)
        except Exception as e:
            self._logger.warning("UX plan LLM call failed: %s", e)
            return ToolAgentPhaseOutput(
                recommendations=["Consider user flows and interactions."],
                summary="UX planning failed (LLM error).",
            )
        data = lenient_json_object(
            raw,
            logger=self._logger,
            context="UX plan",
            on_fail_msg="falling back to empty plan.",
        )
        recommendations: List[str] = []
        if data.get("user_journeys"):
            recommendations.append(f"User Journeys: {data['user_journeys']}")
        if data.get("wireframes_summary"):
            recommendations.append(f"Wireframes: {data['wireframes_summary']}")
        if data.get("interaction_rules"):
            recommendations.append(f"Interaction Rules: {data['interaction_rules']}")
        if data.get("microcopy_guidelines"):
            recommendations.append(f"Microcopy: {data['microcopy_guidelines']}")
        return ToolAgentPhaseOutput(
            recommendations=recommendations
            if recommendations
            else ["Consider user flows and interactions."],
            summary=data.get("summary", "UX planning complete."),
        )
