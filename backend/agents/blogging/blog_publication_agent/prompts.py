"""
Prompts for the blog publication agent (rejection feedback collection).
"""

REJECTION_FOLLOW_UP_PROMPT = """You are helping an author improve a blog post draft. The author has rejected the draft and provided feedback. Your job is to determine whether we have enough detail to revise the draft, or if we need to ask follow-up questions to clarify what they want changed.

**Author's feedback so far:**
{feedback_collected}

**Author's latest feedback:**
{latest_feedback}

**Guidelines:**
- If the feedback is vague (e.g. "I don't like the tone", "make it shorter", "it's not quite right"), ask 1–3 specific follow-up questions to understand exactly what to change.
- If the feedback is specific and actionable (e.g. "shorten paragraph 2", "add a code example for the auth section", "the intro hook is weak"), we may have enough.
- Ask about: which sections, what tone changes, what to add/remove, target length, examples they want, etc.
- Once we have enough concrete, actionable feedback, set ready_to_revise to true.

**Output format**

Return a single JSON object with exactly these keys:
- "ready_to_revise": boolean – true if we have enough detail to revise the draft; false if we need more.
- "questions": list of strings – 1–3 follow-up questions for the author. Empty if ready_to_revise is true.
- "feedback_summary": string – a concise summary of all collected feedback for the revision (when ready_to_revise).

Respond with valid JSON only. No explanatory text, markdown, or code fences."""

CONVERT_FEEDBACK_TO_EDITOR_PROMPT = """You are converting an author's free-form feedback into structured copy editor feedback items. The author has rejected a blog post draft and provided specific feedback. Convert this into FeedbackItems that the copy editor and draft agents can use.

**Author's collected feedback:**
{feedback}

**Output format**

Return a single JSON object with exactly these keys:
- "feedback_items": list of objects, each with:
  - "category": string – one of "voice", "style", "clarity", "structure", "technical", "formatting"
  - "severity": string – "must_fix" (author explicitly requested), "should_fix" (strong suggestion), or "consider"
  - "location": string or null – where in the draft (e.g. "paragraph 2", "intro", "conclusion")
  - "issue": string – clear description of the issue from the author's perspective
  - "suggestion": string or null – specific suggested fix based on what the author wants

Every piece of author feedback should become at least one feedback item. Be specific and actionable. Prioritize must_fix for explicit requests.

Respond with valid JSON only. No explanatory text, markdown, or code fences."""
