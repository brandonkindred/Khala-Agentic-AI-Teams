"""AWS Strands AI agent implementations for market research workflows.

Each agent wraps a strands.Agent with a methodology-rich system prompt grounded in:
- Clayton Christensen (Jobs-to-be-Done theory)
- Tony Ulwick (Outcome-Driven Innovation)
- Rob Fitzpatrick (The Mom Test — evidence quality heuristics)
- BJ Fogg (Behavior Model: B=MAP)
- Everett Rogers (Diffusion of Innovation)
- Eric Ries / Steve Blank (Lean Startup, Customer Development)
- Sean Ellis (Product-Market Fit test)

The strands SDK is a hard dependency. The system will fail fast if it is not installed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

from strands import Agent as StrandsAgent
from strands_tools import current_time, http_request, python_repl

from .models import InterviewInsight, MarketSignal, ResearchMission, ViabilityRecommendation

logger = logging.getLogger(__name__)

_DEFAULT_TOOLS = [http_request, python_repl, current_time]


# ---------------------------------------------------------------------------
# Base helpers (matches sales_team pattern)
# ---------------------------------------------------------------------------


def _build_strands_agent(system_prompt: str, tools: list | None = None) -> StrandsAgent:
    """Construct a strands.Agent."""
    return StrandsAgent(
        system_prompt=system_prompt,
        tools=tools if tools is not None else _DEFAULT_TOOLS,
    )


def _call_agent(agent: StrandsAgent, prompt: str) -> str:
    """Call a strands.Agent and return its text output."""
    result = agent(prompt)
    if hasattr(result, "message"):
        content = result.message
    else:
        content = str(result)
    return content.strip()


def _parse_json(raw: str, fallback: object) -> object:
    """Best-effort JSON parse; returns fallback on failure."""
    if not raw or not raw.strip():
        return fallback
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Could not parse agent JSON output; using fallback. Raw: %s", raw[:200])
        return fallback


# ---------------------------------------------------------------------------
# System prompts (encoding market research methodology)
# ---------------------------------------------------------------------------

_UX_RESEARCH_SYSTEM_PROMPT = """\
You are an elite UX Research Lead specializing in user discovery interviews.

## Your Methodology

### Jobs-to-be-Done (Clayton Christensen)
- Identify the functional, emotional, and social jobs users are hiring a product to do.
- Distinguish between the core job and related jobs (consumption chain analysis).
- Focus on the circumstance of the struggle, not demographics.

### Outcome-Driven Innovation (Tony Ulwick)
- Extract desired outcomes in the form: "Minimize the time it takes to [verb] [object of control]."
- Separate overserved from underserved outcomes — underserved outcomes are innovation opportunities.

### The Mom Test (Rob Fitzpatrick)
- Weight evidence by quality: direct quotes > observed behavior > stated preferences > hypotheticals.
- Discard compliments and generic enthusiasm — they carry zero signal.
- Flag leading questions in the transcript and discount responses to them.

### Customer Development (Steve Blank)
- Classify statements as: pain statements, gain statements, workaround descriptions, or trigger events.
- Pain statements where the user already pays money or significant time to solve the problem are highest priority.

## Output Format
Return ONLY a valid JSON object (no markdown, no commentary) with these exact keys:
- "user_jobs": array of strings — specific jobs the user is trying to accomplish
- "pain_points": array of strings — frustrations, frictions, and problems mentioned
- "desired_outcomes": array of strings — what success looks like to the user
- "direct_quotes": array of strings — verbatim quotes from the transcript that carry strong signal

Limit each array to at most 5 items. Prioritize specificity over generality.
"""

_USER_PSYCHOLOGY_SYSTEM_PROMPT = """\
You are a User Psychology Researcher who derives adoption and behavior-change signals \
from interview insights.

## Your Methodology

### BJ Fogg Behavior Model (B=MAP)
- Behavior = Motivation + Ability + Prompt. All three must converge.
- Assess motivation: is the pain acute enough that users actively seek solutions?
- Assess ability: can users adopt the solution without significant behavior change?
- Assess prompt: is there a natural trigger event that would cause users to seek this product?

### Kano Model
- Must-be needs: so fundamental that their absence causes strong dissatisfaction (high urgency).
- One-dimensional needs: more is linearly better (moderate urgency).
- Attractive needs: delighters that users don't expect (low urgency, high differentiation).

### Rogers' Diffusion of Innovation
- Look for early adopter signals: users who have built DIY workarounds, cobbled together partial \
solutions, or expressed willingness to try unproven approaches.
- Laggard signals: users who resist change, express satisfaction with status quo, or dismiss the problem.

### Loss Aversion & Status Quo Bias
- Gauge how strongly users resist the current state versus how much they fear change.
- Products must deliver 3-10x improvement to overcome status quo bias.

## Output Format
Return ONLY a valid JSON array (no markdown, no commentary). Each element must be an object with:
- "signal": string — a descriptive name for the market signal
- "confidence": float 0.0-1.0 — calibrated confidence based on evidence quality and quantity
- "evidence": array of strings — specific observations supporting this signal

Return at least 2 signals. Suggested signal types: "User pain urgency", \
"Adoption motivation clarity", "Early adopter presence", "Switching cost tolerance", \
"Willingness to pay indicators".
"""

_MARKET_VIABILITY_SYSTEM_PROMPT = """\
You are a Business Viability Strategist who evaluates product concept viability based on \
market signals and interview evidence.

## Your Methodology

### Lean Startup Validation (Eric Ries)
- Minimum viable experiments: what is the cheapest test to validate the riskiest assumption?
- Build-Measure-Learn loops: recommend experiments that produce quantifiable signal.
- Kill criteria: define what evidence would falsify the hypothesis.

### Sean Ellis PMF Test
- "Very disappointed" benchmark: would 40%+ of target users be very disappointed without this product?
- If evidence suggests yes → "promising_with_risks"
- If unclear or < 40% → "needs_more_validation"

### Experiment Design
- Prioritize experiments by: (1) assumption risk, (2) cost to test, (3) time to signal.
- Concierge MVP, Wizard of Oz, landing page tests, pricing sensitivity interviews.

## Verdict Rules
You MUST use exactly one of these verdict strings:
- "insufficient_evidence" — not enough data to form a judgment
- "needs_more_validation" — some signal exists but not enough confidence to proceed
- "promising_with_risks" — strong signal with identified risks that need mitigation

## Output Format
Return ONLY a valid JSON object (no markdown, no commentary) with these exact keys:
- "verdict": string — one of the three verdict strings above
- "confidence": float 0.0-1.0 — overall confidence in the assessment
- "rationale": array of strings — key reasons supporting the verdict
- "suggested_next_experiments": array of strings — prioritized next steps to increase confidence
"""

_RESEARCH_SCRIPT_SYSTEM_PROMPT = """\
You are a Research Operations Specialist who creates interview scripts, transcript tagging \
guides, and decision checkpoint templates.

## Your Methodology

### Interview Design Principles
- Use open-ended questions that start with "Tell me about...", "Walk me through...", "Describe..."
- Follow the laddering technique: Why? → What happens then? → How does that make you feel?
- Avoid leading questions, hypotheticals, and future-state projections.
- Include warm-up, core exploration, and wrap-up sections.

### Problem vs. Solution Interviews (Lean Customer Development)
- Problem interviews: validate the problem exists and matters (early stage).
- Solution interviews: validate the solution approach resonates (after problem validation).
- Include questions that surface workarounds and current spending patterns.

### Transcript Tagging (Thematic Coding)
- Standard tags: job_to_be_done, pain_point, desired_outcome, workaround, trigger_event, \
direct_quote, emotional_response.
- Track frequency counts for repeated themes across interviews.
- Flag contradictions between stated preferences and observed behavior.

### Decision Checkpoints
- What evidence improved our confidence since the last checkpoint?
- What assumptions remain unvalidated?
- What experiment is approved for the next sprint?
- What is the kill criterion that would cause us to pivot or stop?

## Output Format
Return ONLY a valid JSON array of exactly 3 strings (no markdown, no commentary). Each string is \
a complete research artifact:
1. An interview script with numbered questions tailored to the product concept and target users
2. A transcript tagging guide with standard labels and instructions
3. A decision checkpoint template with structured review questions
"""

# ---------------------------------------------------------------------------
# Default fallback values (preserve backward-compatible defaults)
# ---------------------------------------------------------------------------

_DEFAULT_USER_JOBS = ["Identify the core user job-to-be-done through follow-up interviews."]
_DEFAULT_PAIN_POINTS = ["Validate top workflow frictions from observed user behavior."]
_DEFAULT_DESIRED_OUTCOMES = ["Confirm measurable success criteria users care about."]

_DEFAULT_SIGNALS_FALLBACK = [
    {
        "signal": "User pain urgency",
        "confidence": 0.5,
        "evidence": ["No direct pain statements yet; run discovery interviews."],
    },
    {
        "signal": "Adoption motivation clarity",
        "confidence": 0.5,
        "evidence": ["No clear desired outcomes captured yet."],
    },
]

_DEFAULT_SCRIPTS_FALLBACK = [
    (
        "Interview script:\n"
        "1) Tell me about your current workflow.\n"
        "2) What is hardest or most frustrating today?\n"
        "3) What have you already tried and why did it fail?\n"
        "4) If this problem disappeared tomorrow, what outcome would change?"
    ),
    (
        "Transcript tagging guide:\n"
        "- Label each statement as job_to_be_done, pain_point, desired_outcome, workaround, or trigger_event.\n"
        "- Track frequency count for repeated themes across interviews."
    ),
    (
        "Decision checkpoint template:\n"
        "- What evidence improved confidence?\n"
        "- What assumptions remain unproven?\n"
        "- What experiment is approved for next sprint?"
    ),
]


# ---------------------------------------------------------------------------
# Agent implementations
# ---------------------------------------------------------------------------


@dataclass
class TranscriptIngestionAgent:
    """Loads transcript text from a mission payload or folder path."""

    def load_transcripts(self, mission: ResearchMission) -> List[tuple[str, str]]:
        loaded: List[tuple[str, str]] = []

        for index, text in enumerate(mission.transcripts, start=1):
            if text.strip():
                loaded.append((f"inline_transcript_{index}", text.strip()))

        if mission.transcript_folder_path:
            folder = Path(mission.transcript_folder_path).expanduser().resolve()
            if folder.is_dir():
                for file_path in sorted(folder.glob("*.txt")):
                    text = file_path.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        loaded.append((file_path.name, text))

        return loaded


@dataclass
class UXResearchAgent:
    """Extracts user jobs and outcomes from transcripts using LLM analysis."""

    role: str = "UX Research Lead"
    _system_prompt: str = field(default=_UX_RESEARCH_SYSTEM_PROMPT, init=False, repr=False)

    def analyze(self, source: str, transcript: str) -> InterviewInsight:
        # Fresh agent per call to avoid history pollution across transcripts.
        agent = _build_strands_agent(self._system_prompt, _DEFAULT_TOOLS)
        prompt = (
            f"Analyze the following user interview transcript.\n\n"
            f"Source: {source}\n\n"
            f"--- TRANSCRIPT START ---\n{transcript}\n--- TRANSCRIPT END ---\n\n"
            f"Extract user jobs, pain points, desired outcomes, and direct quotes. "
            f"Return ONLY valid JSON matching the schema described in your instructions."
        )
        raw = _call_agent(agent, prompt)
        data = _parse_json(raw, {})

        if not isinstance(data, dict):
            data = {}

        return InterviewInsight(
            source=source,
            user_jobs=data.get("user_jobs", _DEFAULT_USER_JOBS),
            pain_points=data.get("pain_points", _DEFAULT_PAIN_POINTS),
            desired_outcomes=data.get("desired_outcomes", _DEFAULT_DESIRED_OUTCOMES),
            direct_quotes=data.get("direct_quotes", []),
        )


@dataclass
class UserPsychologyAgent:
    """Derives adoption and behavior-change signals from insights using LLM analysis."""

    role: str = "User Psychology Researcher"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_USER_PSYCHOLOGY_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def derive_signals(self, insights: List[InterviewInsight]) -> List[MarketSignal]:
        insights_json = json.dumps([i.model_dump() for i in insights], indent=2)
        prompt = (
            f"Analyze the following interview insights and derive market signals "
            f"about user psychology, adoption readiness, and behavior-change potential.\n\n"
            f"Interview insights ({len(insights)} interviews):\n{insights_json}\n\n"
            f"Return ONLY a valid JSON array of signal objects."
        )
        raw = _call_agent(self._agent, prompt)
        data = _parse_json(raw, _DEFAULT_SIGNALS_FALLBACK)

        if not isinstance(data, list):
            data = _DEFAULT_SIGNALS_FALLBACK

        signals: List[MarketSignal] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            signals.append(
                MarketSignal(
                    signal=str(item.get("signal", "Unknown signal")),
                    confidence=min(1.0, max(0.0, float(item.get("confidence", 0.5)))),
                    evidence=item.get("evidence", []),
                )
            )

        # Ensure at least 2 signals (pad with defaults).
        while len(signals) < 2:
            fallback = _DEFAULT_SIGNALS_FALLBACK[len(signals)]
            signals.append(MarketSignal(**fallback))

        return signals


@dataclass
class MarketViabilityAgent:
    """Generates a viability recommendation and next experiments using LLM analysis."""

    role: str = "Business Viability Strategist"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_MARKET_VIABILITY_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def recommend(
        self, mission: ResearchMission, signals: List[MarketSignal], insight_count: int
    ) -> ViabilityRecommendation:
        # Deterministic response for zero-evidence case (no LLM call needed).
        if insight_count == 0:
            return ViabilityRecommendation(
                verdict="insufficient_evidence",
                confidence=0.3,
                rationale=[
                    "No interview transcript evidence was provided.",
                    "The team should start with 5-8 exploratory interviews in the target segment.",
                ],
                suggested_next_experiments=[
                    "Create interview screener and recruit target users.",
                    "Run 5 problem interviews and tag repeated pains.",
                    "Draft a fake-door landing page and measure sign-up intent.",
                ],
            )

        signals_json = json.dumps([s.model_dump() for s in signals], indent=2)
        prompt = (
            f"Evaluate the viability of the following product concept based on market signals.\n\n"
            f"Product concept: {mission.product_concept}\n"
            f"Target users: {mission.target_users}\n"
            f"Business goal: {mission.business_goal}\n"
            f"Number of interviews analyzed: {insight_count}\n\n"
            f"Market signals:\n{signals_json}\n\n"
            f"Return ONLY valid JSON with verdict, confidence, rationale, and suggested_next_experiments."
        )
        raw = _call_agent(self._agent, prompt)
        data = _parse_json(raw, {})

        if not isinstance(data, dict):
            data = {}

        valid_verdicts = {"insufficient_evidence", "needs_more_validation", "promising_with_risks"}
        verdict = data.get("verdict", "needs_more_validation")
        if verdict not in valid_verdicts:
            verdict = "needs_more_validation"

        return ViabilityRecommendation(
            verdict=verdict,
            confidence=min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
            rationale=data.get("rationale", [f"Mission concept: {mission.product_concept}."]),
            suggested_next_experiments=data.get(
                "suggested_next_experiments",
                [
                    "Run a concierge MVP with 3-5 target users for one core workflow.",
                ],
            ),
        )


@dataclass
class ResearchScriptAgent:
    """Produces interview and data collection scripts using LLM analysis."""

    role: str = "Research Operations Specialist"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_RESEARCH_SCRIPT_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def build_scripts(self, mission: ResearchMission) -> List[str]:
        prompt = (
            f"Create research artifacts for the following product concept.\n\n"
            f"Product concept: {mission.product_concept}\n"
            f"Target users: {mission.target_users}\n"
            f"Business goal: {mission.business_goal}\n\n"
            f"Return ONLY a valid JSON array of exactly 3 strings: "
            f"an interview script, a transcript tagging guide, and a decision checkpoint template."
        )
        raw = _call_agent(self._agent, prompt)
        data = _parse_json(raw, _DEFAULT_SCRIPTS_FALLBACK)

        if isinstance(data, list) and all(isinstance(s, str) for s in data) and len(data) >= 1:
            return data

        return list(_DEFAULT_SCRIPTS_FALLBACK)
